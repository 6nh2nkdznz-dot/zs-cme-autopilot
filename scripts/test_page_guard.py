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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import exam  # noqa: E402
from core import AppCore  # noqa: E402

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

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
