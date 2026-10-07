"""补全考核里所有未作答的题，然后交卷。

平台在提交时会逐个提示「单选题 第 N 未选，立即去做题？」。
本脚本就顺着这个提示循环：点「去做题」→ 答当前题 → 再提交 → 直到不再提示。

## 为什么要用「提交→提示」这个循环，而不是自己翻遍全卷

翻页式试卷有 20 题，自己从前翻到尾要 20 次翻页，而且**最后一题的
「下一题」是禁用状态**，翻页判断容易出错（实测踩过：点「上一题」
只回退一题，以为回到了第 1 题，结果整轮只答了 2 题）。

而平台的提示直接告诉你**哪一题缺**，跟着走更可靠，也省事。

## 实测的弹窗

```
温馨提示
⚠ 单选题 第 2 未选，立即去做题？
   [去做题] (365,739)    [取消] (461,739)
```

用法:
    python scripts\\fill_exam.py --dry-run      # 只看看缺哪些题
    python scripts\\fill_exam.py --answer A     # 每题填 A
    python scripts\\fill_exam.py                # 随机填，填完交卷
    python scripts\\fill_exam.py --max 25       # 最多补多少轮（兜底）
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import paths  # noqa: E402
from controller import ConfigError, build_controller, load_config  # noqa: E402
from exam import (  # noqa: E402
    DIALOG_CANCEL_TAP,
    DIALOG_GOTO_TAP,
    OPTIONS_ROI,
    OPTION_ROWS,
    ROW_TAP_X,
    STEM_ROI,
    SUBMIT_TAP,
    TYPE_ROI,
    label_to_row,
)

# 「单选题 第 2 未选」/「第 3 题未选」等
_MISSING_RE = re.compile(r"第\s*(\d+)\s*[题]?\s*未选")
# 弹窗里的提示语
_DIALOG_HINT = "未选"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只报告缺哪些题，不点")
    parser.add_argument("--answer", default="", help="每题填这个字母（默认随机）")
    parser.add_argument("--max", type=int, default=30, help="最多补多少轮")
    parser.add_argument("--submit", action="store_true", default=True,
                        help="补完自动交卷（默认开）")
    args = parser.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    resource.post_bundle(str(paths.resource_dir())).wait()
    resource.post_ocr_model(str(paths.ocr_model_dir())).wait()
    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    def ocr_img(img) -> str:
        j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
        if not j.wait().succeeded:
            return ""
        td = j.get()
        if td is None:
            return ""
        for nid in td.node_id_list:
            node = tasker.get_node_detail(nid)
            if node is None or node.recognition is None:
                continue
            return " ".join(str(getattr(r, "text", ""))
                            for r in (node.recognition.all_results or []))
        return ""

    def shot():
        return controller.post_screencap().wait().get()

    def ocr_roi(roi) -> str:
        x, y, w, h = roi
        return ocr_img(shot()[y:y + h, x:x + w])

    def click(x, y, wait=2.0):
        controller.post_click(int(x), int(y)).wait()
        time.sleep(wait)

    def is_judge() -> bool:
        t = ocr_roi(OPTIONS_ROI)
        if "正确" in t and "错误" in t:
            return True
        return ("T." in t or "T、" in t) and ("F." in t or "F、" in t)

    def missing_number(full_text: str) -> int | None:
        m = _MISSING_RE.search(full_text)
        return int(m.group(1)) if m else None

    print("=" * 60)
    print(" 补全考核未作答题")
    print("=" * 60)

    filled: list[str] = []

    for round_no in range(1, args.max + 1):
        # 先点提交，看平台提示什么
        click(*SUBMIT_TAP, wait=3.0)
        full = ocr_img(shot())
        q = missing_number(full)

        if q is None:
            if _DIALOG_HINT in full:
                print(f"[fill] 有未选提示但没解析出题号: "
                      f"{full[:120]}")
                click(*DIALOG_CANCEL_TAP, wait=1.5)
                break
            print(f"\n[fill] ✓ 平台不再提示缺题（第 {round_no} 轮）")
            break

        print(f"\n[fill] 第 {round_no} 轮：平台提示「{q} 未选」")

        if args.dry_run:
            click(*DIALOG_CANCEL_TAP, wait=1.0)
            print("[fill] dry-run：不点击，退出")
            break

        # 点「去做题」跳到那一题
        click(*DIALOG_GOTO_TAP, wait=3.5)

        stem = ocr_roi(STEM_ROI)
        judge = is_judge()
        letters = "TF" if judge else "ABCD"
        lb = args.answer.strip().upper() if args.answer else random.choice(letters)
        if lb not in letters:
            lb = random.choice(letters)

        row = label_to_row(lb)
        if row is None:
            print(f"[fill] 字母 {lb} 无法映射，跳过")
            click(*DIALOG_CANCEL_TAP, wait=1.0)
            continue

        click(ROW_TAP_X, OPTION_ROWS[row], wait=1.5)
        filled.append(f"{q}:{lb}")
        print(f"[fill]   {stem[:40]}")
        print(f"[fill]   {'判断题' if judge else '单选'} → 填 {lb}")

    print()
    print("=" * 60)
    print(f" 已补 {len(filled)} 题: {' '.join(filled) if filled else '(无)'}")
    print("=" * 60)

    # 最后再提交一次
    if not args.dry_run:
        print("\n[fill] 最后提交一次")
        click(*SUBMIT_TAP, wait=4.0)
        final = ocr_img(shot())
        from snap import save_png

        out = paths.snap_dir() / "exam-final.png"
        save_png(shot(), out)
        print(f"[fill] 截图: {out}")
        print(f"[fill] 提交后页面文本:\n{final[:400]}")

        q = missing_number(final)
        if q is not None:
            print(f"\n[fill] ⚠ 仍提示「{q} 未选」——补全没走完")
        elif "未选" in final:
            print("\n[fill] ⚠ 仍有未选提示，但没解析出题号")
        else:
            print("\n[fill] ✓ 已无未选提示")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
