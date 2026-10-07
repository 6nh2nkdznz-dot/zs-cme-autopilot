"""微信（宿主 App）闪退的检测与自动重启。

## 为什么需要

整个看护是**在微信里**跑的（平台是微信 WebView 里的网页）。微信一崩，
整个页面就没了 —— 程序会继续对着一片空白或桌面截图，OCR 读不到任何
页面标记，于是所有判定失败、看护空转。而一门课要跑几小时，中途闪退
概率不低。

原先完全没有处理，用户实测问到「有时候微信闪退了怎么办」。

## 怎么检测

用 `pidof com.tencent.mm`：

    活着  → 输出 PID（如 `40013`）
    退出  → 输出空

比读屏可靠：闪退后可能停在桌面、也可能停在微信自己的崩溃提示页，
靠 OCR 判断这两种情况都不稳。进程在不在是**确定的事实**。

（实测：微信会同时有 `com.tencent.mm`、`com.tencent.mm:push`、
`com.tencent.mm:appbrand0` 几个进程；`pidof` 返回的是主进程 PID。）

## 怎么恢复

1. 用 `monkey` 拉起微信（比 `am start` 更稳，不依赖 Activity 名）
2. 等它起完
3. 交给 `verify.recover_to_home` 回到「我的学习」
4. 继续看护 —— 已看完的课节由 `course_progress.json` 跳过

所以闪退的代价是**几十秒**，不是整门课重看。
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

#: 微信包名（实测）
WECHAT_PKG = "com.tencent.mm"

#: 拉起 App 用的命令。monkey 不依赖具体 Activity 名，比 am start 稳。
_LAUNCH = [
    "shell", "monkey", "-p", WECHAT_PKG,
    "-c", "android.intent.category.LAUNCHER", "1",
]
#: 查进程的命令
_PIDOF = ["shell", "pidof", WECHAT_PKG]


def _adb_serial(config_path=None) -> str:
    r"""取设备序列号。

    ## 为什么不能只读 config

    `config['adb']['address']` 在**自动探测模式**下是**空字符串** ——
    用户的 config 就是这样（靠 `detect.py` 自己找设备）。

    于是命令少了 `-s`，而本机 adb 同时看到两个设备
    （`127.0.0.1:16384` 和 `emulator-5554`），直接失败：

        adb.exe: more than one device/emulator

    这个坑在 `main.py` 的文本输入那里踩过一次，这里是第二次 ——
    **同一个「config 里是空的」陷阱**。

    正确做法：问 adb 自己 `devices`，从实际连接里挑：
      1. config 里写了就用它
      2. 只有一台就用那台
      3. 多台时优先 `127.0.0.1:*`（MuMu 的形态），否则用第一台
    """
    # 1) config 里写死了就用它
    try:
        from controller import load_config

        cfg = load_config(config_path) if config_path else load_config()
        addr = str((cfg.get("adb") or {}).get("address") or "").strip()
        if addr:
            return addr
    except Exception:  # noqa: BLE001 - 读配置失败不算错，继续自己找
        pass

    # 2) 问 adb 当前连着哪些设备
    adb = _adb_path()
    if not adb:
        return ""
    try:
        p = subprocess.run([adb, "devices"], capture_output=True, timeout=20)
        text = (p.stdout or b"").decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""

    serials: list[str] = []
    for line in text.splitlines()[1:]:          # 第一行是 "List of devices attached"
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    if not serials:
        return ""
    if len(serials) == 1:
        return serials[0]
    # 多台：优先 loopback（MuMu 的形态）
    for s in serials:
        if s.startswith("127.0.0.1:"):
            return s
    return serials[0]


def _adb_path() -> str:
    """取 adb 可执行文件路径。"""
    try:
        import detect

        return str(detect.find_adb())
    except Exception:  # noqa: BLE001
        return ""


def _run(adb: str, serial: str, args: list[str], timeout: float = 30.0):
    """跑一条 adb 子命令，返回 (是否成功, 输出)。"""
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace").strip()
        return p.returncode == 0, out
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def app_alive(log: Callable[[str], None] = print) -> bool | None:
    """微信进程还在吗？

    返回 True=在 / False=不在 / **None=查不出来**。

    `None` 很重要：查不出来时**不要**当成「崩了」去盲目重启 ——
    那会在每次 adb 抖动时都重启一遍微信，反而更糟。

    ## 只看输出，不看退出码

    踩过的坑：`pidof` 找到了进程、**输出里就是 PID**，但退出码不是 0，
    于是被判成「进程不存在」，白重启了一次微信。

    adb shell 里这些命令的退出码本来就不可靠（管道、busybox 差异、
    shell 包装都会影响）。所以这里**只按输出判断**：
    有内容 = 活着，空 = 不在。
    """
    adb = _adb_path()
    if not adb:
        log("[app] 拿不到 adb，无法判断微信是否存活")
        return None
    _ok, out = _run(adb, _adb_serial(), _PIDOF)
    return bool(out.strip())


def launch_app(log: Callable[[str], None] = print) -> bool:
    """拉起微信。返回是否发出了启动命令。"""
    adb = _adb_path()
    if not adb:
        log("[app] 拿不到 adb，无法启动微信")
        return False
    ok, out = _run(adb, _adb_serial(), _LAUNCH, timeout=40.0)
    if ok:
        log("[app] 已发出启动微信的命令")
    else:
        log(f"[app] 启动微信失败: {out[:120]}")
    return ok


#: 微信「空壳」Activity。
#:
#: 实测：反复按 BACK 会把微信 WebView 的宿主页面推光，最后停在这个
#: 空白中转页上 —— 进程还活着（`pidof` 有值），但界面什么都没有，
#: 看起来就像「微信自己退出了」。
#:
#: 用户实测反馈就是这个现象：「点击第一个去学习后自动退出了微信」。
EMPTY_ACTIVITY = "com.tencent.mm/com.tencent.mm.ui.EmptyActivity"


def foreground() -> str:
    """当前前台窗口（`mCurrentFocus` 那一行）。拿不到返回空串。"""
    adb = _adb_path()
    if not adb:
        return ""
    ok, out = _run(adb, _adb_serial(), ["shell", "dumpsys", "window"], timeout=25.0)
    if not ok or not out:
        return ""
    for line in out.splitlines():
        if "mCurrentFocus" in line:
            return line.split("mCurrentFocus=")[-1].strip()
    return ""


def stuck_empty(log: Callable[[str], None] = print) -> bool:
    """微信是不是卡在空白中转页上（进程在、界面是空的）。

    ## 为什么要单独判这个

    `app_alive()` 只看进程，而**卡在 EmptyActivity 时进程是活的**。
    如果不额外判这一层，程序会以为一切正常，然后对着一张空白页
    反复识别失败 —— 用户看到的就是「微信退出了，程序还在瞎跑」。
    """
    fg = foreground()
    if not fg:
        return False          # 查不出来就不判，别误触发重启
    return EMPTY_ACTIVITY in fg


def ensure_usable(
    *,
    log: Callable[[str], None] = print,
    settle: float = 18.0,
) -> bool:
    """确保微信用得起来：进程活着**且**没卡在空白页。返回最终是否可用。

    卡空白页时直接重开微信 —— 实测这种状态下再按 BACK 或点页面都没用，
    因为承载网页的 Activity 已经没了。
    """
    if app_alive(log=log) is not True:
        return ensure_alive(log=log, settle=settle)

    if not stuck_empty(log=log):
        return True

    log("[app] ⚠ 微信卡在空白中转页（页面已被推光），重新打开")
    if not launch_app(log=log):
        return False
    time.sleep(settle)
    for i in range(3):
        if not stuck_empty(log=log):
            log("[app] ✓ 微信界面已恢复")
            return True
        log(f"[app] 还是空白页，再等 10s（第 {i + 1}/3 次）")
        time.sleep(10)
    log("[app] ✗ 微信仍停在空白页")
    return False


def ensure_alive(
    *,
    log: Callable[[str], None] = print,
    settle: float = 18.0,
) -> bool:
    """确保微信活着；不在就拉起来并等它起完。返回最终是否活着。

    `settle` 是拉起后等待的秒数：微信冷启动要十几秒，
    等太短会在还没渲染出来时就去识别，又是白跑。
    """
    alive = app_alive(log=log)
    if alive is True:
        return True
    if alive is None:
        # 查不出来，不动它 —— 盲目重启比什么都不做更危险
        log("[app] 无法判断微信状态，保持现状")
        return True

    log("[app] ⚠ 微信已不在（可能闪退了），正在重新拉起…")
    if not launch_app(log=log):
        return False
    log(f"[app] 等待 {settle:.0f}s 让它起完…")
    time.sleep(settle)

    for i in range(3):
        if app_alive(log=log) is True:
            log("[app] ✓ 微信已恢复")
            return True
        log(f"[app] 还没起来，再等 10s（第 {i + 1}/3 次）")
        time.sleep(10)
    log("[app] ✗ 多次尝试后微信仍未恢复")
    return False
