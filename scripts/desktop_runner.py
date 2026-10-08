# -*- coding: utf-8 -*-
"""把「桌面版（浏览器电脑模式）」那几个脚本接到界面上。

## 为什么单独一个文件，而不是塞进 `core.py`

`core.py` 里那一整套是**微信版**的：它要建 `Controller` / `Resource` /
`Tasker`、要确认 `com.tencent.mm` 活着、要检查屏幕方向。桌面版这些**一个都
不需要** —— 它只跟 CDP 说话，浏览器在模拟器里自己跑，平台那边认的是真实的
播放时长。把两套混在一个 `run_tasks()` 里，只会让每个分支前面都挂一串
「这条路用不上但得跳过」的判断。

所以这里只做三件事：排顺序、装停止钩子、把用户填的设置转成命令行。

## 为什么走「命令行参数」而不是直接调函数

`desktop_watch.main()` / `desktop_exam.main()` / `desktop_farm.main()` 的入口
就是 `argparse`，选项已经在那儿定义好了（`--lessons` / `--target` / `--finish` …）。
在这里重新拼一遍参数再调内部函数，等于把同一套语义维护两份 —— 命令行改了
而这里没跟上，界面就会**静默地**用错参数跑一整门课。直接给 argv 最稳。

argv 从哪儿来：用户在界面 ⚙ 里填的值存在 `config.json` 的 `options.<任务键>`
下面，`taskspec.to_argv()` 按声明表把它拼成参数。**空值不拼**，让各脚本自己的
`argparse` 默认值兜底，默认值只有一份。
"""

from __future__ import annotations

from typing import Callable

import desktop
import taskspec

#: 界面上的任务键（和 `core.DESKTOP_TASKS` 里那四个一一对应）。
WATCH = "d_watch"
FARM = "d_farm"
EXAM = "d_exam"
FINISH = "d_finish"


def _argv(task_key: str, log: Callable[[str], None]) -> list[str]:
    """读这个任务当前的设置，拼成命令行参数。

    读不出来（配置坏了）就当全默认跑，**不能因为设置读不到就一个任务都不做** ——
    用户点了「开始运行」结果什么都没发生，比用默认值跑更难排查。
    """
    try:
        vals = taskspec.values(task_key)
    except Exception as exc:  # noqa: BLE001
        log(f"[run] 读设置失败（按默认值跑）: {exc}")
        return []
    return taskspec.to_argv(task_key, vals)


def _show(argv: list[str]) -> str:
    """把 argv 拼成一行给人看，让日志里能对上「这次到底按什么参数跑的」。"""
    return ("  " + " ".join(argv)) if argv else "  （全默认）"


def run(keys: list[str], *, log: Callable[[str], None],
        should_stop: "Callable[[], bool] | None" = None) -> int:
    """按顺序跑选中的桌面版任务。返回退出码（0 正常）。

    `should_stop` 是界面的「立即停止」判据，装上以后
    `desktop.should_stop()` 在**每一层**都能问到（见那边的说明）。
    这里负责在结束时把它卸掉 —— 不卸的话下一次开跑会带着一个已经
    置位的判据，一启动就立刻停。
    """
    import desktop_exam
    import desktop_farm
    import desktop_watch

    result = 0
    desktop.set_stop_check(should_stop)
    # 把日志接到界面。这三个脚本原来一律 `print`，命令行跑没问题，
    # 从界面按钮跑就全进了虚空 —— 界面上只剩一句「运行中…」。
    #
    # `desktop_farm` 的 `log` 就是 `desktop_watch.log`（同一个函数对象），
    # 所以给它装上就够了，那边跟着生效。
    #
    # 离开时**必须还原成 print**：界面上的日志控件会被销毁，留着这个
    # 钩子的话下次从命令行 `--run desktop_watch` 会往一个死控件里写。
    desktop_watch.set_log(log)
    desktop_exam.set_log(log)
    try:
        if WATCH in keys:
            argv = _argv(WATCH, log)
            log("")
            log("=" * 78)
            log(f"[run] ① 自动看课（浏览器版）{_show(argv)}")
            log("=" * 78)
            result = desktop_watch.main(argv) or result
            if desktop.should_stop():
                log("[run] 收到停止，后面的任务不做了")
                return result

        if FARM in keys:
            argv = _argv(FARM, log)
            log("")
            log("=" * 78)
            log(f"[run] ② 刷学习时长（浏览器版）{_show(argv)}")
            log("=" * 78)
            result = desktop_farm.main(argv) or result
            if desktop.should_stop():
                log("[run] 收到停止，后面的任务不做了")
                return result

        # 考核和问卷在同一个脚本里，而且**顺序不能拆**：平台规定考核
        # 没到 60 分就不放行问卷（"请先完成课程学习，再进行问卷作答"）。
        # 所以 `d_exam` 和 `d_finish` 都指向它，区别只在要不要带 `--finish`。
        if EXAM in keys or FINISH in keys:
            # 两个任务各有各的设置，但只有一份 argv 能用。以**先勾的那个**为准：
            # 界面上的顺序是「考核+问卷」在「申请结课」前面，用户真想只结课、
            # 不做考核的话，`d_finish` 里的 `--course` 才会被用上。
            argv = _argv(EXAM if EXAM in keys else FINISH, log)
            if FINISH in keys:
                # `--finish` 会**顺带**把考核和问卷也做掉（都过了就是空跑，
                # 一门课几秒钟）。结了课之后就不能再刷分了，所以这个旗标
                # 只在界面上勾了「申请结课」时才给。
                argv.append("--finish")
            log("")
            log("=" * 78)
            log("[run] ③ 考核 + 问卷" + ("+ 申请结课" if FINISH in keys else "")
                + _show(argv))
            log("=" * 78)
            result = desktop_exam.main(argv) or result
            if desktop.should_stop():
                log("[run] 收到停止，后面的任务不做了")
                return result
    finally:
        # **必须卸**，理由见 docstring。
        desktop.set_stop_check(None)
        desktop_watch.set_log(None)
        desktop_exam.set_log(None)

    log("")
    log("✓ 浏览器版的任务都跑完了")
    return result
