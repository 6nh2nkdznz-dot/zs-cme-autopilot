"""页面前置检查的自测。

## 为什么需要它

用户实测反馈：

    为什么进行每一步时没有识别当前页面？我在后一步时点开了前一步的页面，
    但它没有识别到，反而继续下一步了。

根因是 `core._run_node` 原先**无脑跑管线节点**：

    job = tasker.post_task(entry).wait()

管线的入口多为 `DirectHit`（必命中）+ 无条件点击，页面不对时照样点，
点到完全无关的地方。`verify.py` 那套纠错机制是有的，但**只有
`run_exam_watch.py` 用了，UI 那条路径一直没用上**。

现在补上了 `_run_step`（前置检查 + 后置校验 + 恢复），这个测试盯着它。

运行:
    python scripts\\test_page_guard.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import exam  # noqa: E402
import paths  # noqa: E402
from core import AppCore, OrientationLost, parse_wm_size  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n         期望 {want!r}\n         实际 {got!r}")


def check_true(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


class FakeTasker:
    """记录被跑过哪些节点，并可控地"成功/失败"。"""

    def __init__(self, succeed: bool = True) -> None:
        self.ran: list[str] = []
        self.succeed = succeed

    def post_task(self, entry):
        self.ran.append(entry)
        outer = self

        class _Job:
            succeeded = outer.succeed

            def wait(self):
                return self

        return _Job()


class FakeCore(AppCore):
    """把「读屏」和「恢复」都换成假的，专注测判定逻辑。"""

    def __init__(self, pages: list[str], log=None) -> None:
        super().__init__(log=log or (lambda m="": None))
        #: 依次返回的页面序列；用完后一直返回最后一个
        self._pages = list(pages)
        self._i = 0
        self.recovered = 0
        self.tabbed = 0
        self.backed = 0

    def _page_now(self, tasker) -> str:
        p = self._pages[min(self._i, len(self._pages) - 1)]
        self._i += 1
        return p

    def _safe_back(self, tasker) -> None:
        self.backed += 1
        # 退一次就假设回到列表页
        self._pages = [exam.PAGE_LEARNING_LIST]

    def _recover_to(self, tasker, want: str, tries: int = 3) -> bool:
        self.recovered += 1
        self._pages = [want]          # 假设恢复成功
        self._i = 0
        return True


def main() -> int:
    print("=" * 68)
    print(" 页面前置检查自测")
    print("=" * 68)

    print("\n[1] 页面正确 → 照常执行")
    c = FakeCore([exam.PAGE_LEARNING_LIST])
    t = FakeTasker()
    ok = c._run_step(t, "点第一个去学习", want=exam.PAGE_LEARNING_LIST,
                     check_after=exam.PAGE_COURSE)
    check("返回成功", ok, True)
    check("节点跑了", t.ran, ["点第一个去学习"])

    print("\n[2] **页面不对 → 先恢复，不要盲目往下点**")
    #
    # 这正是用户报的场景：手动点开了别的页面，程序却继续下一步。
    c = FakeCore([exam.PAGE_RESULT])          # 当前在结果页
    t = FakeTasker()
    ok = c._run_step(t, "点第一个去学习", want=exam.PAGE_LEARNING_LIST)
    check("返回成功（恢复后继续）", ok, True)
    check_true("确实做了恢复", c.recovered >= 1,
               f"实际 {c.recovered}")
    check("恢复后才跑节点", t.ran, ["点第一个去学习"])

    print("\n[3] 回不到目标页 → 跳过这一步并说明")
    class NeverRecover(FakeCore):
        def _recover_to(self, tasker, want: str, tries: int = 3) -> bool:
            self.recovered += 1
            return False

    c = NeverRecover([exam.PAGE_RESULT])
    t = FakeTasker()
    ok = c._run_step(t, "播放整门课", want=exam.PAGE_COURSE)
    check("返回失败", ok, False)
    check("**没有跑节点**（关键：不能盲目往下点）", t.ran, [])

    print("\n[4] 有弹题挡着时不要继续（弹题会暂停视频）")
    c = FakeCore([exam.PAGE_QUIZ_POPUP])
    t = FakeTasker()
    ok = c._run_step(t, "播放整门课", want=exam.PAGE_COURSE)
    check("返回失败", ok, False)
    check("没有跑节点", t.ran, [])

    print("\n[5] 后置校验：跑完不在预期页要告警（但不重跑）")
    # 节点跑完后期望到课程页，实际到了结果页 → 应告警但仍算"跑过了"
    c = FakeCore([exam.PAGE_LEARNING_LIST, exam.PAGE_RESULT])
    logs: list[str] = []
    c.log = lambda m="": logs.append(str(m))
    t = FakeTasker()
    c._run_step(t, "点第一个去学习", want=exam.PAGE_LEARNING_LIST,
                check_after=exam.PAGE_COURSE)
    blob = "\n".join(logs)
    check_true("日志里有「不在」告警",
               "不在" in blob or "⚠" in blob, f"日志={blob[-200:]}")
    check("节点只跑了一次（不自动重跑，避免副作用叠加）",
          len(t.ran), 1)

    print("\n[6] 节点失败时的 fallback 只在真失败时用")
    c = FakeCore([exam.PAGE_COURSE])
    t = FakeTasker(succeed=False)
    ok = c._run_step(t, "主节点", want=exam.PAGE_COURSE,
                     fallbacks=("备用节点",))
    check("返回失败", ok, False)
    check("主节点和备用都试了", t.ran, ["主节点", "备用节点"])

    c = FakeCore([exam.PAGE_COURSE])
    t = FakeTasker(succeed=True)
    c._run_step(t, "主节点", want=exam.PAGE_COURSE, fallbacks=("备用节点",))
    check("主节点成功就不试备用", t.ran, ["主节点"])

    print("\n[7] 退避安全：back 只按一次")
    #
    # 实测踩过：连按多次 BACK 会把微信 WebView 的宿主页面推光，
    # 落到空白页，看起来像「微信自己退出了」。
    called: list[int] = []
    c = FakeCore([exam.PAGE_RESULT])
    real_back = AppCore._safe_back

    def spy_back(self, tasker):
        called.append(1)
        return real_back(self, tasker)

    AppCore._safe_back = spy_back      # type: ignore[assignment]
    try:
        c._recover_to(None, exam.PAGE_LEARNING_LIST)
    finally:
        AppCore._safe_back = real_back  # type: ignore[assignment]
    check_true("单次恢复里 back 不会按很多次", len(called) <= 1,
               f"实际按了 {len(called)} 次")

    print("\n[7b] **不在平台页面里就不许按返回键**")
    #
    # 用户实测反馈：「能不能把退出微信的代码删了，每次都要重新打开」。
    #
    # 程序里没有任何 force-stop / StopApp / 关微信的代码，把人顶出微信的
    # 是 KEYCODE_BACK：它不认页面。平台页面已不在前台时，这一下按的
    # 就成了微信自己的返回 —— 从聊天列表退回桌面，看起来像「微信被程序
    # 关掉了」。而微信的 WebView 会话丢了，程序**没有任何办法**恢复。
    #
    # 所以按之前必须先确认「还在站内」。
    class FakeKeyController:
        """只记 `post_click_key` 被调了什么键。"""

        def __init__(self) -> None:
            self.keys: list[int] = []

        def post_click_key(self, key: int):
            self.keys.append(int(key))
            return self

        def wait(self, *_a, **_kw):
            return self

    real_on_site = AppCore._on_site
    try:
        # (a) 掉回微信了 → 一次都不许按
        AppCore._on_site = lambda self, tasker: False   # type: ignore[assignment]
        c2 = AppCore(log=lambda m="": None)
        kc = FakeKeyController()
        c2._controller = kc
        c2._safe_back(None)
        check("掉回微信时没按返回键", kc.keys, [])

        # (b) 还在平台里 → 照常按一次
        AppCore._on_site = lambda self, tasker: True    # type: ignore[assignment]
        c3 = AppCore(log=lambda m="": None)
        kc2 = FakeKeyController()
        c3._controller = kc2
        c3._safe_back(None)
        check("还在平台里时按了一次返回", kc2.keys, [4])
    finally:
        AppCore._on_site = real_on_site              # type: ignore[assignment]

    print("\n[7c] 恢复页面的「学习」tab 坐标要和实测一致")
    #
    # OCR 实测（真机 720x1280 画布）：底部导航「学习」的文字块
    # box = [432, 1250, 36, 24]，中心 (450, 1262)。
    # 原先写 449 —— 差 1px 无所谓，但别有人再凭印象改回别的值。
    src_core = (Path(__file__).resolve().parent / "core.py").read_text(
        encoding="utf-8")
    check_true("core.py 用的是实测坐标 450,1262",
               "post_click(450, 1262)" in src_core,
               "应为 `ctrl.post_click(450, 1262).wait()`")
    check_true("core.py 不再有旧的 449,1262",
               "post_click(449, 1262)" not in src_core,
               "旧的 449 应已替换")
    for fname in ("run_exam_watch.py", "retake_exam.py"):
        src = (Path(__file__).resolve().parent / fname).read_text(
            encoding="utf-8")
        check_true(f"{fname} 的 back() 有「不在站内就不按」的守卫",
                   "on_site" in src,
                   "back() 里应先 `exam.on_site(...)` 再按 KEYCODE_BACK")

    print("\n[8] 考核任务接的节点名与 want 必须对得上")
    #
    # 用户实测报的坑：程序跑到考核说明页就停住，不点底部的「进入答题」。
    #
    # 根因有两个，都在这一个调用点上：
    #   1. 节点名写的是「进入考核」—— 那个节点的职责**只到考核页为止**，
    #      「点底部进入答题」那一段挂在 `进入考核并答题` 上。于是程序停在
    #      说明页不动，而日志里看起来「节点成功了」。
    #   2. want 写的是课程页（`PAGE_COURSE`），可这个节点跑起来**必然**要
    #      离开课程页。前置检查一看「不在课程页」就调 `_recover_to` 按底部
    #      「学习」把它拽回课程页 → 再进考核 → 再被拽回来，来回打转。
    import json

    pipeline = json.loads(
        (Path(__file__).resolve().parent.parent
         / "assets" / "resource" / "pipeline" / "20_exam.json"
         ).read_text(encoding="utf-8")
    )
    entry_node = pipeline.get("进入考核并答题", {})
    entry_next = list(entry_node.get("next") or [])
    check_true("管线里有「进入考核并答题」节点", bool(entry_node))
    check_true("它会把「定位进入按钮」排在 next 里",
               "定位进入按钮" in entry_next, f"next={entry_next}")
    check_true("「定位进入按钮」认得「进入答题」",
               "进入答题" in (pipeline.get("定位进入按钮", {})
                              .get("expected") or []))
    check_true("「点击进入按钮」真点了（不是 DoNothing）",
               pipeline.get("点击进入按钮", {}).get("action") == "Click")
    # 「进入考核」不该背这个职责 —— 它只负责到考核页
    check_true("「进入考核」的 next 里没有点进入答题那一段",
               "定位进入按钮" not in (pipeline.get("进入考核", {})
                                      .get("next") or []))

    # ---- 不允许「静默结束」----
    #
    # 上面那条坑还有第二层：`定位进入按钮` 的 `on_error` 原先是**空列表**。
    # 空列表的含义是「没命中就过」，于是 OCR 一旦漏掉「进入答题」这行字，
    # 整条任务就**悄悄结束**、日志里还显示成功。用户看到的现象是
    # 「停在说明页不动」，而程序自己认为干完了 —— 最难查的一类。
    #
    # 现在 on_error 指向一个按实测坐标点的兜底节点。
    for locator, fallback, label in (
        ("定位进入按钮", "点进入按钮(坐标)", "进入答题"),
        ("定位答题入口", "点答题入口(坐标)", "再做一次"),
    ):
        node = pipeline.get(locator, {})
        err = list(node.get("on_error") or [])
        check_true(f"「{locator}」识别不到时不静默结束", bool(err),
                   f"on_error={err}")
        check_true(f"「{locator}」漏识别时退回「{fallback}」",
                   fallback in err, f"on_error={err}")
        fb = pipeline.get(fallback, {})
        check_true(f"「{fallback}」存在", bool(fb))
        check_true(f"「{fallback}」真点了（不是 DoNothing）",
                   fb.get("action") == "Click")
        target = fb.get("target")
        check_true(f"「{fallback}」用的是固定坐标点（不是锚点）",
                   isinstance(target, list) and len(target) == 2,
                   f"target={target}")
        check_true(f"「{fallback}」的坐标在屏幕里（画布 720x1280）",
                   isinstance(target, list) and len(target) == 2
                   and 0 <= target[0] <= 720 and 0 <= target[1] <= 1280,
                   f"target={target}")

    # 兜底坐标要和实测截图对得上
    _enter = pipeline.get("点进入按钮(坐标)", {}).get("target") or []
    check_true("兜底坐标落在实测的蓝色按钮包围盒里"
               "（x 12..709 y 1232..1270）",
               len(_enter) == 2 and 12 <= _enter[0] <= 709
               and 1232 <= _enter[1] <= 1270, f"target={_enter}")

    class StepRecorder(AppCore):
        """记下 `_run_step` 的入参，并假装每一步都成功。"""

        def __init__(self) -> None:
            super().__init__(log=lambda m="": None)
            self.steps: list[tuple[str, str, str]] = []
            self.retry_texts: tuple[str, ...] = ()
            self.retry_y_min = 0

        def _run_step(self, tasker, entry, want, fallbacks=(),
                      check_after="", retry_texts=(), retry_y_min=0):
            self.steps.append((entry, want, check_after))
            self.retry_texts = retry_texts
            self.retry_y_min = retry_y_min
            return True

    rec = StepRecorder()
    rec._run_one(object(), "exam")
    check("考核任务只跑一步", len(rec.steps), 1)
    if rec.steps:
        entry, want, after = rec.steps[0]
        check("跑的是「进入考核并答题」（不是「进入考核」）",
              entry, "进入考核并答题")
        # want 认的是**起点**（课程页）：这个节点的第一步就是从课程页切
        # 「更多」→ 点「考核」。写成考核入口页会让「已经在考核页」的重跑
        # 被前置检查拽回课程页，来回打转 —— 真实事故就是这样。
        check("前置检查认的是起点（课程页）", want, exam.PAGE_COURSE)
        check("后置校验认的是答题页（交付物）", after, exam.PAGE_ANSWER)
        # 用户实测「还是卡在进入答题的页面」（m06507 / m06982）而我自己复现
        # 不出来；2026-10-07 的日志取证查明：那一版按坐标重试**根本没生效**
        # （MaaTouch 的 touch_down 一次都没被调用）。所以改成把「按钮可能
        # 出现的所有文案」交给 _run_step，让它每轮重新 OCR 找按钮再点。
        check("把按钮文案交给 _run_step 做重试", rec.retry_texts,
              exam.ENTER_BUTTON_TEXTS)
        check("重试只认 y ≥ ENTER_BUTTON_Y_MIN 的文本（躲开标题栏同名词）",
              rec.retry_y_min, exam.ENTER_BUTTON_Y_MIN)

    print("\n[8b] 兜底坐标只有一个来源，别在管线里另抄一份漂走")
    check("exam.ENTER_BUTTON_TAP 就是实测中心", exam.ENTER_BUTTON_TAP,
          (360, 1251))
    _pipeline_taps = []
    _exam_json = json.loads(
        (paths.resource_dir() / "pipeline" / "20_exam.json")
        .read_text(encoding="utf-8"))
    for _name, _node in _exam_json.items():
        if not isinstance(_node, dict):
            continue
        if _node.get("action") == "Click" and isinstance(_node.get("target"),
                                                         list):
            _pipeline_taps.append((_name, tuple(_node["target"])))
    _coord_nodes = {
        name: target for name, target in _pipeline_taps
        if name.endswith("(坐标)")
    }
    check("管线里确实有点固定坐标的兜底节点",
          sorted(_coord_nodes), ["点答题入口(坐标)", "点进入按钮(坐标)"])
    check("管线坐标 == exam.ENTER_BUTTON_TAP（两处不许漂）",
          _coord_nodes.get("点进入按钮(坐标)"), exam.ENTER_BUTTON_TAP)
    check("考核列表页按钮坐标 == exam.EXAM_LIST_BUTTON_TAP",
          _coord_nodes.get("点答题入口(坐标)"), exam.EXAM_LIST_BUTTON_TAP)
    # 名字带「(坐标)」的兜底节点必须真的是固定坐标，不能写成锚点 ——
    # 锚点正是识别失败时拿不到的东西，用锚点兜底等于没兜。
    # 注意 `_pipeline_taps` 里存的是 tuple（方便直接和常量比），
    # 所以这里判 tuple 而不是 list。
    check("点固定坐标的兜底节点不许用锚点",
          all(isinstance(v, tuple) and len(v) == 2
              for v in _coord_nodes.values()), True)
    # 反过来也要钉：主路径那几个节点必须用锚点，不能写死坐标 ——
    # 它们的按钮位置随页面滚动而变（锚点就是为这个存在的）。
    _anchor_nodes = {
        name: target for name, target in _pipeline_taps
        if not name.endswith("(坐标)")
    }
    check("主路径仍走锚点（位置会随滚动变）",
          all(isinstance(v, str) and v.startswith("[Anchor]")
              for v in _anchor_nodes.values()), True)

    print("\n[9] 考核入口页的标志词要覆盖说明页那个按钮")
    check_true("「进入答题」算考核入口页的标志",
               "进入答题" in exam.PAGE_MARKS_EXAM_ENTRY)
    check("说明页判成考核入口页",
          exam.detect_page("本项目考核 说明 进入答题"),
          exam.PAGE_EXAM_ENTRY)

    print("\n[10] _click_until 不许认死坐标：每轮重新找按钮、一轮内试多个点")
    # 为什么盯这个：2026-10-07 的日志取证查明「按坐标点报成功但屏幕上什么
    # 都没发生，且 MaaTouch 的 touch_down 一次都没被调用」。所以修法不是
    # 换个坐标，而是**每轮重新 OCR 定位 + 在按钮框内试多个高度**。
    # 下面用一个假屏（可切换内容）+ 假的 _tap 把它钉住，不需要真模拟器。

    class FakeClickCore(AppCore):
        """假屏 + 假点击：模拟「说明页按钮点中心没反应、点偏上才生效」。"""

        def __init__(self, rows_seq, hit_at: int, raw_hit_at: int = 0):
            super().__init__(log=lambda m="": None)
            self.rows_seq = list(rows_seq)
            self.screen = self.rows_seq[0]
            self.taps: list[tuple[int, int]] = []
            self.raw_taps: list[tuple[int, int]] = []
            self.scans = 0
            self.hit_at = hit_at        # 第几次 MaaTouch 点击开始「生效」
            self.raw_hit_at = raw_hit_at  # 第几次换通道点击开始「生效」

        def _screen_rows(self, tasker):
            self.scans += 1
            return self.screen

        def _tap(self, x, y):
            self.taps.append((int(x), int(y)))
            if self.hit_at and len(self.taps) >= self.hit_at:
                self.screen = self.rows_seq[1]

        def _tap_raw(self, x, y) -> bool:
            self.raw_taps.append((int(x), int(y)))
            if self.raw_hit_at and len(self.raw_taps) >= self.raw_hit_at:
                self.screen = self.rows_seq[1]
                return True
            return True

    # 说明页：按钮框 [317,1229,85,29]（实测 OCR box），页面标志「说明」
    entry_rows = [("本项目考核", 299, 84, 121, 32),
                  ("说明", 21, 148, 80, 37),
                  ("进入答题", 317, 1229, 85, 29)]
    answer_rows = [("上一题", 20, 1200, 60, 30),
                   ("下一题", 640, 1200, 60, 30),
                   ("答题卡", 340, 1200, 60, 30)]

    c = FakeClickCore([entry_rows, answer_rows], hit_at=2)
    ok = c._click_until(object(), exam.PAGE_ANSWER,
                        texts=exam.ENTER_BUTTON_TEXTS,
                        y_min=exam.ENTER_BUTTON_Y_MIN, gap=0)
    check_true("点了没反应会自己换个点再点（不是原地重试同一个坐标）", ok)
    check("真的点了两次", len(c.taps), 2)
    check("两次点的是同一个按钮框内、不同高度", len(set(c.taps)), 2)
    check("落在按钮框里（x 317..402）",
          all(317 <= x <= 402 for x, _ in c.taps), True)
    check("点的是框内偏上的位置（躲开压住下沿的固定条）",
          all(1229 <= y <= 1258 for _, y in c.taps), True)
    check_true("每轮都重新看屏（不是拿缓存的坐标）", c.scans >= 3)

    # 按钮根本不在屏上时：必须老老实实失败，不能瞎点
    c2 = FakeClickCore([answer_rows, answer_rows], hit_at=99)
    check("找不到按钮时返回 False（不瞎点）",
          c2._click_until(object(), exam.PAGE_ANSWER,
                          texts=exam.ENTER_BUTTON_TEXTS,
                          y_min=exam.ENTER_BUTTON_Y_MIN, gap=0), False)
    check("一次点击都没发出去", len(c2.taps), 0)

    # 只有 y < y_min 的同名词时，也必须当作找不到 ——
    # 顶部标题栏里「本项目考核」那个标题就是这么被躲开的。
    c3 = FakeClickCore([entry_rows, answer_rows], hit_at=99)
    c3.screen = [("考核", 300, 60, 60, 30),
                 ("开始答题", 590, 160, 95, 32)]
    check_true("y_min 挡得住标题栏（按钮在 y=160 但仍 ≥120，能认到）",
               c3._find_text_point(object(), exam.ENTER_BUTTON_TEXTS,
                                   y_min=exam.ENTER_BUTTON_Y_MIN)
               == (637, 176))
    c3.screen = [("进入答题", 300, 60, 60, 30)]
    check_true("按钮跑到 y<y_min（顶部标题栏里的同名干扰词）→ 认不到",
               c3._find_text_point(object(), exam.ENTER_BUTTON_TEXTS,
                                   y_min=exam.ENTER_BUTTON_Y_MIN) is None)
    check_true("同一个词在 y_min=0 时反而认得到（证明挡它的确实是 y_min）",
               c3._find_text_point(object(), exam.ENTER_BUTTON_TEXTS,
                                   y_min=0) == (330, 75))

    # ---- 换通道：MaaTouch 点下去屏幕**一点没变**时，必须换 adb shell input tap ----
    # 这是 2026-10-07 那份日志的核心症状：节点报成功、touch_down 却没被调用。
    c4 = FakeClickCore([entry_rows, answer_rows], hit_at=0, raw_hit_at=1)
    ok = c4._click_until(object(), exam.PAGE_ANSWER,
                         texts=exam.ENTER_BUTTON_TEXTS,
                         y_min=exam.ENTER_BUTTON_Y_MIN, gap=0)
    check_true("MaaTouch 那一下没到页面上时，换 adb shell input tap 也能把它点进去", ok)
    check("MaaTouch 先试了一次", len(c4.taps), 1)
    check("换通道那一下也发了", len(c4.raw_taps), 1)
    check("两条通道点的是同一个点", c4.raw_taps[0], c4.taps[0])

    # ---- 跑着跑着屏幕转横：必须立刻停，不能继续点 ----
    # 实测事故：横屏时点「去学习」的坐标 (312,794) 在设备上落到了右边系统键，
    # 直接把设备点回桌面、丢掉微信 WebView 会话。所以横屏 = 立刻停。
    class LandscapeCore(FakeClickCore):
        def _canvas_portrait(self):
            return False

    c5 = LandscapeCore([entry_rows, answer_rows], hit_at=99)
    raised = ""
    try:
        c5._click_until(object(), exam.PAGE_ANSWER,
                        texts=exam.ENTER_BUTTON_TEXTS,
                        y_min=exam.ENTER_BUTTON_Y_MIN, gap=0)
    except OrientationLost as exc:
        raised = str(exc)
    check_true("横屏时 _click_until 抛 OrientationLost（不是硬着头皮点）",
               "转成横屏" in raised)
    check("横屏时一次点击都没发出去", len(c5.taps), 0)
    check_true("OrientationLost 是 RuntimeError 的子类（外层能统一兜住）",
               issubclass(OrientationLost, RuntimeError))

    _src_core = (Path(__file__).resolve().parent / "core.py").read_text(
        encoding="utf-8")
    check_true("run_tasks 里真的接了 OrientationLost（不然会直接崩到界面上）",
               "except OrientationLost" in _src_core)
    check_true("run_tasks 开跑前确认了竖屏（锁不回竖屏就不跑）",
               "ensure_portrait" in _src_core)

    print("\n[11] _tap_raw 的缩放解析：解析错了会静默点到别的地方")
    # `_tap_raw` 走 adb shell input tap，要的是**设备像素**，而我们的坐标是
    # 720x1280 画布。缩放算错不会报错，只会整体点偏 —— 最难查的那种错。
    check("物理竖屏 1080x1920 → 宽 1080", parse_wm_size("Physical size: 1080x1920"),
          1080)
    check("有 Override 时取 Override（它才是生效的那个）",
          parse_wm_size("Physical size: 1080x1920\nOverride size: 720x1280"),
          720)
    check("反过来也一样（Override 在前也认最后一行）",
          parse_wm_size("Override size: 720x1280\nPhysical size: 1080x1920"),
          1080)
    check("多余空行/空格不影响", parse_wm_size("  Physical size:  1080 x 1920 \n"),
          1080)
    check("读不到返回 None", parse_wm_size(""), None)
    check("没有尺寸信息返回 None", parse_wm_size("error: no display"), None)
    check("缩放 = 宽度 / 720（1080 → 1.5）", parse_wm_size(
        "Physical size: 1080x1920") / 720.0, 1.5)

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
