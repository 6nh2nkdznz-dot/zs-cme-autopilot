# -*- coding: utf-8 -*-
"""桌面版（浏览器 + 电脑模式）看课逻辑的自测。

## 为什么单独测这一块

手机版和桌面版是**两套页面**，桌面版这条路上有好几个"错了也不报错、
只是静静地什么都不发生"的坑，全都是实测踩出来的，必须用测试钉住：

  1. 进课**不能点「去学习」** —— 那个按钮被一个卡死的 Element UI 遮罩
     （`opacity:0` + `pointer-events:auto`，铺满视口）吃掉所有点击，
     而且它是 `window.open` 式的。正确做法是直接把入口地址赋给
     `location.href`（`userEnterClass`，**elearning 域**）；
  2. 「学完没有」**只能看接口的 `status`**，不能看左侧列表的小圆点 ——
     实测服务端 `status=2` 的讲次，列表里照样是灰的 `fa-circle-o`；
  3. 平台**把倍速锁死在 1×**：1.25/1.5/1.75/2/2.5/4 全被立刻按回 1，
     拖进度条也不算（服务端按真实播放时长记账）—— 一讲 55 分钟就是
     要播 55 分钟，别再找加速的捷径了；
  4. 浏览器的标签页**一个都不能少**（关到 0 会让渲染进程一起退，
     只能 `am force-stop` 重启，会话 cookie 一起丢）。

这些约束写错了都不会抛异常，所以必须有测试钉子。

运行::

    python scripts\\test_desktop.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import desktop as D  # noqa: E402

PASS = 0
FAIL = 0
HERE = Path(__file__).resolve().parent


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}\n         期望 {want!r}\n         实际 {got!r}")


def check_true(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}  {extra}")


class FakeWS:
    """假的 CDP 连接：按正则匹配 `Runtime.evaluate` 的表达式给答案。

    `answers` 是 `[(正则, 返回值)]`，先命中先用；没命中返回 `None`。
    记录所有 `call()`，这样"发了哪些 CDP 命令、顺序对不对"也能断言。
    """

    def __init__(self, answers=None, calls=None) -> None:
        self.answers = list(answers or [])
        self.calls = list(calls or [])
        self.timeout = 40.0
        self.sock = None

    def evaluate(self, expression: str):
        self.calls.append(("Runtime.evaluate", expression))
        for pat, val in self.answers:
            if pat in expression:
                return val(expression) if callable(val) else val
        return None

    def call(self, method: str, **kw):
        self.calls.append((method, kw))
        return {"result": {}}

    def close(self) -> None:
        pass

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def _session(ws: FakeWS) -> D.Session:
    """造一个连着假标签页的会话（不碰浏览器、不碰框架）。"""
    sess = D.Session(log=lambda _m: None)
    sess.ws = ws
    sess.tab_id = "t1"
    return sess


def _lesson_tree() -> dict:
    """`queryCourseItemList` 的响应样本（照实测的字段抄的）。

    实测第 9 讲 `status=1`（看了一半）、第 10 讲 `status=0`（没看），
    后面跟着一个不是讲座的节点（考核），验证它会被过滤掉。
    """
    return {
        "returnCode": "S0000",
        "title": "肝胆肿瘤整合治疗与肝移植全程化管理护理新进展学习班",
        "chapterList": [{
            "title": "第一章", "wareTypeName": "章节/模块",
            "childList": [{
                "title": "1", "childList": [
                    {"id": "i1", "title": "01a.mp4", "wareTypeName": "讲座", "status": 2},
                    {"id": "i2", "title": "02a.mp4", "wareTypeName": "讲座", "status": 2},
                    {"id": "i9", "title": "09a.mp4", "wareTypeName": "讲座", "status": 1},
                    {"id": "i10", "title": "10a.mp4", "wareTypeName": "讲座", "status": 0},
                    {"id": "x1", "title": "本项目考核", "wareTypeName": "考试", "status": 0},
                ],
            }],
        }],
        "lastRecord": {"lastItemId": "i9"},
    }


def main() -> int:
    print("=" * 68)
    print(" 桌面版（浏览器 + 电脑模式）看课自测")
    print("=" * 68)

    print("\n[1] 常量与两个域名")
    check("平台主站", D.ELEARN, "https://elearning.zs-hospital.sh.cn")
    check("课程站（另一个域名）", D.COURSE, "https://course.zs-hospital.sh.cn")
    check_true("「我的学习」在平台域名下", D.PERSONAL.startswith(D.ELEARN))
    check_true("登录页在平台域名下", D.LOGIN.startswith(D.ELEARN))
    check("看满的阈值", D.TARGET_PERCENT, 97.0)

    print("\n[2] 【关键】进课走入口地址，不点「去学习」")
    url = D.Session.ENTER_URL.format("40288abd9d7032f1019dfca1e2ad772c")
    check_true("入口是 userEnterClass", "userEnterClass" in url, url)
    check_true("带平台课程 id", "courseId=40288abd" in url, url)
    check_true("带 sourcePage=2", "sourcePage=2" in url, url)
    check_true("入口是相对路径（要拼在 elearning 域上）",
               url.startswith("/"), url)
    # 这条断言钉住"域名别搞错"：拿它往课程站导航是 404（实测踩过，白绕一圈）
    check_true("入口【不是】课程站的路径",
               not url.startswith(D.COURSE), url)
    src = (HERE / "desktop.py").read_text(encoding="utf-8")
    check_true("desktop.py 里还有 enter_course", "def enter_course" in src)
    check_true("进课用 Page.navigate 到入口地址",
               "self.ws.call(\"Page.navigate\", url=entrance)" in src)
    check_true("enter_course 里核对 course_ok（会话没建起来就不算成功）",
               "and self.course_ok()" in src)

    print("\n[3] 标签页：挑活的、最后一张不能关")
    check("课程站分最高", D._tab_score(D.COURSE + "/x"), 40 + D._TAB_SCORE_BONUS)
    check("空白页次之", D._tab_score("about:blank"), 30)
    check("本站其它页再次之", D._tab_score(D.ELEARN + "/learning/home"), 20)
    check("别的站最低", D._tab_score("https://example.com/"), 5)
    check("浏览器内部页 0 分", D._tab_score("devtools://x"), 0)
    check_true("课程站排在平台站前面",
               D._tab_score(D.COURSE + "/x") > D._tab_score(D.ELEARN + "/x"))

    real_tabs = D.tabs
    try:
        D.tabs = lambda **_k: [{"id": "only"}]           # type: ignore[assignment]
        check("只剩一张标签页时拒绝关它", D.close_tab("only"), False)
        D.tabs = lambda **_k: [{"id": "a"}, {"id": "b"}]  # type: ignore[assignment]
        check_true("多张时才允许关（这里没有调试端口，关不成也返回 False 而不是抛）",
                   D.close_tab("a") in (True, False))
    finally:
        D.tabs = real_tabs                                # type: ignore[assignment]
    check_true("close_tab 明确挡住关到零（源码里有这段守卫）",
               "最后一次实测" in src or "最后一张不能关" in src)

    print("\n[4] 从地址里抠 courseId")
    sess = _session(FakeWS([("location.href", D.COURSE +
                             "/learning/student/studentIndex.action#"
                             "!/index/course/home?courseId=8a8f87339c05084a019e000d445e197a")]))
    check("抠出课程站 courseId", sess.course_id(),
          "8a8f87339c05084a019e000d445e197a")
    sess2 = _session(FakeWS([("location.href", D.PERSONAL)]))
    check("平台页上没有 courseId 时返回空串", sess2.course_id(), "")

    print("\n[5] 讲次状态只认接口的 status")
    sess = _session(FakeWS([
        ("location.href", D.COURSE + "/x?courseId=cid1"),
        ("queryCourseItemList", json.dumps(_lesson_tree())),
    ]))
    items = sess.items()
    check("只收「讲座」节点（考核被过滤掉）", len(items), 4)
    check("itemId 按接口给的顺序", [i["item_id"] for i in items],
          ["i1", "i2", "i9", "i10"])
    check("编号从 1 连续排", [i["n"] for i in items], [1, 2, 3, 4])
    check("status 原样带出", [i["status"] for i in items], [2, 2, 1, 0])
    check("status>=2 才算学完", [i["done"] for i in items],
          [True, True, False, False])
    check("没学完的只剩第 9、10 讲",
          [i["n"] for i in sess.pending()], [3, 4])

    print("\n[6] 接口失败时不猜")
    sess = _session(FakeWS([
        ("location.href", D.COURSE + "/x?courseId=cid1"),
        ("queryCourseItemList", json.dumps({"returnCode": "E0002"})),
    ]))
    check("E0002（没有当前选课）时 items() 返回空表", sess.items(), [])

    print("\n[7] 只有真到末尾才归零（半看的讲要接着播）")

    def _run_play(video_state: dict) -> str:
        """喂一个 `video()` 的返回值，返回 `play()` 最终发给页面的那段 JS。"""
        seen = {}

        def _capture(expr: str):
            seen["expr"] = expr
            return json.dumps({"rate": 2.0, "cur": 0.0, "dur": 100.0, "paused": False})

        # `ended: !!v.ended` 是 VIDEO_JS 里的独有字段，用它把两条 evaluate 分开
        sess = _session(FakeWS([
            ("ended:", json.dumps(video_state)),
            ("v.playbackRate", _capture),
        ]))
        sess.play()
        return seen.get("expr", "")

    # 实测第 9 讲播到 31.8% 停过一次：不归零，接着播能省下那 14 分钟
    js = _run_play({"dur": 3535.2, "cur": 1125.8, "paused": True, "ended": False})
    check_true("半看的讲【不】归零", "if (false)" in js, js[:150])
    js = _run_play({"dur": 3535.2, "cur": 3535.0, "paused": True, "ended": True})
    check_true("确实播到末尾了才归零", "if (true)" in js, js[:150])

    print("\n[8] 【关键】倍速拿不到 —— 平台把任何值都按回 1×")
    check_true("PLAY_JS 里倍速是个占位符（外面不给就不写死）", "__R__" in D.PLAY_JS)
    check("play() 默认就按 1× 播（试过的倍速全被平台按回）",
          D.Session.play.__kwdefaults__.get("rate"), 1.0)
    check("boost() 默认不碰倍速（rate=0 表示不设）",
          D.Session.boost.__kwdefaults__.get("rate"), 0.0)
    check_true("源码里写明了「拿不到倍速、1× 是唯一速度」这个实测结论",
               "1× 是唯一速度" in src or "桌面版拿不到倍速" in src)

    print("\n[9] 切讲次：判据是播放器认了它，不是地址栏")
    check_true("goto_lesson 里出现 location.hash",
               "location.hash" in src and "def goto_lesson" in src)
    check_true("不靠点左侧列表进讲（没有 click 第N讲）",
               "click_text(\"第" not in src)
    # 2026-10-08 踩的坑：改 hash 之后**地址栏换了、播放器没换**，于是
    # `goto()` 判"已经在目标页"直接返回，整页载入成了空操作 —— 切讲
    # 永远切不过去。所以整页载入那一条必须 force。
    check_true("goto/goto_route 有 force 参数（守卫会误判「已经在目标页」）",
               "force: bool = False" in src and "if not force and self._at(url)" in src)
    check_true("_load_lesson 整页载入时带 force=True",
               "route, settle=settle, force=True" in src)
    check_true("切讲成功与否看 active_lesson()（播放器认的），不是看地址",
               "_wait_active" in src
               and 'self.active_lesson().get("id") == item_id' in src)
    check_true("ACTIVE_JS 读 activeItemObj（平台记账围着它转）",
               "activeItemObj" in src or "activeItemObj" in D.ACTIVE_JS)

    print("\n[10] 看课过程的判据")
    check_true("watch_video 存在", "def watch_video" in src)
    check_true("先看服务端回执（last_record）",
               "self.last_record(item.get(\"item_id\", \"\"))" in src)
    check_true("last_record 按 itemId 过滤（不然会拿到上一讲的成绩）",
               "if item_id and _first(" in src)
    check_true("卡住时先怀疑弹题", "self.answer_popup()" in src)
    check_true("弹题答案按提示选 A", D.QUIZ_ANSWER == "A")
    # 最贵的一次教训：本地 `currentTime` 一直在涨，服务端替**别的讲**记账，
    # 45 分钟全白跑。所以每轮都要核对，对不上就交回上层重切。
    check_true("每轮核对播放器认的是不是这一讲",
               'act.get("id") != item.get("item_id")' in src)
    check_true("认错了返回 wrong-lesson（不在这儿傻等）",
               '"wrong-lesson"' in src)
    check_true("desktop_watch 遇到 wrong-lesson 会重切而不是跳下一讲",
               'result != "wrong-lesson"' in
               (HERE / "desktop_watch.py").read_text(encoding="utf-8"))
    check_true("有「谁按了暂停」的取证（栈记进 __dshPauseLog）",
               "def arm_snitch" in src and "__dshPauseLog" in src)
    # 2026-10-08 第 10 讲白等的那次：`play()` 被自动播放策略拒绝，但被拒
    # 不派发 pause、页面上也没有浮层，从外面看就是"没播、也没人按暂停"。
    # 只有读 `play()` 的 Promise 才看得出来。
    check_true("起播要读 play() 的 Promise（被拒不派发 pause，光看事件看不出来）",
               "def start_video" in src and "__dshPlay" in src
               and '"RESOLVED"' in src)
    check_true("起播会点一下 <video>（真鼠标三连，平台把起播绑在 click 上）",
               "def click_video" in src and "Input.dispatchMouseEvent" in src
               and "self.click_video()" in src)
    check_true("watch_video 用 start_video 而不是裸 play（裸 play 会假报在播）",
               "self.start_video(tries=3)" in src)
    check_true("play() 保留 reset 参数（只有明确要重播才归零）",
               "reset: bool = False" in src)

    print("\n[11] 学完的判据与门槛")
    check_true("close_extra_tabs 存在（用户要求别攒标签页）",
               "def close_extra_tabs" in src)
    check_true("上报钩子写 sessionStorage（整页导航才不会丢）",
               "sessionStorage.setItem(KEY" in src)

    print("\n[11b] 视频弹题：两种题型都要认得出")
    # 2026-10-08 第 10 讲 00:28:27 那道弹题被晾了一整轮：选项选择器
    # （`.question-option` / `li.option` 那一串）**一个都没匹配上**，
    # 日志只会说「认不出这题怎么答」，然后平台每隔几秒把视频按停一次。
    # 真结构是：题干在 `.popup_body .title`，选项是 `.popup_body ul li`
    # 里带 `.checkbox-inline` 的行，文字在那个 `span` 里（`A . 是`）。
    check_true("题干读 .popup_body .title",
               ".popup_body .title" in D.QUIZ_JS)
    check_true("选项要求带 .checkbox-inline（题干那层 li 也被算进来了）",
               ".checkbox-inline" in D.QUIZ_JS)
    # 这道题问的是「是否继续当前视频学习…请选择A」，选「否」= 把学习停掉，
    # 所以挑不出来时必须返回 None，绝不能瞎点一个。
    yes = {"text": "A . 是", "x": 1, "y": 2}
    no = {"text": "B . 否", "x": 3, "y": 4}
    sess = _session(FakeWS([]))
    check("认出 A . 是（字母前缀）", sess.pick_option([yes, no]), yes)
    check("只有「是/否」时挑「是」",
          sess.pick_option([{"text": "否", "x": 1, "y": 1},
                            {"text": "是", "x": 2, "y": 2}])["text"], "是")
    check("带「否」的那条不会被「对」误命中（否定优先排除）",
          sess.pick_option([{"text": "不对", "x": 1, "y": 1}]), None)
    check("认不出就返回 None（宁可不答，也不去点「否」）",
          sess.pick_option([{"text": "红色", "x": 1, "y": 1}]), None)
    check("空选项返回 None", sess.pick_option([]), None)
    check_true("answer_popup 用 pick_option 挑选项",
               "self.pick_option(opts)" in src)
    check_true("按钮字不写死（打分题是「提交」，状态检测题是「继续」）",
               '"提交", "确定", "继续"' in src)
    check_true("打分题按 placeholder 的量级给满分",
               '"100" if "分" in' in src)

    print("\n[11c] 一次只能跑一个看课程序（同一个错犯过两次）")
    wsrc = (HERE / "desktop_watch.py").read_text(encoding="utf-8")
    # 2026-10-08 白丢过 45 分钟和 34 分钟的正片进度：看课是在浏览器那**一个
    # 标签页**里跑的，我在旁边开个探针 `enter_course()` 一下，正在播的
    # `<video>` 就没了 —— 而看课的日志只会说「页面上没有视频元素了」。
    check_true("desktop_watch 有独占锁文件", "LOCK_FILE" in wsrc
               and "def acquire_lock" in wsrc)
    check_true("占不上锁就不碰浏览器（直接退出）",
               "if not acquire_lock():" in wsrc)
    check_true("收尾会放锁", "release_lock()" in wsrc)
    # `os.kill(pid, 0)` 在 Windows 上对死进程会抛 SystemError（不是 OSError，
    # `except OSError` 接不住）→ 第二个实例把别人的锁当成过期清掉，
    # 接着就导航了。非 Windows 上用它是可以的，但**必须是 `os.name != "nt"`
    # 那个分支里**，而且不能出现在 `_alive` 之外。
    kill_uses = [ln.strip() for ln in wsrc.splitlines()
                 if "os.kill(pid, 0)" in ln and not ln.strip().startswith("#")]
    check_true("Windows 上不用 os.kill(pid, 0) 判生死（会抛 SystemError）",
               all("os.kill" in ln for ln in kill_uses)
               and len(kill_uses) <= 2
               and 'os.name != "nt"' in wsrc, f"{kill_uses}")
    check_true("用 Win32 的 GetExitCodeProcess 判生死",
               "GetExitCodeProcess" in wsrc)
    check_true("desktop.Session 会提醒「有看课程序在跑」",
               "def busy_hint" in src and "有看课程序在跑" in src)

    print("\n[12] 看课程序（desktop_watch.py）")
    check_true("用 enter_course 进课", "sess.enter_course(" in wsrc)
    check_true("不再用「点去学习」那套",
               "open_course(" not in wsrc)
    check_true("跑完一门要复核（踩过假完成的坑）",
               "收尾复核" in wsrc or "复核" in wsrc)
    check_true("收尾会清多余标签页", "close_extra_tabs()" in wsrc)
    check_true("有 --dry-run", '"--dry-run"' in wsrc)
    # 起播这一下必须当场判成败：2026-10-08 第 10 讲就是"日志一直在说重新播、
    # 视频 20 分钟一动没动"，跑完才发现。
    check_true("起播失败当场报出来，不丢给 watch_video 刷屏",
               "sess.start_video(tries=3)" in wsrc
               and '"no-start"' in wsrc)

    print("\n" + "=" * 68)
    print(f" {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
