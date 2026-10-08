# -*- coding: utf-8 -*-
"""AI 答题（`ai_answer.py`）的自测。

## 为什么需要这个模块

用户 m19597 要「支持填入 API 以进行 ai 答题」，m19647 选了 OpenAI 兼容协议。
这一块的风险和别处不一样：

1. **它花钱**，而且是在「考核已经开始计时」的时候花。一个解析错误 = 白问一遍
   + 消耗一次考核机会。
2. **它永远不该成为故障点**。AI 挂了、没配、超时、回了一堆废话 ——
   任何一种都必须退回 `desktop_exam.guess()`，绝不能把整场考核带崩。
3. **最容易静默出错的地方是 `parse_answer()`**。实测模型（尤其中文模型）
   并不老实照 system prompt 只回一个字母，常见的是「选 A」「答案是 B」
   「**C**」，而它答完还爱补一句「因为 B 选项描述的是……」——
   **取全文大写字母会抠出 `AB`**，那是错的答案，还不报错。
   所以这里把真实见过的回复形状全钉一遍。

`_post()` 不发真请求：用假的 `urlopen` 换掉，验的是**重试策略**
（4xx 是确定性的，重试只是白等——考核有时限）。

运行:
    python scripts\\test_ai_answer.py
"""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import ai_answer  # noqa: E402
import desktop_exam  # noqa: E402

_passed = 0
_failed = 0

V = set("ABCD")


def check(name: str, got, want=True) -> None:
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}\n           期望 {want!r}\n           实际 {got!r}")


def _q(title="成人心肺复苏时胸外按压的深度是多少", kind="danxuan",
       opts="A:至少 5 厘米|B:至少 6 厘米|C:至少 3 厘米|D:至少 4 厘米") -> dict:
    return {
        "id": "q1", "title": title, "kind": kind,
        "options": [{"index": p.split(":", 1)[0], "text": p.split(":", 1)[1]}
                    for p in opts.split("|")],
    }


# ------------------------------------------------------- 1. parse_answer
print("[1] 从模型回复里抠答案")

# (模型原话, 期望)
_CASES = [
    # —— 老实的 ——
    ("A", "A"),
    ("B.", "B"),
    ("B。", "B"),
    ("A\n", "A"),
    ("A\n因为心脏按压深度…", "A"),
    # —— 不老实但很常见的 ——
    ("**C**", "C"),
    ("选 A", "A"),
    ("选A", "A"),
    ("答案是 B。", "B"),
    ("答案：C", "C"),
    ("正确选项是 D", "D"),
    ("[A]", "A"),
    ("（B）", "B"),
    ("(C)", "C"),
    # ★ 最关键的一条：答案后面那句解释里也有大写字母。
    # 取全文大写字母会得到 "AB"，那是错的，而且不报错。
    ("选 A。因为 B 选项描述的是成人按压深度…", "A"),
    ("B，因为 A 太浅了", "B"),
    # —— 多选 ——
    ("A|C", "A|C"),
    ("A、C", "A|C"),
    ("AC", "A|C"),
    ("A, C", "A|C"),
    ("A/C", "A|C"),
    ("B 和 D", "B|D"),
    ("A、B、C", "A|B|C"),
    # —— 抠不出来的（必须返回空串，让调用方退回 guess）——
    ("", ""),
    ("   ", ""),
    ("我不知道", ""),
    ("这道题信息不足，无法判断", ""),
    ("E", ""),          # 选项只有 ABCD，答 E 是幻觉
    (None, ""),
]
for _text, _want in _CASES:
    check(f"parse_answer({_text!r})", ai_answer.parse_answer(_text, V), _want)

# 落不到 valid 里的字母要丢掉：答 "AE" 而这道题没有 E → 只留 A
check("valid 之外的字母被丢掉", ai_answer.parse_answer("AE", V), "A")
check("全部越界 → 空串", ai_answer.parse_answer("EF", V), "")
check("重复字母只留一个", ai_answer.parse_answer("A|A", V), "A")

# single=True（单选/判断）：模型偶尔回 "AB"，单选卷上填两个字母平台判无效，
# 留一个至少还有对的机会。
check("single=True 只留第一个", ai_answer.parse_answer("AC", V, single=True), "A")
check("single=False 保留多个", ai_answer.parse_answer("AC", V), "A|C")
check("字母顺序归一（CA → A|C）", ai_answer.parse_answer("CA", V), "A|C")

# ★ 缓存 key 只能有一份。`ai_answer` 里曾经也有一个 `_norm()`，但它**小写化**
# 而 `desktop_exam._norm()` 不 —— 差这一个字节，AI 答过的题下次就永远命中
# 不了缓存，表现是"每跑一轮都重新花钱问同一批题"，而且不报错。
# 所以这里钉死：归一化函数只有 `desktop_exam` 那一份。
check("ai_answer 里没有第二份 _norm", not hasattr(ai_answer, "_norm"))
check("desktop_exam._norm 不去大小写（原样）",
      desktop_exam._norm(" A B "), "AB")
check("desktop_exam._norm 去掉所有空白",
      desktop_exam._norm("成人心肺\u3000复苏\n时"), "成人心肺复苏时")


# ------------------------------------------------ 2. 配置 / URL 拼装
print("\n[2] 三个框和 URL")

_a = ai_answer.AI("https://api.openai.com/v1", "sk-x", "gpt-4o-mini")
check("配好了 ready=True", _a.ready, True)
check("why_not() 配好时是空串", _a.why_not(), "")
check("补全 /chat/completions",
      _a.url, "https://api.openai.com/v1/chat/completions")

# 用户很可能直接粘浏览器地址栏那条（已经带 /chat/completions），
# 不能再拼一遍成 .../chat/completions/chat/completions
_a2 = ai_answer.AI("https://api.openai.com/v1/chat/completions", "", "m")
check("已经带了就不重复拼", _a2.url, "https://api.openai.com/v1/chat/completions")

# 末尾斜杠 / 前后空白都要吃掉（从界面输入框里粘出来的）
_a3 = ai_answer.AI("  http://127.0.0.1:11434/v1/  ", "", "qwen2.5")
check("去掉末尾斜杠", _a3.url, "http://127.0.0.1:11434/v1/chat/completions")

check("没填接口地址 → 不 ready", ai_answer.AI("", "k", "m").ready, False)
check("没填模型名 → 不 ready", ai_answer.AI("http://x/v1", "k", "").ready, False)
check("本地模型不填密钥也算 ready",
      ai_answer.AI("http://x/v1", "", "m").ready, True)
check("缺接口地址时报的是「接口地址」",
      ai_answer.AI("", "", "m").why_not(), "接口地址")
check("缺模型名时报的是「模型名」",
      ai_answer.AI("http://x/v1", "", "").why_not(), "模型名")
check("两个都缺就都报",
      ai_answer.AI("", "", "").why_not(), "接口地址、模型名")
check("计数初值", (_a.asked, _a.ok), (0, 0))


# ---------------------------------------------------- 3. prompt_for
print("\n[3] 给模型看的正文")

_p = _a.prompt_for(_q())
check("带了题干", "胸外按压的深度" in _p, True)
check("四个选项的文字都在", all(x in _p for x in
                                 ("至少 5 厘米", "至少 6 厘米", "至少 3 厘米", "至少 4 厘米")), True)
check("选项带字母序号", "A. 至少 5 厘米" in _p, True)
check("单选说清单选", "单选题" in _p, True)
check("单选要求回一个字母", "只回一行一个选项字母" in _p, True)

_p2 = _a.prompt_for(_q(kind="duoxuan"))
check("多选说清多选", "多选题" in _p2, True)
check("多选要求用 | 隔开", "A|C" in _p2, True)
_p3 = _a.prompt_for(_q(kind="panduan"))
check("判断题说清判断", "判断题" in _p3, True)


# --------------------------------------------- 4. ask()（换掉 _post）
print("\n[4] ask() 的成败与计数")


def _fake_post(payload):
    def _p(self, body, headers):
        _fake_post.last = (body, headers)
        return payload
    return _p


_real_post = ai_answer.AI._post
_LOGS: list[str] = []
try:
    _a.log = _LOGS.append

    ai_answer.AI._post = _fake_post(
        {"choices": [{"message": {"content": "选 B。因为 6 厘米…"}}]})
    _got = _a.ask(_q())
    check("正常一轮拿到字母", _got, "B")
    check("asked 加了 1", _a.asked, 1)
    check("ok 加了 1", _a.ok, 1)
    check("请求体里带了模型名", _fake_post.last[0]["model"], "gpt-4o-mini")
    check("temperature 是 0（要稳定不要创意）",
          _fake_post.last[0]["temperature"], 0)
    check("带了 Authorization", _fake_post.last[1].get("Authorization"), "Bearer sk-x")
    check("messages 两条（system + user）",
          [m["role"] for m in _fake_post.last[0]["messages"]], ["system", "user"])

    # 解析不出来：asked 要加、ok 不能加，而且**模型原话要落日志**
    # —— 不然"AI 答了但没用上"根本无从查起。
    _LOGS.clear()
    ai_answer.AI._post = _fake_post(
        {"choices": [{"message": {"content": "这道题我不会"}}]})
    check("答歪了 → 空串", _a.ask(_q()), "")
    check("答歪了 asked 还是加", _a.asked, 2)
    check("答歪了 ok 不加", _a.ok, 1)
    check("答歪了会把模型原话写进日志",
          any("这道题我不会" in m for m in _LOGS), True)

    # 请求失败（_post 回 None）→ 空串，计数只加 asked
    ai_answer.AI._post = _fake_post(None)
    check("请求失败 → 空串", _a.ask(_q()), "")
    check("请求失败 ok 不加", _a.ok, 1)

    # 没配好 → 一次都不问（省钱）
    _before = _a.asked
    check("没配好 → 空串", ai_answer.AI("", "", "").ask(_q()), "")
    check("没配好 → 不计数", ai_answer.AI("", "", "").asked, 0)
    check("_a 没被牵连", _a.asked, _before)

    # 选项读不出来 → 不问（没有 valid 就没法校验模型答案）
    check("题目没选项 → 空串", _a.ask({"id": "x", "title": "t", "options": []}), "")

    # ★ 永不抛异常：_post 炸了也不能往上传
    def _boom(self, body, headers):
        raise RuntimeError("模拟网络层炸了")

    ai_answer.AI._post = _boom
    _LOGS.clear()
    check("_post 抛异常时 ask 不抛", _a.ask(_q()), "")
    check("…并且写了日志", any("跳过" in m or "出错" in m for m in _LOGS), True)

    # 题目数据本身是脏的（页面读来的，缺字段/类型不对都可能）
    ai_answer.AI._post = _fake_post({"choices": []})
    check("题目数据脏也不抛", isinstance(_a.ask({"options": None, "title": None}), str), True)

    # 单选：模型回 "AB" 只留一个
    ai_answer.AI._post = _fake_post({"choices": [{"message": {"content": "AB"}}]})
    check("单选题上只留一个字母", _a.ask(_q(kind="danxuan")), "A")
    ai_answer.AI._post = _fake_post({"choices": [{"message": {"content": "AB"}}]})
    check("多选题上保留两个", _a.ask(_q(kind="duoxuan")), "A|B")
finally:
    ai_answer.AI._post = _real_post


# ---------------------------------------------------- 5. _pull_text
print("\n[5] 响应形状的兼容")

_pull = _a._pull_text
check("标准形状 choices[0].message.content",
      _pull({"choices": [{"message": {"content": "A"}}]}), "A")
check("老 completions 形状 choices[0].text",
      _pull({"choices": [{"text": "B"}]}), "B")
check("多模态形状 content 是列表",
      _pull({"choices": [{"message": {"content": [
          {"type": "text", "text": "C"}, {"type": "text", "text": "D"}]}}]}), "CD")
check("空 choices → 空串", _pull({"choices": []}), "")
check("没有 choices → 空串", _pull({}), "")
check("choices 是 None → 空串", _pull({"choices": None}), "")
check("乱七八糟的形状也不抛", isinstance(_pull({"choices": [None]}), str), True)


# ------------------------------------------------- 6. 重试策略
print("\n[6] 重试（4xx 不重试，5xx 才重试）")

_orig_urlopen = ai_answer.urllib.request.urlopen
_counts = {"n": 0}


def _http_error(code):
    def _fn(req, timeout=None):
        _counts["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, code, "boom", {}, io.BytesIO(b'{"error":"nope"}'))
    return _fn


try:
    # 401 是确定性的（密钥错）—— 重试只是白等，考核有时限。
    _b = ai_answer.AI("http://x/v1", "k", "m", retries=3, log=lambda _m: None)
    _counts["n"] = 0
    ai_answer.urllib.request.urlopen = _http_error(401)
    check("401 → 回 None", _b._post({}, {}), None)
    check("401 只发一次（不重试）", _counts["n"], 1)

    _counts["n"] = 0
    ai_answer.urllib.request.urlopen = _http_error(429)
    check("429 也只发一次（额度问题重试没用）", _b._post({}, {}) is None and _counts["n"], 1)

    # 5xx 是暂时性的，该重试。
    _b2 = ai_answer.AI("http://x/v1", "k", "m", retries=3, log=lambda _m: None)
    _counts["n"] = 0
    ai_answer.urllib.request.urlopen = _http_error(503)
    check("503 → 回 None", _b2._post({}, {}), None)
    check("503 重试到用完次数", _counts["n"], 3)

    # 连不上（服务没开 / 地址填错域名）
    def _urlerr(req, timeout=None):
        _counts["n"] += 1
        raise urllib.error.URLError("connection refused")

    _counts["n"] = 0
    ai_answer.urllib.request.urlopen = _urlerr
    if _b2.retries != 1:
        _b2.retries = 1
    check("连不上 → 回 None", _b2._post({}, {}), None)

    # 回的不是 JSON → 多半是地址填成了网页，别重试（重试也一样）
    class _Resp:
        def read(self):
            return b"<html>hello</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    _counts["n"] = 0
    _b3 = ai_answer.AI("http://x/v1", "k", "m", retries=3, log=lambda _m: None)
    ai_answer.urllib.request.urlopen = lambda req, timeout=None: (
        _counts.__setitem__("n", _counts["n"] + 1), _Resp())[1]
    check("回的不是 JSON → None", _b3._post({}, {}), None)
    check("回的不是 JSON → 不重试", _counts["n"], 1)
finally:
    ai_answer.urllib.request.urlopen = _orig_urlopen


# ------------------------------------------------------ 7. from_config
print("\n[7] 从配置里读")

_cfg_ai = ai_answer.from_config()
check("配置里没填接口地址时返回 None（或一个 ready 的 AI）",
      _cfg_ai is None or isinstance(_cfg_ai, ai_answer.AI), True)


# ------------------------------------------------- 8. 和考核接上的样子
print("\n[8] 和桌面版考核接上了没有（跑真的 plan_for）")


class _FakeAI:
    """假的 AI：答「B」，并且数着自己被问了几次。"""

    def __init__(self, answer="B", model="fake-1"):
        self.answer = answer
        self.model = model
        self.asked = 0
        self.ok = 0

    def ask(self, q):
        self.asked += 1
        if self.answer:
            self.ok += 1
        return self.answer


_Q = _q()

# —— 第一轮：题库空的，必须去问 ——
_fake = _FakeAI()
_cache: dict = {}
_plan, _unsure = desktop_exam.plan_for([_Q], _cache, ai=_fake)
check("题库没有时去问了 AI", _fake.asked, 1)
check("计划里用上了 AI 的答案", _plan.get("q1"), "B")
check("官方答案段没被写脏", _cache.get("questions"), None)
check("AI 答案进了 cache['ai']", len(_cache.get("ai") or {}), 1)
check("AI 答的也算「没把握」", _unsure, 1)

# —— 第二轮：同一道题**不能再花钱问一遍** ——
_fake2 = _FakeAI()
_plan2, _ = desktop_exam.plan_for([_Q], _cache, ai=_fake2)
check("同一道题第二次不再问（吃缓存）", _fake2.asked, 0)
check("第二次答案还是 B", _plan2.get("q1"), "B")

# —— 选项顺序被平台重排了，还得能对上 ——
# `resolve()` 是按选项**文字**认的，不是按字母。这一条就是验它。
_shuffled = {
    "id": "q1", "title": _Q["title"], "kind": "danxuan",
    "options": [{"index": "A", "text": "至少 6 厘米"},      # 原来的 B
                {"index": "B", "text": "至少 5 厘米"},      # 原来的 A
                {"index": "C", "text": "至少 3 厘米"},
                {"index": "D", "text": "至少 4 厘米"}],
}
_fake3 = _FakeAI()
_plan3, _ = desktop_exam.plan_for([_shuffled], _cache, ai=_fake3)
check("换卷时不重新问", _fake3.asked, 0)
check("换卷后按选项文字搬到了 A（正确答案「至少 6 厘米」）",
      _plan3.get("q1"), "A")

# —— 官方答案优先于 AI：平台公布的是可信的，AI 是猜的 ——
_cache4 = {"questions": {desktop_exam._norm(_Q["title"]): {
    "answer": "C",
    "texts": ["至少 3 厘米"],
    "options": {str(o["index"]): o["text"] for o in _Q["options"]},
}}}
_fake4 = _FakeAI()
_plan4, _unsure4 = desktop_exam.plan_for([_Q], _cache4, ai=_fake4)
check("官方答案优先，不去问 AI", _fake4.asked, 0)
check("用的是官方那个答案", _plan4.get("q1"), "C")
check("官方命中就不算「没把握」", _unsure4, 0)
check("官方缓存没被 AI 污染", "ai" not in _cache4 or not _cache4["ai"])

# —— AI 答歪（空串）→ 必须退回 guess()，不能留空 ——
# 空着交上去平台会拦（`checkAnswerModel()`），连"交一次看答案"都做不成。
_fake5 = _FakeAI(answer="")
_plan5, _ = desktop_exam.plan_for([_Q], {}, ai=_fake5)
check("AI 答不出来时仍然每题都有答案", len(_plan5), 1)
check("…用的是 guess()（第一个选项）", _plan5.get("q1"), "A")

# —— 没接 AI 时行为和接之前一模一样 ——
_plan6, _unsure6 = desktop_exam.plan_for([_Q], {})
check("不传 ai 也能跑", _plan6.get("q1"), "A")
check("不传 ai 就没把握", _unsure6, 1)

_EXAM = (Path(__file__).resolve().parent / "desktop_exam.py").read_text(encoding="utf-8")
check("plan_for 收 ai 参数", "*, ai=None" in _EXAM)
check("do_exam 收 ai 参数", "ai=None) -> dict:" in _EXAM)
# AI 答案和平台公布的答案**必须分开存**：官方是可信的，AI 是猜的；
# 混在一起 harvest() 就分不清该覆盖谁了。
check("AI 答案存在 cache['ai']（和官方分开）", 'cache["ai"]' in _EXAM)
# dry-run 只是"报一下打算怎么做"，不该为看一眼花用户的额度。
check("dry-run 不接 AI", 'plan_for(form.get("questions") or [], cache)' in _EXAM)
check("AI 答案会落盘（中途被停不白花钱）",
      "save_cache(cache)" in _EXAM.split("plan_for(questions, cache, ai=ai)")[1][:300])


# ------------------------------------------------------------------ 收尾
print()
print(f"test_ai_answer: {_passed} passed, {_failed} failed")
raise SystemExit(1 if _failed else 0)
