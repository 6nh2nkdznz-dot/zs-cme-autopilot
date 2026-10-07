"""OCR 结果的后处理：把同一行的碎片拼起来、按坐标排序等。

## 为什么需要这个

OCR 会把**同一行**的文本拆成多段。实测例子（考核列表页的成绩区）：

```
y=222  x=66   '最高成绩：'
y=222  x=152  '60'
```

这两段其实是同一行「最高成绩： 60」。如果不拼起来，
按「标签: 数值」写的正则就匹配不到——因为正则看到的是两个独立字符串。

另一个实测例子（结果页题干）：

```
y=240  x=15   '1、脓毒症患者达到血压目标后仍需监测的是（单选题 5分）'
```

这种是完整的，不需要拼。所以拼接要温和：只合并 y 接近且 x 相邻的片段。

## 用法

    rows = ocr_rows()                    # [(文本, x, y), ...]
    lines = merge_same_line(rows)        # [(合并后的文本, y), ...]
    text = join_lines(lines)             # 一整个字符串，供正则用
"""

from __future__ import annotations

#: 同一行的 y 容差。字体高度约 20-30px，取 12 既能合并同行的
#: 上下抖动，又不会把相邻行误并。
ROW_Y_TOLERANCE = 12

#: 同一行内两段之间的最大水平间隙（像素）。超过就认为是不同栏位，
#: 中间补一个空格再拼——避免把「最高成绩：」和页脚的数字粘死。
MAX_INLINE_GAP = 300


def merge_same_line(
    rows: list[tuple[str, int, int]],
    y_tolerance: int = ROW_Y_TOLERANCE,
) -> list[tuple[str, int]]:
    """把同一 y 的 OCR 片段按 x 顺序拼起来。

    输入：[(文本, x, y), ...]（顺序任意）
    输出：[(合并后的行文本, y), ...]，按 y 升序
    """
    if not rows:
        return []

    # 先按 y 分桶
    ordered = sorted(rows, key=lambda r: (r[2], r[1]))
    buckets: list[list[tuple[str, int, int]]] = []
    for item in ordered:
        if buckets and abs(item[2] - buckets[-1][0][2]) <= y_tolerance:
            buckets[-1].append(item)
        else:
            buckets.append([item])

    lines: list[tuple[str, int]] = []
    for bucket in buckets:
        bucket.sort(key=lambda r: r[1])          # 同行内按 x 排
        parts: list[str] = []
        prev_right = None
        for text, x, y in bucket:
            t = text.strip()
            if not t:
                continue
            if prev_right is not None and x - prev_right > MAX_INLINE_GAP:
                # 间隙太大，可能是不同栏位，插个空格隔开
                parts.append(" ")
            parts.append(t)
            prev_right = x + len(t) * 8          # 粗估宽度，够用
        if parts:
            lines.append(("".join(parts), bucket[0][2]))

    return lines


def join_lines(lines: list[tuple[str, int]], sep: str = " ") -> str:
    """把合并后的行拼成一个字符串，供正则匹配用。"""
    return sep.join(t for t, _y in lines if t).strip()


def rows_to_text(
    rows: list[tuple[str, int, int]],
    sep: str = " ",
) -> str:
    """一步到位：原始 OCR 行 → 合并后的整页文本。"""
    return join_lines(merge_same_line(rows), sep=sep)
