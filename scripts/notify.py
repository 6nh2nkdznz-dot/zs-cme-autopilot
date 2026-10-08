# -*- coding: utf-8 -*-
r"""发 Windows 系统通知。

**为什么要单独一个模块**：这个程序跑起来是几小时起步的（一门课 90 分钟、
一轮刷时长十几小时），人不可能一直盯着窗口。任务跑完了得能"喊一声" ——
用户 m19597 要的就是这个（「到达时长后自动暂停并发送通知」「在所有任务
完成后发送系统通知」）。

**两条腿，永不抛异常**。通知发不出去是小事，把跑了几小时的任务带崩是大事：

1. `_toast()`：真·Windows 通知（进操作中心，程序退了也还在）。走
   PowerShell 调 WinRT。**PowerShell 脚本本身保持纯 ASCII**，标题和正文
   从环境变量 `DSH_NOTIFY_TITLE` / `DSH_NOTIFY_BODY` 读 —— 这样绕开了
   「Windows PowerShell 5.1 把无 BOM 的 .ps1 当 ANSI 读、中文全花」那个坑
   （见 README「从源码构建」里 build.ps1 那条同样的注意事项）。
2. `_msgbox()`：退路。**起一个独立的 PowerShell 进程**弹 `MessageBox`。
   为什么不在本进程里 `ctypes` 调 `MessageBoxW`：这条路径的调用方常常
   **马上就要退出**（`desktop_runner` 跑完就 return），进程一退窗口就跟着
   没了 —— 实测过，`MessageBoxW` 放 daemon 线程里也是这个结果，
   照样是"返回成功、屏幕上什么都没有"。独立进程的窗口归系统持有。

★ **发之前先查开关**（`toasts_enabled()`）：`HKCU\Software\Microsoft\Windows\
CurrentVersion\PushNotifications` 里的 `ToastEnabled` 是 0 时，Windows 会把
toast **静默丢掉** —— `Show()` 照样返回成功、PowerShell 退出码照样是 0，
屏幕上什么也没有。实测本机就是 `ToastEnabled = 0`（Win11 26100），
所以这里**不认返回值**，先看开关。开关关着就直接走弹窗，并在日志里说清楚
是"系统的通知被关了"，不是程序坏了。

两条都失败就只写日志。返回值只表示"有没有送出去"，调用方不该据此做任何事。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from typing import Callable

#: 通知中心里显示成谁发的。用一个**已注册**的 AppUserModelID 才弹得出来 ——
#: 随手编一个（比如 `MyApp`）在 Win10/11 上会被系统静默丢掉，什么也不显示。
#: 这里借 PowerShell 自己的，实测能弹。
_AUMID = (r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
          r"\WindowsPowerShell\v1.0\powershell.exe")

#: 调 PowerShell 的超时（秒）。正常 1~2 秒就回来了；卡住就当失败走退路。
_TOAST_TIMEOUT = 20.0

#: **纯 ASCII** —— 正文靠环境变量传，见模块 docstring。
_TOAST_PS1 = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]
$aumid = '""" + _AUMID + r"""'
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
    [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $tpl.GetElementsByTagName('text')
[void]$texts.Item(0).AppendChild($tpl.CreateTextNode($env:DSH_NOTIFY_TITLE))
[void]$texts.Item(1).AppendChild($tpl.CreateTextNode($env:DSH_NOTIFY_BODY))
$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
"""

#: 弹窗退路。同样**纯 ASCII**，正文走环境变量。
#:
#: ★ 为什么用 PowerShell 起一个**独立进程**，而不是在本进程里 `ctypes` 调
#: `MessageBoxW`：本进程在「所有任务跑完了」这条路径上**马上就要退出了**
#: （`desktop_runner` 跑完就 return，CLI 那边直接 exit）。不管把 MessageBoxW
#: 放在主线程还是 daemon 线程，进程一退窗口就跟着没了 —— 实测就是这个现象：
#: `send()` 返回 True，屏幕上什么都没有。
#: 独立进程的窗口由系统持有，我们退不退出都不影响它。
_MSGBOX_PS1 = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
[void][System.Windows.Forms.MessageBox]::Show(
    $env:DSH_NOTIFY_BODY, $env:DSH_NOTIFY_TITLE)
"""


def _quiet(msg: str) -> None:
    """默认的日志出口：不打印。调用方自己传 `log` 进来。"""
    del msg


def _powershell() -> str:
    """找一个能用的 PowerShell。Windows 自带的 `powershell.exe` 一定有。"""
    for name in ("powershell.exe", "pwsh.exe"):
        found = _which(name)
        if found:
            return found
    return ""


def _which(name: str) -> str:
    """在 PATH 里找可执行文件（不 import shutil，少一个依赖面）。"""
    exts = [""]
    if not name.lower().endswith(".exe"):
        exts = [".exe", ".cmd", ".bat", ""]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        for e in exts:
            p = os.path.join(d, name + e)
            if os.path.isfile(p):
                return p
    return ""


def toasts_enabled() -> bool | None:
    """Windows 的「通知」总开关开着吗？`None` = 查不出来（不当成"关着"）。

    只有 `ToastEnabled` 被**显式写成 0** 才算关。键不存在 = 用户没动过 = 默认开着
    —— 这种情况返回 `None`，让调用方照发不误（在别人机器上得能弹出来）。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\PushNotifications")
        try:
            value, _ = winreg.QueryValueEx(key, "ToastEnabled")
        except FileNotFoundError:
            return None
        finally:
            winreg.CloseKey(key)
        return bool(value)
    except OSError:
        return None


def _toast(title: str, body: str, *, log: Callable[[str], None]) -> bool:
    """真·系统通知。返回有没有成功送出去。"""
    ps = _powershell()
    if not ps:
        log("[notify] 没找到 powershell，跳过系统通知")
        return False

    path = ""
    try:
        # 写临时 .ps1 而不是 `-Command`：脚本里有多行和引号，`-Command` 的
        # 转义规则在 PowerShell 5.1 上很坑，写成文件省事且可复现。
        fd, path = tempfile.mkstemp(prefix="dsh_notify_", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="ascii", newline="\r\n") as fh:
            fh.write(_TOAST_PS1)

        env = dict(os.environ)
        env["DSH_NOTIFY_TITLE"] = title
        env["DSH_NOTIFY_BODY"] = body

        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        r = subprocess.run(
            [ps, "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", path],
            env=env, capture_output=True, timeout=_TOAST_TIMEOUT,
            creationflags=flags,
        )
        if r.returncode != 0:
            raw = (r.stderr or b"").decode("utf-8", "replace").strip()
            log(f"[notify] 系统通知没弹出来（PowerShell 退出码 {r.returncode}）"
                + (f"：{raw.splitlines()[-1]}" if raw else ""))
            return False
        return True
    except subprocess.TimeoutExpired:
        log(f"[notify] 系统通知超时了（{_TOAST_TIMEOUT:.0f} 秒）")
        return False
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不能带崩任务
        log(f"[notify] 系统通知出错（{type(exc).__name__}: {exc}）")
        return False
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _msgbox(title: str, body: str, *, log: Callable[[str], None]) -> bool:
    """退路：弹个消息框。**独立进程**，见 `_MSGBOX_PS1` 上面的说明。

    只看"进程起没起来"，不看人点没点 —— 那个窗口归系统管，可能一直挂到人回来。
    """
    ps = _powershell()
    if not ps:
        log("[notify] 没找到 powershell，弹窗也发不了")
        return False

    path = ""
    try:
        fd, path = tempfile.mkstemp(prefix="dsh_msgbox_", suffix=".ps1")
        with os.fdopen(fd, "w", encoding="ascii", newline="\r\n") as fh:
            fh.write(_MSGBOX_PS1)

        env = dict(os.environ)
        env["DSH_NOTIFY_TITLE"] = title
        env["DSH_NOTIFY_BODY"] = body

        flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        # ★ 用 Popen **不等待**：那个进程要活到人点确定为止，等它就把我们自己也
        # 挂住了。临时 .ps1 因此也不能在这儿删（见下）。
        subprocess.Popen(
            [ps, "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", path],
            env=env, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # 文件留给那个进程读，不删 —— 它在系统临时目录里，重启会清。
        # 硬要删的话得等对方读完，那就又变成"等"了，得不偿失。
        return True
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不能带崩任务
        log(f"[notify] 弹窗没起来（{type(exc).__name__}: {exc}）")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        return False


def send(title: str, body: str = "", *, log: Callable[[str], None] | None = None,
         msgbox_fallback: bool = True) -> bool:
    """发一条系统通知。**不抛异常**，返回有没有送出去。

    标题和正文都当纯文本处理（不认 HTML/XML 之类的标记），太长的话
    通知中心自己会截。

    ★ 整个函数体包在 try 里：调用它的是"跑了几小时刚跑完"那条路径，
    通知出任何意外都不能把那个结果带崩。里面每个分支自己也会写日志。
    """
    log = log or _quiet
    try:
        return _send(title, body, log=log, msgbox_fallback=msgbox_fallback)
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        log(f"[notify] 通知出错（{type(exc).__name__}: {exc}），不影响任务")
        return False


def _send(title: str, body: str, *, log: Callable[[str], None],
          msgbox_fallback: bool) -> bool:
    title = (title or "").strip() or "继续教育助手"
    body = (body or "").strip()

    if toasts_enabled() is False:
        # ★ 系统通知被关了。这时候 `Show()` 照样"成功"、PowerShell 退出码照样 0，
        # 但屏幕上什么都不会出现 —— 只看返回值会一直以为自己发成功了。
        # 直接走弹窗，并且把这句写进日志，免得用户以为程序坏了。
        log("[notify] 系统的「通知」总开关是关的（设置 › 系统 › 通知），"
            "横幅会被静默丢掉 → 改用弹窗通知")
        if msgbox_fallback and _msgbox(title, body, log=log):
            return True
        log(f"[notify] 通知没发出去（不影响任务）：{title}")
        return False

    if _toast(title, body, log=log):
        log(f"[notify] 已发系统通知：{title}")
        return True
    if msgbox_fallback and _msgbox(title, body, log=log):
        log(f"[notify] 改用弹窗通知：{title}")
        return True
    log(f"[notify] 通知没发出去（不影响任务）：{title}")
    return False


def finished(what: str, detail: str = "", *,
             log: Callable[[str], None] | None = None) -> bool:
    """「活儿干完了」的通知，统一一下长相。"""
    return send("继续教育助手 · 跑完了", f"{what}\n{detail}".strip(), log=log)


def main(argv: list[str] | None = None) -> int:
    """自测用。

    * `python scripts/notify.py` —— 发一条测试通知。
    * `python scripts/notify.py "标题" "正文"` —— 发指定的。
    * `python scripts/notify.py --status` —— **只查不发**：系统的通知开关
      到底开没开。排查"为什么没弹出来"先跑这个。
    """
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "--status":
        on = toasts_enabled()
        state = {True: "开着", False: "★ 关着（横幅会被丢掉）",
                 None: "查不出来（大概率是开着的，会照发）"}[on]
        print(f"系统「通知」总开关：{state}")
        return 0

    title = argv[0] if argv else "继续教育助手 · 测试通知"
    body = argv[1] if len(argv) > 1 else "看到这条就说明系统通知能弹出来。"
    ok = send(title, body, log=lambda m: print(m, flush=True))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
