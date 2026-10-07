"""考核结果页解析的自测。

用**实测的 OCR 输出**当夹具，验证「从结果页读出正确答案」这件事。
这是「先故意答错、再从结果页取官方答案」策略的核心，解析错了整条链就废了。

运行:
    python scripts/test_harvest.py
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

from harvest_answers import normalize_correct, parse_rows  # noqa: E402

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


# 实测的一屏 OCR 输出（文本, x, y）
SCREEN = [
    ("本项目考核", 21, 96),
    ("60分", 652, 138),
    ("1、脓毒症患者达到血压目标后仍需监测的是 （单选题 5分） X", 15, 240),
    ("A、体温", 15, 288),
    ("B、电解质", 15, 340),
    ("C、凝血功能", 15, 393),
    ("D、微循环指标", 15, 445),
    ("您的答案：A", 20, 530),
    ("正确答案：D", 20, 558),
    ("答案解析:", 15, 578),
    ("暂无", 15, 600),
    ("2、以下哪种情况会导致静脉回流减少 （单选题 5分） X", 15, 638),
    ("A、PEEP过高", 15, 688),
    ("B、深吸气", 15, 740),
    ("C、抬高足端", 15, 792),
    ("D、抬高头位", 15, 845),
    ("您的答案：B", 20, 898),
    ("正确答案：A", 20, 926),
    ("答案解析:", 17, 950),
    ("暂无", 15, 978),
    ("3、以下哪项不是评估组织灌注的指标 （单选题 5分） √", 15, 1030),
    ("A、尿量", 15, 1078),
    ("B、微循环评分", 15, 1130),
    ("C、血红蛋 白", 15, 1183),
    ("D、血乳酸", 15, 1235),
    ("您的答案：C", 19, 1266),
]

# 判断题屏
SCREEN_JUDGE = [
    ("11、中心静脉压可以准确反映右心室前负荷。（判断题 5分） X", 15, 240),
    ("T.正确", 15, 288),
    ("F.错误", 15, 340),
    ("您的答案：T", 20, 425),
    ("正确答案：F", 20, 453),
    ("答案解析:", 15, 473),
    ("暂无", 15, 495),
    ("14、VA-ECMO患者必须同时监测桡动脉和股动脉血气。(判断题 5分)", 15, 540),
    ("T.正确", 15, 588),
    ("F.错误", 15, 640),
    ("您的答案：F", 20, 725),
    ("正确答案：T", 20, 753),
]


def main() -> int:
    print("=" * 64)
    print(" 考核结果页解析自测")
    print("=" * 64)

    # 1) 基本解析
    print("\n[1] 单选屏解析")
    items = parse_rows(SCREEN)
    check("解析出 3 题", len(items), 3)
    check("题号正确", [i["num"] for i in items], [1, 2, 3])

    first = items[0]
    check("第 1 题正确答案", first["correct"], "D")
    check("第 1 题我的答案", first["mine"], "A")
    check("第 1 题题型", first["qtype"], "single")
    check("第 1 题选项数", len(first["options"]), 4)
    check("第 1 题选项文字", [o["text"] for o in first["options"]],
          ["体温", "电解质", "凝血功能", "微循环指标"])

    # 2) 题干末尾的对错标记必须清掉
    print("\n[2] 题干末尾的对错标记（关键：会污染题库 key）")
    for it in items:
        stem = it["stem"]
        check_true(f"第 {it['num']} 题题干不含尾部 X/√",
                   not stem.rstrip().endswith(("X", "x", "×", "√", "✓")),
                   f"实际: {stem!r}")
    check_true("题干保留了分值括号",
               "（单选题 5分）" in first["stem"] or "（单选题5分）" in first["stem"],
               f"实际: {first['stem']!r}")
    check_true("题干正文完整", "脓毒症患者达到血压目标后仍需监测的是" in first["stem"])

    # 3) 选项行不该把「正确答案」当成选项
    print("\n[3] 选项收集不能把答案行混进来")
    for it in items:
        labels = [o["label"] for o in it["options"]]
        check_true(f"第 {it['num']} 题选项只有 A-F",
                   all(l in "ABCDEF" for l in labels), f"实际: {labels}")
        check_true(f"第 {it['num']} 题选项里没有「答案」二字",
                   not any("答案" in o["text"] for o in it["options"]))

    # 4) 判断题
    print("\n[4] 判断题解析")
    jitems = parse_rows(SCREEN_JUDGE)
    check("解析出 2 题", len(jitems), 2)
    check("第 11 题正确答案 F", jitems[0]["correct"], "F")
    check("第 11 题题型 judge", jitems[0]["qtype"], "judge")
    check("第 11 题选项是 T/F", [o["label"] for o in jitems[0]["options"]], ["T", "F"])
    check("第 14 题正确答案 T", jitems[1]["correct"], "T")

    # 5) 答案规范化
    print("\n[5] 答案规范化")
    check("单选 D", normalize_correct({"correct": "D", "qtype": "single"}), ["D"])
    check("多选 AC", normalize_correct({"correct": "AC", "qtype": "multi"}), ["A", "C"])
    check("多选乱序 CA → 保序", normalize_correct({"correct": "CA", "qtype": "multi"}), ["C", "A"])
    check("判断题 T", normalize_correct({"correct": "T", "qtype": "judge"}), ["T"])
    check("判断题 A→T（兼容）", normalize_correct({"correct": "A", "qtype": "judge"}), ["T"])
    check("判断题 B→F（兼容）", normalize_correct({"correct": "B", "qtype": "judge"}), ["F"])
    check("判断题只取一个", normalize_correct({"correct": "TF", "qtype": "judge"}), ["T"])
    check("空答案", normalize_correct({"correct": "", "qtype": "single"}), [])
    check("单选里混入 T → 剔掉", normalize_correct({"correct": "AT", "qtype": "single"}), ["A"])

    # 6) 异常输入
    print("\n[6] 异常输入容错")
    check("空列表", parse_rows([]), [])
    check("只有答案没有题干 → 不产出题目",
          len(parse_rows([("正确答案：D", 0, 100)])), 0)
    check("题干缺分值 → 不误判为题目",
          len(parse_rows([("随便一句话(单选题)", 0, 100)])), 0)

    # 7) 与题库的衔接：题干归一化后应能互相匹配
    print("\n[7] 与题库的衔接（重做时能否命中）")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from quiz import Question

    # 结果页读到的题干
    harvested = Question(stem=first["stem"]).cache_key()
    # 重做时从答题页读到的题干（实测格式：带题号、无对错标记）
    redo = Question(stem="1、脓毒症患者达到血压目标后仍需监测的是(5分)").cache_key()
    check_true("结果页题干与重做时题干归一化后一致",
               harvested == redo,
               f"\n           结果页: {harvested!r}\n           重做时: {redo!r}")

    print("\n" + "=" * 64)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
