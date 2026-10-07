"""挂起题目的管理工具（人工确认闭环的命令行端）。

答题引擎遇到拿不准的题会把它挂起到 `debug/pending/questions.jsonl`。
本工具让你**离线**把这些题答掉，答案写进本地题库，之后同题永久命中。

这样程序不用一直挂着等人——可以课间、隔天再补，甚至换台机器答复制过来的挂起文件。

用法:
    python scripts\\pending.py list                 # 列出待答题
    python scripts\\pending.py show 3               # 看第 3 题的完整内容
    python scripts\\pending.py answer 3 A           # 第 3 题答 A
    python scripts\\pending.py answer 3 A C         # 多选题答 AC
    python scripts\\pending.py answer 3 --from-cache  # 用联网搜索结果作参考
    python scripts\\pending.py stats                # 题库统计
    python scripts\\pending.py export 题库.json      # 导出题库
    python scripts\\pending.py import 题库.json      # 导入别人的题库
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import paths  # noqa: E402
import quiz  # noqa: E402
from quiz import AnswerCache, Option, Question  # noqa: E402


def cmd_list(args) -> int:
    items = quiz.load_pending(paths.debug_dir())
    if not items:
        print("没有待答题。")
        return 0

    print(f"共 {len(items)} 道待答题")
    print("提示：序号会随答题重排；想稳妥可用 --key（下面每题的【键】）\n")
    for i, rec in enumerate(items, 1):
        stem = rec.get("stem", "")
        kind = rec.get("qtype", "?")
        note = rec.get("note", "")
        print(f"[{i}] ({kind}) {stem[:52]}{'…' if len(stem) > 52 else ''}")
        opts = rec.get("options") or []
        if opts:
            print("     " + "  ".join(f"{o['label']}.{o['text'][:14]}" for o in opts))
        if note:
            print(f"     ↳ {note}")
        print(f"     键: {rec.get('key', '')}")
        print()
    print("答题: python scripts\\pending.py answer <序号> <字母...>")
    return 0


def cmd_show(args) -> int:
    items = quiz.load_pending(paths.debug_dir())
    if not items:
        print("没有待答题。")
        return 0
    if not (1 <= args.index <= len(items)):
        print(f"[FATAL] 序号超出范围（1..{len(items)}）", file=sys.stderr)
        return 2

    rec = items[args.index - 1]
    print("=" * 60)
    print(f"题干: {rec.get('stem')}")
    print(f"题型: {rec.get('qtype')}")
    print("-" * 60)
    for o in rec.get("options") or []:
        print(f"  {o['label']}. {o['text']}")
    print("-" * 60)
    print(f"挂起原因: {rec.get('note', '(无)')}")
    print(f"挂起时间: {rec.get('at')}")
    print(f"缓存键  : {rec.get('key')}")
    print("=" * 60)
    return 0


def cmd_answer(args) -> int:
    items = quiz.load_pending(paths.debug_dir())
    if not items:
        print("没有待答题。")
        return 0

    # target 是纯数字就按序号（方便手敲），否则当缓存键（稳定）。
    # 不用 --key 选项——argparse 会把 `--key "xxx" A C` 里的 A 当成序号，
    # 这个坑实测踩过。
    target = (args.target or "").strip()
    if target.isdigit():
        idx = int(target)
        if not (1 <= idx <= len(items)):
            print(f"[FATAL] 序号超出范围（1..{len(items)}）", file=sys.stderr)
            return 2
        rec = items[idx - 1]
    else:
        rec = next((r for r in items if r.get("key") == target), None)
        if rec is None:
            print(f"[FATAL] 找不到匹配的待答题: {target!r}", file=sys.stderr)
            print("       用 `list` 查看可用的序号或键", file=sys.stderr)
            return 2

    labels = [x.strip().upper() for x in (args.labels or []) if x.strip()]

    valid = {o["label"].strip().upper() for o in (rec.get("options") or [])}
    bad = [x for x in labels if valid and x not in valid]
    if bad:
        print(f"[FATAL] 选项 {bad} 不存在。本题可选: {sorted(valid)}", file=sys.stderr)
        return 2

    if not labels:
        print("[FATAL] 没有给出答案。用法: answer <序号或键> A B ...", file=sys.stderr)
        return 2

    if rec.get("qtype") == "multi" and len(labels) < 2:
        print(f"[警告] 这是多选题，只给了 {len(labels)} 个答案。"
              f"确认无误请加 --force")
        if not args.force:
            return 2

    cache = AnswerCache(paths.answer_cache_path())
    n = quiz.resolve_pending(paths.debug_dir(), {rec["key"]: labels}, cache)

    print(f"✓ 已入库 {n} 题: {rec.get('stem', '')[:40]} -> {labels}")
    print(f"  题库现有 {len(cache)} 条")

    # 还有剩的就把下一题也显示出来，省一次命令。
    # 注意：用「剩余的第一题」而不是固定序号——答完一题后列表会重排，
    # 按序号接着答会答错题（这个 bug 实测踩过）。
    rest = quiz.load_pending(paths.debug_dir())
    if rest:
        nxt = rest[0]
        print(f"\n下一题 ({len(rest)} 道剩余)")
        print(f"  {nxt.get('stem', '')}")
        for o in nxt.get("options") or []:
            print(f"    {o['label']}. {o['text']}")
        print(f"\n  作答: python scripts\\pending.py answer {nxt.get('key','')} <字母...>")
    else:
        print("\n全部答完，没有待答题了。")
    return 0


def cmd_stats(args) -> int:
    cache = AnswerCache(paths.answer_cache_path())
    stats = cache.stats()
    total = stats.pop("__total__", 0)

    print(f"题库: {paths.answer_cache_path()}")
    print(f"共 {total} 条\n")
    if total:
        print("来源分布:")
        for src, cnt in sorted(stats.items(), key=lambda kv: -kv[1]):
            label = {"human": "人工确认", "web": "联网命中", "cache": "历史"}.get(src, src)
            print(f"  {label:<10} {cnt}")
    print(f"\n待答题: {len(quiz.load_pending(paths.debug_dir()))} 道")
    return 0


def cmd_export(args) -> int:
    src = paths.answer_cache_path()
    if not src.is_file():
        print("[FATAL] 题库文件不存在", file=sys.stderr)
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    out = Path(args.file)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已导出 {len(data)} 条到 {out}")
    return 0


def cmd_import(args) -> int:
    src = Path(args.file)
    if not src.is_file():
        print(f"[FATAL] 文件不存在: {src}", file=sys.stderr)
        return 1
    try:
        incoming = json.loads(src.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        print(f"[FATAL] JSON 解析失败: {exc}", file=sys.stderr)
        return 1
    if not isinstance(incoming, dict):
        print("[FATAL] 题库格式应为 {题干: {...}}", file=sys.stderr)
        return 1

    cache = AnswerCache(paths.answer_cache_path())
    before = len(cache)

    added = updated = skipped = 0
    for key, entry in incoming.items():
        if not isinstance(entry, dict) or not entry.get("labels"):
            skipped += 1
            continue
        existing = cache._data.get(key)  # 直接操作，避免 put() 每题都写盘
        if existing is None:
            added += 1
        elif existing.get("labels") == entry.get("labels"):
            skipped += 1
            continue
        else:
            # 已有记录且答案不同：保留本地的人工答案，不被覆盖
            if existing.get("source") == "human" and entry.get("source") != "human":
                skipped += 1
                continue
            updated += 1
        cache._data[key] = entry

    cache.save()
    print(f"✓ 导入完成: 新增 {added}, 更新 {updated}, 跳过 {skipped}")
    print(f"  题库 {before} -> {len(cache)} 条")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="挂起题目管理 / 题库维护")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出待答题").set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="查看某题完整内容")
    p_show.add_argument("index", type=int)
    p_show.set_defaults(func=cmd_show)

    p_ans = sub.add_parser("answer", help="回答某题并入库")
    p_ans.add_argument("target",
                       help="题干序号（纯数字）或缓存键（其余情况）。"
                            "序号会随答题重排，稳妥用键")
    p_ans.add_argument("labels", nargs="*", help="选项字母，如 A 或 A C")
    p_ans.add_argument("--force", action="store_true",
                       help="多选题只给一个答案时强制提交")
    p_ans.set_defaults(func=cmd_answer)

    sub.add_parser("stats", help="题库统计").set_defaults(func=cmd_stats)

    p_exp = sub.add_parser("export", help="导出题库")
    p_exp.add_argument("file")
    p_exp.set_defaults(func=cmd_export)

    p_imp = sub.add_parser("import", help="导入题库（人工答案优先，不会被覆盖）")
    p_imp.add_argument("file")
    p_imp.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
