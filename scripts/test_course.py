"""课程目录解析的自测。

用**实测的 OCR 输出**当夹具，验证「从杂乱文本里挑出视频条目」这件事。

运行:
    python scripts/test_course.py
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

from course import (  # noqa: E402
    CourseConfig,
    Lesson,
    enumerate_lessons,
    filter_lessons,
    is_course_complete,
    is_non_lesson,
    merge_lessons,
    parse_duration,
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


def check_true(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


# 实测的一屏 OCR 输出（文本, x, y）——直接从真机探针抄下来的
SCREEN_A = [
    ("简介", 100, 496), ("目录", 342, 497), ("更多", 582, 496),
    ("老年认知症的流行病学与疾病轨迹进展", 27, 570),
    ("4吴琳-认知症的流行病学与疾病轨迹进展.MP4", 56, 629),
    ("53:08", 660, 629),
    ("认知功能障碍患者的精神行为症状表现和处理", 28, 675),
    ("5叶尘宇-BPSD的管理和照护者心理调适.mp4", 55, 730),
    ("60:40", 660, 730),
    ("护理人文关怀在老年认知症中的应用进展", 29, 778),
    ("6李晶晶-护理人文关怀在老年认知症中的应用进展.mp4", 56, 833),
    ("43:48", 660, 834),
    ("精神运动康复在老年认知症中的应用", 29, 881),
    ("7张晓红-精神运动康复在老年认知中的应用.mp4", 56, 934),
    ("60:24", 660, 936),
    ("老年认知症人文关怀导向下的非药物干预", 29, 985),
    ("8高冰馨-老年认知症人文关怀导向下的非药物干预.mp4", 55, 1038),
    ("45:27", 660, 1037),
    ("老年认知症人文关怀导向下的护患沟通", 29, 1089),
    ("9苏伟-建立与认知症患者的有效沟通技巧.mp4", 55, 1141),
    ("55:05", 661, 1142),
    ("老年认知症患者益智康复游戏的实践", 23, 1192),
    ("10沈军-老年认知症患者益智游戏实践.mp4", 55, 1242),
    ("+", 656, 1216),          # 右下角悬浮按钮，不该被当成条目
    ("20:03", 6, 4),           # 状态栏时钟，不该被当成条目
]


def main() -> int:
    print("=" * 64)
    print(" 课程目录解析自测")
    print("=" * 64)

    # ---------------- 1. 时长识别 ----------------
    print("\n[1] 时长格式识别")
    check("mm:ss", parse_duration("45:03"), "45:03")
    check("h:mm:ss", parse_duration("1:02:33"), "1:02:33")
    check("带空格", parse_duration("  45:03  "), "45:03")
    check("非时长 → None", parse_duration("4吴琳-认知症"), None)
    check("纯数字 → None", parse_duration("10"), None)
    check("状态栏时钟也算时长（由 y 范围排除）", parse_duration("20:03"), "20:03")

    # ---------------- 2. 条目枚举 ----------------
    print("\n[2] 从杂乱 OCR 里挑出视频条目")
    lessons = enumerate_lessons(SCREEN_A)
    check("识别到 6 条", len(lessons), 6)

    titles = [l.title for l in lessons]
    durs = [l.duration for l in lessons]
    check("时长序列正确", durs, ["53:08", "60:40", "43:48", "60:24", "45:27", "55:05"])

    check_true("标题取到了视频名而不是章节名",
               all("吴琳" in t or "叶尘宇" in t or "李晶晶" in t
                   or "张晓红" in t or "高冰馨" in t or "苏伟" in t
                   for t in titles),
               f"实际标题: {titles}")

    check_true("章节标题没被误当成视频",
               not any("流行病学与疾病轨迹进展" == t for t in titles))
    check_true("右下角 + 按钮没被误当成条目",
               not any(l.tap_y == 1216 for l in lessons))
    check_true("状态栏时钟没被误当成条目",
               not any(l.tap_y == 4 for l in lessons))
    check_true("已按 y 从上到下排序",
               all(lessons[i].tap_y < lessons[i + 1].tap_y for i in range(len(lessons) - 1)))
    check_true("点击落点避开了右下角悬浮按钮",
               all(l.tap_x < 500 for l in lessons))

    # ---------------- 3. 滚动合并去重 ----------------
    print("\n[3] 滚动后合并去重（相邻两屏会有重叠）")
    # 第二屏是向上滚动后的**屏幕坐标**：与第一屏重叠 4 条，尾部多出 2 条新的。
    # y 必须落在目录区内（DIR_TOP_Y=525 .. DIR_BOTTOM_Y=1205）。
    screen_b = [
        ("6李晶晶-护理人文关怀在老年认知症中的应用进展.mp4", 56, 545),
        ("43:48", 660, 546),
        ("7张晓红-精神运动康复在老年认知中的应用.mp4", 56, 646),
        ("60:24", 660, 648),
        ("8高冰馨-老年认知症人文关怀导向下的非药物干预.mp4", 55, 750),
        ("45:27", 660, 749),
        ("9苏伟-建立与认知症患者的有效沟通技巧.mp4", 55, 853),
        ("55:05", 661, 854),
        ("10沈军-老年认知症患者益智游戏实践.mp4", 55, 954),
        ("46:19", 659, 954),
        ("11新的一节-第十二讲.mp4", 55, 1057),
        ("50:00", 659, 1057),
    ]
    a = enumerate_lessons(SCREEN_A)
    b = enumerate_lessons(screen_b)
    check("第二屏识别到 6 条", len(b), 6)

    merged = merge_lessons(a, b)
    # 重叠的是 李晶晶/张晓红/高冰馨/苏伟 这 4 条
    check("合并后共 8 条（去掉了 4 条重叠）", len(merged), 8)
    check_true("合并后没有重复标题+时长",
               len({(l.title, l.duration) for l in merged}) == len(merged))

    # ---------------- 4. 播放状态在合并中保留 ----------------
    print("\n[4] 合并时保留播放状态")
    a2 = enumerate_lessons(SCREEN_A)
    a2[0].played = True
    merged2 = merge_lessons(a2, b)
    done = [l for l in merged2 if l.played]
    check("已播放标记被保留", len(done), 1)
    check("保留的是同一条", done[0].duration if done else None, "53:08")

    # ---------------- 5. 时长换算 ----------------
    print("\n[5] 时长换算（用于估算看护上限）")
    check("45:03 → 2703 秒", int(Lesson("x", "45:03", 0).seconds), 2703)
    check("1:02:33 → 3753 秒", int(Lesson("x", "1:02:33", 0).seconds), 3753)

    # ---------------- 6. 空输入容错 ----------------
    print("\n[6] 异常输入容错")
    check("空列表 → 空结果", enumerate_lessons([]), [])
    check("只有章节标题 → 空结果",
          enumerate_lessons([("流行病学", 27, 570), ("认知功能", 28, 675)]), [])
    check("时长在目录区外 → 被排除",
          enumerate_lessons([("45:03", 660, 300)]), [])

    stray = enumerate_lessons([("45:03", 660, 700)])
    check("只有时长没有标题 → 仍生成条目（标题回退）", len(stray), 1)
    check_true("回退标题可辨认", "未识别标题" in stray[0].title)

    # ---------------- 7. 配置默认值合理性 ----------------
    print("\n[7] 配置默认值")
    cfg = CourseConfig()
    check_true("单课上限大于最长视频（60:40）",
               cfg.lesson_max_seconds > 60 * 60 + 40,
               f"{cfg.lesson_max_seconds}s")
    check_true("整门课上限能容下 10 个 60 分钟视频",
               cfg.course_max_seconds >= 10 * 3600,
               f"{cfg.course_max_seconds}s")

    # ---------------- 8. 非章节浮层过滤 ----------------
    print("\n[8] 目录里的非章节项要剔除")
    check_true("「视频插题」不是章节", is_non_lesson("视频插题：1 2"))
    check_true("「签到」不是章节", is_non_lesson("每日签到"))
    check_true("正常视频是章节", not is_non_lesson("1花佩-认知症长期照护.mp4"))

    mixed = [
        Lesson("视频插题：1 2", "00:00", 600),
        Lesson("1花佩-认知症长期照护.mp4", "45:00", 700),
        Lesson("签到", "00:00", 800),
    ]
    kept = filter_lessons(mixed)
    check("过滤后只剩 1 个真章节", len(kept), 1)
    check("留下的是视频条目", kept[0].title, "1花佩-认知症长期照护.mp4")

    # ---------------- 9. 「全部学完」判定 ----------------
    print("\n[9] 「全部学完」判定（决定要不要进考核）")

    # 9a) 平台文案判据
    for text, want in [
        ("已学完", True),
        ("学习进度100%", True),
        ("已完成", True),
        ("未完成", False),
        ("未学完", False),
        ("目录 简介 更多", False),
    ]:
        check(f"文案 {text!r} → {want}", is_course_complete(text), want)

    # 9b) 本地记录兜底（目录没有完成标记，只能靠自己播过的记录）
    done_lessons = [
        Lesson("1甲.mp4", "10:00", 600, played=True),
        Lesson("2乙.mp4", "10:00", 700, played=True),
    ]
    check("全部播过 → 判定学完", is_course_complete("", done_lessons), True)

    partial = [
        Lesson("1甲.mp4", "10:00", 600, played=True),
        Lesson("2乙.mp4", "10:00", 700, played=False),
    ]
    check("只播了一半 → 未学完", is_course_complete("", partial), False)
    check("空列表 → 未学完", is_course_complete("", []), False)

    # 9c) 浮层项不该影响判定
    tricky = [
        Lesson("视频插题：1 2", "00:00", 600, played=True),
        Lesson("1甲.mp4", "10:00", 700, played=False),
    ]
    check("插题播过但正课没播 → 未学完",
          is_course_complete("", filter_lessons(tricky)), False)

    # 9d) 明确的「未完成」文案优先于本地记录
    check("平台说未完成时，即使本地都播过也判未完成",
          is_course_complete("未完成", done_lessons), False)

    print("\n" + "=" * 64)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 64)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
