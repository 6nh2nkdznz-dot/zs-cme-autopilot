"""调试页面：实时显示设备画面 + OCR 判定框 + 叠加信息 + 日志。

## 为什么需要它

用户反馈「识别错误」时，光看日志很难判断是**哪一步**读错了 ——
是 OCR 没读到、还是读到了但判定逻辑用错了、还是圆圈状态判反了。
把这三样画在同一张图上，一眼就能看出来。

## 界面上有什么

```
┌──────────────────────────┬──────────────────────────┐
│  设备画面（实时刷新）      │  识别明细                │
│  ├ 每个 OCR 文本框画绿框   │  页面类型: 课程页         │
│  ├ 圆圈检测区画黄框        │  圆圈判定: ● ◐ ○ …       │
│  ├ 识别到的文字标在框上    │  当前视频: 43:48 / 1:00  │
│  └ 判定结论画在顶部        │  自定义识别/动作结果      │
├──────────────────────────┴──────────────────────────┤
│  日志（与运行日志同一个格式）                          │
└─────────────────────────────────────────────────────┘
```

## 怎么用

    MaaElearning.exe --run debug_view              # 在 exe 里
    python scripts\\debug_view.py                  # 源码里

按「暂停/继续」可以冻住画面方便细看；「存图」把带框的图存到
`debug/overlay/`，方便发给别人。
"""

from __future__ import annotations

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

#: 显示缩放的**上限**。实际取值在运行时按屏幕可用高度算出来（见 `_fit_scale`）。
#:
#: 为什么不能写死：窗口高度 = 图高 + 日志区 + 边距，而屏幕高度各人不同。
#: 早先写死 0.34 配 `total_h = min(..., 935)`，在 1070 高的屏幕上整窗
#: 935 就会把底部日志、以及画面最下面那节课的判定标注一起挤出可视区 ——
#: 用户报「最后一个课程的完成度没被识别到」，其实**识别到了**，
#: 只是标注画在被裁掉的那一段里（实测 6 节课全部识别正确）。
DEFAULT_SCALE = 0.34
MAX_SCALE = 0.52
#: 右栏宽度、日志区高度、以及左右上下边距（都是像素，用来算可用高度）
RIGHT_W = 305
LOG_H = 180
CHROME_H = 130
#: 窗口边框 + 标题栏的实际占位。实测 `geometry("595x655")` 出来，
#: `GetWindowRect` 是 **611x694** —— 比内容多 16x39。算「放得下」时必须算进去，
#: 否则贴边的配置会差几十像素、恰好被裁掉（正是用户报的那个现象）。
FRAME_W = 20
FRAME_H = 45

# 颜色（RGB）
CLR_OCR_BOX = (34, 197, 94)       # 绿：普通 OCR 文本框
CLR_OCR_TEXT = (220, 255, 220)
CLR_CIRCLE = (250, 204, 21)       # 黄：圆圈检测区
CLR_DONE = (34, 197, 94)
CLR_PARTIAL = (250, 204, 21)
CLR_NONE = (148, 163, 184)
CLR_HEAD = (59, 130, 246)         # 蓝：顶部结论条


def fit_scale(screen_w: int, work_h: int) -> float:
    """按屏幕尺寸算出「整窗放得下」的最大显示缩放。

    抽成纯函数是为了能离线测 —— 之前这段逻辑内联在 GUI 里，
    只能靠起窗口肉眼看，结果调了六七轮才凑对。

    窗口高 = 图高(1280*scale) + LOG_H + CHROME_H + FRAME_H，必须 <= work_h；
    窗口宽 = 图宽(720*scale)  + RIGHT_W + FRAME_W + 边距，必须 <= screen_w。
    """
    by_h = max(300, work_h - LOG_H - CHROME_H) / CANVAS_H
    by_w = (screen_w - RIGHT_W - FRAME_W - 40) / CANVAS_W
    return max(0.25, min(DEFAULT_SCALE, MAX_SCALE, by_h, by_w))


class DebugView:
    """实时调试窗。"""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("调试视图 — MaaElearning")
        self.scale = DEFAULT_SCALE

        self._paused = False
        self._close = False
        #: 是否在画面上叠 OCR 文字标签（默认关：会糊住原文）
        self._show_text = tk.BooleanVar(value=False)
        self._lock = threading.Lock()
        #: 最新一帧的分析结果，由后台线程填、主线程读
        self._frame: dict = {}

        self._fit_scale()
        self._build()
        self._start_worker()
        self.root.after(120, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 界面 ----------------

    def _fit_scale(self) -> None:
        """按屏幕可用高度定初始缩放，保证整窗（含日志）都放得下。

        **为什么不写死**：窗口高 = 图高 + 日志区 + 边距。写死缩放的话，
        屏幕矮一点整窗就超出屏幕，底部日志和画面最下面那节的判定标注会被
        裁掉 —— 看起来就像「最后一条没识别到」，其实识别对了只是没画出来。

        实测踩过：`DEFAULT_SCALE = 0.34` + `total_h = min(..., 935)`，
        在 1070 高的屏幕上正好超出，用户报「最后一个课程没被识别到」。
        """
        self.scale = fit_scale(self.root.winfo_screenwidth(),
                               self._work_area_height())

    @staticmethod
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

    def _build(self) -> None:
        w = int(CANVAS_W * self.scale)
        h = int(CANVAS_H * self.scale)

        outer = tk.Frame(self.root, bg="#16181d")
        outer.pack(fill="both", expand=True)

        top = tk.Frame(outer, bg="#16181d")
        top.pack(fill="both", expand=True)

        # 左：画面
        left = tk.Frame(top, bg="#16181d", width=w + 4)
        left.pack(side="left", fill="y", padx=(10, 6), pady=10)
        left.pack_propagate(False)
        self.canvas = tk.Canvas(left, width=w, height=h, bg="#000000",
                                highlightthickness=0)
        self.canvas.pack()

        # 右：信息
        right = tk.Frame(top, bg="#16181d", width=RIGHT_W)
        right.pack(side="left", fill="both", expand=True, padx=(6, 10), pady=10)
        # 必须禁止按内容撑大：明细栏内容一多就会把底部日志顶出窗口
        right.pack_propagate(False)
        right.grid_propagate(False)

        tk.Label(right, text="识别明细", bg="#16181d", fg="#94a3b8",
                 font=("Consolas", 11, "bold"), anchor="w").pack(fill="x")

        self.info = tk.Text(right, bg="#1e2128", fg="#e2e8f0", bd=0,
                            font=("Consolas", 9), wrap="none", height=20,
                            insertbackground="#e2e8f0")
        self.info.pack(fill="both", expand=True, pady=(4, 8))

        # 按钮条。分两行放：一行塞 5 个控件时「文字标签」会被右栏裁掉。
        bar = tk.Frame(right, bg="#16181d")
        bar.pack(fill="x", pady=(4, 0))
        self.btn_pause = tk.Button(bar, text="暂停", width=6,
                                   command=self._toggle_pause)
        self.btn_pause.pack(side="left")
        tk.Button(bar, text="存图", width=6,
                  command=self._save_overlay).pack(side="left", padx=4)
        tk.Button(bar, text="缩小", width=5,
                  command=lambda: self._zoom(0.85)).pack(side="left")
        tk.Button(bar, text="放大", width=5,
                  command=lambda: self._zoom(1.18)).pack(side="left", padx=4)

        bar2 = tk.Frame(right, bg="#16181d")
        bar2.pack(fill="x", pady=(2, 0))
        tk.Checkbutton(bar2, text="显示文字标签（会糊住原文）",
                       variable=self._show_text,
                       bg="#16181d", fg="#cbd5e1", selectcolor="#1e2128",
                       activebackground="#16181d", font=("Microsoft YaHei UI", 8),
                       activeforeground="#e2e8f0").pack(side="left")

        # 底：日志
        bottom = tk.Frame(outer, bg="#16181d")
        bottom.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        tk.Label(bottom, text="日志", bg="#16181d", fg="#94a3b8",
                 font=("Consolas", 11, "bold"), anchor="w").pack(fill="x")
        self.log = tk.Text(bottom, bg="#1e2128", fg="#cbd5e1", bd=0,
                           font=("Consolas", 9), height=8, wrap="none",
                           insertbackground="#cbd5e1")
        self.log.pack(fill="both", expand=True, pady=(4, 0))
        self.log.tag_configure("err", foreground="#ef4444")
        self.log.tag_configure("ok", foreground="#22c55e")
        self.log.tag_configure("warn", foreground="#f59e0b")
        self.log.tag_configure("head", foreground="#60a5fa")

        # 窗口尺寸：宽度按「图 + 右栏 + 边距」，高度按「图 + 日志 + 边距」。
        #
        # `self.scale` 已由 `_fit_scale()` 按屏幕可用高度算过，所以这里
        # 算出来的整窗**必然放得下屏幕** —— 不会再出现底部日志、或画面
        # 最下面那节的判定标注被裁掉的情况。
        total_w = w + RIGHT_W + 46
        total_h = h + LOG_H + 40
        self.root.geometry(f"{total_w}x{total_h}+30+10")
        self.root.minsize(520, 480)

    def _zoom(self, factor: float) -> None:
        self.scale = max(0.25, min(1.2, self.scale * factor))
        self.canvas.configure(width=int(CANVAS_W * self.scale),
                              height=int(CANVAS_H * self.scale))

    def _toggle_pause(self) -> None:
        self._paused = not self._paused
        self.btn_pause.configure(text="继续" if self._paused else "暂停")

    def _save_overlay(self) -> None:
        with self._lock:
            im = self._frame.get("overlay")
            labels = list(self._frame.get("labels") or [])
        if im is None:
            self._log("[ui] 还没有画面可存", "warn")
            return
        # 存图时把标注也烧进去（画布文字没法存），用一个自带的中文字体。
        # 找不到字体就只存几何框，并说一声 —— 不要静默丢标注。
        try:
            from PIL import ImageDraw, ImageFont
            fpath = None
            for cand in (r"C:\\Windows\\Fonts\\msyh.ttc",
                         r"C:\\Windows\\Fonts\\simhei.ttf"):
                if Path(cand).is_file():
                    fpath = cand
                    break
            if fpath:
                font = ImageFont.truetype(fpath, 13)
                d = ImageDraw.Draw(im)
                for lx, ly, ltxt, lcolor in labels:
                    d.text((lx + 2, ly), ltxt, fill=lcolor, font=font)
            else:
                self._log("[ui] 没找到中文字体，存图不含文字标注", "warn")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[ui] 烧录标注失败（只存几何框）: {exc}", "warn")
        out = paths.debug_dir() / "overlay"
        out.mkdir(parents=True, exist_ok=True)
        f = out / f"overlay-{time.strftime('%Y%m%d-%H%M%S')}.png"
        im.save(f)
        self._log(f"[ui] 已存图: {f}", "ok")

    def _on_close(self) -> None:
        self._close = True
        self.root.destroy()

    def _log(self, msg: str, tag: str = "") -> None:
        self.log.insert("end", msg + "\n", tag or ())
        self.log.see("end")

    # ---------------- 采集线程 ----------------

    def _start_worker(self) -> None:
        self._log("[dbg] 正在初始化 adb / OCR …", "head")
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self) -> None:
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
            self._log("[dbg] ✓ 就绪，开始实时刷新", "ok")
        except Exception as exc:  # noqa: BLE001 - 初始化失败要显示出来
            self._log(f"[dbg] ✗ 初始化失败: {type(exc).__name__}: {exc}", "err")
            return

        while not self._close:
            if self._paused:
                time.sleep(0.2)
                continue
            t0 = time.monotonic()
            try:
                self._grab_once(controller, tasker, JOCR, JRecognitionType)
            except Exception as exc:  # noqa: BLE001 - 单帧失败不该终止
                self._log(f"[dbg] 采集异常: {type(exc).__name__}: {exc}", "err")
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

        overlay, labels = self._draw(img_rgb, rows, circles_info, page, full_text)
        with self._lock:
            self._frame = {
                "overlay": overlay,
                "labels": labels,
                "rows": rows,
                "page": page,
                "circles": circles_info,
                "full_text": full_text,
            }

    # ---------------- 画图 ----------------

    def _draw(self, img, rows, circles_info, page, full_text):
        """画几何图形（框、检测区、顶部条），返回 (PIL图, 标注列表)。

        ## 为什么文字不在这里画

        PIL 的默认位图字体**不含中文字形**，`draw.text("已看完")` 会画成一串
        黑方块 —— 实测看到的「黑色糊块」就是这个。

        加载中文字体又很麻烦（要指定 ttf 路径、还得随包带字体）。
        所以改成：**PIL 只画几何**，文字交给 tkinter 画布的原生 `create_text`
        （用系统字体，中文天然正常，而且缩放时字不会糊）。
        """
        if not isinstance(img, Image.Image):
            # 传进来的是已经转好通道的 RGB 数组（见 `_grab_once` 里的
            # `channels.to_rgb`），直接交给 PIL 即可。
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
        # （实测存图后就是这个效果）。要看时用界面上的「显示文字标签」开关。
        for txt, x, y, w, h in rows:
            d.rectangle([x, y, x + w, y + h], outline=CLR_OCR_BOX, width=1)
            if self._show_text:
                label = txt if len(txt) <= 26 else txt[:25] + "…"
                labels.append((x, max(0, y - 15), label, "#dcffdc"))

        # 圆圈检测区：画**实际找到的**圆圈位置，而不是按固定偏移推的位置。
        # 固定偏移（老的 CIRCLE_DY=12）会随列表滚动漂到 -19，框就画偏了，
        # 这正是旧版漏判的原因之一 —— 让框跟着真实检测结果走，一眼能看出偏差。
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
        # 顶部条的中文版本交给画布叠加
        labels.append((7, 5, f"页面={exam.page_name(page)}   "
                             f"文本块={len(rows)}   圆圈={len(circles_info)}",
                       "#ffffff"))
        return im, labels

    # ---------------- 主线程刷新 ----------------

    def _tick(self) -> None:
        if self._close:
            return
        with self._lock:
            fr = dict(self._frame)
        if fr:
            # 必须自己兜住异常。tkinter 的 `after` 回调里抛异常**不会**让
            # 窗口崩，只会被 Tk 自己的报错处理吞掉 —— 表现是「界面静默不
            # 更新」，很难查。踩过一次：`f"{CircleShape:>4}"` 抛 TypeError，
            # 右栏就一直空白，界面看起来像「没识别到数据」。
            try:
                self._render(fr)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[ui] 刷新界面失败: {type(exc).__name__}: {exc}")
        self.root.after(300, self._tick)

    def _render(self, fr: dict) -> None:
        im = fr.get("overlay")
        if im is not None:
            w = int(CANVAS_W * self.scale)
            h = int(CANVAS_H * self.scale)
            photo = ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=photo)
            # 必须留引用，否则会被 GC 掉变成空白
            self.canvas.image = photo
            # 标注用**画布原生文字**画：系统字体带中文字形，
            # 而且缩放时字不会糊（PIL 默认字体画中文会变成黑块）。
            k = self.scale
            for lx, ly, ltxt, lcolor in (fr.get("labels") or []):
                self.canvas.create_text(
                    lx * k + 2, ly * k, anchor="nw", text=ltxt,
                    fill=lcolor, font=("Microsoft YaHei UI", 9))

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
                lines.append(f"  {mark} {info}  {txt[:30]}")
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


    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    DebugView().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
