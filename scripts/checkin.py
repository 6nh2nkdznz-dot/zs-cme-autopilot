"""「每日签到」浮层：识别到就顺手签掉，不做成一步独立任务。

## 为什么要单独一个模块

用户实测指出两点：

1. **这个浮层每天每门课点进去都会弹**（不是只弹一次），所以它出现在
   看课、考核、弹题处理的**任何时刻**；
2. 它会**盖住底下的视频弹题** —— 实测点输入框点到浮层、点提交点到浮层的 X，
   后续所有点击都被它吃掉。

原先的做法是把它当成 `TASKS` 里的一个**顺序步骤**（`("checkin", "每日签到", …)`），
排在「整门课轮播」**后面**。问题是：轮播一跑就是几小时，期间签到浮层弹出来
只会被点 X 关掉、**不会真的签到**；而独立任务排在后面，还要等轮播结束。

用户的要求是改成「**识别到就做**，而不是先后顺序执行」。所以这里提供一个
幂等的处理器，由各个循环在每一步之前调用。

## 两个按钮的坐标（实测）

* 「立即签到」橙色按钮，中心 **(360, 764)** —— 点它就完成签到
* 右上角 X **(521, 613)** —— 只关掉浮层

**优先点「立即签到」**：既清掉遮挡，又顺手把签到做了。
只有点不到（例如今天已签到、按钮文案变了）才退回点 X。

注意：「立即签到」用 `adb shell input tap` 点不动，必须走 MaaTouch
（`controller.post_click`）。这是 `30_checkin.json` 里就记着的实测结论。
"""

from __future__ import annotations

#: 浮层标题
POPUP_MARK = "每日签到"
#: 已签到的按钮文案
DONE_TEXT = "已经签到"
#: 未签到的按钮文案
TODO_TEXT = "立即签到"

#: 「立即签到」按钮中心（实测）
CHECKIN_TAP = (360, 764)
#: 浮层右上角 X（实测）
CLOSE_TAP = (521, 613)

#: 点完之后等界面反应
SETTLE_AFTER_CHECKIN = 2.5
SETTLE_AFTER_CLOSE = 2.0


def has_popup(text: str) -> bool:
    """整屏文本里有没有签到浮层。"""
    return POPUP_MARK in (text or "")


def already_done(text: str) -> bool:
    """浮层上是不是显示「已经签到」（今天签过了）。"""
    return DONE_TEXT in (text or "")


def handle_popup(controller, screen_text, log=None) -> bool:
    """把签到浮层处理掉，**顺手完成签到**。返回是否真的清掉了。

    幂等：今天已签到、或浮层没弹，调用它都不会有副作用。

    参数
    ----
    controller   : MaaFramework 的 controller（要 `post_click`）
    screen_text  : 可调用对象，返回当前整屏文本。每次点击后重新取，
                   不要用旧文本判断 —— 点完界面就变了。
    log          : 可选的日志函数
    """
    import time

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    text = screen_text() or ""
    if not has_popup(text):
        return False                      # 没弹，什么都不做

    if already_done(text):
        # 今天签过了：只关掉浮层，不要重复点「立即签到」
        _log("[checkin] 浮层显示「已经签到」，只关掉不重复点")
        return _close(controller, screen_text, _log)

    _log("[checkin] 识别到签到浮层 → 点「立即签到」（顺手把今天的签了）")
    try:
        controller.post_click(*CHECKIN_TAP).wait()
    except Exception as exc:  # noqa: BLE001 - 点不动就退回关掉，别中断主流程
        _log(f"[checkin] 点「立即签到」失败: {exc}")
        return _close(controller, screen_text, _log)
    time.sleep(SETTLE_AFTER_CHECKIN)

    text = screen_text() or ""
    if not has_popup(text):
        _log("[checkin] ✓ 签到完成，浮层已消失")
        return True
    if already_done(text):
        _log("[checkin] ✓ 签到成功（按钮已变成「已经签到」）")
        return _close(controller, screen_text, _log)

    # 点了但浮层还在 → 按钮可能已变文案，别重复点，直接关掉
    _log("[checkin] 点完浮层还在，改点右上角 X 关掉")
    return _close(controller, screen_text, _log)


def _close(controller, screen_text, log) -> bool:
    """点右上角 X 关掉浮层。返回是否关掉了。"""
    import time

    try:
        controller.post_click(*CLOSE_TAP).wait()
    except Exception as exc:  # noqa: BLE001
        log(f"[checkin] 点 X 失败: {exc}")
        return False
    time.sleep(SETTLE_AFTER_CLOSE)

    if has_popup(screen_text() or ""):
        log("[checkin] ✗ 浮层还在，这次没关掉")
        return False
    log("[checkin] 浮层已关掉")
    return True
