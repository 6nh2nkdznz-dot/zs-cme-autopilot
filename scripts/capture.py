"""批量截图辅助：按预设页面名依次截图，并同时跑一遍 OCR 留下文字记录。

用途：你登录平台后，只需按顺序翻页，我这边跑这个脚本就能一次性拿到
「截图 + 文字坐标」两份材料，直接用来写 Pipeline 节点。

用法:
    python scripts/capture.py home courses playing quiz checkin
    python scripts/capture.py --list
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from controller import ConfigError, build_controller, load_config  # noqa: E402
from snap import save_png  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "debug" / "capture"

# 我需要的页面清单。你翻到对应页面，脚本负责拍下来。
PAGES: dict[str, str] = {
    "home": "登录后的首页 —— 顶部导航、待办问卷提示",
    "courses": "「我的学习」列表页 —— 课程条目与进度",
    "playing": "视频播放页 —— 进度条区域、播放/暂停按钮",
    "popup": "播放中的验证弹窗 —— 最关键，等它自己弹出来再拍",
    "quiz": "课后答题页 —— 题干区与选项区",
    "checkin": "签到 / 学分页",
}


def list_pages() -> None:
    print("可用页面名：\n")
    for name, desc in PAGES.items():
        print(f"  {name:<10} {desc}")
    print("\n示例: python scripts\\capture.py home courses playing")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pages", nargs="*", help="要截取的页面名")
    parser.add_argument("--list", action="store_true", help="列出可用的页面名")
    parser.add_argument("--delay", type=float, default=6.0, help="每张之间等待秒数（留时间给你翻页）")
    args = parser.parse_args()

    if args.list or not args.pages:
        list_pages()
        return 0

    unknown = [p for p in args.pages if p not in PAGES]
    if unknown:
        print(f"[FATAL] 未知页面名: {unknown}", file=sys.stderr)
        list_pages()
        return 2

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session_dir = OUT_DIR / stamp
    session_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []

    for i, page in enumerate(args.pages):
        if i > 0:
            print(f"\n[wait] {args.delay:.0f}s —— 请翻到「{PAGES[page]}」")
            time.sleep(args.delay)

        job = controller.post_screencap().wait()
        if not job.succeeded:
            print(f"[warn] {page}: 截图失败，跳过")
            continue

        img = job.get()
        png_path = session_dir / f"{page}.png"
        save_png(img, png_path)
        h, w = img.shape[:2]
        print(f"[ok] {page:<10} {w}x{h} -> {png_path.name}")

        manifest.append({
            "page": page,
            "description": PAGES[page],
            "file": png_path.name,
            "width": int(w),
            "height": int(h),
        })

    (session_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n[完成] {len(manifest)} 张，目录: {session_dir}")
    print("[下一步] 我会对每张图跑 OCR 探针，读出坐标和真实文案，然后把 pipeline 写实。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
