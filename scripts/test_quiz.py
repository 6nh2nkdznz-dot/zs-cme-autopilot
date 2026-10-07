"""答题引擎的自测。

不依赖网络，用合成数据把完整闭环跑通：

    未命中 → 挂起 → 人工作答 → 入库 → 再遇到同题直接命中

运行:
    python scripts/test_quiz.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import quiz  # noqa: E402
from quiz import AnswerCache, Option, Question  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n         期望 {want!r}\n         实际 {got!r}")


def check_true(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="quiztest-"))
    debug = tmp / "debug"
    cache = AnswerCache(tmp / "answer_cache.json")

    print("=" * 64)
    print(" 答题引擎自测")
    print("=" * 64)

    # ---------------- 1. 题干归一化 ----------------
    print("\n[1] 题干归一化（决定题库 key 是否稳定）")
    variants = [
        "1、老年认知症患者最常见的痴呆类型是",
        "(1) 老年认知症患者最常见的痴呆类型是",
        "1.老年认知症患者最常见的痴呆类型是",
        "  老年认知症患者最常见的痴呆类型是  ",
        "1）老年认知症患者最常见的痴呆类型是",
    ]
    keys = {Question(stem=v).cache_key() for v in variants}
    check("5 种题号写法归一到同一个 key", len(keys), 1)
    check("归一化结果正确", list(keys)[0], "老年认知症患者最常见的痴呆类型是")

    # ---------------- 2. 题型判定 ----------------
    print("\n[2] 题型判定")
    single = Question("下列哪项正确", [Option("A", "甲"), Option("B", "乙"), Option("C", "丙")])
    check("普通三选项 → single", quiz.classify(single.stem, single.options), "single")

    judge = Question("该说法是否正确", [Option("A", "是"), Option("B", "否")])
    check("是/否 两选项 → judge", quiz.classify(judge.stem, judge.options), "judge")

    multi = Question("以下哪些属于认知症的核心症状",
                     [Option(str(i), t) for i, t in zip("ABCD", "记忆减退 发热 失语 计算障碍".split())])
    check("含「哪些」→ multi", quiz.classify(multi.stem, multi.options), "multi")

    check("无选项 → blank", quiz.classify("请简述护理要点", []), "blank")

    # ---------------- 3. 答案抽取 ----------------
    print("\n[3] 从文本抽答案字母")
    q = Question("老年认知症患者最常见的痴呆类型是",
                 [Option("A", "阿尔茨海默病"), Option("B", "血管性痴呆"),
                  Option("C", "路易体痴呆"), Option("D", "额颞叶痴呆")])
    check("「答案：A」", quiz.extract_labels(q, "...答案：A ..."), ["A"])
    check("「正确答案是AC」", quiz.extract_labels(q, "正确答案是AC"), ["A", "C"])
    check("「【答案】 B」", quiz.extract_labels(q, "【答案】 B"), ["B"])
    check("无标记 → 空", quiz.extract_labels(q, "这里没有答案标记"), [])
    check("字母不在选项里 → 空", quiz.extract_labels(q, "答案：Z"), [])

    # ---------------- 4. 置信度打分 ----------------
    print("\n[4] 置信度打分（决定会不会瞎猜）")
    ev_exact = "本题正确答案是A，阿尔茨海默病是最常见的痴呆类型。"
    sc = quiz.score_answer(q, ["A"], ev_exact)
    check_true(f"选项文字完全命中 → 高分 ({sc:.2f})", sc > 0.9)

    ev_weak = "本题答案是A。"
    sc2 = quiz.score_answer(q, ["A"], ev_weak)
    check_true(f"证据里没有选项文字 → 低分 ({sc2:.2f})", sc2 < 0.3)

    sc3 = quiz.score_answer(q, ["Z"], ev_exact)
    check("字母不存在于选项 → 0 分", sc3, 0.0)

    m = Question("以下哪些是核心症状",
                 [Option(str(i), t) for i, t in
                  zip("ABCD", ["记忆减退", "发热", "失语", "计算障碍"])],
                 qtype="multi")
    # 证据只覆盖 A 和 C 两个选项的文字
    ev_multi = "答案：AC。记忆减退与失语均属核心症状。"
    multi_ok = quiz.score_answer(m, ["A", "C"], ev_multi)
    multi_bad = quiz.score_answer(m, ["A", "B"], ev_multi)  # B「发热」不在证据里
    check_true(f"多选答全 → 高分 ({multi_ok:.2f})", multi_ok > 0.8)
    check_true(f"多选含证据外的选项 → 低分 ({multi_bad:.2f})", multi_bad < 0.5)
    check_true("多选引入 0.85 折扣（不会给到满分 1.0）", multi_ok <= 0.85)

    # ---------------- 5. 多页面投票 ----------------
    print("\n[5] 多页面投票")
    pages = [
        ("http://a.com", "答案：A 阿尔茨海默病最常见"),
        ("http://b.com", "正确答案是A 阿尔茨海默病"),
        ("http://c.com", "答案：B 血管性痴呆"),
    ]
    labels, conf, votes, ev = quiz.vote_from_pages(q, pages)
    check("多数票选出 A", labels, ["A"])
    check("票数统计正确", votes, {"A": 2, "B": 1})
    check_true(f"多页一致有加成 ({conf:.2f})", conf > 0.8)

    # ---------------- 6. 端到端闭环 ----------------
    print("\n[6] 端到端：未命中 → 挂起 → 人工 → 入库 → 再命中")
    q2 = Question("老年认知症患者最常见的痴呆类型是",
                  [Option("A", "阿尔茨海默病"), Option("B", "血管性痴呆")])

    ans, need_human = quiz.resolve(q2, cache, debug, allow_web=False)
    check("无题库且禁网 → 需要人工", need_human, True)
    check("返回空答案", ans.labels, [])

    pending = quiz.load_pending(debug)
    check("题目已挂起", len(pending), 1)
    check("挂起项带正确 key", pending[0]["key"], q2.cache_key())
    check("挂起项保留了选项", len(pending[0]["options"]), 2)

    # 模拟人工作答
    n = quiz.resolve_pending(debug, {q2.cache_key(): ["A"]}, cache)
    check("人工答案入库", n, 1)
    check("挂起列表已清空", len(quiz.load_pending(debug)), 0)
    check("题库里有 1 条", len(cache), 1)
    check("入库来源标记为 human", cache.stats().get("human"), 1)

    # 再遇到同题（换个题号写法，验证 key 归一化真的有用）
    q2b = Question("1、老年认知症患者最常见的痴呆类型是",
                   [Option("A", "阿尔茨海默病"), Option("B", "血管性痴呆")])
    ans2, need_human2 = quiz.resolve(q2b, cache, debug, allow_web=False)
    check("同题换个题号写法 → 命中题库", need_human2, False)
    check("命中答案正确", ans2.labels, ["A"])
    check("来源标记为 cache", ans2.source, "cache")

    # ---------------- 7. 题型一致性防护 ----------------
    print("\n[7] 题库污染防护")
    mq = Question("以下哪些是核心症状",
                  [Option(str(i), t) for i, t in
                   zip("ABCD", ["记忆减退", "发热", "失语", "计算障碍"])],
                  qtype="multi")
    cache.put(mq, ["A"], source="human")          # 故意只存一个答案
    got = cache.get(mq)
    check("多选题只存一个答案 → 拒绝命中", got, None)

    # ---------------- 8. 题库损坏容错 ----------------
    print("\n[8] 题库文件损坏时的容错")
    bad_path = tmp / "bad_cache.json"
    bad_path.write_text("{ this is not json", encoding="utf-8")
    bad = AnswerCache(bad_path)
    check("损坏题库 → 空启动不抛异常", len(bad), 0)
    check("损坏文件已备份", bad_path.with_suffix(".corrupt.json").is_file(), True)

    # ---------------- 汇总 ----------------
    print("\n" + "=" * 64)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 64)

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
