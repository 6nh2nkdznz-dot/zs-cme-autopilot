"""主程序：把 MaaFramework 的控制器、资源、任务串起来。

与官方「Agent 进程」方案的区别：
  Agent 方案要求通用 UI 提供一个 sock_id 来配对；我们要做的是自带交互的
  命令行脚本（需要「停下来等人确认」这种能力），所以在**同进程内**用
  Resource.register_custom_recognition / register_custom_action 注册自定义模块。
  能力等价，调试更直接。

用法:
    python main.py --check          # 只检查配置+连接+资源加载
    python main.py --ocr            # 全屏 OCR 当前画面（用来对照页面写节点）
    python main.py --ocr --roi 0,600,1280,120
    python main.py --task <节点名>   # 跑一个 Pipeline 入口
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 控制台默认 GBK，OCR 出来的中文/符号会直接抛 UnicodeEncodeError。
# 所有脚本入口都强制 UTF-8，否则中文识别结果根本打不出来。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from controller import ConfigError, build_controller, load_config  # noqa: E402
from progress import parse_progress  # noqa: E402
from quiz import AnswerCache, Option, Question, classify  # noqa: E402
from quiz import resolve as resolve_quiz  # noqa: E402

import paths  # noqa: E402
from course import PLAYER_DUR_ROI, PLAYER_TIME_ROI  # noqa: E402
from exam import (  # noqa: E402
    PAGE_ANSWER,
    PAGE_EXAM_ENTRY,
    PAGE_RESULT,
    detect_page,
    label_to_row,
    looks_like_answer_page,
)

# 注意：不要用 Path(__file__) 推算路径。打包后 __file__ 指向 _MEIPASS，
# 而用户数据要写到 exe 旁边。统一走 paths 模块，它区分只读资源与可写数据。
ROOT = paths.app_root()
RESOURCE_DIR = paths.resource_dir()


# --------------------------------------------------------------------------
# 自定义识别 / 动作（进程内注册）
# --------------------------------------------------------------------------

def register_custom_modules(resource) -> list[str]:
    """把自定义识别器和动作注册到 Resource。返回已注册的名字列表。

    注意：这里刻意做得「防御性」——注册失败不抛异常，只报告，
    因为缺少自定义模块时基础点击流程仍然可用。
    """
    from maa.custom_recognition import CustomRecognition
    from maa.custom_action import CustomAction

    registered: list[str] = []

    class VideoProgress(CustomRecognition):
        """读取播放器时间区域并解析进度。

        param(JSON): {"time_roi": [x, y, w, h]}
        实测该平台播放器左下角显示「43:49」，右侧显示总长「45:00」，
        所以 time_roi 只需覆盖左下角当前时间即可（总长从目录列表拿）。
        detail 带 percent / position / duration / finished 供后续判断。
        """

        def analyze(self, context, argv):
            try:
                param = json.loads(argv.custom_recognition_param or "{}")
            except json.JSONDecodeError:
                param = {}

            roi = param.get("time_roi")
            img = argv.image
            if roi:
                x, y, w, h = roi
                img = img[y:y + h, x:x + w]
                offset = (x, y)
            else:
                offset = (0, 0)

            text = _ocr_image(context, img)
            # 时间区通常只有 "43:49"，补一个总长占位让解析器能算比例；
            # 真正判定是否学完由 WatchVideo 用目录里的总时长负责。
            reading = parse_progress(text)
            if reading is None:
                return None

            h_img, w_img = img.shape[:2]
            box = (offset[0], offset[1], w_img, h_img)
            return CustomRecognition.AnalyzeResult(
                box=box,
                detail={
                    "percent": reading.percent,
                    "position": reading.position,
                    "duration": reading.duration,
                    "finished": reading.finished,
                    "text": reading.raw_text,
                },
            )

    class QuizAnswer(CustomRecognition):
        """识别一道题并决定答案。

        param(JSON):
          {
            "stem_roi": [x,y,w,h],
            "option_rois": [[x,y,w,h], ...],   # 按 A/B/C/D 顺序
            "qtype": "single" | "multi",
            "allow_web": true
          }

        detail 里带 labels / confidence / needs_human / source。
        置信度不足时返回 None，让 Pipeline 走到「转人工」分支。
        """

        def analyze(self, context, argv):
            param_json = argv.custom_recognition_param or "{}"
            try:
                param = json.loads(param_json)
            except json.JSONDecodeError:
                return None

            stem_roi = param.get("stem_roi")
            option_rois = param.get("option_rois") or []
            allow_web = bool(param.get("allow_web", True))
            # 没把握时先随便选，交卷后从结果页采正确答案。
            # 平台答错不扣分、可重复提交，所以这比挂起等人快得多
            # （挂起要求全程有人盯着，跑不动整卷）。
            guess = bool(param.get("guess_when_unsure", True))

            if not stem_roi or not option_rois:
                print("[quiz] 未配置 stem_roi / option_rois，请先用 --ocr 确定坐标")
                return None

            # 先确认在答题页再动手。
            # 实测踩过的坑：交卷成功后页面已跳到结果页，但脚本以为提交失败、
            # 继续「答题」——把「本次成绩：45分」当题干、「最高成绩：60分」当选项，
            # 而且**不报错**，一路点到超时。所以这里必须挡掉。
            page = _ocr_image(context, argv.image)
            if not looks_like_answer_page(page):
                _probe(f"[quiz] 当前不是答题页，不动作: {page[:80]}")
                return None

            img = argv.image
            stem = _ocr_image(context, _crop(img, stem_roi))

            # 选项的**标签要从 OCR 文本里读**，不能按位置硬编码 A/B/C/D——
            # 判断题的选项是 T/F（实测「F.错误」「T.正确」），
            # 硬编码会让判断题答案变成 ['A']，指向错误的那一行。
            #
            # 读不到正文的行要**丢掉**。判断题只有 2 个选项，第 3、4 个 ROI
            # 必然是空的；留着会带来两个问题：
            #   * classify() 看到「4 个选项」就不判 judge（实测踩过：
            #     判断题被判成 single，答案从 T 变成 A）
            #   * 空正文参与 match_labels_by_text，污染按正文的匹配
            options = []
            for i, r in enumerate(option_rois):
                raw = _ocr_image(context, _crop(img, r))
                fallback = chr(ord("A") + i)
                label, body = _split_option_label(raw, fallback)
                _probe(f"[quiz] 选项 {i} roi={r} raw={raw!r} -> "
                       f"label={label!r} body={body!r}")
                if not body:
                    _probe(f"[quiz]   选项 {i} 是空的，丢弃")
                    continue
                options.append(Option(label=label, text=body))
            _probe(f"[quiz] 题干 raw={stem!r}  有效选项 {len(options)} 个")

            # 题型不靠配置写死，从题干和选项自动判——配置写死容易和实际不符
            qtype = param.get("qtype") or classify(stem, options)
            q = Question(stem=stem, options=options, qtype=qtype)
            if not q.normalized_stem():
                return None

            cfg = load_config()
            quiz_cfg = cfg.get("quiz", {})
            cache = AnswerCache(paths.answer_cache_path())

            # 这一题本轮已经挂起过了吗？框架会重试未命中的节点，
            # 不去重的话同一题会被反复挂起、反复联网（实测重复 3 次）。
            #
            # 但**不能静默返回 None** —— 那样框架一直重试到节点超时，
            # 题目始终没被作答，整卷做不完（实测踩过：第 12 题之后全部卡住）。
            # 所以第一次返回 None（挂起），后续重试返回**猜测值**让流程继续。
            key = q.normalized_stem()
            if key in _SUSPENDED_STEMS:
                retry = _first_guess(q)
                if retry:
                    _probe(f"[quiz] 该题已挂起过，重试改为直接选 "
                           f"{''.join(retry)}: {stem[:30]}")
                    x, y, w, h = stem_roi
                    return CustomRecognition.AnalyzeResult(
                        box=(x, y, w, h),
                        detail={"labels": retry, "confidence": 0.0,
                                "source": "guess", "qtype": qtype,
                                "option_labels": [o.label for o in options],
                                "votes": {}, "needs_human": False},
                    )
                _probe(f"[quiz] 该题已挂起过且无法猜测，跳过重试: {stem[:30]}")
                return None

            # resolve() 内部会在需要人工时自动挂起到 debug/pending/questions.jsonl，
            # 所以这里不用再单独落盘题干。
            ans, needs_human = resolve_quiz(
                q,
                cache,
                paths.debug_dir(),
                min_confidence=float(quiz_cfg.get("min_confidence", 0.75)),
                allow_web=allow_web,
                guess_when_unsure=guess,
            )

            if needs_human:
                _SUSPENDED_STEMS.add(key)
                print(f"[quiz] 已挂起到 {paths.pending_dir() / 'questions.jsonl'}")
                print("[quiz]   用 `python scripts\\pending.py list` 作答，答完入库后同题永久命中")
                return None

            x, y, w, h = stem_roi
            return CustomRecognition.AnalyzeResult(
                box=(x, y, w, h),
                detail={
                    "labels": ans.labels,
                    "confidence": ans.confidence,
                    "source": ans.source,
                    "qtype": qtype,
                    # 选项在**页面上的实际顺序**。动作节点按它查行号——
                    # 判断题实测是 F 在前、T 在后，写死顺序会点错行。
                    "option_labels": [o.label for o in options],
                    "votes": ans.votes,
                    "needs_human": False,
                },
            )

    class AnswerQuestion(CustomAction):
        """按 QuizAnswer 给出的 labels 点选项行，然后翻到下一题。

        param(JSON):
          {
            "option_rows": [268, 322, 377, 432],   # A/B/C/D 行的 y（画布坐标）
            "row_tap_x": 300,                       # 行内点击的 x
            "next_tap": [600, 1247],                # 「下一题」
            "submit_tap": [694, 101],               # 右上角「提交」
            "max_questions": 100,
            "judge_rows": {"T": 0, "F": 1}          # 判断题只有两行时的映射
          }

        ## 怎么知道该继续下一题还是交卷

        靠**题干重复**判定：每题点完「下一题」后重新读一次题干，
        如果题干和上一题相同，说明已经在最后一题、翻不动了 → 交卷。
        这比数题号稳，因为平台会把题目打乱、题号也不连续。

        ## 为什么连续两次相同才停

        点完「下一题」到新题渲染出来有延迟，可能读到还没刷新的旧题干。
        所以要求**连续两次**读到相同题干才认定到底了，避免误交卷。

        状态挂在 context 上（按 tasker 复用），跨节点调用保持连续。
        """

        def run(self, context, argv):
            try:
                param = json.loads(argv.custom_action_param or "{}")
            except json.JSONDecodeError:
                print("[answer] custom_action_param 不是合法 JSON")
                return False

            rows = param.get("option_rows") or [268, 322, 377, 432]
            row_tap_x = int(param.get("row_tap_x", 300))
            next_tap = param.get("next_tap") or [600, 1247]
            submit_tap = param.get("submit_tap") or [694, 101]
            judge_rows = param.get("judge_rows") or {"T": 0, "F": 1}
            max_q = int(param.get("max_questions", 100))

            # --- 取出识别阶段给出的答案 ---
            #
            # 踩过的坑（两个，都实测确认）：
            #
            # 1) argv.reco_detail 是**当前节点自己的**识别结果。
            #    所以识别和动作必须在**同一个节点**上（pipeline 里已合并）；
            #    拆成两个节点的话，动作节点拿到的是它自己 DirectHit 的 detail，
            #    看不到 QuizAnswer 给的 labels。
            #
            # 2) RecognitionDetail 没有 `detail` 字段。自定义识别的结果走
            #    detail_json 序列化后落在 **raw_detail**，而且外面还包了一层：
            #
            #        raw_detail = {
            #          "all":  [ {"box": [...], "detail": {"labels": [...]}}, ... ],
            #          "best": {"box": [...], "detail": {"labels": [...]}}
            #        }
            #
            # 注意：get_recognition_detail 只是**读已存结果**，同步查询不排队，
            # 回调里调用不会死锁（死锁只发生在 post_recognition().wait()）。
            labels: list[str] = []
            qtype = "single"

            rec = getattr(argv, "reco_detail", None)

            def _pick_detail(obj) -> dict | None:
                """从任意嵌套层级里挖出带 labels 的那个 detail 字典。

                结构不固定（raw_detail.all[].detail / raw_detail.best.detail /
                也可能直接就是 detail 本身），所以递归找而不是写死路径。
                """
                if isinstance(obj, str):
                    try:
                        obj = json.loads(obj)
                    except json.JSONDecodeError:
                        return None
                if isinstance(obj, list):
                    for item in obj:
                        got = _pick_detail(item)
                        if got:
                            return got
                    return None
                if not isinstance(obj, dict):
                    return None
                if obj.get("labels"):          # 本层就是答案
                    return obj
                for key in ("detail", "all", "best", "filtered", "results"):
                    if key in obj:
                        got = _pick_detail(obj[key])
                        if got:
                            return got
                return None

            found = None
            if rec is not None:
                found = (_pick_detail(getattr(rec, "raw_detail", None))
                         or _pick_detail(getattr(rec, "best_result", None))
                         or _pick_detail(getattr(rec, "all_results", None)))

            if found:
                labels = list(found.get("labels") or [])
                qtype = found.get("qtype") or "single"

            if not labels:
                print("[answer] 没拿到 labels，跳过本题")
                if rec is not None:
                    print(f"[answer]   algorithm={getattr(rec, 'algorithm', None)}"
                          f" hit={getattr(rec, 'hit', None)}")
                    print(f"[answer]   raw_detail="
                          f"{getattr(rec, 'raw_detail', None)!r}"[:300])
                return False

            if not labels:
                print("[answer] 没拿到 labels，跳过本题")
                if rec is not None:
                    print(f"[answer]   algorithm={getattr(rec, 'algorithm', None)}"
                          f" hit={getattr(rec, 'hit', None)}")
                    print(f"[answer]   raw_detail={getattr(rec, 'raw_detail', None)!r}"[:300])
                return False

            controller = context.tasker.controller

            # --- 状态：跨节点调用保持 ---
            state = getattr(context, "_exam_state", None)
            if state is None:
                state = {"answered": 0, "last_stem": "", "same_count": 0,
                         "finished": False}
                try:
                    context._exam_state = state
                except AttributeError:
                    pass

            if state["finished"]:
                return True

            # --- 先读当前题干，用于「是否翻到底」判定 ---
            stem_now = self._read_stem(context, controller, param)

            if stem_now and stem_now == state["last_stem"]:
                state["same_count"] += 1
                if state["same_count"] >= 2:
                    print(f"[answer] 题干连续 {state['same_count']} 次未变，"
                          f"判定已到最后一题，交卷")
                    self._click(controller, *submit_tap)
                    state["finished"] = True
                    return True
            else:
                state["same_count"] = 0

            # --- 点选项 ---
            #
            # 行号怎么算：**按识别阶段读到的选项顺序**，不是按字母大小。
            # 实测判断题的选项顺序是 F 在前、T 在后：
            #     y=253  F.错误
            #     y=305  T.正确
            # 所以写死 "T 在第 0 行" 会点错行。识别阶段把顺序放进
            # detail["option_labels"]，这里查表得行号。
            option_labels = [str(x).upper() for x in (found.get("option_labels") or [])] \
                if found else []

            def row_of(lb: str) -> int | None:
                up = str(lb).upper()
                if option_labels:
                    try:
                        return option_labels.index(up)
                    except ValueError:
                        return None
                if qtype == "judge":
                    return judge_rows.get(up)
                return label_to_row(up)

            clicked = 0
            for lb in labels:
                row_idx = row_of(lb)
                if row_idx is None or row_idx >= len(rows):
                    print(f"[answer] 标签 {lb} 无法定位行（顺序={option_labels}），跳过")
                    continue
                self._click(controller, row_tap_x, int(rows[row_idx]), wait=1.0)
                clicked += 1

            if clicked == 0:
                print(f"[answer] labels={labels} 无法映射到行，跳过")
                return False

            state["answered"] += 1
            state["last_stem"] = stem_now
            print(f"[answer] 第 {state['answered']} 题 → 选 {''.join(labels)}"
                  f"（{qtype}）")

            if state["answered"] >= max_q:
                print(f"[answer] 已达 max_questions={max_q}，交卷")
                self._click(controller, *submit_tap)
                state["finished"] = True
                return True

            # --- 翻下一题 ---
            self._click(controller, next_tap[0], next_tap[1], wait=2.0)
            return True

        # --- 小工具 ---

        @staticmethod
        def _click(controller, x, y, wait: float = 0.8) -> None:
            import time

            controller.post_click(int(x), int(y)).wait()
            time.sleep(wait)

        @staticmethod
        def _read_stem(context, controller, param: dict) -> str:
            """读当前题干（用于翻页判重）。读不到就返回空串。"""
            roi = param.get("stem_roi")
            if not roi:
                return ""
            job = controller.post_screencap().wait()
            if not job.succeeded:
                return ""
            img = job.get()
            x, y, w, h = (int(v) for v in roi)
            h_img, w_img = img.shape[:2]
            x2, y2 = min(x + w, w_img), min(y + h, h_img)
            if x >= x2 or y >= y2:
                return ""
            return _ocr_image(context, img[y:y2, x:x2]).strip()


    # ---- 单课看护 / 整门课轮播：共用的构件 ----

    def _make_player_helpers(context, param: dict):
        """构造「读进度 / 处理弹题」两个闭包，供单课与整门课两处复用。

        抽出来是因为这两个逻辑都要在「看护一个视频」的循环里用，
        写两份必然会漂移。
        """
        import time

        controller = context.tasker.controller

        tap = param.get("control_tap") or [360, 200]
        roi = param.get("time_roi") or list(PLAYER_TIME_ROI)
        dur_roi = param.get("dur_roi") or list(PLAYER_DUR_ROI)
        answer_tap = param.get("answer_a_tap") or [340, 300]
        submit_tap = param.get("submit_tap") or [359, 1249]

        def screen_text() -> str:
            job = controller.post_screencap().wait()
            if not job.succeeded:
                return ""
            return _ocr_image(context, job.get())

        def ocr_rows() -> list[tuple[str, int, int]]:
            """整屏 OCR，带绝对坐标。用于目录枚举。

            走共用的 _ocr_rows —— 它内部按「是否在回调里」选正确路径，
            不会自死锁（这个函数原来是手写 tasker.post_recognition().wait()，
            在 WatchCourse 回调里会挂死）。
            """
            job = controller.post_screencap().wait()
            if not job.succeeded:
                return []
            return _ocr_rows(context, job.get())

        def read_progress() -> str:
            """唤起控件并立即截图，OCR 左下角当前时间 + 右下角总时长。

            实测：控件只显示约 3 秒就自动隐藏，所以「点击 → 等 0.45s 淡入
            → 截图」必须在同一口气内完成。
            """
            controller.post_click(int(tap[0]), int(tap[1])).wait()
            time.sleep(0.45)  # 控件淡入
            job = controller.post_screencap().wait()
            if not job.succeeded:
                return ""
            img = job.get()

            x, y, w, h = roi
            cur = _ocr_image(context, img[y:y + h, x:x + w])
            dx, dy, dw, dh = dur_roi
            dur = _ocr_image(context, img[dy:dy + dh, dx:dx + dw])
            return f"{cur} / {dur}"

        def handle_popup() -> bool:
            """检测并处理视频弹题。返回 True 表示确实处理了弹题。

            ## 实测到的两类弹题

            **1) 学习状态检测题** —— 题干自带答案：

                是否继续当前视频学习(此题为学习状态检测，如需继续学习，请选择A)

            流程：点 A → 点底部「提交」→ 按钮文案变成「继续学习」
            → 再点一次才真正回到视频。

            **2) 评分简答题** —— 实测在 00:40:41 处弹出：

                1、请您为老师此堂讲课总体效果打分，满分100分(…)
                请输入 50-100 的数值
                （简答题）

            这类题**不填就永远卡住视频**（弹题期间视频暂停），而它并非知识考核，
            是给老师讲课打分，所以可以安全地填一个区间内的值。
            **区间从文案里解析**，不写死——不同课程下限不同，填到区间外弹题不关。

            ## 安全闸

            知识考核题（课后考试里的）绝不在这里猜。判断依据是：只有
            「题干自带答案」或「评分区间」这两种可确定作答的形态才动手，
            其余一律存证转人工。答题有及格线，猜错会把课挂掉。
            """
            text = screen_text()

            # --- 类型 0：先清掉遮挡浮层 ---
            #
            # 「每日签到」浮层会盖在弹题上面，导致后面所有点击都打在浮层上
            # （点输入框点到浮层、点提交点到浮层的 X）。实测它反复弹出，
            # 所以每轮处理弹题前都先检查一次。
            if "每日签到" in text:
                print("[watch] 有签到浮层遮挡，先处理掉")
                # 优先点「立即签到」而不是 X：既清掉遮挡，又顺手把签到做了。
                # 实测按钮中心在 (360,764)，X 在 (521,613)。
                controller.post_click(360, 764).wait()
                time.sleep(2.5)
                text = screen_text()
                if "每日签到" in text:
                    print("[watch] 「立即签到」没关掉浮层，改点右上角 X")
                    controller.post_click(521, 613).wait()
                    time.sleep(2.0)
                    text = screen_text()
                    if "每日签到" in text:
                        print("[watch] 签到浮层仍关不掉，放弃本轮弹题处理")
                        return False

            if "视频弹题" not in text:
                return False

            # --- 类型 2：评分简答题（先判它，因为它会卡住视频）---
            rng = _parse_numeric_range(text)
            if rng is not None:
                lo, hi = rng
                # 取区间中位偏上：既在有效范围内，又是个像样的分数
                val = lo + (hi - lo) * 2 // 3

                # 输入框**已经有值**就不要再输入。
                #
                # `adb shell input text` 是**追加**到光标处而不是替换，重复输入
                # 会变成「8383」这种非法值，弹题反而关不掉。实测踩过：
                # 手工测试填了 83，正式跑时再填一次就成了 8383。
                #
                # 判断依据：占位提示「请输入 N-M 的数值」**只在框为空时显示**，
                # 它不在了就说明已有内容。
                already = ""
                if "请输入" not in text and "的数值" not in text:
                    for m in re.finditer(r"\b(\d{2,3})\b", text):
                        if lo <= int(m.group(1)) <= hi:
                            already = m.group(1)
                            break

                if already:
                    print(f"[watch] 评分题输入框已有值 {already}，跳过输入直接提交")
                else:
                    print(f"[watch] 检测到评分简答题，填 {val}（区间 {lo}-{hi}）")
                    # ⚠️ 文本输入**必须用 adb shell input text**，
                    # MaaFramework 的 post_input_text 在这个 WebView 里**无效**
                    # （实测：调用成功返回、但输入框仍是占位符）。
                    # 这与本 WebView 的既有规律一致但方向相反——
                    # 点击要用 MaaTouch，文本输入反而要用 adb。
                    controller.post_click(int(answer_tap[0]),
                                          int(answer_tap[1])).wait()
                    time.sleep(1.0)
                    if not _adb_input_text(context, str(val)):
                        print("[watch] 文本输入失败，存证转人工")
                        _dump_pending_popup(text, context)
                        return False
                    time.sleep(1.5)

                # 提交 → 按钮常变成「继续学习」→ 需要再点一次才真正关闭。
                # 实测：第一次点后底部文案从「提交」变「继续学习」，
                # 第二次点才关掉弹题、视频恢复。
                controller.post_click(int(submit_tap[0]), int(submit_tap[1])).wait()
                time.sleep(2.5)
                if "视频弹题" in screen_text():
                    print("[watch] 弹题仍在，再点一次底部按钮（提交→继续学习）")
                    controller.post_click(int(submit_tap[0]), int(submit_tap[1])).wait()
                    time.sleep(2.5)
                    if "视频弹题" in screen_text():
                        print("[watch] 评分题两次点击仍未关闭，存证转人工")
                        _dump_pending_popup(screen_text(), context)
                        return False
                print("[watch] 评分题已处理")
                return True

            # --- 类型 1：学习状态检测题 ---
            if not _looks_like_state_check(text):
                print("[watch] 弹题既非状态检测题也解析不出评分区间，转人工存证")
                _dump_pending_popup(text, context)
                return False

            print("[watch] 检测到学习状态检测题，选 A 并提交")
            controller.post_click(int(answer_tap[0]), int(answer_tap[1])).wait()
            time.sleep(1.2)
            controller.post_click(int(submit_tap[0]), int(submit_tap[1])).wait()
            time.sleep(2.0)

            text2 = screen_text()
            if "视频弹题" in text2:
                print("[watch] 弹题仍在，再点一次底部按钮（提交→继续学习）")
                controller.post_click(int(submit_tap[0]), int(submit_tap[1])).wait()
                time.sleep(2.0)
                text3 = screen_text()
                if "视频弹题" in text3:
                    print("[watch] 两次点击仍未关闭，存证转人工")
                    _dump_pending_popup(text3, context)
                    return False
            print("[watch] 弹题已处理")
            return True

        return {
            "controller": controller,
            "read_progress": read_progress,
            "handle_popup": handle_popup,
            "ocr_rows": ocr_rows,
            # _enter_exam 要用它做页面判定（确认真的到了考核页，
            # 而不是只看动作有没有发出去）
            "screen_text": screen_text,
        }

    def _watch_seconds(param: dict, lesson=None) -> float:
        """决定单个视频的看护上限。

        有课时按「视频时长 + 余量」，避免时长解析错时无限等；
        没课时用固定的上限。
        """
        from course import CourseConfig

        ccfg = CourseConfig()
        if lesson is not None and lesson.seconds > 0:
            return min(lesson.seconds + ccfg.lesson_slack_seconds,
                       ccfg.lesson_max_seconds)
        return float(param.get("max_watch_seconds", 2 * 3600))

    class WatchVideo(CustomAction):
        """看护**当前正在播放的**视频直到学完（不切课）。

        适合「我手动点开了一课，你帮我挂着看完」这种用法。
        要整门课轮播请用 WatchCourse。

        参数(JSON): control_tap / time_roi / dur_roi / target_percent /
                    poll_seconds / max_watch_seconds / answer_a_tap / submit_tap
        """

        def run(self, context, argv):
            from progress import VideoWatcher, WatchConfig

            try:
                param = json.loads(argv.custom_action_param or "{}")
            except json.JSONDecodeError:
                param = {}

            target = float(param.get("target_percent", 97.0))
            h = _make_player_helpers(context, param)

            watcher = VideoWatcher(
                read_progress=h["read_progress"],
                handle_popup=h["handle_popup"],
                cfg=WatchConfig(
                    target_percent=target,
                    poll_seconds=float(param.get("poll_seconds", 20)),
                    max_watch_seconds=_watch_seconds(param),
                    # 同 WatchCourse：靠整屏文本找「播完」的直接证据
                    read_screen=h["screen_text"],
                ),
            )
            reading = watcher.watch()
            # 判定要看 finished 标记，不能只比百分比 ——
            # 视频可能不循环、停在最后一帧，那时只有文案/按钮判据能认出来。
            return reading is not None and (
                reading.finished or reading.percent >= target)

    class WatchCourse(CustomAction):
        """整门课轮播：枚举目录 → 逐个播放 → 自动切下一节。

        这是「自动点击下一节课」的主体。合并了原来的继续学习逻辑：
        每个视频内部仍然是「读进度 + 处理弹题」，只是外面套了一层
        「看完就点下一节」的循环。

        参数(JSON):
          max_lessons        : 最多播几个（0=不限）
          course_max_seconds : 整门课兜底上限
          lesson_slack_seconds : 每课看护余量
          target_percent     : 单课目标百分比
          以及 _make_player_helpers 认的那些（control_tap / time_roi / ...）
        """

        def run(self, context, argv):
            from course import CourseConfig, CourseRunner
            from progress import VideoWatcher, WatchConfig

            try:
                param = json.loads(argv.custom_action_param or "{}")
            except json.JSONDecodeError:
                param = {}

            target = float(param.get("target_percent", 97.0))
            h = _make_player_helpers(context, param)

            # 本地进度：目录里没有「已完成」标记，所以「哪节看过了」只能自己记。
            # 整门课好几小时，中途重启/回主页重来时靠它跳过已看过的课节。
            from course_progress import CourseProgress

            prog = CourseProgress(paths.data_dir() / "course_progress.json")
            skip_done = bool(param.get("skip_locally_done", True))
            # 课程名优先用调用方传进来的（`course_name`）。
            #
            # 为什么不能只在页面读：实测**看护视频时页面顶部没有课程名**
            # （只有「课程学习」和站点域名），课程名只在「我的学习」列表的
            # 卡片上出现。所以进入课程之后就再也读不到了——第一节看完了、
            # course_progress.json 却没生成，就是这个原因。
            course_name = (param.get("course_name") or "").strip() \
                or _course_name_from(h["ocr_rows"]())
            if course_name:
                print(f"[course] 课程名: {course_name}"
                      f"（本地已记录 {prog.done_count(course_name)} 节）")
            else:
                print("[course] ⚠ 拿不到课程名，本节不会记入本地进度"
                      "（不影响看课，只是下次可能重看）")

            ccfg = CourseConfig(
                target_percent=target,
                poll_seconds=float(param.get("poll_seconds", 20)),
                max_lessons=int(param.get("max_lessons", 0)),
                course_max_seconds=float(
                    param.get("course_max_seconds", 12 * 3600)),
                lesson_slack_seconds=float(
                    param.get("lesson_slack_seconds", 15 * 60)),
                settle_seconds=float(param.get("settle_seconds", 6)),
            )

            def watch_one(lesson) -> bool:
                """看护刚点开的这一课。"""
                print(f"[course] 开始看护 {lesson.short(40)} "
                      f"(时长 {lesson.duration})")
                watcher = VideoWatcher(
                    read_progress=h["read_progress"],
                    handle_popup=h["handle_popup"],
                    cfg=WatchConfig(
                        target_percent=target,
                        poll_seconds=ccfg.poll_seconds,
                        max_watch_seconds=_watch_seconds(param, lesson),
                        # 把目录里的时长传进去，用来识别贴片广告。
                        # 实测踩过：广告也走到 100%，不校验就会把 60 分钟的正片
                        # 误标成「已学完」并永久跳过（静默错误，最难发现）。
                        expect_duration=lesson.duration,
                        # 整屏文本：用来找「播完」的直接证据
                        # （结尾致谢文案 / 重新播放按钮）。
                        # 视频不一定循环、也可能停在最后一帧，那时只看位置和
                        # 百分比判不出来，一节 45~60 分钟的课会白看。
                        read_screen=h["screen_text"],
                    ),
                )
                reading = watcher.watch()
                # 判定要看 watcher 给的 finished 标记，而不是只比百分比阈值。
                #
                # 为什么：视频播到末尾会**循环回开头**，最高只到 98.8%；
                # 「位置 ≈ 总时长」和「播完一轮」这两种完结信号是更可靠的证据，
                # watcher 会把它们统一标成 finished。只看百分比会把这些
                # 明明播完的课判成未达标，60 分钟白看。
                ok = reading is not None and (
                    reading.finished or reading.percent >= target)

                if course_name:
                    if ok:
                        prog.mark_done(course_name, lesson.title, lesson.duration)
                        print(f"[course] 已记入本地进度: {lesson.short(30)}")
                    elif prog.is_done(course_name, lesson.title, lesson.duration):
                        # 本地说看过、实际拉进度发现没达标 —— 记录是错的，撤掉，
                        # 否则下次还会跳过它。
                        prog.unmark(course_name, lesson.title, lesson.duration)
                        print(f"[course] 本地记录与实测不符，已撤销: "
                              f"{lesson.short(30)}")
                return ok

            runner = CourseRunner(
                controller=h["controller"],
                ocr_full=h["ocr_rows"],
                watch_one=watch_one,
                cfg=ccfg,
                log=print,
                is_done=(lambda l: skip_done and bool(course_name)
                         and prog.is_done(course_name, l.title, l.duration)),
            )
            info = runner.run()

            # 全部学完 → 自动接着进考核。这是「学完就考」的衔接点。
            # 注意考核入口在「更多」tab 里，而进考核前要先切回课程页顶部。
            if info.get("all_complete"):
                print("")
                print("[course] 本门课已全部学完，自动进入考核")
                if self._enter_exam(context, h, param):
                    print("[course] 考核流程已启动")
                else:
                    print("[course] 进入考核失败（见上面的日志）")
            else:
                print("")
                print(f"[course] 仍有 {info['failed']} 个视频未达标，"
                      f"暂不进入考核——平台要求先学完才能考。")

            # 只要有任何一个视频没达标就报失败，让上层能感知
            return info["total"] > 0 and info["failed"] == 0

        def _enter_exam(self, context, h, param: dict) -> bool:
            """学完后进入考核。委托给管线的 `进入考核` 节点。

            ## 为什么改成委托

            这里原本是一段**硬编码坐标**的实现（「更多」tab 点 `(600,506)`、
            考核图标点 `(121,555)`）。而 `20_exam.json` 里已经用 **[Anchor] 锚点**
            重写过同一段导航——于是项目里同时存在两套实现、两套机制。

            硬编码那套正是本项目反复点空的原因（实测「更多」tab 在不同布局下
            位于 y≈497 或 y≈942，差 445px，写死坐标必然失效）。

            ## 为什么用 context.run_task 而不是 post_task

            `post_task` 是把任务**排进 tasker 自己的队列**，而回调就运行在
            那个队列的线程上 —— 会自死锁（这个坑在 `_ocr_image` 里踩过，
            表现为任务挂住几分钟、无日志）。
            `context.run_task` 是**同步**执行，不排队，回调里用它是安全的。

            返回是否到达考核页（用页面识别层判定，不靠猜）。
            """
            print("[exam] 学完了 → 走管线「进入考核」（锚点导航）")
            try:
                detail = context.run_task("进入考核")
            except Exception as exc:  # noqa: BLE001 - 进考核失败不该让整门课崩
                print(f"[exam] 调「进入考核」异常: {type(exc).__name__}: {exc}")
                return False

            # 用页面识别层确认真的到了，而不是只看动作有没有发出去
            ok = self._on_exam_page(context, h)
            if ok:
                print("[exam] ✓ 已到达考核页")
            else:
                print("[exam] ✗ 仍未在考核页（见上面的节点日志）")
            return ok

        def _on_exam_page(self, context, h) -> bool:
            """当前是否在考核列表页 / 说明页 / 答题页之一。"""
            try:
                text = h["screen_text"]()
            except Exception as exc:  # noqa: BLE001
                print(f"[exam] 读页面失败: {exc}")
                return False
            page = detect_page(text)
            return page in (PAGE_EXAM_ENTRY, PAGE_ANSWER, PAGE_RESULT)

    class LogProgress(CustomAction):
        """把当前进度写进日志，不做任何点击。

        param(JSON):
          { "label": "页面=答题页", "show_ocr": false }

        `label` 会在日志里原样打出来——页面探测节点靠它确认「匹配到了哪一页」。
        没有 label 时退回打整屏 OCR（老的调试用法）。
        """

        def run(self, context, argv):
            try:
                param = json.loads(argv.custom_action_param or "{}")
            except json.JSONDecodeError:
                param = {}

            label = param.get("label") or ""
            if label:
                _probe(f"[page] {label}")
                if param.get("show_ocr"):
                    job = context.tasker.controller.post_screencap().wait()
                    if job.succeeded:
                        _probe(f"[page]   OCR: "
                               f"{_ocr_image(context, job.get())[:160]}")
                return True

            job = context.tasker.controller.post_screencap().wait()
            if job.succeeded:
                text = _ocr_image(context, job.get())
                print(f"[progress] OCR: {text[:200]}")
            return True

    for name, cls in (
        ("VideoProgress", VideoProgress),
        ("QuizAnswer", QuizAnswer),
    ):
        try:
            if resource.register_custom_recognition(name, cls()):
                registered.append(name)
            else:
                print(f"[warn] 注册识别器失败: {name}")
        except Exception as exc:
            print(f"[warn] 注册识别器异常 {name}: {exc}")

    for name, cls in (
        ("WatchVideo", WatchVideo),
        ("WatchCourse", WatchCourse),
        ("LogProgress", LogProgress),
        ("AnswerQuestion", AnswerQuestion),
    ):
        try:
            if resource.register_custom_action(name, cls()):
                registered.append(name)
            else:
                print(f"[warn] 注册动作失败: {name}")
        except Exception as exc:
            print(f"[warn] 注册动作异常 {name}: {exc}")

    return registered


# --------------------------------------------------------------------------
# OCR / 图像小工具
# --------------------------------------------------------------------------

def _crop(img: np.ndarray, roi) -> np.ndarray:
    x, y, w, h = roi
    return img[y:y + h, x:x + w]


_ocr_unused_marker = None


def _ocr_rows(context, img: np.ndarray) -> list[tuple[str, int, int]]:
    """对给定图像跑 OCR，返回 [(文本, x, y), ...]（绝对坐标）。

    和 `_ocr_image` 一样，**回调内必须走 context.run_recognition_direct()**，
    用 tasker.post_recognition().wait() 会自死锁（详见 `_ocr_image` 的说明）。

    抽成共用函数是因为这个「投队列 + 等」的错误写法在项目里出现了三处
    （`_ocr_image`、目录枚举、以及别处的整屏 OCR），各自踩一遍同一个坑。
    """
    from maa.pipeline import JOCR, JRecognitionType

    results = None

    # --- 路径 1：回调内，有 context → 同步直调 ---
    if context is not None and hasattr(context, "run_recognition_direct"):
        try:
            detail = context.run_recognition_direct(
                JRecognitionType.OCR, JOCR(), img
            )
            results = getattr(detail, "all_results", None) if detail else None
        except Exception as exc:  # noqa: BLE001 - 识别失败不该让整条流程崩
            print(f"[ocr] run_recognition_direct 异常: {exc}")
            results = None

    # --- 路径 2：回调外，只有 tasker ---
    if results is None:
        tasker = getattr(context, "tasker", None) if context is not None else None
        if tasker is None:
            return []
        j = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
        if not j.wait().succeeded:
            return []
        td = j.get()
        if td is None:
            return []
        for nid in td.node_id_list:
            node = tasker.get_node_detail(nid)
            if node is not None and node.recognition is not None:
                results = node.recognition.all_results or []
                break

    rows: list[tuple[str, int, int]] = []
    for r in (results or []):
        box = getattr(r, "box", None)
        text = getattr(r, "text", None)
        if box is None or not text:
            continue
        bx, by = int(box[0]), int(box[1])
        rows.append((str(text), bx, by))
    return rows


#: 本次任务里已经挂起过的题干（归一化后）。
#:
#: 为什么需要：框架对**未命中**的节点会反复重试（直到节点 timeout）。
#: 实测同一道题被挂了 3 次、白跑 3 轮联网请求。
#:
#: 但不能只靠「静默返回 None」去重——那样题目永远不会被作答。
#: 正确做法：第一次返回 None（挂起等人），后续重试改成**返回猜测值**，
#: 让框架停止重试、把题选上，整卷才能做完。
#: 猜测的答案不入库，交卷后由结果页采集纠正。
#:
#: 作用域是**单次任务执行**：新任务开始时清空（见 _reset_analyze_state）。
_SUSPENDED_STEMS: set[str] = set()


def _reset_analyze_state() -> None:
    """每次新任务开始前清空去重集合。

    由 main() 在 post_task 之前调用——那是最可靠的「新一轮」信号。
    （早先想按识别参数变化判断，但同一节点的参数永远不变，判据是错的。）
    """
    _SUSPENDED_STEMS.clear()


def _course_name_from(rows: list[tuple[str, int, int]]) -> str:
    """从课程页的 OCR 结果里认出课程名。

    课程名在页面顶部（实测 y 约 20~60），是**最长的一段中文文本**——
    顶部那一带还有「课程学习」这种固定标题、以及站点域名，靠长度区分。

    认不出来时返回空串，调用方会退化成「不记录进度」而不是记错课程名。
    """
    cands: list[tuple[int, str]] = []
    for text, _x, y in rows:
        if not (15 <= y <= 75):
            continue
        t = " ".join((text or "").split())
        if len(t) < 4:
            continue
        # 固定标题与域名不是课程名
        if any(k in t for k in ("课程学习", "我的学习", "http", ".cn", ".com")):
            continue
        # 纯数字/时间/符号跳过
        if not any("\u4e00" <= ch <= "\u9fff" for ch in t):
            continue
        cands.append((len(t), t))

    if not cands:
        return ""
    cands.sort(reverse=True)
    return cands[0][1]


def _probe(msg: str) -> None:
    """诊断探针输出。带 [probe] 前缀，便于上层脚本从日志里挑出来。

    用 flush 是因为死锁/被强杀的场景下缓冲输出会丢——
    而恰恰是那种场景最需要看到输出。
    """
    print(msg, flush=True)


#: 选项行开头的标签，形如「A.」「A、」「A．」「A)」「T.」
_OPTION_LABEL_RE = __import__("re").compile(
    r"^\s*([A-Fa-f]|[TF])\s*[.、．，,)）:：]\s*"
)


def _split_option_label(text: str, fallback: str) -> tuple[str, str]:
    """把 OCR 读到的选项行拆成 (标签, 正文)。

    实测判断题的选项是 **T/F 而不是 A/B**：

        F.错误
        T.正确

    早先按位置硬编码 chr(ord("A")+i) 当标签，于是判断题答案变成
    `['A']`——指向第 0 行（=错误），而正确答案本该是 `['T']`。
    **标签必须从文本里读，不能按位置猜。**

    文本里没有可识别的标签时退回 `fallback`（单选常是 A/B/C/D）。
    """
    s = (text or "").strip()
    if not s:
        return fallback, ""
    m = _OPTION_LABEL_RE.match(s)
    if m:
        return m.group(1).upper(), s[m.end():].strip()
    return fallback, s


def _ocr_image(context, img: np.ndarray) -> str:
    """用当前 Tasker 对给定图像跑一次 OCR，返回拼接文本。

    ## 两条路径，按是否在回调里选

    **在自定义识别/动作回调里必须用 `context.run_recognition_direct()`。**
    它同步直调，不经过 tasker 队列。

    绝不能在回调里用 `context.tasker.post_recognition().wait()` ——
    那是把 job 投给**同一个** tasker 的队列，然后在队列自己的执行线程上等它。
    队列正忙着跑当前回调，那个 job 永远轮不到 → **自死锁**。

    实测（scripts\\run_deadlock_probe.py）：
        tasker.post_recognition 在回调内    → 12s 超时，判定自死锁
        context.run_recognition_direct      → 0.02s 返回
    这个 bug 让 QuizAnswer 挂住几分钟、无日志、无 pending 记录，极难查。

    回调外（比如 CLI 探针、非回调的辅助函数）没有 context，
    退回 tasker 路径 —— 那时 tasker 是空闲的，不会死锁。
    那条路径返回的是 TaskJob 而非 RecognitionDetail，取值链路不同：
        job.wait().get() -> TaskDetail -> node_id_list -> get_node_detail(nid)
    直接拿 job.job_id 去 get_recognition_detail 会 failed to get_reco_result，
    因为那是 task_id 不是 reco_id。这个坑也实测确认过。
    """
    from maa.pipeline import JOCR, JRecognitionType

    # --- 路径 1：在回调里，有 context → 同步直调 ---
    if context is not None and hasattr(context, "run_recognition_direct"):
        try:
            detail = context.run_recognition_direct(
                JRecognitionType.OCR, JOCR(), img
            )
        except Exception as exc:  # noqa: BLE001 - 识别失败不该让整条流程崩
            print(f"[ocr] run_recognition_direct 异常: {exc}")
            detail = None
        if detail is None:
            return ""
        return " ".join(
            str(getattr(r, "text", ""))
            for r in (getattr(detail, "all_results", None) or [])
        )

    # --- 路径 2：回调外，只有 tasker ---
    tasker = getattr(context, "tasker", None) if context is not None else None
    if tasker is None:
        print("[ocr] 既没有 context 也没有 tasker，无法 OCR")
        return ""

    job = tasker.post_recognition(JRecognitionType.OCR, JOCR(), img)
    if not job.wait().succeeded:
        return ""

    task_detail = job.get()
    if task_detail is None:
        return ""

    for node_id in task_detail.node_id_list:
        node = tasker.get_node_detail(node_id)
        if node is None or node.recognition is None:
            continue
        return " ".join(
            str(getattr(r, "text", "")) for r in (node.recognition.all_results or [])
        )
    return ""


_STATE_CHECK_HINTS = (
    "请选择A",
    "请选择 A",
    "选择A",
    "学习状态检测",
    "如需继续学习",
)


def _looks_like_state_check(ocr_text: str) -> bool:
    """判断弹题是不是「学习状态检测题」。

    这类题自己把答案写在题干里（「如需继续学习，请选择A」），所以可以安全地
    自动作答。不满足这个特征的一律转人工，避免脚本瞎猜把课挂掉。
    """
    if not ocr_text:
        return False
    flat = ocr_text.replace(" ", "").replace("\u3000", "")
    return any(h.replace(" ", "") in flat for h in _STATE_CHECK_HINTS)


def _parse_numeric_range(ocr_text: str) -> tuple[int, int] | None:
    """从弹题文案里解析「请输入 50-100 的数值」这类取值范围。

    返回 (下限, 上限)；认不出来返回 None。

    ## 为什么需要

    实测视频里除了「学习状态检测题」（自带答案「请选择A」），还有**评分题**：

        视频弹题（00:40:41）正确作答后继续视频学习
        1、请您为老师此堂讲课总体效果打分，满分100分(内容新颖实用，讲授清晰易懂，ppt制作精良)
        请输入50-100的数值
        （简答题）

    这类题**不填就永远卡住视频**（弹题期间视频是暂停的），而它并不是知识考核
    ——是给老师讲课打分。所以可以安全地填一个区间内的值。

    必须**从文案里取区间**再填，不能写死数字：不同课程的下限不一样，
    填到区间外会被判无效、弹题不关。

    ## 为什么还要从「满分」推

    实测踩过：占位提示「请输入 50-100 的数值」**只在输入框为空时显示**。
    一旦框里已经有内容（上一次填过、或页面自己回填），这段文案就没了，
    只按占位文案解析会返回 None → 弹题关不掉 → 视频永久暂停。

    所以退一步：题干里既然写了「满分100分」，就按满分推一个合理区间
    （下限取满分的 50%）。评分题是给老师打分，不是知识考核，
    填区间内的值都能过。
    """
    if not ocr_text:
        return None
    flat = ocr_text.replace(" ", "").replace("\u3000", "")

    # 1) 优先用显式的输入范围提示
    for m in re.finditer(r"(?:请输入|输入|范围)[^0-9]{0,6}(\d{1,3})\s*[-~—－至到]\s*(\d{1,3})",
                         flat):
        lo, hi = int(m.group(1)), int(m.group(2))
        if 0 <= lo < hi <= 1000:
            return lo, hi

    # 2) 退一步：整段里有「N-M」且带「数值/分」字样
    if any(k in flat for k in ("数值", "分)")) or "满分" in flat:
        m = re.search(r"(\d{1,3})\s*[-~—－至到]\s*(\d{1,3})", flat)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 0 <= lo < hi <= 1000:
                return lo, hi

    # 3) 兜底：从「满分N分」推（占位提示消失时的唯一线索）
    m = re.search(r"满分\s*(\d{1,3})\s*分", flat)
    if m:
        full = int(m.group(1))
        if 50 <= full <= 1000:
            return full // 2, full

    return None


def _adb_input_text(context, text: str) -> bool:
    """用 `adb shell input text` 往当前焦点控件输入文本。返回是否成功。

    ## 为什么不用 MaaFramework 的 post_input_text

    实测这个平台（微信 WebView）**对 MaaTouch 的文本输入不生效**：

        controller.post_input_text("83")  → 调用成功返回，但输入框仍是占位符
        adb shell input text 77           → 输入框立刻变成 "77"

    方向正好和点击相反——本项目里点击是「adb tap 不行、MaaTouch 行」，
    而文本输入是「MaaTouch 不行、adb 行」。所以两者都要留着，按用途选。

    ## 必须带 `-s <serial>`

    实测踩过：本机 adb 同时看到两个设备

        127.0.0.1:16384   device      ← MuMu，我们要的
        emulator-5554     device      ← 另一个模拟器

    不带 `-s` 时 adb 直接报 `more than one device/emulator` 并退出 1，
    什么也不会输入。**隔离测试时能用是因为我手工带了 `-s`**，
    而代码里把地址取自 `config['adb']['address']`——那一项在自动探测模式下
    **是空字符串**，于是 `-s` 根本没拼上去。这个差异骗过了一次测试。

    正确来源是 **controller.info['adb_serial']**：那是控制器实际连上的地址，
    自动探测的结果也在里面。
    """
    global _ADB_CACHE
    import subprocess

    cmd = build_adb_input_cmd(context, text)

    # adb 路径兜底：info 里没有就自己探测一次并缓存
    if cmd[0] in ("adb", ""):
        if _ADB_CACHE is None:
            try:
                import detect

                _ADB_CACHE = str(detect.find_adb())
            except Exception as exc:  # noqa: BLE001 - 探测失败要能看到原因
                print(f"[watch] 找不到 adb，无法输入文本: {exc}")
                _ADB_CACHE = ""
        if not _ADB_CACHE:
            return False
        cmd[0] = _ADB_CACHE

    if "-s" not in cmd:
        print("[watch] ⚠ 拿不到设备序列号，adb 在多设备环境会报错")

    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        ok = proc.returncode == 0
        if not ok:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[:160]
            print(f"[watch] adb input text 返回 {proc.returncode}: {err}")
        return ok
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[watch] adb input text 异常: {exc}")
        return False


#: adb 可执行文件路径缓存（探测一次要几秒，别在弹题处理里重复扫）
_ADB_CACHE: str | None = None


def _controller_of(context):
    """从回调上下文里取出 AdbController；拿不到返回 None。

    ## 为什么需要这个转换

    `_adb_input_text(context, ...)` 里的 `context` 有两种可能：

    * 在**回调里**调用时，它是 `maa.context.Context`（框架传进来的），
      控制器在 `context.tasker.controller` 上；
    * 在**回调外**（CLI 探针、单测）调用时，可能直接就是控制器。

    实测踩过：只按「context 本身有 .info」去取，回调里永远取不到 →
    `adb_serial` 为空 → `adb shell input text` 不带 `-s` →
    多设备环境直接 `more than one device/emulator` 失败 →
    **弹题永远填不上、视频永久暂停**。

    更值得记的是**它骗过了一次验证**：我隔离测试时直接传了控制器对象，
    所以「能用」；而生产路径传的是回调 Context。测试载体和生产不一致，
    这种差异靠肉眼看代码是发现不了的。
    """
    if context is None:
        return None
    # 已经是控制器（有 info 且 info 里有 adb 相关字段）
    info = getattr(context, "info", None)
    if isinstance(info, dict) and ("adb_serial" in info or "adb_path" in info):
        return context
    # 回调上下文 → 控制器
    tasker = getattr(context, "tasker", None)
    ctrl = getattr(tasker, "controller", None) if tasker is not None else None
    return ctrl


def build_adb_input_cmd(context, text: str) -> list[str]:
    """拼出 `adb shell input text` 的命令行（**不含执行**，便于单测）。

    抽出来单独测是因为「漏了 -s」这个 bug 靠肉眼看不出来，
    而它的后果是静默失败——弹题永远关不掉、视频永远卡住。
    """
    ctrl = _controller_of(context)
    info = getattr(ctrl, "info", None) if ctrl is not None else None

    adb = ""
    serial = ""
    if isinstance(info, dict):
        adb = str(info.get("adb_path") or "")
        serial = str(info.get("adb_serial") or "")
    if not adb:
        adb = "adb"
    cmd = [adb]
    if serial:
        cmd += ["-s", serial]
    cmd += ["shell", "input", "text", str(text)]
    return cmd


def _dump_pending_popup(ocr_text: str, context) -> None:
    """弹题处理失败时存证：截图 + OCR 文本，方便人工定位真实答案。"""
    out_dir = paths.pending_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")

    (out_dir / f"popup-{stamp}.txt").write_text(ocr_text, encoding="utf-8")

    try:
        job = context.tasker.controller.post_screencap().wait()
        if job.succeeded:
            from imageio_util import save_png

            save_png(job.get(), out_dir / f"popup-{stamp}.png")
    except Exception as exc:
        print(f"[watch] 弹题截图失败: {exc}")

    print(f"[watch] 弹题存证已写入: {out_dir}")


# --------------------------------------------------------------------------
# OCR 探针（写节点用）
# --------------------------------------------------------------------------

def _ocr_via_temp_node(tasker, roi: list[int] | None):
    """用临时 Pipeline 节点跑一次 OCR，返回 (RecognitionDetail, err)。

    两个坑：
    1. tasker.post_recognition 返回的是 TaskJob，而 MaaTaskerGetRecognitionDetail
       要的是 reco_id（传 job_id 会 failed to get_reco_result）。必须走 post_task
       再从 TaskDetail.nodes 取。
    2. OCR 在区域内没识别到文字时，节点算「未命中」，next 超时 → 任务失败。
       但这不代表探针出错，所以任务失败也要去取识别详情。
    """
    node_name = "__ocr_probe__"
    entry: dict = {
        "recognition": "OCR",
        "action": "DoNothing",
        "timeout": 3000,
    }
    if roi:
        entry["roi"] = roi

    job = tasker.post_task(node_name, pipeline_override={node_name: entry}).wait()

    detail = tasker.get_task_detail(job.job_id)
    if detail is not None:
        for node in detail.nodes:
            if node.name == node_name and node.recognition is not None:
                return node.recognition, None

    if not job.succeeded:
        return None, f"OCR 节点未产生结果（区域可能是空白，或 OCR 模型未加载）"
    return None, "节点未产生识别结果"


def run_ocr_probe(controller, roi: list[int] | None) -> int:
    """对当前画面做一次全屏/区域 OCR 并打印带坐标的结果。

    这是把「截图」翻译成「Pipeline 节点」的主要工具：
    你给截图，我用它拿到精确坐标和真实文案。
    """
    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    job = resource.post_bundle(str(RESOURCE_DIR)).wait()
    if not job.succeeded:
        print(f"[FATAL] 资源加载失败: {RESOURCE_DIR}", file=sys.stderr)
        return 1

    if not resource.post_ocr_model(str(RESOURCE_DIR / "model" / "ocr")).wait().succeeded:
        print("[FATAL] OCR 模型加载失败，检查 assets/resource/model/ocr "
              "（需要 det.onnx / rec.onnx / keys.txt）", file=sys.stderr)
        return 1

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] tasker.bind 失败", file=sys.stderr)
        return 1

    print(f"[ocr] 识别区域: {roi if roi else '全屏'}")
    detail, err = _ocr_via_temp_node(tasker, roi)
    if detail is None:
        # 没识别到文字不算错误——空白区域本来就没有文字
        print(f"[ocr] 未识别到文本 ({err})")
        return 0

    ox, oy = (roi[0], roi[1]) if roi else (0, 0)
    results = detail.all_results or []
    if not results:
        print("[ocr] 未识别到任何文本（画面可能是纯图或有遮挡）")
        return 0

    print(f"[ocr] 命中 {len(results)} 段文本：\n")
    print(f"{'文本':<44} {'x':>5} {'y':>5} {'w':>5} {'h':>5}  {'score':>6}")
    print("-" * 80)
    rows = []
    for r in results:
        box = getattr(r, "box", None)
        if box is None:
            continue
        text = str(getattr(r, "text", ""))
        score = float(getattr(r, "score", 0.0))
        bx, by, bw, bh = box
        rows.append((text, bx + ox, by + oy, bw, bh, score))
    for text, bx, by, bw, bh, score in rows:
        print(f"{text:<44} {bx:>5} {by:>5} {bw:>5} {bh:>5}  {score:>6.3f}")

    # 直接给出可粘进 pipeline 的 roi 写法
    print("\n[提示] 圈某一行做节点时，roi 可用该行 box 外扩几像素，例如：")
    t, bx, by, bw, bh, _ = rows[0]
    print(f'  "{t[:20]}"  ->  "roi": [{max(0, bx - 10)}, {max(0, by - 6)}, {bw + 20}, {bh + 12}]')
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="中山医院继续教育平台自动化")
    parser.add_argument("--check", action="store_true", help="检查配置 / 连接 / 资源加载")
    parser.add_argument("--ocr", action="store_true", help="OCR 探针：打印当前画面文本及坐标")
    parser.add_argument("--roi", default="", help="配合 --ocr，格式 x,y,w,h")
    parser.add_argument("--task", default="", help="运行指定 Pipeline 入口节点")
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    try:
        controller = build_controller(cfg)
    except (ConfigError, RuntimeError) as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 1

    if args.ocr:
        roi = [int(v) for v in args.roi.split(",")] if args.roi else None
        if roi and len(roi) != 4:
            print("[FATAL] --roi 需要 4 个数字: x,y,w,h", file=sys.stderr)
            return 2
        return run_ocr_probe(controller, roi)

    from maa.resource import Resource
    from maa.tasker import Tasker

    resource = Resource()
    job = resource.post_bundle(str(RESOURCE_DIR)).wait()
    if not job.succeeded:
        print(f"[FATAL] 资源加载失败: {RESOURCE_DIR}", file=sys.stderr)
        return 1
    print(f"[resource] 已加载 {len(resource.node_list)} 个节点")

    # 用 ppocr 模型；失败只警告，因为纯模板匹配流程仍可用
    if not resource.post_ocr_model(str(RESOURCE_DIR / "model" / "ocr")).wait().succeeded:
        print("[warn] OCR 模型加载失败，OCR 相关节点将不可用")

    names = register_custom_modules(resource)
    print(f"[resource] 自定义模块: {names or '(无)'}")

    tasker = Tasker()
    if not tasker.bind(resource, controller):
        print("[FATAL] tasker.bind 失败", file=sys.stderr)
        return 1

    # 日志与调试产物
    tasker.set_log_dir(str(paths.log_dir()))
    tasker.set_save_draw(True)
    tasker.set_save_on_error(True)

    if args.check:
        print("[OK] 配置 / 连接 / 资源 全部就绪")
        print(f"[OK] 可用入口节点: {[n for n in resource.node_list]}")
        return 0

    if not args.task:
        print("[INFO] 未指定 --task。用 --check 验证环境，或 --task <节点名> 执行任务。")
        return 0

    print(f"[task] 开始执行: {args.task}")
    _reset_analyze_state()
    tjob = tasker.post_task(args.task).wait()
    if tjob.succeeded:
        print("[task] 完成")
        return 0
    print(f"[task] 失败, status={tjob.status}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
