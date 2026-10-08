# -*- coding: utf-8 -*-
"""声明式设置（`taskspec.py` / `inference.py` / `desktop_farm.py`）的自测。

## 为什么需要这个模块

用户 2026-10-08 要求「增加每一个功能的设置」，参考 MaaAssistantArknights 的
做法。落地方式是**一张声明表**（`taskspec.TASK_OPTIONS` / `SETTINGS`），
界面控件、配置读写、命令行拼装全从表里推出来。

好处是「加一个可调项只改一处」，代价是**这张表错了不会有任何显式报错** ——
界面照常渲染、程序照常启动，只是参数**静默地**变成了别的值，然后用错的
参数跑一整门课（几小时）。所以这里把三类事情钉死：

1. **表本身**：路径绑对了没有、有没有重名、和 `core` 里的任务表对不对得上；
2. **取值**：非法输入必须回退默认（手改配置写个 `"九十分钟"` 不能让面板打不开）；
3. **落盘与拼装**：`save()` 只能动自己那几个键，`to_argv()` 只能拼非空值
   （空值交给各脚本自己的 argparse 默认值，默认值只有一份，不能抄第二遍）。

`inference.apply()` 用假 resource 测 —— 真的 `Resource()` 要载框架 DLL，
而且**载入成功不代表生效**（惰性加载，见 `inference.py` 开头的说明），
拿它当断言等于没测。

运行:
    python scripts\\test_options.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import desktop  # noqa: E402
import desktop_farm  # noqa: E402
import inference  # noqa: E402
import paths  # noqa: E402
import taskspec  # noqa: E402

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


def opt_of(task_key: str, opt_key: str):
    """按 key 取某项声明。

    ★ 按 key 取、**不按下标** —— 表里插一项就会让下标全挪位。
    `d_farm.target` 原来写成 `options_for("d_farm")[0]`，
    后来在最前面插了 `switch_course` 就会静默地断言到别的项上。
    """
    for o in taskspec.options_for(task_key):
        if o.key == opt_key:
            return o
    raise AssertionError(f"设置表里没有 {task_key}.{opt_key} 这一项")


# ==========================================================================
print("\n[1] 声明表本身：每个任务、每个可调项都得是完整的")
# ==========================================================================

# 三个浏览器版任务要有可调项 —— 用户要的是「每一个功能都能设置」，
# 少一个就意味着那个功能只能用写死的默认值。
for key in ("d_watch", "d_farm", "d_exam"):
    check(f"{key} 有可配置项", taskspec.has_options(key))
    check(f"{key} 的 ⚙ 能显示摘要（summary 可调用）",
          isinstance(taskspec.summary(key, taskspec.values(key, {})), str))

# `d_finish`（申请结课）**故意没有**：它只有「点一下申请」这一个动作。
# 用户 m19597 第 7 条原话是「删除申请结课的设置按键」——
# 落到这里就是 `_TASK_OPTION_SPECS["d_finish"] == ()`，
# `has_options()` 为假，界面上那个 ⚙ 就不会被建出来。
check("d_finish 没有可配置项（用户要求删掉那个 ⚙）",
      not taskspec.has_options("d_finish"))
check("d_finish 的选项表是空的", taskspec.TASK_OPTIONS.get("d_finish"), ())
check("d_finish 的 summary 是空串", taskspec.summary("d_finish", {}), "")
check("d_finish 的 to_argv 是空表", taskspec.to_argv("d_finish", {}), [])

# 微信版那四条**故意不给设置**：那条路线是备选，且参数是硬编码在管线里的。
for key in ("course", "checkin", "exam", "watch"):
    check(f"微信版 {key} 没有设置（它是备选路线）",
          not taskspec.has_options(key))

# `_COURSE` 是共用的模板，`_bind()` 必须给每个任务绑出**自己的**路径，
# 否则在「自动看课」里填的课程名会同时写进「刷时长」和「考核」。
seen_paths: dict[tuple, str] = {}
for key, opts in taskspec.TASK_OPTIONS.items():
    for opt in opts:
        check(f"{key}.{opt.key} 的 path 绑到了自己任务上",
              opt.path[1] == key, True)
        check(f"{key}.{opt.key} 的 path 是三段",
              len(opt.path), 3)
        check(f"{key}.{opt.key} 的 path 指向 options 节",
              opt.path[0], "options")
        prev = seen_paths.get(opt.path)
        check(f"{key}.{opt.key} 的 path 没和别人撞（{prev}）", prev is None)
        seen_paths[opt.path] = f"{key}.{opt.key}"

# `all_options()` 的键是界面上的唯一标识，重名会让设置面板串行。
# 这里把总数钉死是**故意**的：加一项就报错，逼着人确认新项的 key 没撞、
# 也顺带提醒「界面上的 ⚙ 多了一个」。
# 16 → 19 是用户 m19597 那批：`d_watch.switch_course`（自动切换课程）、
# `d_farm.unit`（时长单位）、`d_exam.ai_base`/`ai_key`/`ai_model`（AI 答题）
# 加起来加了几项、`d_finish` 那两项又删掉了，净 +3。
allopts = taskspec.all_options()
check("all_options 有 19 个键", len(allopts), 19)
check("all_options 的键数 = 表里项数之和",
      len(allopts),
      sum(len(o) for o in taskspec.TASK_OPTIONS.values())
      + sum(len(s.options) for s in taskspec.SETTINGS))

# 用户 m19597 点名要加的那几个开关，一个都不能少 ——
# 少一个就是「界面上根本没这个选项」，而不是「默认值不对」。
for _k in ("d_watch.switch_course", "d_farm.unit",
           "d_exam.ai_base", "d_exam.ai_key", "d_exam.ai_model"):
    check(f"{_k} 在设置表里", _k in allopts)
# 反过来的三个：删掉的、不该再出现的
for _k in ("d_finish.all", "d_finish.course"):
    check(f"{_k} 已经删掉了", _k not in allopts)

# `input` 必须声明类型，否则 `coerce` 会当字符串原样传下去，
# `--target "九十分钟"` 会一路走到 argparse 才炸。
for key, opts in list(taskspec.TASK_OPTIONS.items()):
    for opt in opts:
        if opt.type == "input":
            check(f"{key}.{opt.key} 声明了 kind",
                  opt.kind in ("int", "float", "str"))
        if opt.type == "select":
            check(f"{key}.{opt.key} 的 select 有候选项", len(opt.cases) > 0)
            check(f"{key}.{opt.key} 的默认值是个合法选项",
                  opt.default in {c.name for c in opt.cases})

# 带密钥的输入框要标 `secret` —— `_SettingsForm` 按它打点显示，
# `summary()` 按它只写「已填」而不把密钥原样打到界面上（会被截图/录屏）。
_ai_key = [o for o in taskspec.options_for("d_exam") if o.key == "ai_key"]
check("ai_key 标了 secret", bool(_ai_key) and _ai_key[0].secret, True)
check("别的输入框没被误标成 secret",
      [o.key for o in taskspec.all_options().values()
       if getattr(o, "secret", False)], ["ai_key"])

# 任务表是唯一的事实来源，两边必须对得上 —— 界面上的 ⚙ 就是按
# `core.DESKTOP_TASKS` 的键去 `taskspec` 里查的。
import core  # noqa: E402  （放在后面 import：它会拉框架的 DLL）

desktop_keys = [k for k, *_ in core.DESKTOP_TASKS]
check("core.DESKTOP_TASKS 里有 d_farm", "d_farm" in desktop_keys)
check("DESKTOP_TASKS 的键和 TASK_OPTIONS 的键一致（d_finish 也在）",
      sorted(desktop_keys), sorted(taskspec.TASK_OPTIONS))
check("d_farm 排在 d_watch 之后、d_exam 之前（先看完再刷）",
      desktop_keys.index("d_watch") < desktop_keys.index("d_farm")
      < desktop_keys.index("d_exam"))

# 刷时长的默认值必须和脚本里的默认值一样。两边各写一份，改了忘一边
# 就会出现「界面显示 90、实际按 120 跑」。
# `target` 现在是 `kind="float"`（要支持「1.5 小时」这种填法），
# 所以拿 float 比，别拿 `==` 直接磕 int/float 的字面量。
check("d_farm.target 默认值和 desktop_farm 一致",
      float(opt_of("d_farm", "target").default),
      float(desktop_farm.TARGET_MINUTES))
check("d_farm.max_hours 默认值和 desktop_farm 一致",
      int(opt_of("d_farm", "max_hours").default),
      int(desktop_farm.MAX_HOURS))

# ★ 「时长单位」这个下拉（用户 m19597 第 3 条要的）：
# `to_argv` 得把它翻成 `--unit hour`，而 `desktop_farm` 的 argparse 得认这个
# 参数名，还得真的做换算 —— 三处少一处就是「界面选了小时、实际按分钟跑」，
# 而这种错不报错，只是刷得慢 60 倍。
_unit = opt_of("d_farm", "unit")
check("unit 是个下拉", _unit.type, "select")
check("unit 的选项就是 minute / hour",
      [c.name for c in _unit.cases], ["minute", "hour"])
check("unit 默认是分钟", _unit.default, "minute")
check("unit 拼得出 --unit hour",
      taskspec.to_argv("d_farm", {"target": 1.5, "unit": "hour",
                                  "max_hours": 8, "course": ""})[:2],
      ["--target", "1.5"])
check("--unit 也拼进去了",
      "--unit" in taskspec.to_argv("d_farm", {"target": 1.5, "unit": "hour",
                                              "max_hours": 8, "course": ""}))
_farm_src = (Path(__file__).resolve().parent / "desktop_farm.py").read_text(
    encoding="utf-8")
check("desktop_farm 的 argparse 认得 --unit",
      '"--unit"' in _farm_src and 'choices=("minute", "hour")' in _farm_src)
check("desktop_farm 真的做了小时 → 分钟的换算",
      'args.target = args.target * 60.0' in _farm_src)
check("desktop_farm 挡住了非正数",
      "args.target <= 0" in _farm_src)


# ==========================================================================
print("\n[2] coerce：非法输入一律回退默认，绝不抛异常")
# ==========================================================================

farm = {o.key: o for o in taskspec.options_for("d_farm")}
target = farm["target"]
course = farm["course"]
max_hours = farm["max_hours"]

# `target` 是 `kind="float"` —— 因为「时长单位」可以选小时，
# 1.5 小时这种填法必须收得下。所以这里断言的都是 `float`。
check("target 正常整数", taskspec.coerce(target, 120), 120.0)
check("target 字符串数字", taskspec.coerce(target, "120"), 120.0)
check("target 带空格", taskspec.coerce(target, " 120 "), 120.0)
check("target 中文数字 → 回退默认 90", taskspec.coerce(target, "九十分钟"), 90.0)
# ★ 这条原来是「小数 → 回退默认」。加了「时长单位」之后小数**必须收下**，
# 否则「1.5 小时」根本填不进去。
check("target 小数收下（1.5 小时要能填）",
      taskspec.coerce(target, "1.5"), 1.5)
check("target 小数（0.5）", taskspec.coerce(target, 0.5), 0.5)
check("target 空串 → 回退默认", taskspec.coerce(target, ""), 90.0)
check("target None → 回退默认", taskspec.coerce(target, None), 90.0)
check("target 越下界 0 → 回退默认", taskspec.coerce(target, 0), 90.0)
# 下界是 0.1 而不是 0：`--target 0` 会让刷时长那边直接报错退出。
check("target 下界是 0.1", taskspec.coerce(target, 0.1), 0.1)
check("target 越上界 → 回退默认", taskspec.coerce(target, 100001), 90.0)
check("target 边界 1 可用", taskspec.coerce(target, 1), 1.0)
check("target 边界 100000 可用", taskspec.coerce(target, 100000), 100000.0)

# `max_hours` 的默认值和 target 不同，回退时要各回各的 —— 早先一版
# `coerce` 里写死了 `opt.default`，这两个用例就是防它退化成常数。
check("max_hours 越界回退的是它自己的默认 8",
      taskspec.coerce(max_hours, 999), 8)
check("course 是字符串类型，原样保留", taskspec.coerce(course, "老年认知"), "老年认知")
check("course 的空值就是空串", taskspec.coerce(course, ""), "")
check("course 的 None 也变空串", taskspec.coerce(course, None), "")

# select 的非法值 → 默认
only = {o.key: o for o in taskspec.options_for("d_exam")}["only"]
check("select 合法值原样", taskspec.coerce(only, "questionnaire"), "questionnaire")
check("select 非法值 → 默认 both", taskspec.coerce(only, "nope"), "both")
check("select 大小写不放过（NOPE）", taskspec.coerce(only, "NOPE"), "both")
check("select 非字符串 → 默认", taskspec.coerce(only, 123), "both")

# switch 的语义：配置里手写成各种真值都要能读
sw = {o.key: o for o in taskspec.options_for("d_exam")}["all"]
check("switch True", taskspec.coerce(sw, True), True)
check("switch False", taskspec.coerce(sw, False), False)
check("switch None → 默认 False", taskspec.coerce(sw, None), False)
check("switch 非空串算真", taskspec.coerce(sw, "yes"), True)
check("switch 空串算假", taskspec.coerce(sw, ""), False)

# ---------------------------------------------------------------- 新加的项
# 用户 m19597 第 2 条：「自动看课设置中加入自动切换课程的开关」。
sw_course = opt_of("d_watch", "switch_course")
check("switch_course 是个开关", sw_course.type, "switch")
check("switch_course 默认关（第一次跑只看一门）", sw_course.default, False)
check("switch_course 的 flag 是 --switch-course",
      sw_course.flag, "--switch-course")
check("switch_course 全默认时不拼",
      "--switch-course" not in taskspec.to_argv(
          "d_watch", taskspec.values("d_watch", {})))
check("switch_course 勾上就拼 --switch-course",
      "--switch-course" in taskspec.to_argv(
          "d_watch", {"course": "", "lessons": 0, "max_courses": 0,
                      "switch_course": True}))
# `to_argv` 的 switch 分支是「真值 + 有 flag 就 append」，所以脚本那边的
# argparse 必须是 `store_true`，否则 `--switch-course 1` 这种会直接报错退出。
_watch_src = (Path(__file__).resolve().parent / "desktop_watch.py").read_text(
    encoding="utf-8")
check("desktop_watch 的 --switch-course 是 store_true",
      'ap.add_argument("--switch-course", action="store_true"' in _watch_src)
# ★ 不勾就强制只看一门。为什么不在 taskspec 里把默认值写成 1：
# 命令行单跑时 `--max-courses 3` 得保持「3 门」的语义，不能被默认值劫持。
check("不勾时不依赖 --max-courses 的默认值（脚本里强制成 1）",
      "args.max_courses = 1" in _watch_src)

# 用户 m19597 第 6 条 + m19647：「填入 API 以进行 ai 答题」→ OpenAI 兼容三件套。
check("d_exam 有三个 AI 框",
      [o.key for o in taskspec.options_for("d_exam") if o.key.startswith("ai_")],
      ["ai_base", "ai_key", "ai_model"])
check("三个 AI 框默认都空（不填 = 不启用）",
      [o.default for o in taskspec.options_for("d_exam") if o.key.startswith("ai_")],
      ["", "", ""])
check("AI 三个框拼成命令行",
      taskspec.to_argv("d_exam",
                       {"course": "", "only": "both", "all": False,
                        "max_courses": 0, "ai_base": "http://x/v1",
                        "ai_key": "sk-1", "ai_model": "m"}),
      ["--ai-base", "http://x/v1", "--ai-key", "sk-1", "--ai-model", "m",
       "--max-courses", "0"])
check("没填就不拼（等于不启用）",
      [x for x in taskspec.to_argv("d_exam", taskspec.values("d_exam", {}))
       if x.startswith("--ai")], [])
# ★ 密钥绝不能出现在任务标题旁边那行小字里 —— 那行会被截图/录屏。
_ai_sum = taskspec.summary("d_exam", {"course": "", "only": "both", "all": False,
                                      "max_courses": 0, "ai_base": "http://x/v1",
                                      "ai_key": "sk-秘密密钥", "ai_model": "m"})
check("summary 里不出现密钥原文", "sk-秘密密钥" not in _ai_sum)
check("summary 里说「已填」", "已填" in _ai_sum)
check("summary 里接口地址照常显示", "http://x/v1" in _ai_sum)


# ==========================================================================
print("\n[3] values()：配置里没存的补默认，存了非法值的也回退")
# ==========================================================================

cfg = {"options": {"d_farm": {"target": 200, "course": "老年"}}}
vals = taskspec.values("d_farm", cfg)
# `target` 是 float（要支持 1.5 小时），所以读出来是 200.0。
check("读到存的 target（转成 float）", vals["target"], 200.0)
check("读到存的 course", vals["course"], "老年")
check("没存的 max_hours 补默认", vals["max_hours"], 8)
check("没存的 unit 补默认 minute", vals["unit"], "minute")

cfg2 = {"options": {"d_farm": {"target": "九十分钟"}}}
check("配置里是垃圾值时回退默认", taskspec.values("d_farm", cfg2)["target"], 90.0)

check("完全空配置 → 全默认",
      taskspec.values("d_farm", {}),
      {"target": 90.0, "unit": "minute", "all_courses": False,
       "max_hours": 8, "course": ""})
check("没有这个任务 → 空 dict", taskspec.values("不存在", {}), {})

# `_dig` 中间层缺失不能抛 KeyError（配置被手删了一节是很常见的）
check("_dig 中间层缺失返回 None",
      taskspec._dig({}, ("options", "d_farm", "target")), None)
check("_dig 中间层不是 dict 也返回 None",
      taskspec._dig({"options": "坏了"}, ("options", "d_farm", "target")), None)
check("_plant 自动建中间层",
      (lambda c: (taskspec._plant(c, ("a", "b", "c"), 1), c)[1])({}),
      {"a": {"b": {"c": 1}}})
check("_plant 不覆盖已有的同层兄弟",
      (lambda c: (taskspec._plant(c, ("a", "b"), 2), c)[1])({"a": {"z": 9}}),
      {"a": {"z": 9, "b": 2}})

gvals = taskspec.global_values({"inference": {"mode": "cpu", "gpu_id": 3}})
check("全局设置读到 mode", gvals["inference.mode"], "cpu")
# `gpu_id` 按 `kind="int"` 转成整数落盘。`inference.read()` 两种都收
# （它自己 `int(raw_id)`），所以配置里是 `3` 还是 `"3"` 都行 —— 整数更干净。
check("全局设置读到 gpu_id（转成 int）", gvals["inference.gpu_id"], 3)
check("gpu_id 留空时仍是空串（意思是「交给框架自选」）",
      taskspec.global_values({"inference": {"gpu_id": ""}})["inference.gpu_id"],
      "")
check("全局设置没配的补默认", gvals["browser.debug_port"], 9222)
check("全局设置非法 mode 回退 auto",
      taskspec.global_values({"inference": {"mode": "nope"}})["inference.mode"],
      "auto")


# ==========================================================================
print("\n[4] to_argv：只有非空值才拼（默认值留给各脚本的 argparse）")
# ==========================================================================

check("全默认 → 只拼有值的项（course 空串不拼）",
      taskspec.to_argv("d_farm", taskspec.values("d_farm", {})),
      ["--target", "90", "--unit", "minute", "--max-hours", "8"])
# ↑ target/max_hours 是数字且有值，所以会拼出来；course 空串不拼。
#   这正是想要的行为：脚本那边 argparse 的默认值和这边一致，
#   显式传过去只是把「界面看到的值」和「实际跑的值」对齐。
#   `90.0` 写成 `90` 是 `_fmt()` 干的 —— argparse 的 `type=float` 两者都收，
#   但界面上「刷到多久 90.0」看着像出了 bug。
check("整数值的 float 不印成 90.0（fmt_value）", taskspec.fmt_value(90.0), "90")
check("1.5 照原样印", taskspec.fmt_value(1.5), "1.5")
check("字符串不受影响", taskspec.fmt_value("老年"), "老年")
check("整数不受影响", taskspec.fmt_value(42), "42")

check("course 填了才拼",
      taskspec.to_argv("d_farm", {"target": 90, "max_hours": 8, "course": "老年"}),
      ["--target", "90", "--unit", "minute", "--max-hours", "8", "--course", "老年"])
check("course 是空白字符串不拼（等于没填）",
      taskspec.to_argv("d_farm", {"target": 1, "max_hours": 1, "course": "   "}),
      ["--target", "1", "--unit", "minute", "--max-hours", "1"])

check("d_watch 的 lessons=0 也会拼出来（0 是有意义的「全部」）",
      taskspec.to_argv("d_watch", {"course": "", "lessons": 0, "max_courses": 0}),
      ["--lessons", "0", "--max-courses", "0"])
check("d_watch 的 lessons=3",
      taskspec.to_argv("d_watch", {"course": "", "lessons": 3, "max_courses": 0}),
      ["--lessons", "3", "--max-courses", "0"])
# 不勾「自动切换课程」时 `--switch-course` 一个都不拼 —— 脚本那边
# 靠「有没有这个 flag」决定是只看一门还是一路往后跑。
check("d_watch 不勾自动切换课程 → 没有 --switch-course",
      taskspec.to_argv("d_watch", {"course": "", "lessons": 0,
                                   "max_courses": 0, "switch_course": False}),
      ["--lessons", "0", "--max-courses", "0"])
check("d_watch 勾了自动切换课程 → 拼在最前面",
      taskspec.to_argv("d_watch", {"course": "", "lessons": 0,
                                   "max_courses": 0, "switch_course": True}),
      ["--switch-course", "--lessons", "0", "--max-courses", "0"])

check("d_exam 的 both 不加任何开关（本来就默认两件都做）",
      taskspec.to_argv("d_exam",
                       {"course": "", "only": "both", "all": False,
                        "max_courses": 0}),
      ["--max-courses", "0"])
check("d_exam 选只做考核 → --exam-only",
      taskspec.to_argv("d_exam",
                       {"course": "", "only": "exam", "all": False,
                        "max_courses": 0}),
      ["--exam-only", "--max-courses", "0"])
check("d_exam 选只补问卷 → --questionnaire-only",
      taskspec.to_argv("d_exam",
                       {"course": "", "only": "questionnaire", "all": False,
                        "max_courses": 0}),
      ["--questionnaire-only", "--max-courses", "0"])
check("d_exam 的 all 开关 → --all",
      taskspec.to_argv("d_exam",
                       {"course": "", "only": "both", "all": True,
                        "max_courses": 0}),
      ["--all", "--max-courses", "0"])
# ★ 用户 m19597 第 7 条：申请结课那项的 ⚙ 已经删了，
# 所以 `d_finish` 现在**没有任何命令行参数** —— 拿旧键去拼也不会拼出东西，
# 这正是「删干净了」的证据（旧配置里残留的 `all: true` 不该再影响行为）。
check("d_finish 全默认 → 空 argv",
      taskspec.to_argv("d_finish", {}), [])
check("d_finish 就算配置里残留旧键也不拼参数",
      taskspec.to_argv("d_finish", {"course": "老年", "all": True}), [])

# 拼出来的参数必须**真的被脚本认**，否则 argparse 会直接退出。
# 这条是源码级断言：把每个 flag 拿去脚本里找一遍。
src_watch = (Path(__file__).resolve().parent / "desktop_watch.py").read_text(
    encoding="utf-8")
src_farm = (Path(__file__).resolve().parent / "desktop_farm.py").read_text(
    encoding="utf-8")
src_exam = (Path(__file__).resolve().parent / "desktop_exam.py").read_text(
    encoding="utf-8")
where = {"d_watch": src_watch, "d_farm": src_farm,
         "d_exam": src_exam, "d_finish": src_exam}
for key, opts in taskspec.TASK_OPTIONS.items():
    flags = [o.flag for o in opts if o.flag]
    for o in opts:
        flags += [c.flag for c in o.cases if c.flag]
    for flag in flags:
        check(f"{key} 的 {flag} 在脚本里有定义", f'"{flag}"' in where[key])


# ==========================================================================
print("\n[5] summary：全默认返回空串（界面上那行小字就不显示）")
# ==========================================================================

check("全默认 → 空串", taskspec.summary("d_farm", taskspec.values("d_farm", {})), "")
check("改了 target → 显示人话",
      taskspec.summary("d_farm", {"target": 120, "max_hours": 8, "course": ""}),
      "刷到多久 120")
# ↑ 界面上那行小字。`_fmt()` 保证 120.0 印成 120 而不是「刷到多久 120.0」。
check("select 显示的是 label 不是 name",
      taskspec.summary("d_exam",
                       {"course": "", "only": "questionnaire",
                        "all": False, "max_courses": 0}),
      "仅问卷")
check("选只做考核显示「仅考核」",
      taskspec.summary("d_exam",
                       {"course": "", "only": "exam",
                        "all": False, "max_courses": 0}),
      "仅考核")
check("switch 打开时显示它的 label",
      taskspec.summary("d_exam",
                       {"course": "", "only": "both", "all": True,
                        "max_courses": 0}),
      "已经做完的课也重做一遍")
# ★ 用户 m19597 第 2 条那个开关也要在这行小字里看得见，
# 否则「明明勾了」和「没勾」在界面上长得一模一样。
check("勾了自动切换课程也显示出来",
      taskspec.summary("d_watch",
                       {"course": "", "lessons": 0, "max_courses": 0,
                        "switch_course": True}),
      "自动切换课程")
check("没勾自动切换课程就不显示（这行小字全默认时是空的）",
      taskspec.summary("d_watch",
                       {"course": "", "lessons": 0, "max_courses": 0,
                        "switch_course": False}),
      "")
check("多个改动用 · 连起来",
      taskspec.summary("d_farm",
                       {"target": 120, "all_courses": False,
                        "max_hours": 2, "course": "老年"}),
      "刷到多久 120 · 每门课最多刷几小时 2 · 只做哪门课 老年")
check("刷时长勾了「每门课都刷一遍」也显示出来",
      taskspec.summary("d_farm",
                       {"target": 90, "all_courses": True,
                        "max_hours": 8, "course": ""}),
      "每门课都刷一遍")


# ==========================================================================
print("\n[6] save / write_config：只动自己那几个键，且要原子写")
# ==========================================================================

tmpdir = Path(tempfile.mkdtemp(prefix="opt_test_"))
cfgfile = tmpdir / "config.json"
_orig_config_path = paths.config_path
paths.config_path = lambda: cfgfile  # type: ignore[assignment]

try:
    seed = {
        "adb": {"path": "C:/adb.exe", "address": "127.0.0.1:16384"},
        "screenshot": {"target_long_side": 1280},
        "inference": {"mode": "auto", "gpu_id": ""},
        "browser": {"phone": "", "debug_port": 9222},
        "options": {"d_watch": {"lessons": 5}},
    }
    cfgfile.write_text(json.dumps(seed, ensure_ascii=False, indent=2),
                       encoding="utf-8")

    taskspec.save("d_farm", {"target": 120, "max_hours": 3, "course": "老年"})
    back = json.loads(cfgfile.read_text(encoding="utf-8"))
    check("新任务的值写进去了",
          back["options"]["d_farm"],
          {"target": 120.0, "max_hours": 3, "course": "老年"})
    check("原来别的任务的设置没被抹掉",
          back["options"]["d_watch"]["lessons"], 5)
    check("adb 节没被动", back["adb"]["path"], "C:/adb.exe")
    check("screenshot 节没被动", back["screenshot"]["target_long_side"], 1280)

    # 存 d_watch 不能反过来影响 d_farm
    taskspec.save("d_watch", {"course": "", "lessons": 9, "max_courses": 1})
    back = json.loads(cfgfile.read_text(encoding="utf-8"))
    check("存 d_watch 之后 d_farm 还在",
          back["options"]["d_farm"]["target"], 120)
    check("d_watch 的值更新了", back["options"]["d_watch"]["lessons"], 9)

    # 落盘的是合法数字（不是字符串）—— 手改配置的人看到 `120.0` 才不会被误导。
    # `target` 是 `kind="float"`（要能填 1.5 小时），所以是 float 不是 int；
    # 但**绝不能是字符串** —— 字符串会让 `--target "120"` 这种一路混到 argparse。
    check("落盘的是数字不是 str",
          isinstance(back["options"]["d_farm"]["target"], (int, float))
          and not isinstance(back["options"]["d_farm"]["target"], bool))
    check("落盘的 target 是 float", isinstance(
        back["options"]["d_farm"]["target"], float))

    taskspec.save_global({"inference.mode": "cpu",
                          "inference.gpu_id": "2",
                          "browser.debug_port": 9333})
    back = json.loads(cfgfile.read_text(encoding="utf-8"))
    check("全局设置写进了它本来的家（不搬家）",
          back["inference"], {"mode": "cpu", "gpu_id": 2})
    check("端口写进去了", back["browser"]["debug_port"], 9333)
    check("browser 节里别的键还在", back["browser"]["phone"], "")
    check("写全局设置也没碰任务选项",
          back["options"]["d_farm"]["target"], 120)

    # 非法值在落盘时也要被挡住
    taskspec.save("d_farm", {"target": "九十分钟", "max_hours": 8, "course": ""})
    back = json.loads(cfgfile.read_text(encoding="utf-8"))
    check("落盘时非法值被换成默认", back["options"]["d_farm"]["target"], 90.0)

    # 编码：Windows 上不带 BOM 才不会被别的工具读成乱码
    taskspec.save("d_farm", {"target": 120, "max_hours": 8, "course": "老年认知"})
    raw = cfgfile.read_bytes()
    check("写出来不带 BOM", raw[:3] != b"\xef\xbb\xbf")
    check("中文原样写进去（没被转成 \\uXXXX）",
          "老年认知".encode("utf-8") in raw)
    check("中文能原样读回来",
          taskspec.values("d_farm")["course"], "老年认知")
    check("临时文件没留下", not (tmpdir / "config.json.tmp").exists())
    check("结尾有换行（免得下次 diff 一片红）", raw.endswith(b"\n"))

    # 配置坏掉时 read_config 返回 {}，界面照样能打开
    cfgfile.write_text("{ 这不是 JSON", encoding="utf-8")
    check("坏配置 → 空 dict，不抛异常", paths.read_config(), {})
    check("坏配置时 values() 仍然给全默认",
          taskspec.values("d_farm"),
          {"target": 90.0, "unit": "minute", "all_courses": False,
           "max_hours": 8, "course": ""})

    cfgfile.unlink()
    check("文件不在时 → 空 dict", paths.read_config(), {})
finally:
    paths.config_path = _orig_config_path  # type: ignore[assignment]


# ==========================================================================
print("\n[7] inference：读配置 + apply 用假 resource 验分支")
# ==========================================================================

check("默认 → auto / 无适配器", inference.read({}), ("auto", None))
check("mode 大写也认",
      inference.read({"inference": {"mode": "GPU"}})[0], "gpu")
check("mode 带空格也认",
      inference.read({"inference": {"mode": " cpu "}})[0], "cpu")
check("mode 非法 → auto",
      inference.read({"inference": {"mode": "nope"}})[0], "auto")
check("inference 节不是 dict 也不炸",
      inference.read({"inference": "坏了"})[0], "auto")
check("gpu_id 空串 → None（交给框架自选）",
      inference.read({"inference": {"gpu_id": ""}})[1], None)
check("gpu_id = -1 → None", inference.read({"inference": {"gpu_id": -1}})[1], None)
check("gpu_id = 3 → 3", inference.read({"inference": {"gpu_id": 3}})[1], 3)
check("gpu_id 字符串数字也能读",
      inference.read({"inference": {"gpu_id": "2"}})[1], 2)
check("gpu_id 垃圾值 → None（不是 0！0 是那个慢 13 倍的虚拟适配器）",
      inference.read({"inference": {"gpu_id": "abc"}})[1], None)

check("describe 默认", inference.describe({}), "自动（推荐）")
check("describe 指定适配器",
      inference.describe({"inference": {"mode": "gpu", "gpu_id": 1}}),
      "显卡加速（适配器 1）")
check("describe cpu 时不显示适配器",
      inference.describe({"inference": {"mode": "cpu", "gpu_id": 1}}), "只用 CPU")


class FakeResource:
    """记下被调了什么，并按剧本返回成功/失败。

    真的 `Resource()` 要载框架的 DLL，而且**载入成功不代表生效**
    （惰性加载，见 `inference.py` 开头），拿它当断言等于没测。
    """

    def __init__(self, *, directml_ok: bool = True, cpu_ok: bool = True,
                 boom: str = "") -> None:
        self.calls: list[tuple] = []
        self.directml_ok = directml_ok
        self.cpu_ok = cpu_ok
        self.boom = boom

    def use_cpu(self) -> bool:
        self.calls.append(("cpu",))
        if self.boom == "cpu":
            raise RuntimeError("驱动炸了")
        return self.cpu_ok

    def use_directml(self, device_id=None) -> bool:
        self.calls.append(("directml", device_id))
        if self.boom == "directml":
            raise RuntimeError("显卡炸了")
        return self.directml_ok


r = FakeResource()
check("默认 auto → 走 directml 且不给适配器号",
      (inference.apply(r, {}), r.calls),
      ("显卡（DirectML，适配器由框架自选）", [("directml", None)]))
r = FakeResource()
check("mode=gpu → 同样走 directml",
      (inference.apply(r, {"inference": {"mode": "gpu"}}), r.calls),
      ("显卡（DirectML，适配器由框架自选）", [("directml", None)]))
r = FakeResource()
check("指定了适配器就传进去",
      (inference.apply(r, {"inference": {"gpu_id": 1}}), r.calls),
      ("显卡（DirectML，适配器 1）", [("directml", 1)]))
r = FakeResource()
check("mode=cpu → 走 use_cpu",
      (inference.apply(r, {"inference": {"mode": "cpu"}}), r.calls),
      ("只用 CPU", [("cpu",)]))

# 失败路径必须**不抛异常**：一个性能开关不该把整个程序弄挂
r = FakeResource(directml_ok=False)
logs: list[str] = []
check("显卡返回 False → 退回默认并记一行日志",
      inference.apply(r, {}, log=logs.append), "框架默认")
check("确实记了日志", len(logs) == 1)
check("日志说的是人话", "用不了显卡" in logs[0])

r = FakeResource(boom="directml")
check("显卡抛异常 → 也不抛出去", inference.apply(r, {}), "框架默认")
r = FakeResource(boom="cpu")
check("切 CPU 抛异常 → 也不抛出去",
      inference.apply(r, {"inference": {"mode": "cpu"}}),
      "CPU（设置失败，用框架默认）")

# 日志出口本身坏了也不能连带炸掉 —— 它可能接的是界面，界面已经关了
r = FakeResource()
def _bad_log(_msg: str) -> None:
    raise RuntimeError("日志通道断了")

check("日志出口抛异常也不影响返回",
      inference.apply(r, {}, log=_bad_log), "显卡（DirectML，适配器由框架自选）")

# gpu_id=0 要给出警告（实测那是最慢的那个虚拟适配器）
r = FakeResource()
warns: list[str] = []
inference.apply(r, {"inference": {"gpu_id": 0}}, log=warns.append)
check("填 0 会警告（那是虚拟显示器适配器）", len(warns) >= 1)
check("警告里说清了原因", "虚拟显示器" in warns[0])


# ==========================================================================
print("\n[8] desktop_farm：时间格式化与常量")
# ==========================================================================

check("fmt 4391 → 1小时13分11秒", desktop_farm.fmt(4391), "1小时13分11秒")
check("fmt 2712 → 45分12秒", desktop_farm.fmt(2712), "45分12秒")
# 不满一分钟也照样写「0分NN秒」，和平台自己那行 `.time_text`
# （`00分07秒`）是同一种口径 —— 宁可多两个字符，也别让「45秒」和
# 「45分」在日志里长得像。
check("fmt 45 → 0分45秒", desktop_farm.fmt(45), "0分45秒")
check("fmt 0 → 0分00秒", desktop_farm.fmt(0), "0分00秒")
check("fmt 3600 → 1小时00分00秒", desktop_farm.fmt(3600), "1小时00分00秒")
check("fmt 负数不炸（夹到 0）", desktop_farm.fmt(-5), "0分00秒")

check("默认目标是 90 分钟", desktop_farm.TARGET_MINUTES, 90.0)
check("轮询 10 秒一次", desktop_farm.POLL_SECONDS, 10.0)
# 卡死判据是 **`totalTime` 和播放位置双双不动**（`desktop_farm.py` 里的
# `moved = st["total"] != last_total or abs(cur - last_cur) > 0.5`）。
# 视频在播的时候 `cur` 每轮都在动，所以它才是主要的活性信号；
# 阈值只要**不短于** 300 秒的上报周期就不会误判。
check("卡死阈值不短于 300 秒的上报周期",
      desktop_farm.STALL_ROUNDS * desktop_farm.POLL_SECONDS >= 300,
      desktop_farm.STALL_ROUNDS * desktop_farm.POLL_SECONDS >= 300)
check("安全阀有默认值", desktop_farm.MAX_HOURS > 0)

# 判据本身是两个信号都得看 —— 只看 `total` 会被 300 秒的上报周期骗到，
# 每 5 分钟就误报一次「卡死」。
_src_farm = (Path(__file__).resolve().parent / "desktop_farm.py").read_text(
    encoding="utf-8")
check("卡死判据同时看 totalTime 和播放位置",
      'moved = st["total"] != last_total or abs(cur - last_cur)' in _src_farm)
check("弹框处理调的是 dismiss_dialog（点「取消」而不是「学习下一课节」）",
      "sess.dismiss_dialog()" in _src_farm)

# `desktop.parse_time_text` 是读「总计时长」的工具，和 fmt 互为逆运算
for secs in (0, 7, 45, 59, 60, 61, 2712, 3600, 3723, 4391):
    text = desktop_farm.fmt(secs)
    check(f"fmt/parse 往返 {secs}s（{text}）",
          desktop.parse_time_text(text), secs)
check("parse 认不出时返回 -1（不是 0 —— 要能分清「0 秒」和「没读出来」）",
      desktop.parse_time_text("废话"), -1)
check("parse 空串 → -1", desktop.parse_time_text(""), -1)


# ==========================================================================
print("\n[9] 弹窗与倒带：源码里那几句关键判据还在")
# ==========================================================================

# 这些是「取消」逻辑的核心，改动时最容易顺手删掉，钉一下。
check("KEEP_WORDS 里有「取消」", "取消" in desktop.Session.KEEP_WORDS)
check("LAYER_JS 里出现了「学习下一课节」（框的按钮文案）",
      "学习下一课节" in desktop.LAYER_JS)
check("LAYER_JS 只看可见的框（display:none 的要排除）",
      "display" in desktop.LAYER_JS)
check("REWIND_JS 会把 currentTime 归零", "currentTime = 0" in desktop.REWIND_JS)
check("REWIND_JS 会接着播", ".play()" in desktop.REWIND_JS)
# ★ 回归钉子（2026-10-08 真机踩过）：老版 REWIND_JS 是同步 IIFE，`play()` 的
# Promise 拒绝被 `p.catch(() => {})` 吞掉、**恒返回 'ok'**。于是上层那句
# `if what != "ok": start_video()` 的判据不可能为真，点按兜底永远不执行 ——
# 现象是刷时长每 10 秒报一次「被按停了 → 倒回 0 秒重播」，`cur` 死死停在 0，
# 连刷 30 轮一动不动。真因只是整页载入丢了用户激活、`play()` 回
# NotAllowedError，点一下视频就能恢复。
check("REWIND_JS 是 async IIFE（要看 900 毫秒后的 paused 才能下结论）",
      desktop.REWIND_JS.lstrip().startswith("(async ()"), True)
check("REWIND_JS 会把「没播起来」如实报出来",
      "still-paused" in desktop.REWIND_JS, True)
check("REWIND_JS 不再无条件返回 ok",
      desktop.REWIND_JS.count("return 'ok'"), 1)
_rw_src = desktop.Session.rewind_and_play.__doc__ or ""
check("rewind_and_play 的文档写明了 still-paused 要走上层兜底",
      "still-paused" in _rw_src, True)
# 刷时长那边必须留着这条兜底，否则上面那个真值也没人用
import desktop_farm as _farm  # noqa: E402
_farm_src = open(_farm.__file__, encoding="utf-8").read()
check("desktop_farm 里倒带失败会改用点按起播",
      'what != "ok"' in _farm_src and "start_video" in _farm_src, True)
check("STUDY_TIME_JS 读的是 totalTime", "totalTime" in desktop.STUDY_TIME_JS)
# ★ 总计时长是**按课**记的（同一账号两门课实测 4391 / 2220 秒）。
# 文档里那句「账号级」曾经把「刷哪门都一样」写进了日志，是错的。
check("study_time 的文档更正成了「按课」",
      "按课" in (desktop.Session.study_time.__doc__ or ""), True)
check("刷时长脚本里不再说「时长是账号级的」",
      "账号级的" not in _farm_src, True)

print()
print("=" * 68)
print(f"结果: {_passed} 通过 / {_failed} 失败")
print("=" * 68)
sys.exit(1 if _failed else 0)
