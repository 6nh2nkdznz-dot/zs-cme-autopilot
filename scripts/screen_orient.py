"""屏幕方向：检测是否竖屏，必要时锁定为竖屏。

## 为什么需要

整套 UI 坐标（`roi` / `target` / 点击位置）都是按 **720x1280 竖屏**量出来的。
一旦屏幕转了向：

* 截图变成 1280x720，归一化后坐标系整个变了
* 所有写死的坐标点击全部错位 —— 会点到完全无关的地方

实测现场的旋转状态：

    SurfaceOrientation: 0          竖屏
    Physical size: 1080x1920       物理竖屏
    accelerometer_rotation: 1      ⚠ 自动旋转**开着**
    user_rotation: 1               当前竖屏

**自动旋转开着**就是隐患：视频是横屏内容，某些播放器会请求横屏，
系统一转，后面的识别和点击就全乱了。

所以：开始看护前锁成竖屏，看完再恢复用户原来的设置。

## 怎么判断当前是不是竖屏

不能只看 `dumpsys input` 的 SurfaceOrientation（各版本含义有差异），
**直接看截图的长宽比**最可靠：竖屏时高 > 宽。

（MaaFramework 会把截图长边归一为 1280，所以竖屏是 720x1280、
横屏是 1280x720 —— 用 shape 一比就知道。）
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

#: 恢复原设置时用。只记一次，避免连锁覆盖。
_saved: dict[str, str] = {}


def _adb_and_serial():
    """复用 app_recover 里那套（已经处理过「config 里地址是空的」那个坑）。"""
    try:
        import app_recover as A

        return A._adb_path(), A._adb_serial()
    except Exception:  # noqa: BLE001
        return "", ""


def _shell(args: list[str], timeout: float = 20.0) -> tuple[bool, str]:
    adb, serial = _adb_and_serial()
    if not adb:
        return False, ""
    cmd = [adb] + (["-s", serial] if serial else []) + ["shell"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        return True, out
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def is_portrait(image=None, log: Callable[[str], None] = print) -> bool | None:
    """当前是否竖屏。返回 True/False/**None（判断不了）**。

    优先用传入的截图判断（免费，已经截了）；没传就自己截一张。

    用**长宽比**而不是 `dumpsys` 的 rotation 值：各 Android 版本对
    rotation 数字的含义不完全一致，而「高 > 宽」是不会有歧义的。
    """
    if image is None:
        try:
            from controller import build_controller, load_config

            ctrl = build_controller(load_config())
            job = ctrl.post_screencap().wait()
            if not job.succeeded:
                return None
            image = job.get()
        except Exception as exc:  # noqa: BLE001
            log(f"[screen] 取截图判断方向失败: {exc}")
            return None
    try:
        h, w = image.shape[0], image.shape[1]
    except (AttributeError, IndexError):
        return None
    if h == w:
        return None
    return h > w


def current_rotation(log: Callable[[str], None] = print) -> str:
    """`dumpsys input` 里的 SurfaceOrientation。拿不到返回空串。"""
    ok, out = _shell(["dumpsys", "input"])
    if not ok:
        return ""
    for line in out.splitlines():
        if "SurfaceOrientation" in line:
            return line.split(":")[-1].strip()
    return ""


def lock_portrait(log: Callable[[str], None] = print) -> bool:
    """锁成竖屏（关掉自动旋转 + 指定竖屏）。返回是否发出成功。

    ## 为什么先关 accelerometer_rotation

    只设 `user_rotation` 而留着自动旋转的话，系统一感知到姿态变化
    就会把 user_rotation 覆盖掉 —— 锁了等于没锁。
    """
    global _saved
    if not _saved:
        _ok, acc = _shell(["settings", "get", "system", "accelerometer_rotation"])
        _ok2, rot = _shell(["settings", "get", "system", "user_rotation"])
        _saved = {"accelerometer_rotation": acc or "1", "user_rotation": rot or "0"}

    a_ok, _ = _shell(["settings", "put", "system", "accelerometer_rotation", "0"])
    b_ok, _ = _shell(["settings", "put", "system", "user_rotation", "0"])
    if a_ok and b_ok:
        log("[screen] 已锁定竖屏（关闭自动旋转）")
        return True
    log("[screen] ⚠ 锁定竖屏失败（不影响已竖屏的情况）")
    return False


def unlock(log: Callable[[str], None] = print) -> None:
    """恢复用户原来的旋转设置。没锁过就什么都不做。"""
    if not _saved:
        return
    for k, v in _saved.items():
        _shell(["settings", "put", "system", k, v])
    log("[screen] 已恢复原来的屏幕旋转设置")
    _saved = {}


def ensure_portrait(
    *,
    image=None,
    log: Callable[[str], None] = print,
    settle: float = 1.5,
) -> bool:
    """确保竖屏。不是竖屏就锁回来再确认。返回最终是否竖屏。

    只做两件确定的事：关自动旋转 + 指定竖屏。**不去转动设备**——
    模拟器没有重力感应，"转回来"只能靠设置，靠猜姿态是没意义的。
    """
    state = is_portrait(image, log=log)
    if state is True:
        return True
    if state is None:
        log("[screen] 判断不出屏幕方向，跳过竖屏检查")
        return True

    log("[screen] ⚠ 屏幕不是竖屏 —— 所有坐标点击都会错位，正在锁回竖屏")
    if not lock_portrait(log=log):
        return False
    time.sleep(settle)

    for i in range(3):
        if is_portrait(log=log) is True:
            log("[screen] ✓ 已恢复竖屏")
            return True
        log(f"[screen] 还没转回来，再等 2s（第 {i + 1}/3 次）")
        time.sleep(2.0)
    log("[screen] ✗ 仍然是横屏，坐标点击会错位")
    return False
