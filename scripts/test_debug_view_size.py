"""调试视图窗口尺寸的离线测试。

## 为什么要单独测这个

`debug_view.py` 的窗口尺寸我调了六七轮才对，全靠「起窗口 → 截图 → 肉眼看」，
非常慢。而且它踩的坑很隐蔽：屏幕矮一点，整窗就超出屏幕，**底部日志和画面
最下面那节的判定标注会被裁掉** —— 用户报「最后一个课程的完成度没被识别到」，
其实圆圈读得好好的，只是标注画在被裁掉的那一段里（实测 6 节全部识别正确）。

所以把尺寸计算抽成纯函数 `fit_scale(screen_w, work_h)`，用这一组用例
把「任何屏幕上都放得下」钉死，不用再开窗口。

运行：`python scripts\test_debug_view_size.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import debug_view  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


def window_size(scale: float) -> tuple[int, int]:
    """复刻 `_build` 里的整窗尺寸算法 + 窗口边框，用于断言「放得下」。

    `FRAME_W`/`FRAME_H` 是实测出来的：`geometry("595x655")` 实际
    `GetWindowRect` 得 611x694。不算进来的话，断言会偏乐观 ——
    这正是之前「测试说放得下、用户却看到被裁」的原因。
    """
    w = int(debug_view.CANVAS_W * scale)
    h = int(debug_view.CANVAS_H * scale)
    return (w + debug_view.RIGHT_W + 46 + debug_view.FRAME_W,
            h + debug_view.LOG_H + 40 + debug_view.FRAME_H)


def main() -> int:
    print("=== 1. 常见屏幕分辨率下整窗都放得下 ===")
    # 覆盖：1366x768 笔记本、1440x900、1920x1080、2560x1440、本机 1707x1067
    screens = [
        (1366, 728), (1440, 860), (1920, 1040),
        (2560, 1400), (1707, 1019), (1280, 680),
    ]
    for sw, wh in screens:
        sc = debug_view.fit_scale(sw, wh)
        ww, wwh = window_size(sc)
        fits = ww <= sw and wwh <= wh
        check(f"{sw}x{wh} -> scale {sc:.3f} 整窗 {ww}x{wwh}",
              fits, f"超出屏幕（窗 {ww}x{wwh} vs 屏 {sw}x{wh}）")

    print("\n=== 2. 缩放上下限 ===")
    big = debug_view.fit_scale(3840, 2160)
    check("4K 屏也不会超过 MAX_SCALE",
          big <= debug_view.MAX_SCALE + 1e-9, f"得到 {big:.3f}")
    tiny = debug_view.fit_scale(800, 400)
    check("极小屏不低于 0.25（再小就看不清了）",
          tiny >= 0.25 - 1e-9, f"得到 {tiny:.3f}")
    check("宽屏够宽时用 DEFAULT_SCALE 或更大",
          debug_view.fit_scale(1920, 1040) >= debug_view.DEFAULT_SCALE - 1e-9)

    print("\n=== 3. 高度是瓶颈时的行为 ===")
    # 屏幕很高但很窄 -> 应当被宽度限制
    sc_narrow = debug_view.fit_scale(900, 2000)
    ww, _ = window_size(sc_narrow)
    check("窄屏时整窗宽度仍不超屏", ww <= 900, f"窗宽 {ww} > 900")
    # 屏幕很宽但很矮 -> 应当被高度限制
    sc_short = debug_view.fit_scale(3000, 600)
    _, wwh = window_size(sc_short)
    check("矮屏时整窗高度仍不超屏", wwh <= 600, f"窗高 {wwh} > 600")

    print("\n=== 4. 回归：曾经出问题的那次配置 ===")
    # 旧实现写死 scale 0.34 + total_h = min(..., 935)，
    # 在 1019 的工作区上整窗 935+ 就贴边了；而当时窗口实际被撑到 930+，
    # 底部日志区被裁掉。现在同样的屏幕必须留出余量。
    sc = debug_view.fit_scale(1707, 1019)
    _, wwh = window_size(sc)
    check("本机 1707x1019 下高度有余量（不贴边）",
          wwh <= 1019 - 40, f"窗高 {wwh}，工作区 1019")
    check("本机下画面不小于 0.25（还能看清圆圈）", sc >= 0.25)

    print("\n=== 5. 常量自洽 ===")
    check("MAX_SCALE >= DEFAULT_SCALE",
          debug_view.MAX_SCALE >= debug_view.DEFAULT_SCALE)
    check("LOG_H 够放日志（>=150）", debug_view.LOG_H >= 150,
          f"LOG_H={debug_view.LOG_H}")
    check("RIGHT_W 够放明细栏（>=280）", debug_view.RIGHT_W >= 280,
          f"RIGHT_W={debug_view.RIGHT_W}")

    print("\n=== 6. 内嵌模式的缩放（embed_scale）===")
    # 内嵌只有**高度**这一个约束，上限该比独立窗口宽（EMBED_MAX_SCALE >
    # MAX_SCALE），否则画面只占右栏上半截、下面一大块空着（实测过：
    # canvas 665px 高，可用 1242px）。
    check("EMBED_MAX_SCALE > MAX_SCALE（内嵌允许更大）",
          debug_view.EMBED_MAX_SCALE > debug_view.MAX_SCALE,
          f"{debug_view.EMBED_MAX_SCALE} vs {debug_view.MAX_SCALE}")
    # 主界面右栏的真实尺寸（1515 逻辑宽窗口）：可用约 1720x1242
    sc = debug_view.embed_scale(1720, 1242)
    check("主界面右栏尺寸下能顶到上限", abs(sc - debug_view.EMBED_MAX_SCALE) < 1e-9,
          f"得到 {sc:.3f}")
    check("主界面右栏下画面高 <= 可用高（不溢出）",
          debug_view.CANVAS_H * sc <= 1242,
          f"画面高 {debug_view.CANVAS_H * sc:.0f} > 1242")
    # 矮窗口时必须缩得下来
    sc_short = debug_view.embed_scale(900, 500)
    check("矮窗口下按高度缩（500 高 -> 约 0.39）",
          abs(sc_short - 500 / debug_view.CANVAS_H) < 1e-9,
          f"得到 {sc_short:.3f}")
    sc_tiny = debug_view.embed_scale(1, 1)
    check("极端小也不低于 0.12（不崩、不除零）",
          sc_tiny >= 0.12 - 1e-9, f"得到 {sc_tiny:.3f}")
    # 宽度成为瓶颈时也要缩
    sc_narrow = debug_view.embed_scale(200, 2000)
    check("宽度成为瓶颈时按宽度缩",
          abs(sc_narrow - 200 / debug_view.CANVAS_W) < 1e-9,
          f"得到 {sc_narrow:.3f}")

    print("\n=== 7. 内嵌面板的尺寸常量 ===")
    check("EMBED_DETAIL_W 够放明细文本（>=200）",
          debug_view.EMBED_DETAIL_W >= 200,
          f"EMBED_DETAIL_W={debug_view.EMBED_DETAIL_W}")
    check("EMBED_DETAIL_W 不喧宾夺主（<=340）",
          debug_view.EMBED_DETAIL_W <= 340,
          f"EMBED_DETAIL_W={debug_view.EMBED_DETAIL_W}")
    check("EMBED_TOOLBAR_H 够放一排按钮（>=32）",
          debug_view.EMBED_TOOLBAR_H >= 32,
          f"EMBED_TOOLBAR_H={debug_view.EMBED_TOOLBAR_H}")

    print("\n=== 8. 内嵌布局：画面在上、明细在下（用户要求「左半边日志，"
          "右半边视图」）===")
    # 关键回归：`embed_scale` 必须**真的扣掉**明细区占的高度。
    # 第一版忘了扣，`embed_scale(1720, 700, 320)` 返回的是 700/1280=0.547，
    # 画面 700 高 + 明细 320 = 1020 > 可用 700，画面底边被明细压掉一大截。
    #
    # 这两组参数是特意挑的，避开上限和下限：
    #   - 宽度给 1720（右栏不受宽度限制的真实尺寸），别让宽度成为瓶颈 ——
    #     给 540 的话宽度算出 0.75，本来就顶到上限 0.72，扣不扣高度都是 0.72；
    #   - 高度给 700：不扣是 700/1280=0.547，扣了是 380/1280=0.297，
    #     两个都在 [0.12, 0.72] 区间内，差值看得出来。
    sc_nodetail = debug_view.embed_scale(1720, 700, 0)
    sc_detail = debug_view.embed_scale(1720, 700, 320)
    check("传了 detail_h 之后缩放确实变小（真的扣了）",
          sc_detail < sc_nodetail,
          f"{sc_detail:.3f} vs {sc_nodetail:.3f}")
    check("画面高 = 可用高 - 明细高（不多占）",
          abs(debug_view.CANVAS_H * sc_detail - (700 - 320)) < 1.0,
          f"得到 {debug_view.CANVAS_H * sc_detail:.0f}，期望 380")

    # 端到端：按真实可用高走一遍 panel 的算式，画面 + 明细 + 工具栏必须
    # 装得进可用高。照抄 `DebugPanel._vertical_budget` 的 chrome 扣减。
    chrome = (debug_view.EMBED_TOOLBAR_H + 6
              + 2 * debug_view.EMBED_PAD + debug_view.EMBED_PAD)
    for avail_h in (1242, 900, 700, 500, 360):
        budget = avail_h - chrome
        dh = debug_view.embed_detail_height(budget)
        s = debug_view.embed_scale(540, budget, dh)
        total = debug_view.CANVAS_H * s + dh + chrome
        check(f"可用高 {avail_h} 时画面+明细+工具栏装得下",
              total <= avail_h + 1,
              f"合计 {total:.0f} > {avail_h}")

    check("明细区高有下限（矮窗口下不会缩没）",
          debug_view.embed_detail_height(50) >= debug_view.EMBED_DETAIL_MIN_H,
          f"得到 {debug_view.embed_detail_height(50)}")
    check("明细区高有上限（不把画面挤没）",
          debug_view.embed_detail_height(5000) <= debug_view.EMBED_DETAIL_MAX_H,
          f"得到 {debug_view.embed_detail_height(5000)}")

    # 源码断言：内嵌时明细**在下边**（`pack(side="top")`），独立窗口才在右边
    # （`pack(side="left")`）。这两行是布局本身，钉住它们避免以后又被改回去。
    src = (Path(debug_view.__file__).read_text(encoding="utf-8"))
    check("内嵌时明细区 pack 在下方（side=\"top\", fill=\"x\"）",
          'right.pack(side="top", fill="x"' in src,
          "没找到内嵌分支的 pack 调用")
    check("独立窗口的明细区仍在右侧（side=\"left\"）",
          'right.pack(side="left", anchor="n", padx=(pad, 0))' in src,
          "没找到独立窗口分支的 pack 调用")
    check("独立窗口不绑 <Configure>（否则缩放会被打回 0.12）",
          "if not self.standalone:" in src,
          "没找到只在内嵌时绑 <Configure> 的守卫")

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
