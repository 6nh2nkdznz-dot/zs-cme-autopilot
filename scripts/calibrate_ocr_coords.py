"""OCR 坐标标定：搞清 `post_recognition` 返回的 box 是什么坐标系。

## 为什么要做这个

写管线时发现一个诡异现象：用 `roi` 裁一小块图再 OCR，
报告的坐标会**超出裁剪区域**，而且看起来像是被放大了 2 倍：

```
roi=[100,900,400,200]  →  报告 x 最小值恰好 200 (=2×100)
roi=[0,900,720,200]    →  报告 y 集中在 1837..1941 (≈2×900 .. 2×970)
```

而整屏（roi=[0,0,720,1280]）时坐标是**对的**。

推断：MaaFramework 对输入图像做**分辨率归一化**——为了让识别算法
在统一的尺度上工作，会把图像缩放到某个目标尺寸。整屏 720x1280 恰好
已经在目标尺度上，所以不变；小图会被**放大**，于是坐标被乘了系数。

如果是这样，写管线时 `roi` 的语义就要按「框架会归一化」来理解，
而不是「我给的像素坐标」。

## 做法

把当前屏幕截图裁成若干已知尺寸的小块，对每块跑 OCR，
把「报告坐标」和「真实坐标」画在一起比对，算出缩放系数。

用法:
    python scripts\\calibrate_ocr_coords.py
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

import paths  # noqa: E402
from controller import ConfigError, build_controller, load_config  # noqa: E402


def main() -> int:
    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    resource.post_bundle(str(paths.resource_dir())).wait()
    resource.post_ocr_model(str(paths.ocr_model_dir())).wait()
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    def ocr_boxes(img) -> list[tuple[str, int, int, int, int]]:
        j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
        if not j.wait().succeeded:
            return []
        td = j.get()
        if td is None:
            return []
        out: list[tuple[str, int, int, int, int]] = []
        for nid in td.node_id_list:
            node = tasker.get_node_detail(nid)
            if node is None or node.recognition is None:
                continue
            for r in (node.recognition.all_results or []):
                b = getattr(r, "box", None)
                t = getattr(r, "text", None)
                if b and t:
                    out.append((str(t), int(b[0]), int(b[1]), int(b[2]), int(b[3])))
        return out

    shot = controller.post_screencap().wait().get()
    h, w = shot.shape[:2]
    print("=" * 70)
    print(" OCR 坐标标定")
    print("=" * 70)
    print(f"整屏画布: {w} x {h}")
    print()

    # --- 基准：整屏 ---
    print("[基准] 整屏 OCR（不做任何裁剪）")
    full = ocr_boxes(shot)
    print(f"  命中 {len(full)} 段")
    for t, x, y, bw, bh in full[:4]:
        print(f"    {t[:26]!r:30} box=({x},{y},{bw},{bh})")
    print()

    # --- 逐个尺寸的裁剪实验 ---
    # 裁同一块内容（页面中部），但用不同的裁剪尺寸，
    # 看报告坐标随裁剪尺寸怎么变。
    crop_specs = [
        ("720x1280 (整屏)", 0, 0, 720, 1280),
        ("720x640  (半屏高)", 0, 320, 720, 640),
        ("720x320", 0, 480, 720, 320),
        ("720x200", 0, 900, 720, 200),
        ("400x200", 100, 900, 400, 200),
        ("200x100", 100, 900, 200, 100),
    ]

    print("[裁剪实验] 同一块内容、不同裁剪尺寸，看报告坐标如何变化")
    print(f"  {'裁剪':<20} {'裁剪尺寸':<12} {'命中':<5} 首条报告坐标")
    print("  " + "-" * 66)

    for label, cx, cy, cw, ch in crop_specs:
        # 越界保护
        if cy + ch > h or cx + cw > w:
            print(f"  {label:<20} 越界，跳过")
            continue
        crop = shot[cy:cy + ch, cx:cx + cw]
        boxes = ocr_boxes(crop)
        if not boxes:
            print(f"  {label:<20} {cw}x{ch:<8} {'0':<5} (无文本)")
            continue
        t, x, y, bw, bh = boxes[0]
        print(f"  {label:<20} {cw}x{ch:<8} {len(boxes):<5} "
              f"({x},{y},{bw},{bh})  {t[:20]!r}")

    print()
    print("=" * 70)
    print(" 判读方法")
    print("=" * 70)
    print("  若裁剪后报告的坐标 ≈ 原图坐标 × (归一化目标尺寸 / 裁剪尺寸)，")
    print("  则说明框架对输入图做了分辨率归一化，roi 的像素语义不可直接当坐标用。")
    print("  若裁剪后坐标 == 裁剪偏移 + 局部坐标，则是普通 ROI 语义。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
