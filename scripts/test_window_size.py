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
check("窗口宽下限 1080、上限 1620",
      "w = int(max(1080, min(need, 1620)))" in SRC)
check("宽度算式 = 前三栏之和（四等分），不再是「左栏 + 右栏×缩放」",
      "need = LEFT_COL_W + SETTINGS_COL_W + RIGHT_COL_W" in SRC)
check("左栏列宽用 LEFT_COL_W",
      "root.grid_columnconfigure(0, weight=0, minsize=int(LEFT_COL_W * s))" in SRC)
# 用户 2026-10-08 要求「削减日志窗口的宽度达到每一部分都四分之一页面的
# 效果」→ 四栏各 405，加起来正好是窗口的 1620 逻辑px。
check("窗口宽够放下四栏（405×4）", 405 * 4, 1620)

print("\n[6b] 版本号只出现一次（别在标题下一行又写一遍）")

check("窗口标题带版本", 'root.title(f"{APP_TITLE} v{APP_VER}")' in SRC)
check("副标题不再重复版本号", "v{APP_VER} · 中山医院远程教育" not in SRC)

print("\n[7] 任务行不再有说明小字，改成一个 ⚙（用户 2026-10-08 要求）")

m = re.search(r"^LEFT_COL_W = (\d+)", SRC, re.M)
check("能读到 LEFT_COL_W", bool(m))
col = int(m.group(1))


def _colw(name: str) -> int:
    """从源码里读一个列宽常量。读不到就返回 -1（断言会当场报出来）。"""
    mm = re.search(rf"^{name} = (\d+)", SRC, re.M)
    return int(mm.group(1)) if mm else -1


LEFT, SETTINGS, LOGW, DEBUGW = (_colw("LEFT_COL_W"), _colw("SETTINGS_COL_W"),
                                _colw("LOG_COL_W"), _colw("DEBUG_COL_W"))
# 480 → 405：用户要求四等分（2026-10-08）。钉死是故意的 —— 改这个数
# 会把四栏的对齐破掉，必须连带改 SETTINGS_COL_W / LOG_COL_W / DEBUG_COL_W。
check(f"LEFT_COL_W = {col}", col, 405)
check("四栏等宽（各占约四分之一页面）",
      [LEFT, SETTINGS, LOGW, DEBUGW],
      [405, 405, 405, 405])


def desc_avail(col_w: int, col_padx: int = 21, scroll_shrink: int = 56,
               card_padx: int = 28, indent: int = 46, margin: int = 5) -> int:
    """左栏里任务行真正可用的宽度（逻辑px）。

    逐段减：
      列宽 → 减去 `col.grid(padx=(14, 7))` 的 21
           → 减去滚动框相对列的收窄（实测 480 → 424，收 56）
           → 减去卡片左右 padx 14×2
           → 减去左边缩进 46（开关文字对齐的位置）
           → 再留 margin 的余量
    留着这个算式是因为**开关那一行仍然受它约束**：右边还要塞一个 ⚙。
    """
    return col_w - col_padx - scroll_shrink - card_padx - indent - margin


avail = desc_avail(col)
# 405 - 21 - 56 - 28 - 46 - 5 = 249。`scroll_shrink=56` 当初是在 480 下
# 实测的，收窄到 405 后真实值可能略小 —— 这个算式是**回归钉子**（防止
# 以后有人把 ⚙ 撑大把标题压没），不是精确测量，所以不为了 ±8px 改它。
check(f"任务行可用宽度算出来是 {avail}", avail, 249)

# ⚙ 本体 30px + 左边距 6px = 36px。开关文字要和它同排，所以可用宽度得
# 扣掉这 36px 还有剩 —— 否则标题会被 ⚙ 压掉一截。
check("扣掉 ⚙ 之后还剩得下标题", avail - 36 > 180)


def method_src(name: str) -> str:
    """截出某个方法的源码正文（到下一个同级 `def` 为止）。

    断言必须**只看这个方法**：`text=desc` 和 `wraplength=LEFT_COL_W - 156`
    这两个词在别处仍然合法 —— `self.lbl_route.configure(text=desc)`（路线
    说明）和它自己的 wraplength 都还在用。早先按整份源码下断言，结果
    「已经删掉的东西」和「故意留下的东西」分不开。
    """
    m2 = re.search(rf"^    def {re.escape(name)}\(", SRC, re.M)
    if not m2:
        return ""
    rest = SRC[m2.end():]
    end = re.search(r"^    def ", rest, re.M)
    return rest[:end.start()] if end else rest


rows = method_src("_render_tasks")
check("能截到 _render_tasks 的源码", bool(rows))

# --- 小字必须真的没了 ---
check("任务行里不再渲染 desc 文本标签", "text=desc" not in rows)
check("任务行里不再有描述用的 wraplength",
      "wraplength=LEFT_COL_W - 156" not in rows)
# 说明文字没丢，它挪到 ⚙ 面板的 intro 里了。
check("desc 仍被存进 _task_desc（拿去当设置面板的 intro）",
      "self._task_desc[key] = desc" in rows)
check("设置面板用了 _task_desc",
      "intro=self._task_desc.get(key" in SRC)

# --- ⚙ 只在声明过可配置项的任务上出现 ---
check("⚙ 有 has_options 守卫", "if taskspec.has_options(key):" in rows)
check("⚙ 绑到 on_task_settings",
      "command=lambda k=key: self.on_task_settings(k)" in rows)
check("gears 每次重建都清空", "self.gears.clear()" in rows)

# --- 长文字仍然用原生 tk.Label ---
# `CTkLabel` 会把高度锁死在 42px（实测：写不写 height 都一样），只装得下
# 3 行 10 号字；折成 4 行的说明第 4 行整行被吃掉，症状看着像「右边被截」，
# 实际是丢了一整行 —— 这个坑绕过一次，所以用测试钉住，防止以后有人
# 「顺手改回 CTkLabel 统一风格」。
check("长文字用 _wrapped 辅助函数渲染", "def _wrapped(" in SRC)
check("_wrapped 用的是原生 tk.Label",
      re.search(r"return tk\.Label\(\s*\n\s*parent, text=_plain\(text\)", SRC)
      is not None)
check("_wrapped 没用 CTkLabel", "ctk.CTkLabel(\n        parent, text=text" not in SRC)
check("_wrapped 的底色是调用方传的（弹窗里是弹窗底色）",
      "bg=bg, fg=fg," in SRC)
check("设置面板里的说明传了弹窗底色 COL_BG",
      SRC.count("COL_BG, COL_TEXT_DIM).pack(") >= 2,
      SRC.count("COL_BG, COL_TEXT_DIM).pack(") >= 2)

# --- 说明文字里的 Markdown 记号要在渲染层吃掉 ---
# `tk.Label` 不懂 Markdown。设置项的 `description` 是按人话写的，随手就会带
# `**强调**` 和 `` `代码` ``；不处理的话界面上**原样画出星号和反引号**，
# 实测截图里是「填了才启用 AI 答题。任何 **OpenAI 兼容**的接口都行」——
# 看着像程序拼接字符串漏了一步。用 `_plain` 统一在渲染层解决。
check("_wrapped 先过一遍 _plain 去掉 Markdown 记号",
      "text=_plain(text)" in SRC)
check("有 _plain 这个函数", "def _plain(text: str) -> str:" in SRC)
check("_plain 处理 **粗体**", r'r"\*\*(.+?)\*\*"' in SRC)
check("_plain 处理 `反引号`", r'r"`([^`]+)`"' in SRC)

print("\n[8] 外框尺寸也要算进去（不然「算着放得下、实际被裁」）")


def frame_size(logical_w: int, logical_h: int, scale: float) -> tuple[int, int]:
    return (int(logical_w * scale) + FRAME_W, int(logical_h * scale) + FRAME_H)


fw, fh = frame_size(405 * 4, 900, 1.0)
# FRAME_W = 16（边框），1620 + 16 = 1636。原来这条写的是
# `frame_size(480 + 1050, …)` —— 那个 1050 是右栏的物理当量，四等分之后
# 宽度不再按缩放放大，直接是全逻辑宽 1620。
check(f"1620x900 → 外框 {fw}x{fh}", (fw, fh), (1636, 939))
check("外框仍小于工作区 2560x1528", fw < 2560 and fh < 1528)

print("\n[9] 右栏：左半边日志、右半边调试视图（并排同时可见）")
# 用户要求：「把调试窗口去了，把右边的空白处改成调试模式显示的东西」，
# 随后纠正：「我的意思是说那一块左半边日志，右半边视图」—— 两个要**同时**
# 看到，不是互相切换。这一组钉住布局本身，防止以后被「顺手简化」掉：
#   1. 左栏 weight=0（不许抢富余宽度，否则左栏会被撑变形）
#   2. 设置栏 weight=0（用户要四等分，它也不许抢）
#   3. 右栏 weight=1（吃掉富余，否则窗口右侧留一大块死空白）
#   4. 右栏**内部**再横分两列：日志 weight=1、调试 show 保底 DEBUG_COL_W
#   5. 两个视图都**不隐藏** —— 一旦出现 grid_remove/destroy 就又变成切换了
check("左栏列 weight=0（不许抢富余宽度，否则左栏会被撑变形）",
      re.search(r"grid_columnconfigure\(0,\s*weight=0,"
                r"\s*minsize=int\(LEFT_COL_W \* s\)\)", SRC) is not None)
check("设置栏列 weight=0（四等分，它也不许抢富余）",
      re.search(r"grid_columnconfigure\(1,\s*weight=0,"
                r"\s*minsize=int\(SETTINGS_COL_W \* s\)\)", SRC) is not None)
check("右栏列 weight=1（吃掉富余，否则右侧留死空白）",
      re.search(r"grid_columnconfigure\(2,\s*weight=1,"
                r"\s*minsize=int\(RIGHT_COL_W \* s\)\)", SRC) is not None)
# ★ 这三处 `int(… * s)` 是必须的：`grid_columnconfigure(minsize=…)` 的单位
# 是**设备像素**，而常量是逻辑px。少了这个乘法，150% 缩放下四栏宽度全错
# （实测左栏只剩 270 逻辑px、日志吃掉富余宽到 737）。见 `_dpi_scale`。
check("minsize 都乘了 _dpi_scale（设备像素 ↔ 逻辑px 的单位差）",
      SRC.count("minsize=int(") >= 5)
check("右栏内部：日志列 weight=1（吃掉右栏富余）",
      re.search(r"col\.grid_columnconfigure\(0,\s*weight=1,"
                r"\s*minsize=int\(LOG_COL_W \* s\)\)", SRC) is not None)
check("右栏内部：调试列保底 DEBUG_COL_W",
      re.search(r"col\.grid_columnconfigure\(1,\s*weight=0,"
                r"\s*minsize=int\(DEBUG_COL_W \* s\)\)", SRC) is not None)
check("RIGHT_COL_W = LOG_COL_W + DEBUG_COL_W（两半之和）",
      "RIGHT_COL_W = LOG_COL_W + DEBUG_COL_W" in SRC)
_toggle_defs = len(re.findall(r"def on_debug_view\(", SRC))
check("on_debug_view 只有一个定义（曾经编辑失误留下两个）",
      _toggle_defs, 1)
_toggle_body = SRC.split("def on_debug_view(")[1].split("\n    def ")[0]
check("on_debug_view 不再 import subprocess 开独立窗口",
      "subprocess" not in _toggle_body)
check("调试视图按钮仍绑在 on_debug_view 上",
      "command=self.on_debug_view" in SRC)
check("右栏行 weight=1（两半都吃满高度）",
      re.search(r"col\.grid_rowconfigure\(0,\s*weight=1\)", SRC) is not None)
check("debug_view_cmd() 保留（命令行 `--run debug_view` 还要用）",
      "def debug_view_cmd(" in SRC)
# 并排布局的核心：**不许再有隐藏任何一个视图的代码**。用户明确要的是
# 「左半边日志，右半边视图」同时可见；出现 grid_remove 就说明又退回切换了。
check("日志视图不再被 grid_remove（并排要一直可见）",
      "self._log_view.grid_remove()" in SRC, False)
check("调试视图不再被 grid_remove（并排要一直可见）",
      "self._debug_view.grid_remove()" in SRC, False)
check("_build_debug_view() 有被调用（并排就得一开始就在）",
      "self._build_debug_view()" in SRC)

print("\n[10] 设置从弹窗改成常驻栏位（用户 2026-10-08 要求）")

# 用户原话：「在日志UI的左边加入设置的部分，在用户没点击设置时显示
# 『点击左边的设置以设置选项』」。改常驻还有个附带好处：弹窗那条路上
# 撞过两个真 bug（master 传成 App、`self._options` 撞 tkinter 内部方法），
# 而它们的失败模式都是「点了没反应」—— 无控制台的窗口程序里根本看不见。
check("旧的弹窗类已经拆掉了", "class _SettingsDialog" not in SRC)
check("换成不依赖 Toplevel 的 _SettingsForm", "class _SettingsForm:" in SRC)
check("有 _build_settings_panel", "def _build_settings_panel(" in SRC)
check("_build_settings_panel 有被调用", "self._build_settings_panel()" in SRC)
check("空态提示文案就是用户要的那句",
      "点击左边的设置以设置选项" in SRC)
# ★ 只能查**代码行**，不能查整份源码：`_SettingsForm` 的 docstring 里
# 正解释着「`self._options` 撞了 tkinter 的 `Misc._options`」这件事，
# 按整份源码匹配会把那句说明本身当成违规（这条断言第一版就是这么假红的）。
_code_lines = [ln.strip() for ln in SRC.splitlines()
               if not ln.strip().startswith("#")]
check("没有哪一行代码真的去赋 self._options（撞 tkinter 内部方法，历史真 bug）",
      any(ln.startswith("self._options") for ln in _code_lines), False)
check("表单里的字段叫 self._opts", "self._opts = list(options)" in SRC)
check("Tk 回调异常会进运行日志（弹窗那次就是被静默吞掉的）",
      "report_callback_exception" in SRC)
# 面板底部那两个按钮必须**不跟着滚动区**，否则滚一屏设置就够不着保存了。
# 它们自己放在 `bar` 里，`bar` 按 grid row 排在滚动区（row=2）下面。
check("保存按钮那一行不在滚动区里（排在面板 row=4）",
      'bar.grid(row=4, column=0, sticky="ew"' in SRC)
check("出错提示也不在滚动区里", "self.lbl_set_err.grid(row=3" in SRC)

print()
print("=" * 68)
print(f"结果: {_passed} 通过 / {_failed} 失败")
print("=" * 68)
sys.exit(1 if _failed else 0)
