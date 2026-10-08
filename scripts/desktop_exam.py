# -*- coding: utf-8 -*-
"""桌面版（浏览器 + 电脑模式）的**结课三件事**里除视频课以外的两件：考核 + 问卷。

用法::

    ZSCMEAutopilot.exe --run desktop_exam --list              # 只列考核和问卷，不动手
    ZSCMEAutopilot.exe --run desktop_exam --course 肝胆        # 只做名字里含这几个字的课
    ZSCMEAutopilot.exe --run desktop_exam --questionnaire-only # 只补问卷
    ZSCMEAutopilot.exe --run desktop_exam --dry-run           # 只报准备做什么

## 结课到底要什么

平台的结课要求写在课程列表的 `classAssessmentDesc` 里，原文是：
**完成所有视频课件学习 + 考核 ≥60 分 + 完成问卷调查**。三件缺一不可，
课程列表上就是按这三件拼出来的一句话（实测：`视频课件已完成，考核90分，
未完成问卷调查`）。视频那件由 `desktop_watch.py` 负责，这里管后两件。

## 考核：为什么"先故意交一次"是正路

考核走课程站的 `homework` 族接口（**不是** `testing`/`ks` ——
`queryTestingList` / `queryTestList` 对这门课全回空，2026-10-08 在这上面
绕了很久）。真正的题库考核有单选/多选/判断，答案只有平台知道。

但平台自己写着两条规则（考核页正文原文）：

    1、客观题考核可以重复提交。
    4、学生重复提交考核时，系统会记录重做次数。

再配上这门课 `answerShowType = 2`、`allowRedoNum = 8`，它的实际效果就是：
**交一次卷 → 批改后「查看」页把每道题的正确答案摊开（`sanswer`）→
拿着这份答案重做一次 → 100 分**。这不是破解，是平台设计好的学习闭环
（先做、再看解析、再订正）；只是人来做要来回点很多次，程序做得快。

所以 `do_exam()` 的循环是「交卷 → 去查看页收答案 → 用答案重做」。
**收不到答案就不再重交**（`sanswer` 空 = 这门课不显示答案），瞎蒙第二次
没有意义，如实报出来更好。

## 问卷：它的 `classId` 就是课程 id

问卷那一族接口（`/user/queryQuestionnaireList` 等）在 `elearning` 域上，
`selectType=1` 是「未参加」。**每条的 `classId` 和「我的学习」里
`courseList[].id` 是一个东西**（实测：重症那条两边字符完全一致），
所以不用建映射表，拿课程 id 直接对。

问卷全是满意度题（没有对错），策略是**挑最正面/最常见的那个选项**，
挑不出来就选第一个。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop  # noqa: E402
import paths  # noqa: E402

#: 题库缓存落在 `data/`（跟手机版的 `answer_cache_path` 同一个目录，
#: 但**另一个文件** —— 手机版那份是按"题干文字"索引的四选一，格式不一样，
#: 混用会把两边的匹配都搞乱）。
CACHE_NAME = "exam_answers.json"

#: 及格线。平台原文就是「考核 ≥60 分」，写成常量免得各处抄错。
PASS_SCORE = 60

#: 一门课最多交几次卷。
#:
#: 三次是有讲究的：**第 1 次是"投石问路"**（没答案，只能按缓存/蒙），
#: 第 2 次是"用收来的答案订正"，理论上就该 100 分了；第 3 次留给
#: 题库里有题换了顺序/换了题的意外。**不做无限重试** —— 平台给的
#: `allowRedoNum` 是 8 次，但把 8 次都刷完既没必要也不礼貌。
MAX_TRIES = 3

#: 问卷满意度题的偏好顺序。**先整串相等地找，再退化成包含匹配** ——
#: 因为「很满意」里含「满意」，只做包含匹配会把「满意」也命中。
PREFER = ("很满意", "非常满意", "满意", "很大", "较大", "部分知道",
          "开阔思路", "提高临床诊治能力", "是", "基本是")


#: 日志出口。`None` = 直接 `print`（命令行跑的时候）。
_sink: "Callable[[str], None] | None" = None


def set_log(sink: "Callable[[str], None] | None") -> None:
    """把日志改接到界面（或任何别的地方）。

    为什么要这个钩子：**界面里没有控制台**。这个脚本原来一律 `print`，
    命令行跑没问题，但从界面按钮跑起来那些输出就全进了虚空 —— 界面上
    只剩一句「运行中…」，考了几分、问卷登记上没有，一概看不见。

    接上之后界面上的日志面板就是它的 stdout；`None` 恢复成 `print`。
    """
    global _sink
    _sink = sink


def log(msg: str) -> None:
    if _sink is not None:
        try:
            _sink(msg)
            return
        except Exception:  # noqa: BLE001
            # 日志出口坏了不能把考核带崩（同 desktop_watch 的理由）。
            pass
    print(msg, flush=True)


def _cache_path() -> Path:
    return paths.data_dir() / CACHE_NAME


def _norm(text: object) -> str:
    """题干归一化：去掉所有空白再截断。

    页面上的题干带全角空格、换行、序号前缀（`1、`），原样当 key 会
    一个都对不上。只去空白是**故意的** —— 序号也去掉的话，
    "题库里同一道题出现在不同序号下"就会互相覆盖，而那其实是好事；
    但风险是两道不同的题被归一成同一个 key。实测这个题库里题干
    本身够长够唯一，不差那个序号。
    """
    return re.sub(r"\s+", "", str(text or ""))[:100]


def load_cache() -> dict:
    """读题库缓存。文件坏了就当成空的（**绝不因为缓存坏了就不干活**）。"""
    p = _cache_path()
    if not p.exists():
        return {"questions": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"questions": {}}
    if not isinstance(d, dict):
        return {"questions": {}}
    if not isinstance(d.get("questions"), dict):
        d["questions"] = {}
    return d


def save_cache(data: dict) -> None:
    """写题库缓存（先写临时文件再替换，别把半截 JSON 留在那儿）。"""
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log(f"[exam] 题库缓存写不进去（不影响这次作答）：{exc}")


def resolve(entry: dict, q: dict) -> str | None:
    """把缓存里存的答案搬到**这次**这张卷子上，返回答案串；搬不动返回 None。

    为什么要搬：缓存里存的是上一次那卷的选项**字母**（`"D"`、`"A|C|D|E"`），
    而题干相同不代表选项顺序相同 —— 题库考核换卷时选项很可能重排，
    照抄字母就会把对的答成错的。

    所以缓存里同时存了正确答案对应的**选项文字**（`texts`）。优先按文字
    在当前选项里找；找不到（题干变了、措辞改了）再退一步用字母，
    但要求那个字母在当前选项里确实存在。
    """
    if not isinstance(entry, dict):
        return None
    opts = {str(o.get("index")): _norm(o.get("text")) for o in (q.get("options") or [])}
    if not opts:
        return None
    texts = [t for t in (entry.get("texts") or []) if t]
    if texts:
        picked = []
        for t in texts:
            want = _norm(t)
            hit = next((ix for ix, txt in sorted(opts.items()) if txt == want), None)
            if hit is None:
                picked = []
                break
            picked.append(hit)
        if picked:
            return "|".join(sorted(set(picked)))
    letters = [x.strip() for x in str(entry.get("answer") or "").split("|") if x.strip()]
    kept = [x for x in letters if x in opts]
    if kept and len(kept) == len(letters):
        return "|".join(sorted(set(kept)))
    return None


def guess(q: dict) -> str:
    """没答案时给一个"最不坏"的默认：单选/判断选第一个选项，多选也选第一个。

    为什么**不用随机**：随机的话同一个错题每轮答案都不同，缓存里收来的
    正确答案下一次就对不上了（`resolve` 按选项文字找，也是这样才稳）；
    而且随机让整件事不可复现，出了问题没法回放。

    为什么默认选第一个而不是"A"：判断/某些题的选项 `index` 不是 A/B
    而是别的编码，写死 "A" 会填不进去（`HOMEWORK_FILL_JS` 按 `index`
    匹配，填不进去就等于空着，平台会拦住不让交）。
    """
    kind = q.get("kind")
    opts = q.get("options") or []
    if kind == "wenda":
        return "无"
    if not opts:
        return ""
    if kind == "duoxuan":
        return str((opts[0] or {}).get("index") or "")
    return str((opts[0] or {}).get("index") or "")


def plan_for(questions: list, cache: dict) -> tuple[dict, int]:
    """按缓存给出这一卷的作答计划。返回 `(plan, 有几道没把握)`。

    `plan` 是 `{题目 id: 答案}`。答案的取值规则（从平台源码 `p()` 反推，
    原文见 DEVELOPMENT.md 7.5.2）：单选/判断 = 选项的 `index`，
    多选 = `"A|C"` 这种拼接。

    没把握的题**照样给一个答案**：空着交上去会被平台的
    `checkAnswerModel()` 拦住不让提交，那就连"交一次看答案"都做不成。
    """
    known = cache.get("questions") if isinstance(cache.get("questions"), dict) else {}
    plan: dict = {}
    unsure = 0
    for q in questions:
        qid = str(q.get("id") or "")
        if not qid:
            continue
        got = resolve(known.get(_norm(q.get("title"))) or {}, q)
        if got not in (None, ""):
            plan[qid] = got
            continue
        unsure += 1
        g = guess(q)
        if g:
            plan[qid] = g
    return plan, unsure


def harvest(questions: list, cache: dict) -> int:
    """把**平台自己公布**的正确答案收进缓存，返回收了几道。

    只在「查看」页（`homework_open(route="show")`）上有 `sanswer`。
    存的是三件套：字母（`answer`）+ 对应的选项文字（`texts`）+ 当时的
    整张选项表（`options`）—— 前两件给 `resolve()` 用，最后一件是
    留给排查的（"当初这题到底有哪些选项"）。
    """
    known = cache.setdefault("questions", {})
    got = 0
    for q in questions:
        sa = str(q.get("sanswer") or "").strip()
        if not sa:
            continue
        key = _norm(q.get("title"))
        if not key:
            continue
        opts = {str(o.get("index")): str(o.get("text") or "") for o in (q.get("options") or [])}
        letters = [x.strip() for x in sa.split("|") if x.strip()]
        known[key] = {
            "answer": sa,
            "texts": [opts[x] for x in letters if opts.get(x)],
            "options": opts,
            "kind": q.get("kind"),
        }
        got += 1
    return got


def harvest_wait(sess: desktop.Session, homework_id: str, cache: dict,
                 *, wait: float = 90.0) -> int:
    """去「查看」页把平台公布的答案收下来，返回收了几道。

    **为什么要反复试**：`homework_open` 回来只说明路由到位了，页面还要
    自己去打 `showHomework` 才把 `questionObj` 填上；而**刚交完卷的
    那一小段时间服务端还在批改**，那时候 `showHomework` 会回错
    （页面上是 `loadError = true`、`loaded = false`、零道题），
    整页 reload 也救不回来。2026-10-08 实测：交完 10 秒进去是空的，
    隔了几分钟再进去就 20 道题全带 `sanswer`。
    所以这里按 `wait` 秒反复重开「查看」页，直到读出 `sanswer` 为止。
    """
    deadline = time.monotonic() + max(0.0, wait)
    round_no = 0
    last = "还没试"
    while True:
        round_no += 1
        if not sess.homework_open(homework_id, route="show"):
            last = "打不开「查看」页"
        else:
            form = sess.homework_answers_shown()
            qs = form.get("questions") or []
            got = harvest(qs, cache) if qs else 0
            if got:
                return got
            last = (f"loaded={form.get('loaded')} route={form.get('route')} "
                    f"题 {len(qs)} 道" + ("（服务端还在批改）" if not form.get("loaded") else ""))
        if time.monotonic() >= deadline:
            log(f"[exam] 收了 {round_no} 轮都没收到正确答案（{last}）")
            return 0
        time.sleep(8.0)


def _score_of(res: dict) -> int | None:
    """从交卷回执里抠分数（字段名不确定，宽容地认几个）。

    ⚠ 必须把 `bool` 挡在外面：`queryHomeworkList` 每一项里 `showTkScore`
    是个**布尔** `true`（"允许显示分数"这个开关，不是分数），而 Python 里
    `isinstance(True, int)` 是 True —— 不挡的话这里会稳稳地返回 `1`，
    看着像"交完卷得了 1 分"。
    """
    if not isinstance(res, dict):
        return None
    for key in ("score", "tkScore", "showTkScore", "homeworkScore"):
        v = res.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().lstrip("-").isdigit():
            return int(v.strip())
    for key in ("homeworkObj", "result"):
        v = res.get(key)
        if isinstance(v, dict):
            deep = _score_of(v)
            if deep is not None:
                return deep
    return None


def _status_of(sess: desktop.Session, item_id: str) -> dict:
    """交完卷回平台问一次**权威**结果（回执里的分数不一定有）。"""
    try:
        for it in sess.homework_list():
            if str(it.get("itemId") or it.get("id") or "") == str(item_id):
                return it
    except Exception:  # noqa: BLE001
        pass
    return {}


def do_exam(sess: desktop.Session, course: dict, item: dict, cache: dict,
            *, dry_run: bool = False, harvest_only: bool = False) -> dict:
    """做一门课的一项考核。返回统计。"""
    name = (course.get("name") or "")[:30]
    title = (item.get("title") or "")[:30]
    hid = str(item.get("id") or "")
    item_id = str(item.get("itemId") or hid)
    stat = {"course": name, "title": title, "tries": 0, "score": item.get("score"),
            "status": "", "done": False}

    # `--harvest-only`：**只收答案，一次卷都不交**。
    #
    # 这是最省事的一条路，理由：`sanswer` 只要**这门课历史上交过一次卷**就
    # 一直在（`homeworkStatus=3` 的课，`showHomework` 回 20 道题、20 道带答案），
    # 不用重新交。所以开跑之前先把所有"已经考过"的课的答案收干净，
    # 题库是共用的，收得越多，后面没考过的课要蒙的就越少 ——
    # 每少蒙一门课就少浪费一次「重做次数」和一次交卷。
    #
    # 注意这一支要放在"已经过了就不用做"那条**前面**：过了的课照样有答案可收。
    if harvest_only:
        score = item.get("score") or 0
        if item.get("homeworkStatus") in (0, None):
            log(f"[exam] 「{title}」还没交过卷（状态{item.get('homeworkStatus')}），"
                f"没有答案可收")
            stat["status"] = "没交过"
            return stat
        got = harvest_wait(sess, hid, cache, wait=30.0)
        if got:
            log(f"[exam] 「{title}」收下 {got} 道题的正确答案")
            save_cache(cache)
        stat["status"] = f"收了 {got}"
        stat["score"] = score
        return stat

    score = item.get("score") or 0
    if item.get("homeworkStatus") == 3 and score >= PASS_SCORE:
        log(f"[exam] 「{title}」已经 {score} 分，不用做")
        stat.update(status="已批改", done=True)
        return stat
    if not item.get("allowRedo") and item.get("homeworkStatus") not in (0, None):
        log(f"[exam] 「{title}」平台不让重做（状态 {item.get('homeworkStatus')}，"
            f"{score} 分），跳过")
        stat["status"] = "不许重做"
        return stat

    if not sess.homework_open(hid, route="do"):
        log(f"[exam] 进不去「{title}」的考核页，先放过")
        stat["status"] = "进不去"
        return stat
    if dry_run:
        form = sess.homework_form()
        plan, unsure = plan_for(form.get("questions") or [], cache)
        log(f"[exam] （dry-run）「{title}」{form.get('count', 0)} 道题，"
            f"手上有答案 {len(plan) - unsure} 道，要交 {min(MAX_TRIES, 2)} 次")
        return stat

    for k in range(1, MAX_TRIES + 1):
        stat["tries"] = k

        # **收上一轮的答案，放在这一轮的开头。**
        #
        # 为什么不在交完卷立刻收（原来的写法）：平台交完卷还要批改一会儿，
        # 这期间打开「查看」页，`showHomework` 会回错 —— 实测页面上是
        # `courseLearnHomeworkShowConfig.loadError = true`、`loaded = false`、
        # 一道题都读不到，而且**整页 reload 也救不回来**（服务端那边还没好，
        # 跟客户端状态无关）；等十来秒再进去就一切正常。2026-10-08 连丢
        # 三次交卷机会都是这个原因。
        #
        # 所以顺序改成「先补收上一轮的 → 再打开答题页 → 交」。第一轮没有
        # 上一轮，跳过；最后一轮交完再补收一次（见循环后面），这样下次跑
        # 这门课（或者别的课撞上同一道题）手上就有答案了。
        if k > 1:
            got = harvest_wait(sess, hid, cache, wait=90.0)
            if got:
                log(f"[exam] 收下 {got} 道题的正确答案")
                save_cache(cache)
            else:
                # 上一轮交完到现在还是收不到答案：再交一次也只是**同一批蒙的
                # 答案**重交一遍，分数不会变，白白消耗一次「重做次数」。
                log(f"[exam] 平台没给正确答案（`sanswer` 是空的）—— "
                    f"再交也只是重新蒙一遍，停在这儿，如实报出去")
                stat["status"] = stat["status"] or "平台不显示答案"
                break

        if k > 1:
            sess.homework_open(hid, route="do")
        form = sess.homework_form()
        questions = form.get("questions") or []
        if not questions:
            log(f"[exam] 「{title}」读不到题目（loaded={form.get('loaded')}），停手")
            stat["status"] = "读不到题"
            break
        kinds = ", ".join(sorted({str(q.get("kind")) for q in questions}))
        plan, unsure = plan_for(questions, cache)
        log(f"[exam] 「{title}」第 {k}/{MAX_TRIES} 次：{len(questions)} 道题（{kinds}）"
            f"，手上有答案 {len(plan) - unsure} 道，蒙 {unsure} 道")
        sess.homework_answer(plan)
        res = sess.homework_submit()
        log(f"[exam] 交卷回执：{str(res)[:150]}")
        time.sleep(2.0)

        # **交完卷读到的分可能是旧的。**
        #
        # 服务端批改是异步的，交完立刻查 `queryHomeworkList` 拿到的还是上一次
        # 的结果。2026-10-08 实测：第 1 次手上明明有 16 道答案（该 ≥80 分），
        # 读回来却是**交卷前那 35 分** —— 于是程序以为没过、又交了一次，
        # 白烧一次「重做次数」。
        #
        # 所以分数跟交卷前**一模一样**时要多问几遍（每 6s 一次、最多 5 次
        # = 30s），别急着当成"没过"。真的一分没涨的话也照样往下走，只是多花
        # 半分钟。
        before = stat.get("score")
        after = _status_of(sess, item_id)
        score = _score_of(res)
        if score is None or (before is not None and int(score) == int(before)):
            for _ in range(5):
                time.sleep(6.0)
                again = _status_of(sess, item_id)
                s2 = _score_of(again)
                if s2 is None:
                    continue
                after = again
                score = s2
                if before is None or int(s2) != int(before):
                    break
        if score is None:
            score = after.get("score")
        if score is not None:
            stat["score"] = score
            stat["status"] = "已批改"
            if int(score) >= PASS_SCORE:
                stat["done"] = True
                log(f"[exam] ✓ 「{title}」{score} 分，过了")
                return stat
            log(f"[exam] 「{title}」{score} 分，还没到 {PASS_SCORE}")

    # 兜底：万一这一轮还是没过，也尽量把答案收下来 —— 收下来的答案存在
    # `data/exam_answers.json` 里，下次跑（哪怕换一门课）直接就能用上。
    if not stat["done"]:
        got = harvest_wait(sess, hid, cache, wait=90.0)
        if got:
            log(f"[exam] 收下 {got} 道题的正确答案（留给下次）")
            save_cache(cache)
    _back_to_courseware(sess)
    return stat


def pick_option(topic: dict) -> dict | None:
    """问卷单选题挑一个选项：先整串相等，再包含匹配，最后退到第一个。"""
    opts = [o for o in (topic.get("optionList") or []) if isinstance(o, dict)]
    if not opts:
        return None
    for word in PREFER:
        for o in opts:
            if _norm(o.get("content")) == _norm(word):
                return o
    for word in PREFER:
        for o in opts:
            if _norm(word) and _norm(word) in _norm(o.get("content")):
                return o
    return opts[0]


def do_questionnaire(sess: desktop.Session, course: dict, it: dict,
                     *, dry_run: bool = False) -> dict:
    """交一门课的问卷。`it` 是 `queryQuestionnaireList` 里那一条。"""
    name = (course.get("name") or "")[:30]
    stat = {"course": name, "title": "问卷调查", "done": False, "status": ""}
    tp_id = str(it.get("id") or "")
    class_id = str(it.get("classId") or "")
    d = sess.q_detail(tp_id, class_id)
    topics = d.get("topicList") or []
    if not topics:
        # 最常见的原因是**考核还没到 60 分**：服务端会回
        # `{"errorCode":-1,"message":"请先完成课程学习，再进行问卷作答"}`。
        # 把平台的原话打出来，不然「读不到题目」看着像我们自己的毛病。
        why = str(d.get("message") or d.get("returnMessage") or "").strip()
        why = why or f"回执里没有 topicList：{str(d)[:80]}"
        log(f"[exam] 问卷 {tp_id[:12]}… 打不开：{why}")
        stat["status"] = f"打不开：{why}"[:40]
        return stat
    rec = []
    for t in topics:
        if not isinstance(t, dict):
            continue
        o = pick_option(t)
        row = {"topicId": str(t.get("id") or "")}
        if o:
            row["optionIdArr"] = [o.get("id")]
        else:
            # 没有选项 = 问答题。实测这份问卷全是单选，这一支只是别让
            # 程序在某门课的问卷上直接崩掉。
            row["answersStr"] = "无"
        rec.append(row)
    if dry_run:
        log(f"[exam] （dry-run）问卷 {len(topics)} 道题，会这样答：")
        for t, row in zip(topics, rec):
            got = (row.get("optionIdArr") or [""])[0]
            text = next((o.get("content") for o in (t.get("optionList") or [])
                         if o.get("id") == got), row.get("answersStr", ""))
            log(f"          {str(t.get('title'))[:34]:36s} → {text}")
        return stat
    bean = {"id": str((d.get("questionnaire") or {}).get("id") or tp_id),
            "classId": str((d.get("questionnaire") or {}).get("classId") or class_id),
            "recordArr": rec}
    out = sess.q_submit(bean)
    ok = str(out.get("responseCode") or "") == "SUCCESS" or out.get("errorCode") == 0
    stat["done"] = bool(ok)
    stat["status"] = str(out.get("message") or out)[:40]
    log(f"[exam] {'✓' if ok else '✗'} 问卷提交：{stat['status']}")
    return stat


def _back_to_courseware(sess: desktop.Session) -> None:
    """做完考核把页面放回课件页 —— 别把浏览器停在考核页上。

    为什么在意这个：考核页的倒计时（`doExpireConfig.leftSeconds`）一直在跑，
    停在上面既没用又可能过期；而且下一次进来（或者用户自己看）时，
    停在"课件"页最不突兀。
    """
    try:
        sess.open_courseware()
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="桌面版考核 / 问卷")
    ap.add_argument("--list", action="store_true", help="只列出考核和问卷，不动手")
    ap.add_argument("--course", default="", help="只做名字里含这几个字的课")
    ap.add_argument("--max-courses", type=int, default=0, help="最多做几门")
    ap.add_argument("--dry-run", action="store_true", help="只报准备做什么，不交卷")
    ap.add_argument("--questionnaire-only", action="store_true", help="只补问卷")
    ap.add_argument("--exam-only", action="store_true", help="只做考核")
    ap.add_argument("--harvest-only", action="store_true",
                    help="只收平台公布的答案（不交卷）—— 开跑前先收一遍最划算")
    ap.add_argument("--finish", action="store_true",
                    help="三件事都齐了之后申请结课（updateCourseFinish）。"
                         "默认**不做** —— 结课会把课程归档发证书，之后就刷不了分了")
    ap.add_argument("--restart", action="store_true",
                    help="先重启浏览器（**会丢登录态**，非必要别用）")
    ap.add_argument("--port", type=int, default=9222, help="CDP 端口")
    ap.add_argument("--login-wait", type=float, default=600.0,
                    help="等用户登录的秒数（0 = 不等）")
    args = ap.parse_args(argv)

    if desktop.LOCK_FILE.exists():
        log(f"[exam] 有程序正在用浏览器（{desktop.LOCK_FILE.name}）"
            "—— 先等它跑完再动考核")
        return 3

    cache = load_cache()
    # `Session` 的 `__init__` 只收 `log`，端口是属性（`desktop_watch.py` 也这么设）。
    sess = desktop.Session(log=log)
    sess.port = args.port
    try:
        if not sess.open(restart=args.restart, wait_login=args.login_wait):
            log("[exam] 没进到登录后的页面，先停下")
            return 2        # 课程列表只在「我的学习」那一页上。**不能拿地址栏里有没有
        # `personalCenter` 当判据** —— 个人中心是同一个地址换 `module=`
        # 参数（`module=learning` / `questionnaire` / `order` / `certificate`），
        # 实测踩过：上一趟探问卷把标签页停在 `module=questionnaire` 上，
        # 地址里照样有 `personalCenter`，于是守卫放行 → `COURSES_JS`
        # 在那一页找不到 `div.my-study.__vue__`，回 `'null'` →
        # 日志只有干巴巴一句「读不到课程列表」，白查半天。
        #
        # 所以判据改成**先试着读，读不出来才导航** —— 不管地址长什么样，
        # 能不能读到课程才是唯一可信的信号。
        #
        # ⚠ 但"读得到"不等于"读的是新的"：`Session.goto()` 见地址已经是目标页
        # 就直接返回，而 Vue 手里那份 `courseList` 还是**上一次进页面时**的
        # 接口结果。2026-10-08 实测踩到 —— `--finish` 已经把这 6 门课都申请
        # 结课成功了（服务端 `finishCourseDate` 都是刚写上的），再跑一次脚本
        # 时页面上却还写着「申请结课」，因为这个标签页从上一轮起就没重新加载。
        # 拿陈旧数据判"要不要动手"，比多花 9 秒强制加载一次危险得多。
        sess.goto(desktop.PERSONAL, settle=9.0, tries=2, force=True)
        courses = sess.courses(wait=20.0, log=log)
        if not courses:
            log("[exam] 读不到课程列表")
            return 2
        if args.course:
            courses = [c for c in courses if args.course in (c.get("name") or "")]
        if args.max_courses:
            courses = courses[:args.max_courses]
        if not courses:
            log("[exam] 没有匹配的课")
            return 0

        todos = []
        for i, course in enumerate(courses, 1):
            log("=" * 78)
            log(f"[exam] ({i}/{len(courses)}) {(course.get('name') or '')[:50]}")
            log(f"[exam] 平台说：{course.get('desc') or course.get('userClassScoreDesc') or '—'}")
            log("=" * 78)
            if not sess.enter_course(course.get("id", "")):
                log("[exam] 进不去这门课，跳过")
                continue
            exams = sess.homework_list()
            log(f"[exam] 考核 {len(exams)} 项")
            for item in exams:
                log(f"        · {item.get('title')}  状态{item.get('homeworkStatus')}  "
                    f"{item.get('score')} 分  可重做 {item.get('allowRedoNum')} 次  "
                    f"答案显示 {item.get('answerShowType')}")
            todos.append((course, exams))
            sess.close_extra_tabs()

        if args.list:
            # `--list` 是"只看不动手"：连问卷清单也只看一眼，然后就走。
            log("")
            log("——— 问卷（只看）———")
            sess.goto(desktop.PERSONAL, settle=9.0, tries=2)
            pending = {str(x.get("classId") or ""): x for x in sess.q_list(1)}
            for course, _ in todos:
                it = pending.get(str(course.get("id") or ""))
                log(f"  {'待交' if it else '已交/没有'}  {(course.get('name') or '')[:44]}")
            return 0

        # ⚠ 顺序不能反：**考核必须先做**。
        #
        # 2026-10-08 实测，在还没通过考核的课上请求问卷详情会被服务端顶回来：
        #     {"errorCode":-1,"message":"请先完成课程学习，再进行问卷作答"}
        # （肝胆 20 分时被拒；重症 90 分时同一份问卷交得进去。）
        # 所以先跑考核这一趟（课程域），再回到 elearning 跑问卷那一趟。
        #
        # 另外两趟各自待在自己的域上，别来回跨域导航 —— 每跨一次就是一次
        # 整页重载，而整页重载会打断正在加载的接口。
        stats = []
        if not args.questionnaire_only:
            log("")
            log("——— 考核 ———" + ("（只收答案，不交卷）" if args.harvest_only else ""))
            for course, exams in todos:
                if not exams:
                    continue
                if not sess.enter_course(course.get("id", "")):
                    log("[exam] 进不去这门课，跳过考核")
                    continue
                for item in exams:
                    stats.append(do_exam(sess, course, item, cache,
                                         dry_run=args.dry_run,
                                         harvest_only=args.harvest_only))
                sess.close_extra_tabs()

        # `--harvest-only` 收完就走，不碰问卷（那一趟要考核 ≥60 才放行）。
        if args.harvest_only:
            log("")
            log(f"[exam] 题库缓存现在有 {len(cache.get('questions') or {})} 道题"
                f"（{desktop.LOCK_FILE.name} 之外，存在 data/{CACHE_NAME}）")
            if stats:
                log("")
                for s in stats:
                    log(f"  {s['course']:<30} {s.get('status') or ''}")
            return 0

        if not args.exam_only:
            log("")
            log("——— 问卷 ———")
            # 问卷接口只在 elearning 域上有，而且要先回到「我的学习」。
            sess.goto(desktop.PERSONAL, settle=9.0, tries=2)
            pending = {str(x.get("classId") or ""): x for x in sess.q_list(1)}
            log(f"[exam] 未参加问卷 {len(pending)} 份")
            for course, _ in todos:
                it = pending.get(str(course.get("id") or ""))
                if not it:
                    log(f"[exam] 「{(course.get('name') or '')[:28]}」的问卷不在待办里"
                        "（要么已交过，要么这门课没问卷）")
                    continue
                stats.append(do_questionnaire(sess, course, it, dry_run=args.dry_run))

        # ——— 结课 ———
        #
        # 这一步**默认不跑**（要显式 `--finish`）。理由：看视频 / 考核 / 问卷
        # 三件事都是"把该学的学完"，而"申请结课"是把这门课**归档**、发证书 ——
        # 一旦结课，平台那边就是已结课状态（`isFinishCourse=true` 带日期），
        # 想再回头刷高考核分数就没机会了。所以留给用户自己决定。
        #
        # 注意这一步和上面两趟**不能混**：它读的是 `courseList` 里的
        # `isFinishCourse`，而那个字段（连同 `userClassScoreDesc`/`isPass`）
        # 是**服务端算好的快照、会滞后** —— 实测刚交完问卷那几门课上仍然是
        # "未完成问卷调查"、`isPass='0'`，但问卷接口那边 `selectType=2`
        # 已经明明白白列着 `isSubmit: true`。所以**不能拿 desc 当门禁**，
        # 直接发请求、让服务端自己判（它回 `请先完成问卷调查` 就说明还没齐）。
        if args.finish:
            log("")
            log("——— 结课 ———" + ("（只看，不发请求）" if args.dry_run else ""))
            sess.goto(desktop.PERSONAL, settle=9.0, tries=2)
            for course, _ in todos:
                cname = (course.get("name") or "")[:30]
                cid = str(course.get("id") or "")
                if course.get("isFinish"):
                    log(f"[exam] 「{cname}」已经结课了"
                        f"（{course.get('finishDate') or ''}）")
                    continue
                if args.dry_run:
                    log(f"[exam] （dry-run）「{cname}」还没结课，会发 "
                        f"updateCourseFinish courseId={cid}")
                    continue
                res = sess.finish_course(cid)
                if res.get("ok"):
                    log(f"[exam] ✓ 「{cname}」结课申请成功")
                    stats.append({"course": cname, "title": "结课", "done": True,
                                  "score": None, "status": "已结课"})
                else:
                    log(f"[exam] 「{cname}」结课没成：{res.get('msg') or res.get('raw')}")
                    stats.append({"course": cname, "title": "结课", "done": False,
                                  "score": None,
                                  "status": str(res.get("msg") or "结课失败")[:18]})

        if stats:
            log("")
            log(f"{'课程':<30} {'项目':<18} {'分数':<6} 结果")
            log("-" * 74)
            for s in stats:
                log(f"{s['course']:<30} {s['title']:<18} "
                    f"{str(s.get('score') if s.get('score') is not None else '—'):<6} "
                    f"{'✓ 过' if s['done'] else s.get('status') or '没做完'}")
    except desktop.NotLoggedIn as exc:
        # 「没登录」不是程序坏了 —— 是要人去那个浏览器窗口里登一次。
        # 所以给一句人话 + 一个专门的退出码，别摔 traceback（原来就是摔的）。
        log("")
        log("=" * 70)
        log(f"[exam] {exc}")
        log("在模拟器那个浏览器窗口里登录一次，再重新跑一遍就行。")
        log("=" * 70)
        return 4
    finally:
        save_cache(cache)
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
