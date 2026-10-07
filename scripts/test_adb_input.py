"""adb 输入命令拼装的自测。

## 为什么单独测这个

踩过一个**静默失败**的坑：`adb shell input text` 没带 `-s <serial>`。
本机 adb 同时看到两个设备（MuMu 的 127.0.0.1:16384 和另一个 emulator-5554），
不带 `-s` 时 adb 直接 `more than one device/emulator` 退出 1，**什么都不输入**。

后果很隐蔽：视频里的评分弹题永远填不上 → 弹题永不关闭 → 视频永久暂停。
日志里只有一行 `adb input text 返回 1`，很容易被当成偶发错误忽略。

更值得记的是**它骗过了一次隔离测试**：那次我在命令行手工写了 `-s`，
所以「能用」；而代码里地址取自 `config['adb']['address']`，
自动探测模式下那一项是**空字符串**，于是 `-s` 根本没拼上。

所以这里把命令拼装抽成纯函数单测，不依赖设备。

运行:
    python scripts\\test_adb_input.py
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

from main import build_adb_input_cmd  # noqa: E402

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


class FakeCtx:
    """模仿 AdbController 的 info 结构。"""

    def __init__(self, info=None):
        if info is not None:
            self.info = info


class FakeTasker:
    def __init__(self, controller):
        self.controller = controller


class FakeCallbackContext:
    """模仿 `maa.context.Context` —— **生产里实际传进来的就是这个**。

    ## 为什么必须单独测这个形态

    踩过一次很有代表性的坑：`_adb_input_text(context, ...)` 只按
    「context 本身有 `.info`」去取设备序列号。隔离测试时我直接传了
    **控制器**对象，所以「能用」；而回调里框架传进来的是
    `maa.context.Context`，它**没有** `.info` → 序列号为空 →
    `adb shell input text` 不带 `-s` → 多设备环境直接
    `more than one device/emulator` 失败 → **弹题永远填不上、视频永久暂停**。

    测试载体和生产不一致，这种差异靠肉眼看代码发现不了。
    所以两种形态都要在测试里显式覆盖。
    """

    def __init__(self, controller):
        self.tasker = FakeTasker(controller)


REAL_INFO = {
    "adb_path": "C:/Program Files/Netease/MuMu/nx_device/15.0/shell/adb.exe",
    "adb_serial": "127.0.0.1:16384",
    "type": "adb",
}


def main() -> int:
    print("=" * 68)
    print(" adb 输入命令拼装自测")
    print("=" * 68)

    print("\n[1] 正常情况：必须带 -s")
    cmd = build_adb_input_cmd(FakeCtx(REAL_INFO), "83")
    check("完整命令", cmd, [
        "C:/Program Files/Netease/MuMu/nx_device/15.0/shell/adb.exe",
        "-s", "127.0.0.1:16384",
        "shell", "input", "text", "83",
    ])
    check_true("含 -s", "-s" in cmd)
    check("-s 后面紧跟序列号",
          cmd[cmd.index("-s") + 1] if "-s" in cmd else "", "127.0.0.1:16384")
    check_true("序列号在 shell 之前",
               cmd.index("127.0.0.1:16384") < cmd.index("shell"))

    print("\n[2] 这就是踩过的坑：info 里没有 serial")
    cmd2 = build_adb_input_cmd(FakeCtx({"adb_path": "adb.exe"}), "83")
    check_true("没有 serial 时不带 -s（但会打印警告）", "-s" not in cmd2)
    check("文本仍然拼上", cmd2[-1], "83")

    print("\n[3] 兜底")
    cmd3 = build_adb_input_cmd(None, "5")
    check("没有 context 时用裸 adb", cmd3, ["adb", "shell", "input", "text", "5"])
    cmd4 = build_adb_input_cmd(FakeCtx({}), "5")
    check("info 为空字典时也用裸 adb",
          cmd4, ["adb", "shell", "input", "text", "5"])

    print("\n[4] 文本内容")
    check("纯数字", build_adb_input_cmd(FakeCtx(REAL_INFO), 100)[-1], "100")
    check("int 会被转成 str",
          isinstance(build_adb_input_cmd(FakeCtx(REAL_INFO), 77)[-1], str), True)

    print("\n[5] 各种取值不影响结构")
    for val in ("0", "50", "100"):
        c = build_adb_input_cmd(FakeCtx(REAL_INFO), val)
        check_true(f"值 {val} 时结构正确",
                   c[-4:] == ["shell", "input", "text", val],
                   f"实际 {c[-4:]}")

    print("\n[6] 回调 Context 形态（生产实际形态，曾漏掉导致线上失败）")
    direct = build_adb_input_cmd(FakeCtx(REAL_INFO), "83")
    via_ctx = build_adb_input_cmd(FakeCallbackContext(FakeCtx(REAL_INFO)), "83")
    check("经回调 Context 拼出的命令", via_ctx, direct)
    check_true("回调形态也带 -s", "-s" in via_ctx)
    check_true("回调形态带回真实序列号",
               "127.0.0.1:16384" in via_ctx,
               f"实际 {via_ctx}")

    print("\n[7] tasker 上没有 controller 时不该崩")
    class NoController:
        tasker = object()

    c = build_adb_input_cmd(NoController(), "5")
    check("退回裸 adb", c, ["adb", "shell", "input", "text", "5"])

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
