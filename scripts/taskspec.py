# -*- coding: utf-8 -*-
"""任务的声明式配置 —— 每个功能能调什么、怎么变成命令行参数。

## 这套东西照谁做的

照 **MaaFramework 的 ProjectInterface V2 协议**
（`vendor/MaaFramework/docs/zh_cn/3.3-ProjectInterfaceV2协议.md`），
也就是 MaaAssistantArknights 那套界面的模型。它把「界面上能勾什么、能填什么」
全部**声明**出来，界面只管照着渲染，自己不写任何业务判断。三个概念各司其职：

* `task`    —— 一个可勾选的任务（`core.DESKTOP_TASKS` / `WECHAT_TASKS` 里那几行）；
* `option`  —— 一个可配置项，带 `type`（switch / select / input）和默认值；
* `setting` —— **一组 option，渲染成「设置」面板里的一个分区**。

照搬这套的好处：**新加一个可调项只改这一张表**。界面控件、配置读写、
命令行拼装全都从表里推出来，不会出现「界面上加了开关、但参数没接到脚本上」
这种半截活 —— 那正是加设置最容易踩的坑。

## 和 MAA 的两处不同（都是刻意的）

1. **`entry` 换成了 `flag`。** MAA 的 option 最终改的是 pipeline 节点参数；
   我们这边每个可调项就是一条命令行开关（`--target 120`）。所以 option 上带
   `flag`，`to_argv()` 直接拼 argv 交给各脚本自己的 `argparse`。
   ★ 刻意**不去调内部函数**：命令行选项已经在各脚本里定义好了，再拼一遍
   等于把同一套语义维护两份，改了一边忘了另一边就会**静默地**用错参数跑一整门课。
2. **`path` 指明存哪儿。** MAA 的 `pipeline_override` 不需要落盘；我们这边
   用户填的值得记住，所以每个 option 带一条指向 `config.json` 的路径。

## 存哪儿

* 任务选项 → `config.json` 的 `options.<任务键>.<项>`；
* 全局设置 → 它**本来就在配置里的家**（`inference.mode` / `browser.debug_port` …），
  不搬家。设置界面直接读写那几节，手工编辑配置的人和界面看到的是同一份值。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import paths  # noqa: E402

# --------------------------------------------------------------------------
# 声明模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    """`select` 的一个候选项。

    `flag` 是这个候选项对应的命令行开关（空 = 不加参数，比如「两者都做」
    本来就不需要开关）。MAA 那边对应 `cases[].pipeline_override`。
    """

    name: str
    label: str
    flag: str = ""
    description: str = ""


@dataclass(frozen=True)
class Option:
    """一个可配置项。`type` 决定界面上做成什么控件、`flag` 决定怎么进命令行。"""

    key: str
    label: str
    #: 写进 `config.json` 的路径，例如 `("options", "d_farm", "target")`。
    path: tuple[str, ...]
    #: `switch`（勾选框）/ `select`（下拉）/ `input`（输入框）。
    type: str = "switch"
    #: 对应的命令行参数名，例如 `"--target"`。空 = 这一项不进命令行。
    flag: str = ""
    description: str = ""
    default: Any = None
    cases: tuple[Case, ...] = ()
    #: `input` 的类型：`int` / `float` 会做范围校验并转成数字，`str` 原样传。
    kind: str = "int"
    minimum: int | float | None = None
    maximum: int | float | None = None
    #: 输入框空白时的提示（等价于「不限」的那种项）。
    placeholder: str = ""
    #: `input` 专用：界面上打点显示（API 密钥这种不能给人看见的）。
    secret: bool = False


@dataclass(frozen=True)
class Section:
    """一组 option —— 就是 MAA 协议里的 `setting` 分区。"""

    name: str
    label: str
    description: str = ""
    options: tuple[Option, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------
# 各任务的可配置项
# --------------------------------------------------------------------------

#: 三个任务都用得上的「只做名字含这几个字的课」。放一份，省得三处抄。
_COURSE = Option(
    key="course",
    label="只做哪门课",
    path=("options", "", "course"),  # `path` 里的空段由 `_bind()` 填上任务键
    type="input",
    flag="--course",
    kind="str",
    default="",
    placeholder="留空 = 所有没做完的课，按列表顺序",
    description="填课程名里的一段字就行（例如「老年认知」）。留空就一门一门往后做。",
)

_MAX_COURSES = Option(
    key="max_courses",
    label="最多做几门课",
    path=("options", "", "max_courses"),
    type="input",
    flag="--max-courses",
    default=0,
    minimum=0,
    maximum=200,
    placeholder="0 = 不限",
    description="跑到这门数就收工。第一次跑建议先填 1，看看效果对不对。",
)


def _bind(opt: Option, task_key: str) -> Option:
    """把 `_COURSE` 这类共用模板的 `path` 绑到具体任务上。

    dataclass 是 frozen 的（怕界面那边手滑改掉声明），所以只能重建一个。
    """
    from dataclasses import replace

    return replace(opt, path=(opt.path[0], task_key, opt.path[2]))


#: 任务键 → 可配置项。**顺序就是设置面板里从上到下的顺序**（和 MAA 一致）。
_TASK_OPTION_SPECS: dict[str, tuple[Option, ...]] = {
    "d_watch": (
        Option(
            key="switch_course",
            label="自动切换课程",
            path=("options", "", "switch_course"),
            type="switch",
            flag="--switch-course",
            default=False,
            description=(
                "一门课的全部讲次看完之后，自动接着看下一门。"
                "**不勾就只看一门**，看完停下 —— 第一次跑建议先不勾，"
                "确认能正常播、能记账再放开。"
                "勾上之后「最多做几门课」才起作用（不勾时它被强制成 1）。"
            ),
        ),
        _COURSE,
        Option(
            key="lessons",
            label="每门课最多看几讲",
            path=("options", "", "lessons"),
            type="input",
            flag="--lessons",
            default=0,
            minimum=0,
            maximum=99,
            placeholder="0 = 全部看完",
            description="用来先试水：填 1 就只看一讲，确认能正常播、能记账再放开。",
        ),
        _MAX_COURSES,
    ),
    "d_farm": (
        Option(
            key="target",
            label="刷到多久",
            path=("options", "", "target"),
            type="input",
            flag="--target",
            kind="float",
            default=90.0,
            minimum=0.1,
            maximum=100000.0,
            placeholder="例如 90",
            description=(
                "视频上方那个「总计时长」刷到这个数就停。"
                "单位由下面那项决定。"
                "★ 它是**按课**记的 —— 同一账号在两门课上实测读到 73分11秒 "
                "和 37分00秒，所以「90 分钟」是每门课各自 90 分钟。"
                "循环当前这一讲就够了，不必往下讲走。"
            ),
        ),
        Option(
            key="unit",
            label="时长单位",
            path=("options", "", "unit"),
            type="select",
            flag="--unit",
            default="minute",
            cases=(
                Case("minute", "分钟", "", "默认。平台自己也是按分钟/秒显示的。"),
                Case("hour", "小时", "", "填「1.5」这种小数也行。"),
            ),
            description="配合上面那个数字用。填 1.5 小时一样能算。",
        ),
        Option(
            key="all_courses",
            label="每门课都刷一遍",
            path=("options", "", "all_courses"),
            type="switch",
            flag="--all-courses",
            default=False,
            description=(
                "总计时长是按课记的，想让 17 门课都到 90 分钟就得勾这个。"
                "★ 很慢：一门课最多要刷 90 分钟，全刷完可能要十几个小时。"
                "不勾就只刷第一门。"
            ),
        ),
        Option(
            key="max_hours",
            label="每门课最多刷几小时",
            path=("options", "", "max_hours"),
            type="input",
            flag="--max-hours",
            default=8,
            minimum=1,
            maximum=72,
            description="安全阀，防止挂着忘了关。到点就自动收工。",
        ),
        _COURSE,
    ),
    "d_exam": (
        _COURSE,
        Option(
            key="only",
            label="做哪一半",
            path=("options", "", "only"),
            type="select",
            default="both",
            cases=(
                Case("both", "考核 + 问卷都做", "",
                     "默认。考核 ≥60 分才放行问卷，所以两件得连着做。"),
                Case("exam", "仅考核", "--exam-only", ""),
                Case("questionnaire", "仅问卷", "--questionnaire-only", ""),
            ),
            description="平台规定考核没到 60 分就不放行问卷，所以一般不用改。",
        ),
        Option(
            key="all",
            label="已经做完的课也重做一遍",
            path=("options", "", "all"),
            type="switch",
            flag="--all",
            default=False,
            description="默认会跳过「考核过了 + 问卷交了」的课。想刷高分时才勾。",
        ),
        Option(
            key="ai_base",
            label="AI 接口地址",
            path=("options", "", "ai_base"),
            type="input",
            flag="--ai-base",
            kind="str",
            default="",
            placeholder="留空 = 不启用 AI，例如 https://api.openai.com/v1",
            description=(
                "填了才启用 AI 答题。任何 **OpenAI 兼容**的接口都行："
                "官方的 https://api.openai.com/v1、国内大模型的兼容端点、"
                "本地 ollama 的 http://127.0.0.1:11434/v1 都可以。"
                "★ 题库里已经有的题照样用缓存答案，只有**没见过**的题才去问 AI。"
            ),
        ),
        Option(
            key="ai_key",
            label="AI 密钥",
            path=("options", "", "ai_key"),
            type="input",
            flag="--ai-key",
            kind="str",
            default="",
            secret=True,
            placeholder="sk-…（本地 ollama 随便填个 x 就行）",
            description=(
                "存在 `data/config.json` 里，**是明文**，别把这份配置发给别人。"
                "界面上打点显示。"
            ),
        ),
        Option(
            key="ai_model",
            label="AI 模型名",
            path=("options", "", "ai_model"),
            type="input",
            flag="--ai-model",
            kind="str",
            default="",
            placeholder="例如 gpt-4o-mini / qwen-plus / llama3.1",
            description="问哪家的哪个模型。填错接口会回 404，日志里能看到原因。",
        ),
        _MAX_COURSES,
    ),
    # `d_finish`（申请结课）**故意留空**：它只有「点一下申请」这一个动作，
    # 没有任何可调的东西（原来那个「已经结课的也再走一遍」开关没人会用，
    # 结课之后本来就不能再刷分）。空元组 → `has_options()` 为假 →
    # 界面上不建那个 ⚙ 按钮。用户 m19597 要的就是这个。
    "d_finish": (),
}

#: `d_watch` 之类的键 → 绑定好 `path` 的 option 元组。
TASK_OPTIONS: dict[str, tuple[Option, ...]] = {
    key: tuple(_bind(o, key) if "" in o.path else o for o in opts)
    for key, opts in _TASK_OPTION_SPECS.items()
}


#: 全局设置分区 —— 就是 MAA 协议里的 `setting[]`。
SETTINGS: tuple[Section, ...] = (
    Section(
        name="inference",
        label="算力",
        description=(
            "文字识别用哪个算力跑。改完下次开始运行时生效。"
        ),
        options=(
            Option(
                key="mode",
                label="推理设备",
                path=("inference", "mode"),
                type="select",
                default="auto",
                cases=(
                    Case("auto", "自动（推荐）", "",
                         "交给 MaaFramework 自己挑。实测它挑的就是显卡，"
                         "一张图 107 毫秒，比强制 CPU 的 352 毫秒快 3.3 倍。"),
                    Case("gpu", "显卡加速", "",
                         "强制走 DirectML。自动挑错了才需要选这个。"),
                    Case("cpu", "只用 CPU", "",
                         "兼容性最好，但最慢。显卡有问题时的退路。"),
                ),
                description="默认「自动」就够好，不用改。",
            ),
            Option(
                key="gpu_id",
                label="显卡序号",
                path=("inference", "gpu_id"),
                type="input",
                default="",
                minimum=0,
                maximum=15,
                placeholder="留空 = 自动挑（推荐）",
                flag="",
                description=(
                    "★ 建议留空。只有自动挑错了才填。⚠ 别随手填 0："
                    "实测适配器 0 是 MuMu/GameViewer 的虚拟显示器适配器，"
                    "一张图要 1409 毫秒，比 CPU 还慢 4 倍。真要指定，从 1 开始试。"
                ),
            ),
        ),
    ),
    Section(
        name="browser",
        label="浏览器",
        description="模拟器里那个 Chrome 的远程调试端口。",
        options=(
            Option(
                key="debug_port",
                label="调试端口",
                path=("browser", "debug_port"),
                type="input",
                default=9222,
                minimum=1024,
                maximum=65535,
                placeholder="默认 9222",
                description=(
                    "程序靠这个端口指挥浏览器（读 DOM、点按钮、读播放进度）。"
                    "只有和别的工具撞端口时才需要改。"
                ),
            ),
        ),
    ),
)


# --------------------------------------------------------------------------
# 查表与读写
# --------------------------------------------------------------------------

def options_for(task_key: str) -> tuple[Option, ...]:
    """这个任务有哪些可配置项（没有就是空元组）。"""
    return TASK_OPTIONS.get(task_key, ())


def all_options() -> dict[str, Option]:
    """`(分区, 键) -> Option`，全局设置和任务选项都在里面。

    键写成 `"<分区>.<项>"`（例如 `d_farm.target` / `inference.mode`），
    界面上当作唯一标识使。
    """
    out: dict[str, Option] = {}
    for task_key, opts in TASK_OPTIONS.items():
        for o in opts:
            out[f"{task_key}.{o.key}"] = o
    for sec in SETTINGS:
        for o in sec.options:
            out[f"{sec.name}.{o.key}"] = o
    return out


def has_options(task_key: str) -> bool:
    """界面上决定那个 ⚙ 按钮要不要能点。"""
    return bool(TASK_OPTIONS.get(task_key))


def _dig(cfg: dict, path: tuple[str, ...]) -> Any:
    """按路径取值，缺任何一层都返回 `None`（不抛 KeyError）。"""
    cur: Any = cfg
    for part in path:
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _plant(cfg: dict, path: tuple[str, ...], value: Any) -> None:
    """按路径写值，中间缺的层自动建。"""
    cur = cfg
    for part in path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[path[-1]] = value


def coerce(opt: Option, raw: Any) -> Any:
    """把界面上/配置里的原始值转成该有的类型。

    非法值一律**回退到默认**，不抛异常 —— 用户手改配置写了个
    `"target": "九十分钟"`，总不能让设置面板打不开。
    """
    if opt.type == "switch":
        if isinstance(raw, bool):
            return raw
        return bool(raw) if raw is not None else opt.default

    if opt.type == "select":
        names = {c.name for c in opt.cases}
        return raw if raw in names else opt.default

    # input
    if opt.kind in ("int", "float"):
        cast = int if opt.kind == "int" else float
        try:
            v = cast(str(raw).strip())
        except (TypeError, ValueError):
            return opt.default
        if opt.minimum is not None and v < opt.minimum:
            return opt.default
        if opt.maximum is not None and v > opt.maximum:
            return opt.default
        return v
    return opt.default if raw is None else str(raw)


def values(task_key: str, cfg: dict | None = None) -> dict[str, Any]:
    """这个任务当前的选项值（配置里存的 + 没存的补默认）。

    `cfg` 给 `None` 时自己去读配置 —— 调用方（界面）大多数时候不关心从哪读。
    """
    cfg = paths.read_config() if cfg is None else cfg
    out: dict[str, Any] = {}
    for opt in options_for(task_key):
        stored = _dig(cfg, opt.path)
        out[opt.key] = opt.default if stored is None else coerce(opt, stored)
    return out


def global_values(cfg: dict | None = None) -> dict[str, Any]:
    """全局设置的值，键是 `"<分区>.<项>"`。"""
    cfg = paths.read_config() if cfg is None else cfg
    out: dict[str, Any] = {}
    for sec in SETTINGS:
        for opt in sec.options:
            stored = _dig(cfg, opt.path)
            out[f"{sec.name}.{opt.key}"] = (
                opt.default if stored is None else coerce(opt, stored)
            )
    return out


def save(task_key: str, vals: dict[str, Any]) -> None:
    """把某个任务的选项写回 `config.json`（只动它自己那几个键）。

    读-改-写整份配置：那个文件里还有 adb 路径、截图设置等别人写的东西，
    整份覆盖会把它们抹掉。
    """
    cfg = paths.read_config()
    for opt in options_for(task_key):
        if opt.key in vals:
            _plant(cfg, opt.path, coerce(opt, vals[opt.key]))
    paths.write_config(cfg)


def save_global(vals: dict[str, Any]) -> None:
    """把全局设置写回 `config.json`。`vals` 的键是 `"<分区>.<项>"`。"""
    cfg = paths.read_config()
    for sec in SETTINGS:
        for opt in sec.options:
            name = f"{sec.name}.{opt.key}"
            if name in vals:
                _plant(cfg, opt.path, coerce(opt, vals[name]))
    paths.write_config(cfg)


# --------------------------------------------------------------------------
# 变成命令行参数
# --------------------------------------------------------------------------

def fmt_value(value: Any) -> str:
    """把值写成界面上/命令行里的样子。

    `float` 直接 `str()` 会得到 `90.0`，于是设置面板的输入框里显示成
    `90.0`、任务标题旁边那行小字显示成「刷到多久 90.0」——
    看着像程序出了毛病，而 90 和 90.0 本来就是同一个数。
    整数值的 float 去掉小数点后的零，其他（含 1.5 这种）照原样。

    只在这里做一次：**输入框初值、范围提示、`to_argv`、`summary` 共用**，
    免得四处各漂各的（argparse 的 `type=float` 对 `90` 和 `90.0` 一视同仁）。
    """
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def to_argv(task_key: str, vals: dict[str, Any]) -> list[str]:
    """把选项值拼成命令行 argv。

    **只有非空值才拼**：没填的项交给各脚本自己的 `argparse` 默认值，
    单一事实来源留在那边，这边不抄一份默认值（抄了就会漂移）。
    """
    argv: list[str] = []
    for opt in options_for(task_key):
        v = vals.get(opt.key, opt.default)

        if opt.type == "switch":
            if v and opt.flag:
                argv.append(opt.flag)
            continue

        if opt.type == "select":
            case = next((c for c in opt.cases if c.name == v), None)
            if not case:
                continue
            if case.flag:
                # 老写法：一个选项值对应一个独立开关（`--exam-only`）。
                argv.append(case.flag)
            elif opt.flag:
                # 新写法：值当参数传（`--unit hour`）。`Option.flag` 为空的
                # select（例如 `d_exam.only`）本来就没有命令行形态，
                # 靠 cases 各自的 flag 表达，所以这里要判一下。
                argv += [opt.flag, case.name]
            continue

        # input：空值不拼（`--course ""` 和不给 `--course` 是一回事）
        if not opt.flag or v is None or str(v).strip() == "":
            continue
        argv += [opt.flag, fmt_value(v)]
    return argv


def summary(task_key: str, vals: dict[str, Any]) -> str:
    """把改动过的项拼成一行，给界面在任务标题旁边显示。

    全默认时返回空串 —— 界面上就不显示那行小字，保持干净。
    """
    bits: list[str] = []
    for opt in options_for(task_key):
        v = vals.get(opt.key, opt.default)
        if v == opt.default or v in ("", None, False):
            continue
        if opt.type == "select":
            case = next((c for c in opt.cases if c.name == v), None)
            bits.append(case.label if case else str(v))
        elif opt.type == "switch":
            bits.append(opt.label)
        elif opt.secret:
            # ★ 密钥绝不能进这行小字 —— 它显示在任务标题旁边，
            # 而窗口是给人看、可能被截图/录屏的。只说「填了」。
            bits.append(f"{opt.label} 已填")
        else:
            bits.append(f"{opt.label} {fmt_value(v)}")
    return " · ".join(bits)
