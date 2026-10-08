"""`CourseRunner` 的「全部学完」判定自测。

## 为什么单独测这个

`all_complete` 这一个布尔值决定了**要不要进入考核**。它错成 False，
表现是「明明全学完了，却一直卡在『仍有 N 个视频未达标』、考核入口不触发」——
而且日志上看不出哪里不对。

实测踩过的两个坑，都让 `all_complete` 永远为假：

1. **目录枚举条数不稳定**。目录是虚拟列表，每次枚举到的条数不同
   （同一门课时而 9 个、时而 10 个，取决于滚动时的渲染时机）。
   本次没枚举到的条目 `played` 永远是 False。

2. **「找不到条目位置」被算成 failed**。那只说明本次没滚到它，
   不代表视频没学完；算成 failed 就永远 `failed != 0`。

所以判据改成「本次播过的 ∪ 本地记录说学完的」，并且把「没滚到」
与「没达标」分开计数。

全部用假的 controller / OCR，不碰真机。

运行:
    python scripts\\test_course_runner.py
"""

from __future__ import annotations

import json
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

from course_progress import (  # noqa: E402
    CourseProgress,
    lesson_key,
    rebuild_key,
)
from course import (  # noqa: E402
    DIR_TOP_Y,
    CourseConfig,
    CourseRunner,
    Lesson,
    merge_lessons,
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


class FakeController:
    """只记录调用，不做任何真实输入。"""

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []
        self.swipes = 0

    def post_click(self, x, y):
        self.clicks.append((x, y))
        return self

    def post_swipe(self, *a, **k):
        self.swipes += 1
        return self

    def wait(self):
        return self


def fresh(*lessons: Lesson) -> list[Lesson]:
    """复制一批 Lesson 并清掉 played 状态。

    ## 为什么需要

    `list(lessons)` 是**浅拷贝** —— Lesson 对象仍是同一批引用，
    而 `run()` 会把 `played` 置 True。于是前一个用例跑完，
    后面用例拿到的就是「已播放过」的对象，走的是 `continue` 分支，
    测的根本不是它想测的东西。

    实测踩过：用例 [5] 期望 pending=2，实际 0，因为对象被 [1] 污染了。
    所以每个用例都要拿**干净**的对象。
    """
    return [
        Lesson(title=l.title, duration=l.duration, tap_y=l.tap_y)
        for l in lessons
    ]


def make_runner(lessons, watch_one, is_done=None, log=None, cfg=None):
    """构造一个 CourseRunner，目录固定为 `lessons`。

    注意 rows 的构造：每一条要占**独立的 y**（实测行距约 102~104）。
    早先把 y 算错导致两条挤在同一行，`enumerate_lessons` 就解析出了
    两个同名条目——测试因此测的不是我想测的东西。
    """
    ys = [DIR_TOP_Y + 10 + 110 * i for i in range(len(lessons))]
    rows = [(l.title, 10, y) for l, y in zip(lessons, ys)]
    rows += [(l.duration, 659, y) for l, y in zip(lessons, ys)]

    r = CourseRunner(
        controller=FakeController(),
        ocr_full=lambda: rows,
        watch_one=watch_one,
        cfg=cfg or CourseConfig(),
        log=log or (lambda m: None),
        is_done=is_done,
    )
    # 跳过目录枚举：直接喂固定的条目列表，专注测 all_complete 判定
    r.scan_lessons = lambda max_scrolls=6: list(lessons)  # type: ignore[assignment]
    # 认为条目始终可见，避免滚动逻辑干扰
    r._ensure_visible = lambda lesson, max_tries=8: True  # type: ignore[assignment]
    # 不真的去问「微信还活着吗」。
    #
    # ## 为什么必须打桩
    #
    # 这个判定会 shell 出去问 adb + 查 `com.tencent.mm` 在不在，**结果取决于
    # 模拟器此刻的状态**。实测踩过：模拟器开着、微信没开时它返回 False，
    # `run()` 于是在第一节之前就 `break` —— 此时 `results` 是空的，而
    # `all(v == "done" for v in {}.values())` **对空字典恒为真**，
    # 于是：
    #
    #   * `done` 停在 0（用例 [13] 报「计入 done 期望 1 实际 0」）
    #   * `all_complete` 反而变成 True（测试"通过"了，但通过的理由是错的）
    #
    # 最坏的一种：测试的结论随模拟器开关而变。这个文件测的是**纯逻辑**，
    # 不该有外部依赖，所以固定成 True。
    r._ensure_app_alive = lambda: True  # type: ignore[assignment]
    return r


def main() -> int:
    print("=" * 68)
    print(" CourseRunner「全部学完」判定自测")
    print("=" * 68)

    L1 = Lesson(title="第一讲.mp4", duration="50:00", tap_y=DIR_TOP_Y + 10)
    L2 = Lesson(title="第二讲.mp4", duration="50:00", tap_y=DIR_TOP_Y + 120)
    L3 = Lesson(title="第三讲.mp4", duration="50:00", tap_y=DIR_TOP_Y + 230)

    print("\n[1] 全部播完 → all_complete")
    r = make_runner(fresh(L1, L2), watch_one=lambda l: True)
    info = r.run()
    check("all_complete", info["all_complete"], True)
    check("done 计数", info["done"], 2)
    check("failed 计数", info["failed"], 0)

    print("\n[2] 有一个没达标 → 不算学完（这是对的，本来就没学完）")
    r = make_runner(fresh(L1, L2), watch_one=lambda l: l.title != "第二讲.mp4")
    info = r.run()
    check("all_complete", info["all_complete"], False)
    check("failed 计数", info["failed"], 1)

    print("\n[3] 关键：本次没枚举到的条目，本地记录说已学完 → 仍算学完")
    # 场景：上轮枚举到 3 个、这次只枚举到 2 个（虚拟列表渲染差异）。
    # L3 不在 lessons 里，但 is_done 说它学完了。
    r = make_runner(
        fresh(L1, L2),
        watch_one=lambda l: True,
        is_done=lambda l: l.title == "第三讲.mp4",
    )
    info = r.run()
    check("all_complete", info["all_complete"], True)

    print("\n[4] 本地记录里没有、这次也没枚举到 → 无从判断，按已枚举的算")
    r = make_runner(fresh(L1, L2), watch_one=lambda l: True, is_done=lambda l: False)
    info = r.run()
    check("all_complete", info["all_complete"], True)

    print("\n[5] 「没滚到条目」不该算 failed（否则考核永远不触发）")
    r = make_runner(fresh(L1, L2), watch_one=lambda l: True)
    r._ensure_visible = lambda lesson, max_tries=8: False  # type: ignore[assignment]
    info = r.run()
    check("pending 计数", info.get("pending"), 2)
    check("failed 不该被记", info["failed"], 0)
    check("没滚到就谈不上学完 → all_complete 仍为 False",
          info["all_complete"], False)

    print("\n[6] 空目录")
    r = make_runner(fresh(), watch_one=lambda l: True)
    info = r.run()
    check("total", info["total"], 0)
    check("all_complete 为假（没有条目谈不上学完）", info["all_complete"], False)

    print("\n[7] is_done 为 None 时不崩")
    r = make_runner(fresh(L1), watch_one=lambda l: True, is_done=None)
    info = r.run()
    check("正常完成", info["all_complete"], True)

    print("\n[8] is_done 抛异常不该让整门课崩")
    def boom(_l):
        raise RuntimeError("模拟记录文件坏了")

    r = make_runner(fresh(L1), watch_one=lambda l: True, is_done=boom)
    try:
        info = r.run()
        check("异常被吞掉并继续", info["all_complete"], True)
    except RuntimeError:
        check("异常被吞掉并继续", "抛出了异常", "应被吞掉")

    print("\n[9] 目录重复条目要去重（merge_lessons 保序）")
    dup = merge_lessons([L1, L2], [L2, L3])
    check("去重后 3 条", len(dup), 3)
    check("顺序保持", [l.title for l in dup],
          ["第一讲.mp4", "第二讲.mp4", "第三讲.mp4"])

    print("\n[11] 课节键：标点漂移要合并，但时长不能被归一化掉")

    # 实测同一节 OCR 出两种标点
    k1 = lesson_key("4吴琳-认知症的流行病学与疾病轨迹进展.MP4", "53:08")
    k2 = lesson_key("4吴琳-认知症的流行病学与疾病轨迹进展，MP4", "53:08")
    check("标点漂移 -> 同一个键", k1, k2)
    check_true("时长保留冒号", "|53:08" in k1, f"实际 {k1}")

    # 这是我修标点漂移时**引入的新 bug**：把时长的冒号也去掉了，
    # 于是同一节课多出第三条记录（`…MP4|5308`）。
    bad = lesson_key("4吴琳认知症的流行病学与疾病轨迹进展MP4", "5308")
    check_true("坏键与好键不同（所以要迁移清掉）", bad != k1, f"实际 {bad}")

    print("\n[12] 迁移：坏键丢弃、重复合并、幂等")
    tmp2 = Path(tempfile.mkdtemp())
    f = tmp2 / "p.json"
    f.write_text(
        json.dumps({
            "某课程": {
                "lessons": {
                    "A.MP4|53:08": "2026-01-01 10:00:00",
                    "A，MP4|53:08": "2026-01-01 11:00:00",   # 标点漂移，应合并
                    "AMP4|5308": "2026-01-01 12:00:00",      # 坏键，应丢弃
                },
                "updated": "2026-01-01 12:00:00",
            }
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    p4 = CourseProgress(f)
    keys = list(p4._data["某课程"]["lessons"])
    check("合并后只剩一条", len(keys), 1)
    check("坏键被丢弃", keys[0], rebuild_key("A.MP4|53:08"))
    check("保留了较新的时间戳",
          p4._data["某课程"]["lessons"][keys[0]], "2026-01-01 11:00:00")
    check("再迁移一次不再变化（幂等）", p4._migrate(), False)

    shutil.rmtree(tmp2, ignore_errors=True)

    LONG = "老年认知症患者护理人文关怀实"
    SHORT = "老年认知症患者护理人文关怀"
    tmp = Path(tempfile.mkdtemp())

    p = CourseProgress(tmp / "a.json")
    p.mark_done(LONG, "A", "10:00")
    p.mark_done(SHORT, "B", "20:00")
    check("已存在长键时不分裂", len(p._data), 1)
    check("沿用已有键（保持稳定）", list(p._data)[0], LONG)
    check("两节都记在同一门下", p.done_count(SHORT), 2)

    p2 = CourseProgress(tmp / "b.json")
    p2.mark_done(SHORT, "A", "10:00")
    p2.mark_done(LONG, "B", "20:00")
    check("反向也集中", len(p2._data), 1)
    check("反向仍用短键", list(p2._data)[0], SHORT)

    p3 = CourseProgress(tmp / "c.json")
    p3.mark_done(SHORT, "A", "10:00")
    check("别的课程不误命中", p3.is_done("肝胆肿瘤整合治疗", "A", "10:00"), False)
    p3.mark_done("肝胆肿瘤整合治疗", "B", "1:00")
    check("别的课程新建独立键", len(p3._data), 2)

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n[13] 平台圆圈决定跳过（权威判据，优先于本地记录）")
    #
    # 用户实测指出：目录里文件名左边的蓝色圆圈就是平台的完成标记
    # （完整=已看完 / 半圈=看了一半）。原先误以为「目录没有完成标记」，
    # 只能靠本地记录判，于是**去点已经看完的课**。
    def mk_lesson(title, state):
        l = Lesson(title=title, duration="50:00", tap_y=600)
        l.platform_state = state
        return l

    watched: list[str] = []

    def watch_rec(l):
        watched.append(l.title)
        return True

    print("  -- 完整蓝圈 → 必须跳过（不能重看）")
    r = make_runner([mk_lesson("已看完.mp4", "done")], watch_one=watch_rec)
    info = r.run()
    check("没有去点它", watched, [])
    check("计入 done", info["done"], 1)
    check("all_complete", info["all_complete"], True)

    print("  -- 半蓝圈 → 必须重看（没看完）")
    watched.clear()
    r = make_runner([mk_lesson("看了一半.mp4", "partial")], watch_one=watch_rec)
    info = r.run()
    check("确实去看了", watched, ["看了一半.mp4"])
    check("all_complete", info["all_complete"], True)

    print("  -- 空圈 → 必须看")
    watched.clear()
    r = make_runner([mk_lesson("没看过.mp4", "none")], watch_one=watch_rec)
    r.run()
    check("确实去看了", watched, ["没看过.mp4"])

    print("  -- 圆圈与本地记录冲突时，以圆圈为准")
    watched.clear()
    r = make_runner([mk_lesson("圆圈说没看完.mp4", "partial")],
                    watch_one=watch_rec, is_done=lambda l: True)
    r.run()
    check("圆圈优先 → 仍然重看", watched, ["圆圈说没看完.mp4"])

    watched.clear()
    r = make_runner([mk_lesson("圆圈说看完了.mp4", "done")],
                    watch_one=watch_rec, is_done=lambda l: False)
    r.run()
    check("圆圈优先 → 仍然跳过", watched, [])

    print("\n[14] 「立即停止」：课与课之间也要查，而且**绝不能进考核**")
    #
    # 用户实测：点了「停止」，视频还继续播到下课。单课内部靠
    # `WatchConfig.should_stop` 打断（见 test_video_end.py 的 [13]），
    # 但一节看完之后若不在这里拦一下，仍会把下一节点开、再从头看护一遍。
    #
    # ⚠️ 最要命的是 `all_complete`：它的判据里有「本地记录说学过也算」这条
    # 兜底，所以中途停止后，剩下那几节若正好都有**过期的本地记录**，
    # 会被算成「学完」→ 自动进考核。**用户刚点了停止，程序却自己去考试了。**
    print("  -- 一开始就要求停止 → 一节都不开")
    watched.clear()
    r = make_runner(fresh(L1, L2, L3), watch_one=watch_rec,
                    cfg=CourseConfig(should_stop=lambda: True))
    info = r.run()
    check("一节都没看", watched, [])
    check("stopped 标记", info.get("stopped"), True)
    check("all_complete 必须是假", info["all_complete"], False)

    print("  -- 看完第一节后要求停止 → 不再开第二节")
    watched.clear()
    state = {"n": 0}

    def watch_then_stop(l):
        watched.append(l.title)
        state["n"] += 1
        return True

    r = make_runner(fresh(L1, L2, L3), watch_one=watch_then_stop,
                    cfg=CourseConfig(should_stop=lambda: state["n"] >= 1))
    info = r.run()
    check("只看了第一节", watched, ["第一讲.mp4"])
    check("stopped 标记", info.get("stopped"), True)
    check("all_complete 必须是假", info["all_complete"], False)

    print("  -- ★ 停止 + 剩下几节都有过期的本地记录 → 仍然不许进考核")
    #
    # 这是最容易出事的一种：本地 course_progress.json 里记着「看完了」，
    # 但这一轮压根没看。不挡的话 all_complete 会被那几条兜底算成 True，
    # 上层（main.py 的 WatchCourse.run）就会 `self._enter_exam(...)`。
    #
    # ⚠️ 注意 `is_done` 要**排除第一节**：本地记录命中时 `run()` 会直接
    # `continue`，根本不调 `watch_one` —— 那样 state2["n"] 永远是 0，
    # 停止条件永远不成立，测的就不是「停止」而是「跳过」了。
    # （第一版就是这么写的，三条断言全挂，实际一条都没走到停止分支。）
    watched.clear()
    state2 = {"n": 0}

    def watch_first_only(l):
        watched.append(l.title)
        state2["n"] += 1
        return True

    r = make_runner(fresh(L1, L2, L3), watch_one=watch_first_only,
                    is_done=lambda l: l.title != "第一讲.mp4",   # 后两节「本地记过」
                    cfg=CourseConfig(should_stop=lambda: state2["n"] >= 1))
    info = r.run()
    check("只看了一节", watched, ["第一讲.mp4"])
    check("★ all_complete 仍然是假", info["all_complete"], False)
    check("stopped 标记", info.get("stopped"), True)

    print("  -- 没要求停止时，stopped 必须是假（别把正常跑完也标成中断）")
    watched.clear()
    r = make_runner(fresh(L1, L2), watch_one=lambda l: True,
                    cfg=CourseConfig(should_stop=lambda: False))
    info = r.run()
    check("all_complete", info["all_complete"], True)
    check("stopped", info.get("stopped"), False)

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
