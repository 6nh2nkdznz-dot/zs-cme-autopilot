"""中山医院继续教育平台助手 —— 图形界面（customtkinter 版）。

替代原来的纯 ttk 界面。外观上的差别：

* 深色主题 + accent 配色，卡片式分区，圆角控件
* 任务用**开关**而不是复选框，每项带一行说明
* 环境状态做成**状态卡**，一眼看出 adb / 连接 / 资源是否就绪
* 日志带级别着色（错误红、成功绿、警告黄），可复制、可清空
* 左栏控制、右栏日志，窗口拉大时日志区优先扩展

行为逻辑全部在 `scripts/core.py`，本文件只负责显示与线程调度——
这样界面换掉不会影响自动化行为，反之亦然。

线程模型：tkinter/customtkinter 都不是线程安全的，所以自动化跑在后台线程，
日志通过 queue 回传，主线程用 after() 轮询刷新。
"""

from __future__ import annotations

import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path

#: 配色。深色主题下 customtkinter 用 ("浅色", "深色") 元组；
#: 这里只给深色值，界面固定用 dark。
COL_BG = "#16181d"          # 窗口底
COL_CARD = "#1e2128"        # 卡片
COL_CARD_HI = "#252932"     # 卡片悬浮/内嵌
COL_BORDER = "#2f343f"
COL_TEXT = "#e6e9ef"
COL_TEXT_DIM = "#8b93a3"
COL_ACCENT = "#3b82f6"      # 主色（蓝）
COL_ACCENT_HI = "#2f6fd8"
COL_OK = "#22c55e"
COL_WARN = "#f59e0b"
COL_ERR = "#ef4444"

#: 主窗口高度上限（**Tk 逻辑像素**，不是物理像素）。
#:
#: 实测（本机缩放 1.5，工作区逻辑高 1018）：左栏内容 800 逻辑px，
#: 窗口给到 900 时左栏视口 1308 物理px，内容全部显示、不出现滚动条；
#: 再矮就会开始滚。所以这是「够用就好」的值，不是拍脑袋定的。
#: 原来窗口高度 = 整个工作区高（1018），顶天立地，左下一大块空白。
WIN_TARGET_H = 900

#: 左栏列宽（**Tk 逻辑像素**）。
#:
#: ## 为什么现在是 405（原来是 480）
#:
#: 用户要求「在日志左边加一块设置，削减日志宽度，**每一部分都四分之一页面**」。
#: 窗口默认宽 1620 逻辑px，四等分就是每栏 405。四栏依次是：
#:
#:     左栏（任务/按钮） │ 设置面板 │ 运行日志 │ 调试画面
#:         LEFT_COL_W      SETTINGS_COL_W  LOG_COL_W  DEBUG_COL_W
#:
#: ## 加宽不能解决问题（老注释，仍然成立）
#:
#: 描述被截**不是**宽度不够：`CTkLabel` 把高度锁在 42px、只装得下 3 行字，
#: 折成 4 行的描述第 4 行整行被吃掉（详见 `_build_task_card` 里的注释，
#: 那里换成了原生 `tk.Label`）。当时我误判成横向被截，把这里从 420 一路
#: 加到 480，其实加宽只是让折行数变少、把问题暂时盖住。既然宽度不是病根，
#: 那它就该让位给真正需要宽度的日志和调试画面。
#:
#: 为什么不用 `_measure_left_width()` 动态量：`wraplength` 会把 label 的
#: requested 宽**钉死**在折行宽度上，量出来的值反过来跟着它变，是个循环 ——
#: 实测量到 646、按 646 给足，渲染出来左栏内容却只有 590，越调越糊涂。
#: 所以改成**先定列宽，再由列宽推 wraplength**（见 `_build_task_card`）。
LEFT_COL_W = 405

#: 设置面板列宽（Tk 逻辑px）。四等分里的第二栏。
#:
#: 这块原来是**弹窗**（`_SettingsDialog`）。改成常驻面板的原因是用户报
#: 「我并不能点开设置按钮」—— 弹窗那条路当时有两个真 bug（见
#: `_SettingsDialog` 和 `App.__init__` 里的注释），而且弹窗会把主界面
#: 盖住，改完设置看不到任务开关的当前状态。常驻面板还有一个好处：
#: 点哪个 ⚙ 就在这儿换内容，**不用等窗口开合**，也没有「弹窗跑到屏幕外」。
SETTINGS_COL_W = 405

#: 右栏列宽下限（Tk 逻辑px）。右栏 `weight=1`，会吃掉窗口的所有富余宽度。
#:
#: 右栏内部又横分成**左半边日志 + 右半边调试画面**（用户要求
#: 「左半边日志，右半边视图」），所以下限 = 两者之和。
#:
#: 两个值现在都是 405 —— 用户要的是「每一部分都四分之一页面」，
#: 窗口 1620 逻辑px 四等分就是 405。原来是 460 / 560，那个分配下
#: 日志独占 480 逻辑px，而调试画面又是最小 560，四栏根本塞不下。
#:
#: ⚠ 调试画面在 405 下比原来窄了 155 —— `debug_view.embed_scale()` 会
#: 跟着折算出更小的缩放（画面本来就 9:16，宽度有富余，损失主要在
#: 右侧明细栏，窄到一定程度它要横向滚动）。这是用户明确要的取舍。
LOG_COL_W = 405
DEBUG_COL_W = 405
RIGHT_COL_W = LOG_COL_W + DEBUG_COL_W


def _bootstrap_path() -> None:
    """把 scripts 目录挂进 sys.path。

    源码运行时是 <项目根>/scripts；打包后 PyInstaller 放在 <bundle>/scripts。
    两种都要能 import。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent
    cand = base / "scripts"
    target = cand if cand.is_dir() else base
    if str(target) not in sys.path:
        sys.path.insert(0, str(target))


_bootstrap_path()

# 名字与版本号来自 scripts/appinfo.py（唯一来源）。
# 原先这里和 launcher.py 各写一份，结果 exe 显示 v0.2.0、源码显示 v0.3.0。
# **必须在 _bootstrap_path() 之后 import** —— 那时 scripts 目录才在 sys.path 上。
from appinfo import APP_TITLE, APP_VER  # noqa: E402

import customtkinter as ctk  # noqa: E402

import paths  # noqa: E402
from core import DEFAULT_ROUTE, ROUTES, TASKS_BY_ROUTE, AppCore  # noqa: E402

# 浏览器版那三个任务的执行器。它是**独立**的一个模块（不并进 core.py），
# 因为那一整套 Controller/Resource/Tasker/微信存活检查，浏览器版一个都
# 不需要 —— 理由写在 scripts/desktop_runner.py 的 docstring 里。
import desktop_runner  # noqa: E402

# 「每个功能能调什么」全部声明在 scripts/taskspec.py 里（照 MaaFramework 的
# ProjectInterface V2 协议那套 task / option / setting 模型）。界面**只负责渲染**
# 那张表：加一个可调项只改 taskspec.py，这里一行都不用动。
import taskspec  # noqa: E402


def debug_view_cmd() -> tuple[list[str], str]:
    """拼出**独立**调试视图窗口的命令行，返回 `(命令, 工作目录)`。

    ## 它现在还用在哪

    主界面的「🔍 调试视图」按钮已经改成**内嵌**（不再开窗口），见
    `App.on_debug_view`。这个函数保留给命令行用法：
    `ZSCMEAutopilot.exe --run debug_view` / `python scripts\\debug_view.py`，
    不开主界面时排查问题用。

    ## 为什么要单独抽成函数

    这个拼法**在打包后会变**，而且踩过一次坑，所以必须能脱离界面测试。

    * 源码运行：`sys.executable` 是真 python.exe → `python debug_view.py`
    * 打包运行：**`sys.executable` 就是 ZSCMEAutopilot.exe 自己**，原来那句
      就变成了 `ZSCMEAutopilot.exe <debug_view.py 路径>`。而 `launcher.py`
      的 `main()` 只认 `--selftest` / `--run` / `--list` / `--classic`，
      **其余参数一律忽略**并去开主界面 —— 于是点「调试视图」会**又弹一个
      主界面窗口**，调试视图根本不出现。
      正确的走法是 exe 自己的 `--run debug_view`（`_run_script` 用 runpy
      执行脚本，脚本的 `__main__` 块照常跑）。

    工作目录统一用「项目的根」，因为脚本和 `assets/` 都按相对路径找。

    Raises:
        RuntimeError: 源码环境下找不到 `debug_view.py`。
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run", "debug_view"], \
            str(Path(sys.executable).resolve().parent)

    script = Path(paths.__file__).resolve().parent / "debug_view.py"
    if not script.is_file():
        raise RuntimeError(f"调试视图不存在: {script}")
    return [sys.executable, str(script)], str(script.parent.parent)


class QueueLogger:
    """把 print 风格输出塞进队列，交给主线程渲染。

    同时**落盘**到 `debug/log/app.log`。

    为什么必须落盘：以前日志只活在界面里，用户跑完一轮我什么都没有 ——
    只能靠 `debug/log/maafw.log` 反推，而那份日志是框架 C++ 写的、
    没有我们自己的 `[step]`/`[page]`/`[tap]` 行，节点名还全是乱码。
    2026-10-07 那次「进入答题点不动」就是这么卡住的：看不到程序自认为
    点到了哪里、判定成什么页面。落盘之后这类问题一眼可查。
    """

    def __init__(self, q: "queue.Queue[str]", path: "Path | None" = None) -> None:
        self.q = q
        self.path = path
        self._fh = None
        self._lock = threading.Lock()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._fh = path.open("a", encoding="utf-8", newline="\n")
                self._fh.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
                self._fh.flush()
            except OSError:
                self._fh = None

    def _to_file(self, line: str) -> None:
        if self._fh is None:
            return
        with self._lock:
            try:
                self._fh.write(f"{datetime.now():%H:%M:%S} {line}\n")
                self._fh.flush()
            except (OSError, ValueError):
                self._fh = None

    def __call__(self, msg: str = "") -> None:
        s = str(msg)
        self.q.put(s)
        for line in s.splitlines() or [""]:
            self._to_file(line)

    def write(self, msg: str) -> None:
        if msg:
            for line in msg.rstrip("\n").split("\n"):
                self.q.put(line)
                self._to_file(line)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                finally:
                    self._fh = None


def _line_tag(line: str) -> str:
    """按内容判断日志级别，用于着色。"""
    low = line.strip()
    if not low:
        return "plain"
    if "✗" in low or "[FATAL]" in low or "[ERROR]" in low or "失败" in low:
        return "err"
    if "⚠" in low or "未成功" in low or "请先" in low:
        return "warn"
    if "✓" in low or low.startswith("✓"):
        return "ok"
    if low.startswith(">>>") or low.startswith("—"):
        return "head"
    if "[task]" in low:
        return "task"
    return "plain"


class _SettingsForm:
    """按 `scripts/taskspec.py` 那张声明表渲染出来的一块设置表单。

    ## 为什么是一个通用表单，而不是每个任务手写一个

    「每个功能都有自己的设置」很容易做成 N 个各写各的窗口，然后每加一个
    可调项就要动界面代码 —— 加着加着就会出现「面板上有这个开关，但保存
    的时候忘了读它」这种半截活。这里界面**只认 `Option.type`**：

    * `switch` → 开关
    * `select` → 下拉（存的是 `Case.name`，显示的是 `Case.label`）
    * `input`  → 输入框（`kind="int"` 会做范围校验；`secret=True` 打点）

    值的语义、默认值、范围全在 `taskspec` 那边，界面一个业务判断都不写。
    加可调项 = 改 `taskspec.py` 一处，这个文件不用动。

    ## 为什么从「弹窗」改成了「常驻面板」

    原来这是个 `CTkToplevel`（叫 `_SettingsDialog`）。用户报「我并不能点开
    设置按钮」，查下来弹窗那条路上撞了两个真 bug：

    1. 两处都写成 `_SettingsDialog(self, …)`，而 `App` **不是控件**
       （它只是持有 `self.root`），`CTkToplevel.__init__` 读 `master.tk`
       直接 `AttributeError`。
    2. `self._options = list(options)` 覆盖了 tkinter `Misc` 上的内部方法
       `_options`，任何 `configure(...)` 都炸 `TypeError: 'list' object is
       not callable`。

    两个都修了，但**继续用弹窗的价值不大**：

    * 弹窗的失败模式是「点了没反应」—— 异常发生在 Tk 回调里，默认只往
      stderr 打 traceback，而这是个没有控制台的窗口程序，用户根本看不见。
      嵌进主界面的面板要是没渲染出来，整栏是空的，一眼就知道坏了。
      （另外 `App.__init__` 现在挂了 `report_callback_exception`，把 Tk
      回调里的异常打进运行日志，这类静默失败以后能查了。）
    * 弹窗会把任务开关盖住，改设置时看不到自己勾了哪些任务。
    * 用户点名要「在日志 UI 的左边加入设置的部分，在用户没点击设置时
      显示『点击左边的设置以设置选项』」—— 本来就是个常驻的栏位。

    ## 几个刻意的选择

    * **长文字用原生 `tk.Label`**（`_wrapped`）：`CTkLabel` 在高 DPI 下会把
      高度锁死在 42px，折行到第 4 行就整行看不见。代价是要手写 `bg`，
      不给的话会在深色底上留一块浅色方块。
    * **保存前先校验**：填了个 `"九十分钟"` 的话，直接标红留在面板里，
      而不是静默回退到默认 —— 静默回退等于用户以为自己改了、其实没改，
      下次跑出来还得再查一遍。
    * **字段叫 `self._opts`，不叫 `self._options`**：见上面 bug 2。这里
      `_SettingsForm` 已经不是控件了，但留着这个会撞名字的坑没有好处，
      而且以后万一想让它继承 `tk.Frame`，就会原地复发。
    """

    def __init__(self, parent, *, options, values: dict, width: int) -> None:
        #: ★ 不能改叫 `_options`，理由见类 docstring 的 bug 2。
        self._opts = list(options)
        self._width = width
        #: key → 取当前值的闭包。保存时逐个调。
        self._readers: dict[str, object] = {}
        #: key → 控件背后的 Tk 变量。「恢复默认」直接改它（双向绑定），
        #: 不用重建控件 —— 重建要处理 pack 顺序，还会丢掉光标位置。
        self._vars: dict[str, "tk.Variable"] = {}
        #: 放红色出错提示的 label，由调用方建好后回填（见 `attach_error`）。
        self._err = None

        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.frame.pack(fill="x")
        for opt in self._opts:
            self._build_option(self.frame, opt, values.get(opt.key, opt.default))

    def attach_error(self, label) -> None:
        """把「出错提示」那一行的 label 交给表单，校验失败时往里写字。

        不在这里自己建，是因为它得固定在面板底部（不跟着滚动），而表单
        本体是塞进滚动区里的。
        """
        self._err = label

    def _fail(self, text: str) -> None:
        """写一句红字。没接 label 就退化成日志，总比吞掉强。"""
        if self._err is not None:
            self._err.configure(text=text)

    # -- 渲染一个控件 ----------------------------------------------------

    def _build_option(self, parent, opt, cur) -> None:
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(box, text=opt.label, anchor="w",
                     font=ctk.CTkFont(size=13),
                     text_color=COL_TEXT).pack(fill="x")

        # 控件宽度跟着栏宽走，**不写死 240**：这一栏是四等分里的 405，
        # 240 留给 API 地址那种长字符串只看得见小半截，用户没法核对
        # 自己粘进去的是不是对的。
        cw = max(160, min(320, self._width - 40))

        if opt.type == "switch":
            var: "tk.Variable" = tk.BooleanVar(value=bool(cur))
            ctk.CTkSwitch(box, text="开启", variable=var, onvalue=True,
                          offvalue=False, height=20,
                          font=ctk.CTkFont(size=12), text_color=COL_TEXT,
                          progress_color=COL_ACCENT, fg_color=COL_BORDER,
                          button_color="#ffffff",
                          button_hover_color="#e5e7eb").pack(anchor="w", pady=(6, 0))
            self._readers[opt.key] = lambda v=var: bool(v.get())

        elif opt.type == "select":
            labels = [c.label for c in opt.cases]
            by_label = {c.label: c.name for c in opt.cases}
            by_name = {c.name: c.label for c in opt.cases}
            var = tk.StringVar(value=by_name.get(cur, labels[0] if labels else ""))
            ctk.CTkOptionMenu(
                box, values=labels, variable=var, width=cw, height=30,
                fg_color=COL_CARD_HI, button_color=COL_BORDER,
                button_hover_color=COL_ACCENT, text_color=COL_TEXT,
                dropdown_fg_color=COL_CARD, dropdown_text_color=COL_TEXT,
                dropdown_hover_color=COL_ACCENT,
                font=ctk.CTkFont(size=12),
            ).pack(anchor="w", pady=(6, 0))
            self._readers[opt.key] = (
                lambda o=opt, v=var: by_label.get(v.get(), o.default))

        else:  # input
            # `taskspec.fmt_value` 而不是 `str()`：`target` 的默认值是
            # `90.0`，直接 `str()` 会让输入框里写着「90.0」——
            # 用户会以为「填小数才合法」。同一个函数也管 `summary`，
            # 两边显示保持一致。
            var = tk.StringVar(value="" if cur is None
                               else taskspec.fmt_value(cur))
            row = ctk.CTkFrame(box, fg_color="transparent")
            row.pack(fill="x", pady=(6, 0))
            # `secret` 是给 API 密钥用的：`show="•"` 让肩膀后面的人看不见。
            # 用 `getattr` 取是**故意的** —— `Option` 上这个字段是后加的，
            # 老的声明表里没有，缺字段时按普通输入框渲染即可。
            secret = bool(getattr(opt, "secret", False))
            entry = ctk.CTkEntry(
                row, textvariable=var, width=cw, height=30,
                fg_color=COL_CARD_HI, border_color=COL_BORDER,
                text_color=COL_TEXT, font=ctk.CTkFont(size=12),
                placeholder_text=opt.placeholder, show="•" if secret else "",
            )
            entry.pack(fill="x")
            if opt.minimum is not None or opt.maximum is not None:
                # 范围提示也走 `fmt_value` —— `target` 的上界是 `100000.0`，
                # 直接格式化会写成「可填 0.1 ~ 100000.0」，看着像必须填小数。
                rng = (f"{taskspec.fmt_value(opt.minimum) if opt.minimum is not None else ''}"
                       f" ~ {taskspec.fmt_value(opt.maximum) if opt.maximum is not None else ''}")
                # 范围提示**单独占一行**，不挤在输入框右边。
                # 挤着放的后果实测过：这一栏是四等分里的 405 逻辑px，
                # 「输入框 + 提示」并排会顶出面板右边，被切掉的正好是
                # 「0 ~ 99」里的后半截 —— 看起来像提示写错了。
                _wrapped(row, f"可填 {rng}", self._width - 60,
                         COL_CARD, COL_TEXT_DIM).pack(fill="x", pady=(3, 0))
            self._readers[opt.key] = lambda v=var: v.get()

        self._vars[opt.key] = var

        if opt.description:
            # 灰字说明。`bg` 必须给成卡片底色，否则深色底上会留一块浅色方块。
            _wrapped(box, opt.description, self._width - 40,
                     COL_CARD, COL_TEXT_DIM).pack(fill="x", pady=(5, 0))

    # -- 动作 ------------------------------------------------------------

    def reset(self) -> None:
        """把控件恢复到声明里的默认值 —— **不写盘**，还得点保存。

        不直接落盘是故意的：点错了「恢复默认」就把用户攒的设置抹掉、
        没有撤销机会。留在面板里，用户还能把值改回来。
        """
        for opt in self._opts:
            var = self._vars.get(opt.key)

            if opt.type == "select":
                case = next((c for c in opt.cases if c.name == opt.default), None)
                if var is not None and case is not None:
                    var.set(case.label)
                    continue

            if var is not None:
                if opt.type == "switch":
                    var.set(bool(opt.default))
                elif opt.default is None:
                    var.set("")
                else:
                    # 和 `_build_option` 的初值用同一个格式化函数，
                    # 否则「恢复默认」会把 `90.0` 塞回输入框、而初值显示的是 `90`。
                    var.set(taskspec.fmt_value(opt.default))
        self._fail("")

    def collect(self) -> "tuple[dict, str]":
        """读出所有值并校验 → `(值, 出错文字)`。出错时第二个元素非空。

        返回错误而不是自己弹框：面板要把红字写在自己底部那一行上，
        表单不知道那一行在哪。
        """
        out: dict = {}
        for opt in self._opts:
            reader = self._readers.get(opt.key)
            if reader is None:
                continue
            raw = reader()
            if opt.type == "input" and opt.kind == "int":
                text = str(raw).strip()
                if text == "":
                    out[opt.key] = opt.default
                    continue
                try:
                    num = int(text)
                except ValueError:
                    return {}, f"「{opt.label}」要填整数，现在填的是「{text}」"
                if opt.minimum is not None and num < opt.minimum:
                    return {}, f"「{opt.label}」不能小于 {opt.minimum}"
                if opt.maximum is not None and num > opt.maximum:
                    return {}, f"「{opt.label}」不能大于 {opt.maximum}"
                out[opt.key] = num
            elif opt.type == "input" and opt.kind == "float":
                # 浮点单独一条：时长那种「2.5 小时」是合法输入，
                # 走 int 那条会被判成「要填整数」。
                text = str(raw).strip()
                if text == "":
                    out[opt.key] = opt.default
                    continue
                try:
                    val = float(text)
                except ValueError:
                    return {}, f"「{opt.label}」要填数字，现在填的是「{text}」"
                if opt.minimum is not None and val < opt.minimum:
                    return {}, f"「{opt.label}」不能小于 {opt.minimum}"
                if opt.maximum is not None and val > opt.maximum:
                    return {}, f"「{opt.label}」不能大于 {opt.maximum}"
                out[opt.key] = val
            else:
                out[opt.key] = raw
        return out, ""


def _plain(text: str) -> str:
    r"""去掉说明文字里的轻量 Markdown 记号。

    设置项的 `description` 是给**人**写的，写的时候顺手打了 `**强调**` 和
    `` `代码` `` —— 但 `_wrapped` 用的是原生 `tk.Label`，它不懂 Markdown，
    会把星号和反引号**原样画出来**。实测截图里的效果是：

        填了才启用 AI 答题。任何 **OpenAI 兼容**的接口都行

    看着像程序拼接字符串时漏了一步。与其在每个 description 里手工避让
    （以后加一项就会再犯一次），不如在渲染这一层统一吃掉 —— 这也是
    `_wrapped` 唯一该知道的「格式」。
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text.replace("**", "").replace("`", "")


def _wrapped(parent, text: str, wraplength: int, bg: str,
             fg: str) -> "tk.Label":
    """一段会折行的灰字，用**原生 `tk.Label`**。

    不能用 `CTkLabel`：它在高 DPI 下把高度锁死在 42px（两行多一点），
    折到第 4 行的说明整行看不见 —— 表现是「右边好像被切了」，实际是
    下面几行没了。实测同一个字符串：`CTkLabel` 42px、原生 `tk.Label` 63px。
    详见 `_render_tasks` 里那段注释。

    代价是失去 CTk 的主题配色，得手写 `bg`；而且 `bg` **必须**等于所在
    容器的底色，写错了会在深色底上留一块突兀的方块。
    """
    return tk.Label(
        parent, text=_plain(text), justify="left", anchor="w",
        wraplength=wraplength, font=("", 10), bg=bg, fg=fg,
        bd=0, highlightthickness=0,
    )


class App:
    def __init__(self, root: "ctk.CTk") -> None:
        self.root = root
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.logger = QueueLogger(self.log_q, paths.log_dir() / "app.log")

        # 后端：不含界面，日志走 logger
        self.core = AppCore(log=self.logger)

        self.worker: threading.Thread | None = None
        self.switches: dict[str, "ctk.CTkSwitch"] = {}
        self.status_dots: dict[str, "ctk.CTkLabel"] = {}
        #: 每个任务行右边那个 ⚙（只有 `taskspec` 里声明了可配置项的任务才有）。
        self.gears: dict[str, "ctk.CTkButton"] = {}
        #: 任务键 → 原来显示在开关下面那行灰字。小字删了，但这段文字没丢：
        #: 它挪到 ⚙ 面板顶上（`on_task_settings` 里当 `intro` 用）。
        self._task_desc: dict[str, str] = {}

        root.title(f"{APP_TITLE} v{APP_VER}")
        root.configure(fg_color=COL_BG)

        # 让**所有控件回调里的异常**都进日志窗口，而不是无声无息。
        #
        # 为什么必须接管：Tk 默认把回调异常丢给 `report_callback_exception`，
        # 它只往 **stderr** 打一份 traceback —— 而这个程序是用 `--classic`
        # 或双击 exe 起的（`launcher.py` 里没有控制台），那份 traceback
        # 谁也看不见。用户看到的就是「点了按钮没反应」。
        #
        # 实测踩过：两个 ⚙ 设置按钮都写成了 `_SettingsDialog(self, …)`，
        # 而 `App` 不是控件（它只是**持有** `self.root`），于是
        # `CTkToplevel.__init__` 读 `master.tk` 时抛
        # `AttributeError: 'App' object has no attribute 'tk'`。
        # 界面上完全看不出来，拖到用户报「我并不能点开设置按钮」才发现。
        def _on_cb_error(exc_type, exc, tb) -> None:  # noqa: ANN001
            try:
                import traceback
                lines = traceback.format_exception(exc_type, exc, tb)
                self.logger("")
                self.logger(f"[ui] ✗ 界面回调出错: {exc_type.__name__}: {exc}")
                for ln in "".join(lines).rstrip().splitlines()[-12:]:
                    self.logger("      " + ln)
                self.logger("[ui] 这一下点击没有生效，上面就是原因。")
                try:
                    self.lbl_state.configure(text="界面出错", text_color=COL_ERR)
                except Exception:  # noqa: BLE001 - 状态栏可能还没建出来
                    pass
            except Exception:  # noqa: BLE001 - 连报错都失败了就只能认了
                pass

        root.report_callback_exception = _on_cb_error

        self._build()
        self._fit_window()
        self._pump_log()
        self._greet()
        self._adopt_data()

    def _fit_window(self) -> None:
        """按左栏内容定窗口尺寸，但不超过屏幕。

        ## 为什么需要

        原来写死 `geometry("1080x720")`，而左栏内容实测要 **904px**
        （物理像素，本机 DPI 缩放 1.5）—— 底部的「调试视图」按钮整个落在
        可视区外，用户根本够不到。这就是「exe 里没有调试按钮」的真相：
        按钮一直在，只是被挤出去了。

        ## 踩过的坑：不能用 winfo_reqheight()

        一开始写的是 `need_h = root.winfo_reqheight()`，结果它返回 **432**，
        比内容实际高度小一半还多 —— 因为左栏换成了 `CTkScrollableFrame`，
        而它**对外只声明「我能滚」，不上报内容的自然高度**。于是窗口被算成
        620（minsize 下限），比改之前还矮，问题反而更严重。
        所以这里用 `_measure_left_height()` 直接量内容。

        **必须在 `_build()` 之后调用**，要等控件建出来才量得到高度。

        实测各块高度（物理像素，缩放 1.5）：
            标题 48 + 环境状态卡 193 + 任务卡 420 + 动作 135 + 工具行 45
        """
        self.root.update_idletasks()
        # 单位提醒（下面高度那段全靠它）：屏幕是「缩放后像素」，而
        # `geometry()` 收的是「CTk 逻辑像素」，本机差 1.5 倍。实测
        #     geometry("1080x621") → Win32 实测窗口 1642x988、winfo_height 932
        # 所以物理像素要 `_to_logical()` 折算过才能进 `geometry()`。
        #
        # 以前这里还算了个 `scale = ScalingTracker.get_window_scaling()`，
        # 用来把右栏宽度按物理当量放大。现在宽度是四等分写死的常量，
        # 这个变量没人用了，删掉。

        # ---- 高度：按内容给，不顶满工作区 ----
        #
        # 用户反馈「窗口高度太高了」。原来这里是
        #     h = (work_px - 16) // scale
        # 也就是直接开成**整个工作区那么高**（逻辑 1019），窗口顶天立地，
        # 左下角还空一大块。
        #
        # 现在按内容高算，并压到 `WIN_TARGET_H`。实测（本机缩放 1.5）：
        #     左栏内容 856 逻辑px  →  窗口 900 逻辑px（1350 物理px）
        #     左栏视口 1308 物理px，内容**全部显示、不需要滚动**
        # 也就是说 900 有实测量背书：再矮就要开始出滚动条了。
        #
        # **单位必须统一**，这里踩过一个坑：
        #   `_measure_left_height()` 返回的是 **Tk 逻辑像素**（本机 856）；
        #   `_work_area_height()` 走 Win32 API，返回的是**物理像素**（本机
        #   1528），两者不能直接比 —— 得靠 `_to_logical()` 折算成 1018。
        # 我一开始把两个数都当物理像素，`952 // 1.5 = 634`，窗口被砍成一半。
        #
        # 另外 `root.winfo_reqheight()` = 432，**靠不住**（原因见
        # `_measure_left_height` 的说明），所以这里直接量内容。
        work_logical = self._to_logical(self._work_area_height())
        need = self._measure_left_height() + 96          # 96 = 标题栏 + 上下边距
        cap = work_logical - 40                          # 留 40px，别贴着任务栏
        h = int(max(660, min(need + 60, WIN_TARGET_H, cap)))
        # ---- 宽度：按内容量出来的需求给，不再猜 ----
        #
        # 这一栏踩了好几个坑，最后落到「量一次、给够」：
        #   1. `max(1200, ...)` —— 实测出 730 逻辑px，左栏拿不到 420，
        #      任务描述右边被硬切（用户看到「…：看」后面没了）。
        #   2. 猜「左栏 420 + 右栏 520 + 边距 = 1080」—— 窗口是变宽了，
        #      但加权分配后左栏还是原来的宽，字**照样**被切。
        #   3. 想靠 `_measure_left_width()` 量出「内容自然宽」——量不准，
        #      `wraplength` 已经把 requested 宽**钉死**在折行宽度上了，
        #      量出来的是 646 而不是文字真正需要的宽（这是个循环）。
        #
        # 宽度 = 前三栏列宽之和，都是逻辑px（`geometry()` 的单位）。
        #
        # 三栏现在**等宽 405**（用户要求「削减日志窗口的宽度达到每一部分都
        # 四分之一页面的效果」），加到 1620 正好是四等分。
        # 左栏别再套 `_measure_left_width()` 了，原因见 `LEFT_COL_W` 的说明。
        #
        # 右栏以前按 `* scale` 放大给：日志是**等宽字体**，逻辑 700px 在 1.5 倍
        # 缩放下只够显示 60 来个字符，日志里的路径就折行了（实测过）。
        # **现在不给这个富余了** —— 用户要的是四等分，日志区独占的宽度
        # 被砍到 405 逻辑px（约 47 个等宽字符，路径会折行）。这是明确
        # 要的取舍：设置面板要地方，日志少显几个字可以忍。
        # 窗口拉宽时右栏 `weight=1` 仍然会跟着长。
        #
        # 上限 1620 = 405 × 4：再加宽只是让右栏更长，边际收益不大，
        # 还会把窗口顶到 85% 屏宽（本机工作区逻辑宽 1707）。
        need = LEFT_COL_W + SETTINGS_COL_W + RIGHT_COL_W
        w = int(max(1080, min(need, 1620)))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(900, 620)
        self._center_in_work_area(w, h)

    def _measure_left_width(self) -> int:
        """左栏**真正**需要的宽度（Tk 逻辑像素）。

        为什么不能直接信 `root.winfo_reqwidth()`：左栏是 `CTkScrollableFrame`
        套 `CTkFrame`，外层对 manager 只声明「我能滚」，所以报出来的 736
        是两栏「挤在一起」的最小宽，不是内容不换行要的宽 —— 和高度那次
        （`winfo_reqheight()` 报 432）是同一个坑。

        这里改成逐个子控件量 requested 宽，再叠上它相对左栏的偏移：
            子控件右边界 = winfo_x() + winfo_reqwidth()
        取最大值就是左栏不被截断所需宽度。子控件自己是 `grid` 排的，
        `winfo_x()` 已经是相对父控件的坐标，直接可用。
        """
        frame = getattr(self, "_left_scroll", None)
        if frame is None:
            return 420
        try:
            self.root.update_idletasks()
            widest = 0
            for child in frame.winfo_children():
                for sub in [child, *child.winfo_children()]:
                    try:
                        right = sub.winfo_x() + sub.winfo_reqwidth()
                    except Exception:  # noqa: BLE001 - 单个控件量不到就跳过
                        continue
                    widest = max(widest, right)
            return int(widest) or 420
        except Exception:  # noqa: BLE001 - 量不到就用实测出来的 420
            return 420

    def _center_in_work_area(self, logical_w: int, logical_h: int) -> None:
        """把窗口摆在屏幕工作区正中。

        必须显式摆位置。踩过的坑：`geometry("WxH")` **只改尺寸、不改位置**，
        窗口停在之前那个 x/y 上。前一个尺寸是 1019 逻辑高（顶满工作区），
        改成 900 之后位置没跟着动，底部照样露在任务栏下面 ——
        实测打包 exe 的窗口是 `y=130..1067`，工作区只到 `1019`，
        也就是 937 高的窗口在 1019 的工作区里**依然超出屏幕 48px**。

        注意 `geometry("+x+y")` 也是**逻辑像素**（和 W/H 同一套单位），
        所以工作区的物理坐标要先换算。
        """
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            if not ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0
            ):
                return
            left = self._to_logical(rect.left)
            top = self._to_logical(rect.top)
            area_w = self._to_logical(rect.right - rect.left)
            area_h = self._to_logical(rect.bottom - rect.top)
            x = left + max(0, (area_w - logical_w) // 2)
            y = top + max(0, (area_h - logical_h) // 2)
            self.root.geometry(f"+{x}+{y}")
        except Exception:  # noqa: BLE001 - 摆位置失败不影响使用，用默认位置
            pass

    def _dpi_scale(self) -> float:
        """本机缩放系数（逻辑px → 设备像素的倍数，本机 150% 时是 1.5）。

        ## 为什么需要它：`minsize` 的单位坑

        **`grid_columnconfigure(minsize=…)` 收的是设备像素，不是 CTk 逻辑
        像素。** 实测（本机 150%）：`minsize=405` 量出来就是 405 设备像素
        = **270 逻辑px** —— 四栏立刻不等宽，左栏窄掉三分之一，右边空出来的
        全被 `weight=1` 的日志吃掉（截图里日志比调试宽一倍多，就是这么来的）。

        成因：CTk 只缩放**控件自己**的 `padx`/`pady`/`width`/`height`
        （`CTkBaseClass` 的 `_grid_configure` → `_apply_argument_scaling`），
        而 `grid_columnconfigure` 是**容器**上的方法，CTk 不管它，参数原样
        转给 Tk —— Tk 在这个进程里的像素就是设备像素。

        所以本文件的列宽常量一律按**逻辑px**写（`geometry()`、控件
        `width=`、`wraplength` 用的都是逻辑px），只在喂给 `minsize` 时乘
        这个系数。**别改成把常量写成设备像素** —— 那样 `geometry()` 和
        `wraplength` 又会错，换个 DPI 的机器上全乱。
        """
        try:
            return max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        except Exception:  # noqa: BLE001 - 取不到就按不缩放算
            return 1.0

    def _to_logical(self, physical_px: int) -> int:
        """把 Win32 API 给的像素数换算成 Tk 逻辑像素。

        Win32 侧的单位是「这个进程看到的像素」：进程开了 DPI 感知就是真物理
        像素，没开就是系统缩放后的像素。用 `GetDpiForWindow` 判断属于哪种，
        统一折算到逻辑像素 —— 也就是 `geometry()` 收的那个单位。

        实测（本机缩放 150%，DPI 144）：
            感知进程   : SystemParametersInfoW → 1528, GetDpiForWindow → 144
                         1528 / 144 * 96 = 1019 ✓
            不感知进程 : SystemParametersInfoW → 1019, GetDpiForWindow → 96
                         1019 / 96 * 96 = 1019 ✓
        两条路都落到 1019。
        """
        try:
            import ctypes

            dpi = ctypes.windll.user32.GetDpiForWindow(self.root.winfo_id())
            if dpi:
                return int(physical_px * 96 / dpi)
        except Exception:  # noqa: BLE001 - 取不到就按 96 算（不缩放）
            pass
        return int(physical_px)

    @staticmethod
    def _work_area_height() -> int:
        """屏幕**工作区**高度（排除任务栏）。

        用的是 `SystemParametersInfoW(SPI_GETWORKAREA)`。不能直接拿
        `winfo_screenheight()` —— 它包含任务栏那 48px，照它开窗口会被
        任务栏盖住底部。
        """
        try:
            import ctypes

            class _RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rc = _RECT()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(rc), 0):
                return rc.bottom - rc.top
        except Exception:  # noqa: BLE001 - 非 Windows 或调用失败时退回去
            pass
        return 900

    def _measure_left_height(self) -> int:
        """量左栏内容的实际总高（物理像素）。

        `CTkScrollableFrame` 的内容放在内部 canvas 里，得往里钻两层才拿到
        真正承载布局的那个 frame —— 直接量滚动容器本身只会得到它自己的
        可视高度，量不出内容高度（`winfo_reqheight()` 也正因为这个不可用）。
        """
        col = None
        for c in self.root.grid_slaves():
            if c.grid_info().get("column") == 0:
                col = c
                break
        if col is None:
            return 0
        container = col
        for child in col.winfo_children():
            for grand in child.winfo_children():
                if grand.grid_slaves():
                    container = grand
                    break
        total = 0
        for g in container.grid_slaves():
            total = max(total, g.winfo_y() + g.winfo_height())
        return total

    def _adopt_data(self) -> None:
        """首次启动时接管源码版攒下的题库（见 core.adopt_existing_data）。

        放后台线程跑：要读磁盘、找上层目录，别卡住窗口显示。
        """
        def job() -> None:
            try:
                self.core.adopt_existing_data()
            except Exception as exc:  # noqa: BLE001 - 迁移失败不该影响启动
                self.logger(f"[data] 接管既有数据时出错（忽略）: {exc}")

        threading.Thread(target=job, daemon=True).start()

    # ================= 构建界面 =================

    def _build(self) -> None:
        root = self.root
        # 列宽分配：**前三栏写死（各占四分之一），最右的右栏吃掉所有富余**。
        #
        # 这一行试过四种写法：
        #   左0右1 → 窗口变宽时富余全进右栏，左栏还是老宽 —— **这个是对的**，
        #            右栏（日志/调试视图）本来就该越宽越好、能多显几个字。
        #   左1右0 → 反过来，左栏把富余全吞了（实测左栏涨到 932 物理px），
        #            右栏被挤到窗口外、整块看不见。
        #   两边都 weight=0 → 两边都不长，窗口右侧留一大块**死空白**
        #            （实测右栏到 x=837 就没了，右边空 495 物理px）。
        # 所以前三栏 weight=0、最后一栏 weight=1。
        #
        # 列宽**写死常量，不去量**：量出来的值会反过来跟着
        # `wraplength` 变（`wraplength` 把 label 的 requested 宽钉死在折行
        # 宽度上），量到 646、给足 646 之后渲染出来却只有 590 —— 是个循环，
        # 越调越糊涂。干脆反过来：**先定列宽，再由列宽推 wraplength**。
        #
        # 四栏从左到右：任务/按钮 │ 设置面板 │ 运行日志 │ 调试画面。
        # 设置面板放在**日志左边**是用户点名要的位置 —— 点左栏那些 ⚙
        # 之后，内容就出现在紧挨着的这一栏里，视线不用跳。
        #
        # ★ `minsize` 要乘 `_dpi_scale()`：它的单位是设备像素，而常量是
        # 逻辑px。不乘的后果实测过 —— 四栏宽度全错，见 `_dpi_scale`。
        s = self._dpi_scale()
        root.grid_columnconfigure(0, weight=0, minsize=int(LEFT_COL_W * s))
        root.grid_columnconfigure(1, weight=0, minsize=int(SETTINGS_COL_W * s))
        root.grid_columnconfigure(2, weight=1, minsize=int(RIGHT_COL_W * s))
        root.grid_rowconfigure(0, weight=1)

        self._build_left()
        self._build_settings_panel()
        self._build_right()

    # ---------- 左栏 ----------

    def _build_left(self) -> None:
        # **左栏必须是可滚动的**（踩过）。
        #
        # 内容请求高 1088px，而默认窗口只有 720px；原先用普通 CTkFrame +
        # grid 装，放不下的部分会被**直接挤出可视区且无法触达** ——
        # 「调试视图」按钮看不到就是这个原因。实测各块高度：
        #    标题 87 + 环境状态卡 314 + 任务卡 438 + 动作 135 + 工具行 45
        # 状态卡里每个状态点就占 42px；任务卡每行 78px（开关 36 + 两行描述 42）。
        #
        # 换 CTkScrollableFrame 后：窗口够高就全显示，窗口小了可以滚，
        # 任何屏幕尺寸下都不会有「够不到的控件」。
        col = ctk.CTkScrollableFrame(
            self.root, fg_color="transparent", corner_radius=0,
            scrollbar_button_color=COL_BORDER,
            scrollbar_button_hover_color=COL_CARD_HI,
        )
        col.grid(row=0, column=0, sticky="nsew", padx=(14, 7), pady=14)
        col.grid_columnconfigure(0, weight=1)
        #: 供 `_measure_left_width()` 量内容用
        self._left_scroll = col

        # 标题
        #
        # 标题和副标题**并在同一行**：原来竖排两行占 87px，左栏本来就紧张
        # （见 _fit_window 的说明），这里省下约 50px。
        head = ctk.CTkFrame(col, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            head, text="继续教育助手",
            font=ctk.CTkFont(size=17, weight="bold"), text_color=COL_TEXT,
        ).pack(side="left")
        ctk.CTkLabel(
            head, text=f"中山医院远程教育",
            font=ctk.CTkFont(size=11), text_color=COL_TEXT_DIM,
        ).pack(side="left", padx=(8, 0), pady=(4, 0))

        # 环境状态卡
        self._build_status_card(col, row=1)

        # 任务开关
        self._build_task_card(col, row=2)

        # 动作按钮
        self._build_actions(col, row=3)

        # 底部工具
        tools = ctk.CTkFrame(col, fg_color="transparent")
        tools.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        # 调试视图按钮**并入底部工具行**，不再单独占一行。
        #
        # 原先它是 actions 卡里独立的第 3 行（高度 32 + 上间距 8），而左栏是
        # 普通 grid、**不滚动**：内容请求高 1190px 远超默认窗口高 720px，
        # 底部整行连同这个按钮一起被挤出可视区 —— 表现就是「按钮找不到」。
        # 实测数据（root.update_idletasks 后量 winfo_reqheight）：
        #   标题 87 + 状态卡 314 + 任务卡 438 + 动作 195 + 工具 45 + 间距
        # 合并进工具行后省掉一整行，同时把工具行做成 3 列。
        tools.grid_columnconfigure((0, 1, 2), weight=1)
        ctk.CTkButton(
            tools, text="数据目录", height=30,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11),
            command=self._open_data,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ctk.CTkButton(
            tools, text="日志目录", height=30,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11),
            command=self._open_logs,
        ).grid(row=0, column=1, sticky="ew", padx=3)
        # 排查「识别错误」时用它：实时画面 + OCR 框 + 判定叠加。
        ctk.CTkButton(
            tools, text="🔍 调试视图", height=30,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11),
            command=self.on_debug_view,
        ).grid(row=0, column=2, sticky="ew", padx=(3, 0))

    def _build_status_card(self, parent, row: int) -> None:
        card = ctk.CTkFrame(parent, fg_color=COL_CARD, corner_radius=10,
                            border_width=1, border_color=COL_BORDER)
        card.grid(row=row, column=0, sticky="ew", pady=(14, 0))
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            card, text="环境状态", height=20,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COL_TEXT_DIM,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 2))

        # 四项状态，检查环境时就地更新
        items = (
            ("adb", "adb 可执行文件"),
            ("config", "配置"),
            ("conn", "模拟器连接"),
            ("res", "资源与 OCR 模型"),
        )
        for i, (key, label) in enumerate(items, start=1):
            # 「○」这个字形本身撑高：size=14 时**单个 Label 就占 42px**（实测），
            # 4 个共 168px —— 是状态卡 314px 的主要来源。降到 11 并去掉
            # 纵向 padding，每个约 26px。
            dot = ctk.CTkLabel(card, text="○", width=16, height=17,
                               text_color=COL_TEXT_DIM,
                               font=ctk.CTkFont(size=11))
            dot.grid(row=i, column=0, sticky="w", padx=(14, 6), pady=0)
            ctk.CTkLabel(card, text=label, height=17, text_color=COL_TEXT,
                         font=ctk.CTkFont(size=12)).grid(row=i, column=1, sticky="w")
            self.status_dots[key] = dot

        self.lbl_status = ctk.CTkLabel(
            card, text="尚未检查", height=16, text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=11), anchor="w",
        )
        self.lbl_status.grid(row=len(items) + 1, column=0, columnspan=2,
                             sticky="ew", padx=14, pady=(4, 10))

    def _build_task_card(self, parent, row: int) -> None:
        card = ctk.CTkFrame(parent, fg_color=COL_CARD, corner_radius=10,
                            border_width=1, border_color=COL_BORDER)
        card.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        card.grid_columnconfigure(0, weight=1)

        # ---- 路线选择 ----
        #
        # 平台有两套前端：电脑版（`/`，普通浏览器就能开）和手机版
        # （`/mobile/`，只认微信 UA）。两条路是**完全独立的代码**：
        # 电脑版是另一个域名、原生 `<video>`、左侧讲次列表，坐标和 roi
        # 一条都不复用。所以这里让用户先选路线，再选这条路线上的任务。
        #
        # 默认给「浏览器版」：在模拟器里跑微信有被风控的风险，而且
        # 这台模拟器的 x86_64 媒体栈会让微信自己的解码线程反复崩
        # （见 DEVELOPMENT.md 7.3）。电脑版全程不碰微信。
        ctk.CTkLabel(
            card, text="用哪条路线", height=20,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COL_TEXT_DIM,
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        self.route_var = ctk.StringVar(value=self._route_label(DEFAULT_ROUTE))
        self.seg_route = ctk.CTkSegmentedButton(
            card,
            values=[label for _key, label, _desc in ROUTES],
            variable=self.route_var,
            command=self._on_route_change,
            height=32, font=ctk.CTkFont(size=12),
            selected_color=COL_ACCENT, selected_hover_color=COL_ACCENT_HI,
            unselected_color=COL_CARD_HI, unselected_hover_color=COL_BORDER,
            text_color="#ffffff",
        )
        self.seg_route.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 6))

        # 路线说明。
        #
        # 用**原生 `tk.Label`**，不是 `CTkLabel` —— 理由和下面任务描述那段
        # 完全一样（`CTkLabel` 会把高度锁在 42px，折成 4 行的说明第 4 行
        # 整行被吃掉，看起来像右边被截）。见下面那段长注释。
        self.lbl_route = tk.Label(
            card, text="", justify="left", anchor="w",
            # 405 - 40：四等分后左栏是 405 逻辑px，卡片左右各留 14。
            # 原来是 `LEFT_COL_W - 156`，那是给「描述右边还有个开关」留的
            # 位置；路线说明是整行独享的，减去 156 白白折掉一行。
            wraplength=LEFT_COL_W - 40,
            font=("", 10), bg=COL_CARD, fg=COL_TEXT_DIM,
            bd=0, highlightthickness=0,
        )
        self.lbl_route.grid(row=2, column=0, sticky="w", padx=14, pady=(0, 8))

        # ---- 任务开关 ----
        ctk.CTkLabel(
            card, text="要执行的任务", height=20,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COL_TEXT_DIM,
        ).grid(row=3, column=0, sticky="w", padx=14, pady=(2, 2))

        # 任务行放这个容器里，换路线时**整块重建**（见 `_render_tasks`）。
        self._task_rows = ctk.CTkFrame(card, fg_color="transparent")
        self._task_rows.grid(row=4, column=0, sticky="ew")
        self._task_rows.grid_columnconfigure(0, weight=1)

        self._update_route_hint()
        self._render_tasks()

    def _route_label(self, key: str) -> str:
        """路线键 → 界面上那个名字。"""
        for k, label, _desc in ROUTES:
            if k == key:
                return label
        return ROUTES[0][1]

    def route_key(self) -> str:
        """当前选中的路线键（`core.ROUTES` 里的第一个元素）。

        分段按钮给回来的是**显示名**，所以这里反查一遍。查不到就回默认 ——
        宁可跑默认路线，也不要因为一个改名让 `TASKS_BY_ROUTE[...]` 抛 KeyError
        把整个界面打崩。
        """
        label = self.route_var.get()
        for key, text, _desc in ROUTES:
            if text == label:
                return key
        return DEFAULT_ROUTE

    def _on_route_change(self, _value: str = "") -> None:
        self._update_route_hint()
        self._render_tasks()

    def _update_route_hint(self) -> None:
        key = self.route_key()
        desc = next((d for k, _t, d in ROUTES if k == key), "")
        self.lbl_route.configure(text=desc)

    def _render_tasks(self) -> None:
        """按当前路线重建任务开关。

        ## 为什么要整块重建，而不是建 4 行再改文案

        两条路线的任务**数量和键都不一样**（浏览器版 4 条、微信版 4 条，
        但键完全不同），只改文案的话 `self.switches` 里会残留上一条路线的键，
        而 `on_run()` 恰恰是**按 `self.switches` 收集**要跑哪些任务的 ——
        残留的键会被当成「用户勾了这一项」发下去，跑到一半才发现在点
        一个界面上根本不存在的任务。所以每次重建都把字典清空。

        ## 为什么每项下面的灰色小字删掉了

        用户 2026-10-08 明确要求（m18611）：「将选项下方的小字删除」。
        那些灰字是**静态说明**，每行占 40 多像素、四项就吃掉小半屏；
        而真正需要它的时候（第一次用）它又不够 —— 每项该填什么、填多少
        合适，一句话根本说不清。

        现在是**每项一个 ⚙**：点开是按 `scripts/taskspec.py` 那张声明表
        渲染出来的设置面板，带默认值、范围校验和完整的「为什么」。
        不点就一行，干净。改过设置的那项 ⚙ 会变蓝并带一个 `*`，
        一眼能看出「这项我调过」（这也是不写小字之后唯一的状态提示）。

        微信版那四个任务在表里没有可配置项，所以它们不出现 ⚙。
        """
        for w in self._task_rows.winfo_children():
            w.destroy()
        self.switches.clear()
        self.gears.clear()

        tasks = TASKS_BY_ROUTE[self.route_key()]
        for i, (key, title, desc, default, _slow) in enumerate(tasks):
            wrap = ctk.CTkFrame(self._task_rows, fg_color="transparent")
            wrap.grid(row=i, column=0, sticky="ew", padx=14, pady=(3, 0))
            wrap.grid_columnconfigure(0, weight=1)

            # 说明文字没有丢，它挪进了 `desc` → 悬停提示不好做（tk 原生
            # 没有 tooltip），所以放在 ⚙ 面板顶上显示。这里留个引用给
            # `on_task_settings` 用。
            self._task_desc[key] = desc

            sw = ctk.CTkSwitch(
                wrap, text=title, onvalue=True, offvalue=False, height=20,
                font=ctk.CTkFont(size=13), text_color=COL_TEXT,
                progress_color=COL_ACCENT, fg_color=COL_BORDER,
                button_color="#ffffff", button_hover_color="#e5e7eb",
            )
            sw.grid(row=0, column=0, sticky="w")
            if default:
                sw.select()
            self.switches[key] = sw

            if taskspec.has_options(key):
                gear = ctk.CTkButton(
                    wrap, text="⚙", width=30, height=22,
                    fg_color="transparent", hover_color=COL_BORDER,
                    text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=14),
                    border_width=0,
                    command=lambda k=key: self.on_task_settings(k),
                )
                # 靠右，和开关同一行 —— 原来这里是一整行灰字，现在压缩成
                # 一个 30px 的按钮，四行一共省下约 160px。
                gear.grid(row=0, column=1, sticky="e", padx=(6, 0))
                self.gears[key] = gear

        self._sync_gears()
        # 底部留白
        ctk.CTkLabel(self._task_rows, text="", height=2).grid(
            row=len(tasks), column=0)

    def _sync_gears(self) -> None:
        """按「这项的设置有没有被改过」刷新每个 ⚙ 的样子。

        `⚙` 变蓝带 `*` = 调过。全默认的项保持暗色 —— 界面上不出现任何
        多余文字（用户要求删掉小字），所以「调过没有」只能靠这点颜色说。
        """
        for key, btn in self.gears.items():
            try:
                vals = taskspec.values(key)
                changed = bool(taskspec.summary(key, vals))
            except Exception:  # noqa: BLE001 - 读不出来就当没改过
                changed = False
            btn.configure(
                text="⚙*" if changed else "⚙",
                text_color=COL_ACCENT if changed else COL_TEXT_DIM,
            )

    def on_task_settings(self, key: str) -> None:
        """把某个任务的 ⚙ 设置渲染进左数第二栏（日志左边那块）。

        存的是**整份值**（`taskspec.save` 会只写这个任务那几个键，
        其余键原样保留），所以面板里没碰过的项也会被写一遍 —— 那正好，
        等于把默认值显式落盘，用户之后在配置里能看见它们。
        """
        options = taskspec.options_for(key)
        if not options:
            return
        title = next((t for k, t, *_ in TASKS_BY_ROUTE[self.route_key()] if k == key),
                     key)
        try:
            vals = taskspec.values(key)
        except Exception as exc:  # noqa: BLE001
            self.logger(f"[ui] 读 {key} 的设置失败: {exc}")
            return

        self._render_settings(
            kind="task", key=key, title=f"{title} · 设置",
            intro=self._task_desc.get(key, ""),
            options=options, values=vals,
            on_save=lambda new: self._save_task_settings(key, new),
        )

    def _save_task_settings(self, key: str, vals: dict) -> None:
        try:
            taskspec.save(key, vals)
        except Exception as exc:  # noqa: BLE001
            self.logger(f"[ui] 保存 {key} 的设置失败: {exc}")
            return
        changed = [f"{o.label}={vals.get(o.key)}" for o in taskspec.options_for(key)
                   if vals.get(o.key) != o.default]
        self.logger(f"[ui] {key} 的设置已保存"
                    + ("（" + "、".join(changed) + "）" if changed else "（全默认）"))
        self._sync_gears()

    def on_global_settings(self) -> None:
        """把全局设置（算力、浏览器端口 …）渲染进设置栏。

        这些项在 `config.json` 里各有各的家（`inference` / `browser` …），
        **不搬进 `options` 节** —— 手工编辑配置的人和界面看到的是同一份值。

        ⚠ 用户原来要求「总设置删了」，后来又改口「我并不能点开设置按钮，
        你要不排查一下就先不删了」—— 点不开是两个真 bug（见
        `_SettingsForm` 的 docstring），不是这个按钮不该存在。所以**保留**。
        """
        try:
            vals = taskspec.global_values()
        except Exception as exc:  # noqa: BLE001
            self.logger(f"[ui] 读全局设置失败: {exc}")
            return
        self._render_settings(
            kind="global", key="", title="全局设置",
            intro="这些是所有任务共用的。改完**下次点「开始运行」时生效**。",
            options=[o for sec in taskspec.SETTINGS for o in sec.options],
            values=vals,
            on_save=self._save_global_settings,
        )

    def _save_global_settings(self, vals: dict) -> None:
        try:
            taskspec.save_global(vals)
        except Exception as exc:  # noqa: BLE001
            self.logger(f"[ui] 保存全局设置失败: {exc}")
            return
        self.logger("[ui] 全局设置已保存: " + "、".join(
            f"{k}={v}" for k, v in vals.items()))


    def _build_actions(self, parent, row: int) -> None:
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=row, column=0, sticky="ew", pady=(8, 0))
        box.grid_columnconfigure(0, weight=1)

        self.btn_check = ctk.CTkButton(
            box, text="检查环境", height=38,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT, font=ctk.CTkFont(size=13),
            command=self.on_check,
        )
        self.btn_check.grid(row=0, column=0, sticky="ew")

        # 全局设置（算力、浏览器端口 …）。
        #
        # 为什么放在「检查环境」旁边、而不是做一排菜单：**它和「检查环境」是
        # 同一类东西** —— 都是「跑之前先看一眼/调一下」。做成菜单项的话，
        # 用户得先知道它藏在哪儿；放在这儿一眼能看见，而且不占额外行高
        # （左栏每多一行，窗口就得再高一点，见 WIN_TARGET_H 那段注释）。
        #
        # 任务自己的设置不在这里，在**每个任务行右边的 ⚙** 上 —— 一个任务
        # 的可调项只对那个任务有意义，凑在一个全局面板里反而看不出归属。
        self.btn_settings = ctk.CTkButton(
            box, text="⚙", width=44, height=38,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT, font=ctk.CTkFont(size=15),
            command=self.on_global_settings,
        )
        self.btn_settings.grid(row=0, column=1, sticky="e", padx=(8, 0))

        # 「浏览器登录（电脑模式）」。
        #
        # 为什么要有这个按钮：平台有两套前端 —— 电脑版（`/`）普通浏览器就能
        # 开，手机版（`/mobile/`）才只认微信。所以在模拟器的浏览器里把 UA
        # 改成桌面 UA，就能**完全不开微信**地看同一批课，既没有封号风险，
        # 也躲开了模拟器上那个反复崩的微信解码器。
        #
        # 这件事手工做要好几步（带调试端口启浏览器 → adb forward → CDP 改 UA
        # → 导航 → 填手机号），而且顺序错了 SPA 会卡死，所以固化成一个按钮。
        #
        # 选「浏览器版」路线时**必须先点它一次**：登录态留在那个浏览器里，
        # 之后看课/考核都不用再登（而且是 httponly cookie，程序自己也读不到）。
        self.btn_login = ctk.CTkButton(
            box, text="🌐  浏览器登录（电脑模式）", height=34,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT, font=ctk.CTkFont(size=12),
            command=self.on_phone_login,
        )
        self.btn_login.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        # **开始/停止合成同一个按钮**。
        #
        # 原先「停止」单独放在右栏工具栏，用户实测反馈「没有停止运行按钮」——
        # 它离「开始运行」很远，而且样式几乎隐形，需要停止时根本找不到。
        # 现在：就放在「开始运行」的位置，点击开始后**原地变成红色的停止按钮**。
        # 位置固定、颜色反差大，不用去找。
        self.btn_run = ctk.CTkButton(
            box, text="▶  开始运行", height=44,
            fg_color=COL_ACCENT, hover_color=COL_ACCENT_HI,
            text_color="#ffffff", font=ctk.CTkFont(size=15, weight="bold"),
            command=self.on_run_or_stop,
        )
        self.btn_run.grid(row=2, column=0, sticky="ew", pady=(8, 0))

    # ---------- 右栏 ----------

    def _build_settings_panel(self) -> None:
        """日志左边那一栏：设置。

        用户要求的原话是「在日志 UI 的左边加入设置的部分，在用户没点击设置
        时显示『点击左边的设置以设置选项』」。所以这一栏**常驻**：

        * 空态是一句提示（不是一块空白 —— 新用户看着空白不知道这儿能干嘛）；
        * 点左栏任务右边的 ⚙、或顶部那颗 ⚙，内容**就地**换成对应设置；
        * 底部固定「恢复默认 / 保存」，它们**不跟着滚动**，滚了一屏设置
          还够得着。

        位置按用户要求放在日志**左边**：点完 ⚙ 内容就出现在紧挨着的那一栏，
        视线不用横跨整个窗口。
        """
        col = ctk.CTkFrame(self.root, fg_color=COL_CARD, corner_radius=10,
                           border_width=1, border_color=COL_BORDER)
        col.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=14)
        col.grid_columnconfigure(0, weight=1)
        # 只有滚动区（row=2）跟着长高，其余行按内容高度。
        col.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(col, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 8))
        head.grid_columnconfigure(0, weight=1)
        self.lbl_set_title = ctk.CTkLabel(
            head, text="设置", anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=COL_TEXT)
        self.lbl_set_title.grid(row=0, column=0, sticky="w")
        # 关掉当前这份设置回到空态。用 `✕` 而不是「返回」，因为它关的
        # 不是窗口、只是这一栏的内容 —— 写「返回」会让人以为要离开界面。
        self.btn_set_close = ctk.CTkButton(
            head, text="✕", width=26, height=24, fg_color="transparent",
            hover_color=COL_BORDER, text_color=COL_TEXT_DIM,
            font=ctk.CTkFont(size=12),
            command=self._show_settings_placeholder)
        self.btn_set_close.grid(row=0, column=1, sticky="e")

        ctk.CTkFrame(col, height=1, fg_color=COL_BORDER).grid(
            row=1, column=0, sticky="ew", padx=14)

        self._set_body = ctk.CTkScrollableFrame(col, fg_color="transparent")
        self._set_body.grid(row=2, column=0, sticky="nsew",
                            padx=(8, 2), pady=(10, 6))
        self._set_body.grid_columnconfigure(0, weight=1)

        # 出错提示固定在底部，不跟着滚 —— 校验失败时不管滚到哪儿都看得见。
        self.lbl_set_err = ctk.CTkLabel(
            col, text="", anchor="w", justify="left",
            wraplength=SETTINGS_COL_W - 44, font=ctk.CTkFont(size=11),
            text_color=COL_ERR)
        self.lbl_set_err.grid(row=3, column=0, sticky="ew", padx=14)

        bar = ctk.CTkFrame(col, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=14, pady=(8, 12))
        bar.grid_columnconfigure(0, weight=1)
        bar.grid_columnconfigure(1, weight=1)
        self.btn_set_reset = ctk.CTkButton(
            bar, text="恢复默认", height=32, fg_color="transparent",
            hover_color=COL_BORDER, border_width=1, border_color=COL_BORDER,
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=12),
            command=self._reset_settings)
        self.btn_set_reset.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_set_save = ctk.CTkButton(
            bar, text="保存", height=32, fg_color=COL_ACCENT,
            hover_color=COL_ACCENT_HI, text_color="#ffffff",
            font=ctk.CTkFont(size=13),
            command=self._save_settings)
        self.btn_set_save.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        #: 现在显示的是哪一份设置（`""` = 空态）。保存时靠它决定往哪写。
        self._set_kind = ""
        #: 当前表单。空态时是 None。
        self._set_form = None
        #: 保存成功后要调的回调（写盘那一步，由 on_task_settings /
        #: on_global_settings 各自给）。
        self._set_on_save = None

        self._show_settings_placeholder()

    # -- 设置面板：三种状态 -------------------------------------------------

    def _clear_settings_body(self) -> None:
        """清空滚动区并复位状态。`destroy()` 会连带把 Tk 变量一起收掉。"""
        for child in self._set_body.winfo_children():
            child.destroy()
        self._set_form = None
        self._set_kind = ""
        self._set_on_save = None
        self.lbl_set_err.configure(text="")

    def _show_settings_placeholder(self) -> None:
        """空态：一句提示 + 灰掉的按钮。

        用户点名要这句话：「在用户没点击设置时显示『点击左边的设置以
        设置选项』」。按钮也一并置灰 —— 空态下点「保存」没有任何东西可存，
        留着能点只会让人怀疑自己是不是漏了什么。
        """
        self._clear_settings_body()
        self.lbl_set_title.configure(text="设置")
        self._set_buttons_enabled(False)
        # 原生 `tk.Label`：要的是**居中**的两行灰字，`CTkLabel` 在高 DPI 下
        # 会把高度锁死在 42px，这里没折行、倒不至于被切，但居中要额外配
        # `justify`，原生 label 一次就够。`bg` 必须给卡片底色。
        tk.Label(self._set_body, text="点击左边的设置以设置选项",
                 justify="center", anchor="center",
                 wraplength=SETTINGS_COL_W - 80,
                 font=("", 11), bg=COL_CARD, fg=COL_TEXT_DIM,
                 bd=0, highlightthickness=0).pack(fill="x", pady=(70, 0))

    def _set_buttons_enabled(self, on: bool) -> None:
        state = "normal" if on else "disabled"
        self.btn_set_save.configure(state=state)
        self.btn_set_reset.configure(state=state)

    def _render_settings(self, *, kind: str, key: str, title: str, intro: str,
                         options, values: dict, on_save) -> None:
        """把一份设置渲染进面板。⚙ 和顶部的全局设置都走这里。"""
        self._clear_settings_body()
        self._set_kind = kind
        self._set_key = key
        self._set_on_save = on_save
        self.lbl_set_title.configure(text=title)
        self._set_buttons_enabled(True)

        width = SETTINGS_COL_W - 30
        if intro:
            # intro 是任务卡上那句说明（`_task_desc`）。它原来显示在任务名
            # 下面，用户要求删掉那些灰字，于是挪到这儿 —— 想了解的任务再点
            # 开看，不占左栏的地方。
            _wrapped(self._set_body, intro, width - 10,
                     COL_CARD, COL_TEXT_DIM).pack(fill="x", pady=(0, 12))

        self._set_form = _SettingsForm(self._set_body, options=options,
                                       values=values, width=width)
        self._set_form.attach_error(self.lbl_set_err)

    def _reset_settings(self) -> None:
        if self._set_form is not None:
            self._set_form.reset()

    def _save_settings(self) -> None:
        """校验 → 写盘回调 → 刷新 ⚙ 标记。

        校验失败就**留在面板里**把原因写在底部红字上：静默回退到默认值等于
        用户以为自己改了、其实没改，下次跑出来还得再查一遍。
        """
        if self._set_form is None or self._set_on_save is None:
            return
        out, err = self._set_form.collect()
        if err:
            self.lbl_set_err.configure(text=err)
            return
        self.lbl_set_err.configure(text="")
        self._set_on_save(out)
        self._sync_gears()

    def _build_right(self) -> None:
        col = ctk.CTkFrame(self.root, fg_color=COL_CARD, corner_radius=10,
                           border_width=1, border_color=COL_BORDER)
        # column=2 —— 设置面板插在左栏和它之间（见 `_build` 的列说明）。
        col.grid(row=0, column=2, sticky="nsew", padx=(7, 14), pady=14)
        col.grid_columnconfigure(0, weight=1)
        col.grid_rowconfigure(0, weight=1)

        self._right_col = col
        # 右栏横向排开：**左半边运行日志、右半边调试画面，两个同时可见**。
        #
        # ## 为什么不是「互斥切换」
        # 我先做成了「点按钮在日志↔调试之间切」，用户纠正：
        # 「我的意思是说那一块左半边日志，右半边视图」—— 他要的是**同时**
        # 看到日志和画面。互斥切换时看画面就没日志，跑任务时反而不好用。
        #
        # ## 宽度分配：日志让位，调试面板保底
        # `column 0`（日志）`weight=1` 吃掉富余，`column 1`（调试）拿
        # `minsize=DEBUG_COL_W` 保底。用户自己拉宽窗口时富余全进日志区 ——
        # 日志是等宽字体，多一行字就多一分用；画面再宽也没意义（9:16 竖屏，
        # 宽度本来就有富余）。
        # `minsize` 同样要乘 `_dpi_scale()`（单位是设备像素，见那里的说明）：
        # 不乘的话实测调试列只剩 270 逻辑px，日志会把富余全吃掉。
        s = self._dpi_scale()
        col.grid_columnconfigure(0, weight=1, minsize=int(LOG_COL_W * s))
        col.grid_columnconfigure(1, weight=0, minsize=int(DEBUG_COL_W * s))
        col.grid_rowconfigure(0, weight=1)

        self._log_view = ctk.CTkFrame(col, fg_color="transparent")
        self._log_view.grid(row=0, column=0, sticky="nsew")
        self._log_view.grid_columnconfigure(0, weight=1)
        self._log_view.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(self._log_view, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        bar.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            bar, text="运行日志",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COL_TEXT_DIM,
        ).grid(row=0, column=0, sticky="w")

        self.lbl_state = ctk.CTkLabel(
            bar, text="就绪", text_color=COL_OK,
            font=ctk.CTkFont(size=12), anchor="e",
        )
        self.lbl_state.grid(row=0, column=2, sticky="e", padx=(0, 8))

        # 停止按钮**不在这里**——它和「开始运行」是同一个按钮（左栏），
        # 运行时原地变成红色的「■ 停止」。见 _build_left / _set_busy。

        ctk.CTkButton(
            bar, text="复制", width=58, height=26,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11),
            command=self.on_copy,
        ).grid(row=0, column=4, padx=(0, 6))
        ctk.CTkButton(
            bar, text="清空", width=58, height=26,
            fg_color=COL_CARD_HI, hover_color=COL_BORDER,
            text_color=COL_TEXT_DIM, font=ctk.CTkFont(size=11),
            command=self.on_clear,
        ).grid(row=0, column=5)

        self.txt = ctk.CTkTextbox(
            self._log_view, wrap="word", corner_radius=8,
            fg_color=COL_CARD_HI, border_width=0,
            text_color=COL_TEXT,
            font=ctk.CTkFont(family="Consolas", size=11),
            scrollbar_button_color=COL_BORDER,
            scrollbar_button_hover_color=COL_TEXT_DIM,
        )
        self.txt.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.txt.configure(state="disabled")

        # 日志级别配色
        self.txt.tag_config("err", foreground=COL_ERR)
        self.txt.tag_config("warn", foreground=COL_WARN)
        self.txt.tag_config("ok", foreground=COL_OK)
        self.txt.tag_config("head", foreground=COL_ACCENT)
        self.txt.tag_config("task", foreground="#a78bfa")
        self.txt.tag_config("plain", foreground=COL_TEXT)

        # 右半边：调试画面。**和日志同时显示**，不是切换。
        self._build_debug_view()

    # ---------- 调试视图（内嵌右栏右半边，与日志并排） ----------

    def _build_debug_view(self) -> None:
        """建右侧的调试面板。

        **和日志并排同时显示**（用户要求「左半边日志，右半边视图」）。

        代价说清楚：它一建起来就会起采集线程、载入 OCR 模型（实测约
        10 秒），并且此后一直占着 adb 约 3 秒抓一帧。想省掉这份开销的话，
        把 `_build_right()` 末尾那行 `self._build_debug_view()` 注释掉即可
        —— 面板本身是独立控件，不建就不占资源。
        """
        self._debug_view = ctk.CTkFrame(self._right_col,
                                        fg_color="transparent")
        self._debug_view.grid(row=0, column=1, sticky="nsew")
        self._debug_panel = None
        try:
            import debug_view
        except Exception as exc:  # noqa: BLE001 - 缺依赖也要说清楚
            # 缺依赖时**不隐藏这一格**（并排布局要两边都在），改成在这里
            # 写一句为什么没有画面 —— 藏起来的话用户只看到半截空白，
            # 不知道是坏了还是在加载。
            msg = f"打不开调试画面（缺模块）\n{type(exc).__name__}: {exc}"
            self.logger(f"[ui] {msg.splitlines()[0]}: "
                        f"{type(exc).__name__}: {exc}")
            ctk.CTkLabel(
                self._debug_view, text=msg, justify="left", anchor="w",
                wraplength=DEBUG_COL_W - 40,
                text_color=COL_ERR, font=ctk.CTkFont(size=11),
            ).grid(row=0, column=0, sticky="nw", padx=14, pady=14)
            return
        self._debug_panel = debug_view.DebugPanel(self._debug_view,
                                                  standalone=False)
        self.logger("[ui] 调试画面在右栏右半边，和日志同时显示")

    def on_debug_view(self) -> None:
        """「调试视图」按钮：把焦点移到右边的调试画面。

        早先这里是「在日志↔调试之间切换」甚至「开一个独立窗口」，
        用户都不要 —— 他要的是两个**同时**可见，所以现在只做聚焦，
        不隐藏任何东西。命令行的独立窗口仍保留：
        `ZSCMEAutopilot.exe --run debug_view`。
        """
        if self._debug_panel is None:
            self.logger("[ui] 调试画面没建起来（见上面那条错误信息）")
            return
        self.logger("[ui] 调试画面就在右栏右半边，和日志并排显示")

    # ================= 日志 =================

    def _pump_log(self) -> None:
        """主线程轮询队列，批量刷新（避免每条都触发一次重绘）。"""
        lines: list[str] = []
        try:
            while True:
                lines.append(self.log_q.get_nowait())
        except queue.Empty:
            pass

        if lines:
            self.txt.configure(state="normal")
            for line in lines:
                self.txt.insert("end", line + "\n", _line_tag(line))
            self.txt.see("end")
            self.txt.configure(state="disabled")

        self.root.after(120, self._pump_log)

    def _greet(self) -> None:
        # 注意别用空格去对齐中文——中文是双宽，用 len() 估算必然错位
        # （实测过：说明文字被推到很远）。一条一行最稳。
        for line in (
            "=" * 62,
            f" {APP_TITLE} v{APP_VER}",
            "=" * 62,
            paths.describe(),
            "",
            "提示：先点左侧「检查环境」，确认 adb / 模拟器 / 资源都就绪，再「开始运行」。",
            "",
            "需要跑单个工具（完整卷、采集答案、补全未答、题库迁移等）用命令行：",
            "    ZSCMEAutopilot.exe --list",
            "    ZSCMEAutopilot.exe --run run_full_exam",
            "    ZSCMEAutopilot.exe --run harvest_answers",
            "",
        ):
            self.logger(line)

    # ================= 状态卡 =================

    def _set_dot(self, key: str, state: str) -> None:
        """state: idle / ok / err"""
        dot = self.status_dots.get(key)
        if dot is None:
            return
        glyph, color = {
            "idle": ("○", COL_TEXT_DIM),
            "ok": ("●", COL_OK),
            "err": ("●", COL_ERR),
            "busy": ("◐", COL_WARN),
        }.get(state, ("○", COL_TEXT_DIM))
        dot.configure(text=glyph, text_color=color)

    def _reset_dots(self) -> None:
        for key in self.status_dots:
            self._set_dot(key, "idle")

    # ================= 动作 =================

    def on_phone_login(self) -> None:
        """「浏览器登录（电脑模式）」按钮。

        做四件事（都在后台线程里，界面不卡）：
          1. 让设备上的浏览器带着**调试端口**起来，并把端口 forward 到本机
          2. 用 CDP 把 UA 覆盖成桌面 UA —— **必须在导航之前**，顺序反了 SPA 卡死
          3. 导航到桌面版的验证码登录页
          4. 如果配置里写了手机号，顺手填进去并点「获取验证码」

        剩下的最后一步（填 6 位短信码 → 点「立即登录」）交给用户：
        短信在用户手机上，程序读不到也不该读。
        """
        self._start_worker(self._phone_login_job, "浏览器登录")

    def _phone_login_job(self) -> None:
        import browser

        phone, port = browser.config_settings()
        if not phone:
            self.logger("[browser] 提示：data/config.json 里 browser.phone 是空的，"
                        "所以只把登录页打开，手机号要你自己输入。"
                        "填上手机号以后这一步会自动填号并发验证码。")
        want = "获取验证码" if phone else ""
        ok = browser.phone_login(phone=phone, want=want, port=port, log=self.logger)
        self.logger("")
        if ok:
            self.logger("[browser] 接下来（在模拟器/手机的浏览器里）：")
            self.logger("[browser]   1. 手机上会收到短信验证码")
            self.logger("[browser]   2. 把 6 位数字填进「输入验证码」")
            self.logger("[browser]   3. 点「立即登录」")
            self.logger("[browser] 登录状态会留在浏览器里，之后程序不用再管登录。")
            self._ok("登录页已就绪")
        else:
            self.logger("[browser] ✗ 没能打开登录页")
            self._fail("桌面版登录页没打开")

    def _set_busy(self, busy: bool, label: str = "运行中…") -> None:
        """切换「空闲 ↔ 运行中」的界面状态。

        「开始/停止」是**同一个按钮**，这里负责换它的文案和颜色：
          空闲 → 蓝色「▶ 开始运行」
          运行 → 红色「■ 立即停止」

        文案写「立即」是有意的，两条路线都兑现了：
          * 浏览器版：看护循环每一圈开头都查一次（`desktop.should_stop()`），
            而且那一圈里没有不可中断的等待（视频在浏览器那边自己播），
            所以最迟十几秒就停手；
          * 微信版：`core.stop()` 调 `tasker.post_stop()`，看护循环自己查
            `tasker.stopping`（见 `main.py` 的 `_stopper()`），正在看的那一节
            也会当场停手，不用等节点跑完。
        """
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_check.configure(state=state)
        self.btn_login.configure(state=state)
        self.btn_settings.configure(state=state)
        # 每个任务的 ⚙ 也一起置灰。设的是**下一次**跑的参数，跑到一半改
        # 对已经在跑的那个 worker 毫无影响（argv 在开跑前就取走了），
        # 留着能点只会让人以为「改了马上生效」。和 seg_route 同一个理由。
        for gear in self.gears.values():
            try:
                gear.configure(state=state)
            except tk.TclError:
                pass
        # 路线也一起置灰：跑到一半换路线只是把下面的勾选框重建一遍，
        # 对已经在跑的那个 worker 毫无影响（`keys` 早就取走了），
        # 留着能点只会让人以为「换过去就换任务了」。
        self.seg_route.configure(state=state)
        if busy:
            self.btn_run.configure(
                text="■  立即停止", fg_color=COL_ERR, hover_color="#b91c1c",
                command=self.on_run_or_stop,
            )
        else:
            self.btn_run.configure(
                text="▶  开始运行", fg_color=COL_ACCENT,
                hover_color=COL_ACCENT_HI, command=self.on_run_or_stop,
            )
        self.lbl_state.configure(text=label if busy else "就绪",
                                 text_color=COL_WARN if busy else COL_OK)

    def _start_worker(self, fn, name: str) -> None:
        if self.worker and self.worker.is_alive():
            self.logger("[warn] 已有任务在运行，请先停止。")
            return
        self.core.begin()
        self._set_busy(True)

        def wrapper() -> None:
            try:
                fn()
            except Exception as exc:  # 后台线程异常必须回传，否则界面像卡死
                self.logger("")
                self.logger(f"[ERROR] {name} 失败: {exc}")
                for ln in traceback.format_exc().split("\n"):
                    self.logger(ln)
            finally:
                self.log_q.put("__DONE__")

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()
        self.root.after(200, self._watch_done)

    def _watch_done(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(300, self._watch_done)
            return
        self._set_busy(False)

    def on_check(self) -> None:
        self._reset_dots()

        def job() -> None:
            from controller import ConfigError, build_controller, load_config

            # 逐项更新状态灯：把 core 的日志当信号源太脆弱，
            # 所以这里直接分步调用，每步自己点灯。
            self._set_dot("adb", "busy")
            try:
                import detect

                detect.find_adb(log=self.logger)
                self._set_dot("adb", "ok")
            except (FileNotFoundError, OSError) as exc:
                self.logger(f"[1/4] adb 探测失败: {exc}")
                self._set_dot("adb", "err")
                self._fail("adb 未找到")
                return

            self._set_dot("config", "busy")
            try:
                cfg = load_config()
                self._set_dot("config", "ok")
            except ConfigError as exc:
                self.logger(f"[2/4] 配置读取失败: {exc}")
                self._set_dot("config", "err")
                self._fail("配置有问题")
                return

            self._set_dot("conn", "busy")
            try:
                controller = build_controller(cfg, log=self.logger)
            except (ConfigError, RuntimeError) as exc:
                self.logger(f"[3/4] 连接失败: {exc}")
                self._set_dot("conn", "err")
                self._fail("连不上模拟器")
                return
            self._set_dot("conn", "ok")

            self._set_dot("res", "busy")
            try:
                from maa.resource import Resource

                resource = Resource()
                if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
                    raise RuntimeError("管线资源加载失败")
                if not resource.post_ocr_model(
                    str(paths.ocr_model_dir())
                ).wait().succeeded:
                    raise RuntimeError("OCR 模型加载失败")
                self.logger(f"[4/4] ✓ {len(resource.node_list)} 个管线节点，OCR 就绪")
                self._set_dot("res", "ok")
            except Exception as exc:  # noqa: BLE001
                self.logger(f"[4/4] 资源加载失败: {exc}")
                self._set_dot("res", "err")
                self._fail("资源有问题")
                return

            self._ok("环境检查通过")

        self._start_worker(job, "环境检查")

    def _fail(self, msg: str) -> None:
        self.root.after(0, lambda: self.lbl_status.configure(
            text=f"✗ {msg}", text_color=COL_ERR))

    def _ok(self, msg: str) -> None:
        self.root.after(0, lambda: self.lbl_status.configure(
            text=f"✓ {msg}", text_color=COL_OK))

    def on_run(self) -> None:
        keys = [k for k, sw in self.switches.items() if sw.get()]
        if not keys:
            self.logger("[warn] 请至少打开一个任务。")
            return

        # 按路线分派。两条路线的任务键**没有交集**（浏览器版一律 `d_` 开头，
        # 微信版是 `course`/`checkin`/`exam`/`watch`），所以看键名就够了，
        # 不必再问一次分段按钮 —— 也就不存在「按钮显示的和实际跑的不是一条
        # 路线」这种错位。
        if any(k.startswith("d_") for k in keys):
            self._run_desktop(keys)
            return

        if "watch" in keys and "course" in keys:
            self.logger("[warn] 同时选了「整门课轮播」和「只看护当前视频」："
                        "轮播会自己切课，之后看护的将是那时正在播的一课。")
        self._start_worker(lambda: self.core.run_tasks(keys), "运行任务")

    def _run_desktop(self, keys: list[str]) -> None:
        """跑浏览器版（电脑模式）的任务。

        ## 为什么不走 `core.run_tasks()`

        那一条是微信版的整条流水线（建 `Controller`/`Resource`/`Tasker`、
        确认 `com.tencent.mm` 活着、检查屏幕方向），浏览器版一个都不需要 ——
        它只跟 CDP 说话。详见 `scripts/desktop_runner.py` 的 docstring。

        ## 跑之前得先在浏览器里登过一次

        「▶ 开始运行」用的浏览器和「🌐 浏览器登录（电脑模式）」是**同一个**
        （同一个调试端口、同一个 profile）。登录态是平台发的 httponly cookie，
        程序自己读不到，也没法代填（要短信验证码）。所以这里只提醒一句，
        不去碰登录页 —— 这是用户明确要求的。
        """
        self.logger("[ui] 路线：浏览器版（电脑模式）")
        # `should_stop` 直接读 `core.stopped`：那个标记由 `core.stop()` 置位，
        # 而 `stop()` 在没有 tasker 时是安全的（浏览器版本来就没有 tasker）。
        # 装上之后 `desktop.should_stop()` 在**每一层**都能问到 —— 包括
        # 几小时那种「整门课看护」循环的最里面一圈。
        self._start_worker(lambda: desktop_runner.run(
            keys, log=self.logger,
            should_stop=lambda: self.core.stopped,
        ), "运行任务")

    def on_run_or_stop(self) -> None:
        """主按钮的**唯一入口**：空闲时开始，运行中则停止。

        用户要求：停止按钮就放在开始按钮的位置，点开始之后原地变成停止按钮。
        这样「开始」和「停止」永远在同一个地方，不用去找。
        """
        if getattr(self, "_busy", False):
            self.on_stop()
        else:
            self.on_run()

    def on_stop(self) -> None:
        """请求停止，并立刻在界面上给出反馈。

        实测用户反馈「没有停止运行按钮」。原因有两层：
          * 它原先在右栏工具栏，离「开始运行」很远，不容易发现
          * 即使找到，`core.stop()` 原先只设了一个标记，而那个标记只在
            **任务之间**检查；「播放整门课」是**一个**跑几小时的节点，
            所以点了要等整门课跑完才生效 —— 等于停不下来

        现在两层都补上了：
          * 按钮和「开始运行」同位同体（见 `on_run_or_stop`）；
          * `core.stop()` 调 `tasker.post_stop()`，而且看护循环自己会查
            `tasker.stopping`（`main.py` 的 `_stopper()` 把它接进
            `WatchConfig` / `CourseConfig`），所以**正在看的那一节也会当场
            停手**，不必等节点跑完。见 progress.py 的 `_nap()`。
        """
        if not getattr(self, "_busy", False):
            self.logger("[ui] 当前没有任务在运行")
            return
        self.logger("[ui] 已发送停止请求…（当前这一步会当场停手）")
        self.core.stop()
        self.lbl_state.configure(text="正在停止…", text_color=COL_WARN)
        # 按钮暂时置灰防连点，3 秒后恢复（万一那一下没停住还能再点）
        self.btn_run.configure(state="disabled", text="正在停止…")
        self.root.after(3000, self._restore_stop_button)

    def _restore_stop_button(self) -> None:
        """停止按钮的可点状态恢复。

        注意只在**任务还在跑**时恢复。任务已经结束的话，
        `_watch_done` 会把按钮切回蓝色「开始运行」，
        这里再改就会把文案覆盖错。
        """
        if getattr(self, "_busy", False):
            self.btn_run.configure(state="normal", text="■  立即停止")

    def on_clear(self) -> None:
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")

    def on_copy(self) -> None:
        text = self.txt.get("1.0", "end").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.logger("[ui] 日志已复制到剪贴板")

    def _open_data(self) -> None:
        import os

        os.startfile(paths.app_root())  # type: ignore[attr-defined]

    def _open_logs(self) -> None:
        import os

        os.startfile(paths.log_dir())  # type: ignore[attr-defined]


def _enable_dpi_awareness() -> str:
    """让进程「DPI 感知」，**越早调用越好**。

    ## 为什么必须抢在第一位

    不感知的进程里，Win32 会把屏幕/工作区尺寸**虚拟化**成缩小后的值
    （本机 150% 缩放下：工作区报 1707x1019 而不是真实的 2560x1528），
    同时 `winfo_fpixels("1i")` 报 96 而不是 144 —— CTk 的 `ScalingTracker`
    据此把窗口缩放算成 **1.0**（真实应为 1.5），字体和控件都不放大。

    实测代价（同一台机器、同一份代码）：
      - 感知：窗口 2430x1350 物理（1620x900 逻辑），右栏 1919px，
        **左半边日志 1359px + 右半边调试画面 560px 并排显示**；
      - 不感知：窗口 1635x937 物理，`ScalingTracker=1.0`，
        右栏被挤到只剩日志，**右边那半调试画面整块看不见**。

    这个顺序错误藏了很久没被发现，因为 `main()` 里那句
    `SetProcessDpiAwareness(1)` 看起来「已经设了」—— 但它写在
    `ctk.CTk()` **之后**，而 CTk 在构造函数里就把 DPI 读完了，设了也白设。

    ## 还会被「进程已经建过窗口」挡住

    Windows 规定：进程一旦创建过窗口，就不再接受 DPI 感知级别的变更，
    `SetProcessDpiAwareness` 返回 `E_ACCESSDENIED`（**不抛异常**）。
    所以这里**读返回值**并如实上报 —— 返回非 0 说明没设上，窗口会小一圈。
    （`launcher.py` 在 import 阶段就设过一次，所以 exe 路径通常是为 0 的。）

    返回一句人话状态，供日志显示。
    """
    try:
        from ctypes import windll

        hr = windll.shcore.SetProcessDpiAwareness(1)  # 1 = SYSTEM_DPI_AWARE
        if hr == 0:
            return "已启用 DPI 感知"
        # E_ACCESSDENIED：**通常不是问题** —— `launcher.py` 在 import 阶段
        # 已经设过一次，那一次成功就够了。这里判「有没有设上」而不是
        # 只看 HRESULT，免得把已经正常的状态报成故障。
        if _dpi_now() >= 120:
            return f"DPI 感知已在更早处生效（{_dpi_now()} dpi）"
        return f"DPI 感知没设上（HRESULT={hr}），界面会偏小"
    except Exception as exc:  # noqa: BLE001 - 非 Windows 或权限不足时忽略
        return f"DPI 感知不可用（{type(exc).__name__}）"


def _dpi_now() -> int:
    """当前窗口的 DPI（96 = 100%，144 = 150%）。探测失败返回 0。"""
    try:
        from ctypes import windll

        return int(windll.user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    # **顺序要紧**：DPI 感知要在建窗口之前。见 `_enable_dpi_awareness`。
    dpi_state = _enable_dpi_awareness()
    ctk.set_appearance_mode("dark")
    try:
        ctk.set_default_color_theme("blue")
    except Exception:  # noqa: BLE001 - 主题缺失不该拦住启动
        pass

    root = ctk.CTk()
    app = App(root)
    app.logger(f"[ui] {dpi_state}（系统 DPI {_dpi_now()}，"
               f"窗口缩放 {root.winfo_fpixels('1i') / 96:.2f}×）")
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
