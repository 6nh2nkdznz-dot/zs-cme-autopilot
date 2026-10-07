"""应用名字与版本号 —— 唯一来源。

## 为什么单独放一个文件

原先 `launcher.py` 和 `launcher_ui.py` **各写了一份**：

    launcher.py:48     APP_VER = "0.2.0"
    launcher_ui.py:27  APP_VER = "0.3.0"

于是同一个程序，**从 exe 启动显示 v0.2.0、直接跑源码显示 v0.3.0**
（exe 走 `launcher.py`，`--classic`/源码直跑走 `launcher_ui.py`）。
改界面那次只更新了一处，另一处就悄悄过期了。

版本号这种东西只该有一个出处 —— 加到这里，两边 import 同一个常量，
以后不会再漂。

## 什么时候要改

对外可见的行为变化（新功能、修复用户能感知的问题）就往上加一位。
正式发版建议同时打个 git tag，例如 `git tag v0.3.1`。
"""

from __future__ import annotations

APP_TITLE = "中山医院继续教育平台助手"

#: 当前版本。改动见模块开头的说明。
APP_VER = "0.3.1"
