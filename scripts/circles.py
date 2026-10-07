"""目录里「完成圆圈」的检测。

## 这是什么

课程目录每一节前面有个蓝色圆圈，是**平台自己给的完成标记**：

* **完整蓝圈** = 这一节已经看完
* **半蓝圈**   = 看了一部分（没看完）
* **空圈**     = 没看过

## 为什么必须靠它

原先 `course.py` 里的注释写的是「目录里**没有**已完成的视觉标记，
蓝色圆点只表示当前播放中」——**这个假设是错的**，是用户实测纠正的。

因为那个错误假设，程序只能自己维护一份 `course_progress.json` 来记
「哪节看过了」。而那份记录会与实际不符（比如中途被中断、或平台把进度
重置了），表现就是**去点已经看完的课**。

平台的圆圈才是权威判据，本地记录只该是缓存。

## 怎么判的（实测数据）

在 720x1280 的画布上，量同一行圆圈的蓝色像素：

    完整蓝圈: 293 / 301 / 298 个蓝像素，列范围 18..37（宽 20px）
    半蓝圈  : 154 / 221       个蓝像素，列范围 28..37（宽 10px）

半圈正好是整圈宽度的一半。所以用**蓝像素数量**阈值就能区分，
不需要模板匹配。

运行:
    python scripts\\test_circle.py
"""

from __future__ import annotations

import numpy as np

#: 圆圈所在水平范围（**画布坐标，720 宽**）。
#:
#: 实测必须留宽：圆圈本身在 x=18..37，但窄窗口会切掉抗锯齿边缘，
#: 蓝像素从 300 掉到 155 —— 正好落进「半圈」的区间，导致**整圈被判成半圈**
#: （踩过：3 个完整圈全判错）。取 10..70 能稳住。
CIRCLE_X0 = 10
CIRCLE_X1 = 70

#: 圆圈中心相对 OCR 标题 y 的偏移。
#:
#: OCR 的 `box[1]` 是**文字顶部**，而圆圈垂直居中，实测中心在 +12px。
#: 标定数据（完整圈蓝像素随 dy 变化）：
#:     dy=+0 → 207~238   dy=+8 → 298   dy=+12 → 298   dy=+24 → 215~242
#: 所以取 +12，上下各留 16px 余量。
CIRCLE_DY = 12
CIRCLE_HALF_H = 16

#: 蓝色判定：B 明显大于 R，且够亮。
#: 用差值而不是绝对色值，避免不同截图亮度/压缩差异导致误判。
BLUE_DELTA = 40
BLUE_MIN = 120

#: 蓝像素超过这个数就认为「完整圆圈」= 已看完。
#:
#: 实测（dy=+12，x=10..70）：
#:     完整圈 298 ~ 301
#:     半 圈  154 ~ 221
#: 取 **260** 正好落在两者中间：下半圈 39 个余量、上完整圈 38 个余量，
#: 两边对称且都够宽。截图亮度有波动也不会误判。
#:
#: （先前取 250 时离半圈上限只有 29，测试里的余量检查把它挑出来了。）
FULL_PIXELS = 260


def circle_pixels(image: np.ndarray, row_y: int) -> int:
    """数出 `row_y` 那一行左侧圆圈的蓝色像素个数。

    image: RGB 的 numpy 数组（H, W, 3），画布坐标 720x1280。
    row_y: 标题行的 y（用 OCR 给出的 `box[1]`，即文字**顶部**）。
    """
    if image is None or getattr(image, "size", 0) == 0:
        return 0
    h, w = image.shape[0], image.shape[1]
    cy = int(row_y) + CIRCLE_DY
    y0 = max(0, cy - CIRCLE_HALF_H)
    y1 = min(h, cy + CIRCLE_HALF_H)
    x0 = max(0, min(int(CIRCLE_X0), w - 1))
    x1 = max(x0 + 1, min(int(CIRCLE_X1), w))
    band = image[y0:y1, x0:x1]
    if band.size == 0:
        return 0
    r = band[:, :, 0].astype(int)
    b = band[:, :, 2].astype(int)
    blue = (b - r > BLUE_DELTA) & (b > BLUE_MIN)
    return int(blue.sum())


class LessonState:
    """一节课在目录里的状态。"""

    DONE = "done"          # 完整蓝圈 → 已看完
    PARTIAL = "partial"    # 半蓝圈   → 看了一部分
    NONE = "none"          # 空圈     → 没看过
    UNKNOWN = "unknown"    # 圆圈没读出来（OCR 行错位等）


def classify(pixels: int) -> str:
    """按蓝像素数给状态。"""
    if pixels >= FULL_PIXELS:
        return LessonState.DONE
    if pixels > 0:
        return LessonState.PARTIAL
    return LessonState.NONE


def state_at(image: np.ndarray, row_y: int) -> str:
    """直接给出 `row_y` 那一行圆圈的状态。"""
    return classify(circle_pixels(image, row_y))
