# -*- coding: utf-8 -*-
"""系统通知（`notify.py`）的自测。

## 为什么需要这个模块

用户 m19597 要「到达时长后自动暂停并发送通知」「在所有任务完成后发送系统通知」。
通知本身不影响业务，但**它是最容易"看起来成功了其实没发出去"的一块**，
2026-10-08 实际踩了两个坑，两个坑都表现为「函数返回 True，屏幕上什么都没有」：

1. **系统的通知总开关是关的**（本机 `HKCU\\...\\PushNotifications\\ToastEnabled
   = 0`）。这种状态下 `ToastNotificationManager.Show()` 照样成功返回、
   PowerShell 退出码照样 0，Windows 只是把横幅**静默丢掉**。
   → 所以 `send()` 必须**先看开关**，不能认返回值。
2. **弹窗退路原本是个空响**：老写法在本进程的 daemon 线程里 `MessageBoxW`，
   而这条路径的调用方（`desktop_runner` 跑完）**紧接着就退出** ——
   进程一退窗口跟着没。→ 改成起独立 PowerShell 进程。

这两条都不是"看一眼代码就知道对不对"的东西，所以在这里钉住：

1. **两份 PowerShell 脚本必须是纯 ASCII** —— 中文标题走环境变量。
   Windows PowerShell 5.1 会把无 BOM 的 .ps1 当 ANSI 读（同 build.ps1 那个坑），
   脚本里带中文会直接语法错误。这条要是破了，通知会**全部**失败，
   而且是静默的（只写日志）。
2. **绝不能抛异常** —— 调用它的时候任务已经跑完几小时了，
   通知挂掉不能把结果带崩。
3. **开关关着时要走弹窗**，不能傻乎乎地发一个会被丢掉的横幅。

运行:
    python scripts\\test_notify.py
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import notify  # noqa: E402

_passed = 0
_failed = 0

SRC = (Path(__file__).resolve().parent / "notify.py").read_text(encoding="utf-8")


def _docstrings(src: str) -> set[int]:
    """找出所有**文档字符串**占的行号（1 起）。

    为什么需要：模块 docstring 里正解释着"为什么不用 `MessageBoxW`"这件事，
    直接按整份源码找那个词会把它自己的说明判成违规（`test_window_size.py`
    里 `self._options` 那条也踩过同一个坑）。
    """
    spans: set[int] = set()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            for i in range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1):
                spans.add(i)
    return spans


_DOC = _docstrings(SRC)
CODE = "\n".join(
    "" if (i + 1) in _DOC or ln.strip().startswith("#") else ln
    for i, ln in enumerate(SRC.splitlines()))


def check(name: str, got, want=True) -> None:
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}\n           期望 {want!r}\n           实际 {got!r}")


# ---------------------------------------------------------------- 1. 脚本内容
print("[1] PowerShell 脚本内容")

for _label, _ps in (("通知", notify._TOAST_PS1), ("弹窗", notify._MSGBOX_PS1)):
    try:
        _ps.encode("ascii")
        _ascii = True
    except UnicodeEncodeError:
        _ascii = False
    check(f"{_label}脚本是纯 ASCII", _ascii)
    # 标题正文必须从环境变量读 —— 这既是为了纯 ASCII，也是为了不让用户
    # 填的课程名（可能带引号、换行）拼进 PowerShell 源码里执行。
    check(f"{_label}脚本从环境变量读标题",
          "$env:DSH_NOTIFY_TITLE" in _ps)
    check(f"{_label}脚本从环境变量读正文",
          "$env:DSH_NOTIFY_BODY" in _ps)

check("通知脚本用的是 ToastNotificationManager",
      "ToastNotificationManager" in notify._TOAST_PS1)
check("弹窗脚本用的是 Windows.Forms.MessageBox",
      "System.Windows.Forms.MessageBox" in notify._MSGBOX_PS1)

# AUMID 得是**已注册的**，随手编一个会被系统丢掉。
check("AUMID 借的是 PowerShell 自己的",
      notify._AUMID.startswith("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}")
      and "powershell.exe" in notify._AUMID)

# ★ 老写法必须不能回来。这条是回归钉子：`MessageBoxW` 在本进程里弹，
# 调用方一退出窗口就没了（实测「返回 True、屏幕空白」）。
# 只看代码行 —— 模块 docstring 里正解释着这件事，按整份源码找会误判。
check("代码里没有 ctypes 的 MessageBoxW", "MessageBoxW" not in CODE)
check("代码里没有 windll", "windll" not in CODE)
check("代码里没有 import ctypes", not re.search(r"^\s*import ctypes", CODE, re.M))
# 正面钉子：弹窗必须走**独立进程**，且**不等它**（等它就等于把人挂在任务里）。
check("弹窗走 subprocess.Popen", "subprocess.Popen(" in CODE)
_popen_at = CODE.find("subprocess.Popen(")
check("Popen 后面没有 .wait() / communicate()",
      ".wait()" not in CODE[_popen_at:] and ".communicate()" not in CODE[_popen_at:])


# ---------------------------------------------------------- 2. 开关读得对
print("\n[2] 系统「通知」总开关")

_state = notify.toasts_enabled()
check("toasts_enabled() 只返回 True/False/None",
      _state in (True, False, None))
check("--status 能跑通", notify.main(["--status"]), 0)

# 开关关着 → send 必须**不去发横幅**，直接走弹窗。
# 把两个底层都换掉，观察调用顺序。
_calls: list[str] = []
_real_toast, _real_msgbox, _real_enabled = (
    notify._toast, notify._msgbox, notify.toasts_enabled)
try:
    notify.toasts_enabled = lambda: False                # type: ignore[assignment]
    notify._toast = lambda t, b, *, log: (_calls.append("toast"), True)[1]   # type: ignore[assignment]
    notify._msgbox = lambda t, b, *, log: (_calls.append("msgbox"), True)[1]  # type: ignore[assignment]
    _ok = notify.send("标题", "正文")
    check("开关关着时不发横幅（直接弹窗）", _calls, ["msgbox"])
    check("开关关着但弹窗起来了 → 返回 True", _ok, True)

    # 开关关着、又不要退路 → 老实返回 False，别假装成功
    _calls.clear()
    check("开关关着 + 不要退路 → False",
          notify.send("标题", "正文", msgbox_fallback=False), False)
    check("开关关着 + 不要退路 → 一个都没调", _calls, [])

    # ★ `msgbox_fallback=False` 时**任何情况**都不能弹窗。
    # `desktop_farm` 就是这么用的：多门课逐门刷完不逐个弹，否则屏幕上
    # 会堆一摞模态框（本机通知关着，弹窗就是唯一的出口）。
    _calls.clear()
    notify.toasts_enabled = lambda: True                 # type: ignore[assignment]
    notify._toast = lambda t, b, *, log: (_calls.append("toast"), False)[1]  # type: ignore[assignment]
    check("开关开着但横幅失败 + 不要退路 → False",
          notify.send("标题", "正文", msgbox_fallback=False), False)
    check("…且确实没弹窗", _calls, ["toast"])

    # 横幅成功 → 不该再弹窗
    _calls.clear()
    notify._toast = lambda t, b, *, log: (_calls.append("toast"), True)[1]   # type: ignore[assignment]
    check("横幅成功 → True", notify.send("标题", "正文"), True)
    check("横幅成功 → 不弹窗", _calls, ["toast"])
finally:
    notify._toast = _real_toast            # type: ignore[assignment]
    notify._msgbox = _real_msgbox          # type: ignore[assignment]
    notify.toasts_enabled = _real_enabled  # type: ignore[assignment]


# ------------------------------------------------------ 3. 永不抛异常
print("\n[3] 出错也不能带崩任务")

_logs: list[str] = []


def _boom(*_a, **_k):
    raise RuntimeError("模拟底层炸了")


for _name in ("_toast", "_msgbox", "toasts_enabled"):
    _saved = getattr(notify, _name)
    try:
        setattr(notify, _name, _boom)
        _logs.clear()
        _r = notify.send("标题", "正文", log=_logs.append)
        check(f"{_name} 炸了也不抛异常", isinstance(_r, bool))
        check(f"{_name} 炸了会写日志", bool(_logs))
    finally:
        setattr(notify, _name, _saved)

# 标题空 → 得落成默认的，不能弹一个没标题的框（通知中心里会显示成空白）。
_seen_title: list[str] = []
_saved_toast2, _saved_box2, _saved_en2 = (
    notify._toast, notify._msgbox, notify.toasts_enabled)
try:
    notify.toasts_enabled = lambda: True                                  # type: ignore[assignment]
    notify._toast = lambda t, b, *, log: (_seen_title.append(t), True)[1]  # type: ignore[assignment]
    notify._msgbox = lambda t, b, *, log: (_seen_title.append(t), True)[1]  # type: ignore[assignment]
    notify.send("   ", "", log=lambda m: None)   # 全空白
finally:
    notify._toast = _saved_toast2            # type: ignore[assignment]
    notify._msgbox = _saved_box2             # type: ignore[assignment]
    notify.toasts_enabled = _saved_en2       # type: ignore[assignment]
check("空标题落成「继续教育助手」", _seen_title and _seen_title[0], "继续教育助手")

# 找得到 PowerShell 吗（本机一定有；找不到也只是"发不出去"，不该崩）
_ps = notify._powershell()
check("_which() 返回的是字符串", isinstance(notify._which("绝对不存在的程序名"), str))
check("_which(绝对不存在的程序名) 返回空串",
      notify._which("绝对不存在的程序名"), "")
if sys.platform == "win32":
    check("Windows 上找到了 powershell", bool(_ps))
    check("找到的是 powershell.exe 或 pwsh.exe",
          _ps.lower().endswith(("powershell.exe", "pwsh.exe")) or _ps == "")


# -------------------------------------------------------- 4. finished()
print("\n[4] finished() 的长相")
_seen: list[tuple] = []
_saved_send = notify.send
try:
    notify.send = lambda t, b="", **k: (_seen.append((t, b)), True)[1]  # type: ignore[assignment]
    notify.finished("刷完了两门课", "用时 3 小时")
finally:
    notify.send = _saved_send  # type: ignore[assignment]
check("finished() 标题统一带程序名",
      _seen and _seen[0][0].startswith("继续教育助手 · "), True)
check("finished() 正文里两段都在",
      _seen and "刷完了两门课" in _seen[0][1] and "用时 3 小时" in _seen[0][1], True)


# -------------------------------------------------- 5. 调用方接上了没有
print("\n[5] 调用方接上了没有（源码级）")

_ROOT = Path(__file__).resolve().parent


def _src(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


_farm = _src("desktop_farm.py")
check("desktop_farm 里 import 了 notify", "import notify" in _farm)
check("desktop_farm 到了目标会按停视频", "pause_video()" in _farm)
# 通知的标题和正文分成好几行写（正文里有换行），所以只钉标题那一行。
check("desktop_farm 到了目标会发通知",
      '"继续教育助手 · 时长刷够了"' in _farm)
check("desktop_farm 那句话说清了「已自动暂停」",
      "视频已自动暂停" in _farm)
check("desktop_farm 多门课有汇总通知",
      '"继续教育助手 · 刷时长跑完了"' in _farm)
# 逐门发通知会堆一摞模态框（本机通知关着时弹窗是唯一出口）。
check("desktop_farm 只在单门课时才逐门发",
      "notify_done=(len(todo) == 1)" in _farm)
# 已经够了 / dry-run 这两条路径不该发（dry-run 只是报数）。
check("desktop_farm 的 dry-run 不发通知",
      "notify_done=not args.dry_run" in _farm
      or "not args.dry_run" in _farm)

_runner = _src("desktop_runner.py")
check("desktop_runner 里 import 了 notify", "import notify" in _runner)
check("desktop_runner 记下了这一轮跑过哪几个任务",
      "ran: list[str] = []" in _runner)
check("desktop_runner 全部跑完发通知",
      '"继续教育助手 · 全部任务完成"' in _runner)
check("desktop_runner 三个任务都记了名",
      _runner.count("ran.append(") >= 3)

_watch = _src("desktop_watch.py")
check("desktop_watch 有 --switch-course",
      '"--switch-course"' in _watch and "args.switch_course" in _watch)

_exam = _src("desktop_exam.py")
check("desktop_exam 的 plan_for 收 ai", "*, ai=None" in _exam)
check("desktop_exam 的 do_exam 收 ai", "ai=None) -> dict:" in _exam)
check("desktop_exam 会报 AI 花了多少",
      "AI 这一轮一共问了" in _exam)
check("AI 答案和官方答案分开放", 'cache["ai"]' in _exam)


# ------------------------------------------------------------------ 收尾
print()
print(f"test_notify: {_passed} passed, {_failed} failed")
raise SystemExit(1 if _failed else 0)
