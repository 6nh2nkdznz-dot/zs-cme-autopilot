# -*- coding: utf-8 -*-
"""桌面版（浏览器 + 电脑模式）的**结课三件事**里除视频课以外的两件：考核 + 问卷。

用法::

    MaaElearning.exe --run desktop_exam --list          # 只列考核和问卷，不动手
    MaaElearning.exe --run desktop_exam --course 肝胆    # 只做名字里含这几个字的课
    MaaElearning.exe --run desktop_exam --dry-run       # 只报准备做什么

## 为什么考核不是"看一眼就知道选哪个"

平台的结课要求（`classAssessmentDesc`）原文是：
**完成所有视频课件学习 + 考核 ≥60 分 + 完成问卷调查**。
视频那一件由 `desktop_watch.py` 负责，这里负责后两件。

考核走的是课程站的 `homework` 族接口（**不是** `testing`/`ks` ——
`queryTestingList` / `queryTestList` 对这门课全回空，2026-10-08 在这上面
绕了很久）。考核页在 `#!/index/course/learn/homework/do?homeworkId=…`。

真正的题库考核有单选/多选/判断，**答案只有平台知道**。所以策略是：

1. 先按题库缓存（`data/exam_answers.json`）填，没有的就先空着；
2. 交卷之后，若这门课 `answerShowType` 允许显示答案，就把平台自己给出的
   正确答案读回来存进缓存；
3. 用缓存重做一次（这门课允许重做 8 次）。

这不是"破解"——把答案显示给考生看、再允许重做，本来就是平台给的功能；
只是人做这件事要来回点很多次，程序做得快而已。**不做无限重试**：
每门课最多重做 `MAX_TRIES` 次，拿不到 ≥60 分就如实报出来，绝不硬刚。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop  # noqa: E402
import paths  # noqa: E402

#: 题库缓存落在 `data/`（跟手机版的 `answer_cache_path` 同一个目录，
#: 但**另一个文件** —— 手机版那份是按"题干文字"索引的四选一，
#: 格式不一样，混用会把两边的匹配都搞乱）。
CACHE_NAME = "exam_answers.json"

#: 及格线。平台原文就是「考核 ≥60 分」，写成常量免得各处抄错。
PASS_SCORE = 60

#: 一门课最多重做几次。**不是** `allowRedoNum`（平台给 8 次）——
#: 我们只在"能读到正确答案"的前提下重做才有意义，读不到就一次次瞎蒙，
#: 纯属浪费。两次够验证一轮「蒙错→读答案→重做」。
MAX_TRIES = 2


def log(msg: str) -> None:
    print(msg, flush=True)


def _cache_path() -> Path:
    return paths.data_dir() / CACHE_NAME


def load_cache() -> dict:
    """读题库缓存。文件坏了就当成空的（**绝不因为缓存坏了就不干活**）。"""
    p = _cache_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


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


def plan_for(course_key: str, questions: list, cache: dict) -> tuple[dict, int]:
    """按缓存给出这一卷的作答计划。返回 `(plan, 有几道没把握)`。

    `plan` 是 `{题目 id: 答案}`。答案的取值规则（从 `p()` 的定义反推，
    `scripts/desktop.py` 里 `HOMEWORK_FILL_JS` 的注释写了原文）：
    单选/判断 = 选项 `index`，多选 = `"0|2"` 这种升序拼接。

    没把握的题**照样给一个答案**（单选给第一个选项、判断给"是"）：
    空着交上去会被平台的 `checkAnswerModel()` 拦住不让提交，
    那就连"交一次看答案"这一步都做不成。
    """
    bucket = (cache.get(course_key) or {}) if isinstance(cache, dict) else {}
    plan: dict = {}
    unsure = 0
    for q in questions:
        qid = str(q.get("id") or "")
        if not qid:
            continue
        known = bucket.get(qid)
        if known not in (None, ""):
            plan[qid] = known
            continue
        unsure += 1
        plan[qid] = guess(q)
    return plan, unsure


def guess(q: dict) -> str:
    """没答案时给一个"最不坏"的默认：单选/判断选 A，多选选 A，主观写"无"。

    为什么单选默认选 A 而不是随机：平台的学习状态检测题自己就写着
    「如需继续学习，请选择A」，说明它的题库语境里 A 是中性的；
    更重要的是**要可复现** —— 随机选的话，同一个错题每轮答案都不同，
    缓存里的"正确答案"就永远对不上。
    """
    kind = q.get("kind")
    opts = q.get("options") or []
    if kind == "wenda":
        return "无"
    first = str((opts[0] or {}).get("index") or "") if opts else ""
    return first or "A"


def harvest_answers(course_key: str, form: dict, cache: dict) -> int:
    """把页面上**平台自己给出来的**正确答案收进缓存，返回收了几道。

    只在交卷之后、且这门课允许显示答案时才有得收。这里宽容地认几种
    可能的字段名（`rightAnswer` / `answer` / `correctAnswer`），
    因为**没实测过**这个字段长什么样 —— 宁可认不出来，也不要写死一个
    猜的字段名然后在别处当成事实用。
    """
    got = 0
    bucket = cache.setdefault(course_key, {})
    for q in form.get("questions") or []:
        qid = str(q.get("id") or "")
        if not qid or bucket.get(qid):
            continue
        for key in ("rightAnswer", "right_answer", "correctAnswer",
                    "correct_answer", "answer", "userAnswer"):
            val = q.get(key)
            if val not in (None, "", []):
                bucket[qid] = val if isinstance(val, str) else "|".join(map(str, val))
                got += 1
                break
    return got


def do_exam(sess: desktop.Session, course: dict, item: dict, cache: dict,
            *, dry_run: bool = False) -> dict:
    """做一门课的一次考核。返回统计。"""
    key = str(course.get("id") or course.get("name") or "")
    name = (course.get("name") or "")[:30]
    title = (item.get("title") or "")[:30]
    stat = {"course": name, "title": title, "tries": 0, "score": None,
            "status": "", "done": False}
    if item.get("homeworkStatus") == 3 and (item.get("score") or 0) >= PASS_SCORE:
        log(f"[exam] 「{title}」已经 {item.get('score')} 分，不用做")
        stat.update(score=item.get("score"), status="已批改", done=True)
        return stat

    for k in range(1, MAX_TRIES + 1):
        stat["tries"] = k
        if dry_run:
            log(f"[exam] （dry-run）会去进「{title}」考核页并交一次")
            return stat
        if not sess.homework_open(str(item.get("id") or "")):
            log(f"[exam] 进不去「{title}」的考核页，先放过")
            stat["status"] = "进不去"
            return stat
        form = sess.homework_form()
        log(f"[exam] 「{title}」第 {k} 次：{form.get('count', 0)} 道题"
            f"（{', '.join(sorted({q.get('kind', '') for q in form.get('questions') or []}))}）")
        plan, unsure = plan_for(key, form.get("questions") or [], cache)
        log(f"[exam] 有把握 {len(plan) - unsure} 道，没把握 {unsure} 道")
        sess.homework_answer(plan)
        got = harvest_answers(key, form, cache)
        if got:
            log(f"[exam] 从页面收了 {got} 道题的答案")
        res = sess.homework_submit()
        score = _score_of(res)
        log(f"[exam] 交卷回执：{str(res)[:160]}")
        if score is not None:
            stat["score"] = score
            stat["status"] = "已交"
            if score >= PASS_SCORE:
                stat["done"] = True
                log(f"[exam] ✓ 「{title}」{score} 分，过了")
                return stat
            log(f"[exam] 「{title}」{score} 分，没过（及格 {PASS_SCORE}）")
        # 交完卷去"查看"页收答案：那里才是平台把正确答案摊开的地方。
        after = sess.homework_form()
        got = harvest_answers(key, after, cache)
        if got:
            log(f"[exam] 从成绩页又收了 {got} 道题的答案")
        save_cache(cache)
        if not got and k >= 1:
            log(f"[exam] 收不到正确答案，再交一次也只是瞎蒙 —— 停在这儿")
            break
    return stat


def _score_of(res: dict) -> int | None:
    """从交卷回执里抠分数（字段名不确定，宽容地认几个）。"""
    if not isinstance(res, dict):
        return None
    for key in ("score", "tkScore", "showTkScore", "homeworkScore"):
        v = res.get(key)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    for key in ("homeworkObj", "result"):
        v = res.get(key)
        if isinstance(v, dict):
            deep = _score_of(v)
            if deep is not None:
                return deep
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="桌面版考核 / 问卷")
    ap.add_argument("--list", action="store_true", help="只列出考核和问卷")
    ap.add_argument("--course", default="", help="只做名字里含这几个字的课")
    ap.add_argument("--max-courses", type=int, default=0, help="最多做几门")
    ap.add_argument("--dry-run", action="store_true", help="只报准备做什么")
    ap.add_argument("--restart", action="store_true",
                    help="先重启浏览器（**会丢登录态**，非必要别用）")
    ap.add_argument("--port", type=int, default=9222, help="CDP 端口")
    ap.add_argument("--login-wait", type=float, default=600.0,
                    help="等用户登录的秒数（0 = 不等）")
    args = ap.parse_args(argv)

    lock = desktop.LOCK_FILE
    if lock.exists():
        log(f"[exam] 有程序正在用浏览器（{lock.name}）—— 先等它跑完再动考核")
        return 3

    cache = load_cache()
    # `Session` 的 `__init__` 只收 `log`，端口是属性（`desktop_watch.py` 也这么设）。
    sess = desktop.Session(log=log)
    sess.port = args.port
    try:
        if not sess.open(restart=args.restart, wait_login=args.login_wait):
            log("[exam] 没进到登录后的页面，先停下")
            return 2
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

        stats = []
        for i, course in enumerate(courses, 1):
            log("=" * 78)
            log(f"[exam] ({i}/{len(courses)}) {(course.get('name') or '')[:50]}")
            log("=" * 78)
            if not sess.enter_course(course.get("id", "")):
                log("[exam] 进不去这门课，跳过")
                continue
            exams = sess.homework_list()
            log(f"[exam] 考核 {len(exams)} 项")
            for item in exams:
                if args.list:
                    log(f"        · {item.get('title')}  "
                        f"状态{item.get('homeworkStatus')}  "
                        f"{item.get('score')} 分  可重做 {item.get('allowRedoNum')} 次")
                    continue
                stats.append(do_exam(sess, course, item, cache, dry_run=args.dry_run))
            sess.close_extra_tabs()

        if stats:
            log("")
            log(f"{'课程':<32} {'考核':<22} {'分数':<6} 结果")
            log("-" * 76)
            for s in stats:
                log(f"{s['course']:<32} {s['title']:<22} "
                    f"{str(s['score']):<6} {'✓ 过' if s['done'] else s['status']}")
    finally:
        save_cache(cache)
        sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
