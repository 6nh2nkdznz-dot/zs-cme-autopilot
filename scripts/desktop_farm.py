# -*- coding: utf-8 -*-
"""刷学习时长：反复看同一讲，把「总计时长」刷到 90 分钟。

用法::

    ZSCMEAutopilot.exe --run desktop_farm                # 刷到 90 分钟
    ZSCMEAutopilot.exe --run desktop_farm --target 120   # 刷到 120 分钟
    ZSCMEAutopilot.exe --run desktop_farm --course 老年认知
    ZSCMEAutopilot.exe --run desktop_farm --dry-run      # 只报现在多少分钟

## 为什么要这个

平台的结课要求是三项（视频课件全部学完 + 考核 ≥60 分 + 问卷调查），
但**「学习时长」是单独一项统计**，显示在视频上方那行：

    本次学习 00分07秒     总计时长 73分11秒

★ 这条数是**按课**记的，不是账号级。2026-10-08 实测：同一账号在
「基层医疗实践中的心理精神进展」上是 4391 秒（73分11秒），在
「三维超声心动图」上是 2220 秒（37分00秒）—— 两处的 DOM 文字都和
各自的 `learnRecordObj.totalTime` 对得上。

（最初按用户 m18611 的说法做成"账号级"，所以日志里写着"刷哪门都一样"；
实测推翻了这个前提。想让每门课都到 90 分钟得加 `--all-courses`。）

所以刷的时候**只循环当前这一讲**就够了 —— 时长是按课记的，跟哪一讲无关。

## 怎么刷

一条视频播完，平台本来会弹：

    该视频课件已观看完毕，是否继续学习下一课程节点？
    ［学习下一课节］［取消］

刷时长的做法是：**点「取消」留在这一讲，再把视频倒回 0 秒重播**，如此循环。
所以整件事只有三个动作：

    读时长 → 有确认框就点「取消」 → 视频停了就倒带重播

## 时长是怎么算出来的

`learnRecordObj` 上有两个数（AngularJS scope，实测可读）：

* `totalTime` —— **这门课**的总计时长（服务端记的），就是视频上方那个「总计时长」；
* `learnTime` —— **本次学习**（浏览器本地攒着、还没上报的部分）。

`CourseLearnTimeService.js`（平台自己的客户端，仅 1342 字节）里写得很清楚：
每 1000ms `addLearnTime()`，攒够 `defaultIntervalTime = 300` 秒才
`sendLearnTime()` 上报一次，上报成功就 `resetLearnTime()`。因此：

* 判「刷够了没有」**只看 `totalTime`**（服务端口径，最权威）；
* 显示进度时用 `totalTime + learnTime` —— 否则每 5 分钟里那 4 分多钟
  进度条纹丝不动，看着像卡死了。

## 一条实测的坑

刷时长期间**不能导航**。换讲、进别的课都会触发平台的 `stopAccumulate()`
（`isLearning = false` 然后立刻冲刷上报），正在攒的那几分钟就断了；
更要命的是边上随便一个探针脚本点一下，正在播的 `<video>` 直接消失。
所以这里和看课共用同一把独占锁（`desktop_watch.LOCK_FILE`）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop  # noqa: E402
import desktop_watch as watch  # noqa: E402

#: 目标：总计时长刷到多少分钟。用户要的是 90（m18611）。
TARGET_MINUTES = 90.0

#: 多久看一眼。
POLL_SECONDS = 10.0

#: 连续这么多轮「总计时长一动没动」才判定卡死。
#: 10 秒一轮 → 5 分钟。留这么宽是因为上报**每 300 秒一次**：
#: 刚好卡在上报边界上时，`totalTime` 确实会有好几分钟纹丝不动。
STALL_ROUNDS = 30

#: 安全阀：一轮最多刷这么久，防止无人值守时无限跑下去。
MAX_HOURS = 8.0

# 日志出口用看课那套（界面里没有控制台，直接 print 会进虚空）。
log = watch.log
set_log = watch.set_log
release_lock = watch.release_lock


def fmt(seconds: float) -> str:
    """`4391` → `1小时13分11秒`。给人看的。"""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}小时{m:02d}分{sec:02d}秒"
    return f"{m}分{sec:02d}秒"


def ensure_on_courseware(sess: desktop.Session, course: dict, *,
                         dry_run: bool = False) -> bool:
    """进到这门课的课件页，并确保播放器已经认了某一讲。

    返回 `False` 表示进不去（调用方别硬刷 —— 在「我的学习」页上刷不出时长，
    那个页面根本没有 `<video>`）。

    **挑哪一讲**：优先挑这门课里还没学完的第一讲。用户说的是「循环当前
    这一讲即可」（时长是按课记的，刷哪一讲都一样），但既然一样，顺手挑
    没看完的那一讲更好 —— 时长刷够了，那几讲也跟着学完了，不用再跑一遍
    看课任务。整门课的视频都学完了才退回第一讲。
    """
    name = (course.get("name") or "")[:52]
    log(f"[farm] 进课: {name}")
    if not sess.enter_course(course.get("id", "")):
        log("[farm] 没能进这门课")
        return False
    time.sleep(3.0)

    got = sess.items()
    if not got:
        log("[farm] 这门课读不到讲次列表")
        return False
    todo = [i for i in got if not i["done"]]
    pick = (todo or got)[0]
    log(f"[farm] 循环第 {pick['n']} 讲「{pick['title'][:40]}」"
        f"（{'还没学完' if todo else '这门课的视频都学完了，随便挑一讲'}）")

    if not sess.open_courseware(pick["item_id"]):
        log("[farm] 没进到课件页")
        return False

    al = sess.active_lesson()
    if not al.get("id"):
        log("[farm] 到课件页了，但播放器还没认讲次")
        return False
    if str(al.get("id")) != str(pick["item_id"]):
        # `goto_lesson` 里已经核过一遍，这里是二道保险 —— 播放器认错讲
        # 的后果是平台替**别的那一讲**记账，从外面看就是「服务端不记账」。
        log(f"[farm] ⚠ 播放器认的是 {str(al.get('id'))[:12]}…，"
            f"不是要刷的 {str(pick['item_id'])[:12]}…，重切一次")
        if not sess.goto_lesson(pick["item_id"]):
            log("[farm] 切不过去")
            return False
    log("[farm] 播放器已就位")
    if dry_run:
        return True
    sess.hook()
    return True


def farm(sess: desktop.Session, *, target_seconds: float,
         poll: float = POLL_SECONDS, max_seconds: float = MAX_HOURS * 3600,
         dry_run: bool = False) -> str:
    """把账号的总计时长刷到 `target_seconds`。返回结束原因，**不抛异常**。

    返回值：`reached`（刷够了）/ `stopped`（用户点了停止）/ `no-video` /
    `stalled`（几轮都没动静）/ `timeout`（到安全阀了）。
    """
    started = time.monotonic()

    st = sess.study_time()
    if st["total"] < 0:
        log("[farm] 读不到「总计时长」—— 页面可能不在课件页上")
        return "no-video"
    begin = st["total"]
    need = max(0, int(target_seconds) - begin)
    log("")
    log("=" * 72)
    log(f"[farm] 现在总计时长 {fmt(begin)}（{begin} 秒），"
        f"目标 {fmt(target_seconds)}，还差 {fmt(need)}")
    log(f"[farm] 平台每 5 分钟才上报一次，所以进度条会一跳一跳的，"
        f"不是卡住了（每 {poll:.0f} 秒看一次）")
    log("=" * 72)
    if dry_run:
        log(f"[farm] （--dry-run）真要刷的话，大概要 {need / 60:.0f} 分钟")
        return "reached"
    if begin >= target_seconds:
        log("[farm] 已经够了，不用刷")
        return "reached"

    last_total = begin
    last_cur = -1.0
    stall = 0
    replayed = 0
    round_no = 0

    while True:
        if desktop.should_stop():
            log("[farm] 收到停止")
            return "stopped"
        elapsed = time.monotonic() - started
        if elapsed > max_seconds:
            log(f"[farm] 到安全阀了（{max_seconds / 3600:.1f} 小时），收工")
            return "timeout"

        round_no += 1
        st = sess.study_time()
        al = sess.active_lesson()
        cur = float(al.get("cur") or 0.0)
        dur = float(al.get("dur") or 0.0)

        # 进度显示用 `total + learn`：`learn` 是还没上报的那部分，
        # 加上它进度条才会平滑地走，否则每 5 分钟才动一次。
        shown = st["total"] + max(0, st["learn"])
        if st["total"] >= target_seconds:
            log("")
            log("=" * 72)
            log(f"[farm] ✓ 刷够了：总计时长 {fmt(st['total'])}"
                f"（平台 {st['total_text'] or st['total']}）")
            log(f"[farm] 一共刷了 {fmt(st['total'] - begin)}，"
                f"重播 {replayed} 次，用时 {fmt(elapsed)}")
            log("=" * 72)
            return "reached"

        # ---- 1) 弹框就点「取消」留在这一讲 ----
        # 视频一播完平台就弹「该视频课件已观看完毕，是否继续学习下一课程节点？」。
        # **必须点「取消」**，点「学习下一课节」当场跳到下一讲，这一讲就白循环了。
        sess.dismiss_dialog()

        # ---- 2) 视频弹题（要答题的那种）顺手答掉 ----
        # 不答的话平台会每几秒把视频按停一次，时长就攒不动了。
        if al and sess.quiz():
            sess.answer_popup()

        # ---- 3) 视频停了就倒带重播 ----
        if not al.get("id") and not al.get("dur"):
            log("[farm] 页面上读不到视频 —— 可能被导航走了")
            return "no-video"

        # 三种"停了"都要管：paused（平台按停）、cur 贴到 dur（播完了，
        # 有时 `ended` 还没翻上来）、以及刚进来时根本没起播。
        stopped = bool(al.get("paused")) or (dur > 0 and cur >= dur - 1.0)
        if stopped:
            how = "播完了" if dur > 0 and cur >= dur - 1.0 else "被按停了"
            what = sess.rewind_and_play()
            if what != "ok":
                # 倒带失败（整页重载后 `play()` 可能被拒），走点按兜底。
                log(f"[farm] 倒带返回 {what}，改用点按起播")
                sess.start_video(tries=3)
            replayed += 1
            log(f"[farm] {how} → 倒回 0 秒重播（第 {replayed} 次）")

        # ---- 4) 卡死判定 ----
        # 判据是 **`totalTime` 和播放位置双双不动**。只看 `total` 会误判：
        # 上报周期是 300 秒，中间几轮它本来就该不动。
        moved = st["total"] != last_total or abs(cur - last_cur) > 0.5
        if moved:
            stall = 0
            if st["total"] != last_total:
                log(f"[farm] 平台记账了：总计时长 {fmt(st['total'])}，"
                    f"还差 {fmt(target_seconds - st['total'])}")
        else:
            stall += 1
            if stall >= STALL_ROUNDS:
                log(f"[farm] 连续 {stall} 轮（约 "
                    f"{stall * poll / 60:.0f} 分钟）时长和播放位置都没动")
                log(f"[farm] 现在 {fmt(shown)}，还差 {fmt(target_seconds - shown)}")
                return "stalled"
        last_total = st["total"]
        last_cur = cur

        if round_no % 6 == 1:
            log(f"[farm] 进度 {fmt(shown)} / {fmt(target_seconds)}"
                f"（{shown / target_seconds * 100:.1f}%），"
                f"本讲 {cur:.0f}/{dur:.0f} 秒，第 {replayed} 次重播")

        time.sleep(poll)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="刷学习时长 —— 反复看同一讲，把「总计时长」刷够")
    ap.add_argument("--target", type=float, default=TARGET_MINUTES,
                    help=f"刷到多少分钟（默认 {TARGET_MINUTES:.0f}）")
    ap.add_argument("--course", default="", help="只刷名字里含这几个字的课")
    ap.add_argument("--all-courses", action="store_true",
                    help="每一门课都刷一遍（★ 总计时长是**按课**记的，见 "
                         "desktop.Session.study_time() 的注释；不加这个开关"
                         "就只刷第一门）")
    ap.add_argument("--max-courses", type=int, default=0,
                    help="配合 --all-courses：最多刷几门（0 = 不限）")
    ap.add_argument("--poll", type=float, default=POLL_SECONDS,
                    help=f"多久看一眼，秒（默认 {POLL_SECONDS:.0f}）")
    ap.add_argument("--max-hours", type=float, default=MAX_HOURS,
                    help=f"**每门课**最多刷几小时（默认 {MAX_HOURS:.0f}）")
    ap.add_argument("--dry-run", action="store_true", help="只报现在多少分钟")
    ap.add_argument("--restart", action="store_true", help="先重启浏览器")
    ap.add_argument("--port", type=int, default=0, help="调试端口（默认 9222）")
    ap.add_argument("--login-wait", type=int, default=600,
                    help="没登录时等多久（秒），0 = 不等直接报错")
    args = ap.parse_args(argv)

    # 和看课共用一把锁：刷时长期间**一秒都不能被别的程序碰**
    # （边上随便一个探针导航一下，正在播的视频就没了）。见 LOCK_FILE 的注释。
    if not watch.acquire_lock():
        return 3

    sess = desktop.Session(log=log)
    try:
        sess.open(restart=args.restart, wait_login=float(args.login_wait))
    except desktop.NotLoggedIn as exc:
        log("")
        log("=" * 70)
        log(f"[farm] {exc}")
        log("在模拟器那个浏览器窗口里登录一次，再重新跑一遍就行。")
        log("=" * 70)
        release_lock()
        return 4
    except Exception as exc:  # noqa: BLE001 - 开不起来要给出人话
        log("")
        log(f"[farm] 起不来: {exc}")
        release_lock()
        return 2

    try:
        courses = watch.show_courses(sess)
        if not courses:
            return 1
        todo = [c for c in courses
                if not args.course or args.course in (c.get("name") or "")]
        if not todo:
            log(f"[farm] 没有名字含「{args.course}」的课")
            return 1
        if not args.all_courses:
            if len(todo) > 1:
                log(f"[farm] 有 {len(todo)} 门课名字对得上，只刷第一门")
                log("[farm] ★ 「总计时长」是**按课**记的（同一账号在两门课上"
                    "实测读到 73分11秒 和 37分00秒），所以要每门都到 90 分钟"
                    "得加 --all-courses")
            todo = todo[:1]
        elif args.max_courses:
            todo = todo[:args.max_courses]

        done = 0
        bad: list[str] = []
        for idx, course in enumerate(todo, 1):
            if desktop.should_stop():
                log("[farm] 收到停止")
                break
            if len(todo) > 1:
                log("")
                log("=" * 72)
                log(f"[farm] ══ 第 {idx}/{len(todo)} 门 ══")
                log("=" * 72)
            if not ensure_on_courseware(sess, course, dry_run=args.dry_run):
                bad.append((course.get("name") or "")[:40])
                continue

            reason = farm(sess, target_seconds=args.target * 60.0,
                          poll=args.poll, max_seconds=args.max_hours * 3600,
                          dry_run=args.dry_run)
            if reason in ("reached", "stopped"):
                done += 1
                if reason == "stopped":
                    # 被用户叫停不是失败，返回 0 —— 否则界面会把一次正常的
                    # 停止显示成「任务出错」。已经刷进去的平台记着账。
                    log("[farm] 已经刷进去的那部分平台记着账，下次接着刷就行")
                    break
            else:
                bad.append((course.get("name") or "")[:40])

        if len(todo) > 1:
            log("")
            log(f"[farm] 这一轮：{done} 门刷够，{len(bad)} 门没成")
            for nm in bad:
                log(f"[farm]   ✗ {nm}")
        return 0 if done else 1
    finally:
        sess.close()
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
