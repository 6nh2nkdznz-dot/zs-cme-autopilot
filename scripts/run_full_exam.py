"""把整份卷子跑完：反复走「作答当前题」直到题干不再变化，然后交卷。

## 为什么要循环调管线节点

「作答当前题」这个节点每次只处理**当前一题**（识别 + 点选 + 翻页），
这是刻意的设计——单节点职责单一，管线图也简单。

但一份卷子有 20 题，靠人手敲 20 次不现实，所以这里循环调用它。

## 怎么知道卷子做完了

靠**题干重复**：每题点完「下一题」后重新读题干，
如果连续两次读到同一个题干，说明已经在最后一题、翻不动了 → 交卷。

比数题号稳——平台会把题目打乱，题号不连续，而且题量也不固定。

## 交卷前的保险

如果循环结束仍有题未作答，提交会被「第 N 未选」拦下。
所以交卷走 `do_submit` 的循环逻辑：点提交 → 被拦就补答 → 再点。

用法:
    python scripts\\run_full_exam.py --dry-run      # 只看当前题和题库
    python scripts\\run_full_exam.py                # 做完整卷并交卷
    python scripts\\run_full_exam.py --max 40       # 最多做多少题（兜底）
    python scripts\\run_full_exam.py --no-submit    # 做完不交卷
"""

from __future__ import annotations

import argparse
import json
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
import quiz  # noqa: E402
import main as main_mod  # noqa: E402
from exam import (  # noqa: E402
    PAGE_ANSWER,
    PAGE_DIALOG,
    PAGE_RESULT,
    detect_page,
    page_name,
)
from controller import ConfigError, build_controller, load_config  # noqa: E402

#: 「作答当前题」节点里的两个 ROI，驱动脚本读题干判重要用同一个值
STEM_ROI = (0, 190, 720, 55)
SUBMIT_TAP = (694, 101)
NEXT_TAP = (600, 1247)
OPTION_ROWS = (268, 322, 377, 432)
ROW_TAP_X = 300

#: 交卷被「第 N 未选」拦下时的弹窗按钮
DIALOG_GOTO_TAP = (365, 739)
DIALOG_CANCEL_TAP = (461, 739)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=40, help="最多做多少题（兜底）")
    ap.add_argument("--dry-run", action="store_true", help="只看状态，不操作")
    ap.add_argument("--no-submit", action="store_true", help="做完不交卷")
    args = ap.parse_args()

    try:
        cfg = load_config()
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    resource.post_bundle(str(paths.resource_dir())).wait()
    resource.post_ocr_model(str(paths.ocr_model_dir())).wait()

    # **必须注册自定义模块**，否则管线里的 `custom_recognition: QuizAnswer`
    # 与 `custom_action: AnswerQuestion` 找不到实现，框架只会报
    # "recognition is null"，节点失败——这个错误很难联想到是注册缺失。
    # （实测踩过：驱动脚本自己建 Resource 却漏了这一步，整卷一道题都答不出。）
    registered = main_mod.register_custom_modules(resource)
    print(f"已注册自定义模块: {registered}")

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    from maa.pipeline import JOCR, JRecognitionType

    def ocr(img) -> str:
        detail = None
        try:
            j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
            if j.wait().succeeded:
                td = j.get()
                if td:
                    for nid in td.node_id_list:
                        node = tasker.get_node_detail(nid)
                        if node is not None and node.recognition is not None:
                            detail = node.recognition.all_results or []
                            break
        except Exception as exc:  # noqa: BLE001
            print(f"[ocr] 异常: {exc}")
        return " ".join(str(getattr(r, "text", "")) for r in (detail or []))

    def read_stem() -> str:
        img = controller.post_screencap().wait().get()
        x, y, w, h = STEM_ROI
        return ocr(img[y:y + h, x:x + w]).strip()

    def click(x, y, wait=1.2):
        controller.post_click(int(x), int(y)).wait()
        time.sleep(wait)

    def full_page() -> str:
        return ocr(controller.post_screencap().wait().get())

    cache = quiz.AnswerCache(paths.answer_cache_path())
    print("=" * 66)
    print(" 跑完整卷")
    print("=" * 66)
    print(f"题库: {len(cache)} 条")

    # --- 先确认在答题页 ---
    #
    # 走统一的页面识别层（exam.detect_page），不自己拼标志词——
    # 实测踩过的坑：交卷成功后页面已跳到结果页，脚本却以为提交失败，
    # 继续对着结果页「答题」——把「本次成绩：45分」当题干，而且**不报错**，
    # 一路点到超时。根因就是每个动作各自猜页面。
    page = full_page()
    kind = detect_page(page)
    print(f"当前页面: {page_name(kind)}")

    if kind != PAGE_ANSWER:
        if kind == PAGE_RESULT:
            print("[full] 在结果页 —— 这一轮已交过卷。先采集答案，再决定是否重做。")
            print("[full]   采集: python scripts\\harvest_answers.py")
        elif kind == PAGE_DIALOG:
            print("[full] 有弹窗挡着（未选提示）。")
        else:
            print("[full] 不在答题页，不动作。")
        print(f"[full]   页面文本: {page[:90]}")
        return 1

    stem = read_stem()
    print(f"起始题: {stem[:52]}")
    if args.dry_run:
        print("\n[dry-run] 不操作")
        return 0

    same_count = 0
    done = 0
    last = stem

    while done < args.max:
        print()
        print(f"--- 第 {done + 1} 次 ---")

        # 调管线节点做一题（识别 + 点选 + 翻页都在里面）。
        # 节点可能因「题库没命中 → 挂起」而返回失败——那是设计内的，
        # 所以失败时**重试几次**再判断，不要立刻当成「题干未变」。
        # （实测踩过：第一次失败就判卷子做完，整卷没答完就去交卷了。）
        ok = False
        for attempt in range(1, 4):
            main_mod._reset_analyze_state()
            tjob = tasker.post_task("作答当前题").wait()
            if tjob.succeeded:
                ok = True
                break
            print(f"[full] 节点执行失败（第 {attempt} 次尝试）")
            time.sleep(1.0)

        time.sleep(1.5)
        now = read_stem()

        if not ok and now == last:
            print(f"[full] 多次尝试均未作答，停止: {now[:44]}")
            break

        done += 1

        if now and now == last:
            same_count += 1
            print(f"[full] 题干未变（第 {same_count} 次）: {now[:46]}")
            if same_count >= 2:
                print("[full] 连续两次题干未变 → 判定已到最后一题")
                break
        else:
            same_count = 0
            print(f"[full] 下一题: {now[:46]}")
        last = now or last

    print()
    print(f"[full] 共处理 {done} 题")

    if args.no_submit:
        print("[full] --no-submit，不交卷")
        return 0

    # --- 交卷（带「第 N 未选」补答循环）---
    #
    # ⚠️ 检测要**快**。实测提交成功后中文提示 toast 只显示一两秒，
    # 而页面切到结果页也很快。早先每次点完等 3 秒再 OCR，结果：
    # toast 已消失、结果页还没读全 → 判定「未成功」→ 再点一次。
    # 可是结果页**没有提交按钮**，那些点击都打空了，白点 9 次。
    # 现在改成点完立刻连查几轮，每次间隔很短。
    import re
    pat = re.compile(r"第\s*(\d+)\s*[题]?\s*未选")

    for attempt in range(1, 10):
        print(f"\n[submit] 第 {attempt} 次点提交")
        click(*SUBMIT_TAP, wait=0.6)

        # 点完立刻连查：先抓 toast（转瞬即逝），再看是否已到结果页
        submitted = False
        for _ in range(6):
            text = full_page()
            if "提交成功" in text or "提交完成" in text:
                print("[submit] ✓ 看到「提交成功」提示")
                submitted = True
                break
            if detect_page(text) == PAGE_RESULT:
                print("[submit] ✓ 已到结果页")
                submitted = True
                break
            time.sleep(0.8)

        if submitted:
            return 0

        time.sleep(1.2)
        text = full_page()

        m = pat.search(text)
        if m:
            print(f"[submit] 被拦：第 {m.group(1)} 未选，去补答")
            click(*DIALOG_GOTO_TAP, wait=3.0)
            # 判断题只有两行；这里简单点第一行，采集环节会纠正
            click(ROW_TAP_X, OPTION_ROWS[0], wait=1.5)
            continue

        if "未选" in text:
            print("[submit] 有未选提示但读不出题号，关掉重试")
            click(*DIALOG_CANCEL_TAP, wait=1.5)
            continue

        print(f"[submit] 未见成功标志，页面: {text[:100]}")

    print("[submit] ✗ 未能确认提交成功")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
