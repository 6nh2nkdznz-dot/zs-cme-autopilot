"""设备显示设置的保存 / 还原 / 切换。

## 为什么要单独做这个

桌面版要横屏（`1280x720`），移动端要竖屏（`720x1280`，物理 `1080x1920`）。
两种模式**不能同时生效**，切过去就可能回不来 —— 而移动端那套坐标是
量了好几轮才跑通的（考核 90 分），不能因为一次实验就废掉。

所以把三种设置都固化成命令，随时可切、可还原。

## 实测的原值（改造前，务必保留）

    Physical size:    1080x1920
    Physical density: 280
    accelerometer_rotation = 1      （自动旋转开着）
    user_rotation = 0

`wm size reset` / `wm density reset` 会回到 Physical 值，
所以还原不需要记住 1080x1920 —— 但**记下来更保险**（万一 reset 行为不同）。

用法:
    python scripts\\display_mode.py              # 看当前状态
    python scripts\\display_mode.py mobile       # 切回移动端竖屏（默认形态）
    python scripts\\display_mode.py desktop      # 切到桌面端横屏
    python scripts\\display_mode.py reset        # 用 wm reset 恢复出厂值
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

#: 移动端：竖屏。这是**已经跑通的那套**（管线坐标全按 720x1280 画布量的）。
MOBILE = {"size": "1080x1920", "density": "280", "rotation": "0"}

#: 桌面端：横屏。桌面版布局约 1200px 宽，720 塞不下，所以转横屏。
#: density 调低会让 CSS 像素更多（相当于"视野更宽"），160 接近桌面 1x。
DESKTOP = {"size": "1280x720", "density": "160", "rotation": "1"}


def _adb() -> tuple[str, str]:
    import app_recover as A

    return A._adb_path(), A._adb_serial()


def shell(args: list[str], timeout: float = 25.0) -> tuple[bool, str]:
    adb, serial = _adb()
    if not adb:
        return False, ""
    cmd = [adb] + (["-s", serial] if serial else []) + ["shell"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return True, (p.stdout or b"").decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def status() -> dict[str, str]:
    """读当前显示设置。"""
    out: dict[str, str] = {}
    for key, args in (
        ("size", ["wm", "size"]),
        ("density", ["wm", "density"]),
        ("accelerometer_rotation", ["settings", "get", "system", "accelerometer_rotation"]),
        ("user_rotation", ["settings", "get", "system", "user_rotation"]),
    ):
        _ok, txt = shell(args)
        out[key] = txt
    return out


def show() -> None:
    st = status()
    print("当前显示设置:")
    for k, v in st.items():
        print(f"  {k:24} {v}")
    # 归一化后的画布尺寸（管线坐标用的是这个）
    try:
        from controller import build_controller, load_config

        ctrl = build_controller(load_config())
        job = ctrl.post_screencap().wait()
        if job.succeeded:
            img = job.get()
            h, w = img.shape[0], img.shape[1]
            print(f"  {'画布(归一化)':24} {w}x{h}"
                  f"   → {'竖屏' if h > w else '横屏'}")
    except Exception as exc:  # noqa: BLE001
        print(f"  （取画布尺寸失败: {exc}）")


def apply(mode: dict[str, str], name: str) -> bool:
    """应用一组显示设置。"""
    print(f"切换到 {name}: size={mode['size']} density={mode['density']} "
          f"rotation={mode['rotation']}")
    ok1, _ = shell(["wm", "size", mode["size"]])
    ok2, _ = shell(["wm", "density", mode["density"]])
    # 先关自动旋转再指定方向，否则方向会被姿态覆盖
    shell(["settings", "put", "system", "accelerometer_rotation", "0"])
    ok3, _ = shell(["settings", "put", "system", "user_rotation", mode["rotation"]])
    time.sleep(2.5)
    if ok1 and ok2 and ok3:
        print("  ✓ 已应用")
        return True
    print("  ⚠ 部分命令失败，用 status 确认实际值")
    return False


def reset() -> bool:
    """`wm reset` 回出厂值，并恢复自动旋转。"""
    print("恢复出厂显示设置")
    shell(["wm", "size", "reset"])
    shell(["wm", "density", "reset"])
    shell(["settings", "put", "system", "accelerometer_rotation", "1"])
    shell(["settings", "put", "system", "user_rotation", "0"])
    time.sleep(2.5)
    print("  ✓ 已 reset")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="设备显示设置切换")
    ap.add_argument("mode", nargs="?", default="status",
                    choices=["status", "mobile", "desktop", "reset"],
                    help="status=只看 / mobile=竖屏 / desktop=横屏 / reset=出厂")
    args = ap.parse_args()

    if args.mode == "status":
        show()
        return 0
    if args.mode == "mobile":
        apply(MOBILE, "移动端竖屏（已跑通的那套）")
    elif args.mode == "desktop":
        apply(DESKTOP, "桌面端横屏（实验）")
    elif args.mode == "reset":
        reset()
    print()
    show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
