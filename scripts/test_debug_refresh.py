"""调试视图「画面会不会跟着新帧刷新」的回归测试。

## 为什么单独写这个文件（用户实测报的 bug）

用户原话：

    右边识别的页面没有实时更新，只有文字更新了

根因在 `scripts/debug_view.py` 的 `DebugPanel._render()`：它只在**缩放变了**
的时候才重画画面 ——

    if abs(scale - self._last_scale) > 0.005:      # 旧代码，少了后半句
        self._last_scale = scale
        self._photo = ImageTk.PhotoImage(...)      # 新画面在这里才生成
        need_redraw = True

而右边明细是**每次 `_tick` 都重写**的。于是采集线程每 3 秒抓到的新画面
一直躺在 `self._frame` 里**没人用** —— 文字一直变、画面冻在第一帧上。
看现象很像「识别卡住了」，但采集其实一直是好的。

## 这个测试怎么验

不碰真设备、不碰 OCR，也不碰采集线程：直接把帧塞进面板，用**真 `mainloop()`**
跑几个 tick，看画面有没有跟着换。

两个必须守住的点：

1. **来了新帧就要重画**（这次修的）
2. **同一帧不要重复重画**（否则每 300ms 白做一次 LANCZOS 缩放）

## 踩过的坑：必须用真 `mainloop()`

用 `root.update_idletasks(); root.update()` 轮询是不行的 ——
`after` 回调只在 `update()` 处理事件时才执行一次，
「循环里看到帧有变化」之后若不继续 `update()` 就不会渲染。
**验证 GUI 一律用真 `mainloop()` + `root.after(ms, ...)`。**
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from PIL import Image  # noqa: E402

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


def _fake_frame(seq: int, color: tuple[int, int, int]) -> dict:
    """造一帧，画面是纯色 —— 换个颜色就等于「换了一帧」。"""
    from debug_view import CANVAS_H, CANVAS_W

    img = Image.new("RGB", (CANVAS_W, CANVAS_H), color)
    img.labels = []                       # type: ignore[attr-defined]
    return {
        "seq": seq,
        "base": img,
        "labels": [],
        "rows": [],
        "page": "course",
        "circles": [],
        "full_text": "",
    }


def main() -> int:
    print("=" * 68)
    print(" 调试视图画面刷新自测")
    print("=" * 68)

    src = (ROOT / "scripts" / "debug_view.py").read_text(encoding="utf-8")

    print("\n[1] 源码契约：帧序号必须三处都在")
    #
    # 少任何一处，这个 bug 就会以不同形式回来：
    #   少 `self._seq += 1`  → 序号永远不变，又回到「只更新文字」
    #   少 `seq != self._last_seq` → 同上
    #   少 `"seq": self._seq` → 主线程拿到的一直是 0
    check_true("__init__ 里有 self._seq = 0", "self._seq = 0" in src)
    check_true("__init__ 里有 self._last_seq = -1", "self._last_seq = -1" in src)
    check_true("采集侧自增 self._seq += 1", "self._seq += 1" in src)
    check_true('帧字典里带 "seq"', '"seq": self._seq' in src)
    check_true("_render 里比较 seq",
               "seq != self._last_seq" in src)
    check_true("_render 里更新 _last_seq", "self._last_seq = seq" in src)
    check_true("缩放判断还在（拖窗口不能变糊）",
               "abs(scale - self._last_scale) > 0.005" in src)

    print("\n[2] 真起一个内嵌面板，看画面跟不跟新帧")
    import customtkinter as ctk

    import debug_view as dv

    root = ctk.CTk()
    root.geometry("900x700")
    holder = ctk.CTkFrame(root)
    holder.pack(fill="both", expand=True)
    panel = dv.DebugPanel(holder, standalone=False)

    results: dict[str, object] = {}

    def stage1() -> None:
        """塞第一帧，记下画布上图片的 id。"""
        panel._render(_fake_frame(1, (200, 0, 0)), 0.30)
        root.update_idletasks()
        items = panel.canvas.find_all()
        results["items1"] = len(items)
        results["photo1"] = panel._photo
        results["seq1"] = panel._last_seq
        # 同一帧再渲染一次：不该换 PhotoImage（免得每 300ms 白做一次缩放）
        panel._render(_fake_frame(1, (200, 0, 0)), 0.30)
        results["photo_same"] = panel._photo is results["photo1"]
        root.after(120, stage2)

    def stage2() -> None:
        """关键一步：**缩放不变、只换帧** —— 这正是原来不刷新的情况。"""
        panel._render(_fake_frame(2, (0, 0, 200)), 0.30)
        root.update_idletasks()
        results["photo2"] = panel._photo
        results["seq2"] = panel._last_seq
        results["items2"] = len(panel.canvas.find_all())
        root.after(120, finish)

    def finish() -> None:
        root.quit()

    root.after(200, stage1)
    root.mainloop()

    check("第一帧渲染后有图片", results.get("items1"), 1)
    check("第一帧的 seq 记下了", results.get("seq1"), 1)
    check_true("同一帧重复渲染不换 PhotoImage",
               bool(results.get("photo_same")),
               "重复渲染会白做一次 LANCZOS 缩放")
    check("**来了新帧就换画面**（缩放没变）", results.get("seq2"), 2)
    check_true("新帧确实生成了新的 PhotoImage",
               results.get("photo2") is not results.get("photo1"),
               "画面冻在上一帧上 —— 就是用户报的『只有文字更新』")
    check("画布上仍然只有一张图（没叠图）", results.get("items2"), 1)

    try:
        panel.stop()
        root.destroy()
    except Exception:  # noqa: BLE001 - 收尾失败不影响判定
        pass

    print("\n" + "=" * 68)
    print(f" 结果: {_pass} 通过 / {_fail} 失败")
    print("=" * 68)
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
