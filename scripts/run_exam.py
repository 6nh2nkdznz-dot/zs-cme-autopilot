"""把考核整卷跑一遍（用于验证答题流程）。

这是**验证工具**，不是日常入口。日常入口是 `ZSCMEAutopilot.exe` 里的
「进入考核并答题」。

为什么要单独有这么一个脚本：答题流程涉及「翻页是否生效」「提交后长什么样」
这些只能真跑一遍才知道的事，而 GUI 里跑一次要等整门课。这个脚本直接在
当前答题页上跑，省时间。

用法:
    python scripts\\run_exam.py --dry-run          # 只读不点，看题目解析对不对
    python scripts\\run_exam.py --stop-after 3     # 只答 3 题，不交卷
    python scripts\\run_exam.py --submit           # 答完整卷并交卷
    python scripts\\run_exam.py --answer A          # 每题都选 A（默认随机）
"""

from __future__ import annotations

import argparse
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
    OPTION_ROWS,
    OPTIONS_ROI,
    STEM_ROI,
    TYPE_ROI,
    ExamConfig,
    ExamRunner,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="只识别不点击，用来核对题干/选项读得对不对")
    parser.add_argument("--stop-after", type=int, default=0,
                        help="答完 N 题就停，不交卷")
    parser.add_argument("--submit", action="store_true", help="答完后交卷")
    parser.add_argument("--answer", default="", help="每题固定选这个字母（默认随机）")
    parser.add_argument("--max", type=int, default=120, help="最多答多少题")
    args = parser.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    # --- 建一个常驻 OCR（必须，否则每题都要重载模型）---
    from maa.pipeline import JOCR, JRecognitionType
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    if not resource.post_bundle(str(paths.resource_dir())).wait().succeeded:
        print("[FATAL] 资源加载失败", file=sys.stderr)
        return 1
    resource.post_ocr_model(str(paths.ocr_model_dir())).wait()

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    def ocr(roi) -> str:
        x, y, w, h = roi
        img = controller.post_screencap().wait().get()
        return _ocr_img(img[y:y + h, x:x + w])

    def _ocr_img(img) -> str:
        """对整张（或裁好的）图跑 OCR，返回拼起来的文本。"""
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

    def read_stem() -> str:
        return ocr(STEM_ROI)

    def on_exam_page() -> bool:
        t = ocr(TYPE_ROI)
        return "选择" in t or "判断" in t

    def is_judge() -> bool:
        """当前页是不是判断题。

        实测判断题的选项字母是 T/F（正确/错误），不是 A/B。
        靠读选项文字判断，而不是靠题型标题——标题 OCR 偶尔会失手。
        """
        text = ocr(OPTIONS_ROI)
        if "正确" in text and "错误" in text:
            return True
        return ("T." in text or "T、" in text) and ("F." in text or "F、" in text)

    # --- 先在不在答题页 ---
    if not on_exam_page():
        print("[FATAL] 当前不在答题页。")
        print(f"        题型区识别结果: {ocr(TYPE_ROI)!r}")
        return 1
    print(f"[exam] 答题页确认，题型: {ocr(TYPE_ROI)!r}")

    # --- dry-run：只读几题看看 ---
    if args.dry_run:
        print("\n[dry-run] 只识别不点击。翻 5 页看看：\n")
        for i in range(1, 6):
            stem = read_stem()
            print(f"  第 {i} 页题干: {stem}")
            opts = []
            for r in OPTION_ROWS:
                opts.append(ocr((0, r - 22, 660, 44)))
            for lb, t in zip("ABCD", opts):
                print(f"      {lb}. {t}")
            print()
            if i < 5:
                controller.post_click(600, 1247).wait()
                time.sleep(2.0)
        return 0

    # --- 正式作答 ---
    fixed = args.answer.strip().upper()

    def resolve(_stem: str) -> list[str] | None:
        """返回要选的字母；None 表示没把握。

        这里刻意不调联网搜索——目的是先把**流程**跑通。
        真实使用时由 Pipeline 的 QuizAnswer 识别器负责，
        它会查本地题库、联网取证，拿不准就挂起。
        """
        if fixed:
            return [fixed]
        return None      # → ExamRunner 会随机选

    runner = ExamRunner(
        controller=controller,
        read_stem=read_stem,
        resolve_answer=resolve,
        cfg=ExamConfig(guess_when_unsure=True, max_questions=args.max),
        log=print,
        ocr=lambda img: _ocr_img(img),
        is_judge=is_judge,
    )

    progress = runner.run(stop_after=args.stop_after)

    print()
    print("=" * 60)
    print(f" 答题记录: 已答 {progress.answered}，"
          f"其中随机猜 {progress.guessed}，挂起 {progress.suspended}")
    print(f" 停止原因: {progress.stopped_reason}")
    print("=" * 60)

    if args.submit and not args.stop_after:
        print()
        runner.submit()
        time.sleep(4)
        from snap import save_png

        img = controller.post_screencap().wait().get()
        out = paths.snap_dir() / "exam-submitted.png"
        save_png(img, out)
        print(f"[exam] 已提交，结果页截图: {out}")
        print(f"[exam] 提交后 OCR: {ocr((0, 100, 720, 600))[:300]}")
    else:
        print("\n（未交卷。加 --submit 才会交卷。）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
