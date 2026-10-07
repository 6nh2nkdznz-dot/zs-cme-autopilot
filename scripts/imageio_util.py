"""图片落盘工具。

为什么不用 cv2：`import cv2` 会让 PyInstaller 把整个 opencv-python 打进去
（实测 112MB），而 MaaFramework 自己已经链接了 opencv_world4_maa.dll，
我们再带一份纯属重复。我们只用它做一件事——存 PNG。

这里做降级链：cv2 → PIL → imageio/其它 → 明确报错。
源码环境下有 cv2 就用；打包时把 cv2 排除掉，自动落到下一级。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class ImageSaveError(RuntimeError):
    pass


def save_png(image: np.ndarray, out_path: Path | str) -> Path:
    """把 BGR 图像存成 PNG。返回实际写入的路径。"""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if image is None or image.size == 0:
        raise ImageSaveError(f"图像为空，无法保存: {out}")

    errors: list[str] = []

    # 1) cv2（源码环境常见，最快）
    try:
        import cv2  # type: ignore

        if cv2.imwrite(str(out), image):
            return out
        errors.append("cv2.imwrite 返回 False")
    except ImportError:
        errors.append("cv2 未安装")

    # 2) Pillow —— MaaFw 的依赖里通常有，打包后能直接命中
    try:
        from PIL import Image  # type: ignore

        # MaaFramework 给的是 BGR，PIL 要 RGB
        Image.fromarray(np.ascontiguousarray(image[:, :, ::-1])).save(out, format="PNG")
        return out
    except ImportError:
        errors.append("Pillow 未安装")
    except Exception as exc:
        errors.append(f"Pillow 保存失败: {exc}")

    raise ImageSaveError(
        f"无法保存图片到 {out}。尝试过：\n"
        + "\n".join(f"  - {e}" for e in errors)
        + "\n请安装其中一个: pip install Pillow"
    )


def save_jpg(image: np.ndarray, out_path: Path | str, quality: int = 90) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        from PIL import Image  # type: ignore

        Image.fromarray(np.ascontiguousarray(image[:, :, ::-1])).save(
            out, format="JPEG", quality=quality
        )
        return out
    except ImportError:
        pass

    try:
        import cv2  # type: ignore

        if cv2.imwrite(str(out), image, [int(cv2.IMWRITE_JPEG_QUALITY), quality]):
            return out
    except ImportError:
        pass

    raise ImageSaveError(f"无法保存 JPEG 到 {out}（cv2 与 Pillow 都不可用）")
