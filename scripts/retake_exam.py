"""考核总控：答题 → 交卷 → 采集答案 → 读分 → 低于阈值自动重做。

## 实测的关键事实（这些决定了策略）

1. **题目是随机抽取/乱序的**。实测第 2 次考核只有约 45% 的题与第 1 次重合。
   所以「采一轮就能满分」是错的假设——题库需要**多轮累积**才能覆盖。

2. **结果页逐题给出官方正确答案**。这是最好的答案来源，比联网搜可靠得多。
   但只有**成功交卷后**才能采到；提交被拦（有题未选）时采不到。

3. **提交会被「单选题 第 N 未选」拦下**。所以要循环「提交 → 补答 → 再提交」，
   直到确认成功。不确认就去采集，等于白跑一轮（这个 bug 实测踩过）。

4. **平台规则原文**：
   ```
   1、客观题考核可以重复提交。
   4、学生重复提交考核时，系统会记录重做次数。
   ```
   → 重做次数会被记录。所以要有上限，并且**每轮都必须有收获**（采到新题），
     否则重做只是白增计数。

## 收敛策略

```
每轮：进入答题 → 逐题作答（查题库）→ 补全 → 交卷 → 采集官方答案 → 读分
      达标 → 结束
      不达标且本轮采到新题 → 继续（题库变强了，下轮命中率更高）
      不达标且本轮没采到新题 → 停（再重做也不会更好）
```

最后那条「没收获就停」很重要：它保证重做次数不会被无意义地消耗。

用法:
    python scripts\\retake_exam.py --dry-run          # 只看当前分数与判定
    python scripts\\retake_exam.py                    # 自动跑到达标
    python scripts\\retake_exam.py --pass-score 90    # 自定义阈值
    python scripts\\retake_exam.py --max-attempts 6   # 最多重做几次
"""

from __future__ import annotations

import argparse
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

import exam  # noqa: E402
import paths  # noqa: E402
import quiz  # noqa: E402
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
    ExamConfig,
    ExamRunner,
    label_to_row,
)
from ocr_text import rows_to_text  # noqa: E402
from score import (  # noqa: E402
    DEFAULT_PASS_SCORE,
    ENTER_TAP_TOP,
    find_enter_button,
    looks_like_exam_list,
    looks_like_result_page,
    parse_score,
    should_retake,
)

#: 考核说明页底部按钮文案与其落点
BOTTOM_BUTTON_TEXTS = ("进入答题",)
ENTER_TAP_BOTTOM = (360, 1251)

#: 「单选题 第 2 未选」这类提示
_MISSING_RE = re.compile(r"第\s*(\d+)\s*[题]?\s*未选")

#: 结果页采集时的滚动
SCROLL_FROM = (360, 1000)
SCROLL_TO = (360, 450)


class ExamSession:
    """把一次运行里要用的 OCR / 点击原语打包，避免到处传闭包。"""

    def __init__(self, controller, tasker, log=print):
        self.controller = controller
        self.tasker = tasker
        self.log = log

    # --- OCR ---

    def _recognize(self, img) -> list[tuple[str, int, int]]:
        from maa.pipeline import JOCR, JRecognitionType

        j = self.tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
        if not j.wait().succeeded:
            return []
        td = j.get()
        if td is None:
            return []
        out: list[tuple[str, int, int]] = []
        for nid in td.node_id_list:
            node = self.tasker.get_node_detail(nid)
            if node is None or node.recognition is None:
                continue
            for r in (node.recognition.all_results or []):
                box = getattr(r, "box", None)
                txt = getattr(r, "text", None)
                if box and txt:
                    out.append((str(txt), int(box[0]), int(box[1])))
        return out

    def shot(self):
        return self.controller.post_screencap().wait().get()

    def rows(self) -> list[tuple[str, int, int]]:
        return self._recognize(self.shot())

    def text(self) -> str:
        """整屏文本。走「带坐标 OCR + 同行合并」，避免标签与数值被拆开。"""
        return rows_to_text(self.rows())

    def region(self, roi) -> str:
        x, y, w, h = roi
        return " ".join(t for t, _x, _y in self._recognize(self.shot()[y:y + h, x:x + w]))

    # --- 输入 ---

    def click(self, x, y, wait=2.0) -> None:
        self.controller.post_click(int(x), int(y)).wait()
        time.sleep(wait)

    def back(self) -> None:
        """按一次返回 —— **只在还站在平台页面里时才按**。

        和 `core.py` 的 `_safe_back` 同一个道理（那边有完整说明）：
        KEYCODE_BACK 不认页面。平台页面已不在前台时，这一下按的成了
        微信自己的返回 —— 从聊天列表退回桌面，看起来像「微信被关掉了」，
        而微信的 WebView 会话丢了就恢复不了。
        """
        t = self.text()
        if not exam.on_site(t):
            print("[retake] 现在不在平台页面里，不按返回键"
                  "（按了会把微信顶出去）")
            return
        self.controller.post_click_key(4).wait()   # KEYCODE_BACK
        time.sleep(3.0)

    def scroll_down(self) -> None:
        self.controller.post_swipe(SCROLL_FROM[0], SCROLL_FROM[1],
                                   SCROLL_TO[0], SCROLL_TO[1], 400).wait()
        time.sleep(1.6)

    # --- 页面判定 ---

    def on_answer_page(self) -> bool:
        t = self.region(TYPE_ROI)
        return "选择" in t or "判断" in t

    def on_result_page(self) -> bool:
        return looks_like_result_page(self.text())

    def is_judge(self) -> bool:
        t = self.region(OPTIONS_ROI)
        if "正确" in t and "错误" in t:
            return True
        return ("T." in t or "T、" in t) and ("F." in t or "F、" in t)


#: 提交成功的 toast 出现的位置（实测在屏幕中部）。
#: **不能**用整屏文本判断——题干里就有「提交」二字，
#: 会把「有一道题含提交字样」误判成「提交成功」。这个假阳性实测踩过，
#: 后果是跳过采集就宣告达标。
TOAST_ROI = (280, 620, 200, 90)

#: toast 上的成功文案（实测「提交成功」，带绿色对勾）
SUBMIT_OK_HINTS = ("提交成功", "提交完成", "交卷成功")


def do_submit(sess: ExamSession, max_tries: int = 10) -> bool:
    """点提交并确认真的交上去了。被拦就补答再试。

    判定成功的两条证据：
      1. 屏幕中部出现「提交成功」toast（只看小区域，避免题干误伤）
      2. 页面变成结果页（有「本次成绩 / 正确答案 / 您的答案」）

    返回是否确认成功。**必须确认**——不确认就去采集会白跑一整轮。
    """
    for attempt in range(1, max_tries + 1):
        sess.click(*SUBMIT_TAP, wait=2.0)

        # 早点看一次：toast 只显示约 3 秒，等太久就没了
        toast = sess.region(TOAST_ROI)
        if any(h in toast for h in SUBMIT_OK_HINTS):
            sess.log(f"[submit] ✓ 看到「提交成功」toast（第 {attempt} 次点击）")
            return True

        time.sleep(2.0)
        text = sess.text()

        if looks_like_result_page(text):
            sess.log(f"[submit] ✓ 已到结果页（第 {attempt} 次点击）")
            return True

        m = _MISSING_RE.search(text)
        if m:
            sess.log(f"[submit] 被拦：第 {m.group(1)} 未选，去补答")
            sess.click(*DIALOG_GOTO_TAP, wait=3.5)
            judge = sess.is_judge()
            lb = "T" if judge else "A"
            row = label_to_row(lb)
            if row is not None:
                sess.click(ROW_TAP_X, OPTION_ROWS[row], wait=1.5)
                sess.log(f"[submit]   补选 {lb}")
            continue

        if "未选" in text:
            sess.log("[submit] 有未选提示但读不出题号，关掉重试")
            sess.click(*DIALOG_CANCEL_TAP, wait=1.5)
            continue

        sess.log(f"[submit] 第 {attempt} 次后未见成功标志: {text[:90]}")

    sess.log("[submit] ✗ 未能确认提交成功")
    return False


def enter_exam(sess: ExamSession, max_steps: int = 8) -> bool:
    """从考核列表页进到答题页。

    「再做一次」和「开始答题」位置基本重合（实测都在右上角 y≈168），
    「进入答题」在说明页底部。
    """
    for step in range(max_steps):
        if sess.on_answer_page():
            sess.log("[enter] 已在答题页")
            return True

        text = sess.text()
        btn = find_enter_button(text)
        if btn is None:
            sess.log(f"[enter] 找不到进入按钮: {text[:120]}")
            return False

        if btn in BOTTOM_BUTTON_TEXTS:
            sess.log(f"[enter] 点底部「{btn}」")
            sess.click(*ENTER_TAP_BOTTOM, wait=5.0)
        else:
            sess.log(f"[enter] 点「{btn}」")
            sess.click(*ENTER_TAP_TOP, wait=5.0)

    sess.log("[enter] 多次尝试仍未进入答题页")
    return False


def harvest(sess: ExamSession, cache) -> int:
    """从结果页采集官方答案。返回新入库条数。"""
    from harvest_answers import harvest_answers

    return harvest_answers(
        rows_provider=sess.rows,
        cache=cache,
        scroll=sess.scroll_down,
        log=sess.log,
    )


def answer_questions(sess: ExamSession, cache, fixed: str = "") -> tuple[int, int]:
    """逐题作答。返回 (已答, 其中查题库命中数)。"""

    def resolve(stem: str):
        from quiz import Question

        if fixed:
            return [fixed]
        ans = cache.get(Question(stem=stem))
        return ans.labels if ans and ans.labels else None

    runner = ExamRunner(
        controller=sess.controller,
        read_stem=lambda: sess.region(STEM_ROI),
        resolve_answer=resolve,
        cfg=ExamConfig(guess_when_unsure=True, max_questions=100),
        log=sess.log,
        ocr=lambda img: " ".join(t for t, _x, _y in sess._recognize(img)),
        is_judge=sess.is_judge,
    )
    p = runner.run()
    return p.answered, p.answered - p.guessed


def return_to_exam_list(sess: ExamSession) -> None:
    """从结果页回到考核列表。

    实测单纯的 back 会退到课程页而不是考核列表（多退了一层），
    所以退到课程页后再走「更多 → 考核」这条已知路径。
    """
    sess.back()
    text = sess.text()

    if looks_like_exam_list(text):
        sess.log("[nav] 已回到考核列表")
        return

    # 退到课程页了 → 走「更多 → 考核」
    if "更多" in text:
        sess.log("[nav] 退到了课程页，走「更多 → 考核」回去")
        sess.click(600, 506, wait=3.0)      # 更多 tab
        sess.click(121, 555, wait=5.0)      # 考核图标
        return

    sess.log(f"[nav] 当前页面不像考核列表: {text[:110]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只读分数与判定，不操作")
    parser.add_argument("--pass-score", type=int, default=DEFAULT_PASS_SCORE,
                        help=f"达标分数，低于它就重做（默认 {DEFAULT_PASS_SCORE}）")
    parser.add_argument("--max-attempts", type=int, default=6,
                        help="最多考几次（重做次数会被平台记录，别给太大）")
    parser.add_argument("--answer", default="", help="每题固定选这个字母（默认查题库）")
    args = parser.parse_args()

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

    # **必须注册自定义模块**。不注册的话管线里的
    # `custom_recognition: QuizAnswer` / `custom_action: AnswerQuestion`
    # 找不到实现，框架只报 "recognition is null"，很难联想到是注册缺失。
    # （实测踩过：run_full_exam.py 也漏了这一步，整卷答不出题。）
    from main import register_custom_modules

    registered = register_custom_modules(resource)
    print(f"已注册自定义模块: {registered}")

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] bind 失败", file=sys.stderr)
        return 1

    sess = ExamSession(controller, tasker)
    cache = quiz.AnswerCache(paths.answer_cache_path())

    text = sess.text()
    score = parse_score(text)
    need, why = should_retake(score, args.pass_score)

    print("=" * 62)
    print(" 考核总控")
    print("=" * 62)
    print(f"当前成绩: {score.describe()}")
    print(f"题库    : {len(cache)} 条")
    print(f"页面    : {'结果页' if looks_like_result_page(text) else '考核列表页' if looks_like_exam_list(text) else '其它'}")
    print(f"判定    : {why}")

    if args.dry_run:
        print("\n[dry-run] 不执行任何操作")
        return 0

    if not need:
        print("\n无需重做，结束。")
        return 0

    if not (looks_like_result_page(text) or looks_like_exam_list(text)):
        print("\n[FATAL] 不在考核相关页面。")
        return 1

    for attempt in range(1, args.max_attempts + 1):
        print()
        print("=" * 62)
        print(f" 第 {attempt} 次 / 最多 {args.max_attempts} 次")
        print("=" * 62)

        # 只要当前在结果页就先退回考核列表——**第 1 轮也要做**。
        # （早先只在 attempt > 1 时退，导致从结果页启动会直接找不到入口。）
        if sess.on_result_page():
            return_to_exam_list(sess)
        elif attempt > 1:
            # 既不在结果页也不是第 1 轮，说明上轮停在奇怪的地方，退一步试试
            sess.back()

        if not enter_exam(sess):
            print("[retake] 进不了答题页，中止")
            return 1

        answered, from_cache = answer_questions(sess, cache, args.answer)
        print(f"\n[retake] 已答 {answered} 题（题库命中 {from_cache}，"
              f"其余随机猜）")

        if not do_submit(sess):
            print("[retake] 提交没确认成功，中止（本轮不发采集）")
            return 1

        time.sleep(3)
        before = len(cache)
        harvested = harvest(sess, cache)
        gained = len(cache) - before
        print(f"[retake] 采集: 本轮可入库 {harvested}，"
              f"题库 {before} → {len(cache)}（新增 {gained}）")

        time.sleep(2)
        after = parse_score(sess.text())
        print(f"[retake] 本轮成绩: {after.describe()}")

        need, why = should_retake(after, args.pass_score)
        print(f"[retake] 判定: {why}")
        if not need:
            print()
            print("=" * 62)
            print(f" ✓ 已达标: {after.describe()}   题库 {len(cache)} 条")
            print("=" * 62)
            return 0

        # 本轮没有新收获 → 再重做也只是白增重做次数
        if gained == 0:
            print()
            print("[retake] 本轮没采到新题，再重做也不会更好，停止。")
            print(f"         当前 {after.describe()}，题库 {len(cache)} 条。")
            print("         题目是随机抽取的，可以过一阵再跑一次继续累积题库。")
            return 1

        print(f"[retake] 本轮有新收获（+{gained} 题），题库变强，继续下一轮")

    print()
    print(f"[retake] 已达最大尝试次数（{args.max_attempts}）。")
    print(f"         当前 {parse_score(sess.text()).describe()}，"
          f"题库 {len(cache)} 条。可再跑一次继续累积。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
