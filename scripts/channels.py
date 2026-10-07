"""截图数组的通道顺序处理。

## 结论（实测两次，最终确定）

`controller.post_screencap().wait().get()` 返回的是 **BGR**，不是 RGB。

证据（同一份数组，一次抓屏）：

* 把原始数组直接当 RGB 存成 PNG → 蓝色圆圈显示成**橙色**（红蓝互换）
* 交换首尾通道后存 → 圆圈是正常的蓝色，像素 `[70, 160, 250]`
* 把原始数组喂给 `circles.circle_shape` → 5 节课**全部**判成「没看过」
* 交换后再喂 → 5 节课**全部判对**（1 个半圈 + 4 个整圈）

## 为什么容易搞错

MaaFramework 的 OCR 不关心颜色，所以绝大多数调用点直接把原始数组丢给
OCR，看不出问题。只有**靠颜色判定**的逻辑（`circles` 的蓝色判据）才会
踩到。我在这上面翻过两次：

1. 先用一个「找最蓝像素」的探测脚本，那次抓屏时画面上叠着视频播放器，
   采样区落到了别处，得出「是 RGB」的错误结论；
2. 据此把 `course.py` 里的转换删掉，结果判定全错。

教训：**判通道顺序要在同一份数组上同时看像素值和判定结果**，
分两次抓屏对比会被页面变化骗到。`scripts/check_circle_now.py` 就是
按这个原则写的。

## 怎么用

```python
from channels import to_rgb
img = to_rgb(controller.post_screencap().wait().get())
state = circles.state_at(img, row_y)      # 现在颜色是对的
```
"""

from __future__ import annotations

import numpy as np


def to_rgb(image: np.ndarray) -> np.ndarray:
    """把抓屏返回的 BGR 数组转成 RGB。

    只处理 3 通道及以上的图像；灰度/异常形状原样返回，不抛异常
    （截图异常不该让整条流程崩）。
    """
    if image is None:
        return image
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return arr
    return np.ascontiguousarray(arr[:, :, 2::-1])


def is_bgr(image: np.ndarray) -> bool:
    """启发式判断抓屏数组是不是 BGR。

    做法：看整幅图里「最饱和的彩色像素」落在哪个通道。
    界面主色是蓝色（圆圈、顶部条），所以蓝色分量最大的像素应当显著多于
    红色分量最大的像素。这个方法只在**调试脚本**里用来自检，
    正常运行不要依赖它 —— 直接 `to_rgb()` 就行。
    """
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return False
    a = arr.reshape(-1, arr.shape[2])[:, :3].astype(int)
    r_win = int((a[:, 0] - a[:, 2] > 60).sum())
    b_win = int((a[:, 2] - a[:, 0] > 60).sum())
    # 若「ch0 明显大」的像素远多于「ch2 明显大」，说明 ch0 装的是蓝色
    # → 数组是 BGR。
    return r_win > b_win * 2
