"""目录里「完成圆圈」检测的自测。

用**真实截图**当夹具（`debug/snap/20261007-094743_circles.png`），
因为阈值是从实测标定出来的，用合成数据测不出真实抗锯齿的影响。

## 为什么需要这个模块

`course.py` 原来的注释写的是「目录里**没有**已完成的视觉标记」——
**这是错的**，用户实测纠正：文件名左边的蓝色圆圈就是完成标记。

* 完整蓝圈 = 已看完
* 半蓝圈   = 看了一部分
* 空圈     = 没看过

因为那个错误假设，程序只能靠自维护的 `course_progress.json` 判「哪节看过」，
而那份记录会与实际不符 → **去点已经看完的课**（用户就是这么发现的）。

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
    FULL_PIXELS,
    LessonState,
    circle_pixels,
    classify,
    state_at,
)

PASS = 0
FAIL = 0

#: 实测截图。它同时含「完整圈」和「半圈」，正好当夹具。
FIXTURE = (Path(__file__).resolve().parent.parent
           / "debug" / "snap" / "20261007-094743_circles.png")

#: 用户提供的三种状态官方参考图（文件名即状态）
REF_DIR = (Path(__file__).resolve().parent.parent
           / "assets" / "resource" / "circle_ref")

#: 从那张图里肉眼读出的真值：(说明, 标题行 OCR y, 期望状态)
TRUTH = [
    ("1花佩-…45:00", 649, LessonState.DONE),
    ("2朱琳-…56:31", 754, LessonState.PARTIAL),
    ("3杜鹏-…60:33", 856, LessonState.PARTIAL),
    ("4吴琳-…53:08", 1040, LessonState.DONE),
    ("5叶尘宇-…60:40", 1142, LessonState.DONE),
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


def main() -> int:
    print("=" * 68)
    print(" 目录「完成圆圈」检测自测")
    print("=" * 68)

    print("\n[1] 阈值分类（纯函数，不依赖截图）")
    check("0 个蓝像素 → 没看过", classify(0), LessonState.NONE)
    check("1 个蓝像素 → 部分", classify(1), LessonState.PARTIAL)
    check("半圈实测值 154 → 部分", classify(154), LessonState.PARTIAL)
    check("半圈实测上限 221 → 部分", classify(221), LessonState.PARTIAL)
    check(f"阈值 {FULL_PIXELS} → 完整（边界含）",
          classify(FULL_PIXELS), LessonState.DONE)
    check("完整圈实测值 298 → 完整", classify(298), LessonState.DONE)
    check("完整圈实测上限 301 → 完整", classify(301), LessonState.DONE)

    print("\n[2] 异常输入不该崩")
    check("None 图", circle_pixels(None, 100), 0)
    check("空数组", circle_pixels(np.zeros((0, 0, 3), dtype=np.uint8), 10), 0)
    check("负 y", circle_pixels(np.zeros((1280, 720, 3), dtype=np.uint8), -50), 0)
    check("超大 y", circle_pixels(np.zeros((1280, 720, 3), dtype=np.uint8), 99999), 0)

    print("\n[3] 全黑图应当没有任何蓝像素")
    black = np.zeros((1280, 720, 3), dtype=np.uint8)
    check("全黑 → 0", circle_pixels(black, 500), 0)
    check("全黑 → 没看过", state_at(black, 500), LessonState.NONE)

    print(f"\n[4] 真实截图（{FIXTURE.name}）")
    if not FIXTURE.is_file():
        print(f"  SKIP  夹具不存在: {FIXTURE}")
        print("        （这是实测截图，缺失时无法验证阈值）")
    else:
        im = np.array(Image.open(FIXTURE).convert("RGB"))
        check("图尺寸是 720x1280", (im.shape[1], im.shape[0]), (720, 1280))
        for name, y, want in TRUTH:
            px = circle_pixels(im, y)
            check(f"{name}（蓝像素 {px}）", state_at(im, y), want)

        print("\n[5] 判据要有余量，不能卡在边界")
        # 完整圈最少 298、半圈最多 221。阈值必须离两者都够远，
        # 否则截图亮度一变就会误判。
        done_vals = [circle_pixels(im, y) for _n, y, w in TRUTH
                     if w == LessonState.DONE]
        half_vals = [circle_pixels(im, y) for _n, y, w in TRUTH
                     if w == LessonState.PARTIAL]
        if done_vals and half_vals:
            gap_low = FULL_PIXELS - max(half_vals)
            gap_high = min(done_vals) - FULL_PIXELS
            print(f"       半圈最大 {max(half_vals)}，阈值 {FULL_PIXELS}，"
                  f"完整圈最小 {min(done_vals)}")
            print(f"       下余量 {gap_low}，上余量 {gap_high}")
            check_true("阈值离半圈至少 30", gap_low >= 30, f"实际 {gap_low}")
            check_true("阈值离完整圈至少 30", gap_high >= 30, f"实际 {gap_high}")

    print("\n[6] 三种状态的官方参考图（用户提供的原始图标）")
    #
    # 这三张是用户直接从界面上截下来的原始图标，文件名即状态：
    #     已完成.png   → 整圆全蓝
    #     完成一半.png → 只填右半
    #     未完成.png   → 几乎是白色空圈
    #
    # 特意验证「未完成」那张：它接近白色，如果蓝色判据太松
    # （比如只看 b > r 而不要求足够差值）就会被误判成已看完。
    # 实测它的 b-r 只有 4，而蓝色是 180，区分度极大。
    refs = REF_DIR
    if not refs.is_dir():
        print(f"  SKIP  参考图目录不存在: {refs}")
    else:
        # 参考图是 22x22 的图标本体，不是整屏截图，
        # 所以这里直接对图标应用同一个蓝色判据、按面积分类。
        full = refs / "已完成.png"
        half = refs / "完成一半.png"
        none = refs / "未完成.png"
        for path, want, desc in ((full, "done", "已完成（整圈）"),
                                 (half, "partial", "完成一半（半圈）"),
                                 (none, "none", "未完成（空圈）")):
            if not path.is_file():
                check(f"{desc} 参考图存在", False, True)
                continue
            im = np.array(Image.open(path).convert("RGB"))
            r = im[:, :, 0].astype(int)
            b = im[:, :, 2].astype(int)
            blue = int(((b - r > 40) & (b > 120)).sum())
            # 22x22 图标：整圈约 387，半圈约 206，空圈 0
            got = ("done" if blue >= 300 else
                   "partial" if blue > 0 else "none")
            check(f"{desc} 蓝像素={blue}", got, want)

        print("\n[7] 空圈的蓝色差值必须远低于阈值（防误判成已看完）")
        if none.is_file():
            im = np.array(Image.open(none).convert("RGB"))
            r = im[:, :, 0].astype(int)
            b = im[:, :, 2].astype(int)
            max_delta = int((b - r).max())
            print(f"       空圈最大 b-r = {max_delta}，判据阈值 = 40")
            check_true("空圈不触发蓝色判据", max_delta < 40,
                       f"实际 {max_delta}")

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
