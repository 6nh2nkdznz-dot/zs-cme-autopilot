"""`launcher_ui.debug_view_cmd()` 的单元测试。

这个函数是补出来的 —— 原先「启动调试视图」的命令行是**内联写在**
`on_debug_view()` 里的，打包后拼错了（用 exe 自己当 python 解释器，
参数又被 `launcher.main()` 忽略），结果点按钮会**又弹一个主界面窗口**。
当时没有任何测试覆盖这条路径，所以没被发现。

## 测试怎么做的

`launcher_ui.py` 一 import 就会拉起 customtkinter 和 core，所以这里
**不 import 那个模块**，而是把 `debug_view_cmd()` 的源码抽出来单独执行 ——
它只依赖 `sys` 和 `paths` 两个名字，剥出来跑没有副作用。

改动这个函数时如果测试挂了，先看是不是又改回了「直接用 sys.executable
跑脚本路径」那种写法。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "launcher_ui.py"

sys.path.insert(0, str(ROOT / "scripts"))

_fail = 0
_pass = 0


def check(name: str, got, want) -> None:
    global _fail, _pass
    if got == want:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}\n          期望 {want!r}\n          实际 {got!r}")


def check_true(name: str, cond: bool, extra: str = "") -> None:
    global _fail, _pass
    if cond:
        _pass += 1
        print(f"  PASS  {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}  {extra}")


def _load_debug_view_cmd():
    """把 debug_view_cmd 的源码从 launcher_ui.py 里抠出来执行。

    直接 import launcher_ui 会连带拉起 customtkinter + core（还可能弹窗），
    测试不该有那种副作用。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "debug_view_cmd"),
              None)
    if fn is None:
        raise SystemExit("launcher_ui.py 里找不到 debug_view_cmd()")
    mod = ast.Module(body=[fn], type_ignores=[])
    code = compile(ast.fix_missing_locations(mod), str(SRC), "exec")

    import paths  # noqa: F401 - 供被抠出来的代码使用

    ns: dict = {"sys": sys, "Path": Path, "paths": paths}
    exec(code, ns)  # noqa: S102 - 只执行自己仓库里的一段函数定义
    return ns["debug_view_cmd"]


print("[1] debug_view_cmd 存在且可解析")
try:
    cmd_fn = _load_debug_view_cmd()
    check_true("成功取出函数", callable(cmd_fn))
except SystemExit as exc:
    check_true("成功取出函数", False, str(exc))
    print("\n无法继续")
    raise SystemExit(1)

print("\n[2] 源码环境：用当前解释器跑脚本文件")
# 当前跑的测试进程是源码环境（sys.frozen 未设置）
check("sys.frozen 未设置", getattr(sys, "frozen", False), False)
cmd, cwd = cmd_fn()
check("第一个参数是当前解释器", cmd[0], sys.executable)
check_true("第二个参数是 debug_view.py 的路径",
           cmd[1].endswith("debug_view.py"), f"实际 {cmd[1]}")
check("命令只有两个参数", len(cmd), 2)
check_true("脚本确实存在", Path(cmd[1]).is_file(), f"实际 {cmd[1]}")
check_true("工作目录是项目根（assets/ 在它下面）",
           (Path(cwd) / "assets").is_dir(), f"实际 {cwd}")

print("\n[3] 打包环境：必须走 exe 自己的 --run 入口")
# 模拟 frozen：这是关键回归点。
# 原来的写法在 frozen 下会拼成 `ZSCMEAutopilot.exe <脚本路径>`，
# 而 launcher.main() 会忽略那个路径参数、直接开主界面。
saved = getattr(sys, "frozen", None)
sys.frozen = True          # type: ignore[attr-defined]
try:
    cmd_f, cwd_f = cmd_fn()
finally:
    if saved is None:
        del sys.frozen      # type: ignore[attr-defined]
    else:
        sys.frozen = saved  # type: ignore[attr-defined]

check("用 exe 自身", cmd_f[0], sys.executable)
check("带上 --run", cmd_f[1], "--run")
check("脚本名是 debug_view", cmd_f[2], "debug_view")
check("命令只有三个参数", len(cmd_f), 3)
check_true("没有把脚本路径当参数传（那会被 main() 忽略）",
           not any(str(a).endswith(".py") for a in cmd_f),
           f"实际 {cmd_f}")
check_true("工作目录是 exe 所在目录",
           Path(cwd_f).resolve() == Path(sys.executable).resolve().parent,
           f"实际 {cwd_f}")

print("\n[4] launcher.main() 确实认 --run（两边对得上）")
# 上面拼的命令是 `exe --run debug_view`，得有对应的分发才算数。
launcher_src = (ROOT / "launcher.py").read_text(encoding="utf-8")
check_true("main() 里有 --run 分支", '"--run" in sys.argv' in launcher_src)
check_true("_run_script 会调用脚本的 main/__main__",
           "runpy.run_path" in launcher_src, "用的是 runpy")
check_true("debug_view.py 有 __main__ 块（runpy 才跑得起来）",
           'if __name__ == "__main__":' in
           (ROOT / "scripts" / "debug_view.py").read_text(encoding="utf-8"))
check_true("debug_view.py 有 main()",
           "\ndef main()" in
           (ROOT / "scripts" / "debug_view.py").read_text(encoding="utf-8"))

print("\n[5] 界面按钮确实接上了这个函数")
ui_src = SRC.read_text(encoding="utf-8")
check_true("左栏有「调试视图」按钮", "调试视图" in ui_src)
check_true("按钮 command 指向 on_debug_view",
           "command=self.on_debug_view" in ui_src)
check_true("on_debug_view 内部调用 debug_view_cmd",
           "debug_view_cmd()" in ui_src)
check_true("on_debug_view 不再自己拼 sys.executable（防回退）",
           "_sys.executable" not in ui_src)

print("\n" + "=" * 68)
print(f" 结果: {_pass} 通过 / {_fail} 失败")
print("=" * 68)
raise SystemExit(1 if _fail else 0)
