"""把课程进度文件里「归一化后同名」的记录合并成一条。

## 为什么会有重复

课程名在不同地方写法不同：列表卡片上是**截断**的（`老年认知症患者护理人文关怀实..`），
而手工记录/别处可能是完整名。归一化（`course_progress.normalize_course`）
虽然让查询能命中，但文件里仍会留下两条键。

两条键本身不影响功能（查得到就行），但看着混乱、也容易误判「记了几节」。
所以合并一次：按归一化后的名字归并 lessons 取并集。

用法:
    python scripts\\merge_course_progress.py
    python scripts\\merge_course_progress.py --dry-run
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

import paths  # noqa: E402
from course_progress import normalize_course  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    f = paths.data_dir() / "course_progress.json"
    if not f.is_file():
        print(f"没有进度文件: {f}")
        return 0

    raw = json.loads(io.open(f, encoding="utf-8").read())
    print(f"原键（{len(raw)} 个）:")
    for k in raw:
        n = len((raw[k].get("lessons") or {}))
        print(f"  {k!r}  ({n} 节)")

    merged: dict[str, dict] = {}
    for key, entry in raw.items():
        nk = normalize_course(key) or "(未知课程)"
        tgt = merged.setdefault(nk, {"lessons": {}, "updated": ""})
        tgt["lessons"].update(entry.get("lessons") or {})
        if (entry.get("updated") or "") > tgt["updated"]:
            tgt["updated"] = entry["updated"]

    if len(merged) == len(raw) and set(merged) == set(raw):
        print("\n没有需要合并的键。")
        return 0

    print(f"\n合并后（{len(merged)} 个）:")
    for k in merged:
        n = len(merged[k]["lessons"])
        print(f"  {k!r}  ({n} 节)")

    if args.dry_run:
        print("\n[dry-run] 未写入")
        return 0

    io.open(f, "w", encoding="utf-8").write(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"\n已写入 {f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
