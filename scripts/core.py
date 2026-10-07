"""启动器后端：环境检查与任务执行。

从 `launcher.py`（旧 tkinter 界面）里抽出来，供新界面 `launcher_ui.py` 复用。

## 为什么要抽

原来「检查环境」「跑任务」的逻辑和 tkinter 控件缠在一起，换界面就得重写一遍，
两套实现必然漂移。这里把**行为**集中下来，界面只负责显示：

* `AppCore.run_env_check()`  —— 检查 adb / 配置 / 连接 / 资源
* `AppCore.run_tasks(keys)`  —— 按顺序跑选中的任务
* `core.stop()`              —— 请求停止（在节点边界生效）
* `core.log(...)`            —— 日志出口，界面自己决定怎么渲染

线程模型：调用方负责把这两个方法放到后台线程里跑；
本模块**不碰任何界面**，只通过 `log` 回调输出，所以可测试。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

# 任务定义集中在这里，界面按它渲染，避免两处各写一份
# (键, 标题, 说明, 默认是否勾选, 是否危险/耗时)
TASKS: tuple[tuple[str, str, str, bool, bool], ...] = (
    ("course", "整门课轮播",
     "逐个播放目录，看完自动点下一节。单课 45~60 分钟真实时间，快进无效。",
     True, True),
    ("checkin", "每日签到",
     "已经签到会自动跳过，不重复点。用按钮文案确认结果，不只看点击有没有发出。",
     True, False),
    ("exam", "进入考核并答题",
     "切「更多」→ 点「考核」→ 开始答题。视频没学完时平台会拦住。",
     True, False),
    ("watch", "只看护当前视频",
     "不切课，只帮你把当前正在播的那一课看完。调试用。",
     False, False),
)


class AppCore:
    """不含界面的业务逻辑。"""

    def __init__(self, log=None) -> None:
        self._log = log or (lambda msg="": None)
        self.stop_flag = threading.Event()
        #: 正在跑的 Tasker。`stop()` 用它调 `post_stop()` 真正中断节点。
        #: 为 None 表示当前没有任务在跑。
        self._tasker = None

    # ---------------- 日志 ----------------

    def log(self, msg: str = "") -> None:
        self._log(str(msg))

    # ---------------- 首次启动：接管已有数据 ----------------

    def adopt_existing_data(self) -> int:
        """把「源码版攒下的题库」搬进 exe 的数据目录。返回搬了几条。

        ## 为什么需要

        打包后 `app_root()` 是 **exe 所在目录**，而源码运行时是项目根。
        所以 exe 首次启动看到的是一个空题库——哪怕你之前已经用源码版
        攒了几十条官方答案。用户升级后会发现「题库怎么空了」，
        然后白白多考几轮才能补回来。

        做法：若当前题库为空，就往上找找有没有旧的数据目录
        （exe 在 dist/MaaElearning 时，项目根在 ../../），有就复制过来。
        **只在当前题库为空时搬**，绝不覆盖已有的。
        """
        import json
        import shutil

        import paths

        target = paths.answer_cache_path()
        try:
            if target.is_file():
                data = json.loads(target.read_text(encoding="utf-8-sig") or "{}")
                if isinstance(data, dict) and data:
                    return 0        # 已有题库，不覆盖
        except (json.JSONDecodeError, OSError):
            pass

        # 候选：exe 目录的上两层（dist/MaaElearning → 项目根）
        here = paths.app_root()
        for up in (here.parent.parent, here.parent, here):
            cand = up / "data" / "answer_cache.json"
            if not cand.is_file() or cand == target:
                continue
            try:
                src = json.loads(cand.read_text(encoding="utf-8-sig") or "{}")
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(src, dict) or not src:
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cand, target)
            self.log(f"[data] 已接管既有题库: {len(src)} 条")
            self.log(f"[data]   {cand}")
            self.log(f"[data]   → {target}")

            # 顺带把配置也带过来，省得重新配 adb 路径
            old_cfg = up / "data" / "config.json"
            new_cfg = paths.config_path()
            if old_cfg.is_file() and not new_cfg.is_file():
                try:
                    shutil.copyfile(old_cfg, new_cfg)
                    self.log("[data] 已接管既有配置")
                except OSError:
                    pass
            return len(src)

        return 0

    # ---------------- 停止 ----------------

    def stop(self) -> None:
        """请求停止，并**尽量中断正在跑的节点**。

        ## 为什么要额外调 `tasker.post_stop()`

        只设 `stop_flag` 时，那个标记只在**任务之间**检查。而「播放整门课」
        是**一个**要跑几小时的节点，所以：

            点停止 → 什么也没发生 → 视频继续播到下课前

        实测用户反馈就是「没有停止运行按钮」（按钮在、点了没用）。

        `tasker.post_stop()` 让框架在当前识别/动作边界退出，所以现在
        点停止是**几秒内**生效，而不是几十分钟。
        """
        self.stop_flag.set()
        tasker = getattr(self, "_tasker", None)
        if tasker is not None:
            try:
                tasker.post_stop()
                self.log("[stop] 已请求停止，并通知框架中断当前任务")
                return
            except Exception as exc:  # noqa: BLE001 - 停止失败也要给出提示
                self.log(f"[stop] 通知框架失败（仍会设标记）: {exc}")
        self.log("[stop] 已请求停止，当前节点跑完就会退出")

    @property
    def stopped(self) -> bool:
        return self.stop_flag.is_set()

    def begin(self) -> None:
        """开始新一轮前清掉停止标记。"""
        self.stop_flag.clear()

    # ---------------- 环境检查 ----------------

    def run_env_check(self) -> bool:
        """检查 adb / 配置 / 连接 / 资源。返回是否全部通过。"""
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
            return False

        # 2) 配置
        self.log("[2/4] 读取配置…")
        try:
            import paths

            cfg = load_config()
            self.log(f"      ✓ {paths.config_path()}")
            self.log(f"      模拟器地址 {cfg.get('adb', {}).get('address')}")
        except ConfigError as exc:
            self.log(f"      ✗ {exc}")
            return False

        # 3) 连接
        self.log("[3/4] 连接模拟器…")
        try:
            controller = build_controller(cfg, log=self.log)
        except (ConfigError, RuntimeError) as exc:
            self.log(f"      ✗ {exc}")
            return False

        # 4) 资源
        self.log("[4/4] 加载资源与 OCR 模型…")
        import paths
        from maa.resource import Resource

        resource = Resource()
        if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
            self.log(f"      ✗ 资源加载失败: {paths.resource_dir()}")
            return False
        self.log(f"      ✓ {len(resource.node_list)} 个管线节点")

        if not resource.post_ocr_model(str(paths.ocr_model_dir())).wait().succeeded:
            self.log(f"      ✗ OCR 模型加载失败: {paths.ocr_model_dir()}")
            self.log("        需要 det.onnx / rec.onnx / keys.txt")
            return False
        self.log("      ✓ OCR 模型就绪")

        self.log("")
        self.log("✓ 环境检查全部通过，可以开始运行了。")
        return True

    # ---------------- 运行任务 ----------------

    def run_tasks(self, keys: list[str]) -> None:
        """按顺序执行选中的任务。"""
        from controller import ConfigError, build_controller, load_config

        import paths

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

        # 自定义模块（看护视频 / 弹题 / 答题）
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

        # 记下来，`stop()` 要靠它**中断正在跑的节点**。
        # 只设 stop_flag 是不够的：那个标记只在任务之间检查，而
        # 「播放整门课」是**一个**跑几小时的节点，点了停止要等整门课
        # 跑完才生效——等于停不下来。实测用户反馈就是这个。
        self._tasker = tasker
        # _screen_rows 要截图，需要 controller
        self._controller = controller

        try:
            for key in keys:
                if self.stopped:
                    self.log("[stop] 收到停止请求，中止")
                    return
                self._run_one(tasker, key)
        finally:
            self._tasker = None

        self.log("")
        self.log("✓ 所选任务执行完毕")
        self.log(f"  截图与落盘证据在: {paths.debug_dir()}")

    def _run_one(self, tasker, key: str) -> None:
        """跑单个任务。每个任务前面打印它自己的注意事项。"""
        import exam

        _learn = lambda: exam.PAGE_LEARNING_LIST   # noqa: E731
        _course = lambda: exam.PAGE_COURSE         # noqa: E731

        if key == "watch":
            self.log("")
            self.log(">>> 只看护当前正在播放的视频（不切课）")
            self.log("    适合你先手动点开某一课，让程序帮你挂着看完。")
            # 「只看护当前视频」没有可预期的页面（用户可能已经在视频页），
            # 所以不做前置检查，只跑节点。
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
            # 每一步都先确认在哪一页，不对就先回到该在的页面；
            # 回不去就**跳过**并说清楚，不要盲目往下点。
            self._run_step(tasker, "进入我的学习", want=_learn(),
                           check_after=_learn())
            self._run_step(tasker, "点第一个去学习", want=_learn(),
                           check_after=_course())
            self._run_step(tasker, "播放整门课", want=_course())

        elif key == "checkin":
            self.log("")
            self.log(">>> 每日签到")
            self.log("")
            self.log("    实测要点：签到按钮 adb shell input tap 点不动，")
            self.log("    但 MaaTouch 一次命中。程序走的就是 MaaTouch。")
            self.log("    已签到时会自动跳过，不会重复点。")
            self.log("    签到结果用「按钮文案是否变成『已经签到』」确认，")
            self.log("    而不是只看点击有没有发出去。")
            self._run_step(tasker, "每日签到", want=_course())

        elif key == "exam":
            self.log("")
            self.log(">>> 进入考核并答题")
            self.log("    流程：切「更多」tab → 点「考核」图标 → 开始答题")
            self.log("")
            self.log("    ⚠ 平台有硬门槛：视频没学完时点「开始答题」会提示")
            self.log("      「请先完成课程视频学习，再进行考核！」")
            self.log("")
            self.log("    题库命中就直接答；没把握的会先随便选，")
            self.log("    交卷后从结果页采集官方正确答案 —— 这样下一轮就对了。")
            self.log("    想跑完整卷请用: MaaElearning.exe --run run_full_exam")
            self._run_step(tasker, "进入考核", want=_course())

    # ---------------- 带页面前置检查的节点执行 ----------------

    def _screen_rows(self, tasker) -> list:
        """当前整屏 OCR。返回 [(text, x, y, w, h)]，拿不到返回空列表。"""
        try:
            from maa.pipeline import JOCR, JRecognitionType

            ctrl = getattr(self, "_controller", None)
            if ctrl is None:
                return []
            job = ctrl.post_screencap().wait()
            if not job.succeeded:
                return []
            j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), job.get())
            if not j.wait().succeeded:
                return []
            td = j.get()
            if td is None:
                return []
            for nid in td.node_id_list:
                nd = tasker.get_node_detail(nid)
                if nd is not None and nd.recognition is not None:
                    out = []
                    for r in (nd.recognition.all_results or []):
                        box = getattr(r, "box", None)
                        txt = getattr(r, "text", None)
                        if box and txt:
                            out.append((str(txt), int(box[0]), int(box[1]),
                                        int(box[2]), int(box[3])))
                    return out
        except Exception as exc:  # noqa: BLE001 - 读不到就当未知，别中断
            self.log(f"[guard] 读屏失败: {exc}")
        return []

    def _page_now(self, tasker) -> str:
        """当前页面类型。"""
        import exam

        rows = self._screen_rows(tasker)
        return exam.detect_page(" ".join(r[0] for r in rows))

    def _safe_back(self, tasker) -> None:
        """按一次返回。

        **只按一次**是刻意为之：实测反复按 BACK 会把微信 WebView 的宿主
        页面推光，落到空白页，看起来像「微信自己退出了」。
        """
        try:
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.post_click_key(4).wait()      # KEYCODE_BACK
                time.sleep(2.5)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[guard] 按返回失败: {exc}")

    def _recover_to(self, tasker, want: str, tries: int = 3) -> bool:
        """把页面弄回 `want`。先按「学习」tab，不行再谨慎退一次。"""
        import exam

        for i in range(1, tries + 1):
            if self._page_now(tasker) == want:
                return True
            self.log(f"[guard] 第 {i} 次尝试回到 {exam.page_name(want)}")
            try:
                ctrl = getattr(self, "_controller", None)
                if ctrl is not None:
                    ctrl.post_click(449, 1262).wait()   # 底部「学习」tab
                    time.sleep(3.0)
            except Exception:  # noqa: BLE001
                pass
            if self._page_now(tasker) == want:
                return True
            if i == 2:
                self.log("[guard] tab 没到，谨慎退一次返回")
                self._safe_back(tasker)

        if self._page_now(tasker) == want:
            self.log(f"[guard] ✓ 已回到 {exam.page_name(want)}")
            return True
        return False

    def _ensure_page(self, tasker, want: str) -> bool:
        """确保当前在 `want` 页才继续。

        ## 为什么必须有这个

        用户实测反馈：「为什么进行每一步时没有识别当前页面，我在后一步时
        点开了前一步的页面，但它没有识别到，反而继续下一步了」。

        原因就是 `_run_node` 原先**无脑跑管线节点**：

            job = tasker.post_task(entry).wait()

        管线的入口多为 `DirectHit`（必命中）+ 无条件点击，所以页面不对时
        它照样点，点到完全无关的地方。

        `verify.py` 里那套纠错机制是有的，但**只有 `run_exam_watch.py`
        用了，UI 这条路径（core.py）一直没用上** —— 这就是缺口。
        """
        import exam

        now = self._page_now(tasker)
        if now == want:
            self.log(f"[guard] ✓ 页面对：{exam.page_name(want)}")
            return True
        if now == exam.PAGE_QUIZ_POPUP:
            self.log("[guard] 当前有视频弹题挡着（它会暂停视频），先不继续")
            return False

        self.log(f"[guard] ⚠ 页面不对：期望 {exam.page_name(want)}，"
                 f"实际 {exam.page_name(now)}")
        if self._recover_to(tasker, want):
            return True
        self.log(f"[guard] ✗ 回不到 {exam.page_name(want)}，跳过这一步")
        return False

    def _run_step(self, tasker, entry: str, want: str,
                  fallbacks: tuple[str, ...] = (),
                  check_after: str = "") -> bool:
        """**带页面前置检查**地跑一个节点。

        want        : 这一步应该在哪一页执行。不对就先回这一页；回不去就跳过。
        fallbacks   : 主节点失败时依次再试的节点名。
        check_after : 跑完后期望到哪一页；不符就告警（不自动重跑，
                      因为这类节点常有副作用，重跑更危险）。
        """
        import exam

        self.log("")
        self.log(f"[step] {entry}（需要在 {exam.page_name(want)}）")
        if not self._ensure_page(tasker, want):
            self.log(f"[step] ✗ 前置条件不满足，跳过 {entry}")
            self.log("[step]   提示：这通常说明上一步没做到位，或有人手动改了页面")
            return False

        ok = self._run_node(tasker, entry)
        if not ok and fallbacks:
            for fb in fallbacks:
                self.log(f"[step] {entry} 没成功，改试 {fb}")
                ok = self._run_node(tasker, fb)
                if ok:
                    break

        if ok and check_after:
            time.sleep(2.0)
            now = self._page_now(tasker)
            if now != check_after:
                self.log(f"[step] ⚠ 跑完了但不在 {exam.page_name(check_after)}"
                         f"（实际 {exam.page_name(now)}）")
            else:
                self.log(f"[step] ✓ 已到 {exam.page_name(check_after)}")
        return ok

    def _run_node(self, tasker, entry: str) -> bool:
        self.log(f"[task] {entry}")
        job = tasker.post_task(entry).wait()
        if job.succeeded:
            self.log(f"[task] {entry} 完成")
            return True
        self.log(f"[task] {entry} 未成功结束（不一定是错误，可能是识别未命中）")
        return False
