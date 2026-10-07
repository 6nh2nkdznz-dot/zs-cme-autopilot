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

## 判定方法（第二版，第一版的 bug 见下）

**看形状，不看像素个数。**

蓝色区域在固定坐标系里的横跨范围：

    完整圈  x 28..47   宽 20   → 填充比例 20/20 = 1.00
    半 圈   x 38..47   宽 10   → 填充比例 10/20 = 0.50

所以「蓝色宽度 / 完整圈宽度」这个比例就能区分，与亮度无关。

### 第一版为什么错（用户报「圆圈检测识别错误」）

第一版数**蓝像素总个数**（>260 算完整圈）。两个致命问题：

1. **个数随截图亮度剧烈波动。** 阈值 `b-r>40 & b>120` 只捞得到圆心最
   饱和的部分，抗锯齿的浅蓝边缘全漏掉。同一节课在不同截图里实测到
   **109 / 215 / 298 / 301** 四种值 —— 109 和 215 都低于阈值 260，
   于是**明明看完的课被判成「没看过」，程序回头重看**。

2. **用固定偏移定位圆心。** 第一版假设「圆心 = OCR 文字顶部 + 12px」
   （`CIRCLE_DY = 12`），这是在**单张截图**上标定的。实测同一节课的偏移
   会随列表滚动从 **+12 漂到 −19**：早先截图里 5 行稳定在 +10~+12，
   滚动后的截图里却是 +0 / −5 / −10 / −13 / −19。采样窗口只有 ±16px，
   偏移 −19 时**圆圈整个落在窗口外**，只量到 109 个像素。

   现在改成在一个**显著偏大的窗口**（`SEARCH_UP=40` / `SEARCH_DOWN=26`，
   覆盖 −40~+26）里扫描，让蓝像素自己暴露圆圈的纵向位置，再以它为中心
   量形状，不再假设偏移。

运行:
    python scripts\\test_circle.py
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: 圆圈所在水平范围（**画布坐标，720 宽**）。
#:
#: 从 0 开始（而不是紧贴圆圈的 28）：官方参考图是 22x22 的紧裁图，圆在
#: x 1..21，窗口左边界一设成 10 就把左半圈切掉，满圈量出宽 12 → 误判
#: 半圈（踩过）。真实截图里圆在 x 28..47，从 0 开始也照样切不到它。
#: 右边取 70：留宽是为了吸纳抗锯齿边缘和轻微水平抖动。列表右边缘的蓝色
#: 横线也会被收进来，但 `_circle_span` 按「蓝色像素最多的连续段」切分，
#: 圆圈永远是最大那块，横线会被排除。
CIRCLE_X0 = 0
CIRCLE_X1 = 70

#: 纵向搜索范围，相对 OCR 标题 y（`box[1]`，即文字**顶部**）。
#:
#: 实测圆心偏移随滚动在 **−19 ~ +12** 之间变化，所以向下要留够
#: （SEARCH_DOWN=26 覆盖到 +26，比观测到的最大 +12 富余一倍），
#: 向上也要留够（SEARCH_UP=40，覆盖 −40，比观测到的最大 −19 富余一倍）。
SEARCH_UP = 40
SEARCH_DOWN = 26

#: 蓝色判定：B 明显大于 R，且够亮。
#: 用差值而不是绝对色值，避免不同截图亮度/压缩差异导致误判。
BLUE_DELTA = 40
BLUE_MIN = 120

#: 完整圆圈的宽度（px），用来算填充比例。实测 20，参考图 21。
FULL_WIDTH = 20

#: 判定阈值。
#:
#: `FILLED_RATIO`：填充比例 >= 它算「完整圈」。
#:   实测完整圈 1.00、半圈 0.50 → 取 0.75 两边各留 0.25，非常宽。
#: `MIN_HEIGHT`：蓝色区域至少这么高才算找到了圆圈（排除噪点）。
#: `MIN_WIDTH`：蓝色区域至少这么宽才算找到了圆圈。
FILLED_RATIO = 0.75
MIN_HEIGHT = 8
MIN_WIDTH = 5

#: 兼容旧名字。第一版用像素个数阈值，现已不用；
#: 保留常量只是为了让外部引用不至于 ImportError。
FULL_PIXELS = 260
CIRCLE_DY = 12
CIRCLE_HALF_H = 16


class LessonState:
    """一节课在目录里的状态。"""

    DONE = "done"          # 完整蓝圈 → 已看完
    PARTIAL = "partial"    # 半蓝圈   → 看了一部分
    NONE = "none"          # 空圈     → 没看过
    UNKNOWN = "unknown"    # 圆圈没读出来（OCR 行错位等）


@dataclass
class CircleShape:
    """一次圆圈检测的完整结果（诊断用，调试视图会显示这些数）。"""

    state: str
    pixels: int = 0        # 蓝色像素个数（仅参考，不参与判定）
    width: int = 0         # 蓝色区域宽度
    height: int = 0        # 蓝色区域高度
    left: int = 0          # 蓝色区域左边界（画布 x）
    top: int = 0           # 蓝色区域上边界（画布 y）
    ratio: float = 0.0     # width / FULL_WIDTH

    @property
    def found(self) -> bool:
        return self.state != LessonState.UNKNOWN


def _blue_mask(image: np.ndarray, row_y: int, x0: int, x1: int):
    """在 `row_y` 上下一个偏大的窗口里取蓝色掩码。"""
    h, w = image.shape[0], image.shape[1]
    cy = int(row_y)
    y0 = max(0, cy - SEARCH_UP)
    y1 = min(h, cy + SEARCH_DOWN)
    xa = max(0, min(int(x0), w - 1))
    xb = max(xa + 1, min(int(x1), w))
    if y1 <= y0:
        return None, 0, 0
    band = image[y0:y1, xa:xb]
    if band.size == 0:
        return None, 0, 0
    r = band[:, :, 0].astype(int)
    b = band[:, :, 2].astype(int)
    return ((b - r > BLUE_DELTA) & (b > BLUE_MIN)), y0, xa


def _circle_span(mask: np.ndarray, xa: int) -> tuple[int, int, int, int] | None:
    """从蓝色掩码里挑出**圆圈**那一块，返回 (left, right, top, bottom)。

    掩码里除了圆圈还可能有列表右边缘的蓝色横线。用「连续列」把水平方向
    切成若干段，取**像素最多**的那一段。

    为什么按像素数挑而不是按位置挑：官方参考图（`assets/resource/
    circle_ref/*.png`）是**紧贴圆圈裁剪**的 22x22 图，圆心可能贴着左边界
    甚至被切掉一部分。按「最靠左」挑会挑到被切断的碎片
    （实测 `已完成.png` 挑出宽 12 的碎片 → 误判半圈）。
    圆圈是画面里蓝色最密集的东西，按像素数挑对紧裁图和真实截图都成立。
    """
    if mask is None or mask.size == 0:
        return None
    col_has = mask.any(axis=0)
    if not col_has.any():
        return None

    # 按「连续列」分段
    segments: list[tuple[int, int]] = []
    start = None
    for i, v in enumerate(col_has):
        if v and start is None:
            start = i
        elif not v and start is not None:
            segments.append((start, i - 1))
            start = None
    if start is not None:
        segments.append((start, len(col_has) - 1))

    best = None
    best_px = -1
    for s, e in segments:
        sub = mask[:, s:e + 1]
        px = int(sub.sum())
        if px < MIN_WIDTH * MIN_HEIGHT:
            continue
        row_has = np.where(sub.any(axis=1))[0]
        if not len(row_has):
            continue
        top, bottom = int(row_has.min()), int(row_has.max())
        if bottom - top + 1 < MIN_HEIGHT:
            continue
        if px > best_px:
            best_px = px
            best = (s + xa, e + xa, top, bottom)
    return best


def circle_shape(image: np.ndarray, row_y: int,
                 x0: int = CIRCLE_X0, x1: int = CIRCLE_X1) -> CircleShape:
    """检测 `row_y` 那一行左侧圆圈，返回完整形状信息。

    image: RGB 的 numpy 数组（H, W, 3），画布坐标 720x1280。
    row_y: 标题行的 y（用 OCR 给出的 `box[1]`，即文字**顶部**）。
    """
    if image is None or getattr(image, "size", 0) == 0:
        return CircleShape(state=LessonState.UNKNOWN)

    mask, y0, xa = _blue_mask(image, row_y, x0, x1)
    if mask is None:
        return CircleShape(state=LessonState.UNKNOWN)

    span = _circle_span(mask, xa)
    if span is None:
        # 没找到圆圈：可能真的没看过（空圈），也可能是这一行不是课。
        # 空圈在实测里就是 0 个蓝像素，所以报 NONE 而不是 UNKNOWN。
        return CircleShape(state=LessonState.NONE)

    left, right, top, bottom = span
    width = right - left + 1
    height = bottom - top + 1
    pixels = int(mask[:, left - xa:right - xa + 1].sum())

    # 填充比例：蓝色区域宽度 / 完整圈宽度。
    #
    # **用宽度而不是像素个数** —— 个数随亮度波动 3 倍（实测 109~301），
    # 宽度只跟形状有关（满圈 20、半圈 10）。
    #
    # 注意不要试图「补偿被搜索窗裁切的情况」：搜索窗左边界在 x=10，
    # 而圈在 x 28..47（半圈 x 38..47），离边界有 18px 富余，真实截图里
    # 永远切不到。官方参考图（22x22 紧裁图）确实被图片边界裁了，但那是
    # 图片自身的裁剪，不是我们的窗口造成的 —— 按宽度算它照样是对的
    # （满圈 21、半圈 11，分界 15）。加补偿反而把半圈算成满圈（踩过）。
    ratio = width / float(FULL_WIDTH)
    ratio = max(0.0, min(1.0, ratio))
    state = LessonState.DONE if ratio >= FILLED_RATIO else LessonState.PARTIAL
    return CircleShape(
        state=state, pixels=pixels, width=width, height=height,
        left=left, top=top + y0, ratio=ratio,
    )


def circle_pixels(image: np.ndarray, row_y: int) -> int:
    """数出 `row_y` 那一行左侧圆圈的蓝色像素个数。

    **保留只是为了向后兼容和诊断显示，判定不要再用它** ——
    这个数随截图亮度波动极大（同一节课实测 109~301），
    第一版拿它和固定阈值比，把看完的课判成没看过。
    判定请用 `state_at` / `circle_shape`。
    """
    return circle_shape(image, row_y).pixels


def classify(pixels: int) -> str:
    """按蓝像素数给状态。

    **已废弃**：这个函数只看个数，第一版就是因为它出错。
    保留是为了让老的测试/调用不炸；新代码请用 `state_at` 或
    `circle_shape`。这里把阈值调低到 60，至少不会再出现
    「满圈 109 个像素被判成没看过」那种离谱结果。
    """
    if pixels <= 0:
        return LessonState.NONE
    if pixels >= 60:
        return LessonState.DONE
    return LessonState.PARTIAL


def state_at(image: np.ndarray, row_y: int) -> str:
    """直接给出 `row_y` 那一行圆圈的状态。"""
    return circle_shape(image, row_y).state
