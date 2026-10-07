"""页面识别层的自测。

夹具全部来自**真机实测的 OCR 输出**，包括那些把脚本骗过去的页面对。

运行:
    python scripts\\test_page_detect.py
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

from exam import (  # noqa: E402
    PAGE_ANSWER,
    PAGE_COURSE,
    PAGE_DIALOG,
    PAGE_EXAM_ENTRY,
    PAGE_LEARNING_LIST,
    PAGE_RESULT,
    PAGE_UNKNOWN,
    detect_page,
    looks_like_answer_page,
    page_name,
)

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


# ---------------- 实测夹具 ----------------

# 结果页：有成绩区 + 逐题答案对照
RESULT_PAGE = (
    "22:49 本次成绩：55分 最高成绩：60分 "
    "1、以下哪项不是评估组织灌注的指标(单选题 5分) X "
    "A、尿量 B、微循环评分 C、血红蛋 白 D、血乳酸 "
    "您的答案：C 正确答案：A 答案解析: 暂无 "
    "2、血流动力学监测中的\"前负荷\"主要指（单选题5分） "
    "您的答案：B 正确答案：C"
)

# 答题页：底部导航三件套，**没有**结果页字样
ANSWER_PAGE = (
    "22:50 20、中心静脉压可以准确反映右心室前负荷。(5分) "
    "F.错误 T.正确 二、判断题 "
    "course.zs-hospital.sh.cn 本项目考核 上一题 答题卡 下一题 提交"
)

# 这是把脚本骗过去的那一对：结果页里**也**含答题页会匹配的词。
# 结果页的逐题回顾会出现题型行和题号，历史上我用「一、选择题」判断就误判了。
RESULT_PAGE_WITH_ANSWER_WORDS = (
    "本次成绩：45分 最高成绩：60分 "
    "一、选择题（单选) 1、脓毒症患者达到血压目标后仍需监测的是（单选题5分） "
    "您的答案：A 正确答案：D "
    "二、判断题 11、中心静脉压可以准确反映右心室前负荷。(5分) "
    "您的答案：T 正确答案：F"
)

COURSE_PAGE = (
    "课程学习 简介 目录 更多 "
    "血流动力学监测基础 01罗哲血流动力学监测基础.mp4 55:14"
)

EXAM_ENTRY_PAGE = (
    "本项目考核 期末大作业-题库作业 最高成绩： 60 "
    "起止时间：2026-05-1200:00:00至2026-11-3023:59:59 再做一次 成绩报告"
)

# 考核说明页：正文是平台规则，底部按钮「进入答题」
EXAM_RULES_PAGE = (
    "本项目考核 1、客观题考核可以重复提交。 2、主观题考核需要老师手动批改 "
    "4、学生重复提交考核时，系统会记录重做次数。 进入答题"
)

LEARNING_LIST_PAGE = (
    "我的学习 全部 未结课 已结课 筛选 "
    "老年认知症患者护理人文关怀实.. 9小时 去学习 申请结课"
)

DIALOG_PAGE = ANSWER_PAGE + " 温馨提示 单选题 第 3 未选，立即去做题？ 去做题 取消"


def main() -> int:
    print("=" * 68)
    print(" 页面识别层自测")
    print("=" * 68)

    # ---------------- 1. 逐页判定 ----------------
    print("\n[1] 各页面判定")
    check("结果页", detect_page(RESULT_PAGE), PAGE_RESULT)
    check("答题页", detect_page(ANSWER_PAGE), PAGE_ANSWER)
    check("课程页", detect_page(COURSE_PAGE), PAGE_COURSE)
    check("考核列表页", detect_page(EXAM_ENTRY_PAGE), PAGE_EXAM_ENTRY)
    check("考核说明页", detect_page(EXAM_RULES_PAGE), PAGE_EXAM_ENTRY)
    check("学习列表页", detect_page(LEARNING_LIST_PAGE), PAGE_LEARNING_LIST)
    check("提示弹窗", detect_page(DIALOG_PAGE), PAGE_DIALOG)
    check("空文本", detect_page(""), PAGE_UNKNOWN)
    check("无关文本", detect_page("hello world 12345"), PAGE_UNKNOWN)

    # ---------------- 2. 优先级：这是历史上出过错的地方 ----------------
    print("\n[2] 结果页优先于答题页（历史上误判过）")
    check("结果页里混有答题页词汇 → 仍判结果页",
          detect_page(RESULT_PAGE_WITH_ANSWER_WORDS), PAGE_RESULT)
    check("该页不能判成答题页",
          looks_like_answer_page(RESULT_PAGE_WITH_ANSWER_WORDS), False)
    check("真答题页仍判答题页", looks_like_answer_page(ANSWER_PAGE), True)

    # ---------------- 3. 指定页面查询 ----------------
    print("\n[3] 指定页面查询（驱动脚本主要用这个）")
    check("结果页问「是答题页吗」→ 否",
          detect_page(RESULT_PAGE, PAGE_ANSWER), PAGE_UNKNOWN)
    check("答题页问「是答题页吗」→ 是",
          detect_page(ANSWER_PAGE, PAGE_ANSWER), PAGE_ANSWER)
    check("结果页问「是结果页吗」→ 是",
          detect_page(RESULT_PAGE, PAGE_RESULT), PAGE_RESULT)
    check("答题页问「是结果页吗」→ 否",
          detect_page(ANSWER_PAGE, PAGE_RESULT), PAGE_UNKNOWN)
    check("课程页问「是答题页吗」→ 否",
          detect_page(COURSE_PAGE, PAGE_ANSWER), PAGE_UNKNOWN)

    # ---------------- 4. 结果页判定的稳健性 ----------------
    print("\n[4] 结果页判定不能被滚动影响")
    check("只有成绩、没有对照 → 不是结果页（那是考核列表页）",
          detect_page("本次成绩：45分 最高成绩：60分 再做一次", PAGE_RESULT),
          PAGE_UNKNOWN)
    check("只有对照、没有成绩 → **仍然**是结果页",
          detect_page("您的答案：A 正确答案：D 答案解析: 暂无", PAGE_RESULT),
          PAGE_RESULT)

    # 实测：滚到卷子中后段时，顶部成绩区已滚出屏幕，只剩逐题对照。
    # 早先要求「成绩区 + 对照」两个都命中，这种页面会被判成未知。
    SCROLLED_RESULT = (
        "23:09 F、错误 T、正确 17、体位改变对静脉回流没有影响。（判断题5分）X "
        "18、导管测量的混合静脉血氧饱和度可反映组织氧供需平衡。(判断题5分) "
        "19、术后患者血压正常就说明血流动力学稳定。(判断题5分) "
        "答案解析: 暂无 正确答案：F 您的答案：T 正确答案：T 您的答案：F "
        "course.zs-hospital.sh.cn 本项目考核"
    )
    check("滚动后的结果页（无成绩区）→ 仍判结果页",
          detect_page(SCROLLED_RESULT), PAGE_RESULT)
    check("滚动后的结果页 → 不是答题页",
          looks_like_answer_page(SCROLLED_RESULT), False)

    # ---------------- 5. 课程页需要三个 tab 同现 ----------------
    print("\n[5] 课程页要求 简介/目录/更多 三者同现")
    check("只有「更多」不算课程页",
          detect_page("更多 别的页面内容", PAGE_COURSE), PAGE_UNKNOWN)
    check("三个都有才算",
          detect_page("简介 目录 更多", PAGE_COURSE), PAGE_COURSE)

    # ---------------- 6. 中文名 ----------------
    print("\n[6] 页面中文名")
    check("结果页名字", page_name(PAGE_RESULT), "结果页")
    check("答题页名字", page_name(PAGE_ANSWER), "答题页")
    check("未知页名字", page_name(PAGE_UNKNOWN), "未知页面")

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
