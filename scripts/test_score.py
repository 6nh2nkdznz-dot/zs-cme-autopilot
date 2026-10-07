"""成绩读取与重做判定的自测。

运行:
    python scripts/test_score.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from score import DEFAULT_PASS_SCORE, parse_score, should_retake  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
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


# 实测的结果页文本
RESULT_PAGE = (
    "本项目考核 考核类型：期末大作业 题目类型：题库作业 "
    "本次成绩：60分 最高成绩：60分 60分 "
    "1、脓毒症患者达到血压目标后仍需监测的是（单选题5分）"
)

# 实测的考核列表页文本（还没交过卷时）
LIST_PAGE_UNDONE = (
    "本项目考核 期末大作业-题库作业 最高成绩：未做 "
    "起止时间：2026-05-12 00:00:00 至 2026-11-30 23:59:59 开始答题"
)

# 列表页，已交过卷、有分数
LIST_PAGE_DONE = (
    "本项目考核 期末大作业-题库作业 最高成绩：85分 "
    "起止时间：2026-05-12 00:00:00 至 2026-11-30 23:59:59 重做"
)


def main() -> int:
    print("=" * 64)
    print(" 成绩读取与重做判定自测")
    print("=" * 64)

    # ---------------- 1. 成绩解析 ----------------
    print("\n[1] 成绩解析")
    s = parse_score(RESULT_PAGE)
    check("本次成绩", s.this_time, 60)
    check("最高成绩", s.best, 60)
    check_true("标记为已解析", s.parsed)

    check("中文冒号/空格变体",
          parse_score("本次成绩 60分 最高成绩 60分").this_time, 60)
    check("全角冒号", parse_score("本次成绩：75分").this_time, 75)
    check("无「分」字也能读", parse_score("本次成绩：75").this_time, 75)

    # ---------------- 2. 未做 / 无成绩 ----------------
    print("\n[2] 没有成绩的情况")
    s2 = parse_score(LIST_PAGE_UNDONE)
    check("「未做」→ 读不到本次成绩", s2.this_time, None)
    check("没有最高成绩", s2.best, None)
    check_true("列表页「未做」但页面有 2026-… 这类数字，不能误读成分数",
               not s2.parsed, f"实际: {s2.describe()}")

    check("空文本", parse_score("").parsed, False)

    # ---------------- 3. 通用回退的边界 ----------------
    print("\n[3] 通用回退「N分」的边界")
    check("只有孤立分数时能读", parse_score("成绩 88分").this_time, 88)
    check("超 100 的不当分数", parse_score("倒计时 55天 步数 200分").this_time, None)
    # 「55天03时29分37秒」里的「29分」不该被当成绩
    s3 = parse_score("截止倒计时：55天03时29分37秒")
    check_true("倒计时里的「29分」不被误读",
               s3.this_time != 29, f"实际: {s3.describe()}")

    # ---------------- 4. 重做判定 ----------------
    print("\n[4] 重做判定（阈值 80）")
    check("默认阈值", DEFAULT_PASS_SCORE, 80)

    cases = [
        # (说明, 本次, 最高, 期望是否重做)
        ("60 分，都低 → 重做", 60, 60, True),
        ("79 分，差一点 → 重做", 79, 79, True),
        ("80 分，刚好达标 → 不重做", 80, 80, False),
        ("100 分 → 不重做", 100, 100, False),
        ("本次60但历史85 → 不重做", 60, 85, False),
        ("本次85 → 不重做", 85, 60, False),
        ("都读不到 → 不重做（保守）", None, None, False),
    ]
    for label, this_v, best_v, want in cases:
        from score import ScoreInfo

        si = ScoreInfo(this_time=this_v, best=best_v, raw="test")
        got, why = should_retake(si)
        check(f"{label}  ({why})", got, want)

    # ---------------- 5. 阈值可配 ----------------
    print("\n[5] 阈值可配")
    from score import ScoreInfo

    si = ScoreInfo(this_time=70, best=70, raw="test")
    check("阈值 60 时 70 分不重做", should_retake(si, pass_score=60)[0], False)
    check("阈值 80 时 70 分要重做", should_retake(si, pass_score=80)[0], True)
    check("阈值 100 时 70 分要重做", should_retake(si, pass_score=100)[0], True)

    # ---------------- 6. 原因文案要能看懂 ----------------
    print("\n[6] 原因文案")
    _, why = should_retake(ScoreInfo(this_time=60, best=60, raw=""))
    check_true("低分说明里带分数和阈值",
               "60" in why and "80" in why, f"实际: {why}")
    _, why2 = should_retake(ScoreInfo(this_time=60, best=85, raw=""))
    check_true("历史达标时说明清楚",
               "85" in why2, f"实际: {why2}")
    _, why3 = should_retake(ScoreInfo(this_time=None, best=None, raw=""))
    check_true("读不到时说明不重做", "不重做" in why3, f"实际: {why3}")

    print("\n" + "=" * 64)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
