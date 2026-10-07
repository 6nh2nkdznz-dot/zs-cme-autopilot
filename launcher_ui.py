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
#: 定 480 是因为宽度得留够，**但这不是「描述被截」的原因** —— 那件事
#: 另有其因：`CTkLabel` 把高度锁在 42px、只装得下 3 行字，折成 4 行的
#: 描述第 4 行整行被吃掉（详见 `_build_task_card` 里的注释，那里换成了
#: 原生 `tk.Label`）。当时我误判成横向被截，把这里从 420 一路加到 480，
#: 其实加宽只是让折行数变少、把问题暂时盖住。
#:
#: 480 本身仍然合理：描述最宽那句折行后约需 470 逻辑px。
#:
#: 为什么不用 `_measure_left_width()` 动态量：`wraplength` 会把 label 的
#: requested 宽**钉死**在折行宽度上，量出来的值反过来跟着它变，是个循环 ——
#: 实测量到 646、按 646 给足，渲染出来左栏内容却只有 590，越调越糊涂。
#: 所以改成**先定列宽，再由列宽推 wraplength**（见 `_build_task_card`）。
LEFT_COL_W = 480

#: 右栏列宽下限（Tk 逻辑px）。右栏 `weight=1`，会吃掉窗口的所有富余宽度。
#:
#: 右栏内部又横分成**左半边日志 + 右半边调试画面**（用户要求
#: 「左半边日志，右半边视图」），所以下限 = 两者之和：
#:   - `LOG_COL_W = 460`：日志是等宽字体，11 号字下一行放得下约 55 个字符，
#:     够显示 `[course] 文件名旁是半蓝圈 → 上次没看完，这次重看: xxx` 这种行。
#:   - `DEBUG_COL_W = 560`：调试画面是 9:16 竖屏，宽度只需够「画面 + 明细栏」。
#:     画面按可用宽高折算出约 0.43 缩放（宽 310、高 552），明细栏 250，
#:     加内边距约 570 —— 560 是「再窄明细就要横向滚动」的临界值。
LOG_COL_W = 460
DEBUG_COL_W = 560
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
from core import TASKS, AppCore  # noqa: E402


def debug_view_cmd() -> tuple[list[str], str]:
    """拼出**独立**调试视图窗口的命令行，返回 `(命令, 工作目录)`。

    ## 它现在还用在哪

    主界面的「🔍 调试视图」按钮已经改成**内嵌**（不再开窗口），见
    `App.on_debug_view`。这个函数保留给命令行用法：
    `MaaElearning.exe --run debug_view` / `python scripts\\debug_view.py`，
    不开主界面时排查问题用。

    ## 为什么要单独抽成函数

    这个拼法**在打包后会变**，而且踩过一次坑，所以必须能脱离界面测试。

    * 源码运行：`sys.executable` 是真 python.exe → `python debug_view.py`
    * 打包运行：**`sys.executable` 就是 MaaElearning.exe 自己**，原来那句
      就变成了 `MaaElearning.exe <debug_view.py 路径>`。而 `launcher.py`
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

        root.title(f"{APP_TITLE} v{APP_VER}")
        root.configure(fg_color=COL_BG)

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
        # 屏幕是「缩放后像素」，而 geometry() 收的是「CTk 逻辑像素」，
        # 两者差一个缩放系数（本机 1.5）。实测：
        #     geometry("1080x621") → Win32 实测窗口 1642x988，winfo_height 932
        # 所以给 geometry() 的值必须是「物理像素 ÷ 缩放」。
        try:
            from customtkinter import ScalingTracker
            scale = ScalingTracker.get_window_scaling(self.root) or 1.0
        except Exception:  # noqa: BLE001 - 取不到缩放就按 1.0 算
            scale = 1.0

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
        # 宽度 = 左栏列宽 + 右栏下限，都是逻辑px（`geometry()` 的单位）。
        # 左栏别再套 `_measure_left_width()` 了，原因见 `LEFT_COL_W` 的说明。
        #
        # 右栏按 `* scale` 放大给：日志是**等宽字体**，逻辑 700px 在 1.5 倍
        # 缩放下只够显示 60 来个字符，日志里的路径就折行了（实测过）。
        # 给到物理当量 700 正好。窗口随后可被拉宽，右栏 `weight=1` 会跟着长。
        #
        # 上限 1560：右栏现在横分成「日志 460 + 调试 560」（`RIGHT_COL_W`），
        # 本身就要 1020；再加左栏 480 和边距约 1620。给到 1620 之后
        # 日志区实测 480 逻辑宽（约 55 个等宽字符），够用；再宽只是让日志
        # 更宽，边际收益不大，还会把窗口顶到 85% 屏宽。
        left_need = LEFT_COL_W
        right_need = RIGHT_COL_W * scale                # 物理像素当量
        w = int(max(1080, min(left_need + right_need, 1620)))
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
        # 列宽分配：**左栏写死，右栏吃掉所有富余**。
        #
        # 这一行试过四种写法：
        #   左0右1 → 窗口变宽时富余全进右栏，左栏还是老宽 —— **这个是对的**，
        #            右栏（日志/调试视图）本来就该越宽越好、能多显几个字。
        #   左1右0 → 反过来，左栏把富余全吞了（实测左栏涨到 932 物理px），
        #            右栏被挤到窗口外、整块看不见。
        #   两边都 weight=0 → 两边都不长，窗口右侧留一大块**死空白**
        #            （实测右栏到 x=837 就没了，右边空 495 物理px）。
        # 所以是 左0右1，且左栏必须 weight=0（否则又走回第二种）。
        #
        # 左栏宽度**写死 `LEFT_COL_W`，不去量**：量出来的值会反过来跟着
        # `wraplength` 变（`wraplength` 把 label 的 requested 宽钉死在折行
        # 宽度上），量到 646、给足 646 之后渲染出来却只有 590 —— 是个循环，
        # 越调越糊涂。干脆反过来：**先定列宽，再由列宽推 wraplength**。
        root.grid_columnconfigure(0, weight=0, minsize=LEFT_COL_W)
        root.grid_columnconfigure(1, weight=1, minsize=RIGHT_COL_W)
        root.grid_rowconfigure(0, weight=1)

        self._build_left()
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

        ctk.CTkLabel(
            card, text="要执行的任务", height=20,
            font=ctk.CTkFont(size=12, weight="bold"), text_color=COL_TEXT_DIM,
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        for i, (key, title, desc, default, _slow) in enumerate(TASKS, start=1):
            # 每行原来 78px（开关 38 + 两行描述 42 + 间距），4 行共 333px。
            # 描述字号 11→10、行距收紧后每行约 63px，省下约 60px。
            wrap = ctk.CTkFrame(card, fg_color="transparent")
            wrap.grid(row=i, column=0, sticky="ew", padx=14, pady=(2, 0))
            wrap.grid_columnconfigure(0, weight=1)

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

            # wraplength 由**列宽**推出来，别硬编码一个数：
            #   左栏列宽 LEFT_COL_W = 480（逻辑px）
            #   − `col.grid(padx=(14, 7))` = 21
            #   − 滚动框相对列的收窄（实测 480 → 424，收 56）
            #   − 卡片左右 padx 14×2 = 28
            #   − 描述左缩进 46（`padx=(46, 0)`，和开关文字对齐）
            #   − 右侧余量 5
            #   = 324，也就是 LEFT_COL_W − 156
            # 测试 `scripts/test_window_size.py` 的 `desc_avail()` 会把这些项
            # 逐条减一遍，**别再手算**（我手算过两次，两次都错：先漏了
            # `col.grid` 的 padx，又把手写的 56 和 padx 重复扣了一遍）。
            #
            # 描述用**原生 `tk.Label`**，不能用 `CTkLabel`。
            #
            # 这是「描述右边被截」的真正原因，也是我绕最久的弯。
            # 症状看着像**水平方向**被卡片右边切掉几个字（用户看到
            # 「已改成『识别到就做』：看」后面没了），所以我不停地调
            # `wraplength`、列宽、grid 权重 —— 全白费。实际是
            # **`CTkLabel` 会把高度锁在 42px**，只装得下 3 行 10 号字，
            # 折成 4 行的描述第 4 行整行被吃掉；那一行又恰好只显示了一部分，
            # 于是看起来就像右侧被截。
            #
            # 实测（同样文字、`wraplength=324`）：
            #   `CTkLabel` 不写 height → 外层/内层都 42px（正确应 63px）
            #   `CTkLabel(height=28)`  → 同样 42px（写了 height 也没用）
            #   原生 `tk.Label`        → 63px，**内容完整**
            # 注意 `CTkLabel` 的 `height` 是**逻辑px 且会被 `_apply_...`
            # 缩放**，所以「给等于内容的 height」这条路走不通：兜底
            # `height=line_height*lines` 在 1.5 倍下又变成 1.5 倍高，
            # 要么继续裁、要么留一大截空白。原生 Label 让 Tk 自己算，
            # 两个毛病都没有。
            #
            # 代价：失去 CTk 的主题配色，得手写 `bg`/`fg`。`bg` 要写成
            # 卡片的底色 `COL_CARD`，否则会在卡片上留一块突兀的方块。
            # 字体也不写死字体名（各家机器上雅黑/苹方的名字不同），
            # 只给字号，让 Tk 用系统默认字体 —— 与旁边 CTkLabel 视觉一致。
            tk.Label(
                wrap, text=desc, justify="left", anchor="w",
                wraplength=LEFT_COL_W - 156,
                font=("", 10), bg=COL_CARD, fg=COL_TEXT_DIM,
                bd=0, highlightthickness=0,
            ).grid(row=1, column=0, sticky="w", padx=(46, 0))
        # 底部留白
        ctk.CTkLabel(card, text="", height=2).grid(row=len(TASKS) + 1, column=0)

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

        # 「手机浏览器登录（桌面版）」。
        #
        # 为什么要有这个按钮：在模拟器里跑微信有**被封号**的风险，而平台其实
        # 有两套前端 —— 桌面版（`/`）普通浏览器就能开，手机版（`/mobile/`）
        # 才只认微信。所以只要把手机浏览器的 UA 改成桌面 UA，就能完全不开
        # 微信地看同一批课，顺带躲开模拟器上那个反复崩的微信解码器。
        #
        # 这件事手工做要好几步（带调试端口启浏览器 → adb forward → CDP 改 UA
        # → 导航 → 填手机号），而且顺序错了 SPA 会卡死，所以固化成一个按钮。
        self.btn_login = ctk.CTkButton(
            box, text="📱  手机浏览器登录（桌面版）", height=34,
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

    def _build_right(self) -> None:
        col = ctk.CTkFrame(self.root, fg_color=COL_CARD, corner_radius=10,
                           border_width=1, border_color=COL_BORDER)
        col.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=14)
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
        col.grid_columnconfigure(0, weight=1, minsize=LOG_COL_W)
        col.grid_columnconfigure(1, weight=0, minsize=DEBUG_COL_W)
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
        `MaaElearning.exe --run debug_view`。
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
            "    MaaElearning.exe --list",
            "    MaaElearning.exe --run run_full_exam",
            "    MaaElearning.exe --run harvest_answers",
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
        """「手机浏览器登录（桌面版）」按钮。

        做四件事（都在后台线程里，界面不卡）：
          1. 让设备上的浏览器带着**调试端口**起来，并把端口 forward 到本机
          2. 用 CDP 把 UA 覆盖成桌面 UA —— **必须在导航之前**，顺序反了 SPA 卡死
          3. 导航到桌面版的验证码登录页
          4. 如果配置里写了手机号，顺手填进去并点「获取验证码」

        剩下的最后一步（填 6 位短信码 → 点「立即登录」）交给用户：
        短信在用户手机上，程序读不到也不该读。
        """
        self._start_worker(self._phone_login_job, "手机浏览器登录")

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
            self.logger("[browser] ✗ 没能打开："
                        "模拟器请确认已启动；真机请开 USB 调试并连上数据线。")
            self._fail("桌面版登录页没打开")

    def _set_busy(self, busy: bool, label: str = "运行中…") -> None:
        """切换「空闲 ↔ 运行中」的界面状态。

        「开始/停止」是**同一个按钮**，这里负责换它的文案和颜色：
          空闲 → 蓝色「▶ 开始运行」
          运行 → 红色「■ 停止」
        """
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_check.configure(state=state)
        self.btn_login.configure(state=state)
        if busy:
            self.btn_run.configure(
                text="■  停止", fg_color=COL_ERR, hover_color="#b91c1c",
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
        if "watch" in keys and "course" in keys:
            self.logger("[warn] 同时选了「整门课轮播」和「只看护当前视频」："
                        "轮播会自己切课，之后看护的将是那时正在播的一课。")
        self._start_worker(lambda: self.core.run_tasks(keys), "运行任务")

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

        现在：按钮和「开始运行」同位同体，且 `core.stop()` 会调
        `tasker.post_stop()` 真正中断，几秒内生效。
        """
        if not getattr(self, "_busy", False):
            self.logger("[ui] 当前没有任务在运行")
            return
        self.logger("[ui] 已发送停止请求…（正在通知框架中断当前任务）")
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
            self.btn_run.configure(state="normal", text="■  停止")

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
