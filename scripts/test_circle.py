"""目录里「完成圆圈」检测的自测。

用**真实截图**当夹具，因为判据是从实测标定出来的，合成数据测不出真实
抗锯齿/亮度波动的影响。

## 为什么需要这个模块

`course.py` 原来的注释写的是「目录里**没有**已完成的视觉标记」——
**这是错的**，用户实测纠正：文件名左边的蓝色圆圈就是完成标记。

* 完整蓝圈 = 已看完
* 半蓝圈   = 看了一部分
* 空圈     = 没看过

因为那个错误假设，程序只能靠自维护的 `course_progress.json` 判「哪节看过」，
而那份记录会与实际不符 → **去点已经看完的课**（用户就是这么发现的）。

## 两个夹具，第二个是回归用例

`20261007-094743_circles.png` 是最早标定用的那张，圆心相对 OCR 标题
稳定在 +10~+12。

`circle_bug_now.png` 是**列表滚动之后**的截图，圆心偏移变成
+0/−5/−10/−13/−19。第一版代码在这里全面失败（第 5 行满圈只量到
109 个蓝像素 < 阈值 260 → 判成「没看过」，于是回头重看已经看完的课）。
用户报的「圆圈检测识别错误」就是它。**这个夹具必须一直留着。**

运行:
    python scripts\\test_circle.py
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

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from circles import (  # noqa: E402
    FILLED_RATIO,
    FULL_WIDTH,
    LessonState,
    circle_shape,
    state_at,
)

PASS = 0
FAIL = 0

_SNAP = Path(__file__).resolve().parent.parent / "debug" / "snap"

#: 最早标定的那张截图，同时含完整圈和半圈。
FIXTURE = _SNAP / "20261007-094743_circles.png"

#: 滚动后的截图。第一版代码在这里失败，是回归用例。
FIXTURE_SCROLLED = _SNAP / "circle_bug_now.png"

#: 用户提供的三种状态官方参考图（文件名即状态）
REF_DIR = (Path(__file__).resolve().parent.parent
           / "assets" / "resource" / "circle_ref")

#: 标定截图的真值：(说明, 标题行 OCR y, 期望状态)
TRUTH = [
    ("1花佩-…45:00", 649, LessonState.DONE),
    ("2朱琳-…56:31", 754, LessonState.PARTIAL),
    ("3杜鹏-…60:33", 856, LessonState.PARTIAL),
    ("4吴琳-…53:08", 1040, LessonState.DONE),
    ("5叶尘宇-…60:40", 1142, LessonState.DONE),
]

#: 滚动截图的真值：同样的课，但 y 完全不同，且圆心偏移漂了。
#: 注意第 3 节在这张里是**完整圈**（用户看完了），与上一张不同。
TRUTH_SCROLLED = [
    ("1花佩-…45:00", 661, LessonState.DONE),
    ("2朱琳-…56:31", 769, LessonState.PARTIAL),
    ("3杜鹏-…60:33", 877, LessonState.DONE),
    ("4吴琳-…53:08", 1064, LessonState.DONE),
    ("5叶尘宇-…60:40", 1172, LessonState.DONE),
]


def check_true(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n         期望 {want!r}\n         实际 {got!r}")


def _run_truth(im: np.ndarray, truth, title: str) -> list[tuple[str, int, str]]:
    """跑一组真值，返回 (说明, 宽度, 状态) 列表供余量检查。"""
    print(f"\n{title}")
    out = []
    for name, y, want in truth:
        sh = circle_shape(im, y)
        out.append((name, sh.width, sh.state))
        check(f"{name}（宽 {sh.width} 比例 {sh.ratio:.2f}）",
              sh.state, want)
    return out


def main() -> int:
    print("=" * 68)
    print(" 目录「完成圆圈」检测自测")
    print("=" * 68)

    print("\n[1] 几何判据（纯函数，不依赖截图）")
    print(f"       完整圈宽 {FULL_WIDTH}，比例阈值 {FILLED_RATIO}")
    black = np.zeros((1280, 720, 3), dtype=np.uint8)

    print("\n[2] 异常输入不该崩")
    check("None 图", circle_shape(None, 100).state, LessonState.UNKNOWN)
    check("空数组", circle_shape(
        np.zeros((0, 0, 3), dtype=np.uint8), 10).state, LessonState.UNKNOWN)
    check("负 y 不崩", isinstance(circle_shape(black, -50).state, str), True)
    check("超大 y 不崩", isinstance(circle_shape(black, 99999).state, str), True)

    print("\n[3] 全黑图没有蓝 → 没看过")
    check("全黑 → 没看过", state_at(black, 500), LessonState.NONE)
    check("全黑 → 宽度 0", circle_shape(black, 500).width, 0)

    print(f"\n[4] 标定截图（{FIXTURE.name}）")
    widths_a: list[tuple[str, int, str]] = []
    if not FIXTURE.is_file():
        print(f"  SKIP  夹具不存在: {FIXTURE}")
    else:
        im = np.array(Image.open(FIXTURE).convert("RGB"))
        check("图尺寸是 720x1280", (im.shape[1], im.shape[0]), (720, 1280))
        widths_a = _run_truth(im, TRUTH, "  —— 逐行判定 ——")

    print(f"\n[5] 滚动后的截图（{FIXTURE_SCROLLED.name}）"
          "  ← 第一版代码在这里失败")
    widths_b: list[tuple[str, int, str]] = []
    if not FIXTURE_SCROLLED.is_file():
        print(f"  SKIP  夹具不存在: {FIXTURE_SCROLLED}")
        print("        （这是回归用例，缺失就失去了对「固定偏移」bug 的防护）")
    else:
        im_b = np.array(Image.open(FIXTURE_SCROLLED).convert("RGB"))
        widths_b = _run_truth(im_b, TRUTH_SCROLLED, "  —— 逐行判定 ——")

    print("\n[6] 判据要有余量，不能卡在边界")
    #
    # 完整圈宽 20、半圈宽 10，阈值比例 0.75 对应宽度 15。
    # 必须离两边都够远，否则轻微的抗锯齿差异就会翻盘。
    allw = widths_a + widths_b
    done_w = [w for _n, w, s in allw if s == LessonState.DONE]
    half_w = [w for _n, w, s in allw if s == LessonState.PARTIAL]
    if done_w and half_w:
        cut = FILLED_RATIO * FULL_WIDTH
        print(f"       完整圈宽度 {sorted(set(done_w))}，半圈宽度 "
              f"{sorted(set(half_w))}，判定分界 {cut:.1f}")
        gap_low = cut - max(half_w)
        gap_high = min(done_w) - cut
        print(f"       下余量 {gap_low:.1f}，上余量 {gap_high:.1f}")
        check_true("分界离半圈至少 2px", gap_low >= 2.0, f"实际 {gap_low:.1f}")
        check_true("分界离完整圈至少 2px", gap_high >= 2.0,
                   f"实际 {gap_high:.1f}")

    print("\n[7] 三种状态的官方参考图（用户提供的原始图标）")
    #
    # 这三张是用户直接从界面上截下来的原始图标，文件名即状态：
    #     已完成.png   → 整圆全蓝（蓝区宽 21）
    #     完成一半.png → 只填右半（蓝区宽 11）
    #     未完成.png   → 几乎是白色空圈（0 蓝像素）
    #
    # 特意验证「未完成」：它接近白色，如果蓝色判据太松
    # （比如只看 b > r 而不要求足够差值）就会被误判成已看完。
    # 实测它的 b-r 只有 4，而蓝色是 180，区分度极大。
    #
    # 注意参考图是 **22x22 的紧裁图**，圆比真实截图（20px 直径）略大，
    # 所以比例会到 1.05/0.55 —— 都在阈值 0.75 的两侧，不影响判定。
    if not REF_DIR.is_dir():
        print(f"  SKIP  参考图目录不存在: {REF_DIR}")
    else:
        for name, want, desc in (("已完成", LessonState.DONE, "已完成（整圈）"),
                                 ("完成一半", LessonState.PARTIAL, "完成一半（半圈）"),
                                 ("未完成", LessonState.NONE, "未完成（空圈）")):
            path = REF_DIR / f"{name}.png"
            if not path.is_file():
                check(f"{desc} 参考图存在", False, True)
                continue
            im = np.array(Image.open(path).convert("RGB"))
            # 紧裁图只有 22px 高，圆基本占满，row_y 取 0 附近即可
            sh = circle_shape(im, 0)
            check(f"{desc} 宽={sh.width} 比例={sh.ratio:.2f}",
                  sh.state, want)

        print("\n[8] 空圈的蓝色差值必须远低于阈值（防误判成已看完）")
        none_path = REF_DIR / "未完成.png"
        if none_path.is_file():
            im = np.array(Image.open(none_path).convert("RGB"))
            r = im[:, :, 0].astype(int)
            b = im[:, :, 2].astype(int)
            max_delta = int((b - r).max())
            print(f"       空圈最大 b-r = {max_delta}，判据阈值 = 40")
            check_true("空圈不触发蓝色判据", max_delta < 40,
                       f"实际 {max_delta}")

    print("\n[9] 固定偏移 bug 的专项防护")
    #
    # 第一版假设「圆心 = OCR 标题 y + CIRCLE_DY(12)」，采样窗只有 ±16px。
    # 滚动后实际偏移是 −19，圆圈整个落在窗外。
    # 这个用例直接验证：同一节课在两张不同滚动的截图里都判对。
    if FIXTURE.is_file() and FIXTURE_SCROLLED.is_file():
        a = np.array(Image.open(FIXTURE).convert("RGB"))
        b = np.array(Image.open(FIXTURE_SCROLLED).convert("RGB"))
        # 5叶尘宇：标定图偏移 +11，滚动图偏移 −19，差 30px
        st_a = state_at(a, 1142)
        st_b = state_at(b, 1172)
        print(f"       同一节课：标定图 → {st_a}，滚动图 → {st_b}")
        check("两处都判为已看完", (st_a, st_b),
              (LessonState.DONE, LessonState.DONE))

    print("\n[10] 通道顺序：circle_shape 要求 RGB，喂 BGR 会全盘判错")
    #
    # 蓝色判据按通道**位置**取（`ch2 - ch0 > 40`），所以通道顺序错了就完全
    # 失效。这条用例把「必须喂 RGB」锁住。
    if FIXTURE.is_file():
        rgb = np.array(Image.open(FIXTURE).convert("RGB"))
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        y = 1040          # 4吴琳，完整圈
        check("RGB 输入判为已看完", state_at(rgb, y), LessonState.DONE)
        check("BGR 输入判为没看过（喂错通道顺序会全盘判错）",
              state_at(bgr, y), LessonState.NONE)

        # 像素级确认判据方向。坐标从**检测结果**取（圆心），不要写死 ——
        # 之前写死 x=37 就取到了圆外的白像素，测试假失败。
        sh = circle_shape(rgb, y)
        cx = sh.left + sh.width // 2
        cy = sh.top + sh.height // 2
        r_px = rgb[cy, cx].astype(int)
        b_px = bgr[cy, cx].astype(int)
        print(f"       圆心 ({cx},{cy})：RGB={list(r_px)} → ch2-ch0="
              f"{r_px[2] - r_px[0]}；BGR={list(b_px)} → ch2-ch0="
              f"{b_px[2] - b_px[0]}")
        check_true("RGB 下圆心像素通过蓝色判据",
                   r_px[2] - r_px[0] > 40, f"实际 {r_px[2] - r_px[0]}")
        check_true("BGR 下同一像素不通过（证明顺序错了会失效）",
                   b_px[2] - b_px[0] < 0, f"实际 {b_px[2] - b_px[0]}")

    print("\n[11] channels.to_rgb 能把抓屏的 BGR 转成 RGB")
    #
    # 只测纯函数，**不连设备**。之前这里写的是「连设备抓屏、断言 ch2 最大」，
    # 那条断言本身就是错的 —— 抓屏其实是 BGR，它之所以通过，是因为采样时
    # 画面上叠着视频播放器，采样区落到了别的帧。教训：判通道顺序要在
    # **同一份数组**上同时看像素值和判定结果，别分两次抓屏对比。
    #
    # 端到端的真机验证放在 `scripts/check_circle_now.py`，那里是按上述
    # 原则写的（一次抓屏、两种输入一起跑）。
    import channels

    bgr_row = np.array([[[250, 160, 70], [10, 20, 30]]], dtype=np.uint8)
    rgb_row = channels.to_rgb(bgr_row)
    check("首尾通道互换", list(rgb_row[0, 0]), [70, 160, 250])
    check("转出来的数组是 RGB（蓝最大）",
          bool(rgb_row[0, 0, 2] > rgb_row[0, 0, 0]), True)

    if FIXTURE.is_file():
        rgb_img = np.array(Image.open(FIXTURE).convert("RGB"))
        round_trip = channels.to_rgb(np.ascontiguousarray(rgb_img[:, :, ::-1]))
        check("BGR→RGB 往返后判定恢复正确",
              state_at(round_trip, 1040), LessonState.DONE)

    check("灰度图原样返回不崩", channels.to_rgb(
        np.zeros((10, 10), dtype=np.uint8)).shape, (10, 10))
    check("None 原样返回", channels.to_rgb(None), None)

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
