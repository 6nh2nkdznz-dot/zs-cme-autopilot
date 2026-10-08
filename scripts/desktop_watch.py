# -*- coding: utf-8 -*-
"""桌面版（浏览器 + 电脑模式）看课程序。

用法::

    ZSCMEAutopilot.exe --run desktop_watch                  # 从第一门没学完的课开始
    ZSCMEAutopilot.exe --run desktop_watch --list           # 只看课程列表，不动手
    ZSCMEAutopilot.exe --run desktop_watch --course 老年认知  # 只跑名字里含这几个字的课
    ZSCMEAutopilot.exe --run desktop_watch --lessons 3      # 每门课最多看 3 讲
    ZSCMEAutopilot.exe --run desktop_watch --dry-run        # 只报准备做什么

## 它和手机版那条路的关系

手机版（微信 WebView）那条路**原样保留**，一行没动。这里是并行的另一条：
同一个平台、同一个账号，但走浏览器 + 电脑模式，**完全不开微信** ——
所以既没有封号风险，也不会碰到模拟器里那个「微信解码器崩掉」的坑。

代价是页面版式完全不同，所以坐标、`roi`、页面识别**全都是新的一套**，
在 `scripts/desktop.py` 里。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import desktop  # noqa: E402
import paths  # noqa: E402

#: 跑长任务时的独占锁。
#:
#: 为什么非要这么个东西（同一个错犯过两次，2026-10-08）：看课是**在浏览器
#: 那一个标签页里**跑的，一秒都不能被别的程序碰 —— 旁边随便一个探针
#: `sess.enter_course()` 一下，标签页就被导航走，正在播的 `<video>` 直接
#: 消失，这边只会看到 `页面上没有视频元素了`／`no-video`，看起来像平台的
#: 问题。第一次白丢 45 分钟，第二次白丢 34 分钟的正片进度。
#:
#: 所以：**进课时占锁，退出时放锁**；占不上就直接退出并打印是谁占着。
#: 探针脚本想动浏览器时应该先看这个文件在不在（`desktop.busy_hint()`）。
LOCK_FILE = paths.debug_dir() / "desktop_watch.lock"


def acquire_lock() -> bool:
    """占住独占锁。占上了返回 True；别人占着返回 False（并打印是谁）。"""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            info = LOCK_FILE.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            info = ""
        pid = info.split()[0] if info else ""
        # 进程早就没了却留着锁文件（上次被 Ctrl-C 掉），别一直卡着。
        if pid.isdigit() and not _alive(int(pid)):
            log(f"[watch] 清掉过期的锁（PID {pid} 已经不在了）")
            release_lock()
        else:
            log(f"[watch] 已经有一个看课程序在跑了（{info or '未知'}）")
            return False
    try:
        LOCK_FILE.write_text(f"{os.getpid()} {time.strftime('%H:%M:%S')}\n",
                             encoding="utf-8")
    except OSError as exc:
        log(f"[watch] 占锁失败（{exc}）")
        return False
    return True


def release_lock() -> None:
    """放锁。不是自己占着的时候也照删 —— 只在确认跑完/异常退出时调。"""
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def _alive(pid: int) -> bool:
    """这个 PID 还活着吗（只用于判断锁文件过没过期）。

    ⚠ **不能用 `os.kill(pid, 0)`**：Windows 上没有真信号，CPython 拿
    `TerminateProcess` 去凑，对**已经死掉的** PID 会抛
    `SystemError: <built-in function kill> returned a result with an
    exception set`（实测踩到，而且它不是 OSError，`except OSError` 接不住
    —— 于是第二个实例直接把别人的锁清掉、接着导航，把正在播的视频弄没了）。
    这里改问 Win32 `OpenProcess` + `GetExitCodeProcess`：打不开句柄、
    或者退出码不是 `STILL_ACTIVE(259)`，就算不在。
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        except Exception:  # noqa: BLE001 - 判生死宁可当它死了，别把锁清错
            return False
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - 问不出来就别清锁（保守）
        return True


#: 日志出口。`None` = 直接 `print`（命令行跑的时候）。
_sink: "Callable[[str], None] | None" = None


def set_log(sink: "Callable[[str], None] | None") -> None:
    """把日志改接到界面（或任何别的地方）。

    为什么要这个钩子：**界面里没有控制台**。这个脚本原来一律 `print`，
    命令行跑没问题，但从界面按钮跑起来那些输出就全进了虚空 —— 界面上
    只剩一句「运行中…」，跑得对不对、卡在哪一讲，一概看不见。

    接上之后界面上的日志面板就是它的 stdout；`None` 恢复成 `print`。
    """
    global _sink
    _sink = sink


def log(msg: str = "") -> None:
    if _sink is not None:
        try:
            _sink(msg)
            return
        except Exception:  # noqa: BLE001
            # 日志出口坏了不能把看课带崩：这是几小时的任务，为了「打一行
            # 字失败」把整门课停掉不划算。吞掉，退回 print。
            pass
    print(msg, flush=True)


def show_courses(sess: desktop.Session, *, only_unfinished: bool = False,
                 wait: float = 25.0) -> list[dict]:
    """把「我的学习」页的课程列出来。

    `wait`：页面是 Vue 懒加载的，导航过去之后课程列表要等接口回来才有。
    实测只等 9 秒会读到空列表（标题都对、`courseList` 还是 `[]`），
    所以这里轮询等一会儿再下结论。
    """
    sess.goto(desktop.PERSONAL, settle=9.0)
    courses = sess.courses(wait=wait, log=log)
    if not courses:
        log("[watch] 读不到课程列表 —— 页面结构可能变了，"
            "或者当前不在「我的学习」页")
        return []
    log("")
    log(f"{'课程':<50} {'视频':<8} {'考核':<8} 问卷")
    log("-" * 92)
    for c in courses:
        desc = c.get("desc") or ""
        video = "已完成" if "视频课件已完成" in desc else "未完成"
        exam = _between(desc, "考核", "分") or "?"
        quiz = "已完成" if "已完成问卷调查" in desc else "未完成"
        if only_unfinished and video == "已完成":
            continue
        log(f"{(c.get('name') or '')[:48]:<50} {video:<8} {exam + '分':<8} {quiz}")
    # 报总数时优先用**平台接口自己报的 `totalCount`**（`Session.course_total`），
    # 不是 `len(courses)`：分页没读全（网络抖动、页面改版）时后者会少报，
    # 而那正好会让人以为"这门账号下就这么多课"。用户 2026-10-08 要求的
    # 「总课程数为从网站上读取而非记录的数量」就是这个数 —— 它每次都是
    # 现问平台拿的，跟本地 `data/*.json` 里那些记录没有关系。
    total = getattr(sess, "course_total", None)
    if isinstance(total, int) and total != len(courses):
        log(f"共 {total} 门课（平台接口的 totalCount），这次读到 {len(courses)} 门")
    else:
        log(f"共 {len(courses)} 门课（平台接口读的，不是本地记录）")
    log("")
    return courses


def _between(text: str, left: str, right: str) -> str:
    """抠出 `left` 和 `right` 之间的那段（`考核90分` → `90`）。"""
    i = text.find(left)
    if i < 0:
        return ""
    j = text.find(right, i + len(left))
    return text[i + len(left):j if j >= 0 else len(text)].strip()


def watch_course(sess: desktop.Session, course: dict, *,
                 max_lessons: int = 0, dry_run: bool = False) -> dict:
    """看完一门课里所有没看完的讲次。返回统计。

    `course` 是「我的学习」列表里的一条（`desktop.Session.courses()` 的
    元素，带平台 `id`）。判据全部来自平台接口，**不看左侧列表的小圆点**：
    实测那个点是骗人的（服务端 `status=2` 的讲次，列表里照样是灰的
    `fa-circle-o`）。
    """
    stat = {"lessons": 0, "ok": 0, "bad": 0, "started": time.time(),
            "before": 0, "after": 0, "stopped": False}

    name = (course.get("name") or "")[:52]
    if not sess.enter_course(course.get("id", "")):
        log("[watch] 没能进这门课")
        stat["bad"] += 1
        stat["elapsed"] = time.time() - stat["started"]
        return stat
    time.sleep(3.0)

    # 课程页先落在「首页」。实测首页那个绿色的「继续学习」是**死按钮**
    # （点三次地址都不动，它是给手机版/小程序用的），能进课件的只有
    # 顶部导航的「在线学习」。
    if not sess.open_courseware():
        log("[watch] 没进到课件页，这门课跳过")
        stat["bad"] += 1
        stat["elapsed"] = time.time() - stat["started"]
        return stat

    todo = sess.pending()
    stat["before"] = len(todo)
    if not todo:
        log("[watch] 这门课的视频课件都学完了")
        stat["elapsed"] = time.time() - stat["started"]
        return stat

    log(f"[watch] 这门课还欠 {len(todo)} 讲: "
        + "、".join(f"第{i['n']}讲" for i in todo))
    for item in todo:
        # 用户点了「立即停止」：**讲次之间**也要看一眼，不能只在
        # `watch_video` 的轮询里看 —— 否则一讲的看护结束后还会自动
        # 切下一讲，用户看到的就是「点了停止它又开了新课」。
        if desktop.should_stop():
            log("[watch] 收到停止，这门课剩下的讲次不看了")
            stat["stopped"] = True
            break
        if max_lessons and stat["lessons"] >= max_lessons:
            log(f"[watch] 已看 {stat['lessons']} 讲，到上限了，停在这里")
            break
        if dry_run:
            log(f"[watch] （--dry-run）要看的是第 {item['n']} 讲 "
                f"{item['title'][:40]}（平台状态 {item['status']}）")
            stat["lessons"] += 1
            continue

        n = item["n"]
        log("")
        log(f"[watch] ── 第 {n} 讲 {item['title'][:44]} "
            f"（平台状态 {item['status']}）──")
        stat["lessons"] += 1

        # 一讲最多试两次：第一次切过去若播放器没认（`wrong-lesson`），
        # 说明整页载入那一跳没落地，再整页载入一次通常就好了。绝不能
        # 因为"播放器认的是别的讲"就跳到下一讲 —— 那正是 2026-10-08
        # 白跑 45 分钟的坑：平台整段时间都在替别的讲记账。
        result = "not-started"
        for attempt in (1, 2):
            if not sess.goto_lesson(item["item_id"]):
                log(f"[watch] 第 {n} 讲切不过去（第 {attempt} 次）")
                result = "no-lesson"
                continue
            sess.hook()
            # 装"谁按了暂停"的监听再起播 —— 页内助推会把视频捞起来，
            # 第一次暂停一定是平台自己干的，得当场把栈留下来。
            if sess.arm_snitch() == "armed":
                log("[watch] 已挂上暂停取证")
            sess.boost()
            # ⚠ **起播必须走 `start_video()`，不能只 `play()`**：
            # 整页载入会清掉文档的"用户激活"，`play()` 的 Promise 直接
            # `NotAllowedError`（被拒绝不派发 pause，光看事件会以为
            # "平台把播放按停了"）。`start_video()` 会点一下 `<video>`
            # 再用 Promise 复核 —— 2026-10-08 第 10 讲就是在这儿白等的：
            # `cur` 恒为 63 秒，日志每 15 秒刷一行"重新播"，一次没播成。
            started = sess.start_video(tries=3)
            if not started:
                log(f"[watch] 第 {n} 讲起播失败（play(): "
                    f"{sess.play_result() or '无回执'}）")
                result = "no-start"
                continue
            log(f"[watch] 第 {n} 讲已经播起来了（play(): {sess.play_result()}）")
            result = sess.watch_video(item)
            if result != "wrong-lesson":
                break
            log(f"[watch] 第 {n} 讲播放器没认（第 {attempt} 次），重切一遍")

        if result == "done":
            stat["ok"] += 1
        elif result == "stopped":
            # 这**不是**「没看完」—— 是我们被用户叫停的。别算进 `bad`
            # （否则总账会报「有讲次没看完」，看着像平台出问题了），
            # 也别打「先跳到下一讲」，因为下一讲根本不会再开。
            stat["stopped"] = True
            log(f"[watch] 第 {n} 讲看到一半被叫停，这门课就停在这儿")
            break
        else:
            stat["bad"] += 1
            log(f"[watch] 第 {n} 讲没看完（{result}）—— 先跳到下一讲")

    # 收尾复核：跑完再问一次平台。**这一步不能省** —— 2026-10-08 第一次
    # 真跑就是被 `sessionStorage` 里上一讲残留的上报记录骗了，1.6 分钟
    # "看完"两讲，复核才发现一讲没动。
    left = sess.pending()
    stat["after"] = len(left)
    if stat["before"] and not left:
        log(f"[watch] ✓ 复核对上了：{name} 的视频课件现在全部学完")
    elif left:
        log(f"[watch] 复核：{name} 还剩 {len(left)} 讲没学完（"
            + "、".join(f"第{i['n']}讲" for i in left) + "）")
    stat["elapsed"] = time.time() - stat["started"]
    return stat


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="桌面版（浏览器 + 电脑模式）看课 —— 不开微信")
    ap.add_argument("--list", action="store_true", help="只列课程，不动手")
    ap.add_argument("--course", default="", help="只跑名字里含这几个字的课")
    ap.add_argument("--lessons", type=int, default=0,
                    help="每门课最多看几讲（0 = 不限）")
    ap.add_argument("--max-courses", type=int, default=0,
                    help="最多处理几门课（0 = 不限）")
    ap.add_argument("--dry-run", action="store_true", help="只报准备做什么")
    ap.add_argument("--restart", action="store_true", help="先重启浏览器")
    ap.add_argument("--port", type=int, default=0, help="调试端口（默认 9222）")
    ap.add_argument("--login-wait", type=int, default=600,
                    help="没登录时等多久（秒），0 = 不等直接报错")
    args = ap.parse_args(argv)

    # 拿独占锁再碰浏览器（见 LOCK_FILE 的说明：边上随便一个探针导航一下，
    # 正在播的视频就没了，而日志只会说「页面上没有视频元素了」）。
    if not acquire_lock():
        return 3

    sess = desktop.Session(log=log)
    try:
        sess.open(restart=args.restart, wait_login=float(args.login_wait))
    except desktop.NotLoggedIn as exc:
        # 「没登录」不是程序坏了 —— 是要人去那个浏览器窗口里登一次。
        # 所以给一句人话 + 一个专门的退出码，别摔 traceback（原来就是摔的）。
        log("")
        log("=" * 70)
        log(f"[watch] {exc}")
        log("在模拟器那个浏览器窗口里登录一次，再重新跑一遍就行。")
        log("=" * 70)
        release_lock()
        return 4
    except Exception as exc:  # noqa: BLE001 - 开不起来要给出人话
        log("")
        log(f"[watch] 起不来: {exc}")
        release_lock()
        return 2

    try:
        log(f"[watch] 登录态正常，开始（浏览器里现在还停在原页面）")
        courses = show_courses(sess)
        if not courses:
            return 1
        if args.list:
            return 0

        # 缺视频的优先。**一门课要不要跑只看视频这一项**：平台的结课要求
        # 是三项（视频 + 考核≥60 + 问卷），考核和问卷是后面另外的功能，
        # 这里先把视频这条腿走完。
        #
        # 判据用平台现算的 `userClassScoreDesc`（**不是本地记录**）：
        # `/user/getMyCourseList` 每次都会重新算这句话，写着「视频课件已完成」
        # 就是真学完了，不用再点进去。这一条也决定了下面「准备看 N 门课」
        # 里的 N —— 它就是"网站上还剩几门要看"。
        todo = [c for c in courses
                if (not args.course or args.course in (c.get("name") or ""))
                and "视频课件已完成" not in (c.get("desc") or "")]
        if args.max_courses:
            todo = todo[:args.max_courses]
        if not todo:
            log(f"[watch] 平台说这 {len(courses)} 门课的视频课件都已经学完了"
                f" —— 没有需要看的课")
            return 0

        log("")
        log(f"[watch] 准备看 {len(todo)} 门课:")
        for c in todo:
            log(f"        · {(c.get('name') or '')[:60]}")

        total = {"lessons": 0, "ok": 0, "bad": 0}
        stopped = False
        for idx, c in enumerate(todo, 1):
            # 讲次之间停过一次，就**别再开下一门课**：用户点的是「立即停止」，
            # 不是「上完这节再停」。
            if desktop.should_stop():
                log("[watch] 收到停止，剩下的课不看了")
                stopped = True
                break
            log("")
            log("=" * 78)
            log(f"[watch] ({idx}/{len(todo)}) {(c.get('name') or '')[:60]}")
            log("=" * 78)
            stat = watch_course(sess, c, max_lessons=args.lessons,
                                dry_run=args.dry_run)
            for k in total:
                total[k] += stat.get(k, 0)
            stopped = stopped or bool(stat.get("stopped"))
            log(f"[watch] 这门课: 看了 {stat['lessons']} 讲，"
                f"完成 {stat['ok']}，没看完 {stat['bad']}，"
                f"用时 {stat.get('elapsed', 0) / 60:.1f} 分钟")
            # 用户要求过别攒标签页（"不然 cookie 都保存不下来"）。每门课
            # 收一次，留着自己这张 —— 再开课会在同一个标签页里导航。
            sess.close_extra_tabs()

        if stopped:
            log("")
            log("=" * 78)
            log(f"[watch] 已停止: 共看了 {total['lessons']} 讲，"
                f"完成 {total['ok']}，没看完 {total['bad']}")
            log("[watch] 看过的那部分平台已经记账了，下次跑会从没看完的接着来")
            # 被用户叫停不是失败，返回 0 —— 否则界面会把一次正常的停止
            # 显示成「任务出错」。
            return 0

        log("")
        log("=" * 78)
        log(f"[watch] 全部结束: 共 {total['lessons']} 讲，"
            f"完成 {total['ok']}，没看完 {total['bad']}")
        return 0 if total["bad"] == 0 else 1
    finally:
        sess.close()
        release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
