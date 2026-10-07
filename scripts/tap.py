"""交互小工具：点按 / 输入文本 / 滑动 / 按键，用于在模拟器里驱动 App。

为什么需要它：MaaFramework 的 Pipeline 适合跑「识别→点击」的固定流程，但
「在微信里输入一个网址」这种一次性交互用 CLI 更快。二者共用同一个
AdbController，坐标空间完全一致。

坐标说明：横屏应用（浏览器/视频）是 720x1280 的宽高比，
竖屏应用（微信）是 720x1280 的高宽比。脚本会打印当前截图尺寸提醒你。

用法:
    python scripts\tap.py click 229 51
    python scripts\tap.py text "https://example.com"
    python scripts\tap.py key enter
    python scripts\tap.py swipe 360 900 360 300
    python scripts\tap.py size
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from controller import ConfigError, build_controller, load_config  # noqa: E402

# Android keycodes（仅列出常用）
KEYS = {
    "enter": 66,
    "back": 4,
    "home": 3,
    "del": 67,
    "search": 84,
    "tab": 61,
    "space": 62,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_click = sub.add_parser("click", help="点击坐标")
    p_click.add_argument("x", type=int)
    p_click.add_argument("y", type=int)
    p_click.add_argument("--repeat", type=int, default=1)

    p_text = sub.add_parser("text", help="输入文本")
    p_text.add_argument("value")

    p_key = sub.add_parser("key", help="按键")
    p_key.add_argument("name", choices=sorted(KEYS))

    p_swipe = sub.add_parser("swipe", help="滑动")
    p_swipe.add_argument("x1", type=int)
    p_swipe.add_argument("y1", type=int)
    p_swipe.add_argument("x2", type=int)
    p_swipe.add_argument("y2", type=int)
    p_swipe.add_argument("--duration", type=int, default=400)

    sub.add_parser("size", help="打印当前截图尺寸")

    args = parser.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    if args.cmd == "size":
        job = controller.post_screencap().wait()
        if not job.succeeded:
            print("[FATAL] 截图失败", file=sys.stderr)
            return 1
        h, w = job.get().shape[:2]
        print(f"当前画布: 宽 {w} x 高 {h}")
        print("竖屏应用(微信): 高 > 宽   横屏应用(浏览器/视频): 宽 > 高")
        return 0

    if args.cmd == "click":
        for i in range(args.repeat):
            controller.post_click(args.x, args.y).wait()
            print(f"[tap] ({args.x}, {args.y})")
            if i + 1 < args.repeat:
                time.sleep(0.6)
        return 0

    if args.cmd == "text":
        # 先把光标放到目标输入框（调用方负责先 click），再输入
        controller.post_input_text(args.value).wait()
        print(f"[text] 已输入 {len(args.value)} 字符")
        return 0

    if args.cmd == "key":
        code = KEYS[args.name]
        controller.post_click_key(code).wait()
        print(f"[key] {args.name} (keycode={code})")
        return 0

    if args.cmd == "swipe":
        controller.post_swipe(args.x1, args.y1, args.x2, args.y2, args.duration).wait()
        print(f"[swipe] ({args.x1},{args.y1}) -> ({args.x2},{args.y2}) {args.duration}ms")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
