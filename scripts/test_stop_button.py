# -*- coding: utf-8 -*-
"""「开始/停止」那一个按钮的状态机测试。

## 为什么单独为它写一个文件

用户 2026-10-10 报：「停止后无法点击开始运行」。

复现出来的东西**光看源码看不出来**（`debug/_probe_stopbtn.py`）：

    停止后 0.50s → _busy=False  state=disabled  text='▶  开始运行'
    ……一直保持 disabled，按钮点不动

根因是「谁负责把按钮解灰」这件事被写岔了：
`on_stop()` 把主按钮 `state="disabled"` 挂一个 3 秒后的恢复回调，而
`_set_busy(False)` 只改文案**从不碰 `state`**，那个恢复回调又写成
`if _busy: 恢复` —— 停止生效很快（浏览器版所有等待都切成 0.2 秒的小片），
worker 往往几百毫秒就结束了，等 3 秒后回调跑起来 `_busy` 已经是假的，
于是那个 `if` 不成立，按钮永远停在「看着能点、实际点不动」。

它是**状态机**的错，不是某一行拼错，所以这里既跑真窗口验状态，
也用源码断言钉住「解灰必须无条件」。
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老终端不支持就算了
        pass

PASS = 0
FAIL = 0


def check(name: str, got, want=True) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   期望 {want!r} 实际 {got!r}")


def check_true(name: str, cond, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}   {detail}")


SRC = ""

#: `launcher_ui.py` 的路径。**不能靠 `__file__` 往上两级算** —— 打好的 exe 里
#: 这个测试脚本被放在 `_internal/scripts/`，往上一级是 `_internal/`，那儿没有
#: `launcher_ui.py`（它被编译进 exe 了）。实测就是这么炸的：
#: `FileNotFoundError: '...\\_internal\\launcher_ui.py'`。
#: 所以改成问**已经导入的模块**要 `__file__`，拿不到就跳过 [1] 节
#: （[2] 节用的是导入进来的模块对象，打包版照样能跑）。
try:
    import launcher_ui as _ui_mod

    _p = Path(getattr(_ui_mod, "__file__", "") or "")
    if _p.is_file():
        SRC = _p.read_text(encoding="utf-8")
except Exception:  # noqa: BLE001 - 打包版/无 GUI 环境下拿不到源码很正常
    pass


# ------------------------------------------------------------------ [1] 源码

def section_source() -> None:
    print("[1] 源码：解灰必须无条件")
    if not SRC:
        print("  SKIP  拿不到 launcher_ui.py 的源码（打包版里它被编译进 exe），跳过")
        return

    def body(name: str) -> str:
        """抠出某个方法的源码（到下一个同缩进的 `def` 为止）。"""
        i = SRC.find(f"    def {name}(")
        if i < 0:
            return ""
        j = SRC.find("\n    def ", i + 10)
        return SRC[i:j if j > 0 else len(SRC)]

    rs = body("_restore_stop_button")
    check_true("_restore_stop_button 存在", bool(rs))
    check_true("★ 解灰是无条件的（不再被 if _busy 挡住）",
               "self.btn_run.configure(state=\"normal\")" in rs, rs[:200])
    # 老写法：整个恢复动作都塞在 if 里，_busy 变假就整段跳过 —— 就是那个 bug。
    check_true("★ 没有 `if _busy` 包住解灰那种写法",
               "if getattr(self, \"_busy\", False):\n            "
               "self.btn_run.configure(state=\"normal\"" not in rs)

    sb = body("_set_busy")
    check_true("_set_busy 里两处 btn_run.configure 都带 state=\"normal\"",
               sb.count("state=\"normal\",") >= 2, f"实际 {sb.count('state=\"normal\",')} 处")
    check_true("_set_busy 仍会把按钮切成红色「■  立即停止」",
               "\"■  立即停止\"" in sb)
    check_true("_set_busy 仍会把按钮切回蓝色「▶  开始运行」",
               "\"▶  开始运行\"" in sb)

    so = body("on_stop")
    check_true("on_stop 仍然临时置灰按钮（防连点）",
               "state=\"disabled\", text=\"正在停止…\"" in so)
    check_true("on_stop 仍然挂了 3 秒后的恢复回调",
               "self.root.after(3000, self._restore_stop_button)" in so)


# ------------------------------------------------------------- [2] 真窗口状态机

def section_runtime() -> None:
    """跑真的 `App` + 真的 `on_run_or_stop`，只把 `desktop_runner.run` 换成假的。"""
    print()
    print("[2] 真窗口：点开始 → 点停止 → 再点开始")

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 非 Windows 或已设过
        pass

    import customtkinter as ctk

    import desktop
    import desktop_runner
    import launcher_ui as ui

    calls: list[str] = []

    def fake_run(keys, log=None, should_stop=None):
        calls.append("run")
        desktop.set_stop_check(should_stop)
        try:
            while True:
                # 和真代码同一套停止语义：循环顶查 + 可打断的 nap。
                if desktop.should_stop():
                    return 0
                if desktop.nap(0.2):
                    return 0
        finally:
            desktop.set_stop_check(None)

    desktop_runner.run = fake_run

    root = ctk.CTk()
    root.withdraw()          # 测试不要把窗口弹到人脸上
    app = ui.App(root)
    root.update()

    def pump(seconds: float, step: float = 0.05) -> None:
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(step)

    def state() -> tuple[str, str]:
        return str(app.btn_run.cget("state")), str(app.btn_run.cget("text"))

    # --- 1. 点「开始运行」
    app.on_run_or_stop()
    pump(0.6)
    st, tx = state()
    check("开始后 _busy 为真", app._busy, True)
    check("开始后按钮可点", st, "normal")
    check("开始后按钮文案变红字", tx, "■  立即停止")
    check("开始后 worker 在跑", bool(app.worker and app.worker.is_alive()), True)
    check("fake_run 被调用一次", len(calls), 1)

    # --- 2. 点「立即停止」，再等过那 3 秒的恢复窗口
    app.on_run_or_stop()
    pump(4.0)
    st, tx = state()
    check("停止后 _busy 变假", app._busy, False)
    check("停止后 worker 结束", bool(app.worker and app.worker.is_alive()), False)
    check("停止后按钮文案回到「开始运行」", tx, "▶  开始运行")
    # ★ 这一条就是用户报的 bug：老代码在这里是 disabled。
    check("★ 停止后按钮**可以再点**", st, "normal")

    # --- 3. 再点一次，必须真的能重新跑起来
    app.on_run_or_stop()
    pump(0.5)
    check("★ 再点一次能重新开始跑", len(calls), 2)
    check("再点之后 _busy 又是真", app._busy, True)

    # --- 4. 「没停住」那条路：worker 迟迟不退，3 秒后按钮该解灰且文案回「立即停止」
    app.on_run_or_stop()      # 先停掉第 3 步那个
    pump(1.0)

    def stubborn(keys, log=None, should_stop=None):
        calls.append("stubborn")
        end = time.time() + 6.0
        while time.time() < end:      # 故意不理停止
            time.sleep(0.05)
        return 0

    desktop_runner.run = stubborn
    app.on_run_or_stop()
    pump(0.4)
    app.on_run_or_stop()      # 点停止
    pump(0.4)
    check("刚点停止时按钮是灰的", state()[0], "disabled")
    pump(3.2)                 # 越过 3 秒的恢复窗口（这时 worker 还没退）
    st, tx = state()
    check("★ 没停住时按钮也要解灰（不然就没法再点一次）", st, "normal")
    check("没停住时文案还是「立即停止」", tx, "■  立即停止")

    pump(4.0)                 # 等 stubborn 自己跑完，收尾
    app._set_busy(False)
    root.update()
    root.destroy()


def main() -> int:
    section_source()
    section_runtime()
    print()
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
