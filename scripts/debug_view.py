"""调试面板：实时显示设备画面 + OCR 判定框 + 识别明细。

## 为什么需要它

用户反馈「识别错误」时，光看日志很难判断是**哪一步**读错了 ——
是 OCR 没读到、还是读到了但判定逻辑用错了、还是圆圈状态判反了。
把这三样画在同一张图上，一眼就能看出来。

## 两种用法

1. **内嵌**（主路径）：主界面右侧「🔍 调试视图」按钮把右栏从
   `运行日志` 切到本面板。这是用户要求的形式 —— 原先它是个**独立窗口**，
   主界面右栏还空着一大块，「把调试窗口去了，把右边的空白处改成调试模式
   显示的东西」。

2. **独立窗口**：`python scripts\\debug_view.py` 或
   `MaaElearning.exe --run debug_view`。保留它是为了不开主界面时也能排查。

两种用法共用 `DebugPanel`，所以不会出现「窗口里有、内嵌版没有」这种漂移。

## 面板上有什么

```
┌──────────────────────────────┬──────────────────┐
│  设备画面（自适应缩放）        │  识别明细         │
│  ├ 每个 OCR 文本框画绿框       │  页面类型: 课程页  │
│  ├ 圆圈检测区画黄框            │  圆圈判定: ● ◐ ○  │
│  ├ 识别到的文字标在框上        │  识别到的文本块    │
│  └ 判定结论画在顶部            │                  │
├──────────────────────────────┴──────────────────┤
│ 暂停/继续 · 存图 · 缩小 · 放大 · ☑文字标签         │
└─────────────────────────────────────────────────┘
```

按「暂停/继续」可以冻住画面方便细看；「存图」把带框的图存到
`debug/overlay/`，方便发给别人。
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageTk  # noqa: E402

import channels  # noqa: E402
import circles  # noqa: E402
import exam  # noqa: E402
import paths  # noqa: E402

#: 画布尺寸 = MaaFramework 的归一化尺寸，所有 roi/target 都用这个坐标系
CANVAS_W, CANVAS_H = 720, 1280

#: 显示缩放的**上限**。实际取值在运行时按可用空间算出来（见 `fit_scale`）。
#:
#: 为什么不能写死：窗口高度 = 图高 + 日志区 + 边距，而屏幕高度各人不同。
#: 早先写死 0.34 配 `total_h = min(..., 935)`，在 1070 高的屏幕上整窗
#: 935 就会把底部日志、以及画面最下面那节课的判定标注一起挤出可视区 ——
#: 用户报「最后一个课程的完成度没被识别到」，其实**识别到了**，
#: 只是标注画在被裁掉的那一段里（实测 6 节课全部识别正确）。
DEFAULT_SCALE = 0.34
MAX_SCALE = 0.52
#: **内嵌模式**的缩放上限。内嵌在右栏里，右栏高度能到 1200+ 逻辑px，
#: 用独立窗口那个 0.52 会让画面只占上半截、下面一大块空着（实测
#: canvas 只有 665 物理px 高，而可用高度 1242）。
#: 放到 0.72 让画面能长到 921 物理px，既填满右栏又不至于喧宾夺主；
#: 想更大可以点「放大」（那是手动缩放，不受这个上限约束）。
EMBED_MAX_SCALE = 0.72
#: 独立窗口的右栏宽度、日志区高度、以及左右上下边距
#: （都是像素，只用于**独立窗口**模式；内嵌模式不用这些）
RIGHT_W = 305
LOG_H = 180
CHROME_H = 130
#: 窗口边框 + 标题栏的实际占位。实测 `geometry("595x655")` 出来，
#: `GetWindowRect` 是 **611x694** —— 比内容多 16x39。算「放得下」时必须算进去，
#: 否则贴边的配置会差几十像素、恰好被裁掉（正是用户报的那个现象）。
FRAME_W = 20
FRAME_H = 45

#: 内嵌模式：识别明细栏宽度、工具栏高度、各方向内边距（Tk 逻辑像素）。
#: 画面缩放按「右栏实际宽高 − 这些」反推，所以面板多大都能自适应。
EMBED_DETAIL_W = 250
EMBED_TOOLBAR_H = 40
EMBED_PAD = 10

# 颜色（RGB）
CLR_OCR_BOX = (34, 197, 94)       # 绿：普通 OCR 文本框
CLR_OCR_TEXT = (220, 255, 220)
CLR_CIRCLE = (250, 204, 21)       # 黄：圆圈检测区
CLR_DONE = (34, 197, 94)
CLR_PARTIAL = (250, 204, 21)
CLR_NONE = (148, 163, 184)
CLR_HEAD = (59, 130, 246)         # 蓝：顶部结论条

# 界面配色（与 launcher_ui 的暗色主题保持一致）
BG = "#16181d"
CARD = "#1e2128"
CARD_HI = "#252932"
BORDER = "#2f343f"
TEXT = "#e6e9ef"
TEXT_DIM = "#8b93a3"
ACCENT = "#3b82f6"
OK = "#22c55e"
WARN = "#f59e0b"
ERR = "#ef4444"

#: 存图时优先用的中文字体（PIL 默认位图字体不含中文字形，画出来是黑方块）
_FONT_CANDIDATES = (r"C:\Windows\Fonts\msyh.ttc",
                    r"C:\Windows\Fonts\simhei.ttf")


def fit_scale(screen_w: int, work_h: int) -> float:
    """按屏幕尺寸算出「独立窗口整窗放得下」的最大显示缩放。

    抽成纯函数是为了能离线测 —— 之前这段逻辑内联在 GUI 里，
    只能靠起窗口肉眼看，结果调了六七轮才凑对。

    窗口高 = 图高(1280*scale) + LOG_H + CHROME_H + FRAME_H，必须 <= work_h；
    窗口宽 = 图宽(720*scale)  + RIGHT_W + FRAME_W + 边距，必须 <= screen_w。
    """
    by_h = max(300, work_h - LOG_H - CHROME_H) / CANVAS_H
    by_w = (screen_w - RIGHT_W - FRAME_W - 40) / CANVAS_W
    return max(0.25, min(DEFAULT_SCALE, MAX_SCALE, by_h, by_w))


def embed_scale(avail_w: int, avail_h: int) -> float:
    """按内嵌面板的可用空间算画面缩放。

    与 `fit_scale` 分开：那边算的是**整窗**装进屏幕（要扣掉日志区、标题栏），
    这边算的是**画面**装进已经给定的那块地方（主界面右栏），没有窗口边框、
    没有独立日志区。上限也不同（`EMBED_MAX_SCALE` > `MAX_SCALE`），
    因为内嵌只有**高度**这一个约束，不该被独立窗口那个保守上限卡住。
    抽成纯函数同样是为了能离线测。
    """
    by_h = max(120, avail_h) / CANVAS_H
    by_w = max(120, avail_w) / CANVAS_W
    return max(0.12, min(EMBED_MAX_SCALE, by_h, by_w))


def _find_cjk_font() -> str | None:
    for cand in _FONT_CANDIDATES:
        if Path(cand).is_file():
            return cand
    return None


class DebugPanel:
    """实时调试面板：画面 + 明细 + 工具栏。

    `standalone=False` 时嵌入宿主容器（主界面右栏）；`True` 时用在
    独立窗口里（见 `DebugView`）。
    """

    def __init__(self, parent, standalone: bool = False) -> None:
        self.standalone = standalone
        self.scale = DEFAULT_SCALE
        self._parent = parent
        #: 明细栏宽度。独立窗口用固定 `RIGHT_W`（那窗口窄，固定才稳）；
        #: 内嵌用 `EMBED_DETAIL_W` —— 再宽也没用，明细只是短文本，
        #: 多出来的横向空间留给画面更好。
        self._detail_w = RIGHT_W if standalone else EMBED_DETAIL_W

        self._paused = False
        self._close = False
        self._show_text = tk.BooleanVar(value=False)
        self._lock = threading.Lock()
        #: 采集线程 → 主线程的明细消息队列。**Tk 控件只能在主线程碰**，
        #: 详见 `_append_detail` 的说明。
        self._detail_q: queue.Queue[str] = queue.Queue()
        #: 「正在初始化…」当占位用，第一条真消息到达时把它顶掉
        self._drop_placeholder = False
        #: 最新一帧的分析结果，由后台线程填、主线程读
        self._frame: dict = {}
        #: 上一次渲染用过的缩放。变了就要重画 overlay（见 `_render`）
        self._last_scale = -1.0
        #: 上一张 PhotoImage。**必须留引用**，否则会被 GC 掉、画面变空白
        self._photo = None

        if standalone:
            self.root = parent
        else:
            self.root = parent.winfo_toplevel()

        self._build()
        self._start_worker()
        self.root.after(120, self._tick)

    # ---------------- 界面 ----------------

    def _build(self) -> None:
        pad = EMBED_PAD if not self.standalone else 10
        if self.standalone:
            outer = tk.Frame(self._parent, bg=BG)
            outer.pack(fill="both", expand=True)
            self.scale = fit_scale(self._parent.winfo_screenwidth(),
                                   _work_area_height())
        else:
            outer = tk.Frame(self._parent, bg=CARD)
            outer.pack(fill="both", expand=True)

        # 上方：画面 + 明细。下方：工具栏。
        #
        # 用 `grid` 而不是 `pack`：工具栏要**钉在底部**（`side="bottom"`
        # 在 pack 里也行，但画面行要 `weight=1` 才能吃掉富余高度，
        # grid 表达得更直白）。
        top = tk.Frame(outer, bg=BG if self.standalone else CARD)
        top.pack(fill="both", expand=True, padx=pad, pady=(pad, 0))
        self._top = top

        # 左：画面 + 右：明细，用 `pack`。
        #
        # ## 为什么不是 `fill="both", expand=True` 让两边自己长
        # 内嵌时右栏有 1700+ 逻辑px 宽、1200+ 高：
        #   - canvas 若 `expand=True`，它是 9:16 竖屏、宽度先到顶，
        #     底部会留一大块**纯黑死区**（实测第一版就是这样）；
        #   - 明细栏若 `expand=True`，会涨到 1191px 宽 —— 明细只是短文本，
        #     宽成那样纯属浪费，还把画面挤到左边一小条。
        # 所以**两边都固定尺寸**：canvas 由 `_fit_canvas()` 按画面尺寸调，
        # 明细栏由同一个方法设成 `EMBED_DETAIL_W × 画面高`，
        # 富余空间留在最右边（纯背景，不放任何东西）。
        #
        # ## 为什么 `pack_propagate(False)` 而不是 `grid_propagate(False)`
        # 两个都是「禁止按内容改自身尺寸」，但明细栏里是 `pack` 的子控件，
        # 对应的是 `pack_propagate`。用错了不报错，只是尺寸控制不了
        # （实测要 250 宽却成了 562）。
        self.canvas = tk.Canvas(
            top, width=int(CANVAS_W * self.scale),
            height=int(CANVAS_H * self.scale),
            bg="#000000", highlightthickness=0,
        )
        self.canvas.pack(side="left", anchor="n")

        right = tk.Frame(top, bg=BG if self.standalone else CARD,
                         width=self._detail_w,
                         height=int(CANVAS_H * self.scale))
        right.pack(side="left", anchor="n", padx=(pad, 0))
        right.pack_propagate(False)
        self._right = right

        tk.Label(right, text="识别明细", bg=BG if self.standalone else CARD,
                 fg=TEXT_DIM, font=("", 10, "bold"),
                 anchor="w").pack(fill="x")
        self.info = tk.Text(
            right, bg=CARD, fg=TEXT, bd=0, highlightthickness=0,
            font=("Consolas", 9), wrap="none",
            insertbackground=TEXT, height=6,
        )
        self.info.pack(fill="both", expand=True, pady=(4, 0))
        self.info.configure(state="disabled")

        # 工具栏。用**原生 tk**：主界面里 CTk 控件会被 DPI 缩放放大一倍，
        # 一排按钮能把画面挤掉一大块。
        bar_h = EMBED_TOOLBAR_H if not self.standalone else 34
        bar = tk.Frame(outer, bg=BG if self.standalone else CARD, height=bar_h)
        bar.pack(side="bottom", fill="x", padx=pad, pady=(6, pad))
        bar.pack_propagate(False)
        self._bar = bar
        self.btn_pause = _tool_button(bar, "暂停", self._toggle_pause, width=6)
        _tool_button(bar, "存图", self._save_overlay, width=6)
        _tool_button(bar, "缩小", lambda: self._resize_canvas(0.85), width=5)
        _tool_button(bar, "放大", lambda: self._resize_canvas(1.18), width=5)
        tk.Checkbutton(
            bar, text="文字标签", variable=self._show_text,
            bg=BG if self.standalone else CARD, fg=TEXT_DIM,
            selectcolor=CARD_HI, activebackground=BG if self.standalone else CARD,
            activeforeground=TEXT, font=("", 8), bd=0,
            highlightthickness=0,
        ).pack(side="left", padx=(8, 0))

        # 可用区一变大小就重算画面尺寸和位置（内嵌时窗口可被拖动）
        top.bind("<Configure>", self._on_space_resize)
        #: 用户用「放大/缩小」指定的缩放；`None` = 跟随可用空间自动
        self._manual_scale: float | None = None

    def _on_space_resize(self, ev) -> None:
        """可用区尺寸变了：重算画面缩放，下一帧按新尺寸重画。"""
        if self._manual_scale is not None:
            self._fit_canvas()
            return
        avail_w = max(80, ev.width - self._detail_w - EMBED_PAD)
        avail_h = max(120, ev.height)
        new = embed_scale(avail_w, avail_h)
        if abs(new - self.scale) > 0.004:
            self.scale = new
            self._last_scale = -1.0      # 强制下一帧按新尺寸重画
        self._fit_canvas()

    def _fit_canvas(self) -> None:
        """把 canvas 调成「画面尺寸」，明细栏调成同高。

        **只在尺寸真的变了才 `configure`** —— `configure` 会触发父容器的
        `<Configure>` 事件，无条件调用就成死循环（实测会卡住界面）。
        位置不用管：两者都由 `pack(side="left", anchor="n")` 摆好，
        富余空间留在最右边。
        """
        w = int(CANVAS_W * self.scale)
        h = int(CANVAS_H * self.scale)
        if (self.canvas.winfo_reqwidth() != w
                or self.canvas.winfo_reqheight() != h):
            self.canvas.configure(width=w, height=h)
        # 明细栏跟着画面长高 —— 否则它只有一行高（实测 109px），
        # 下面一大片空白全浪费了
        if self._right.winfo_reqheight() != h:
            self._right.configure(height=h)

    def _resize_canvas(self, factor: float) -> None:
        """「放大/缩小」：手动定缩放，此后不再跟随可用空间。"""
        base = self._manual_scale if self._manual_scale is not None else self.scale
        self._manual_scale = max(0.12, min(1.2, base * factor))
        self.scale = self._manual_scale
        self._last_scale = -1.0
        self._fit_canvas()

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.btn_pause.configure(text="继续" if self._paused else "暂停")

    def _log(self, msg: str, tag: str = "") -> None:
        # 面板内部没有日志区了（日志在主界面右栏），所以只往明细里记
        # 一句，别静默吞掉。
        self._append_detail(msg)

    def _append_detail(self, msg: str) -> None:
        """往明细栏追加一句话。

        ## 为什么走队列，不直接写控件（踩过的坑）

        **Tk 控件只能在主线程碰。** 这个方法原先直接
        `self.info.configure/insert/see`，而采集线程也在调它 —— 一旦
        主线程正卡在别的地方（内嵌时点开面板，主线程还在建界面），
        Tk 就抛 `RuntimeError: main thread is not in main loop`。
        在采集线程里那是个**未捕获异常**，线程直接死掉，
        表现是「面板一片空白、明细只停在『正在初始化』」，而且
        连异常都看不到（另一处 `except tk.TclError` 兜不住
        `RuntimeError`）。所以改成：工作线程只**入队**，
        主线程在 `_tick()` 里取出来写控件。
        """
        self._detail_q.put(msg)

    def _drain_details(self) -> None:
        """主线程里把队列里的明细写进控件（见 `_append_detail` 的说明）。"""
        lines: list[str] = []
        while True:
            try:
                lines.append(self._detail_q.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        state = self.info.cget("state")
        self.info.configure(state="normal")
        # 初始化那行「正在初始化…」是**占位**，第一条真消息来了就顶掉它
        if self._drop_placeholder:
            first = self.info.get("1.0", "end-1c").splitlines()
            if len(first) == 1 and first[0].startswith("[dbg] 正在初始化"):
                self.info.delete("1.0", "end")
        self._drop_placeholder = True
        self.info.insert("end", "\n".join(lines) + "\n")
        self.info.see("end")
        self.info.configure(state=state)

    def _save_overlay(self) -> None:
        with self._lock:
            im = self._frame.get("overlay")
            labels = list(self._frame.get("labels") or [])
        if im is None:
            self._log("[ui] 还没有画面可存")
            return
        # 存图时把标注也烧进去（画布文字没法存），用一个自带的中文字体。
        # 找不到字体就只存几何框，并说一声 —— 不要静默丢标注。
        try:
            from PIL import ImageFont
            fpath = _find_cjk_font()
            if fpath:
                font = ImageFont.truetype(fpath, 13)
                d = ImageDraw.Draw(im)
                for lx, ly, ltxt, lcolor in labels:
                    d.text((lx + 2, ly), ltxt, fill=lcolor, font=font)
            else:
                self._log("[ui] 没找到中文字体，存图不含文字标注")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[ui] 烧录标注失败（只存几何框）: {exc}")
        out = paths.debug_dir() / "overlay"
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"overlay-{time.strftime('%Y%m%d-%H%M%S')}.png"
        im.save(f)
        self._log(f"[ui] 已存图: {f}")

    def stop(self) -> None:
        """宿主切走 / 关窗时调用：停掉采集线程，别让它继续占着 adb。"""
        self._close = True

    # ---------------- 采集线程 ----------------

    def _start_worker(self) -> None:
        self._append_detail("[dbg] 正在初始化 adb / OCR …")
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
        """采集线程：初始化 adb/OCR，然后约 3 秒一帧地抓屏+识别。

        **绝对不碰 Tk 控件** —— 一切给界面的消息都走 `self._detail_q`
        （见 `_append_detail`）。这里踩过一次严重坑：直接写控件导致
        `RuntimeError: main thread is not in main loop`，线程静默死掉，
        面板一片空白。
        """
        try:
            from controller import build_controller, load_config
            from maa.pipeline import JOCR, JRecognitionType
            from maa.resource import Resource
            from maa.tasker import Tasker

            res = Resource()
            res.post_bundle(str(paths.resource_dir())).wait()
            res.post_ocr_model(str(paths.ocr_model_dir())).wait()
            controller = build_controller(load_config())
            tasker = Tasker()
            tasker.bind(res, controller)
            self._append_detail("[dbg] ✓ 就绪，开始实时刷新")
        except Exception as exc:  # noqa: BLE001 - 初始化失败要显示出来
            self._append_detail(f"[dbg] ✗ 初始化失败: {type(exc).__name__}: {exc}")
            return

        while not self._close:
            if self._paused:
                time.sleep(0.2)
                continue
            t0 = time.monotonic()
            try:
                self._grab_once(controller, tasker, JOCR, JRecognitionType)
            except Exception as exc:  # noqa: BLE001 - 单帧失败不该终止
                self._append_detail(f"[dbg] 采集异常: {type(exc).__name__}: {exc}")
                time.sleep(1.0)
            dt = time.monotonic() - t0
            # 目标约 3 秒一帧，别把 adb 打满
            time.sleep(max(0.0, 3.0 - dt))

    def _grab_once(self, controller, tasker, JOCR, JRecognitionType) -> None:
        job = controller.post_screencap().wait()
        if not job.succeeded:
            return
        img = job.get()

        j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
        rows: list[tuple[str, int, int, int, int]] = []
        if j.wait().succeeded:
            td = j.get()
            if td is not None:
                for nid in td.node_id_list:
                    nd = tasker.get_node_detail(nid)
                    if nd is None or nd.recognition is None:
                        continue
                    for r in (nd.recognition.all_results or []):
                        box = getattr(r, "box", None)
                        txt = getattr(r, "text", None)
                        if box and txt:
                            rows.append((str(txt), int(box[0]), int(box[1]),
                                         int(box[2]), int(box[3])))
                    break

        full_text = " ".join(t for t, *_ in rows)
        page = exam.detect_page(full_text)

        # 通道顺序：抓屏返回的是 **BGR**，**必须**先转成 RGB。
        # 转错的症状很直观：调试视图里画面偏色（蓝色圆圈变橙色），
        # 而且圆圈判定会**全部**变成「没看过」。详见 `channels.py` 的说明。
        img_rgb = channels.to_rgb(img)

        # 圆圈检测：只在「课程页」有意义
        circles_info: list[tuple[str, int, str, object]] = []
        if page == exam.PAGE_COURSE:
            for txt, x, y, _w, _h in rows:
                if ".mp4" not in txt.lower():
                    continue
                sh = circles.circle_shape(img_rgb, y)
                circles_info.append((txt, y, sh.state, sh))

        # 只在**几何**上标注（框、检测区、顶部条）。文字交给画布原生
        # `create_text`，因为 PIL 默认位图字体不含中文字形，会画成黑方块。
        base = _draw_geometry(img_rgb, rows, circles_info, page, self._show_text)
        with self._lock:
            self._frame = {
                "base": base,
                "labels": getattr(base, "labels", []),
                "rows": rows,
                "page": page,
                "circles": circles_info,
                "full_text": full_text,
            }

    # ---------------- 主线程刷新 ----------------

    def _tick(self) -> None:
        if self._close:
            return
        # 先把采集线程排队的明细写进控件（**必须在主线程做**，
        # 见 `_append_detail` 的说明）
        try:
            self._drain_details()
        except Exception as exc:  # noqa: BLE001
            self._log(f"[ui] 写明细失败: {type(exc).__name__}: {exc}")
        with self._lock:
            fr = dict(self._frame)
            scale = self.scale
        if fr:
            # 必须自己兜住异常。tkinter 的 `after` 回调里抛异常**不会**
            # 让窗口崩，只会被 Tk 自己的报错处理吞掉 —— 表现是「界面静默不
            # 更新」，很难查。踩过一次：`f"{CircleShape:>4}"` 抛 TypeError，
            # 右栏就一直空白，界面看起来像「没识别到数据」。
            try:
                self._render(fr, scale)
            except Exception as exc:  # noqa: BLE001
                self._append_detail(f"[ui] 刷新界面失败: {type(exc).__name__}: {exc}")
        self.root.after(300, self._tick)

    def _render(self, fr: dict, scale: float) -> None:
        base = fr.get("base")
        if base is not None:
            w = max(80, int(CANVAS_W * scale))
            h = max(120, int(CANVAS_H * scale))
            # 画面缩放到当前面板大小。缩放变了就重画 —— 否则拖大窗口时
            # 图还是糊的旧尺寸（`ImageTk` 不会自己跟着 canvas 拉伸）。
            if abs(scale - self._last_scale) > 0.005:
                self._last_scale = scale
                self._photo = ImageTk.PhotoImage(
                    base.convert("RGB").resize((w, h), Image.LANCZOS))
                need_redraw = True
            else:
                need_redraw = False
            if need_redraw or not self.canvas.find_all():
                self.canvas.delete("all")
                self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
                # 标注用**画布原生文字**画：系统字体带中文字形，
                # 而且缩放时字不会糊（PIL 默认字体画中文会变成黑块）。
                k = scale
                for lx, ly, ltxt, lcolor in (fr.get("labels") or []):
                    self.canvas.create_text(
                        lx * k + 2, ly * k, anchor="nw", text=ltxt,
                        fill=lcolor, font=("", 9))

        # 右侧明细
        lines = [f"页面类型: {exam.page_name(fr.get('page', ''))}", ""]
        cs = fr.get("circles") or []
        if cs:
            lines.append(f"圆圈判定（{len(cs)} 条）:")
            for txt, _y, state, sh in cs:
                mark = {"done": "●", "partial": "◐", "none": "○"}.get(state, "?")
                # 报「宽度/比例」而不是像素个数 —— 判定依据是宽度。
                # 注意 sh 是 CircleShape 对象，**不能**直接用数字格式符
                # （`f"{sh:>4}"` 会抛 TypeError，让整个 _render 中断、
                #  右栏静默变空白。踩过。）
                info = (f"宽{sh.width:2d} 比例{sh.ratio:.2f}"
                        if getattr(sh, "found", False) else "无蓝")
                lines.append(f"  {mark} {info}  {txt[:26]}")
        else:
            lines.append("圆圈判定: （非课程页，不检测）")
        lines.append("")
        rows_all = fr.get("rows") or []
        lines.append(f"识别到 {len(rows_all)} 个文本块:")
        for txt, _x, _y, _w, _h in rows_all:
            lines.append(f"  {txt}")

        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", "\n".join(lines))
        self.info.configure(state="disabled")


def _draw_geometry(img, rows, circles_info, page, show_text):
    """在画面上画几何图形（框、检测区、顶部条），返回 PIL 图。

    待画的文字标注挂在返回值的 `.labels` 上（`PIL.Image` 允许挂自定义
    属性）。用**挂属性**而不是包一层类，是因为下游 `resize`/`convert`/
    `save` 都要转发一遍，包一层很容易漏掉某个方法（我先写了一版
    `_Drawn` 包装，转发 `save` 时就漏了参数）。

    ## 为什么文字不在这里画

    PIL 的默认位图字体**不含中文字形**，`draw.text("已看完")` 会画成一串
    黑方块 —— 实测看到的「黑色糊块」就是这个。

    加载中文字体又很麻烦（要指定 ttf 路径、还得随包带字体）。
    所以改成：**PIL 只画几何**，文字交给 tkinter 画布的原生 `create_text`
    （用系统字体，中文天然正常，而且缩放时字不会糊）。
    只有「存图」那条路才把文字烧进图里（见 `_save_overlay`）。
    """
    if not isinstance(img, Image.Image):
        arr = np.asarray(img)
        im = Image.fromarray(np.ascontiguousarray(arr))
    else:
        im = img.convert("RGB")
    im = im.resize((CANVAS_W, CANVAS_H), Image.LANCZOS).convert("RGB")
    d = ImageDraw.Draw(im)
    labels: list[tuple[int, int, str, str]] = []   # (x, y, text, color)

    # OCR 文本框。
    #
    # 文字标签**默认不画**：它和画面原文重叠，反而把内容糊住看不清
    # （实测存图后就是这个效果）。要看时用界面上的「文字标签」开关。
    for txt, x, y, w, h in rows:
        d.rectangle([x, y, x + w, y + h], outline=CLR_OCR_BOX, width=1)
        if show_text:
            label = txt if len(txt) <= 26 else txt[:25] + "…"
            labels.append((x, max(0, y - 15), label, "#dcffdc"))

    # 圆圈检测区：画**实际找到的**圆圈位置，而不是按固定偏移推的位置。
    # 固定偏移会随列表滚动漂移，框就画偏了 —— 让框跟着真实检测结果走，
    # 一眼能看出偏差。
    for txt, y, state, sh in circles_info:
        if sh.found:
            d.rectangle([sh.left, sh.top, sh.left + sh.width,
                         sh.top + sh.height],
                        outline=CLR_CIRCLE, width=2)
            cy = sh.top + sh.height // 2
        else:
            # 空圈：没有蓝像素可定位，退回搜索窗范围的提示框
            cy = y + circles.CIRCLE_DY
            d.rectangle([circles.CIRCLE_X0, cy - circles.CIRCLE_HALF_H,
                         circles.CIRCLE_X1, cy + circles.CIRCLE_HALF_H],
                        outline=CLR_CIRCLE, width=2)
        mark = {"done": "● 已看完", "partial": "◐ 一半",
                "none": "○ 没看"}.get(state, "?")
        # 用**深色**：圆圈右侧是浅色背景，浅色字看不清
        color = {"done": "#15803d", "partial": "#b45309"}.get(
            state, "#475569")
        # 报「宽度/比例」而不是像素个数：判定依据就是宽度，
        # 显示它才能一眼判断对错（像素个数会误导）。
        info = (f"宽{sh.width} 比例{sh.ratio:.2f}" if sh.found else "无蓝")
        labels.append((circles.CIRCLE_X1 + 4, cy - 8,
                       f"{mark} {info}", color))

    # 顶部结论条（这条是纯 ASCII，PIL 画就够了，保证一定在最上层）
    head = (f"page={exam.page_name(page)}  blocks={len(rows)}  "
            f"circles={len(circles_info)}  canvas={CANVAS_W}x{CANVAS_H}")
    d.rectangle([0, 0, CANVAS_W, 24], fill=CLR_HEAD)
    d.text((5, 7), head, fill=(255, 255, 255))
    labels.append((7, 5, f"页面={exam.page_name(page)}   "
                         f"文本块={len(rows)}   圆圈={len(circles_info)}",
                   "#ffffff"))
    im.labels = labels          # type: ignore[attr-defined]
    return im


def _tool_button(parent, text: str, command, width: int) -> tk.Button:
    """工具栏按钮（原生 tk，见 `DebugPanel._build` 里为什么不 use CTk）。"""
    btn = tk.Button(
        parent, text=text, width=width, command=command,
        bg=CARD_HI, fg=TEXT, activebackground=BORDER, activeforeground=TEXT,
        relief="flat", bd=0, highlightthickness=0, font=("", 9),
        cursor="hand2",
    )
    btn.pack(side="left", padx=(0, 4))
    return btn


def _work_area_height() -> int:
    """屏幕**工作区**高度（排除任务栏）。

    不能直接用 `winfo_screenheight()` —— 它含任务栏，照它算会超出。
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
    except Exception:  # noqa: BLE001 - 非 Windows 或调用失败时退回屏幕高
        pass
    return 900


class DebugView:
    """独立调试窗（保留给 `--run debug_view`）。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("调试视图 — MaaElearning")
        self.root.configure(bg=BG)

        # 画面 + 明细
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="both", expand=True)
        self.panel = DebugPanel(holder, standalone=True)

        # 独立窗口才有日志区（内嵌时日志就是主界面右栏）
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        tk.Label(bottom, text="日志", bg=BG, fg=TEXT_DIM,
                 font=("Consolas", 11, "bold"), anchor="w").pack(fill="x")
        self.log = tk.Text(bottom, bg=CARD, fg=TEXT, bd=0,
                           font=("Consolas", 9), height=8, wrap="none",
                           insertbackground=TEXT)
        self.log.pack(fill="both", expand=True, pady=(4, 0))
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("head", foreground="#60a5fa")

        # 窗口尺寸：宽度按「图 + 右栏 + 边距」，高度按「图 + 日志 + 边距」。
        w = int(CANVAS_W * self.panel.scale)
        h = int(CANVAS_H * self.panel.scale)
        total_w = w + RIGHT_W + 46
        total_h = h + LOG_H + 40
        self.root.geometry(f"{total_w}x{total_h}+30+10")
        self.root.minsize(520, 480)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        self.panel.stop()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    DebugView().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
