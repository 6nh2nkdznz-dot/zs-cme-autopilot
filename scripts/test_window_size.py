"""主界面窗口尺寸/位置的自检（不启动界面）。

用户反馈过两次窗口问题：
  1. 「窗口高度太高了」—— 原来 `h = (work_px - 16) / scale`，直接把窗口开成
     整个工作区那么高，顶天立地、左下角还空一大块。
  2. 改矮之后**底部仍然露在任务栏下面** —— 因为 `geometry("WxH")` 只改尺寸、
     不改位置，窗口停在旧坐标上。

这个脚本把 `launcher_ui.py` 里的算式**抄成纯函数**，在多种屏幕/DPI 组合下
验算，替代「起窗口、肉眼截屏」那种既慢又容易看错的口径。抄写有漂移风险，
所以最后几项断言会去源码里核对关键代码仍在。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "launcher_ui.py").read_text(encoding="utf-8")

#: 与 `launcher_ui.py` 常量保持一致
WIN_TARGET_H = 900
FRAME_W = 16          # 外框比 client 宽出的部分（实测 611-595）
FRAME_H = 39          # 外框比 client 高出的部分（实测 694-655）

_passed = 0
_failed = 0


def check(name: str, got, want=True) -> None:
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}\n           期望 {want!r}\n           实际 {got!r}")


def fit_height(work_logical: int, left_content: int) -> int:
    """复刻 `App._fit_window()` 的高度算式（逻辑像素）。"""
    need = left_content + 96
    cap = work_logical - 40
    return int(max(660, min(need + 60, WIN_TARGET_H, cap)))


def place(area_left: int, area_top: int, area_w: int, area_h: int,
          win_w: int, win_h: int) -> tuple[int, int]:
    """复刻 `App._center_in_work_area()` 的位置算式（逻辑像素）。"""
    x = area_left + max(0, (area_w - win_w) // 2)
    y = area_top + max(0, (area_h - win_h) // 2)
    return x, y


print("[1] 高度算式：正常屏幕下就是 WIN_TARGET_H")

# 本机实测：工作区逻辑高 1018，左栏内容 856
check("本机 1018/856 → 900", fit_height(1018, 856), WIN_TARGET_H)
check("大屏也一样封顶", fit_height(1400, 856), WIN_TARGET_H)

print("\n[2] 高度算式：不会再顶满工作区")

for work, content in ((1018, 856), (1400, 856), (1040, 900), (728, 600)):
    h = fit_height(work, content)
    check(f"工作区 {work} 时窗口 {h} < {work}", h < work, True)
    check(f"工作区 {work} 时窗口 {h} 不超工作区", h <= work - 40, True)

print("\n[3] 高度算式：矮屏要退让，但不能退到装不下")

#: 窗口高 → 左栏可视高 的换算。
#:
#: **不是我推的，是量出来的**：本机窗口 900 逻辑px（1350 物理）时，左栏视口
#: 实测 1308 物理px，也就是窗口高里有 42 物理px（28 逻辑px）是标题栏等外框。
#: 所以可视高 ≈ (窗口高 - 28) × 1.5。
#: 这里是**反直觉**的地方：窗口 900 逻辑px 只等于 872 逻辑px 的可视高，
#: 我一开始按「900 - 96 = 804」估，得出「900 装不下内容 856」的错误结论。
#: 实测是装得下的 —— 因为标题栏那部分在逻辑坐标里是固定的 28px，
#: 不跟着缩放。
CHROME_LOGICAL = 28


def left_viewport_logical(win_h: int, content: int) -> int:
    """窗口高 win_h 时，左栏能看到多少逻辑px的内容。

    内容比可视高还高时会被滚动裁剪，所以取较小值。
    """
    return min(content, int((win_h - CHROME_LOGICAL) * 1.5))


def content_visible(work_h: int, content: int) -> bool:
    """左栏内容能否**全部**显示、不需要滚动。"""
    return left_viewport_logical(fit_height(work_h, content), content) >= content


check("本机 1018/856：内容全显示、不用滚", content_visible(1018, 856))
check("回归实测：900 逻辑px 窗口 → 视口 ≥ 856", 
      left_viewport_logical(900, 856) >= 856)
check("装得下 856 内容的最低窗口高是 599", left_viewport_logical(599, 856) >= 856)
check("598 就装不下了，左栏出滚动条",
      left_viewport_logical(598, 856) < 856)
# 注意：高度算式有 660 的下限，所以正常屏幕下窗口**永远**高到装得下内容。
# 滚动条只在用户的屏幕比 599 逻辑px 还矮时才可能用到 —— 那时窗口本来就
# 超出屏幕，滚动是唯一合理的表现。
check("算式下限 660 > 临界 599，所以正常屏幕不会滚", fit_height(500, 856), 660)
check("下限 660 也够装内容", content_visible(660, 856))

print("\n[4] 位置算式：窗口必须落进工作区（这才是「露在任务栏下」的根因）")

cases = [
    # (工作区左, 上, 宽, 高, 窗口宽, 窗口高)
    (0, 0, 1707, 1019, 1200, 900),
    (0, 0, 1707, 1019, 1200, 978),
    (0, 0, 1280, 680, 1200, 640),
    (0, 0, 2560, 1400, 1400, 900),
    (0, 40, 1920, 1000, 1300, 960),      # 任务栏在顶部
    (-1920, 0, 1920, 1040, 1200, 900),   # 副屏在主屏左边
]
for (al, at, aw, ah, ww, wh) in cases:
    x, y = place(al, at, aw, ah, ww, wh)
    inside = (x >= al and y >= at and x + ww <= al + aw and y + wh <= at + ah)
    check(f"工作区({al},{at},{aw}x{ah}) 窗口{ww}x{wh} → 位置({x},{y}) 放得下",
          inside)

print("\n[5] 位置算式：居中，且不会算出负偏移")


def _off(v, lo, span, size) -> int:
    return max(0, (span - size) // 2)


check("窗口比工作区窄 507 → 左偏 253", _off(0, 0, 1707, 1200), 253)
check("窗口比工作区高 119 → 上偏 59", _off(0, 0, 1019, 900), 59)
check("窗口比工作区还大时偏移 0（不推出去）", _off(0, 0, 800, 1200), 0)

print("\n[6] 源码没漂：关键代码还在")

check("常量 WIN_TARGET_H = 900", bool(re.search(r"^WIN_TARGET_H = 900", SRC, re.M)))
check("高度算式有 WIN_TARGET_H", "min(need + 60, WIN_TARGET_H, cap)" in SRC)
check("调了 _center_in_work_area", "self._center_in_work_area(w, h)" in SRC)
check("_center_in_work_area 有定义", "def _center_in_work_area(" in SRC)
check("位置用 getworkarea(0x0030)", "0x0030" in SRC)
check("位置也走 _to_logical 折算",
      SRC.count("self._to_logical(rect.left)") == 1
      and SRC.count("self._to_logical(rect.top)") == 1)
check("窗口宽下限 1080",
      "w = int(max(1080, min(left_need + right_need, 1500)))" in SRC)
check("左栏列宽用 LEFT_COL_W",
      "root.grid_columnconfigure(0, weight=0, minsize=LEFT_COL_W)" in SRC)
# 左栏 480 + 右栏日志 700×1.5=1050（物理）= 1530 → 封顶 1500
check("窗口宽够放下左栏 + 右栏", 480 + 700 * 1.5 >= 1080)

print("\n[6b] 版本号只出现一次（别在标题下一行又写一遍）")

check("窗口标题带版本", 'root.title(f"{APP_TITLE} v{APP_VER}")' in SRC)
check("副标题不再重复版本号", "v{APP_VER} · 中山医院远程教育" not in SRC)

print("\n[7] 源码没漂：wraplength 必须容得下左栏可用宽度")


def desc_avail(col_w: int, col_padx: int = 21, scroll_shrink: int = 56,
               card_padx: int = 28, indent: int = 46, margin: int = 5) -> int:
    """左栏里任务描述真正可用的宽度（逻辑px）。

    逐段减：
      列宽 → 减去 `col.grid(padx=(14, 7))` 的 21
           → 减去滚动框相对列的收窄（实测 480 → 424，收 56）
           → 减去卡片左右 padx 14×2
           → 减去描述左缩进 46（`padx=(46, 0)`）
           → 再留 margin 的余量
    """
    return col_w - col_padx - scroll_shrink - card_padx - indent - margin


m = re.search(r"^LEFT_COL_W = (\d+)", SRC, re.M)
check("能读到 LEFT_COL_W", bool(m))
col = int(m.group(1))
check(f"LEFT_COL_W = {col}", col, 480)
avail = desc_avail(col)
check(f"可用宽度算出来是 {avail}", avail, 324)

# wraplength 是 `LEFT_COL_W - 156`，得把它们还原成实际数值再比。
w = re.search(r"wraplength=LEFT_COL_W - (\d+)", SRC)
check("能读到 wraplength 的推导式", bool(w))
wrap = col - int(w.group(1))
check(f"wraplength 实际是 {wrap}", wrap, 324)

# 描述必须用原生 tk.Label。CTkLabel 会把高度锁死在 42px（实测：写不写
# height 都一样），只装得下 3 行 10 号字；折成 4 行的描述第 4 行整行被
# 吃掉，症状看着像「右边被截」，实际是丢了一整行 —— 这个坑绕了很久，
# 所以用测试把它钉住，防止以后有人「顺手改回 CTkLabel 统一风格」。
check("描述用的是原生 tk.Label",
      re.search(r"tk\.Label\(\s*\n\s*wrap, text=desc", SRC) is not None)
check("描述不再用 CTkLabel 渲染",
      re.search(r"ctk\.CTkLabel\(\s*\n\s*wrap, text=desc", SRC) is None)
check("原生 Label 补了底色（否则卡片上会留一块突兀的方块）",
      "bg=COL_CARD" in SRC)
check(f"wraplength({wrap}) ≤ 可用宽度({avail})", wrap <= avail)
check("wraplength 也没小得离谱（≥ 可用宽度 - 30）", wrap >= avail - 30)

print("\n[8] 外框尺寸也要算进去（不然「算着放得下、实际被裁」）")


def frame_size(logical_w: int, logical_h: int, scale: float) -> tuple[int, int]:
    return (int(logical_w * scale) + FRAME_W, int(logical_h * scale) + FRAME_H)


fw, fh = frame_size(480 + 1050, 900, 1.0)
check(f"1200x900 → 外框 {fw}x{fh}", (fw, fh), (1546, 939))
check("外框仍小于工作区 2560x1528", fw < 2560 and fh < 1528)

print("\n[9] 右栏：日志 / 调试视图 互斥切换（内嵌，不再开独立窗口）")
# 用户要求：「把调试窗口去了，把右边的空白处改成调试模式显示的东西」。
# 这一组钉住三件事，防止以后被「顺手简化」掉：
#   1. 右栏两列都 weight=0 —— 否则富余宽度会让右栏（或左栏）被拉变形，
#      更早的写法「两边都 weight=0」则让窗口右侧留一大块死空白。
#   2. 用 `grid_remove()` 隐藏日志，不是 `destroy()` —— destroy 之后再
#      切回来就得重建，日志内容（跑了几小时的那份）会丢。
#   3. 调试面板懒加载 —— 它要起采集线程、载入 OCR 模型（几秒），
#      不想看调试的人不该为它付启动开销。
check("左栏列 weight=0（不许抢富余宽度，否则左栏会被撑变形）",
      re.search(r"grid_columnconfigure\(0,\s*weight=0,\s*minsize=LEFT_COL_W\)",
                SRC) is not None)
check("右栏列 weight=1（吃掉富余，否则右侧留死空白）",
      re.search(r"grid_columnconfigure\(1,\s*weight=1,\s*minsize=RIGHT_COL_W\)",
                SRC) is not None)
check("切回日志用 grid_remove（不是 destroy，否则日志内容会丢）",
      re.search(r"self\._log_view\.grid_remove\(\)", SRC) is not None)
_toggle_defs = len(re.findall(r"def on_debug_view\(", SRC))
check("on_debug_view 只有一个定义（曾经编辑失误留下两个）",
      _toggle_defs, 1)
_toggle_body = SRC.split("def on_debug_view(")[1].split("\n    def ")[0]
check("on_debug_view 现在走内嵌切换，不再 import subprocess 开窗口",
      "subprocess" not in _toggle_body)
check("调试视图按钮仍绑在 on_debug_view 上",
      "command=self.on_debug_view" in SRC)
check("右栏行 weight 配好（两视图叠在同一格）",
      re.search(r"col\.grid_rowconfigure\(0,\s*weight=1\)", SRC) is not None)
check("debug_view_cmd() 保留（命令行 `--run debug_view` 还要用）",
      "def debug_view_cmd(" in SRC)

print()
print("=" * 68)
print(f"结果: {_passed} 通过 / {_failed} 失败")
print("=" * 68)
sys.exit(1 if _failed else 0)
