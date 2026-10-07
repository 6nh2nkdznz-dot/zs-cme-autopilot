"""答题引擎。

设计前提（都是实测得出的，不是推测）：

1. **通用搜索引擎搜题干，往往搜不到答案。**
   实测：搜「老年认知症患者最常见的痴呆类型是」，Bing 返回的是
   「老年人」百科、WHO 老龄健康页这类**泛化结果**，里面没有
   「答案：X」这种标记。而且百度百科等站点直接 403。
   → 所以「联网搜答案」只能作为**多路证据之一**，不能当主力。

2. **课程配的题库才是准确率最高的来源。**
   同一门课的题是固定的，做过一次就能记住。

3. **答错不扣分、可重复做**（已确认）。
   → 第一遍可以「记录 + 让人确认」，把题库喂起来；
     之后同一门课重做时全部命中本地题库，零人工。

因此本模块的核心是**本地题库 + 人工确认闭环**，联网搜索是辅助：

    ┌─ 本地题库命中 ──────────────→ 直接用（零错误）
    │
    ├─ 多路联网证据 + 投票 ────────→ 置信度达标就用，并入库
    │
    └─ 都不确定 ──→ 挂起（截图+题干落盘）→ 人工作答 → 入库
                                              ↑
                                     下次同题直接命中

挂起/恢复是「可中断」的：程序不用一直挂着等人，可以退出，
下次运行读 pending 文件继续。这样课间、隔天都能补。

题型支持：单选 / 多选 / 判断。填空与简答无法可靠自动作答，
一律走人工。
"""

from __future__ import annotations

import difflib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

QuizType = Literal["single", "multi", "judge", "blank", "unknown"]

# 置信度阈值。低于此值一律转人工——答题有及格线，猜错会把课挂掉。
DEFAULT_MIN_CONFIDENCE = 0.75

# 多选题因为「字母→选项」映射不可靠，额外打这个折扣
MULTI_PENALTY = 0.85


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Option:
    """一个选项。label 是 A/B/C/D（判断题是 A/B 或 对/错），text 是正文。"""
    label: str
    text: str


#: 题干尾部「（单选题 5分）」这类标记。
#: 两个页面读到的格式不同——
#:     考核结果页：1、脓毒症患者…（单选题 5分）
#:     答题页    ：1、脓毒症患者…(5分)
#: 差别是「单选题」三个字和空格。题库 key 必须跨页面一致，
#: 否则从结果页采集的答案在重做时匹配不上。这是实测踩到的坑。
_QTYPE_TAIL_RE = re.compile(
    r"[（(]\s*(?:单选题|多选题|判断题|填空题|问答题)?\s*\d+\s*分\s*[)）]\s*$"
)

#: 平台在题干后粘的对错标记（❌/✅ 被 OCR 成 X/×/√ 等）
_MARK_TAIL_RE = re.compile(r"[\sXx×✗✘√✓✔❌✅*※]+$")


@dataclass
class Question:
    """一道题。"""
    stem: str
    options: list[Option] = field(default_factory=list)
    qtype: QuizType = "unknown"

    def normalized_stem(self) -> str:
        """归一化题干，用作题库 key。

        必须做到**跨页面一致**，因为同一个题在两个页面上的写法不同：

        * 考核结果页：`1、脓毒症患者…（单选题 5分）❌`
        * 答题页    ：`1、脓毒症患者…(5分)`

        所以要依次去掉：尾部对错标记 → 尾部「（题型 N分）」→ 题号 →
        空白与标点。少做一步，从结果页采集的官方答案在重做时就命中不了。
        """
        s = self.stem or ""
        s = _MARK_TAIL_RE.sub("", s)
        s = _QTYPE_TAIL_RE.sub("", s)
        s = re.sub(r"^\s*[（(]?\d+[）)]?[.、,，]?\s*", "", s)
        s = re.sub(r"[\s\u3000]+", "", s)
        s = re.sub(r"[（）()【】\[\]「」『』]", "", s)
        return s.strip()

    def cache_key(self) -> str:
        return self.normalized_stem()

    def option_by_label(self, label: str) -> Option | None:
        want = (label or "").strip().upper()
        for o in self.options:
            if o.label.strip().upper() == want:
                return o
        return None


@dataclass
class Answer:
    """一次作答结果。

    `labels` 是**当前这一次页面上的**选项字母。
    `texts` 是答案对应的选项**正文**——平台会打乱选项顺序（实测确认），
    所以字母只在当次有效，跨次复用必须靠正文。
    """
    labels: list[str]
    confidence: float
    source: Literal["cache", "web", "human", "none", "guess"]
    evidence: str = ""
    votes: dict[str, int] = field(default_factory=dict)
    texts: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.labels) and self.confidence > 0


#: 选项正文里可能残留的标签前缀，形如「A.」「B、」「T．」
_OPT_LABEL_PREFIX_RE = re.compile(r"^\s*(?:[A-Fa-f]|[TF])\s*[.、．，,)）:：]\s*")


def _norm_option_text(s: str) -> str:
    """归一化选项正文，用于跨来源比较。

    为什么必须做：同样的选项正文在不同来源格式不同——

    * OCR 读到的整行自带标签：`T.正确` / `A.去甲肾上腺素`
    * 题库里存的是剥掉标签的：`正确` / `去甲肾上腺素`

    不归一化的话，`match_labels_by_text` 会拿 `正确` 去比 `T.正确`，
    相似度不够 → 判定「匹配不到当前选项」→ 明明题库命中了却用不上。
    实测踩过：模糊命中 0.960，却因为这一层格式差而转人工。
    """
    s = (s or "").strip()
    s = _OPT_LABEL_PREFIX_RE.sub("", s)
    return re.sub(r"[\s\u3000]+", "", s)


def match_labels_by_text(
    want_texts: list[str],
    options: list["Option"],
    threshold: float = 0.86,
) -> list[str]:
    """把「答案正文」映射回**当前页面**的选项字母。

    ## 为什么必须这么做

    实测：同一个平台、同一道题，两次考核的选项顺序**不一样**。

    ```
    第 1 次  2、脓毒症…  A.体温 B.电解质   C.凝血功能 D.微循环指标
    第 2 次  2、脓毒症…  A.体温 B.凝血功能 C.电解质   D.微循环指标
    ```

    所以从结果页采到的「正确答案：C」只在**那一次**是 C；
    换个顺序 C 就变成别的选项了。按字母复现会答错。

    正确做法：把答案按**正文**存下来，答题时在当前页面上找正文最接近的
    那个选项，用它的字母。

    ## 归一化

    比较前两边都过 `_norm_option_text` 剥掉可能残留的标签前缀
    （`T.正确` → `正确`），否则跨来源比对会失败。

    返回匹配到的字母列表；某个答案找不到足够接近的选项时**整体放弃**
    （返回空），宁可挂起也不要答错。
    """
    if not want_texts or not options:
        return []

    got: list[str] = []
    used: set[str] = set()

    for want in want_texts:
        want_n = _norm_option_text(want)
        if not want_n:
            return []
        best_label = None
        best_score = 0.0
        for o in options:
            if o.label in used:
                continue
            score = option_match_score(_norm_option_text(o.text), want_n)
            if score > best_score:
                best_score = score
                best_label = o.label
        if best_label is None or best_score < threshold:
            return []          # 有一个对不上就整体放弃，避免半对半错
        used.add(best_label)
        got.append(best_label)

    return got


# --------------------------------------------------------------------------
# 本地题库
# --------------------------------------------------------------------------

class AnswerCache:
    """本地题库。一次做对，之后同题永久命中。

    结构：
        {
          "<归一化题干>": {
             "stem": 原始题干,
             "options": [{"label":..,"text":..}],
             "qtype": "single",
             "labels": ["A"],
             "source": "human" | "web",
             "hits": 3,
             "saved_at": "2026-10-06 20:00:00"
          }
        }

    **查询策略：精确优先，模糊兜底。**

    只做精确匹配是不够的——OCR 会抖。实测例子：

        题库里（采集时）：11、中静脉压可以准确反映右室前负荷
        答题页（重做时）：11、中心静脉压可以准确反映右心室前负荷

    漏了「心」字就精确匹配不上。所以精确未命中时退到模糊匹配，
    用相似度找最接近的一条。医学题干通常足够长，模糊匹配的误判风险很低；
    短题干（< MIN_FUZZY_LEN）不做模糊匹配，避免「体温」这种短词乱配。
    """

    #: 低于这个长度的题干不做模糊匹配（太短容易误配）
    MIN_FUZZY_LEN = 8
    #: 模糊匹配的相似度门槛
    FUZZY_THRESHOLD = 0.90

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        self.load()

    # --- 读写 ---

    def load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self._data = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            # 题库损坏不该让脚本挂掉，但必须让人知道
            backup = self.path.with_suffix(".corrupt.json")
            try:
                self.path.replace(backup)
                print(f"[cache] 题库解析失败，已备份到 {backup}，本次以空题库启动")
            except OSError:
                print("[cache] 题库解析失败且备份失败，本次以空题库启动")
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # --- 查询 ---

    def get(self, q: Question) -> Answer | None:
        entry = self._get_entry(q)
        if entry is None:
            return None

        labels = entry.get("labels") or []
        if not labels:
            return None

        # 题型校验：多选题只存了一个答案，说明当初就存错了
        if q.qtype == "multi" and len(labels) < 2:
            print(f"[cache] 命中但多选题答案数不足({len(labels)})，忽略该记录")
            return None

        # ---- 关键：按正文把答案映射回**当前页面**的字母 ----
        #
        # 平台会打乱选项顺序（实测确认），所以库里存的字母只在采集那次有效。
        # 库里同时存了答案的正文（answer_texts），用它去匹配当前页面的选项。
        want_texts = entry.get("answer_texts") or []
        if want_texts and q.options:
            remapped = match_labels_by_text(want_texts, q.options)
            if not remapped:
                print(f"[cache] 命中但按正文匹配不到当前选项，转人工: "
                      f"{q.stem[:30]}")
                return None
            if remapped != labels:
                print(f"[cache] 选项顺序变了，按正文重映射 "
                      f"{''.join(labels)} → {''.join(remapped)}")
            labels = remapped
        elif want_texts and not q.options:
            # 没读到选项就没法重映射，只能用原字母（判断题这类问题不大）
            pass

        entry["hits"] = int(entry.get("hits", 0)) + 1
        self.save()
        return Answer(list(labels), 1.0, "cache", entry.get("evidence", ""),
                      texts=list(want_texts))

    def _get_entry(self, q: Question) -> dict | None:
        """先精确，后模糊。返回题库条目。"""
        key = q.cache_key()
        entry = self._data.get(key)
        if entry is not None:
            return entry

        # 精确未命中 → 模糊兜底（OCR 抖动）
        if len(key) < self.MIN_FUZZY_LEN:
            return None

        best_key = ""
        best_score = 0.0
        for stored_key in self._data:
            if len(stored_key) < self.MIN_FUZZY_LEN:
                continue
            # 长度差太多就不必算了，先做个廉价剪枝
            if abs(len(stored_key) - len(key)) > max(3, len(key) // 5):
                continue
            score = difflib.SequenceMatcher(None, key, stored_key).ratio()
            if score > best_score:
                best_score = score
                best_key = stored_key

        if best_score >= self.FUZZY_THRESHOLD and best_key:
            print(f"[cache] 模糊命中 (相似度 {best_score:.3f})")
            return self._data.get(best_key)
        return None

    def put(self, q: Question, labels: Iterable[str], source: str,
            evidence: str = "", answer_texts: Iterable[str] | None = None) -> None:
        """写入一条答案。

        `answer_texts` 是答案对应的选项**正文**。强烈建议提供——
        平台会打乱选项顺序，只有正文才能跨次复用（见 match_labels_by_text）。
        不提供时会尝试用当前 q.options 反查字母对应的正文。
        """
        labels = [str(x).strip().upper() for x in labels if str(x).strip()]
        if not labels:
            return

        # 没显式给正文就自己从选项里反查
        texts = [str(t) for t in (answer_texts or []) if str(t).strip()]
        if not texts and q.options:
            by_label = {o.label.strip().upper(): o.text for o in q.options}
            texts = [by_label.get(lb, "") for lb in labels]
            texts = [t for t in texts if t]

        self._data[q.cache_key()] = {
            "stem": q.stem,
            "options": [{"label": o.label, "text": o.text} for o in q.options],
            "qtype": q.qtype,
            "labels": labels,
            "answer_texts": texts,
            "source": source,
            "evidence": evidence[:800],
            "hits": 0,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.save()

    def stats(self) -> dict[str, int]:
        kinds: dict[str, int] = {}
        for entry in self._data.values():
            src = str(entry.get("source", "?"))
            kinds[src] = kinds.get(src, 0) + 1
        kinds["__total__"] = len(self._data)
        return kinds

    def __len__(self) -> int:
        return len(self._data)


# --------------------------------------------------------------------------
# 相似度与打分
# --------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000，。、；：（）()【】\[\]\"'’“”-]", "", s or "")


def option_match_score(option_text: str, evidence: str) -> float:
    """选项正文与证据文本的相似度。

    用来把「答案字母」映射回「本站选项」——各题库站的选项顺序不保证一致，
    所以必须比对**文字**而不是只看字母。
    """
    a, b = _norm(option_text), _norm(evidence)
    if not a or not b:
        return 0.0
    if a in b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def score_answer(q: Question, labels: list[str], evidence: str) -> float:
    """给候选答案打分（0~1）。

    多选题刻意更严格：必须每个被选中的选项都能在证据里找到对应文字，
    否则说明「字母→选项」映射不可靠，直接压到阈值以下。
    """
    if not labels or not q.options:
        return 0.0

    scores: list[float] = []
    for lb in labels:
        opt = q.option_by_label(lb)
        if opt is None:
            return 0.0  # 答案里的字母本站根本没有 → 不可信
        scores.append(option_match_score(opt.text, evidence))

    if not scores:
        return 0.0

    weakest = min(scores)
    if q.qtype == "multi":
        return weakest * MULTI_PENALTY
    return sum(scores) / len(scores)


# --------------------------------------------------------------------------
# 联网取证
# --------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_FETCH_TIMEOUT = 15

# 各题库/问答站的答案标记形态
_ANSWER_PATTERNS = [
    r"答案[是为：:\s]*([A-Da-d]{1,4})(?![A-Za-z])",
    r"正确答案[是为：:\s]*([A-Da-d]{1,4})(?![A-Za-z])",
    r"参考答案[是为：:\s]*([A-Da-d]{1,4})(?![A-Za-z])",
    r"【答案】[：:\s]*([A-Da-d]{1,4})(?![A-Za-z])",
]


def _fetch(url: str, timeout: int = _FETCH_TIMEOUT) -> str:
    """只读 GET。不提交任何表单，不登录。"""
    headers = {
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    }
    try:
        import requests  # type: ignore

        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        # 中文站常见 GBK，requests 有时猜错
        if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except ImportError:
        from urllib.request import Request, urlopen

        req = Request(url, headers=headers)
        with urlopen(req, timeout=timeout) as fh:
            raw = fh.read()
        for enc in ("utf-8", "gbk", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&[a-z]{2,8};", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _search_urls_bing(query: str, limit: int) -> list[str]:
    from urllib.parse import quote_plus

    html = _fetch(f"https://www.bing.com/search?q={quote_plus(query)}")
    hits = re.findall(r'<h2[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', html)
    return [u for u in hits if "bing.com" not in u][:limit]


def _search_urls_baidu(query: str, limit: int) -> list[str]:
    """百度结果是跳转链接，这里只取直链形态的，避免解析跳转参数。"""
    from urllib.parse import quote_plus

    html = _fetch(f"https://www.baidu.com/s?wd={quote_plus(query)}")
    hits = re.findall(r'<h3[^>]*>\s*<a[^>]+href="(https?://[^"]+)"', html)
    out: list[str] = []
    for u in hits:
        if "baidu.com/link" in u or "baidu.com/s" in u:
            continue  # 跳转链接，跳过
        out.append(u)
    return out[:limit]


# 题库站的特征词：命中的页面更可能包含「答案：X」
_BANK_HINTS = ("答案", "题库", "试题", "考试", "习题", "单选", "多选", "参考答案")


def gather_evidence(q: Question, limit_pages: int = 6) -> list[tuple[str, str]]:
    """多路取证据，返回 [(来源URL, 正文文本), ...]。

    注意：实测通用搜索对「通用措辞的题干」效果很差（搜到的常是泛化资料页），
    所以这里只负责**尽量多取**，可信度交给上层投票与打分。
    """
    stem = q.normalized_stem()
    if not stem:
        return []

    # 用几种提问方式，提高命中题库页的概率
    queries = [
        f"{stem} 答案",
        f"{stem} 正确答案",
        f'"{stem}"',
    ]

    urls: list[str] = []
    seen: set[str] = set()

    for query in queries:
        for fn in (_search_urls_bing, _search_urls_baidu):
            try:
                for u in fn(query, limit_pages):
                    if u not in seen:
                        seen.add(u)
                        urls.append(u)
            except Exception:
                continue
        if len(urls) >= limit_pages * 2:
            break

    # 题库站优先抓（更可能带答案标记）
    def priority(u: str) -> int:
        return 0 if any(h in u for h in ("tiku", "exam", "shiti", "ask", "wenda")) else 1

    urls.sort(key=priority)

    pages: list[tuple[str, str]] = []
    for u in urls[:limit_pages]:
        try:
            text = _strip_html(_fetch(u))
        except Exception:
            continue
        if len(text) < 200:
            continue
        pages.append((u, text[:40000]))
    return pages


def extract_labels(q: Question, text: str) -> list[str]:
    """从一段文本里抽「答案：X」并映射成选项字母。"""
    if not q.options or not text:
        return []
    valid = {o.label.strip().upper() for o in q.options}

    for pat in _ANSWER_PATTERNS:
        for m in re.finditer(pat, text):
            letters: list[str] = []
            for ch in m.group(1):
                up = ch.upper()
                if up in valid and up not in letters:
                    letters.append(up)
            if letters:
                return letters
    return []


def vote_from_pages(q: Question, pages: list[tuple[str, str]]
                    ) -> tuple[list[str], float, dict[str, int], str]:
    """让多个页面给答案投票。

    返回 (最佳答案, 置信度, 票数表, 证据摘要)。
    """
    votes: dict[str, int] = {}
    best_evidence = ""

    for url, text in pages:
        labels = extract_labels(q, text)
        if not labels:
            continue
        key = "".join(sorted(labels))
        votes[key] = votes.get(key, 0) + 1
        if not best_evidence:
            # 留一小段上下文作为证据
            idx = text.find("答案")
            best_evidence = (text[max(0, idx - 60): idx + 120] if idx >= 0
                             else text[:180])

    if not votes:
        return [], 0.0, {}, ""

    # 票数最多的答案；平票时取置信度高的
    def rank(item: tuple[str, int]) -> tuple[int, float]:
        key, cnt = item
        labels = list(key)
        conf = score_answer(q, labels, best_evidence)
        return (cnt, conf)

    best_key, cnt = max(votes.items(), key=rank)
    labels = list(best_key)

    base = score_answer(q, labels, best_evidence)
    # 只有一票时不额外加权；多页一致则加成
    agreement = min(1.0, 0.7 + 0.15 * (cnt - 1))
    return labels, min(base * agreement + (0.05 if cnt > 1 else 0.0), 1.0), votes, best_evidence


# --------------------------------------------------------------------------
# 挂起 / 恢复（人工确认闭环）
# --------------------------------------------------------------------------

PENDING_DIRNAME = "pending"
PENDING_FILE = "questions.jsonl"


def _pending_path(debug_dir: Path) -> Path:
    d = debug_dir / PENDING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / PENDING_FILE


def suspend(q: Question, debug_dir: Path, note: str = "") -> None:
    """把题挂起等人作答。追加写，不覆盖——同一题重复挂起也能看出频次。"""
    path = _pending_path(debug_dir)
    record = {
        "key": q.cache_key(),
        "stem": q.stem,
        "qtype": q.qtype,
        "options": [{"label": o.label, "text": o.text} for o in q.options],
        "note": note,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_pending(debug_dir: Path) -> list[dict]:
    """读回挂起的题（去重，保留最后一次）。"""
    path = debug_dir / PENDING_DIRNAME / PENDING_FILE
    if not path.is_file():
        return []
    out: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("key"):
            out[rec["key"]] = rec
    return list(out.values())


def resolve_pending(debug_dir: Path, answers: dict[str, list[str]],
                    cache: AnswerCache) -> int:
    """把人工答案写进题库，并从挂起列表移除。

    answers: {cache_key: ["A"] 或 ["A","C"]}
    返回成功入库的数量。
    """
    if not answers:
        return 0

    n = 0
    for rec in load_pending(debug_dir):
        labels = answers.get(rec["key"])
        if not labels:
            continue
        q = Question(
            stem=rec.get("stem", ""),
            options=[Option(o["label"], o["text"]) for o in rec.get("options", [])],
            qtype=rec.get("qtype", "unknown"),
        )
        cache.put(q, labels, source="human")
        n += 1

    # 重写挂起文件，只留还没回答的
    remaining = [r for r in load_pending(debug_dir) if r["key"] not in answers]
    path = _pending_path(debug_dir)
    if remaining:
        with path.open("w", encoding="utf-8") as fh:
            for r in remaining:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    else:
        path.unlink(missing_ok=True)

    return n


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def resolve(
    q: Question,
    cache: AnswerCache,
    debug_dir: Path,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    allow_web: bool = True,
    guess_when_unsure: bool = False,
) -> tuple[Answer, bool]:
    """解题。返回 (答案, 是否需要人工)。

    需要人工时，题目已经被挂起到 debug/pending/questions.jsonl，
    调用方只需**不要提交**并继续下一题（或停下）。

    ## guess_when_unsure：没把握时先随便选

    平台规则是「客观题答错不扣分、可重复提交」，而且**交卷后的结果页会
    逐题给出官方正确答案**。所以在「跑通考核」这个目标下，最划算的策略是：

        没把握 → 先随便选一个 → 交卷 → 从结果页采集官方答案 → 重做

    这样一轮就能把答案补齐，比挂起等人作答快得多（后者要求全程有人盯着）。

    开启后：题库和联网都没命中时不再挂起，而是返回一个「先选第一个选项」
    的低置信度答案。**采集环节会把它变成正确答案**，所以不会一直错下去。

    默认关闭——因为「挂起等人」在做题库积累时更安全，不会把错答案
    当成真答案写进缓存。这里返回的答案**不会**入库（source="guess"）。
    """
    # 1) 本地题库
    hit = cache.get(q)
    if hit is not None:
        print(f"[quiz] 题库命中: {q.stem[:36]} -> {hit.labels}")
        return hit, False

    # 无法可靠自动作答的题型，直接转人工
    if q.qtype in ("blank", "unknown") and not q.options:
        suspend(q, debug_dir, note=f"题型 {q.qtype} 无法自动作答")
        return Answer([], 0.0, "none", "题型不支持自动作答"), True

    if not allow_web and not guess_when_unsure:
        suspend(q, debug_dir, note="未启用联网搜索")
        return Answer([], 0.0, "none", "未启用联网搜索"), True

    # 2) 联网取证 + 投票（开了猜也要先试联网——命中就省一轮重做）
    pages = gather_evidence(q) if allow_web else []
    labels, conf, votes, evidence = vote_from_pages(q, pages)

    if labels and conf >= min_confidence:
        # 高置信度结果自动入库，下次直接命中
        cache.put(q, labels, source="web", evidence=evidence)
        print(f"[quiz] 联网命中并入库: {q.stem[:36]} -> {labels} "
              f"(conf={conf:.2f}, votes={votes})")
        return Answer(labels, conf, "web", evidence, votes), False

    # 3) 没把握 → 按策略决定：挂起等人，还是先随便选
    if guess_when_unsure and q.options:
        pick = _first_guess(q)
        if pick:
            why = (f"联网未命中（抓了 {len(pages)} 页）"
                   if allow_web and pages else "未启用联网搜索")
            suspend(q, debug_dir, note=f"{why}；已先随便选 {''.join(pick)}，交卷后采答案")
            print(f"[quiz] 没把握，先选 {''.join(pick)}"
                  f"（{q.qtype}）—— 交卷后从结果页采正确答案")
            return Answer(pick, 0.0, "guess", f"{why}；随便选，待采集纠正"), False

    if not labels:
        suspend(q, debug_dir, note=f"联网未找到答案标记（抓了 {len(pages)} 页）")
        print(f"[quiz] 转人工: {q.stem[:36]}（联网未命中，抓了 {len(pages)} 页）")
        return Answer([], 0.0, "none", "联网未命中"), True

    suspend(q, debug_dir,
            note=f"联网置信度不足 {conf:.2f} < {min_confidence}（票数 {votes}）")
    print(f"[quiz] 转人工: {q.stem[:36]} "
          f"(conf={conf:.2f} < {min_confidence}, votes={votes})")
    return Answer(labels, conf, "web", evidence, votes), True


def _first_guess(q: Question) -> list[str]:
    """没把握时先选的答案。

    单选/判断取第一个选项；多选取前两个（多选只选一个通常必错，
    但我们只是要「有个答案」好交卷，采答案才是真正目的）。

    判断题的字母是 T/F，所以按选项的 label 取，而不是写死 "A"。
    """
    if not q.options:
        return []
    if q.qtype == "multi" and len(q.options) >= 2:
        return [q.options[0].label, q.options[1].label]
    return [q.options[0].label]


def classify(stem: str, options: list[Option]) -> QuizType:
    """粗判题型。

    判断依据：
      * 只有两个选项且正文是「是/否」「对/错」「正确/错误」→ judge
      * 无选项 → blank
      * 题干含「多选」「哪些」「错误的是」且选项 >= 3 → 倾向 multi
      * 其余 → single
    """
    if not options:
        return "blank"

    texts = [_norm(o.text) for o in options]
    if len(options) == 2 and all(t in ("是", "否", "对", "错", "正确", "错误") for t in texts):
        return "judge"

    multi_hint = re.search(r"多选|哪些|哪几|错误的是|不正确的是|包括", stem or "")
    if multi_hint and len(options) >= 3:
        return "multi"

    return "single"
