"""用 UI Automation 把界面上的日志文字读出来，用来核对「新文案是否生效」。

## 为什么要用它

改文案之后，靠截图看日志有两个问题：
  1. 日志面板一屏只显示十几行，后面的看不到；
  2. 要放大才认得出字，容易看错。

Windows 的 UI Automation 能直接读到 Tk 文本框里的字符串，
比截图可靠，也省得把窗口拉大。

## 局限

Tk 的**按钮**不暴露 UIA 名字（实测 125 个后代元素名字全为空），
所以这个脚本只能读文本，不能用来点按钮。

用法:
    python scripts/ui_read_text.py                # 所有 tk 窗口的文本
    python scripts/ui_read_text.py 助手            # 只读标题含该关键词的窗口
"""

from __future__ import annotations

import sys

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover - 环境没装就给出办法
    print("需要 uiautomation: pip install uiautomation")
    raise SystemExit(2) from None


def _walk(ctrl, depth: int = 0, out: list | None = None) -> list[str]:
    """深度优先收集所有非空文本。"""
    if out is None:
        out = []
    try:
        name = (ctrl.Name or "").strip()
        # Tk 的 Text 控件类型是 Document；Edit 是单行输入框
        if name and ctrl.ControlTypeName in ("DocumentControl", "EditControl",
                                             "TextControl"):
            out.append(name)
    except Exception:  # noqa: BLE001 - UIA 偶尔对失效元素抛异常
        pass
    if depth > 12:
        return out
    try:
        for child in ctrl.GetChildren():
            _walk(child, depth + 1, out)
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    root = auto.GetRootControl()
    found = 0
    for win in root.GetChildren():
        try:
            title = (win.Name or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not title or (want and want not in title):
            continue
        # Tk 顶层窗口的类名是 TkTopLevel
        if win.ClassName != "TkTopLevel":
            continue
        found += 1
        print(f"\n{'=' * 70}\n窗口: {title}\n{'=' * 70}")
        texts = _walk(win)
        for i, t in enumerate(texts, 1):
            for line in t.splitlines():
                if line.strip():
                    print(f"{i:3d} | {line}")
                    i += 1
    if not found:
        print(f"没找到 Tk 窗口{'（关键词: ' + want + '）' if want else ''}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
