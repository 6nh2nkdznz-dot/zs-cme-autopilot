"""中山医院继续教育平台助手 —— 图形启动器。

这个文件是打包成 exe 的入口。设计原则：

* **不依赖命令行**：所有交互在窗口里，日志实时滚动到文本框；
* **环境检查前置**：连不上模拟器就明确说哪一步不对，而不是抛一堆栈；
* **任务可多选**：视频 / 签到 / 问卷各自独立；
* **人工介入不吃死**：需要人工确认的题目落盘到 debug/pending，并在日志里
  给出明确提示，而不是在后台线程里等 stdin（GUI 下没有 stdin，会永久卡住）。

线程模型：tkinter 不是线程安全的，所以自动化跑在后台线程，
日志通过 queue 回传，主线程用 after() 轮询刷新。
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from pathlib import Path


def _bootstrap_path() -> None:
    """把 scripts 目录挂进 sys.path。

    源码运行时是 <项目根>/scripts；打包后 PyInstaller 把它放在
    <bundle>/scripts（见 build.spec 的 datas）。两种都要能import。
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

import tkinter as tk  # noqa: E402
from tkinter import messagebox, scrolledtext, ttk  # noqa: E402

import paths  # noqa: E402

APP_TITLE = "中山医院继续教育平台助手"
APP_VER = "0.2.0"


class QueueLogger:
    """把 print 风格的输出塞进队列，交给主线程渲染。"""

    def __init__(self, q: "queue.Queue[str]") -> None:
        self.q = q

    def __call__(self, msg: str = "") -> None:
        self.q.put(str(msg))

    def write(self, msg: str) -> None:
        if msg:
            self.q.put(msg.rstrip("\n"))

    def flush(self) -> None:
        pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.log = QueueLogger(self.log_q)
        self.worker: threading.Thread | None = None
        self.stop_flag = threading.Event()

        root.title(f"{APP_TITLE} v{APP_VER}")
        root.geometry("860x620")
        root.minsize(720, 520)

        self._build_ui()
        self._pump_log()
        self._show_env_summary()

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")

        ttk.Label(
            top,
            text=f"{APP_TITLE}  v{APP_VER}",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left")

        # 任务选择
        box = ttk.LabelFrame(self.root, text="要执行的任务", padding=(10, 6))
        box.pack(fill="x", padx=10, pady=(0, 6))

        self.var_course = tk.BooleanVar(value=True)
        self.var_checkin = tk.BooleanVar(value=True)
        self.var_watch = tk.BooleanVar(value=False)

        ttk.Checkbutton(box, text="整门课轮播（逐个播放，看完自动点下一节）",
                        variable=self.var_course).grid(row=0, column=0, columnspan=2,
                                                       sticky="w", padx=(0, 18))
        ttk.Checkbutton(box, text="每日签到",
                        variable=self.var_checkin).grid(row=0, column=2, sticky="w")
        ttk.Checkbutton(box, text="只看护当前正在播放的视频（不切课，调试用）",
                        variable=self.var_watch).grid(row=1, column=0, columnspan=3,
                                                      sticky="w", pady=(4, 0))

        self.var_exam = tk.BooleanVar(value=True)
        ttk.Checkbutton(box,
                        text="学完后进入考核并答题（需先在「更多」里解锁）",
                        variable=self.var_exam).grid(row=2, column=0, columnspan=3,
                                                     sticky="w", pady=(4, 0))

        # 按钮
        btns = ttk.Frame(self.root, padding=(10, 0))
        btns.pack(fill="x")

        self.btn_check = ttk.Button(btns, text="① 检查环境", command=self.on_check)
        self.btn_check.pack(side="left")

        self.btn_run = ttk.Button(btns, text="② 开始运行", command=self.on_run)
        self.btn_run.pack(side="left", padx=6)

        self.btn_stop = ttk.Button(btns, text="停止", command=self.on_stop, state="disabled")
        self.btn_stop.pack(side="left")

        ttk.Button(btns, text="打开日志目录", command=self.on_open_logs).pack(side="right")
        ttk.Button(btns, text="打开数据目录", command=self.on_open_data).pack(side="right", padx=6)

        # 日志
        logbox = ttk.LabelFrame(self.root, text="运行日志", padding=(6, 4))
        logbox.pack(fill="both", expand=True, padx=10, pady=(6, 10))

        self.txt = scrolledtext.ScrolledText(
            logbox, wrap="word", height=20,
            font=("Consolas", 9), state="disabled",
        )
        self.txt.pack(fill="both", expand=True)

        self.status = ttk.Label(self.root, text="就绪", anchor="w", padding=(12, 0))
        self.status.pack(fill="x", side="bottom")

    # ---------------- 日志 ----------------

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
            self.txt.insert("end", "\n".join(lines) + "\n")
            self.txt.see("end")
            self.txt.configure(state="disabled")

        self.root.after(120, self._pump_log)

    def _show_env_summary(self) -> None:
        self.log("=" * 62)
        self.log(f" {APP_TITLE} v{APP_VER}")
        self.log("=" * 62)
        self.log(paths.describe())
        self.log("")
        self.log("提示：先点「① 检查环境」。它会验证 adb 路径、模拟器连接和资源加载。")
        self.log("     一切正常再点「② 开始运行」。")
        self.log("")

    # ---------------- 动作 ----------------

    def on_open_logs(self) -> None:
        import os

        d = paths.log_dir()
        os.startfile(d)  # type: ignore[attr-defined]

    def on_open_data(self) -> None:
        import os

        os.startfile(paths.app_root())  # type: ignore[attr-defined]

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.btn_check.configure(state=state)
        self.btn_run.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")
        self.status.configure(text="运行中…" if busy else "就绪")

    def _start_worker(self, fn, name: str) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(APP_TITLE, "已有任务在运行，请先停止。")
            return
        self.stop_flag.clear()
        self._set_busy(True)

        def wrapper() -> None:
            try:
                fn()
            except Exception as exc:  # 后台线程的异常必须回传，否则界面像卡死
                self.log("")
                self.log(f"[ERROR] {name} 失败: {exc}")
                self.log(traceback.format_exc())
            finally:
                self.log_q.put("__DONE__")

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()
        self.root.after(200, self._watch_done)

    def _watch_done(self) -> None:
        if self.worker and self.worker.is_alive():
            self.root.after(300, self._watch_done)
            return
        # 排空剩余的 __DONE__ 标记
        self._set_busy(False)

    # ---------------- 环境检查 ----------------

    def on_check(self) -> None:
        self._start_worker(self._do_check, "环境检查")

    def _do_check(self) -> None:
        from controller import ConfigError, build_controller, load_config

        self.log("—" * 62)
        self.log("开始环境检查")
        self.log("")

        # 1) adb —— 全自动探测，不假设安装目录
        self.log("[1/4] 自动探测 adb…")
        try:
            import detect

            adb = detect.find_adb(log=self.log)
            self.log(f"      ✓ {adb}")
        except (FileNotFoundError, OSError) as exc:
            self.log(f"      ✗ {exc}")
            self.log("        可在 data/config.json 的 adb.adb_path 里写死完整路径")
            return

        # 2) 配置
        self.log("[2/4] 读取配置…")
        try:
            cfg = load_config()
            self.log(f"      ✓ {paths.config_path()}")
            self.log(f"      模拟器地址 {cfg.get('adb', {}).get('address')}")
        except ConfigError as exc:
            self.log(f"      ✗ {exc}")
            return

        # 3) 连接
        self.log("[3/4] 连接模拟器…")
        try:
            controller = build_controller(cfg, log=self.log)
        except (ConfigError, RuntimeError) as exc:
            self.log(f"      ✗ {exc}")
            return

        # 4) 资源
        self.log("[4/4] 加载资源与 OCR 模型…")
        from maa.resource import Resource

        resource = Resource()
        if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
            self.log(f"      ✗ 资源加载失败: {paths.resource_dir()}")
            return
        self.log(f"      ✓ {len(resource.node_list)} 个管线节点")

        if not resource.post_ocr_model(str(paths.ocr_model_dir())).wait().succeeded:
            self.log(f"      ✗ OCR 模型加载失败: {paths.ocr_model_dir()}")
            self.log("        需要 det.onnx / rec.onnx / keys.txt")
            return
        self.log("      ✓ OCR 模型就绪")

        self.log("")
        self.log("✓ 环境检查全部通过，可以点「② 开始运行」了。")

    # ---------------- 运行 ----------------

    def on_run(self) -> None:
        tasks: list[str] = []
        # 整门课轮播要排在「只看护当前视频」前面：
        # 后者只是挂着看，先跑轮播才符合预期。两者同时勾选时也提醒一下。
        if self.var_course.get():
            tasks.append("course")
        if self.var_exam.get():
            tasks.append("exam")
        if self.var_checkin.get():
            tasks.append("checkin")
        if self.var_watch.get():
            if self.var_course.get():
                if not messagebox.askyesno(
                    APP_TITLE,
                    "你同时勾选了「整门课轮播」和「只看护当前视频」。\n\n"
                    "整门课轮播会自己切课，之后再「只看护当前视频」"
                    "只会看护那时正在播的一课。\n\n继续吗？",
                ):
                    return
            tasks.append("watch")

        if not tasks:
            messagebox.showinfo(APP_TITLE, "请至少勾选一个任务。")
            return
        self._start_worker(lambda: self._do_run(tasks), "运行任务")

    def _do_run(self, tasks: list[str]) -> None:
        from controller import ConfigError, build_controller, load_config

        self.log("—" * 62)
        self.log("开始运行")
        self.log("")

        try:
            cfg = load_config()
            controller = build_controller(cfg, log=self.log)
        except (ConfigError, RuntimeError) as exc:
            self.log(f"[FATAL] {exc}")
            return

        from maa.resource import Resource
        from maa.tasker import Tasker

        resource = Resource()
        if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
            self.log("[FATAL] 资源加载失败")
            return
        resource.post_ocr_model(str(paths.ocr_model_dir())).wait()

        # 自定义模块（看护视频 / 弹题 / 进度）
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import main as engine

        names = engine.register_custom_modules(resource)
        self.log(f"[resource] 自定义模块: {names}")

        tasker = Tasker()
        if not tasker.bind(resource, controller):
            self.log("[FATAL] tasker.bind 失败")
            return
        tasker.set_log_dir(str(paths.log_dir()))
        tasker.set_save_draw(True)
        tasker.set_save_on_error(True)

        for key in tasks:
            if self.stop_flag.is_set():
                self.log("[stop] 收到停止请求，中止")
                return

            if key == "watch":
                self.log("")
                self.log(">>> 只看护当前正在播放的视频（不切课）")
                self.log("    适合你先手动点开某一课，让程序帮你挂着看完。")
                self._run_node(tasker, "唤起播放器控件")

            elif key == "course":
                self.log("")
                self.log(">>> 整门课轮播")
                self.log("    流程：进入我的学习 → 打开第一门课 → 枚举目录")
                self.log("          → 逐个播放，每个看完自动点下一节")
                self.log("")
                self.log("    ⚠ 模拟器窗口必须保持打开且不要最小化，否则视频会暂停。")
                self.log("    ⚠ 期间不要手动操作模拟器，你的点击会和程序的打架。")
                self.log("    ⚠ 单课 45~60 分钟真实时间，快进无效。整门课可能几小时。")
                self.log("")
                self._run_node(tasker, "进入我的学习")
                self._run_node(tasker, "点第一个去学习")
                # 这个节点内部完成「枚举目录 → 逐个看护 → 自动切下一节」
                self._run_node(tasker, "播放整门课")

            elif key == "checkin":
                self.log("")
                self.log(">>> 每日签到")
                self.log("")
                self.log("    实测要点：签到按钮 adb shell input tap 点不动，")
                self.log("    但 MaaTouch 一次命中。程序走的就是 MaaTouch，")
                self.log("    所以这个按钮现在是能签上的。")
                self.log("")
                self.log("    已签到时会自动跳过，不会重复点。")
                self.log("    签到结果用「按钮文案是否变成『已经签到』」来确认，")
                self.log("    而不是只看点击有没有发出去。")
                self._run_node(tasker, "每日签到")

            elif key == "exam":
                self.log("")
                self.log(">>> 进入考核并答题")
                self.log("    流程：切「更多」tab → 点「考核」图标 → 开始答题")
                self.log("")
                self.log("    ⚠ 平台有硬门槛：视频没学完时点「开始答题」会提示")
                self.log("      「请先完成课程视频学习，再进行考核！」")
                self.log("      所以「整门课轮播」没跑完时，这一步进不去。")
                self.log("")
                self.log("    注意：答题的识别区域需要按真实题目版式配置。")
                self.log("    没配好时会在日志里提示，并把拿不准的题挂起等你作答。")
                self._run_node(tasker, "进入考核")

        self.log("")
        self.log("✓ 所选任务执行完毕")
        self.log(f"  截图与落盘证据在: {paths.debug_dir()}")

    def _run_node(self, tasker, entry: str) -> None:
        self.log(f"[task] {entry}")
        job = tasker.post_task(entry).wait()
        if job.succeeded:
            self.log(f"[task] {entry} 完成")
        else:
            self.log(f"[task] {entry} 未成功结束（这不一定是错误，可能是识别未命中）")

    def on_stop(self) -> None:
        self.stop_flag.set()
        self.log("[stop] 已请求停止，当前节点跑完就会退出")


def _selftest() -> int:
    """无界面自检：验证打包后的路径解析、资源、OCR、存图链路。

    GUI 程序没法用肉眼在一次构建里验证所有环节，所以留一个命令行模式。
    打包后运行 `MaaElearning.exe --selftest` 即可。
    """
    def emit(msg: str = "") -> None:
        print(msg)

    ok = True

    emit("=" * 60)
    emit(" 自检模式")
    emit("=" * 60)
    emit(paths.describe())
    emit()

    # 1) 关键路径
    emit("[1/6] 检查打包路径…")
    checks = [
        ("资源包", paths.resource_dir()),
        ("管线目录", paths.pipeline_dir()),
        ("OCR 模型", paths.ocr_model_dir()),
        ("默认配置", paths.default_config_path()),
        ("Agent 二进制", paths.agent_binary_dir()),
    ]
    for label, p in checks:
        exists = p.exists()
        emit(f"      {'✓' if exists else '✗'} {label}: {p}")
        ok = ok and exists

    cfg = paths.config_path()
    emit(f"      {'✓' if cfg.is_file() else '✗'} 生效配置: {cfg}")

    # 2) 存图降级链
    emit()
    emit("[2/6] 检查存图能力…")
    try:
        import numpy as np

        from imageio_util import save_png

        probe = np.zeros((8, 8, 3), dtype=np.uint8)
        probe[:, :, 1] = 128
        out = paths.snap_dir() / "_selftest.png"
        save_png(probe, out)
        emit(f"      ✓ 存图成功: {out} ({out.stat().st_size} 字节)")
        out.unlink(missing_ok=True)
    except Exception as exc:
        emit(f"      ✗ 存图失败: {exc}")
        ok = False

    # 3) adb 自动探测
    emit()
    emit("[3/6] 自动探测 adb…")
    adb_path = None
    try:
        import detect

        adb_path = detect.find_adb(log=emit)
        emit(f"      ✓ {adb_path}")
    except (FileNotFoundError, OSError) as exc:
        emit(f"      ✗ {exc}")
        ok = False

    # 4) 模拟器端点探测（不假设端口）
    emit()
    emit("[4/6] 探测模拟器端点…")
    if adb_path is None:
        emit("      - 跳过（上一步没有可用 adb）")
    else:
        try:
            import detect

            addr = detect.find_device(adb_path, log=emit)
            emit(f"      ✓ {addr}")
        except ConnectionError as exc:
            emit(f"      ✗ {exc}")
            ok = False

    # 5) 资源与管线
    emit()
    emit("[5/6] 加载资源与管线…")
    try:
        from maa.resource import Resource

        res = Resource()
        if res.post_bundle(str(paths.resource_dir())).wait().succeeded:
            emit(f"      ✓ {len(res.node_list)} 个管线节点")
        else:
            emit("      ✗ 资源加载失败")
            ok = False

        if res.post_ocr_model(str(paths.ocr_model_dir())).wait().succeeded:
            emit("      ✓ OCR 模型加载成功")
        else:
            emit("      ✗ OCR 模型加载失败")
            ok = False
    except Exception as exc:
        emit(f"      ✗ {exc}")
        ok = False

    # 5) 控制器连接
    emit()
    emit("[6/6] 连接模拟器…")
    try:
        from controller import build_controller, load_config

        build_controller(load_config(), log=emit)
    except Exception as exc:
        emit(f"      ✗ {exc}")
        ok = False

    emit()
    emit("=" * 60)
    emit(" 自检结果: " + ("全部通过 ✓" if ok else "存在问题 ✗（见上面标 ✗ 的项）"))
    emit("=" * 60)
    return 0 if ok else 1


def _run_script(name: str, argv: list[str]) -> int:
    """在 exe 里直接跑某个打包进来的脚本。

    用法:
        MaaElearning.exe --list                      列出可跑的脚本
        MaaElearning.exe --run run_full_exam         跑完整卷
        MaaElearning.exe --run harvest_answers --max 16
        MaaElearning.exe --run pending -- list       透传参数给脚本

    ## 为什么要这个

    打包只把「项目模块」收进 hiddenimports 还不够——那些脚本本身是
    独立 CLI，用户不能只靠 GUI 复选框用到它们（比如采答案、补全未答、
    迁移题库这些一次性操作）。与其为每个操作做一个按钮，
    不如给一个通用入口，脚本更新了也不用改 exe。

    实现方式是 import 目标模块并调用它的 `main()`，
    所以脚本之间共享同一份 paths / 配置解析逻辑，行为与源码运行一致。
    """
    import io
    import runpy

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    scripts_dir = None
    for cand in (Path(__file__).resolve().parent / "scripts",
                 Path(__file__).resolve().parent):
        if (cand / f"{name}.py").is_file():
            scripts_dir = cand
            break

    if scripts_dir is None:
        print(f"[run] 找不到脚本: {name}.py")
        print("[run] 用 --list 看有哪些可跑")
        return 1

    target = scripts_dir / f"{name}.py"
    print(f"[run] 执行 {target.name} 参数={argv}")

    # 让脚本自己解析参数：把剩余参数塞回 sys.argv
    saved = sys.argv
    sys.argv = [str(target)] + list(argv)
    try:
        # runpy 会正常执行脚本的 __main__ 块（那里是 raise SystemExit(main())）
        runpy.run_path(str(target), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = saved
    return 0


def _list_scripts() -> int:
    """列出打包进来的可跑脚本。"""
    import io

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    here = Path(__file__).resolve().parent
    for cand in (here / "scripts", here):
        if cand.is_dir() and any(cand.glob("*.py")):
            print(f"可用脚本（{cand}）：")
            for f in sorted(cand.glob("*.py")):
                print(f"  {f.stem}")
            print()
            print("用法: MaaElearning.exe --run <脚本名> [参数...]")
            return 0
    print("找不到 scripts 目录")
    return 1


def main() -> int:
    """统一入口：默认开新界面，其余交给命令行开关。

    为什么入口放这里而不是 launcher_ui：
    `--selftest` / `--run` / `--list` 这些无界面开关本来就实现在本文件，
    而 launcher_ui 只负责画窗口。让本文件做分发，职责更清楚，
    也避免把参数处理复制两份。

    实测踩过：把入口换成 launcher_ui 后 `--selftest` 直接开出了窗口、
    在无界面环境里挂死——因为那个文件根本不看 sys.argv。
    """
    # 无界面自检模式（打包后用来验证 frozen 路径）
    if "--selftest" in sys.argv:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                pass
        return _selftest()

    # 通用脚本入口
    if "--list" in sys.argv:
        return _list_scripts()
    if "--run" in sys.argv:
        i = sys.argv.index("--run")
        rest = sys.argv[i + 1:]
        if not rest:
            print("用法: MaaElearning.exe --run <脚本名> [参数...]")
            print("      MaaElearning.exe --list   看有哪些脚本")
            return 1
        return _run_script(rest[0], rest[1:])

    # 界面：默认新 UI，--classic 用旧的 tkinter 界面
    if "--classic" in sys.argv:
        return _classic_ui()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    try:
        import launcher_ui

        return launcher_ui.main()
    except Exception as exc:  # noqa: BLE001 - 新 UI 起不来时退回旧界面
        print(f"[warn] 新界面启动失败（{exc}），改用经典界面")
        import traceback

        traceback.print_exc()
        return _classic_ui()


def _classic_ui() -> int:
    """旧的 tkinter 界面。保留作为兜底（新 UI 依赖 customtkinter）。"""
    root = tk.Tk()
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
