"""看护一门课：导航 → 进入课程 → 逐个看护全部课节。

## 和 `run_full_exam.py` 的分工

* `run_exam_watch.py`（本文件）——**看课**
* `run_full_exam.py` ——**考试**

分开是因为课程看护动辄几小时，中途还需要「识别出错就回主页重来」的
恢复逻辑；而考试是几分钟一轮的短循环。混在一起会让两边的状态机都变复杂。

## 恢复策略（用户指定）

> 识别到东西有误的话，那么就回到主页重新看课

所以每节课开看之前，先校验「我还在课程页」；一旦发现页面不对
（被弹窗顶掉、误跳到结果页、点击没生效……），就
`recover_to_home()` 回主页，再重新走一遍导航进课程。

**已看过的课不会白看**：`course_progress` 记着哪些课节已达标，
重来时直接跳过。

## 为什么用 subprocess 之外的方式

之前的教训（`run_full_exam.py`）：自己建 Resource 却忘了注册自定义模块，
框架只报 `recognition is null`，很难联想到。所以这里统一走
`main.register_custom_modules()`。

用法:
    python scripts\\run_exam_watch.py --dry-run     # 只看当前在哪、列表首项是谁
    python scripts\\run_exam_watch.py                # 看护第一门课
    python scripts\\run_exam_watch.py --max-lessons 1
    python scripts\\run_exam_watch.py --forget       # 清空本地课程进度后重来
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import app_recover  # noqa: E402
import exam  # noqa: E402
import main as engine  # noqa: E402
import paths  # noqa: E402
import verify  # noqa: E402
from controller import ConfigError, build_controller, load_config  # noqa: E402
from course_progress import CourseProgress  # noqa: E402
from ocr_text import rows_to_text  # noqa: E402


def _topmost_learn_button_y(rows) -> int | None:
    """列表里最靠上的那个「去学习」按钮的 y。"""
    ys = [y for t, _x, y in rows if "去学习" in (t or "")]
    return min(ys) if ys else None


def _name_above(rows, btn_y: int) -> str:
    """取某个「去学习」按钮**上方**那一段里最长的中文文本，即卡片标题。

    实测卡片布局（自上而下）：
        标题(y=284) → 时长(y=322) → 过期时间(y=379) → 按钮行(y=461)

    标题在按钮上方约 177px，所以窗口取 (btn_y-220, btn_y-60)：
    既框得住标题，又不会碰到上一张卡的按钮行。
    """
    best = ""
    for text, _x, y in rows:
        if not (btn_y - 220 <= y <= btn_y - 60):
            continue
        t = " ".join((text or "").split())
        if len(t) < 5 or not any("\u4e00" <= c <= "\u9fff" for c in t):
            continue
        # 「19小时」「过期时间：永久有效」这类不是课程名
        if any(k in t for k in ("小时", "过期", "永久", "有效",
                                "申请", "报名", "结课")):
            continue
        if len(t) > len(best):
            best = t
    return best


_LOG_FH = None
#: 内建 print 的引用。覆盖前先抓住，避免递归。
_BUILTIN_PRINT = print
#: 原始的 sys.stdout，供 Tee 转发
_ORIG_STDOUT = sys.stdout


class _Tee:
    r"""把写入同时送到「独占日志文件」和原始 stdout。

    ## 为什么必须替换 `sys.stdout` 而不只是模块的 `print`

    早先只做 `globals()["print"] = _print`，那只影响**本模块命名空间**的
    `print`。而 MaaFramework 回调（`WatchCourse` 等）里的 `print` 用的是
    **内建 `print`**，它写 `sys.stdout` —— 于是回调的日志跑去了框架那个
    文件（`fw.log`），我的看护日志反而停更：

        watch_course.log: 194s 前      ← 本该是最活跃的
        fw.log:            3s 前       ← 回调日志跑到这里了

    替换 `sys.stdout` 才能覆盖所有写入方（包括被回调调用的模块）。
    原始 stdout 仍然转发，免得丢掉框架自己的 C++ 日志。
    """

    def __init__(self, primary, mirror):
        self._primary = primary
        self._mirror = mirror

    def write(self, s):
        try:
            self._primary.write(s)
            self._primary.flush()
        except (OSError, ValueError):
            pass
        try:
            self._mirror.write(s)
            self._mirror.flush()
        except (OSError, ValueError):
            pass
        return len(s)

    def flush(self):
        for f in (self._primary, self._mirror):
            try:
                f.flush()
            except (OSError, ValueError):
                pass

    def isatty(self) -> bool:
        return False

    def reconfigure(self, **kw):        # 兼容有人调 reconfigure
        pass


def _tee_own_log(path: str = "") -> None:
    r"""把日志同时写进**自己独占的**文件和原始 stdout。

    ## 为什么需要独占文件

    早先靠 stdout 重定向。但 **MaaFramework 的 C++ 日志也往同一个文件写**，
    两边按各自的偏移写，中间留下 NUL 空洞（实测 862 字节，是文件尾部一整块）。
    所以 Python 自己 `open(..., "a", buffering=1)` 拿独占句柄。

    ## 踩过的坑（严重）

    覆盖 `print` 时 `_print` 内部**又调用了 `print`** —— 那一刻 `print`
    已经是 `_print` 自己，于是无限递归：

        RecursionError: maximum recursion depth exceeded
          File "run_exam_watch.py", line 122, in _print
            print(*a, **kw)

    栈爆掉时文件被截断留下 NUL 空洞，**看护进程整个崩了而日志看不出来**。
    所以先用模块级的 `_BUILTIN_PRINT`/`_Tee`，不再自引用。
    """
    global _LOG_FH, _ORIG_STDOUT
    if not path:
        return
    try:
        _LOG_FH = open(path, "a", encoding="utf-8", buffering=1, errors="replace")
    except OSError:
        _LOG_FH = None
        return

    # 替换 sys.stdout —— 这样连框架回调里的内建 print 也会写进日志
    sys.stdout = _Tee(_LOG_FH, _ORIG_STDOUT)


def _make_stdout_line_buffered() -> None:
    """stdout 每次 print 立刻落盘（框架日志也走 stdout 时的兜底）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):
            pass


#: 兜底选课程名时要排除的界面/机构文案。
#:
#: 实测踩过：兜底用「整屏最长中文文本」选出了
#: `复旦大学附属中山医院远程教育` —— 比课程名还长，于是整轮进度都记到这个
#: 假名字下（`done_count` 显示 0 节、已看的课也照样重看）。
_NAME_BLOCKLIST = (
    "学习", "课程", "小时", "过期", "永久", "有效", "申请", "报名", "结课",
    "签到", "我的", "全部", "未结课", "已结课", "去学习", "复旦大学",
    "中山医院", "远程医学", "远程教育", "Zhongshan", "Hospital", "University",
    "简介", "目录", "更多", "详情", "首页", "返回", "确定", "取消",
)


def _longest_chinese(rows, btn_y: int = 0, min_len: int = 5) -> str:
    """兜底认课程名：只挑「第一张卡的按钮**之上**」的文本。

    ## 为什么不能用「整屏最长」

    实测两个失败模式：

    * 选出 `复旦大学附属中山医院远程教育`（页头机构名，比课程名还长）
    * 选出 `肝胆肿瘤整合治疗与肝移植全程...`（**第二张卡**的标题）

    第二个尤其危险：那是**另一门课**的名字，记到它名下会让两门课的进度
    混在一个键里。

    ## 结构性规则

    卡片是自上而下的，所以第一门课的标题一定在**它自己那个「去学习」按钮
    的上方**，且在上一张卡的按钮下方。只在这个区间里挑，再排除界面/机构
    文案（`_NAME_BLOCKLIST`），取其中最长的一条。
    """
    if btn_y <= 0:
        # 没给按钮位置时退化为「整屏最长」——调用方应尽量传
        upper = 10 ** 9
    else:
        upper = btn_y - 20          # 按钮上方 20px 起算

    best = ""
    for text, _x, y in rows:
        if y >= upper:
            continue                # 只看到按钮上方
        t = " ".join((text or "").split())
        if len(t) < min_len or not any("\u4e00" <= c <= "\u9fff" for c in t):
            continue
        if any(k in t for k in _NAME_BLOCKLIST):
            continue
        if len(t) > len(best):
            best = t
    return best


def main() -> int:
    _make_stdout_line_buffered()

    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="", help="本脚本的日志文件（独占写，避免 NUL 空洞）")
    ap.add_argument("--dry-run", action="store_true", help="只看状态，不操作")
    ap.add_argument("--max-lessons", type=int, default=0, help="最多看几节（0=不限）")
    ap.add_argument("--forget", action="store_true", help="清空本地课程进度后重来")
    ap.add_argument("--max-recover", type=int, default=4, help="最多恢复几次")
    args = ap.parse_args()

    # 拿到自己的日志句柄后再开始输出。
    # 框架的 C++ 日志走 stdout，由外面重定向到别的文件——两边不碰同一句柄。
    _tee_own_log(args.log)

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    resource.post_bundle(str(paths.resource_dir())).wait()
    resource.post_ocr_model(str(paths.ocr_model_dir())).wait()

    registered = engine.register_custom_modules(resource)
    print(f"[watch] 自定义模块: {registered}")

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    # ---------------- 基础原语 ----------------

    def ocr_rows() -> list[tuple[str, int, int]]:
        img = controller.post_screencap().wait().get()
        j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
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
                        out.append((str(txt), int(box[0]), int(box[1])))
                return out
        return []

    def read_state() -> str:
        return exam.detect_page(rows_to_text(ocr_rows()))

    def click(x, y, wait=1.2):
        controller.post_click(int(x), int(y)).wait()
        time.sleep(wait)

    def back():
        """按一次返回 —— **只在还站在平台页面里时才按**。

        和 `core.py` 的 `_safe_back` 同一个道理（那边有完整说明）：
        KEYCODE_BACK 不认页面，平台页面已经不在前台时这一下按的
        就成了微信自己的返回 —— 从聊天列表退回桌面，看起来像
        「微信被程序关掉了」，而微信的 WebView 会话丢了就恢复不了。
        """
        rows = ocr_rows()
        text = " ".join(str(r[0]) for r in rows)
        if rows and not exam.on_site(text, rows):
            print("[watch] 现在不在平台页面里，不按返回键"
                  "（按了会把微信顶出去）")
            return
        controller.post_click_key(4).wait()
        time.sleep(2.5)

    def learning_tab():
        click(450, 1262, wait=3.0)

    def run_node(entry: str, timeout: float = 0.0,
                 note: str = "", override: dict | None = None) -> bool:
        """跑一个管线节点，**带超时**，并且在等待期间看护微信。

        ## 为什么必须自己实现超时

        `TaskJob.wait()` 是**无限阻塞**的（绑定里没有超时参数，实测确认：
        `def wait(self) -> "Job"` 只调 `self._wait_func(job_id)`）。
        一旦管线里某个节点因为 `[JumpBack]` 或反复重试而迟迟不收尾，
        调用方就永远回不来——日志停在"执行节点 X"之后再无输出。

        对看课这种几小时的任务，卡住而不自知是最糟的情况。所以这里改成
        用 `job.done` 轮询，超时就**放弃等待并继续**（把 job 丢在后台），
        让上层有机会走恢复逻辑而不是干等。

        timeout=0 表示不限制（只有明确知道节点会长时间运行时才这么用，
        例如「播放整门课」本身就是几小时）。

        ## 为什么轮询里要顺便查微信

        「不限时」的那个分支原来是 `job.wait()` —— **一次都查不了**。
        而它正是最长的那个节点（几小时）。微信要是中途崩了，
        `播放整门课` 会一路识别失败却仍然「在跑」，日志里一句原因都没有。
        所以两条路径**都**改成轮询，每轮顺手 `crash_watchdog()` 一次：
        崩了就当场说出来，别等几小时后再靠猜。
        """
        print(f"[watch] 执行节点 {entry} {note}"
              + (f"（上限 {timeout:.0f}s）" if timeout else "（不限时）"))

        job = tasker.post_task(entry, pipeline_override=override or {})

        baseline = len(app_recover.crashes(limit=200))
        waited = 0.0
        step = 2.0
        while True:
            if job.done:
                ok = bool(job.succeeded)
                suffix = f"（{waited:.0f}s）" if timeout else ""
                print(f"[watch] {'✓' if ok else '✗'} {entry}{suffix}")
                return ok

            if timeout and waited >= timeout:
                print(f"[watch] ⏱ {entry} 超过 {timeout:.0f}s 仍未结束 → 放弃等待")
                print("[watch]    节点可能仍在后台跑；上层会走恢复逻辑")
                return False

            time.sleep(step)
            waited += step

            # 每 30s 查一次就够：查一次要跑一条 adb，太密会拖慢看课本身。
            if waited % 30 == 0:
                baseline = app_recover.crash_watchdog(
                    baseline=baseline, log=print)

    def go_home() -> bool:
        """回到「我的学习」列表。

        ## 为什么要先按 back

        `verify.recover_to_home` 会先试底部「学习」tab。但**在课程页上按
        这个 tab 不生效** —— 课程页把 tab 栏盖住了/吃掉了点击。实测：

            点「学习」tab → 仍是课程页
            点「学习」tab → 仍是课程页
            [recover] tab 没到，改用 back

        它最后会退到 back，但那要浪费两次无效点击和等待。既然我们知道
        课程页必须先用 back 离开，就先按一次，再交给通用恢复逻辑。

        ## 为什么第一步是「捞微信」而不是「回主页」

        恢复的前提是**微信还活着**。实测（2026-10-07，一天 17 次）微信在
        看课过程中会被自己的视频解码器崩掉：

            Fatal signal 11 (SIGSEGV) ... in tid 4534 (MediaCodec_loop),
            pid 2207 (com.tencent.mm)
            #01 /system/lib64/libstagefright.so (android::MediaCodec::setState)

        微信一没，后面每一步识别都会失败，`recover_to_home` 也会一路
        「tab 没到、back 也没用」，最后报一个和真因毫无关系的错。
        所以先 `crash_recovery()`：**先把原因读出来（logcat 里的崩溃记录），
        再把微信拉回来**，然后才谈得上回主页。
        """
        # 崩了就先把微信捞回来 —— 顺序不能反。
        app_recover.crash_recovery(log=print)

        # 转横了也回不去 —— 横屏下每个坐标都是错的，
        # 而且框架不会重算缩放，继续点只会把页面点到别处去。
        if app_recover.ensure_portrait(log=print) is False:
            return False

        if read_state() == exam.PAGE_COURSE:
            print("[watch] 在课程页，先按 back 退出（tab 在这里不生效）")
            back()
            time.sleep(2)
        return verify.recover_to_home(
            read_state=read_state,
            log=print,
            tap_back=back,
            tap_learning_tab=learning_tab,
            home_page=exam.PAGE_LEARNING_LIST,
        )

    def _screen_text() -> str:
        """整屏 OCR 文本。拿不到就返回空串（调用方按「认不出来」处理）。"""
        try:
            return " ".join(t for t, _x, _y in ocr_rows())
        except Exception:  # noqa: BLE001
            return ""

    def _report_not_on_site() -> None:
        """不在网站里时的报错提示。

        要给出**能照着做**的信息，而不是只说「出错了」：
        告诉用户屏幕上读到的是什么、可能什么原因、下一步怎么做。
        """
        txt = _screen_text()
        print("")
        print("!" * 66)
        print("  ✗ 没有进入继续教育平台，已停止")
        print("!" * 66)
        print("")
        print("  期望：画面能认出平台页面（我的学习 / 课程页 / 答题页…）")
        print("        或者看到站点域名 zs-hospital.sh.cn")
        print("")
        if txt:
            shown = txt if len(txt) <= 160 else txt[:159] + "…"
            print(f"  实际读到的画面文字: {shown}")
        else:
            print("  实际读到的画面文字: （一个字都没读到 —— 可能是白屏/黑屏）")
        print("")
        print("  常见原因:")
        print("    1. 微信不在前台（退到了聊天列表，或被别的应用盖住）")
        print("    2. 微信闪退了 —— 程序会自动尝试拉起，但可能失败")
        print("    3. 平台登录掉了，停在登录页")
        print("    4. 网络断了，页面没加载出来")
        print("")
        print("  怎么处理:")
        print("    · 手动把微信切到前台，打开「中山医院继续教育平台」")
        print("    · 停在「我的学习」列表页，再点开始")
        print("    · 想先看看屏幕到底长什么样，跑:")
        print("        MaaElearning.exe --run debug_view")
        print("")

    # ---------------- 状态 ----------------

    prog = CourseProgress(paths.data_dir() / "course_progress.json")
    if args.forget:
        n = prog.clear()
        print(f"[watch] 已清空本地课程进度（{n} 条），本次会重看")

    state = read_state()
    print("=" * 66)
    print(" 看护一门课")
    print("=" * 66)
    print(f"当前页面: {exam.page_name(state)}")
    print(f"本地课程进度: {paths.data_dir() / 'course_progress.json'}")

    if args.dry_run:
        print("\n[dry-run] 不操作")
        return 0

    # ---------------- 导航 + 看护 ----------------

    # 用可变容器把「动作到底成不成」传给 verify_ok。
    #
    # 踩过的坑：这里原本写 `verify_ok=lambda: True`，等于**不管动作结果
    # 一律判定成功**。于是日志里明明写着「进入第一门课失败或超时」，
    # 最后却报告「结果: 完成」。这正是本项目反复犯的错——
    # 判定不看真实结果。所以结果必须真的从动作传出来。
    outcome = {"ok": False}

    def navigate_and_watch() -> bool:
        """确保在课程页，然后看护全部课节。

        三种起点都要能处理：
          * 已在课程页     → 直接开看，**不要**白跑一趟回主页
          * 在学习列表页   → 点第一张卡的「去学习」
          * 其它页面       → 先回主页，再走上面那条
        """
        where = read_state()
        # 课程名放在可变容器里，由 click_first_course 就地写入。
        #
        # 为什么不用 nonlocal：那要求它在 click_first_course 里声明，
        # 而那个函数定义在后面，读起来容易误解。容器最直白。
        # 关键点是「读到名字」和「点下按钮」必须来自**同一次 OCR**——
        # 实测踩过：两次读之间发生滚动，名字是老年认知症的、点开的却是肝胆肿瘤。
        name_box = {"name": ""}

        # 开跑前记一行运行环境（abi/sdk/board）。
        #
        # 动机很实际：查「微信为什么自己退出」时，一半时间花在反推
        # 环境上（是不是 x86_64、渲染后端是什么）。而这些恰恰是决定性的
        # —— 崩溃就出在 x86_64 模拟器的媒体栈上。
        app_recover.describe_environment(log=print)

        # **横竖屏必须在这里拦一次。**
        #
        # 实测（2026-10-07 19:40 那轮）：跑到一半设备自己转成横屏
        # （`SurfaceOrientation: 1`、1920x1080），我们用的画布还是
        # 720x1280，于是课程目录**一节课都认不出来**，日志里只留一句
        # 「屏幕上没找到任何视频条目」——看起来像「目录页读错了」，
        # 其实是屏幕转了。而且框架不会重算缩放，转回来之前每一步都错。
        if app_recover.ensure_portrait(log=print) is False:
            print("[watch] 屏幕转不回来，这一轮不跑 —— 横屏下所有坐标都是错的")
            outcome["ok"] = False
            return False

        # **先识别当前在哪一页，就地接着做**，不要无脑从头再来。
        #
        # 用户反馈：「先识别当前页面，不要从头再来」。之前我为了解决
        # 「课程名读不到」的问题，写成了**总是先回列表**——过度纠正，
        # 变成用户手动点进某节课后一按开始就被拽回主页，很烦。
        #
        # 正确做法：分情况。
        #   * 已在课程页 → **就地开始看护**，课程名从已记进度里反查
        #     （课程页/视频页顶部都读不到课程名，实测确认）
        #   * 在列表页   → 点名进课（顺便拿到课程名）
        #   * 其它页面   → 回列表再走上面那条
        where = read_state()

        # **不在网站里就报错停手**，不要继续瞎点。
        #
        # 实测动机：用户停在微信首页 / 退到聊天列表 / 登录掉了的时候，
        # detect_page 只返回「未知页面」，程序会对着微信界面一通乱点，
        # 既没用又可能误触别的东西。
        #
        # 判据见 exam.on_site：认得出已知页面 或 画面里有站点域名。
        if where == exam.PAGE_UNKNOWN and not exam.on_site(_screen_text()):
            outcome["ok"] = False
            _report_not_on_site()
            return False

        if where == exam.PAGE_COURSE:
            print("[watch] 已在课程页 → 就地开始看护（不回列表、不重新进课）")
            name_box["name"] = prog.guess_course_name()
            if name_box["name"]:
                print(f"[watch] 课程名（从已记进度反查）: {name_box['name']}")
            else:
                print("[watch] ⚠ 反查不到课程名，本轮不记进度"
                      "（不影响看课，只是下次可能重看）")
            return _watch_all(name_box)

        if where != exam.PAGE_LEARNING_LIST:
            if not go_home():
                outcome["ok"] = False
                return False

        if read_state() != exam.PAGE_LEARNING_LIST:
            print(f"[watch] 回列表失败（实际 {exam.page_name(read_state())}）")
            outcome["ok"] = False
            return False

        # ⚠️ 这一段**必须在 if 块外面**。
        #
        # 踩过的坑：上一版把它们缩进进了上面那个 `if` 里面，而那个分支以
        # `return False` 收尾 —— 于是**点击进入课程这一步永远不会执行**，
        # 代码直接从「我的学习」列表页去跑「播放整门课」，在列表页上枚举
        # 课程目录当然一条都识别不到：
        #
        #     [watch] 执行节点 播放整门课
        #     [course] 第 1 屏：识别到 0 条 …
        #     [course] 没识别到任何视频条目，中止
        #
        # 教训：重构导航流程后**必须重新跑一遍**，光看 diff 发现不了
        # 缩进错误（语法还是合法的）。
        #
        # 分两步走，而不是跑「点第一个去学习」整条链：
        # 那条链包含「等课程页 → 关签到弹窗 → 等视频列表」，只要其中一环
        # 没命中就会一直重试，实测超过 180s 都不收尾
        # （`TaskJob.wait()` 无限阻塞，只能靠外部超时兜）。
        if not click_first_course(name_box):
            outcome["ok"] = False
            return False
        if read_state() != exam.PAGE_COURSE:
            print(f"[watch] 点完之后不在课程页"
                  f"（实际 {exam.page_name(read_state())}）")
            outcome["ok"] = False
            return False
        print("[watch] ✓ 已进入课程页")
        return _watch_all(name_box)

    def _watch_all(name_box: dict) -> bool:
        """看护整门课（假定**已经在课程页**）。

        两条入口共用：从列表点进来、以及本来就在课程页就地开始。
        """
        # 认不出课节时，最省时间的排查手段就是先看方向对不对 ——
        # 横屏会让「一节课都认不出来」这件事看起来像「目录页读错了」。
        course_name = name_box["name"]
        if app_recover.canvas_portrait() is False:
            print("[watch] ⚠ 屏幕已经是横屏了 —— 课程目录一定认不出来。")
            app_recover.ensure_portrait(log=print)
            outcome["ok"] = False
            return False
        if course_name:
            print(f"[watch] 本次课程: {course_name}"
                  f"（本地已记录 {prog.done_count(course_name)} 节）")
        else:
            print("[watch] ⚠ 没能认出课程名，本轮不会记录进度"
                  "（不影响看课，只是下次可能重看）")

        # 看护整门课。这个节点**本身就要跑几小时**（每节课真实时长，
        # 快进无效），所以不限时——它的内部循环自己有进度输出。
        # 把课程名覆盖进管线参数，让 WatchCourse 能记进度。
        override = {}
        if course_name:
            override["播放整门课"] = {
                "custom_action_param": {"course_name": course_name},
            }
        ok = run_node("播放整门课", timeout=0,
                      note="（整门课，可能数小时）", override=override)
        outcome["ok"] = ok
        return ok

    def scroll_list_to_top() -> None:
        """把「我的学习」列表滚回顶部，**并确认真的到顶了**。

        ## 为什么要确认

        原实现固定滑 6 次就认为到顶。实测列表比这更长（3 门课、每张卡约
        160~300px），6 次不够，于是「最上面可见的卡片」并不是真正的第一门课。
        实测后果：把**第三门课**（分子诊断新技术临床转化）当成了第一门，
        课程名也跟着错（抓到了页头机构名），整轮进度都记到错误的课程名下。

        所以改成：滑到**位置连续两次不再变化**才算到顶，并设上限防死循环。
        """
        last = None
        for i in range(14):
            rows = ocr_rows()
            ys = [y for t, _x, y in rows if "去学习" in (t or "")]
            fingerprint = min(ys) if ys else None
            if fingerprint is not None and fingerprint == last:
                print(f"[watch] 列表已到顶（第 {i} 次滑动后，"
                      f"最上「去学习」y={fingerprint}）")
                return
            last = fingerprint
            controller.post_swipe(360, 400, 360, 1150, 300).wait()
            time.sleep(0.7)
        print("[watch] ⚠ 滑了多次仍未确认到顶，继续")



    def click_first_course(name_box: dict) -> bool:
        """滚到列表顶部，**同一次 OCR** 里取第一张卡的课程名和按钮位置，点它。

        ## 踩过的坑（两个都得一起修）

        1. **课程名和按钮坐标取自两次 OCR** → 一次滚动就能让它们错位。
           实测：日志显示捕获到的课程名是「老年认知症…」（对的、还知道已看 1 节），
           点下去却打开了「肝胆肿瘤」那门课——因为名字来自滚动前那一屏、
           坐标来自滚动后那一屏。

        2. **没先回列表顶部** → 列表是虚拟列表，上次操作会把它留在中间位置，
           那时「最上面可见的卡片」并不是真正的第一门课。

        所以：先滚到顶 → 一次 `ocr_rows()` 同时读出名字与按钮 → 立刻点。
        """
        scroll_list_to_top()

        rows = ocr_rows()
        btn_y = _topmost_learn_button_y(rows)
        if btn_y is None:
            print("[watch] 列表里找不到「去学习」按钮")
            return False

        # 课程名与按钮来自**同一次**读，保证是同一张卡
        found = _name_above(rows, btn_y)
        if not found:
            # 回退：卡片标题行本身就是这一屏里最长的中文文本。
            # 实测踩过 `_name_above` 因为布局微调而返回空，导致**课程名整轮丢失**
            # → `is_done` 里的 `bool(course_name)` 为假 → 跳过已看和记录进度
            # **全部失效**（已看完的两节被重看，而且新进度一条都不记）。
            # 所以这里必须有个更宽松的兜底，不能因为一个偏移量就全盘失效。
            print("[watch] 按按钮上方窗口没读到名字，改用整屏最长中文文本兜底")
            found = _longest_chinese(rows, btn_y)
        if found:
            name_box["name"] = found
            print(f"[watch] 第一门课: {found}")
        else:
            print("[watch] ⚠ 没读出第一门课的名字")

        # 按钮文本实测在 (271..353, btn_y..btn_y+40)，取中心
        btn = (312, btn_y + 20)
        print(f"[watch] 点「去学习」于 {btn}")
        click(*btn, wait=3.0)

        return verify.verify(
            exam.PAGE_COURSE,
            read_state=read_state,
            log=print,
            what="点「去学习」",
            tries=8,
            interval=1.5,
        )

    ok = verify.retry_with_recovery(
        action=navigate_and_watch,
        # **判定必须看动作的真实结果**，不能写 lambda: True。
        # 早先写死 True，导致「进入第一门课失败」也报告「结果: 完成」。
        verify_ok=lambda: outcome["ok"],
        recover=go_home,                 # 失败就回主页重来
        log=print,
        what="看护第一门课",
        max_rounds=max(1, args.max_recover),
    )

    print()
    print("=" * 66)
    print(f" 结果: {'完成' if ok else '未完成'}")
    print(f" 本地进度: {paths.data_dir() / 'course_progress.json'}")
    print("=" * 66)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
