# -*- coding: utf-8 -*-
"""把「桌面版（浏览器电脑模式）」那几个脚本接到界面上。

## 为什么单独一个文件，而不是塞进 `core.py`

`core.py` 里那一整套是**微信版**的：它要建 `Controller` / `Resource` /
`Tasker`、要确认 `com.tencent.mm` 活着、要检查屏幕方向。桌面版这些**一个都
不需要** —— 它只跟 CDP 说话，浏览器在模拟器里自己跑，平台那边认的是真实的
播放时长。把两套混在一个 `run_tasks()` 里，只会让每个分支前面都挂一串
「这条路用不上但得跳过」的判断。

所以这里只做三件事：排顺序、装停止钩子、把参数转成那几个脚本自己的命令行。

## 为什么走「命令行参数」而不是直接调函数

`desktop_watch.main()` / `desktop_exam.main()` 的入口就是 `argparse`，
选项已经在那儿定义好了（`--lessons` / `--max-courses` / `--finish` …）。
在这里重新拼一遍参数再调内部函数，等于把同一套语义维护两份 —— 命令行改了
而这里没跟上，界面就会**静默地**用错参数跑一整门课。直接给 argv 最稳。
"""

from __future__ import annotations

from typing import Callable

import desktop

#: 界面上的任务键 → 说明（`core.DESKTOP_TASKS` 里那三个）。
WATCH = "d_watch"
EXAM = "d_exam"
FINISH = "d_finish"


def run(keys: list[str], *, log: Callable[[str], None],
        should_stop: "Callable[[], bool] | None" = None) -> int:
    """按顺序跑选中的桌面版任务。返回退出码（0 正常）。

    `should_stop` 是界面的「立即停止」判据，装上以后
    `desktop.should_stop()` 在**每一层**都能问到（见那边的说明）。
    这里负责在结束时把它卸掉 —— 不卸的话下一次开跑会带着一个已经
    置位的判据，一启动就立刻停。
    """
    import desktop_exam
    import desktop_watch

    result = 0
    desktop.set_stop_check(should_stop)
    # 把日志接到界面。这两个脚本原来一律 `print`，命令行跑没问题，
    # 从界面按钮跑就全进了虚空 —— 界面上只剩一句「运行中…」。
    #
    # 离开时**必须还原成 print**：界面上的日志控件会被销毁，留着这个
    # 钩子的话下次从命令行 `--run desktop_watch` 会往一个死控件里写。
    desktop_watch.set_log(log)
    desktop_exam.set_log(log)
    try:
        if WATCH in keys:
            log("")
            log("=" * 78)
            log("[run] ① 自动看课（浏览器版）")
            log("=" * 78)
            result = desktop_watch.main([]) or result
            if desktop.should_stop():
                log("[run] 收到停止，后面的任务不做了")
                return result

        # 考核和问卷在同一个脚本里，而且**顺序不能拆**：平台规定考核
        # 没到 60 分就不放行问卷（"请先完成课程学习，再进行问卷作答"）。
        # 所以 `d_exam` 和 `d_finish` 都指向它，区别只在要不要带 `--finish`。
        if EXAM in keys or FINISH in keys:
            argv: list[str] = []
            if FINISH in keys:
                # `--finish` 会**顺带**把考核和问卷也做掉（都过了就是空跑，
                # 一门课几秒钟）。结了课之后就不能再刷分了，所以这个旗标
                # 只在界面上勾了「申请结课」时才给。
                argv.append("--finish")
            log("")
            log("=" * 78)
            log("[run] ② 考核 + 问卷" + ("+ 申请结课" if FINISH in keys else ""))
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
