"""把 `_页面_结果页` 的判据同步成「只看逐题对照」。

## 为什么

早先要求「成绩区（本次成绩/最高成绩）+ 逐题对照」两个都命中才算结果页。
但实测滚到卷子中后段时，顶部成绩区**已滚出屏幕**，只剩逐题对照，
于是页面识别失败（判成「未知页面」）。

「您的答案 / 正确答案 / 答案解析」这三个词本身就**只有结果页才有**，
光凭它们足够判定。

这与脚本侧 `exam.detect_page` 的 PAGE_RESULT 判据保持一致——
两边词表不一致会导致「管线说在答题页、脚本说在结果页」这种自相矛盾。

用法:
    python scripts\\sync_result_page_rule.py
"""

from __future__ import annotations

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
PAGES = ROOT / "assets" / "resource" / "pipeline" / "05_pages.json"

NEW_COMMENT = [
    "在结果页 = 有逐题答案对照（您的答案 / 正确答案 / 答案解析）。",
    "这三个词只有结果页才有，光凭它们就够判定。",
    "",
    "早先还要求「成绩区」也命中，结果**页面一滚就失效**——实测滚到卷子",
    "中后段时「本次成绩/最高成绩」已滚出屏幕，只剩逐题对照，于是判不出来。",
    "教训：判据不要依赖会被滚动带走的内容。",
    "",
    "这与脚本侧 exam.detect_page 的 PAGE_RESULT 判据保持一致。",
]


def main() -> int:
    if not PAGES.is_file():
        print(f"[FATAL] 找不到 {PAGES}", file=sys.stderr)
        return 1

    with io.open(PAGES, encoding="utf-8") as fh:
        data = json.load(fh)

    node = data.get("_页面_结果页")
    if node is None:
        print("[FATAL] 05_pages.json 里没有 _页面_结果页", file=sys.stderr)
        return 1

    before = list(node.get("all_of") or [])
    node["_comment"] = NEW_COMMENT
    node["all_of"] = ["_原子_结果页标志"]

    with io.open(PAGES, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"[sync] _页面_结果页 的 all_of: {before} -> {node['all_of']}")
    print("[sync] 判据已同步为「只看逐题对照」，与 exam.detect_page 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
