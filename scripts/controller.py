"""MaaFramework 控制器封装。

负责：读配置 → 自动探测 adb 与模拟器端点 → 建立 AdbController → 连接。

**不硬编码任何安装路径**：不同用户的 MuMu 可能装在任意盘符/目录，
引擎版本目录（`nx_device\\12.0` / `15.0`）也会随平台升级变化，
端口在多开时还会递增（16384/16385/...）。这些都靠 `detect.py` 探测：

* adb 路径 —— 配置 → 运行实例版本匹配的引擎自带 adb → Android SDK →
  PATH → 注册表 → 进程路径 → 目录扫描；
* 端点地址 —— 配置 → `MuMuManager info -v all` 的 adb_port（权威，支持多开）
  → 配置里的 fallback → `adb devices` 里像模拟器的条目。

每一步都**实际验证**（跑得起来 / 连得上 / 是真安卓），验证优先于猜测。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from maa.controller import AdbController, Controller

sys.path.insert(0, str(Path(__file__).resolve().parent))
import detect  # noqa: E402
import paths  # noqa: E402


class ConfigError(RuntimeError):
    """配置缺失或指向不存在的文件。"""


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else paths.config_path()
    if not cfg_path.is_file():
        raise ConfigError(
            f"配置文件不存在: {cfg_path}\n"
            f"（首次运行应自动从 {paths.default_config_path()} 复制一份）"
        )
    with cfg_path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


# 兼容旧调用点：这两个名字以前在本模块，现在实现搬到 detect.py
find_adb = detect.find_adb
find_adb_candidates = detect.find_adb_candidates
_candidate_adb_paths = detect.find_adb_candidates


# --------------------------------------------------------------------------
# 控制器
# --------------------------------------------------------------------------

def build_controller(cfg: dict[str, Any] | None = None, log=print) -> Controller:
    """构造并连接 AdbController。连接失败直接抛错，不返回半死状态。"""
    cfg = cfg or load_config()
    adb_cfg = cfg.get("adb", {})

    # --- adb 路径：自动探测 ---
    try:
        adb_path = detect.find_adb(adb_cfg.get("adb_path"), log=log)
    except FileNotFoundError as exc:
        raise ConfigError(str(exc)) from exc

    # --- 端点地址：自动探测 + 验证 ---
    try:
        address = detect.find_device(
            adb_path,
            explicit=(adb_cfg.get("address") or "").strip() or None,
            fallbacks=list(adb_cfg.get("address_fallbacks") or []),
            log=log,
        )
    except ConnectionError as exc:
        raise RuntimeError(str(exc)) from exc

    log(f"[controller] adb     : {adb_path}")
    log(f"[controller] address : {address}")

    agent_dir = paths.agent_binary_dir()
    controller = AdbController(
        adb_path=str(adb_path),
        address=address,
        agent_path=str(agent_dir) if agent_dir.is_dir() else None,
    )

    if not controller.post_connection().wait().succeeded:
        raise RuntimeError(
            f"连接模拟器失败 (address={address})。\n"
            "排查步骤：\n"
            "  1) 模拟器是否已启动并进入桌面；\n"
            "  2) 端口是否变化——MuMu 多开时端口会递增，可在其安装目录运行：\n"
            "     MuMuManager.exe info -v all    （看每个实例的 adb_port）\n"
            "  3) 跑 `python scripts/detect.py` 看探测过程哪一步失败。"
        )

    log(f"[controller] connected: {controller.connected}")
    log(f"[controller] uuid     : {controller.uuid}")

    long_side = cfg.get("screenshot", {}).get("target_long_side")
    if long_side:
        ok = controller.set_screenshot_target_long_side(int(long_side))
        log(f"[controller] 截图长边归一为 {long_side}: {ok}")

    return controller


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    print(paths.describe())
    print()
    try:
        build_controller()
    except (ConfigError, RuntimeError) as exc:
        print(f"\n[FATAL] {exc}", file=sys.stderr)
        return 1
    print("\n[OK] 控制器连接正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
