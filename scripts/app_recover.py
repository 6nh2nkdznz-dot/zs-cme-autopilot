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

import re
import struct
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


#: 崩在哪个线程 —— 2026-10-07 当天 17 次崩溃**全是这一个线程**。
#: 用它做指纹：命中就说明是「微信自己的视频解码器崩在模拟器上」，
#: 跟我们的点击/返回键**没有关系**。
CRASH_THREAD = "MediaCodec_loop"
#: 崩溃日志里的库指纹（模拟器的 libstagefright 在这一层挂掉）
CRASH_LIB = "libstagefright.so"

_CRASH_RE = re.compile(
    r"(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+\s+\d+\s+\d+\s+F libc\s+:\s+"
    r"Fatal signal (\d+) \(([A-Z_]+)\).*?in tid \d+ \(([^)]+)\), "
    r"pid \d+ \(([^)]+)\)")


def crashes(limit: int = 30) -> list[dict]:
    """最近的 `com.tencent.mm` 致命崩溃（从 logcat 的 crash 缓冲区读）。

    ## 为什么要专门查这个

    用户问过两次「微信为什么会自己退出」。查日志得到的答案是：
    **不是我们把它关掉的** —— `scripts/` 和管线里没有任何 `force-stop` /
    停进程的代码，唯一能「顶出微信」的是返回键（已有守卫）。

    真正的原因是**微信自己的原生视频解码线程崩了**：

        10-07 12:33:12.468  2207  4534 F libc : Fatal signal 11 (SIGSEGV),
        code 2 (SEGV_ACCERR) ... in tid 4534 (MediaCodec_loop),
        pid 2207 (com.tencent.mm)
        backtrace: #01 android::MediaCodec::setState
                   /system/lib64/libstagefright.so

    2026-10-07 一整天记到 **17 次**，全都是 `MediaCodec_loop` 线程、
    全都挂在 `libstagefright.so`（模拟器的 x86_64 媒体栈），
    崩溃地址都是 `0x73fbeb2d7156` 附近 —— 同一个 bug 反复触发。
    而**其它进程一次都没崩过**。

    触发场景也对得上：我们让微信 WebView 里的**视频课**一路播下去，
    平台那种视频播放最容易踩到模拟器解码器的坑。所以「微信自己退出」
    不是我们的点击错了，是**微信 + 模拟器解码器**的问题 —— 知道了这点，
    处理办法就是「崩了就重新拉起来、从没看完的那节接着看」，
    而不是去怀疑点击坐标。

    返回 `[{when, signal, thread, pkg, line}]`，按时间从早到晚。
    """
    adb = _adb_path()
    if not adb:
        return []
    ok, out = _run(adb, _adb_serial(),
                   ["logcat", "-d", "-b", "crash"], timeout=45.0)
    if not ok or not out:
        return []

    found: list[dict] = []
    for raw in out.splitlines():
        m = _CRASH_RE.search(raw)
        if not m:
            continue
        _mo, _d, hh, mm, ss, sig, name, thread, pkg = m.groups()
        if pkg != WECHAT_PKG:
            continue
        found.append({
            "when": f"{hh}:{mm}:{ss}",
            "signal": f"{sig} ({name})",
            "thread": thread,
            "pkg": pkg,
            "line": raw.strip(),
        })
    return found[-limit:]


def explain_crash(log: Callable[[str], None] = print) -> bool:
    """微信不在了 —— 顺便告诉用户「是不是它自己崩的」。返回是否确认崩溃。

    只在真的查到 `libc` 致命信号时才下结论；查不到就说「查不出来」，
    **绝不猜**（以前没日志的时候就是靠猜，白折腾了很多轮）。
    """
    recs = crashes()
    if not recs:
        log("[app] 日志里没查到微信自己的崩溃记录"
            "（也可能是 logcat 缓冲区已经被冲掉了）")
        return False

    fn = recs[-1]
    same = [r for r in recs if r["thread"] == CRASH_THREAD]
    log(f"[app] ⚠ 查到微信自己的崩溃记录 {len(recs)} 条，最后一条 "
        f"{fn['when']} {fn['signal']}")
    if same:
        log(f"[app]    其中 {len(same)} 条都崩在 `{CRASH_THREAD}` 线程 "
            f"（{CRASH_LIB} 的原生解码器）——")
        log("[app]    **这是微信/模拟器的视频解码器崩了，不是我们把它关掉的**：")
        log("[app]    程序里没有任何关微信的代码，唯一的返回键已经加了"
            "「不在平台页面就不按」的守卫。")
        log("[app]    崩溃地址整天都是同一个 → 同一个 bug 反复触发；"
            "别的进程一次都没崩过。")
        log("[app]    处理办法只有「重新拉起微信、从没看完的那节接着看」。")
    else:
        log(f"[app]    但没崩在 `{CRASH_THREAD}` 线程上，超出已知模式，"
            f"把上面那条发给开发者看")
    return True


#: 一轮运行里遇到的微信崩溃次数（进程内计数，重启程序归零）。
#:
#: 为什么要它：崩溃一次不可怕（重新拉起、从没看完的那节接着看就行），
#: 可怕的是**反复崩** —— 那就说明这台模拟器上根本播不动，继续硬跑只是
#: 一遍遍白等。所以上层拿这个数来判断「要不要放弃并告诉用户换个环境」。
_CRASH_ROUNDS = 0


def crash_rounds() -> int:
    """本轮运行里遇到的微信崩溃次数。"""
    return _CRASH_ROUNDS


def reset_crash_rounds() -> None:
    """把崩溃计数清零（每轮恢复开始时调）。"""
    global _CRASH_ROUNDS
    _CRASH_ROUNDS = 0


def crash_recovery(
    *,
    log: Callable[[str], None] = print,
    settle: float = 18.0,
) -> bool:
    """崩了/卡了就恢复微信，并且**顺便把崩溃原因读出来**。返回最终是否可用。

    这是给「跑到一半突然发现微信没了」用的：比直接调 `ensure_usable`
    多做一件事 —— **先解释原因**。

    ## 为什么要把「解释」和「恢复」绑在一起

    用户实测报的是「微信会一直自己退出」，而程序原来的表现是：
    微信没了 → 所有识别失败 → 看护空转，日志里**一句原因都没有**。
    于是只能靠猜（我为此白折腾了好几轮：怀疑过返回键、坐标、输入通道）。

    实际上 `adb logcat` 里写得明明白白（2026-10-07 一天 17 次）：

        Fatal signal 11 (SIGSEGV) ... in tid 4534 (MediaCodec_loop),
        pid 2207 (com.tencent.mm)
        #01 /system/lib64/libstagefright.so (android::MediaCodec::setState)

    **是微信自己的原生视频解码器崩在模拟器的媒体栈上**，
    跟我们的点击/返回键都没有关系。

    所以这里固定做三件事，顺序不能反：

    1. 读崩溃记录（**在重启之前读**，免得新进程把缓冲区冲掉；
       其实 logcat 的 crash 缓冲区是全局的，但语义上「先取证再动手」更清楚）
    2. 记录一条「本轮崩过」的标记，让上层知道这轮被中断过
    3. 恢复微信（`ensure_usable`：进程没了就拉起，卡空白页也重开）
    """
    # 1) 先取证
    if explain_crash(log=log):
        global _CRASH_ROUNDS
        _CRASH_ROUNDS += 1
        log(f"[app] 本轮已经遇到 {_CRASH_ROUNDS} 次微信崩溃")

    # 2) 再恢复
    alive = app_alive(log=log)
    if alive is None:
        log("[app] 无法判断微信状态，保持现状")
        return True
    if alive is False:
        return ensure_alive(log=log, settle=settle)

    # 进程活着，但可能卡在空白中转页（界面没了、进程还在）
    return ensure_usable(log=log, settle=settle)


def canvas_portrait() -> bool | None:
    r"""设备屏幕现在是不是竖的。判断不了返回 `None`。

    ## 为什么必须直接问 adb，不能用框架的截图

    我们认图/点击用的「画布」是 720x1280。一旦设备自己转成横屏
    （1920x1080），画布坐标**全部错位** —— 实测点「去学习」那一坐标
    在横屏下落到了右边的系统键上，**把设备点回了桌面、微信 WebView
    会话就此丢掉**（只能人工重进）。

    而且框架**不会跟着转**：缩放只在第一张截图时算过一次
    （`ControllerAgent.cpp:1252 postproc_screenshot`），转向后不重算。

    所以这里直接 `adb exec-out screencap -p` 拿原始 PNG，**只读文件头**
    里的 IHDR 尺寸（第 16..23 字节是大端两个 uint32）—— 不解码整张图，
    也不经过框架，因此**不会被框架的缓存/缩放骗到**。

    ## 为什么要单独放一份（而不是用 core 里那个）

    看课走的入口是 `run_exam_watch.py`，它**根本不建 `AppCore`**，
    所以 `core.AppCore._canvas_portrait` 那条路在几小时的看课里
    **一次都不会被检查**。2026-10-07 那一轮就是这么翻车的：
    中途屏幕转横 → 课程目录一节课都认不出来 → 节点白跑，
    日志里只留一句「屏幕上没找到任何视频条目」。
    """
    adb = _adb_path()
    if not adb:
        return None
    try:
        proc = subprocess.run(
            [adb, "-s", _adb_serial(), "exec-out", "screencap", "-p"],
            capture_output=True, timeout=30.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    data = proc.stdout or b""
    # PNG 签名 + IHDR：8 字节签名，4 字节长度，4 字节类型，然后宽高各 4 字节
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    if width == height or not width or not height:
        return None
    return height > width


def ensure_portrait(
    *,
    log: Callable[[str], None] = print,
) -> bool | None:
    """确认屏幕是竖的；不是就**尽量转回来**。判断不了返回 `None`。

    转屏优先用 adb 设置（不需要 root）：

        settings put system accelerometer_rotation 0   # 关掉自动旋转
        settings put system user_rotation 0            # 锁成竖屏

    ## 为什么「关掉自动旋转」是第一步

    MuMu 的 `window_auto_rotate` 是 `true` —— 窗口会跟着 Android 转。
    平台页面里播视频/某些页面会请求横屏，于是**跑到一半自己就横了**。
    先关自动旋转，才不会刚转回来又被转走。

    ## 转不回来时必须让上层知道

    实测**有转不回来的情况**（`user_rotation 0` 都设了，
    `dumpsys input` 还报 `SurfaceOrientation: 1`）。返回 `False` 而不是
    硬着头皮继续 —— 横屏下继续点只会把页面点到别处去。
    """
    state = canvas_portrait()
    if state is None:
        log("[screen] 判断不了屏幕方向，按竖屏继续")
        return None
    if state:
        return True

    log("[screen] ⚠ 屏幕是横屏（设备自己转的）—— 所有坐标都会错位，"
        "先转回竖屏")
    adb = _adb_path()
    if not adb:
        log("[screen] 读不到 adb，转不了")
        return False
    serial = _adb_serial()
    for args in (
        ["shell", "settings", "put", "system", "accelerometer_rotation", "0"],
        ["shell", "settings", "put", "system", "user_rotation", "0"],
    ):
        try:
            subprocess.run([adb, "-s", serial, *args],
                           capture_output=True, timeout=20.0)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"[screen] 转屏命令失败（{exc}）")

    for i in range(6):
        time.sleep(2.0)
        if canvas_portrait() is True:
            log(f"[screen] ✓ 已转回竖屏（等了 {(i + 1) * 2}s）")
            return True
    log("[screen] ✗ 转不回来 —— 请手动把模拟器转成竖屏（或把它的窗口"
        "横竖比调回竖的），然后重开本程序。横屏下继续跑只会乱点。")
    return False


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
    """确保微信用得起来：进程活着**且**在前台、且没卡在空白页。

    卡空白页时直接重开微信 —— 实测这种状态下再按 BACK 或点页面都没用，
    因为承载网页的 Activity 已经没了。
    """
    if app_alive(log=log) is not True:
        return ensure_alive(log=log, settle=settle)

    if stuck_empty(log=log):
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

    # 进程活着、也不是空白页，但**可能整个不在前台** ——
    # 实测见过：微信被切到后台、前台是 `app.lawnchair`
    # （一轮里被这么坑过：进程活着、截图是桌面、于是所有页面判定失败，
    #  上层只能报「不在平台页面里」，完全看不出真因是「微信在后台」）。
    # 捞到前台不需要重启进程，代价只有一次 monkey。
    fg = foreground()
    if fg and WECHAT_PKG not in fg:
        log(f"[app] ⚠ 微信在后台（前台是 {fg.split('/')[0]}），把它切到前台")
        if not launch_app(log=log):
            return False
        time.sleep(6.0)
        fg2 = foreground()
        if fg2 and WECHAT_PKG in fg2:
            log("[app] ✓ 微信已回到前台")
        else:
            log(f"[app] ⚠ 切前台后前台仍是 {fg2 or '（读不到）'}")
    return True


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
    explain_crash(log=log)
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


def _quiet(_msg: str) -> None:
    """吞掉探测过程的噪音。

    探活/查前台之类的中间状态对用户没意义 —— 只有**查出问题**时
    才值得往日志里写一行。把噪音压掉，日志里留下的才是「发生了什么」。
    """


def describe_environment(
    *,
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    """把「崩溃时该知道的环境信息」记进日志。返回读到的字典。

    ## 为什么要主动记

    2026-10-07 那次查「微信为什么自己退出」花了好几轮，一半时间花在
    **反推环境**上：ABI 是不是 x86_64？渲染后端是 Vulkan 还是 DirectX？
    有没有 root？这些信息当时都不在日志里，只能一条条 adb 现查。

    而且这些恰恰是**决定性的**：崩溃就出在 x86_64 模拟器的媒体栈
    （`libstagefright.so`），换个渲染后端/真机可能就没了。
    所以开跑之前先记一行，以后看日志就知道当时是什么环境。

    读不到的项留空 —— 记日志不能因为某个命令失败就中断。
    """
    adb = _adb_path()
    info: dict[str, str] = {}
    if not adb:
        log("[env] 读不到 adb 路径，跳过环境记录")
        return info

    serial = _adb_serial()
    props = {
        "abi": "ro.product.cpu.abi",
        "sdk": "ro.build.version.sdk",
        "board": "ro.board.platform",
    }
    for label, prop in props.items():
        ok, out = _run(adb, serial, ["shell", "getprop", prop], timeout=15.0)
        info[label] = out.strip().splitlines()[0].strip() if (ok and out.strip()) else ""

    log("[env] 运行环境："
        f"abi={info.get('abi') or '?'} "
        f"sdk={info.get('sdk') or '?'} "
        f"board={info.get('board') or '?'}")
    if info.get("abi") == "x86_64":
        log("[env]    ⚠ 这是 x86_64 模拟器 —— 微信的原生视频解码器"
            "在这套媒体栈上不稳（2026-10-07 一天崩了 17 次，"
            "全在 libstagefright.so）。崩了程序会自动重来，"
            "但**真机不会有这个问题**。")
    return info


def crash_watchdog(
    *,
    baseline: int,
    log: Callable[[str], None] = print,
) -> int:
    """看护期间的一次健康检查：微信是不是崩了/被顶走了。返回新的崩溃条数。

    ## 为什么要「看护」

    最要命的一段是**整门课轮播**：那个节点本身要跑几小时
    （每节课真实播放时间，快进无效）。微信在这个过程里崩掉之后，
    程序的表现却是「一切正常」—— 节点还在跑，只是每次识别都失败，
    日志里一行原因都没有。用户看到的是「微信自己退出了，程序还在瞎跑」。

    所以每过一会儿主动查三件事：

    1. `crashes()` 里有没有新增（logcat 的 crash 缓冲区是**全局**的，
       不用重启进程就能读，代价是一条 adb 命令）
    2. 微信进程还在不在（`app_alive`）
    3. 是不是卡在空白中转页（`stuck_empty`）

    命中任何一条就**当场报出来**，不留到下一节课才发现。

    返回**新的崩溃条数**（调用方把它当作下次的 `baseline`）。
    只涨不跌：`crashes()` 读的是缓冲区里的**现存**记录，
    缓冲区被冲掉时条数会变少，这时候**不能**把 `baseline` 调小，
    否则会把「缓冲区冲掉了」误判成「又崩了一次」。
    """
    found = crashes(limit=200)
    now = len(found)
    if now > baseline:
        latest = found[-1]
        log(f"[app] ⚠ 看护期间微信新增 {now - baseline} 次崩溃，"
            f"最后一次 {latest['when']} {latest['signal']} "
            f"在 {latest['thread']} 线程里")
        for item in found[baseline:]:
            log(f"[app]     {item['when']} {item['signal']} {item['thread']}")
        return now

    alive = app_alive(log=_quiet)          # 探活的结果不必逐次刷给用户
    if alive is False:
        log("[app] ⚠ 看护期间微信进程没了"
            "（没查到崩溃记录 —— 可能 logcat 缓冲区被冲掉，或被系统回收了）")
    elif alive and stuck_empty(log=_quiet):
        log("[app] ⚠ 看护期间微信卡在空白中转页上（进程在、界面是空的）")
    return baseline
