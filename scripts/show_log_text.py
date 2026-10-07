"""打印改造后的日志文案，用来肉眼检查「看不看得懂」。

## 为什么要有这个

用户反馈过「你写的让人看不懂」。日志文案是**给人看的**，
所以改完必须能一眼扫一遍，而不是靠读 diff 猜效果。

这个脚本不连设备、不跑任务，只把 `self.log(...)` 里的字符串按顺序
打出来，模拟真实运行时的样子。

用法：
    python scripts/show_log_text.py            # 看全部
    python scripts/show_log_text.py course     # 只看某个文件
"""

from __future__ import annotations

import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

#: 日志文案集中在这些文件里（其余文件不产日志或已不参与运行）
SOURCES = ("core.py", "course.py", "exam.py", "checkin.py")

_CALL = re.compile(
    r"self\.log\(\s*(f?)(['\"])(.*?)\2",
    re.S,
)


def collect(path: Path) -> list[str]:
    """把一个文件里所有 self.log(...) 的第一段文本抽出来。"""
    src = io.open(path, encoding="utf-8").read()
    out: list[str] = []
    for _isf, _q, text in _CALL.findall(src):
        # 多行拼接的字符串在这里只取到第一段，够看措辞了
        text = text.replace("\\n", " ")
        out.append(text)
    return out


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    total = 0
    for name in SOURCES:
        if want and want not in name:
            continue
        path = SCRIPTS / name
        if not path.is_file():
            continue
        lines = collect(path)
        total += len(lines)
        print(f"\n{'=' * 70}\n{name}（{len(lines)} 条）\n{'=' * 70}")
        for i, t in enumerate(lines, 1):
            print(f"{i:3d}. {t}")
    print(f"\n合计 {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
