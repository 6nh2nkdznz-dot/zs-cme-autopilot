"""动作后置校验 + 失败恢复。

## 为什么需要

这个项目反复栽在同一件事上：**动作发出去了就以为成功**，从不用结果验证。

```
点「提交」  → 只确认"点了"，没确认结果页出现
            → 以为失败，再点一次；结果页没有提交按钮，全打空
            → 循环 9 次才发现早就交上了

点「下一题」→ 没确认题干变了 → 翻页失败却继续往下走
点「选项」  → 没确认圆圈选中了 → 点空了也不知道
```

这类失败**不报错**，只是静默地做无用功，所以最难查。

本模块提供两层：

1. `verify(...)` —— 动作后声明期望状态，实测比对，不一致就**报出差异**。
   日志直接写「我做了 X，期望 Y，实际 Z」，不必事后靠猜。

2. `recover_to_home(...)` —— 用户指定的恢复策略：**识别出错就回主页重新来**。
   从任意页面退回「我的学习」列表，让上层重新走一遍导航。
   已经看过的课由 `course_progress` 跳过，所以重来不会白看。

## 设计取舍

`verify` **不自己重试**。重试策略属于调用方的业务判断（有的动作重试有意义，
有的只会更糟），这里只负责把事实摆出来。
"""

from __future__ import annotations

import time
from typing import Callable


def verify(
    expected: str | tuple[str, ...],
    *,
    read_state: Callable[[], str],
    log: Callable[[str], None] = print,
    what: str = "",
    tries: int = 3,
    interval: float = 1.0,
    strict: bool = False,
) -> bool:
    """确认当前处于期望的页面状态。

    参数:
        expected: 期望的页面类型（`exam.PAGE_*`），可给多个表示「其一即可」
        read_state: 返回当前页面类型的回调（通常包一层 detect_page）
        what: 刚刚做了什么，用于日志（如 "点提交"）
        tries/interval: 允许的收敛时间——页面切换有延迟，等一下再看
        strict: True 时不符算失败返回 False；False 时也返回 False 但只警告

    返回是否在 `tries` 次内看到期望状态。

    ## 为什么要重试几次

    点击到页面渲染出来有几百毫秒到一两秒的延迟。只看一次会把
    「还没来得及变」误判成「没生效」——那正是当初连点 9 次提交的原因。
    """
    want = (expected,) if isinstance(expected, str) else tuple(expected)
    seen = ""
    for i in range(max(1, tries)):
        seen = read_state() or ""
        if seen in want:
            if i:
                log(f"[verify] ✓ {what or '动作'} 生效（第 {i + 1} 次确认，"
                    f"页面={seen}）")
            return True
        if i < tries - 1:
            time.sleep(interval)

    log(f"[verify] ✗ {what or '动作'} 未生效")
    log(f"[verify]     期望页面: {' / '.join(want)}")
    log(f"[verify]     实际页面: {seen or '(读不到)'}")
    if strict:
        log("[verify]     按严格模式处理，交由调用方决定是否重试")
    return False


def recover_to_home(
    *,
    read_state: Callable[[], str],
    log: Callable[[str], None] = print,
    tap_back: Callable[[], None],
    tap_learning_tab: Callable[[], None],
    home_page: str,
    extra_backs: int = 6,
    settle: float = 2.0,
) -> bool:
    """从任意页面退回「我的学习」列表。

    这是用户指定的恢复策略：**识别出错就回主页重新看课**。

    做法很朴素但可靠：先按「学习」tab（一步到位），没到再**谨慎地**退一次
    back。不猜自己在哪一页——反正目标是同一个。

    返回是否到达主页。

    ## 为什么先按 tab 而不是先 back

    底部「学习」tab 在**所有**主页面都在，一步就能到列表页；
    而 back 的次数取决于当前在哪一层，猜错会退到微信里。
    所以优先 tab，back 只作为兜底。

    ## ⚠️ back 最多只按 1 次（用户实测反馈）

    原先会连按最多 6 次 back。实测后果很糟 —— 用户反馈
    「点击第一个去学习后自动退出了微信」：

        [recover] tab 没到，改用 back（最多 6 次）
        [recover] ✗ 没能回到主页，需要人工介入
        前台变成 com.tencent.mm/ui/EmptyActivity   ← 微信的空白中转页

    按 BACK 会把微信 WebView 的**宿主页面**退掉，落在 `EmptyActivity`
    这种空白页上，看起来就跟微信挂了/退出了一样。

    现在改成：**最多退 1 次，退完立刻确认**。还回不去就说明 tab 和 back
    都没用，再按也没用 —— 交给上层处理（重开微信），不要一路把
    WebView 推光。
    """
    log("[recover] 识别出错 → 回主页重新来过")

    if (read_state() or "") == home_page:
        log("[recover] 已经在主页")
        return True

    for attempt in range(1, 3):
        log(f"[recover] 第 {attempt} 次：按「学习」tab")
        tap_learning_tab()
        time.sleep(settle)
        if (read_state() or "") == home_page:
            log("[recover] ✓ 已回到主页")
            return True

    # 只退一次。退多了会把微信 WebView 的宿主页面推光。
    log("[recover] tab 没到，谨慎退一次 back")
    tap_back()
    time.sleep(settle)
    if (read_state() or "") == home_page:
        log("[recover] ✓ 退回 1 次后到主页")
        return True

    # 退回后再按一次 tab：有些层级被 back 弹掉后 tab 才生效
    log("[recover] 再按一次「学习」tab")
    tap_learning_tab()
    time.sleep(settle)
    if (read_state() or "") == home_page:
        log("[recover] ✓ 退 1 次 + tab 后到主页")
        return True

    log("[recover] ✗ tab 和一次 back 都没回到主页")
    log("[recover]   不再继续按 back（会把微信页面推光，变成空白页）")
    return False


def retry_with_recovery(
    action: Callable[[], bool],
    *,
    verify_ok: Callable[[], bool],
    recover: Callable[[], bool],
    log: Callable[[str], None] = print,
    what: str = "",
    max_rounds: int = 3,
) -> bool:
    """动作失败就恢复现场再重试。

    流程：做事 → 校验 → 不通过就回主页 → 重做。
    上限 `max_rounds` 轮，避免在死循环里烧时间。

    返回最终是否成功。
    """
    for rnd in range(1, max_rounds + 1):
        log(f"[retry] 第 {rnd}/{max_rounds} 轮: {what or '动作'}")
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - 恢复流程要能兜住任何异常
            log(f"[retry] 动作抛异常: {type(exc).__name__}: {exc}")

        if verify_ok():
            return True

        if rnd >= max_rounds:
            break
        if not recover():
            log("[retry] 恢复失败，停止重试")
            return False

    log(f"[retry] {what or '动作'} 在 {max_rounds} 轮内未成功")
    return False
