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
     "已改成「识别到就做」：看课时浮层一弹出来就顺手签掉，不用等这一步。"
     "只勾它则主动去「更多」→「签到」页确认一次。",
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
            self.log(f"[data] 这是第一次用 exe 跑，已把之前攒的 {len(src)} 条题库"
                     f"搬过来，不用重新考")
            self.log(f"[data]   从: {cand}")
            self.log(f"[data]   到: {target}")

            # 顺带把配置也带过来，省得重新配 adb 路径
            old_cfg = up / "data" / "config.json"
            new_cfg = paths.config_path()
            if old_cfg.is_file() and not new_cfg.is_file():
                try:
                    shutil.copyfile(old_cfg, new_cfg)
                    self.log("[data] 顺带把之前的配置也搬过来了")
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
                self.log("[stop] 收到停止，正在打断当前这一步…")
                return
            except Exception as exc:  # noqa: BLE001 - 停止失败也要给出提示
                self.log(f"[stop] 打断没成功（仍会把停止标记设上）: {exc}")
        self.log("[stop] 已记录停止，当前这一步跑完就退出")

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
        self.log("检查 1/4：找 adb（连模拟器要用它）…")
        try:
            import detect

            adb = detect.find_adb(log=self.log)
            self.log(f"      ✓ 找到了: {adb}")
        except (FileNotFoundError, OSError) as exc:
            self.log(f"      ✗ 没找到: {exc}")
            self.log("        办法：在 data/config.json 里把 adb.adb_path "
                     "写成 adb.exe 的完整路径")
            return False

        # 2) 配置
        self.log("检查 2/4：读配置文件…")
        try:
            import paths

            cfg = load_config()
            self.log(f"      ✓ {paths.config_path()}")
            self.log(f"      模拟器地址 {cfg.get('adb', {}).get('address')}")
        except ConfigError as exc:
            self.log(f"      ✗ {exc}")
            return False

        # 3) 连接
        self.log("检查 3/4：连模拟器…")
        try:
            controller = build_controller(cfg, log=self.log)
        except (ConfigError, RuntimeError) as exc:
            self.log(f"      ✗ 连不上: {exc}")
            self.log("        办法：确认 MuMu 模拟器已经开着、没被关掉")
            return False

        # 4) 资源
        self.log("检查 4/4：载入识别用的资源（管线 + 文字识别模型）…")
        import paths
        from maa.resource import Resource

        resource = Resource()
        if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
            self.log(f"      ✗ 资源读不出来: {paths.resource_dir()}")
            return False
        self.log(f"      ✓ 载入 {len(resource.node_list)} 个流程节点")

        if not resource.post_ocr_model(str(paths.ocr_model_dir())).wait().succeeded:
            self.log(f"      ✗ 文字识别模型读不出来: {paths.ocr_model_dir()}")
            self.log("        这个目录里应该有三个文件: det.onnx / rec.onnx / keys.txt")
            return False
        self.log("      ✓ 文字识别模型就绪")

        self.log("")
        self.log("✓ 四项全过，可以点「开始运行」了。")
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
            self.log(f"[出错] 启动不了: {exc}")
            self.log("       多半是模拟器没开，或配置里的模拟器地址不对")
            return

        from maa.resource import Resource
        from maa.tasker import Tasker

        resource = Resource()
        if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
            self.log("[出错] 读不出资源目录（assets/resource）"
                     "—— 程序目录可能被挪动或删过文件")
            return
        resource.post_ocr_model(str(paths.ocr_model_dir())).wait()

        # 自定义模块（看护视频 / 弹题 / 答题）
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import main as engine

        names = engine.register_custom_modules(resource)
        self.log(f"[resource] 载入了这些自定义模块: {names}")

        tasker = Tasker()
        if not tasker.bind(resource, controller):
            self.log("[出错] 框架初始化失败（tasker.bind）—— 通常是模拟器连接断了，重开模拟器再试")
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
                    self.log("[stop] 收到停止，正在收尾…")
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
            self.log(">>> 只看护当前正在播放的视频（不换课）")
            self.log("    用法：你自己先点开想看的课，程序只负责帮你挂着看完这一节。")
            self.log("    什么时候用：想临时看某几节，不想让它把整门课都跑一遍。")
            # 「只看护当前视频」没有可预期的页面（用户可能已经在视频页），
            # 所以不做前置检查，只跑节点。
            self._run_node(tasker, "唤起播放器控件")

        elif key == "course":
            self.log("")
            self.log(">>> 整门课轮播（把一门课里所有视频都看完）")
            self.log("    它会自动做这些事：")
            self.log("      1. 进「我的学习」，打开第一门课")
            self.log("      2. 把课程目录从上到下列出来")
            self.log("      3. 一节一节地看；一节播到结尾就自动点下一节")
            self.log("      4. 中途弹「每日签到」，会顺手签掉再关掉它")
            self.log("")
            self.log("    ⚠ 模拟器窗口要一直开着、别最小化，最小化视频就暂停了。")
            self.log("    ⚠ 跑的时候别去点模拟器，你点的会和程序点的打架。")
            self.log("    ⚠ 一节 45~60 分钟，是真实播放时间，快进无效；")
            self.log("      整门课可能要几个小时，挂着就行。")
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
            self.log("    说明：这个浮层每天、每门课点进去都会弹，还会盖住底下的题目，")
            self.log("    所以程序在任何一步只要看到它，都会先处理掉再继续。")
            self.log("    已经签到过的，它只会把浮层关掉，不会重复点。")
            self.log("    怎么确认签到了：按钮上的字变成「已经签到」才算成功，")
            self.log("    不是点了就算。")
            self._run_step(tasker, "每日签到", want=_course())

        elif key == "exam":
            self.log(">>> 进入考核并答题")
            self.log("    它会自动做这些事：")
            self.log("      1. 切到「更多」，点「考核」")
            self.log("      2. 在考核说明页点底部「进入答题」，进到答题页")
            self.log("      3. 题库里有的题直接答；没把握的先随便选一个")
            self.log("      4. 交卷后从结果页把官方正确答案抄下来，下一轮就会答了")
            self.log("")
            self.log("    ⚠ 有硬门槛：视频没全部学完时，点「开始答题」只会看到")
            self.log("      「请先完成课程视频学习，再进行考核！」")
            self.log("      所以想考的话，得先让「整门课轮播」把所有课看完。")
            self.log("")
            self.log("    想单独跑一整份卷子（不干别的），用命令行：")
            self.log("      MaaElearning.exe --run run_full_exam")
            # ⚠️ 这里踩过一个很绕的坑：节点名以前写的是「进入考核」，
            # 而那个节点的职责**只到考核页为止**（点完考核图标就结束），
            # 「点底部进入答题按钮」那一段挂在 `进入考核并答题` 上。
            # 于是程序停在说明页不动 —— 而日志里看起来「节点成功了」。
            #
            # want 保持 `_course()`：`进入考核并答题` 的**第一步**是从课程页
            # 切「更多」→ 点「考核」，所以起点确实该在课程页。
            #
            # 曾经想过把它改成 `PAGE_EXAM_ENTRY`（「这一步该在考核页」），
            # 那是错的：用户重跑时页面可能**已经**停在考核页，此时前置检查
            # 发现「不在课程页」→ 按底部「学习」把它拽回课程页 → 再走一遍
            # 进考核 → 又回到考核页、又被拽回来，来回打转。真实事故就是这样：
            # 程序一直在「课程页 ↔ 考核说明页」之间弹，就是不去点那个按钮。
            #
            # 所以判据要认**起点**，交付物交给 `check_after`。
            self._run_step(tasker, "进入考核并答题", want=_course(),
                           check_after=exam.PAGE_ANSWER,
                           retry_click=exam.ENTER_BUTTON_TAP)

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
            self.log(f"[guard] 截图/识别失败，这一步先跳过: {exc}")
        return []

    def _page_now(self, tasker) -> str:
        """当前页面类型。"""
        import exam

        rows = self._screen_rows(tasker)
        return exam.detect_page(" ".join(r[0] for r in rows))

    def _on_site(self, tasker) -> bool:
        """现在是否**还在继续教育平台里**（而不是掉回微信自己的界面）。"""
        import exam

        try:
            rows = self._screen_rows(tasker)
        except Exception:  # noqa: BLE001 - 读不到就当未知
            return True
        if not rows:
            # 读不到屏幕信息时**保守返回 True**：宁可不按返回键，
            # 也不要因为「读不到」就把微信顶出去。
            return True
        return exam.on_site(" ".join(r[0] for r in rows), rows)

    def _tap(self, x: int, y: int) -> None:
        """在画布坐标上点一下（走控制器的 post_click，不是 adb shell input）。"""
        ctrl = getattr(self, "_controller", None)
        if ctrl is None:
            return
        ctrl.post_click(int(x), int(y)).wait()

    def _click_until(self, tasker, x: int, y: int, expect: str,
                     tries: int = 3, gap: float = 2.5) -> bool:
        """点 `(x, y)` 直到页面变成 `expect`。

        ## 为什么"点一次然后祈祷"不够（用户实测反馈）

        用户原话（m06507）：「还是卡在进入答题的页面」。

        平台底部那个「进入答题」是个 **WebView 里的固定定位按钮**。
        实测它有三种点不动的方式，而且都不报错：
          1. 那一下落在页面还没渲染完的空白上（WebView 首帧）；
          2. 被上层浮层（每日签到／弹题）吃掉；
          3. 点到了，但页面要几秒才切，而检查紧接着就做了。
        点一次就往下走，这三种都会表现为「停在原地不动」。

        所以这里改成：点一下 → 等页面 → 没变就**再点一下**，最多 `tries` 次。
        坐标是实测死的（说明页蓝色按钮包围盒 x 12..709 y 1232..1270、
        中心 (360,1251)、三张截图跨度 0px），重复点是幂等的 ——
        真进去了按钮就没了，多点一下不会点坏什么。

        三次都点不动 → 把整屏文本和坐标 dump 出来再放弃。这个 dump 是
        刻意加的：用户报「卡住」而我在自己这边**复现不出来**，没有现场
        文本就只能猜。有了它，下次日志里直接能看出是「按钮根本没出现」
        还是「按钮在、坐标不对」。
        """
        import exam

        for i in range(1, max(1, tries) + 1):
            self.log(f"[step] 兜底点击 ({x},{y}) —— 第 {i}/{tries} 次")
            self._tap(x, y)
            time.sleep(gap)
            self._handle_checkin(tasker)
            now = self._page_now(tasker)
            if now == expect:
                self.log(f"[step] ✓ 兜底点击生效，已经到「{exam.page_name(expect)}」")
                return True
            self.log(f"[step]   点完还是「{exam.page_name(now)}」")
        self._dump_screen(tasker)
        return False

    def _dump_screen(self, tasker) -> None:
        """把当前屏的文本和坐标打出来 —— 卡住时唯一能定位原因的东西。"""
        rows = self._screen_rows(tasker)
        if not rows:
            self.log("[dump] 读不到屏幕（截图或 OCR 失败）")
            return
        self.log(f"[dump] 卡住时屏幕上共 {len(rows)} 个文本块，按 y 从上到下：")
        for txt, x, y, w, h in sorted(rows, key=lambda r: (r[2], r[1])):
            self.log(f"[dump]   ({x:4d},{y:4d}) [{w:3d}x{h:3d}] {txt}")
        self.log("[dump] ↑ 把这段发给我就能定位：是要点的按钮没出现、"
                 "还是按钮在但坐标不对")

    def _safe_back(self, tasker) -> None:
        """按一次返回 —— **只在还站在平台页面里时才按**。

        ## 为什么要加这道门（用户实测反馈）

        用户原话：「能不能把退出微信的代码删了，每次都要重新打开」。

        先说结论：程序里**没有**任何 `force-stop` / `StopApp` / 关闭微信的代码
        （`scripts/` 与 pipeline JSON 里都没有）。把人顶出微信的是**这个返回键**：

        KEYCODE_BACK 是「当前 activity 的返回」，它**不认页面**。
        当平台页面已经不在前台时（比如已被顶掉、或用户自己关了 WebView），
        这一下按的就成了**微信自己的返回** —— 从聊天列表退回桌面，
        看起来就是「微信被程序关掉了」。而微信的 WebView 会话一旦丢掉，
        程序**没有任何办法**把它恢复（那要微信自己的会话），
        只能人工重进，也就是用户说的「每次都要重新打开」。

        ## 现在的规矩

        按之前先确认「还在站内」；不在站内就**什么都不做**。
        代价是「万一真需要退出某一层」时这一步退不动 —— 但那种情况
        下一步的页面检查会发现并如实报告，比把微信顶掉好得多。
        """
        if not self._on_site(tasker):
            self.log("[guard] 现在不在平台页面里（像是退回微信了），"
                     "不按返回键 —— 按了会把微信顶出去，而你得手动重进")
            return
        try:
            ctrl = getattr(self, "_controller", None)
            if ctrl is not None:
                ctrl.post_click_key(4).wait()      # KEYCODE_BACK
                time.sleep(2.5)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[guard] 按手机返回键没成功: {exc}")

    def _recover_to(self, tasker, want: str, tries: int = 3) -> bool:
        """把页面弄回 `want`。先按底部「学习」，不行再退一次返回键。"""
        import exam

        for i in range(1, tries + 1):
            if self._page_now(tasker) == want:
                return True
            self.log(f"[guard] 第 {i} 次尝试回到「{exam.page_name(want)}」"
                     f"（按底部「学习」）")
            try:
                ctrl = getattr(self, "_controller", None)
                if ctrl is not None:
                    # 底部 tab 行的中心按 OCR 实测是 (450,1262)
                    ctrl.post_click(450, 1262).wait()
                    time.sleep(3.0)
            except Exception:  # noqa: BLE001
                pass
            if self._page_now(tasker) == want:
                return True
            if i == 2:
                self.log("[guard] 按「学习」没回去，再按一次手机返回键试试")
                self._safe_back(tasker)

        if self._page_now(tasker) == want:
            self.log(f"[guard] ✓ 回到「{exam.page_name(want)}」了")
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
            self.log(f"[guard] ✓ 确认停在「{exam.page_name(want)}」，继续")
            return True
        if now == exam.PAGE_QUIZ_POPUP:
            self.log("[guard] 屏幕上有一道视频弹题挡着（视频是暂停的），先不往下做")
            return False

        self.log(f"[guard] ⚠ 页面不对：期望 {exam.page_name(want)}，"
                 f"实际 {exam.page_name(now)}")
        if self._recover_to(tasker, want):
            return True
        self.log(f"[guard] ✗ 回不到「{exam.page_name(want)}」，这一步跳过"
                 f"（多半是有人手动动了页面；下一轮会自动重来）")
        return False

    def _run_step(self, tasker, entry: str, want: str,
                  fallbacks: tuple[str, ...] = (),
                  check_after: str = "",
                  retry_click: tuple[int, int] | None = None) -> bool:
        """**带页面前置检查**地跑一个节点。

        want        : 这一步应该在哪一页执行。不对就先回这一页；回不去就跳过。
        fallbacks   : 主节点失败时依次再试的节点名。
        check_after : 跑完后期望到哪一页；不符就告警。
        retry_click : `(x, y)` —— 跑完页面**没到** `check_after` 时，
                      在这个坐标上重试点击直到页面变过去（最多 3 次）。
                      只给「目标是一个固定位置按钮」的步骤用（目前只有
                      「进入考核并答题」那个底部按钮）。节点链里其实也有
                      坐标兜底节点，但那个只点**一次**、点空就没了；
                      实测那一次是会被 WebView 吃掉的，所以这里再补一层。
        """
        import exam

        self.log("")
        self.log(f"[step] 做「{entry}」——它应该在「{exam.page_name(want)}」上做")
        self._handle_checkin(tasker)
        if not self._ensure_page(tasker, want):
            self.log(f"[step] ✗ 现在不在该在的页面，跳过「{entry}」")
            self.log("[step]   原因多半是：上一步没做成，或者你自己动了模拟器页面")
            return False

        ok = self._run_node(tasker, entry)
        if not ok and fallbacks:
            for fb in fallbacks:
                self.log(f"[step]「{entry}」没成功，换「{fb}」再试一次")
                self._handle_checkin(tasker)
                ok = self._run_node(tasker, fb)
                if ok:
                    break

        if ok and check_after:
            time.sleep(2.0)
            self._handle_checkin(tasker)
            now = self._page_now(tasker)
            if now != check_after and retry_click:
                self.log(f"[step] ⚠ 跑完了，但页面没来到"
                         f"「{exam.page_name(check_after)}」（实际 "
                         f"{exam.page_name(now)}）—— 在最下面那个按钮上重试点击")
                if self._click_until(tasker, retry_click[0], retry_click[1],
                                     check_after):
                    return True
                now = self._page_now(tasker)
            if now != check_after:
                self.log(f"[step] ⚠ 跑完了，但页面没来到「{exam.page_name(check_after)}」"
                         f"（实际 {exam.page_name(now)}）")
            else:
                self.log(f"[step] ✓ 成功，已经到「{exam.page_name(check_after)}」")
        return ok

    def _handle_checkin(self, tasker) -> bool:
        """看到「每日签到」浮层就顺手签掉。

        ## 为什么是「每一步之前查一次」而不是一个任务步骤

        用户实测指出：这个浮层**每天每门课点进去都会弹**，而且会**盖住底下的
        视频弹题**（点输入框点到浮层、点提交点到浮层的 X），后续所有点击都被
        它吃掉。

        原先它被排成 `TASKS` 里的一个**顺序步骤**，位置在「整门课轮播」**后面** ——
        轮播一跑几小时，期间浮层弹出来只会被点 X 关掉、**不真的签到**。

        用户要求改成「识别到就做，而不是先后顺序执行」。所以这里做成幂等的
        前置处理，挂在每一步之前；识别不到就什么都不做，零开销。
        """
        import checkin

        try:
            if not checkin.has_popup(self._screen_text(tasker)):
                return False
            return checkin.handle_popup(
                self._controller, lambda: self._screen_text(tasker), self.log)
        except Exception as exc:  # noqa: BLE001 - 签到失败绝不能中断看课
            self.log(f"[checkin] 处理签到浮层出错（忽略，继续）: {exc}")
            return False

    def _screen_text(self, tasker) -> str:
        """当前整屏文本拼在一起，用于「有没有某个浮层」这类判断。"""
        try:
            rows = self._screen_rows(tasker)
            return "".join(r[0] for r in rows)
        except Exception:  # noqa: BLE001
            return ""

    def _run_node(self, tasker, entry: str) -> bool:
        self.log(f"[task] 开始: {entry}")
        job = tasker.post_task(entry).wait()
        if job.succeeded:
            self.log(f"[task] 完成: {entry}")
            return True
        self.log(f"[task] {entry} 没能确认做完 —— 不一定出错，也可能只是没识别到")
        return False
