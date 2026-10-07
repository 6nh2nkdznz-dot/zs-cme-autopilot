"""从考核结果页读取正确答案，建成本地题库。

## 为什么这是最好的答案来源

平台提交后会逐题给出「您的答案 / **正确答案**」（实测确认）。
这是**官方答案**，零误差——比联网搜题干可靠一个数量级。

于是有个很划算的策略：

```
第一遍：故意全错（或随便答）→ 提交 → 从结果页读出全部正确答案 → 入库
第二遍：重做 → 题库 100% 命中 → 满分
```

代价只是「重做次数 +1」——而平台明确说客观题可以重复提交。
用户已确认「答错不扣分、可反复重做」。

## 实测的结果页版式（MaaFramework 720x1280）

```
y  ~96    标题栏「本项目考核」+ 右上角得分「60分」
y ~135    考核信息（考核类型 / 题目类型 / 本次成绩 / 最高成绩）
y ~183    「1、脓毒症患者…（单选题 5分）   ❌」
y ~230+   选项 A、B、C、D 各占约 52px
y ~530    [灰底块]
            您的答案：A
            正确答案：D      ← 要抓的就是这个
            答案解析: 暂无
```

一屏约能放 2 道题，所以要滚动采集并去重。

用法:
    python scripts\\harvest_answers.py --dry-run     # 只读不写库
    python scripts\\harvest_answers.py               # 采集并写入题库
    python scripts\\harvest_answers.py --max 30      # 最多滚 30 屏
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import paths  # noqa: E402
import quiz  # noqa: E402
from controller import ConfigError, build_controller, load_config  # noqa: E402
from quiz import AnswerCache, Option, Question  # noqa: E402

# 题干行：「1、脓毒症患者…（单选题 5分）」
_STEM_RE = re.compile(r"^\s*(\d{1,3})\s*[、.．]\s*(.+?)\s*[（(]\s*"
                      r"(单选题|多选题|判断题|填空题|问答题)\s*(\d+)\s*分\s*[)）]")
# 「正确答案：D」/「正确答案：AC」
_CORRECT_RE = re.compile(r"正确答案\s*[：:]\s*([A-Za-zTF]{1,6})")
# 「您的答案：A」
_MINE_RE = re.compile(r"您的答案\s*[：:]\s*([A-Za-zTF]{0,6})")
# 选项行：「A、体温」/「A.体温」/「A．体温」
_OPTION_RE = re.compile(r"^\s*([A-Fa-f]|[TF])\s*[、.．,，]\s*(.+?)\s*$")

# 题干行末尾会粘上平台的对错标记（❌/✅ 被 OCR 成 X/×/√ 等）。
# 这些标记不能留在题干里 —— 题库 key 是按题干归一化的。
# 更细的归一化（去题型标注、去题号）统一由 quiz.Question.normalized_stem
# 负责，这样采集端和答题端天然一致，不靠两边各自记规则。
_TRAILING_MARK_RE = re.compile(r"[\sXx×✗✘√✓✔❌✅*※]+$")


def canonical_stem(stem: str) -> str:
    """题干规范形式，用作题库 key。

    直接委托给 `quiz.Question.normalized_stem()`——**单一事实来源**。
    早先这里自己实现了一套规则，结果和答题端不一致，
    采到的答案重做时匹配不上。
    """
    from quiz import Question as _Q

    return _Q(stem=stem).normalized_stem()

# 结果页的滚动区域
SCROLL_FROM = (360, 1000)
SCROLL_TO = (360, 450)
SCROLL_SETTLE = 1.6


def parse_rows(rows: list[tuple[str, int, int]]) -> list[dict]:
    """从一屏 OCR 结果里抽出题目。

    返回 [{"num": 1, "stem": ..., "qtype": ..., "options": [...],
           "correct": "D", "mine": "A"}, ...]
    """
    # 按 y 排序，OCR 顺序不保证
    ordered = sorted(rows, key=lambda r: r[2])

    items: list[dict] = []
    cur: dict | None = None

    for text, _x, y in ordered:
        t = text.strip()
        if not t:
            continue

        m = _STEM_RE.match(t)
        if m:
            # 去掉题干末尾粘上的对错标记，否则题库 key 会被污染
            stem = _TRAILING_MARK_RE.sub("", t)
            qtype = {
                "单选题": "single", "多选题": "multi",
                "判断题": "judge", "填空题": "blank", "问答题": "blank",
            }.get(m.group(3), "unknown")
            cur = {
                "num": int(m.group(1)),
                "stem": stem,
                # 存一份去掉题型与分值的规范题干，和答题页读到的对齐。
                # 结果页是「…（单选题 5分）」，答题页是「…(5分)」，
                # 不归一化的话题库 key 对不上，采到的答案重做时命中不了。
                "stem_canonical": canonical_stem(stem),
                "qtype": qtype,
                "options": [],
                "correct": "",
                "mine": "",
                "correct_seen": False,
                "y": y,
            }
            items.append(cur)
            continue

        if cur is None:
            continue

        m = _CORRECT_RE.search(t)
        if m:
            cur["correct"] = m.group(1).upper()
            cur["correct_seen"] = True
            continue

        m = _MINE_RE.search(t)
        if m:
            cur["mine"] = m.group(1).upper()
            continue

        # 选项行：只在拿到题干之后、拿到「正确答案」之前收集。
        #
        # 用显式的 correct_seen 标记，而不是 `not cur["correct"]` ——
        # 后者有个隐蔽的 bug：滚动重读同一屏时，上一轮已经把 correct
        # 填好了，第二轮就会跳过所有选项行，导致选项缺失（判断题尤其明显）。
        if not cur.get("correct_seen"):
            m = _OPTION_RE.match(t)
            if m:
                label = m.group(1).upper()
                body = m.group(2).strip()
                # 排掉答案行被误认成选项。
                #
                # 只按「答案」判断，**不能**再按「正确」判断——
                # 判断题的选项正文恰好就是「正确 / 错误」，
                # 用 startswith("正确") 会把 T.正确 整个过滤掉，
                # 结果判断题选项只剩 ['F']。这个坑实测踩过。
                if (body
                        and "答案" not in body
                        and not body.startswith("您的")
                        and not any(o["label"] == label for o in cur["options"])):
                    cur["options"].append({"label": label, "text": body})

    return items


def normalize_correct(item: dict) -> list[str]:
    """把「正确答案」字段拆成字母列表，并按题型做基本校验。"""
    raw = (item.get("correct") or "").strip().upper()
    if not raw:
        return []

    letters = [c for c in raw if c.isalnum()]
    qtype = item.get("qtype", "unknown")

    if qtype == "judge":
        # 判断题只认 T/F。平台可能写「对/错」，这里映射。
        mapped: list[str] = []
        for c in letters:
            if c in ("T", "F"):
                mapped.append(c)
            elif c in ("A",):          # 有的平台判断题也用 A/B，A=对
                mapped.append("T")
            elif c in ("B",):
                mapped.append("F")
        return mapped[:1]

    # 单选/多选只保留 A-F
    return [c for c in letters if "A" <= c <= "F"]


def harvest_answers(
    rows_provider,
    cache,
    screenshot=None,
    scroll=None,
    max_scrolls: int = 40,
    settle: float = SCROLL_SETTLE,
    dry_run: bool = False,
    log=print,
) -> int:
    """从结果页采集官方答案并写入题库。返回新增条数。

    参数都是回调，便于被 `retake_exam.py` 复用（它已经有常驻 OCR，
    不需要再建一套）：

        rows_provider() -> [(文本, x, y), ...]   带坐标的整屏 OCR
        scroll()        -> None                  向下滚一屏
        screenshot()    -> ndarray               整屏截图（存证用，可省）

    也便于单测：传一个返回固定夹具的 rows_provider 即可。
    """
    collected: dict[int, dict] = {}

    def absorb(rows) -> int:
        added = 0
        for item in parse_rows(rows):
            key = item["num"]
            prev = collected.get(key)
            if prev is None:
                collected[key] = item
                added += 1
            else:
                # 跨屏读到一半的题，补齐缺的字段
                for f in ("correct", "options", "mine"):
                    if not prev.get(f) and item.get(f):
                        prev[f] = item[f]
        return added

    # 先判断在不在结果页，避免对着别的页面白滚
    first = rows_provider()
    flat = " ".join(t for t, _x, _y in first)
    if "正确答案" not in flat and "您的答案" not in flat:
        log("[harvest] 当前不像考核结果页（没读到「正确答案 / 您的答案」）")
        return 0

    absorb(first)
    log(f"[harvest] 第 1 屏：累计 {len(collected)} 题")

    idle = 0
    for i in range(2, max_scrolls + 1):
        if scroll is None:
            break
        scroll()
        time.sleep(settle)

        rows = rows_provider()
        if not rows:
            break
        added = absorb(rows)
        log(f"[harvest] 第 {i} 屏：新增 {added}，累计 {len(collected)} 题")

        if added == 0:
            idle += 1
            if idle >= 3:
                log("[harvest] 连续三屏没有新题，认为到底了")
                break
        else:
            idle = 0

    # --- 写库 ---
    before = len(cache) if cache is not None else 0
    n_usable = 0
    for num in sorted(collected):
        item = collected[num]
        labels = normalize_correct(item)
        if not labels or not item.get("options"):
            continue
        if dry_run or cache is None:
            log(f"  {num:>2}. [{item.get('qtype','?'):<6}] "
                f"答案 {''.join(labels):<3} | {item['stem'][:44]}")
            n_usable += 1
            continue
        q = Question(
            stem=item["stem"],
            options=[Option(o["label"], o["text"]) for o in item["options"]],
            qtype=item.get("qtype", "unknown"),
        )
        # 把答案的**正文**也存进去。平台会打乱选项顺序（实测确认），
        # 只存字母的话下次换个顺序就答错了。
        want_texts = [o["text"] for o in item["options"]
                      if o["label"].upper() in labels]
        cache.put(q, labels, source="human",
                  evidence="来自平台考核结果页的官方正确答案",
                  answer_texts=want_texts)
        n_usable += 1

    if not dry_run and cache is not None:
        log(f"[harvest] ✓ 题库 {before} → {len(cache)} 条 "
            f"（本轮可入库 {n_usable}）")
    return n_usable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只读不写库")
    parser.add_argument("--max", type=int, default=40, help="最多滚多少屏")
    parser.add_argument("--settle", type=float, default=SCROLL_SETTLE)
    args = parser.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    resource.post_bundle(str(paths.resource_dir())).wait()
    resource.post_ocr_model(str(paths.ocr_model_dir())).wait()
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    def rows_provider() -> list[tuple[str, int, int]]:
        img = controller.post_screencap().wait().get()
        j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
        if not j.wait().succeeded:
            return []
        td = j.get()
        if td is None:
            return []
        out: list[tuple[str, int, int]] = []
        for nid in td.node_id_list:
            node = tasker.get_node_detail(nid)
            if node is None or node.recognition is None:
                continue
            for r in (node.recognition.all_results or []):
                box = getattr(r, "box", None)
                txt = getattr(r, "text", None)
                if box and txt:
                    out.append((str(txt), int(box[0]), int(box[1])))
        return out

    def scroll() -> None:
        controller.post_swipe(SCROLL_FROM[0], SCROLL_FROM[1],
                              SCROLL_TO[0], SCROLL_TO[1], 400).wait()

    print("=" * 62)
    print(" 从考核结果页采集官方答案")
    print("=" * 62)

    cache = AnswerCache(paths.answer_cache_path())
    print(f"[harvest] 采集前题库 {len(cache)} 条")

    n = harvest_answers(
        rows_provider=rows_provider,
        cache=cache,
        scroll=scroll,
        max_scrolls=args.max,
        settle=args.settle,
        dry_run=args.dry_run,
    )

    if n == 0:
        print("[harvest] 没采到题。确认停在考核结果页（有「您的答案/正确答案」）")
        return 1
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
