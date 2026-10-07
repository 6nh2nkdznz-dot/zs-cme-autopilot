"""「每日签到」浮层处理的测试。

## 背景

用户实测指出：签到浮层**每天每门课都会弹**，而且会**盖住底下的视频弹题**
（点输入框点到浮层、点提交点到浮层的 X）。原先它被排成 `TASKS` 里的一个
**顺序步骤**、位置在「整门课轮播」后面 —— 轮播跑几小时，期间浮层弹出来
只会被点 X 关掉、**不真的签到**。

用户要求：改成「**识别到就做**，而不是先后顺序执行」。

所以现在有两个入口，这里都要钉住：
1. `core._handle_checkin()` —— 挂在每一步之前（幂等，识别不到就零开销）；
2. `00_navigation.json` 的「处理签到浮层」跳转节点。

运行：`python scripts\test_checkin.py`
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checkin  # noqa: E402
import exam  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "assets" / "resource" / "pipeline"

PASS = 0
FAIL = 0


def check(name: str, got, want=True) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}\n         期望 {want!r}\n         实际 {got!r}")


class FakeController:
    """记下所有点击，供断言。"""

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int]] = []

    def post_click(self, x, y):
        self.clicks.append((x, y))
        ctrl = self

        class _Job:
            def wait(self):
                return self
        return _Job()


def make_screen(states: list[str]):
    """按顺序吐出给定的屏幕文本。用完之后一直吐最后一个。"""
    box = {"i": 0}

    def _text() -> str:
        i = min(box["i"], len(states) - 1)
        box["i"] += 1
        return states[i]
    return _text


def main() -> int:
    print("=" * 68)
    print(" 每日签到浮层测试")
    print("=" * 68)

    print("\n[1] 识别：有浮层 / 没浮层")
    check("含「每日签到」→ 有浮层", checkin.has_popup("课程学习 每日签到 立即签到"))
    check("不含 → 没有", checkin.has_popup("课程学习 简介 目录 更多"), False)
    check("空串 → 没有", checkin.has_popup(""), False)
    check("None → 没有", checkin.has_popup(None), False)
    check("「已经签到」→ 已签到", checkin.already_done("每日签到 已经签到"))
    check("「立即签到」→ 未签到", checkin.already_done("每日签到 立即签到"), False)

    print("\n[2] 没弹出浮层时零副作用")
    ctl = FakeController()
    done = checkin.handle_popup(ctl, lambda: "课程学习 简介 目录")
    check("返回 False", done, False)
    check("一次点击都没发", ctl.clicks, [])

    print("\n[3] 未签到：点「立即签到」，浮层消失即算成功")
    ctl = FakeController()
    # 第 1 次读：有浮层、未签到；第 2 次读：浮层没了
    screen = make_screen(["每日签到 立即签到", ""])
    done = checkin.handle_popup(ctl, screen)
    check("返回 True", done, True)
    check("点的是「立即签到」坐标", ctl.clicks, [checkin.CHECKIN_TAP])
    check("**没有**多点 X（浮层已消失）", checkin.CLOSE_TAP in ctl.clicks, False)

    print("\n[4] 已签到：不重复点「立即签到」，只关掉")
    ctl = FakeController()
    screen = make_screen(["每日签到 已经签到", ""])
    done = checkin.handle_popup(ctl, screen)
    check("返回 True", done, True)
    check("只点了 X", ctl.clicks, [checkin.CLOSE_TAP])
    check("**没有**点「立即签到」", checkin.CHECKIN_TAP in ctl.clicks, False)

    print("\n[5] 点了「立即签到」但浮层还在 → 退回点 X")
    ctl = FakeController()
    # 1: 有浮层未签到  2: 浮层还在（按钮文案没变，可能没点上）  3: 关掉了
    screen = make_screen(["每日签到 立即签到", "每日签到 立即签到", ""])
    done = checkin.handle_popup(ctl, screen)
    check("返回 True（最终关掉了）", done, True)
    check("先点「立即签到」再点 X",
          ctl.clicks, [checkin.CHECKIN_TAP, checkin.CLOSE_TAP])

    print("\n[6] 点「立即签到」抛异常 → 不中断，退回点 X")
    class Boom(FakeController):
        def post_click(self, x, y):
            if (x, y) == checkin.CHECKIN_TAP:
                raise RuntimeError("模拟点击失败")
            return super().post_click(x, y)

    ctl2 = Boom()
    screen = make_screen(["每日签到 立即签到", ""])
    done = checkin.handle_popup(ctl2, screen)
    check("返回 True", done, True)
    check("点的是 X", ctl2.clicks, [checkin.CLOSE_TAP])

    print("\n[7] 关不掉也不崩")
    ctl3 = FakeController()
    screen = make_screen(["每日签到 立即签到"])   # 永远关不掉
    done = checkin.handle_popup(ctl3, screen)
    check("返回 False（如实报告没关掉）", done, False)
    check("确实尝试过点击", len(ctl3.clicks) >= 1)

    print("\n[8] 页面识别把浮层当成一种页面")
    check("标志词已定义",
          "每日签到" in exam.PAGE_MARKS_CHECKIN_POPUP)
    check("常量已定义", exam.PAGE_CHECKIN_POPUP, "checkin_popup")
    check("有中文名", exam.page_name(exam.PAGE_CHECKIN_POPUP), "每日签到浮层")
    check("识别到浮层（只有标题）",
          exam.detect_page("课程学习 每日签到"), exam.PAGE_CHECKIN_POPUP)
    check("没有浮层时不误判",
          exam.detect_page("课程学习 简介 目录 更多 我的学习"),
          exam.PAGE_COURSE)

    print("\n[9] 管线：识别到就签，不只是关掉")
    nav = json.loads((PIPELINE / "00_navigation.json").read_text(encoding="utf-8"))
    check("课程页跳转先处理签到浮层",
          "[JumpBack]处理签到浮层" in nav["等待课程页"]["next"])
    check("旧的「只点 X」跳转已移除",
          "[JumpBack]关闭签到弹窗" in nav["等待课程页"]["next"], False)
    handle = nav.get("处理签到浮层", {})
    check("新节点存在", bool(handle))
    check("识别「每日签到」", handle.get("expected"), ["每日签到"])
    check("第一个动作是点「立即签到」坐标",
          handle.get("target"), list(checkin.CHECKIN_TAP))
    check("点完接着关掉浮层", handle.get("next"), ["关掉签到弹窗"])
    check("X 节点仍在（兜底关掉）", "关闭签到弹窗" in nav)
    check("X 坐标没变",
          nav["关闭签到弹窗"]["target"], list(checkin.CLOSE_TAP))

    print("\n[10] 源码里挂在每一步之前")
    src = (Path(__file__).resolve().parent / "core.py").read_text(encoding="utf-8")
    check("有 _handle_checkin 方法", "def _handle_checkin(" in src)
    check("_run_step 里调了它", src.count("self._handle_checkin(tasker)") >= 3,
          True)
    check("签到失败不会抛穿（有 try/except）",
          "处理签到浮层出错（忽略，继续）" in src)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
