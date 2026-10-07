"""通过 MaaFramework 截取模拟器当前画面并保存。

用途：
  1. 验证「能拿到画面」——这是整个自动化成立的前提；
  2. 为 pipeline 模板匹配切素材：你登录后跑这个脚本，我据截图写识别节点。

用法:
    python scripts/snap.py                 # 存到 debug/snap/<时间戳>.png
    python scripts/snap.py --tag home      # 存到 debug/snap/<时间戳>_home.png
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402
from controller import ConfigError, build_controller, load_config  # noqa: E402

ROOT = paths.app_root()
SNAP_DIR = paths.snap_dir()


def save_png(image: np.ndarray, out_path: Path) -> None:
    """存 PNG。实现见 imageio_util（cv2 → PIL 降级链）。"""
    from imageio_util import save_png as _save

    _save(image, out_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", default="", help="文件名后缀标记，例如 home / course")
    args = parser.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    job = controller.post_screencap().wait()
    if not job.succeeded:
        print(f"[FATAL] 截图失败, status={job.status}", file=sys.stderr)
        return 1

    image = job.get()
    if image is None or image.size == 0:
        print("[FATAL] 截图返回空图像", file=sys.stderr)
        return 1

    h, w = image.shape[:2]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"_{args.tag}" if args.tag else ""
    out_path = SNAP_DIR / f"{stamp}{suffix}.png"
    save_png(image, out_path)

    print(f"[OK] 截图尺寸 shape={image.shape} (高 {h} x 宽 {w}), dtype={image.dtype}")
    print(f"[OK] 已保存: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
