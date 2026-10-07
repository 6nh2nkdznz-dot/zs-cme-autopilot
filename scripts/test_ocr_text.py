"""OCR 行合并的自测。

夹具全部来自**真机实测输出**，包括那个把「最高成绩：」和「60」
拆成两段的真实案例。

运行:
    python scripts/test_ocr_text.py
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

from ocr_text import merge_same_line, rows_to_text  # noqa: E402
from score import parse_score  # noqa: E402

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


# 实测：考核列表页的成绩区。注意「最高成绩：」和「60」是**两段**。
REAL_EXAM_LIST = [
    ("21:09", 2, 0),
    ("考核", 336, 28),
    ("考核", 330, 82),
    ("本项目考核", 64, 150),
    ("再做一次", 599, 152),
    ("期末大作业-题库作业", 64, 192),
    ("成绩报告", 607, 202),
    ("最高成绩：", 66, 222),
    ("60", 152, 222),
    ("起止时间：2026-05-1200:00:00至2026-11-3023:59:59", 66, 250),
]

# 实测：结果页顶部
REAL_RESULT = [
    ("本项目考核", 21, 96),
    ("60分", 652, 138),
    ("本次成绩：60分", 6, 228),
    ("最高成绩：60分", 6, 256),
    ("1、脓毒症患者达到血压目标后仍需监测的是（单选题5分）X", 6, 308),
]


def main() -> int:
    print("=" * 64)
    print(" OCR 行合并自测")
    print("=" * 64)

    # ---------------- 1. 基本合并 ----------------
    print("\n[1] 同一行的碎片要拼起来")
    lines = merge_same_line(REAL_EXAM_LIST)
    merged = dict(lines)
    check_true("「最高成绩：」与「60」被合并到同一行",
               any("最高成绩" in t and "60" in t for t, _y in lines),
               f"实际行: {[t for t,_ in lines]}")

    # ---------------- 2. 合并后正则能匹配 ----------------
    print("\n[2] 合并后成绩能被解析（关键）")
    text = rows_to_text(REAL_EXAM_LIST)
    check_true("整页文本含「最高成绩： 60」",
               "最高成绩" in text and "60" in text,
               f"实际: {text[:120]!r}")

    s = parse_score(text)
    check("读出最高成绩 60", s.best, 60)
    check("不把起止时间的年份当本次成绩", s.this_time, None)

    # 对照：不合并时的表现。
    # 注意——正则里冒号已是可选，所以「最高成绩： 60」这种被空格隔开的情况
    # 不合并也能读到。合并的价值在于**更稳**：一旦 OCR 把冒号也省掉或
    # 换成别的分隔符，不合并就会失败。这里只断言合并后结果正确，
    # 不断言「不合并一定失败」——那个断言的前提不成立。
    naive = " ".join(t for t, _x, _y in REAL_EXAM_LIST)
    s_naive = parse_score(naive)
    check("不合并时（冒号可选兜底）也能读到 60", s_naive.best, 60)

    # 真正体现合并价值的情况：标签与数值之间被 OCR 插入了干扰内容，
    # 或者冒号丢失。构造这类输入验证合并后的稳健性。
    tricky = [
        ("最高成绩", 66, 222),
        ("", 100, 222),
        ("60", 152, 222),
    ]
    s3 = parse_score(rows_to_text(tricky))
    check("冒号丢失时合并仍能读到 60", s3.best, 60)

    # ---------------- 3. 结果页不受影响 ----------------
    print("\n[3] 本来就完整的行不该被破坏")
    text2 = rows_to_text(REAL_RESULT)
    s2 = parse_score(text2)
    check("结果页本次成绩", s2.this_time, 60)
    check("结果页最高成绩", s2.best, 60)
    check_true("题干保持完整",
               "脓毒症患者达到血压目标后仍需监测的是" in text2,
               f"实际: {text2[:160]!r}")

    # ---------------- 4. 排序与分桶 ----------------
    print("\n[4] 排序与分桶")
    check("空输入", merge_same_line([]), [])
    check("单行", merge_same_line([("A", 10, 100)]), [("A", 100)])

    mixed = [("下面", 10, 300), ("上面", 10, 100), ("中间", 10, 200)]
    ys = [y for _t, y in merge_same_line(mixed)]
    check("按 y 升序输出", ys, [100, 200, 300])

    # y 差距在容差内的应合并
    close = [("左", 10, 100), ("右", 100, 105)]
    got = merge_same_line(close)
    check("y 差 5px 合并为一行", len(got), 1)

    # y 差距超过容差的不能合并
    far = [("上", 10, 100), ("下", 10, 140)]
    check("y 差 40px 不合并", len(merge_same_line(far)), 2)

    # ---------------- 5. 大间隙要隔开 ----------------
    print("\n[5] 同一行但水平间隙很大时插空格")
    wide = [("左栏", 10, 100), ("右栏", 640, 100)]
    got_text = merge_same_line(wide)[0][0]
    check_true("大间隙处有空格分隔", " " in got_text, f"实际: {got_text!r}")

    narrow = [("前", 10, 100), ("后", 60, 100)]
    got2 = merge_same_line(narrow)[0][0]
    check_true("相邻无大间隙则直接相连", " " not in got2, f"实际: {got2!r}")

    # ---------------- 6. 空片段要被忽略 ----------------
    print("\n[6] 空片段")
    with_empty = [("A", 10, 100), ("   ", 50, 100), ("B", 80, 100)]
    got3 = merge_same_line(with_empty)
    check("空白片段不产生额外行", len(got3), 1)
    check_true("空白片段不进文本", got3[0][0] in ("AB", "A B"), f"实际: {got3[0][0]!r}")

    print("\n" + "=" * 64)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
