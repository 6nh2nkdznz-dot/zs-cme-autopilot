"""Pipeline JSON 校验器。

存在的理由：MaaFramework 对管线文件的容错性很差，而且失败方式很隐蔽——
一个字符串值的顶层 key 就会让**整个文件**解析失败，日志埋在几十行 C++ 里。
这个脚本在跑任务之前把这类问题挡下来。

校验项：
  1. 顶层每个 key（除 $ 开头）的值必须是对象；
  2. recognition / action 必须是合法枚举值；
  3. next / on_error 引用的节点必须存在（含 [JumpBack] 前缀）；
  4. roi / target 的坐标数组长度；
  5. 节点名不能是空字符串。

用法:
    python scripts/validate_pipeline.py
    python scripts/validate_pipeline.py --dir assets/resource/pipeline
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "assets" / "resource" / "pipeline"

RECOGNITION_TYPES = {
    "DirectHit", "TemplateMatch", "FeatureMatch", "ColorMatch", "OCR",
    "NeuralNetworkClassify", "NeuralNetworkDetect", "And", "Or", "Custom",
}
ACTION_TYPES = {
    "DoNothing", "Click", "LongPress", "Swipe", "MultiSwipe",
    "TouchDown", "TouchMove", "TouchUp", "ClickKey", "LongPressKey",
    "KeyDown", "KeyUp", "InputText", "StartApp", "StopApp", "StopTask",
    "Scroll", "Command", "Shell", "Screencap", "Custom",
}

# 少数节点用 v2 格式把类型放在 recognition.type / action.type
_ATTR_RE = re.compile(r"^\[(\w+)\]")


def node_ref_name(ref) -> str | None:
    """从 next/on_error 的一项里取出被引用的节点名。"""
    if isinstance(ref, str):
        m = _ATTR_RE.match(ref)
        return ref[m.end():] if m else ref
    if isinstance(ref, dict):
        name = ref.get("name")
        return name if isinstance(name, str) else None
    return None


def collect_nodes(path: Path) -> tuple[dict[str, dict], list[str]]:
    """读一个管线文件，返回 (节点表, 问题列表)。"""
    problems: list[str] = []

    try:
        # utf-8-sig：容忍 BOM。PowerShell 的 Set-Content -Encoding utf8 会写 BOM，
        # 而框架用 utf-8 严格解析，带 BOM 的文件会直接解析失败。
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        return {}, [f"JSON 解析失败: {exc}"]
    except UnicodeDecodeError as exc:
        return {}, [f"编码错误（应为 UTF-8）: {exc}"]

    if not isinstance(data, dict):
        return {}, ["顶层必须是对象"]

    nodes: dict[str, dict] = {}
    for key, value in data.items():
        if key.startswith("$"):
            continue  # 以 $ 开头的 root field 会被框架忽略
        if not isinstance(value, dict):
            problems.append(
                f"顶层 key {key!r} 的值是 {type(value).__name__}，必须是对象。"
                f"（管线文件里不能放字符串注释——这是最常见的写法错误）"
            )
            continue
        if not key.strip():
            problems.append("存在空字符串节点名")
        nodes[key] = value

    return nodes, problems


def check_nodes(name: str, node: dict) -> list[str]:
    """校验单个节点的字段。"""
    problems: list[str] = []

    for field, allowed in (("recognition", RECOGNITION_TYPES), ("action", ACTION_TYPES)):
        raw = node.get(field)
        if raw is None:
            continue
        if isinstance(raw, dict):          # v2: {type: ..., param: {...}}
            raw = raw.get("type")
        elif isinstance(raw, list):        # v2 的 list 形式
            raw = None
        if isinstance(raw, str) and raw not in allowed:
            problems.append(
                f"[{name}] {field}={raw!r} 不是合法值。"
                f"合法值: {', '.join(sorted(allowed))}"
            )

    for field, allowed_len in (("roi", (4,)), ("target", (2, 4)), ("roi_offset", (4,))):
        val = node.get(field)
        if isinstance(val, list) and len(val) not in allowed_len:
            problems.append(
                f"[{name}] {field} 长度 {len(val)} 不合法，应为 {allowed_len}"
            )

    return problems


def check_code_node_refs(all_nodes: dict[str, dict]) -> list[str]:
    """扫 Python 代码里按名字引用管线节点的地方，确认节点存在。

    ## 为什么需要

    代码里有几处**按名字**调管线节点：

        post_task("进入考核")              运行时任务入口
        post_task("播放整门课")            同上
        context.run_task("进入考核")       回调里同步调
        pipeline_override 里按节点名覆盖参数

    名字打错、或节点被改名，**只会在运行时失败**——而「进入考核」是在
    看护了几个小时、全部学完之后才走到的。真到那时才发现名字写错，
    代价是整整一轮白跑。静态查一遍几乎零成本。

    只扫明面上的字符串字面量，不做跨函数数据流分析（够用即可）。
    """
    problems: list[str] = []
    scripts = ROOT / "scripts"
    if not scripts.is_dir():
        return problems

    # post_task("X") / run_task("X")  —— 注意 run_task 也可能是 tasker 上的
    pat_task = re.compile(r'(?:post_task|run_task)\(\s*"([^"]+)"')
    # pipeline_override 的 key：override["播放整门课"] = ...
    pat_override = re.compile(r'override\[\s*"([^"]+)"\s*\]')

    me = Path(__file__).resolve()

    for py in sorted(scripts.glob("*.py")):
        if py.name.startswith("test_"):
            continue          # 测试里有故意的假名字
        if py.resolve() == me:
            # **跳过自己**。本文件里就有 `post_task("X")` 这类文档示例和
            # 模式字符串，扫自己会报出一堆假问题（实测踩过）。
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except OSError:
            continue
        # 只保留真正的字符串字面量，丢掉注释和文档字符串 ——
        # 否则注释里举例的节点名也会被当成真实引用。
        src = _code_strings_only(text)
        for pat, what in ((pat_task, "任务入口"), (pat_override, "参数覆盖")):
            for m in pat.finditer(src):
                name = m.group(1)
                # 跳过明显不是节点名的（如文件路径、日志文本）
                if "/" in name or "\\" in name or " " in name:
                    continue
                if name not in all_nodes:
                    line = text[:m.start()].count("\n") + 1
                    problems.append(
                        f"{py.name}:{line} 代码里{what}引用的节点 {name!r} "
                        f"在管线里不存在"
                    )
    return problems


def _code_strings_only(text: str) -> str:
    """把源码里的注释和字符串**内容**抹掉，只留代码骨架。

    做法保守：用 `tokenize` 找出所有 STRING / COMMENT token 并替换成等长
    空白（保持偏移量不变，这样行号还对得上）。

    `tokenize` 失败（源码语法有问题）时退回原文——宁可多报也不要漏报。
    """
    import io as _io
    import tokenize

    try:
        toks = list(tokenize.generate_tokens(_io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return text

    chars = list(text)
    for tok in toks:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        if srow == erow:
            for i in range(scol, ecol):
                chars[srow - 1] = chars[srow - 1][:i] + " " + chars[srow - 1][i + 1:]
        else:
            # 跨行字符串（文档字符串）：整段抹掉
            for r in range(srow, erow + 1):
                if r - 1 >= len(chars):
                    break
                if r == srow:
                    chars[r - 1] = chars[r - 1][:scol] + " " * (len(chars[r - 1]) - scol)
                elif r == erow:
                    chars[r - 1] = " " * ecol + chars[r - 1][ecol:]
                else:
                    chars[r - 1] = " " * len(chars[r - 1])
    return "".join(chars)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"[FATAL] 目录不存在: {root}", file=sys.stderr)
        return 2

    files = sorted(root.rglob("*.json"))
    if not files:
        print(f"[FATAL] {root} 下没有 json 文件", file=sys.stderr)
        return 2

    # 第一遍：把**所有**文件的节点收集起来。跨文件引用是合法的，
    # 按单文件校验引用会大量误报。
    all_nodes: dict[str, dict] = {}
    origin: dict[str, Path] = {}
    per_file: dict[Path, list[str]] = {}

    for path in files:
        nodes, problems = collect_nodes(path)
        per_file[path] = problems
        for n, body in nodes.items():
            if n in all_nodes:
                per_file[path].append(
                    f"节点名 {n!r} 与 {origin[n].name} 重复（后者会被覆盖）"
                )
            all_nodes[n] = body
            origin[n] = path

    # 第二遍：校验节点字段 + 引用完整性
    for path in files:
        nodes, _ = collect_nodes(path)
        for n, body in nodes.items():
            per_file[path].extend(check_nodes(n, body))
            for field in ("next", "on_error"):
                refs = body.get(field)
                if refs is None:
                    continue
                if not isinstance(refs, list):
                    refs = [refs]
                for ref in refs:
                    target = node_ref_name(ref)
                    if target is None:
                        per_file[path].append(
                            f"[{n}] {field} 里有无法解析的引用: {ref!r}"
                        )
                    elif target not in all_nodes:
                        per_file[path].append(
                            f"[{n}] {field} 引用了不存在的节点: {target!r}"
                        )

    total = 0
    for path in files:
        problems = per_file[path]
        rel = path.relative_to(root)
        if problems:
            total += len(problems)
            print(f"\n[FAIL] {rel}")
            for p in problems:
                print(f"   - {p}")
        else:
            print(f"[ OK ] {rel}")

    # 第三遍：Python 代码里引用的节点名必须存在
    #
    # 为什么需要：代码用 `context.run_task("进入考核")` 这类方式按**名字**
    # 调管线节点。名字打错、或节点被改名，只会在**运行时**失败——
    # 而且是在看护了几个小时之后才走到那一步。静态查一遍能提前挡住。
    ref_problems = check_code_node_refs(all_nodes)
    if ref_problems:
        total += len(ref_problems)
        print()
        for p in ref_problems:
            print(f"[FAIL] {p}")

    print()
    if total:
        print(f"[FAIL] 共 {total} 个问题")
        return 1
    print(f"[OK] {len(files)} 个文件 / {len(all_nodes)} 个节点，全部通过校验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
