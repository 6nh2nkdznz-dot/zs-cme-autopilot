"""量播放按钮的坐标，并钉住「切课之后要按播放」这条链路。

## 为什么必须**量**而不是推

用户实测：「新课程切换后需要再点击一次播放按键」。要在 `course.py` 里写死
这个坐标，就得先知道它在哪。

我第一版按「播放键应该在当前时间左边一点」推成 `(41, 473)` —— **推错了**，
那是时间文字那一侧。所以这个测试拿真实截图
（`debug/progress-check/20261007-162142_01.png`）量一遍，并把结论钉死。

## 三层验证

1. **坐标层**：拿真截图量出播放图标的包围盒，断言 `course.PLAY_TAP` 落在里面；
   同时断言它不会点歪到右边的时间文字或进度条上。
2. **接线层**：`play_lesson()` 必须在 `watch_one()` **之前**调用 `_start_playing()`，
   否则看护会一直等满 stall_timeout 才判卡住，一节 45~60 分钟白跑。
3. **容错层**：没注入、抛异常、返回 False 三种情况都不能中断整门课。

运行：`python scripts\\test_play_button.py`
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import course  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
#: 量坐标用的那张真机截图（控制条可见）
SHOT = ROOT / "debug" / "progress-check" / "20261007-162142_01.png"
SRC = Path(__file__).resolve().parent / "course.py"
MAIN_SRC = Path(__file__).resolve().parent / "main.py"
PIPELINE = (ROOT / "assets" / "resource" / "pipeline" / "00_navigation.json")

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def play_icon_bbox(img: np.ndarray, x0: int = 4, x1: int = 50,
                   y0: int = 450, y1: int = 490):
    """量控制条最左边那个播放/暂停/重播图标的包围盒。

    只在这个小窗口里找「亮像素」：控制条里的图标和时间文字都是白色，
    而它下面几行是浅色幻灯片（整行都亮），所以 y 必须卡在图标那一行。
    """
    sub = img[y0:y1, x0:x1]
    bright = sub.min(axis=2) > 140
    ys, xs = np.nonzero(bright)
    if len(ys) == 0:
        return None
    return (int(xs.min()) + x0, int(ys.min()) + y0,
            int(xs.max()) + x0, int(ys.max()) + y0)


def time_text_bbox(img: np.ndarray, x0: int = 52, x1: int = 105,
                   y0: int = 450, y1: int = 490):
    """量「当前时间」文字的包围盒（用来确认播放键没点在它上面）。"""
    sub = img[y0:y1, x0:x1]
    bright = sub.min(axis=2) > 140
    ys, xs = np.nonzero(bright)
    if len(ys) == 0:
        return None
    return (int(xs.min()) + x0, int(ys.min()) + y0,
            int(xs.max()) + x0, int(ys.max()) + y0)


def main() -> int:
    print("\n[1] 播放按钮坐标：拿真截图量，断言常量落在图标里")
    src = SRC.read_text(encoding="utf-8")
    if not SHOT.exists():
        print(f"  [SKIP] 量坐标的截图不在了: {SHOT}")
    else:
        img = np.asarray(Image.open(SHOT).convert("RGB")).astype(int)
        box = play_icon_bbox(img)
        check("量到了播放图标", box is not None)
        if box:
            bx0, by0, bx1, by1 = box
            px, py = course.PLAY_TAP
            print(f"       图标包围盒 x {bx0}..{bx1} y {by0}..{by1}"
                  f"   常量 PLAY_TAP=({px}, {py})")
            check("PLAY_TAP 的 x 落在图标里", bx0 <= px <= bx1,
                  f"x={px} 不在 {bx0}..{bx1}")
            check("PLAY_TAP 的 y 落在图标里", by0 <= py <= by1,
                  f"y={py} 不在 {by0}..{by1}")
            check("PLAY_TAP 在图标中心 8px 内",
                  abs(px - (bx0 + bx1) // 2) <= 8 and
                  abs(py - (by0 + by1) // 2) <= 8,
                  f"({px},{py}) vs 中心 ({(bx0+bx1)//2},{(by0+by1)//2})")
        tbox = time_text_bbox(img)
        if tbox:
            tx0 = tbox[0]
            check("PLAY_TAP 没碰右边的时间文字", course.PLAY_TAP[0] < tx0 - 10,
                  f"PLAY_TAP.x={course.PLAY_TAP[0]} 太靠近时间文字 x={tx0}")

    print("\n[2] PLAY_TAP 与控制条区域自洽")
    x, y = course.PLAY_TAP
    tx, ty, tw, th = course.PLAYER_TIME_ROI
    check("PLAY_TAP 在时间 ROI 的左边（播放键在时间左边）", x < tx,
          f"{x} vs {tx}")
    check("PLAY_TAP 的 y 落在时间那一条带上", ty - 10 <= y <= ty + th + 10,
          f"y={y} 不在 {ty - 10}..{ty + th + 10}")
    check("PLAY_TAP 靠左边（控制条最左）", x <= 60, f"x={x}")
    check("PLAY_TAP 是个二元组", isinstance(course.PLAY_TAP, tuple)
          and len(course.PLAY_TAP) == 2)

    print("\n[3] play_lesson：必须在 watch_one 之前把播放按起来")
    body = src.split("def play_lesson")[1].split("def ")[0]
    i_start = body.find("_start_playing")
    i_watch = body.find("_watch_one")
    check("play_lesson 里调了 _start_playing", i_start > 0)
    check("_start_playing 在 _watch_one 之前", 0 < i_start < i_watch,
          f"start={i_start} watch={i_watch}")

    print("\n[4] _start_playing 的三种容错都能过（不能中断整门课）")
    calls: list[str] = []

    def mk(ensure):
        return course.CourseRunner(
            controller=_FakeController(),
            ocr_full=lambda: [],
            watch_one=lambda lesson: True,
            log=calls.append,
            ensure_playing=ensure,
        )

    check("没注入 ensure_playing → 返回 True（不报错）",
          mk(None)._start_playing(_lesson()) is True)
    check("没注入时不打警告",
          not any("按播放" in c for c in calls))
    calls.clear()

    check("注入且返回 True → True", mk(lambda _l: True)._start_playing(_lesson()) is True)
    check("确认在播时打的是 ✓ 日志",
          any("已确认这一节在播放" in c for c in calls), str(calls))
    calls.clear()

    check("注入但返回 False → False（不抛）",
          mk(lambda _l: False)._start_playing(_lesson()) is False)
    check("没确认在播时如实打警告",
          any("没确认到它在播" in c for c in calls), str(calls))
    calls.clear()

    def boom(_l):
        raise RuntimeError("点击失败了")

    check("注入会抛异常 → False，异常不外泄",
          mk(boom)._start_playing(_lesson()) is False)
    check("异常被记进日志",
          any("按播放时出错" in c for c in calls), str(calls))
    check("异常消息带上了原因",
          any("点击失败了" in c for c in calls), str(calls))

    print("\n[5] play_lesson 端到端：切课 → 按播放 → 看护（顺序可观测）")
    order: list[str] = []

    class _Recorder(course.CourseRunner):
        def _click(self, x, y):
            order.append("switch")

        def _start_playing(self, lesson):
            order.append("play")
            return super()._start_playing(lesson)

    r = _Recorder(
        controller=_FakeController(),
        ocr_full=lambda: [],
        watch_one=lambda lesson: order.append("watch") or True,
        log=lambda _m: None,
        # "ensure" 是**被 _start_playing 内部调起来**的那一下，
        # 所以它一定夹在 "play" 和 "watch" 中间。
        ensure_playing=lambda _l: order.append("ensure") or True,
    )
    # 把 sleep 掉到 0，测试别真等 6 秒
    old_settle = r.cfg.settle_seconds
    r.cfg.settle_seconds = 0
    try:
        r.play_lesson(_lesson(), 1, 1)
    finally:
        r.cfg.settle_seconds = old_settle
    check("顺序是 切课 → 按播放（内部走 ensure）→ 看护",
          order == ["switch", "play", "ensure", "watch"], str(order))
    check("按播放早于看护",
          order.index("ensure") < order.index("watch"), str(order))

    print("\n[6] main.py 接线：play_tap 参数 + ensure_playing 传到 runner")
    msrc = MAIN_SRC.read_text(encoding="utf-8")
    check("认 play_tap 参数", 'param.get("play_tap")' in msrc)
    check("play_tap 兜底用 course.PLAY_TAP",
          "or list(PLAY_TAP)" in msrc)
    check("从 course 导入 PLAY_TAP", "PLAY_TAP" in msrc.split("\n")[0:60][-1]
          or re.search(r"from course import[^\n]*PLAY_TAP", msrc) is not None)
    check("helpers 里返回了 ensure_playing",
          '"ensure_playing": ensure_playing' in msrc)
    check("CourseRunner 收到了 ensure_playing",
          "ensure_playing=(lambda" in msrc)
    check("确认在播的间隔是常量",
          "PLAY_CONFIRM_SECONDS" in msrc)

    print("\n[7] 命令行独立 OCR：读数用 time_roi（不是另抄一份坐标）")
    check("read_position 用 time_roi 裁图", "img[y:y + h, x:x + w]" in msrc)
    check("ensure_playing 读两次做差",
          msrc.count("secs(read_position())") == 2,
          f"出现 {msrc.count('secs(read_position())')} 次")
    check("最多按两下播放键", "for attempt in range(3)" in msrc)

    print("\n[8] 管线 JSON：播放整门课 带上 play_tap")
    import json

    data = json.loads(PIPELINE.read_text(encoding="utf-8"))
    node = data.get("播放整门课", {})
    p = node.get("custom_action_param", {})
    check("节点里有 play_tap", "play_tap" in p, str(sorted(p)))
    check("play_tap 与常量一致", p.get("play_tap") == list(course.PLAY_TAP),
          f"{p.get('play_tap')} vs {list(course.PLAY_TAP)}")
    check("注释里说明了为什么要按播放",
          any("播放" in c for c in _as_list(node.get("_comment", ""))))

    print("\n[9] 评分简答题：取区间**上限**（用户明确「写100就行」）")
    body2 = msrc.split("if rng is not None:")[1].split("# --- 类型 1")[0]
    check("填的是上限 hi", re.search(r"\bval\s*=\s*hi\b", body2) is not None,
          body2[:200])
    check("不再用「中位偏上」那套", "lo + (hi - lo)" not in body2)

    print(f"\n 结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


def _as_list(v) -> list[str]:
    if isinstance(v, str):
        return [v]
    return list(v or [])


class _FakeController:
    def post_click(self, *a, **k):
        return _FakeJob()

    def post_swipe(self, *a, **k):
        return _FakeJob()

    def post_screencap(self, *a, **k):
        return _FakeJob()


class _FakeJob:
    succeeded = True

    def wait(self):
        return self

    def get(self):
        return None


def _lesson() -> course.Lesson:
    return course.Lesson(title="测试课.mp4", duration="10:00", tap_y=600)


if __name__ == "__main__":
    raise SystemExit(main())
