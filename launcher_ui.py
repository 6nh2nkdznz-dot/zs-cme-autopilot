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
import traceback
from pathlib import Path

APP_TITLE = "中山医院继续教育平台助手"
APP_VER = "0.3.0"

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

import customtkinter as ctk  # noqa: E402

import paths  # noqa: E402
from core import TASKS, AppCore  # noqa: E402


def debug_view_cmd() -> tuple[list[str], str]:
    """拼出启动调试视图的命令行，返回 `(命令, 工作目录)`。

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
    """把 print 风格输出塞进队列，交给主线程渲染。"""

    def __init__(self, q: "queue.Queue[str]") -> None:
        self.q = q

    def __call__(self, msg: str = "") -> None:
        self.q.put(str(msg))

    def write(self, msg: str) -> None:
        if msg:
            for line in msg.rstrip("\n").split("\n"):
                self.q.put(line)

    def flush(self) -> None:
        pass


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
        self.logger = QueueLogger(self.log_q)

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

        # 内容高（缩放后像素）+ 窗口边框余量。
        #
        # 本机实测数据（都按缩放后像素算）：
        #   屏幕 1067，工作区 1019（任务栏占 48），窗口最高能开到 1084（客户区 1046）
        #   内容需要 ~980（其中左栏内容 856 + 动作按钮 ~57 + 工具行 ~45）
        # 所以：**直接开到屏幕工作区那么高**，别抠计算 —— 一开始我用
        # 「内容高 + 110」，算出窗口只有 620，底部按钮照样看不到。
        #
        # 左栏内容必须压到 ~880px 以内：`_build_left` 里那些 `height=`
        # 显式取值就是为此（不显式给的话，`CTkLabel` 默认 height=28 在 1.5 倍
        # 缩放下实际占 42px，怎么都装不下）。
        # 窗口高度**直接取屏幕工作区**，不抠内容高。
        #
        # 一开始我用「内容高 + 110」算，结果窗口只有 620，底部按钮照样看不到 ——
        # 因为 `CTkScrollableFrame` 不上报内容高度，`winfo_reqheight()` 只返回
        # 432（见 `_measure_left_height` 的说明），算出来的值比实际需要的小一半。
        #
        # 改用「内容高」重算后仍然偏小：内容实测 856，但整列还要加上标题栏、
        # 动作按钮和工具行，实际需要约 1000+。反复抠计算不如直接给足：
        # 开到工作区高度，左栏内容 856 必然放得下（工作区本机 1019）。
        #
        # 代价是窗口底部会有一点空白 —— 比按钮够不到好得多。
        work_px = self._work_area_height()
        # 转成 CTk 的逻辑像素；留 16px 余量，免得窗口底边被任务栏蹭到
        h = max(620, int((work_px - 16) / scale))
        # 宽度也要给足：左栏要放得下任务描述（wraplength=280）和三个工具按钮。
        # 一开始用 `winfo_reqwidth()/scale`，算出 1080 逻辑像素，左栏被压窄、
        # 描述文字被截断。实测 1200 逻辑像素才够。
        w = max(1200, min(int(self.root.winfo_reqwidth() / scale), 1400))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(900, 620)

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
        root.grid_columnconfigure(0, weight=0, minsize=340)
        root.grid_columnconfigure(1, weight=1)
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
            head, text=f"v{APP_VER} · 中山医院远程教育",
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

            # height 要显式给：CTkLabel 默认 height=28，在本机 1.5 倍缩放下
            # 实际占 42px（与字号无关），四个任务行会白白多出上百像素。
            ctk.CTkLabel(
                wrap, text=desc, justify="left", anchor="w", wraplength=280,
                height=28, font=ctk.CTkFont(size=10), text_color=COL_TEXT_DIM,
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
        self.btn_run.grid(row=1, column=0, sticky="ew", pady=(8, 0))

    # ---------- 右栏 ----------

    def _build_right(self) -> None:
        col = ctk.CTkFrame(self.root, fg_color=COL_CARD, corner_radius=10,
                           border_width=1, border_color=COL_BORDER)
        col.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=14)
        col.grid_columnconfigure(0, weight=1)
        col.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(col, fg_color="transparent")
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
            col, wrap="word", corner_radius=8,
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

    def _set_busy(self, busy: bool, label: str = "运行中…") -> None:
        """切换「空闲 ↔ 运行中」的界面状态。

        「开始/停止」是**同一个按钮**，这里负责换它的文案和颜色：
          空闲 → 蓝色「▶ 开始运行」
          运行 → 红色「■ 停止」
        """
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_check.configure(state=state)
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

    def on_debug_view(self) -> None:
        """打开调试视图（独立进程）。

        ## 为什么用独立进程而不是内嵌

        调试视图要自己持有一个 adb 连接和 OCR 模型，跟主界面的任务抢同一个
        模拟器会互相干扰。单独一个进程更干净，关掉也不影响正在跑的看护。

        命令怎么拼由 `debug_view_cmd()` 决定 —— 单独抽出来就是为了能
        脱离界面直接测（打包后拼错命令这个 bug 就是缺测试才漏出去的）。
        """
        import subprocess

        try:
            cmd, cwd = debug_view_cmd()
        except RuntimeError as exc:
            self.logger(f"[ui] {exc}", "err")
            return
        try:
            subprocess.Popen(cmd, cwd=cwd)
            self.logger("[ui] 已启动调试视图（独立窗口）")
        except OSError as exc:
            self.logger(f"[ui] 启动调试视图失败: {exc}", "err")

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


def main() -> int:
    ctk.set_appearance_mode("dark")
    try:
        ctk.set_default_color_theme("blue")
    except Exception:  # noqa: BLE001 - 主题缺失不该拦住启动
        pass

    root = ctk.CTk()
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 非 Windows 或权限不足时忽略
        pass

    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
