"""浏览 MaaFramework 的 pipeline.schema.json，列出所有可用字段。

这份 schema（182KB）是从官方仓库拉下来的**权威字段全集**，
比 md 文档更完整——每个字段都带 description/types/enum/default。

用法:
    python scripts\\inspect_schema.py                 # 总览
    python scripts\\inspect_schema.py --grep 导航       # 按关键词搜字段说明
    python scripts\\inspect_schema.py --field next     # 看某字段的完整定义
    python scripts\\inspect_schema.py --actions        # 列出所有动作类型
    python scripts\\inspect_schema.py --recognitions   # 列出所有识别类型
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "vendor" / "MaaFramework" / "tools" / "pipeline.schema.json"


def load() -> dict:
    if not SCHEMA.is_file():
        raise SystemExit(
            f"找不到 {SCHEMA}\n"
            "先跑: powershell -File scripts\\fetch_maafw_docs.ps1"
        )
    with io.open(SCHEMA, encoding="utf-8") as fh:
        return json.load(fh)


def walk(node, path="", depth=0):
    """递归遍历 schema，产出 (路径, 节点) 对。

    注意这份 schema 是 **JSON Schema draft 2020-12**：
    节点属性在 `patternProperties` 下（不是 `properties`），
    可复用定义在 `$defs` 下（不是 `definitions`）。
    """
    if depth > 12 or not isinstance(node, dict):
        return
    yield path, node
    for key in ("properties", "patternProperties", "definitions", "$defs"):
        sub = node.get(key)
        if isinstance(sub, dict):
            for name, child in sub.items():
                yield from walk(child, f"{path}.{name}" if path else name, depth + 1)
    for key in ("items", "additionalProperties", "oneOf", "anyOf", "allOf"):
        sub = node.get(key)
        if isinstance(sub, dict):
            yield from walk(sub, f"{path}.{key}", depth + 1)
        elif isinstance(sub, list):
            for i, child in enumerate(sub):
                yield from walk(child, f"{path}.{key}[{i}]", depth + 1)


def resolve_ref(schema: dict, ref: str) -> dict:
    """解析 `$ref`（形如 `#/$defs/Node`）。"""
    if not ref.startswith("#/"):
        return {}
    node = schema
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def node_props(schema: dict) -> dict:
    """取「节点」这一层的字段定义。

    这份 schema 把节点定义放在 `$defs` 里，顶层用
    `patternProperties["^(?!\\$).*"] = {"$ref": "#/$defs/..."}`
    引用它——即「所有不以 $ 开头的键都是节点」。
    所以要顺着 $ref 走一层才能拿到真正的 properties。
    """
    pp = schema.get("patternProperties") or {}
    for _pattern, sub in pp.items():
        if not isinstance(sub, dict):
            continue
        if "properties" in sub:
            return sub["properties"]
        ref = sub.get("$ref")
        if ref:
            resolved = resolve_ref(schema, ref)
            props = resolved.get("properties")
            if isinstance(props, dict):
                return props
    return {}


def short_desc(node: dict, limit: int = 100) -> str:
    d = node.get("description") or node.get("title") or ""
    d = " ".join(str(d).split())
    return d[:limit] + ("…" if len(d) > limit else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grep", default="", help="按关键词搜字段名与说明")
    ap.add_argument("--field", default="", help="打印某字段的完整定义")
    ap.add_argument("--actions", action="store_true", help="列出动作类型")
    ap.add_argument("--recognitions", action="store_true", help="列出识别类型")
    ap.add_argument("--enums", action="store_true",
                    help="列出所有带 enum 的定义（动作/识别类型等）")
    ap.add_argument("--defs", action="store_true", help="列出所有 $defs 名称")
    ap.add_argument("--depth", type=int, default=1, help="总览时展开的深度")
    args = ap.parse_args()

    schema = load()

    if args.defs:
        defs = schema.get("$defs") or {}
        print(f"=== $defs 共 {len(defs)} 个 ===")
        for name in sorted(defs):
            node = defs[name]
            desc = short_desc(node, 60) if isinstance(node, dict) else ""
            print(f"  {name:<38} {desc}")
        return 0

    if args.enums:
        defs = schema.get("$defs") or {}
        print("=== 带 enum 的定义（动作/识别类型的合法取值）===")
        for name in sorted(defs):
            node = defs[name]
            if not isinstance(node, dict):
                continue
            enum = node.get("enum")
            if not enum:
                continue
            print(f"\n--- {name} ---")
            print(f"    {short_desc(node, 120)}")
            for e in enum:
                print(f"      {e}")
        return 0

    if args.actions or args.recognitions:
        target = "Action" if args.actions else "Recognition"
        print(f"=== {target} 相关定义 ===")
        for path, node in walk(schema):
            if target.lower() in path.lower():
                enum = node.get("enum")
                if enum:
                    print(f"\n{path}")
                    print(f"  {short_desc(node, 200)}")
                    for v in enum:
                        print(f"    - {v}")
        return 0

    if args.field:
        hits = [(p, n) for p, n in walk(schema) if args.field.lower() in p.lower()]
        if not hits:
            print(f"没找到含 {args.field!r} 的字段")
            return 1
        for p, n in hits[:12]:
            print("=" * 70)
            print(p)
            print("=" * 70)
            print(json.dumps(n, ensure_ascii=False, indent=2)[:2500])
            print()
        return 0

    if args.grep:
        kw = args.grep.lower()
        print(f"=== 搜 {args.grep!r} ===")
        n = 0
        for path, node in walk(schema):
            blob = (path + " " + str(node.get("description", ""))).lower()
            if kw in blob:
                n += 1
                print(f"\n{path}")
                desc = short_desc(node, 300)
                if desc:
                    print(f"  {desc}")
                for k in ("type", "enum", "default", "minimum", "maximum"):
                    if k in node:
                        v = node[k]
                        if k == "enum" and isinstance(v, list) and len(v) > 12:
                            v = v[:12] + ["…"]
                        print(f"  {k}: {v}")
        if n == 0:
            print("（无匹配）")
        else:
            print(f"\n共 {n} 处匹配")
        return 0

    # 总览
    print("=== pipeline.schema.json 总览 ===")
    print("（节点级字段——即管线里每个节点对象能写哪些键）\n")
    props = node_props(schema)
    if not props:
        print("没取到节点级 properties，schema 结构可能变了")
        return 1
    for name, node in sorted(props.items()):
        t = node.get("type")
        if isinstance(t, list):
            t = "|".join(str(x) for x in t)
        print(f"  {name:<24} {str(t):<16} {short_desc(node, 70)}")

    defs = schema.get("$defs") or {}
    print(f"\n另有 {len(defs)} 个 $defs 定义（动作/识别的参数细节在里面）")
    print("  用 --grep <关键词> 或 --field <字段名> 深入查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
