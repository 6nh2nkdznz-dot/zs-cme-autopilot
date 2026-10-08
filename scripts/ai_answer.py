# -*- coding: utf-8 -*-
r"""用 AI 答考核里"题库缓存里没有"的那些题。

用户 m19597：「考核加问卷设置中加入仅考核与仅问卷选项，并支持填入 API 以进行 ai
答题」；m19647 选的是「OpenAI 兼容（自己填 接口地址 + 密钥 + 模型名）」。

## 为什么走 OpenAI 兼容协议，而不是挨个适配各家

`/chat/completions` 这套请求/响应形状已经是事实标准 —— OpenAI 官方、国内
几家大模型的兼容端点、本地 ollama（`http://127.0.0.1:11434/v1`）、
one-api / new-api 这类中转，全都认。适配一个协议就等于支持全部，
所以界面上只有三个框：接口地址、密钥、模型名。

## 只用标准库

`urllib.request` 就够发这一个 POST。这个项目打包成 112MB 的 onedir 已经
不小了，为一次 HTTP 请求再塞一个 `requests` 不划算（而且 PyInstaller 还得
额外收证书包）。

## 这里**只管问**，不管"要不要问"

判断"这题缓存里有没有"是 `desktop_exam.plan_for()` 的事。这样分工的好处：
AI 挂了 / 没配 / 超时，`plan_for` 照样能退回它原来那套（题干文字匹配 →
字母 → `guess()`），不会因为一次网络抖动把整场考核搞崩。

★ 缓存 key（题干归一化）**只有一份**，在 `desktop_exam._norm()` 里。
这里刻意**不再定义一个自己的 `_norm`** —— 两个函数只要差一个字节
（大小写、截断长度），AI 答过的题下次就永远命中不了缓存，
表现是"每跑一轮都重新花钱问同一批题"，而且不报错。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Callable

#: 单次请求超时（秒）。考核是有时限的，不能卡在一个慢模型上。
TIMEOUT = 45.0

#: 单题最多重试几次（网络抖动才重试，4xx 这种确定性错误不重试）。
RETRIES = 2

#: 选项字母的合法范围。平台最多见过 A~E，留到 H 足够。
_LETTERS = "ABCDEFGH"

#: 从模型回复里抠答案用的。**必须锚在回复的开头附近** ——
#: 模型爱在答案后面补一句"因为……"，那句话里也有大写字母。
#:
#: 为什么前面要容忍一堆前缀：system prompt 里写了"只输出选项字母本身"，
#: 但实测模型（尤其中文模型）还是常常回「选 A」「答案是 B」「**C**」。
#: 容忍这些前缀、同时**只在开头吃**，两头都占：
#: 「选 A。因为 B 选项…」→ `A`（取全文大写字母会抠出 `AB`，那是错的）。
_PICK_RE = re.compile(
    r"^\s*[*_#>\-\s]*"                              # markdown / 列表符号
    r"(?:答\s*案|正确\s*选项|应\s*选|选\s*择|选|答)?"   # 可选的引导语
    r"\s*[:：是为]?\s*"                              # 「答案是」的"是"
    r"[\[【(（｛{\"'*_]*\s*"                          # 可能的括号/引号/粗体
    r"([" + _LETTERS + r"](?:\s*[|,，、/和及]\s*[" + _LETTERS + r"]"
    r"|\s*[" + _LETTERS + r"])*)"
)

_SYSTEM = (
    "你是中国医学继续教育考试的答题助手。"
    "你只会收到一道题和它的选项，必须直接给答案。"
    "★ 只输出选项字母本身，不要解释、不要标点、不要写「答案」两个字。"
)


def parse_answer(text: str, valid: set[str], *, single: bool = False) -> str:
    """从模型回复里抠出答案字母串（`"A"` / `"A|C"`），抠不出来返回空串。

    `valid` 是这道题**真实存在**的选项 `index` 集合。落在这之外的字母一律
    丢掉 —— 模型答"A"而这道题只有 B/C 时，硬填进去平台会拦（填不进 = 空着）。

    `single=True`（单选/判断）时**只留第一个字母**：模型偶尔会回 "AB"，
    单选卷上填两个字母平台会判无效，留一个至少还有对的机会。

    为什么不用「取回复里所有大写字母」这种省事的写法：模型很爱写
    「选 A。因为 B 选项描述的是……」—— 那样会抠出 "AB"。所以只从**回复开头**
    按"字母 + 分隔符"的形状连续吃，吃到第一个不是分隔符的东西就停。
    """
    if not text:
        return ""
    try:
        m = _PICK_RE.match(str(text).strip().upper())
    except (AttributeError, TypeError):
        return ""
    if not m:
        return ""
    picked = re.findall(r"[" + _LETTERS + r"]", m.group(1))
    keep: list[str] = []
    for ch in picked:
        if ch in valid and ch not in keep:
            keep.append(ch)
    if single:
        keep = keep[:1]
    return "|".join(sorted(keep))


class AI:
    """一个 OpenAI 兼容的对话补全客户端。线程安全（无共享可变状态）。"""

    def __init__(self, base: str, key: str, model: str, *,
                 timeout: float = TIMEOUT, retries: int = RETRIES,
                 log: Callable[[str], None] | None = None) -> None:
        self.base = (base or "").strip().rstrip("/")
        self.key = (key or "").strip()
        self.model = (model or "").strip()
        self.timeout = timeout
        self.retries = max(1, retries)
        self.log = log or (lambda _m: None)
        #: 已经问过几道、成功几道 —— 收工时要报出来（花钱的东西得让人看见）。
        self.asked = 0
        self.ok = 0

    # -- 配置 ---------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """三个框都填了才算配好。少一个就不启用 AI。"""
        return bool(self.base and self.model)

    def why_not(self) -> str:
        """没配好的话，说清楚缺哪个 —— 日志里得能直接看出该去填什么。"""
        miss = []
        if not self.base:
            miss.append("接口地址")
        if not self.model:
            miss.append("模型名")
        if not miss:
            return ""
        return "、".join(miss)

    @property
    def url(self) -> str:
        """补全 `/chat/completions`。

        用户很可能会填成 `https://api.openai.com/v1/chat/completions`
        （浏览器地址栏里那条），所以这里认一下已经带了的情况，别拼成
        `.../chat/completions/chat/completions`。
        """
        if self.base.endswith("/chat/completions"):
            return self.base
        return f"{self.base}/chat/completions"

    # -- 问 ----------------------------------------------------------------

    def prompt_for(self, q: dict) -> str:
        """把一道题拼成给模型看的正文。"""
        kind = q.get("kind") or ""
        what = {"danxuan": "单选题（只能选一个）",
                "duoxuan": "多选题（可以选多个）",
                "panduan": "判断题（只能选一个）",
                "wenda": "问答题（如果非答不可，就答「无」）"}.get(kind, "选择题")
        lines = [what, f"题干：{q.get('title') or ''}", ""]
        for o in (q.get("options") or []):
            lines.append(f"{o.get('index')}. {o.get('text') or ''}")
        lines.append("")
        if kind == "duoxuan":
            lines.append("只回一行选项字母，多个用 | 隔开。例如：A|C")
        elif kind == "wenda":
            lines.append("只回一个「无」。")
        else:
            lines.append("只回一行一个选项字母。例如：B")
        return "\n".join(lines)

    def ask(self, q: dict) -> str:
        """问一道题，返回答案字母串（`"A"` / `"A|C"`）；问不出来返回空串。

        **永不抛异常** —— 调用方（`plan_for`）拿空串就会退回 `guess()`。
        所以连"拼请求"这一步都包在 try 里：题目数据是页面读来的，
        缺字段/类型不对都可能在这儿炸，而那时考核已经开考了。
        """
        try:
            return self._ask(q)
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            self.log(f"[ai] 问这道题时出错（{type(exc).__name__}: {exc}），跳过")
            return ""

    def _ask(self, q: dict) -> str:
        if not self.ready:
            return ""
        kind = str(q.get("kind") or "")
        opts = q.get("options") or []
        valid = {str(o.get("index") or "") for o in opts}
        valid = {v for v in valid if v}
        if not valid:
            return ""

        self.asked += 1
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": self.prompt_for(q)},
            ],
            # 答题要的是稳定，不是创意。
            "temperature": 0,
        }
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"

        raw = self._post(body, headers)
        if not raw:
            return ""
        text = self._pull_text(raw)
        got = parse_answer(text, valid, single=(kind != "duoxuan"))
        if got:
            self.ok += 1
            self.log(f"[ai] 第 {self.asked} 题 → {got}"
                     f"（{str(q.get('title') or '')[:30]}…）")
        else:
            # 抠不出来也要把模型原话留一行 —— 不然"AI 答了但没用上"无从查起。
            self.log(f"[ai] 第 {self.asked} 题的回答没解析出选项字母，"
                     f"模型原话：{str(text or '')[:80]!r}")
        return got

    def _post(self, body: dict, headers: dict) -> dict | None:
        """发一次请求（带重试），返回解析好的 JSON；失败返回 `None`。"""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = ""
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(self.url, data=data,
                                         headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read().decode("utf-8", "replace")
                out = json.loads(payload)
                return out if isinstance(out, dict) else None
            except urllib.error.HTTPError as exc:
                # 4xx 是确定性的（密钥错、模型名错、余额不足），重试没意义，
                # 只会白白浪费时间 —— 考核有时限。
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:200]
                except Exception:  # noqa: BLE001
                    pass
                self.log(f"[ai] 接口回了 HTTP {exc.code}：{detail or exc.reason}")
                if 400 <= exc.code < 500:
                    return None
                last = f"HTTP {exc.code}"
            except urllib.error.URLError as exc:
                last = f"连不上（{exc.reason}）"
            except (TimeoutError, OSError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            except (ValueError, TypeError) as exc:
                # 回的不是 JSON —— 多半是地址填错了（比如填成了网页地址）。
                self.log(f"[ai] 接口回的不是 JSON：{exc}（检查一下接口地址）")
                return None

            if attempt < self.retries:
                self.log(f"[ai] 第 {attempt} 次没成（{last}），等一下再试")
                time.sleep(1.5 * attempt)
        self.log(f"[ai] 放弃这道题：{last}")
        return None

    def _pull_text(self, raw: dict) -> str:
        """从 OpenAI 形状的响应里取正文。

        兼容 `choices[0].message.content`（标准）和 `choices[0].text`
        （老的 completions 形状，有些中转会这么回）。
        """
        try:
            choices = raw.get("choices") or []
            first = choices[0] if choices else {}
            msg = first.get("message") or {}
            content = msg.get("content")
            if content is None:
                content = first.get("text")
            if isinstance(content, list):
                # 有些实现把 content 做成 [{type, text}, ...]（多模态形状）。
                content = "".join(str(c.get("text") or "") for c in content
                                  if isinstance(c, dict))
            return str(content or "")
        except (AttributeError, IndexError, TypeError):
            return ""


def from_config(log: Callable[[str], None] | None = None) -> AI | None:
    """按 `config.json` 里 `options.d_exam` 那三项建一个 `AI`。

    没配（`ai_base` 空）时返回 `None` —— 调用方据此走原来的 `guess()` 路径，
    一行判断就够了，不用到处 `if`。
    """
    log = log or (lambda _m: None)
    try:
        import taskspec

        vals = taskspec.values("d_exam")
    except Exception as exc:  # noqa: BLE001 - 配置读不到就不启用
        log(f"[ai] 读不到 AI 设置，这次不用 AI（{type(exc).__name__}: {exc}）")
        return None

    base = str(vals.get("ai_base") or "")
    if not base.strip():
        return None
    ai = AI(base, str(vals.get("ai_key") or ""), str(vals.get("ai_model") or ""),
            log=log)
    why = ai.why_not()
    if why:
        log(f"[ai] 填了接口地址但没填「{why}」，这次不用 AI")
        return None
    log(f"[ai] 已启用 AI 答题：{ai.url}，模型 {ai.model}")
    return ai


def main(argv: list[str] | None = None) -> int:
    """命令行自测：拿一道假题去问，看接口通不通。

        python scripts/ai_answer.py --ai-base https://… --ai-key sk-… --ai-model …
        python scripts/ai_answer.py --status        # 只看配置读成什么样
    """
    import argparse
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    ap = argparse.ArgumentParser(description="试一下 AI 答题的接口通不通")
    ap.add_argument("--ai-base", default="")
    ap.add_argument("--ai-key", default="")
    ap.add_argument("--ai-model", default="")
    ap.add_argument("--status", action="store_true", help="只打印配置，不发请求")
    args = ap.parse_args(argv)

    say = lambda m: print(m, flush=True)  # noqa: E731

    if args.status:
        ai = from_config(say)
        if ai is None:
            say("没配 AI（「AI 接口地址」是空的，或者缺模型名）")
            return 1
        say(f"接口 {ai.url}\n模型 {ai.model}\n密钥 {'已填' if ai.key else '没填'}")
        return 0

    ai = (AI(args.ai_base, args.ai_key, args.ai_model, log=say)
          if args.ai_base else from_config(say))
    if ai is None:
        say("没配 AI —— 要么加 --ai-base/--ai-model，要么去界面上填")
        return 1

    demo = {
        "id": "demo",
        "kind": "danxuan",
        "title": "成人心肺复苏时，胸外按压的深度应该是多少？",
        "options": [
            {"index": "A", "text": "2 厘米"},
            {"index": "B", "text": "5~6 厘米"},
            {"index": "C", "text": "8 厘米"},
            {"index": "D", "text": "越深越好"},
        ],
    }
    say(f"拿一道演示题试接口：{demo['title']}")
    got = ai.ask(demo)
    if got:
        say(f"✓ 通了，模型答 {got}（正确答案是 B）")
        return 0
    say("✗ 没问到答案，看上面的报错")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
