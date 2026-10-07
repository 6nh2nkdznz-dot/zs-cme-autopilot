"""给题库里缺少 `answer_texts` 的旧记录回填答案正文。

## 为什么需要

题库早期只存「正确答案的字母」。但实测平台**会打乱选项顺序**：

```
第 1 次  2、脓毒症…  A.体温 B.电解质   C.凝血功能 D.微循环指标
第 2 次  2、脓毒症…  A.体温 B.凝血功能 C.电解质   D.微循环指标
```

字母只在采集那一次有效，换个顺序就答错。所以新记录都带 `answer_texts`
（答案对应的选项正文），答题时按正文重新映射到当前页面的字母。

旧记录没有这个字段。回填是安全的：记录里存的 `options` 就是采集当时的
原始顺序，`labels` 是针对那个顺序的，所以「labels + 当时的 options」
能唯一确定答案正文。

用法:
    python scripts\\migrate_answer_texts.py --dry-run
    python scripts\\migrate_answer_texts.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import paths  # noqa: E402
from quiz import AnswerCache  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cache = AnswerCache(paths.answer_cache_path())
    print("=" * 62)
    print(" 回填答案正文（answer_texts）")
    print("=" * 62)
    print(f"题库: {paths.answer_cache_path()}")
    print(f"共 {len(cache)} 条")

    done = skipped_no_options = skipped_no_match = already = 0

    for key, entry in cache._data.items():
        if entry.get("answer_texts"):
            already += 1
            continue

        labels = [str(x).upper() for x in (entry.get("labels") or [])]
        options = entry.get("options") or []
        if not options:
            # 没有选项就没法反查正文。判断题这类影响不大（字母就是 T/F）。
            skipped_no_options += 1
            continue

        by_label = {str(o.get("label", "")).upper(): str(o.get("text", ""))
                    for o in options}
        texts = [by_label.get(lb, "") for lb in labels]
        texts = [t for t in texts if t]

        if len(texts) != len(labels):
            skipped_no_match += 1
            print(f"  ! 跳过（字母在选项里找不到）: {key[:36]} labels={labels}")
            continue

        note = f"  {''.join(labels):<3} → {texts}"
        print(f"  + {key[:40]}{note}")
        if not args.dry_run:
            entry["answer_texts"] = texts
        done += 1

    if not args.dry_run and done:
        cache.save()

    print()
    print("=" * 62)
    print(f" 回填 {done} 条"
          f"{'（dry-run，未写入）' if args.dry_run else ''}")
    print(f" 已是新版: {already}  ·  无选项: {skipped_no_options}"
          f"  ·  字母对不上: {skipped_no_match}")
    print("=" * 62)

    if done == 0 and already == 0 and skipped_no_options == 0:
        print("没有需要处理的记录。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
