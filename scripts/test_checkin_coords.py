"""核对签到浮层的点击坐标 —— 拿真实截图量，不靠记忆。

## 为什么必须量

`scripts/checkin.py` 里写死了两个坐标：

* 「立即签到」按钮 `CHECKIN_TAP = (360, 764)`
* 右上角 X `CLOSE_TAP = (521, 613)`

坐标写错的后果很严重：点在浮层外面，浮层既没签掉也没关掉，**还会挡住底下
的真实按钮**（实测它盖住视频弹题的输入框和「提交」）。所以拿 `debug/snap/`
里所有含「每日签到」的真实截图，把按钮的**实际包围盒**量出来对一遍。

运行：`python scripts\test_checkin_coords.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import checkin  # noqa: E402

SNAP_DIR = Path(__file__).resolve().parent.parent / "debug" / "snap"

#: 主机 UI 截图不是模拟器画面，要跳过
_SKIP = ("ui_shot", "mainui", "win_", "left_", "exe_", "dbg", "overlay",
         "chan_", "circle_bug")

PASS = 0
FAIL = 0
SKIP = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {extra}")


def orange_button_bbox(img: np.ndarray):
    """找橙色按钮的包围盒（「立即签到」是橙底白字）。

    第一版判据太松（只要求 R-B>60 且偏亮），把首页那圈**橙色向日葵装饰**
    整个框进来了 —— 框 (16,501)-(283,1246)，宽 267 高 745，明显不是按钮。

    收紧成：**逐行数橙色像素**，找一段「行内橙色像素够多」的连续横带。
    按钮是宽扁的（实测宽约 220、高约 60），装饰是竖向大块。
    """
    a = img.astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mask = (r - b > 60) & (r > 180) & (g > 110) & (g < 210)
    mask[:450, :] = False          # 避开顶部装饰

    row_counts = mask.sum(axis=1)
    # 按钮那一行应当有 150 以上的橙色像素
    band = np.nonzero(row_counts >= 150)[0]
    if len(band) < 20:
        return None
    y0, y1 = int(band.min()), int(band.max())
    if not (40 <= y1 - y0 <= 120):     # 按钮高度量级
        return None
    sub = mask[y0:y1 + 1, :]
    cols = np.nonzero(sub.sum(axis=0) >= (y1 - y0) * 0.5)[0]
    if len(cols) < 80:
        return None
    x0, x1 = int(cols.min()), int(cols.max())
    if not (120 <= x1 - x0 <= 320):    # 按钮宽度量级
        return None
    return x0, y0, x1, y1


def measured_buttons() -> list[tuple]:
    """扫全部真机截图，量出橙色按钮。返回 (名字, x0, y0, x1, y1, cx, cy)。"""
    out = []
    for p in sorted(SNAP_DIR.glob("*.png")):
        if p.name.lower().startswith(_SKIP):
            continue
        try:
            img = np.asarray(Image.open(p).convert("RGB"))
        except Exception:
            continue
        if img.shape[0] < 1000:
            continue
        box = orange_button_bbox(img)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        out.append((p.name, x0, y0, x1, y1,
                    (x0 + x1) // 2, (y0 + y1) // 2))
    return out


def white_x_bbox(img: np.ndarray):
    """找浮层右上角那个白色 X 的包围盒。

    浮层头部是蓝色渐变，X 是白色描边。做法：在浮层上半部找**白色像素**，
    要求这一块的形状是「小方块」（宽高都在 20~60px），排除大标题文字。

    实测头部的雪花装饰也是白的，所以限定 x > 420（X 在浮层右上角）。
    """
    a = img.astype(int)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    white = (r > 200) & (g > 200) & (b > 200)
    white[:, :420] = False        # X 在右侧；左边是白色标题文字
    white[:520, :] = False        # 浮层头部的雪花装饰
    white[720:, :] = False        # 下面是白色内容区
    ys, xs = np.nonzero(white)
    if len(xs) < 30:
        return None
    x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
    if not (10 <= x1 - x0 <= 80 and 10 <= y1 - y0 <= 80):
        return None
    return x0, y0, x1, y1


def measured_x_marks() -> list[tuple]:
    """扫全部真机截图，量出白色 X。返回 (名字, x0, y0, x1, y1, cx, cy)。"""
    out = []
    for p in sorted(SNAP_DIR.glob("*.png")):
        if p.name.lower().startswith(_SKIP):
            continue
        try:
            img = np.asarray(Image.open(p).convert("RGB"))
        except Exception:
            continue
        if img.shape[0] < 1000:
            continue
        box = white_x_bbox(img)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        # 只认「同一张里也量到了橙色按钮」的（确认确实在签到浮层上）
        if orange_button_bbox(img) is None:
            continue
        out.append((p.name, x0, y0, x1, y1, (x0 + x1) // 2, (y0 + y1) // 2))
    return out
    """扫全部真机截图，量出橙色按钮。返回 (名字, x0, y0, x1, y1, cx, cy)。"""
    out = []
    for p in sorted(SNAP_DIR.glob("*.png")):
        if p.name.lower().startswith(_SKIP):
            continue
        try:
            img = np.asarray(Image.open(p).convert("RGB"))
        except Exception:
            continue
        if img.shape[0] < 1000:
            continue
        box = orange_button_bbox(img)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        out.append((p.name, x0, y0, x1, y1,
                    (x0 + x1) // 2, (y0 + y1) // 2))
    return out


def main() -> int:
    global SKIP
    print("=" * 68)
    print(" 签到浮层点击坐标核对")
    print("=" * 68)

    print("\n[1] 常量本身")
    check("CHECKIN_TAP 是二元组", isinstance(checkin.CHECKIN_TAP, tuple)
          and len(checkin.CHECKIN_TAP) == 2)
    check("CLOSE_TAP 是二元组", isinstance(checkin.CLOSE_TAP, tuple)
          and len(checkin.CLOSE_TAP) == 2)
    x, y = checkin.CHECKIN_TAP
    check("「立即签到」在画布中央偏下（x 200~520, y 650~900）",
          200 < x < 520 and 650 < y < 900, f"实际 {(x, y)}")
    cx, cy = checkin.CLOSE_TAP
    check("X 在浮层右上（x 440~600, y 550~680）",
          440 < cx < 600 and 550 < cy < 680, f"实际 {(cx, cy)}")
    check("X 在按钮上方（y 更小）", cy < y, f"X y={cy}, 按钮 y={y}")

    print("\n[2] 拿真实截图量「立即签到」按钮的位置")
    measured = measured_buttons()

    if not measured:
        SKIP += 1
        print("  SKIP  没在截图里找到橙色按钮，跳过坐标核对")
    else:
        print(f"  量到 {len(measured)} 张带橙色按钮的截图：")
        for name, x0, y0, x1, y1, mx, my in measured[:12]:
            print(f"    {name[:40]:42s} 框({x0},{y0})-({x1},{y1}) 中心({mx},{my})")

        tx, ty = checkin.CHECKIN_TAP
        miss = [m for m in measured
                if not (m[1] <= tx <= m[3] and m[2] <= ty <= m[4])]
        check(f"CHECKIN_TAP {(tx, ty)} 落在每一张的按钮框内"
              f"（{len(measured) - len(miss)}/{len(measured)}）",
              not miss,
              "未命中：" + "; ".join(f"{m[0][:30]} 框({m[1]},{m[2]})-({m[3]},{m[4]})"
                                     for m in miss))

        cxs = [m[5] for m in measured]
        cys = [m[6] for m in measured]
        spread_x = max(cxs) - min(cxs)
        spread_y = max(cys) - min(cys)
        check(f"各截图按钮中心一致（x 跨度 {spread_x}px <= 12）", spread_x <= 12)
        check(f"各截图按钮中心一致（y 跨度 {spread_y}px <= 12）", spread_y <= 12)
        if measured:
            print(f"  实测中心范围: x {min(cxs)}~{max(cxs)}, y {min(cys)}~{max(cys)}")

    print("\n[3] 拿真实截图量右上角 X 的位置")
    xs_marks = measured_x_marks()
    if not xs_marks:
        SKIP += 1
        print("  SKIP  没量到白色 X")
    else:
        print(f"  量到 {len(xs_marks)} 张带 X 的浮层截图：")
        for name, x0, y0, x1, y1, mx, my in xs_marks[:10]:
            print(f"    {name[:40]:42s} 框({x0},{y0})-({x1},{y1}) 中心({mx},{my})")
        cx, cy = checkin.CLOSE_TAP
        miss = [m for m in xs_marks
                if not (m[1] - 12 <= cx <= m[3] + 12
                        and m[2] - 12 <= cy <= m[4] + 12)]
        check(f"CLOSE_TAP {(cx, cy)} 落在 X 附近（{len(xs_marks)-len(miss)}/{len(xs_marks)}）",
              not miss,
              "未命中：" + "; ".join(f"{m[0][:30]} 框({m[1]},{m[2]})-({m[3]},{m[4]})"
                                     for m in miss))
        mcx = [m[5] for m in xs_marks]
        mcy = [m[6] for m in xs_marks]
        print(f"  实测 X 中心范围: x {min(mcx)}~{max(mcx)}, y {min(mcy)}~{max(mcy)}")
        check("X 位置稳定（x 跨度 <= 12）", max(mcx) - min(mcx) <= 12)
        check("X 位置稳定（y 跨度 <= 12）", max(mcy) - min(mcy) <= 12)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败" + (f" / {SKIP} 跳过" if SKIP else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
