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
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import desktop as D  # noqa: E402
import desktop_exam as X  # noqa: E402

PASS = 0
FAIL = 0
HERE = Path(__file__).resolve().parent

#: `post_form()` 发出去的表达式里一定有的记号 —— 用它把「这次 evaluate 是
#: 一个表单请求」跟别的 `Runtime.evaluate` 区分开（`POST_JS` 独有的请求头）。
POST_MARK = "X-Requested-With"


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


def _fn_body(src: str, name: str) -> str:
    """从 `def name(` 抠到下一个顶层 `def ` / `class ` 之前。

    要它是因为「A 出现在 B 之前」这种顺序断言**必须限定在同一个函数里** ——
    2026-10-08 就在 `desktop.py` 上翻过车：判「新标签页先走 HTTP /json/new
    再退 Target.createTarget」时拿整个文件的 `find()` 比位置，结果比的是
    `json_new()` 自己的定义和 `new_tab()` 里的调用，恒为真/恒为假。
    """
    i = src.find(f"def {name}(")
    if i < 0:
        return ""
    j = len(src)
    for marker in ("\ndef ", "\nclass "):
        k = src.find(marker, i + 1)
        if k >= 0:
            j = min(j, k)
    return src[i:j]


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

    实测第 9 讲 `status=1`（看了一半）、第 10 讲 `status=0`（没看）。

    ★ 这里**故意混用平台那两种叫法**：i1/i2 是 `wareTypeName="讲座"`，
    i9/i10 是 `wareTypeName="视频"` —— 平台在同一份课件树里对同一种东西
    两种都写（普查 17 门课：39 个「讲座」+ 138 个「视频」，`wareType`
    全是 `"jz"`）。曾经按 `wareTypeName == "讲座"` 卡，于是用「视频」
    那套叫法的课 `items()` 恒为 0，看课脚本一节都不播 —— 这个 fixture
    两种都放，就是为了让那种错法**必然**把下面的条数断言撑爆。
    末尾 x1 是 `作业/tk`（「本项目考核」），验证它被排掉。
    """
    return {
        "returnCode": "S0000",
        "title": "肝胆肿瘤整合治疗与肝移植全程化管理护理新进展学习班",
        "chapterList": [{
            "title": "第一章", "wareTypeName": "章节/模块",
            "childList": [{
                "title": "1", "childList": [
                    {"id": "i1", "title": "01a.mp4", "wareTypeName": "讲座",
                     "wareType": "jz", "status": 2},
                    {"id": "i2", "title": "02a.mp4", "wareTypeName": "讲座",
                     "wareType": "jz", "status": 2},
                    {"id": "i9", "title": "09a.mp4", "wareTypeName": "视频",
                     "wareType": "jz", "status": 1},
                    {"id": "i10", "title": "10a.mp4", "wareTypeName": "视频",
                     "wareType": "jz", "status": 0},
                    {"id": "x1", "title": "本项目考核", "wareTypeName": "作业",
                     "wareType": "tk", "status": 0},
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
    check("「讲座」和「视频」两种叫法都收，作业被排掉", len(items), 4)
    check("itemId 按接口给的顺序", [i["item_id"] for i in items],
          ["i1", "i2", "i9", "i10"])
    check("编号从 1 连续排", [i["n"] for i in items], [1, 2, 3, 4])
    check("status 原样带出", [i["status"] for i in items], [2, 2, 1, 0])
    check("status>=2 才算学完", [i["done"] for i in items],
          [True, True, False, False])
    check("没学完的只剩第 9、10 讲",
          [i["n"] for i in sess.pending()], [3, 4])

    print("\n[5b] ★ 判据用 wareType 而不是 wareTypeName（回归钉子）")
    # 曾经写成 `wareTypeName == "讲座"`，后果是**静默失效**：用「视频」
    # 那套叫法的课 items() 恒为 0 → pending() 恒空 → 看课脚本以为都看完了。
    # 这里把一个**纯「视频」树**喂进去，只要有人把判据改回按名字卡，
    # 下面第一条就必然变 0。
    check("_is_video_leaf 认 讲座/jz", D._is_video_leaf(
        {"wareTypeName": "讲座", "wareType": "jz"}), True)
    check("_is_video_leaf 认 视频/jz", D._is_video_leaf(
        {"wareTypeName": "视频", "wareType": "jz"}), True)
    check("_is_video_leaf 排掉 作业/tk", D._is_video_leaf(
        {"wareTypeName": "作业", "wareType": "tk"}), False)
    check("_is_video_leaf 不认章节", D._is_video_leaf(
        {"wareTypeName": "章节/模块"}), False)
    # wareType 缺字段时才退回按名字认（只认一个字段会重演同一类事故）
    check("wareType 缺失时退回按名字认", D._is_video_leaf(
        {"wareTypeName": "视频"}), True)
    check("wareType 是别的值时不再猜", D._is_video_leaf(
        {"wareTypeName": "视频", "wareType": "tk"}), False)
    check("wareType 判据的取值就是 jz", D._VIDEO_WARE_TYPE, "jz")

    tree = _lesson_tree()
    for leaf in tree["chapterList"][0]["childList"][0]["childList"]:
        if leaf["id"] in ("i1", "i2", "i9", "i10"):
            leaf["wareTypeName"] = "视频"  # 全换成另一种叫法
    sess_v = _session(FakeWS([
        ("location.href", D.COURSE + "/x?courseId=cid1"),
        ("queryCourseItemList", json.dumps(tree)),
    ]))
    check("整棵树都叫「视频」时照样收全 4 讲", len(sess_v.items()), 4)

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

    print("\n[13] 刚开出来的空白标签页也算「能用」（踩过：白丢登录态）")
    # 2026-10-08 又踩一次：`_LIVE_JS` 第三级是 `fetch('/robots.txt')`，
    # 相对地址在 `about:blank` 上没有 origin → 必然 `net-fail:TypeError`
    # → `_live()` 把**刚开出来的新标签页**一律判死（`open_tab()` /
    # `json_new()` 开出来的正是 about:blank）。表现是浏览器明明好好的、
    # `1+1` 也答得出来，却报「N 个标签页一个都不能用（N 个探活就死）」，
    # 于是我重启浏览器 —— **httponly 的会话 cookie 就这么丢了，用户得重登**。
    check_true("有 _blank_page 兜底", "def _blank_page(" in src)
    check_true("判活失败但页面是空白页时放行",
               "if _blank_page(ws):" in src)
    check_true("空白页判据认 about: 和 chrome://newtab",
               'startswith("about:")' in src
               and 'startswith("chrome://newtab")' in src)
    check_true("_blank_page 自己吞异常（保守返回 False）",
               "def _blank_page(ws) -> bool:" in src)
    check_true("文案里写明了这次踩的坑（免得以后又被改回去）",
               "about:blank" in src and "会话 cookie" in src)

    print("\n[14] 只开一个标签页（用户 m11305 的要求）")
    check_true("有 close_extra_tabs", "def close_extra_tabs(" in src)
    check_true("最后一张标签页关不掉（关了渲染进程一起退）",
               "len(tabs(" in src and "<= 1" in src)
    #     ⚠ 断言只能比**真代码**里的两处：`Target.createTarget` 这个词在
    #     `new_tab` 的 docstring 里也出现过（那条通道列在文档里），拿它比位置
    #     比到的是 docstring，必然假失败 —— 实测翻过这个车。
    _nt = _fn_body(src, "new_tab")
    check_true("新标签页先走 HTTP /json/new 再退 Target.createTarget",
               "json_new(url, port=port)" in _nt
               and "browser_ws(port=port)" in _nt
               and _nt.find("json_new(url, port=port)")
               < _nt.find("browser_ws(port=port)"))

    print("\n[15] 服务端进度要直接查接口，别信页面自己上报的字段")
    # 页面自己上报的响应里常常**只回 state/status/key、没有 studyTime**，
    # 于是进度行永远显示「服务端已学 0.0 分钟」，看着像没记账。
    check_true("有 server_time() 走 queryVideoItemDetail",
               "def server_time(" in src
               and '"queryVideoItemDetail"' in src)
    check_true("看课循环用 server_time 的 study_time 打进度",
               "srv.get('study_time'" in src)

    print("\n[16] 考核（desktop_exam.py）：答案要按选项文字搬，不能抄字母")
    xsrc = (HERE / "desktop_exam.py").read_text(encoding="utf-8")
    # desktop.py 的源码在更早的段落里读过（`src`），但那一节的作用域已经过去了，
    # 结课这一节要查的是 desktop.py 里新加的 finish_course()，单独再读一份清楚些。
    dsrc = (HERE / "desktop.py").read_text(encoding="utf-8")

    # ★ 这条是本模块存在的理由。2026-10-08 实测同一道题
    #   「在我国，与原发性肝癌关系最密切的疾病是」：
    #     查看页（show）里 正确答案='A'，A 的内容是「肝炎后肝硬化」；
    #     切到答题页（do）同一道题，正确答案变成 'B'，B 的内容才是「肝炎后肝硬化」。
    #   —— 题库考核**每次加载选项都重排**，照抄字母会把对的答成错的。
    q_show = {"id": "q1", "title": "与原发性肝癌关系最密切的疾病是",
              "options": [{"index": "A", "text": "肝炎后肝硬化"},
                          {"index": "B", "text": "胆石症"},
                          {"index": "C", "text": "胰腺炎"}]}
    q_do = {"id": "q1", "title": "与原发性肝癌关系最密切的疾病是",
            "options": [{"index": "A", "text": "胆石症"},
                        {"index": "B", "text": "肝炎后肝硬化"},
                        {"index": "C", "text": "胰腺炎"}]}
    check("查看页上正确答案是 A", X.resolve({"answer": "A", "texts": ["肝炎后肝硬化"]},
                                            q_show), "A")
    check("同一道题换到答题页（选项重排）要跟着搬成 B，不能照抄 A",
          X.resolve({"answer": "A", "texts": ["肝炎后肝硬化"]}, q_do), "B")
    check("多选题按文字逐个搬",
          X.resolve({"answer": "A|C", "texts": ["胆石症", "胰腺炎"]}, q_do), "A|C")
    check("文字对不上才退回字母（且字母要真的存在）",
          X.resolve({"answer": "B", "texts": ["这道题改过措辞了"]}, q_do), "B")
    check("字母在当前选项里不存在 → 搬不动，返回 None（宁可蒙也别填错）",
          X.resolve({"answer": "E", "texts": []}, q_do), None)
    check("空缓存条目返回 None", X.resolve({}, q_do), None)

    # ★ `queryHomeworkList` 每一项里 `showTkScore` 是个**布尔** true
    #   （"允许显示分数"这个开关，不是分数），而 `isinstance(True, int)` 在
    #   Python 里是 True —— 不挡 bool 的话 `_score_of()` 会稳稳返回 1，
    #   看着像"交完卷得了 1 分"。2026-10-08 实测这一项原文：
    #   {"homeworkStatus": 3, "redoNum": 0, "allowRedoNum": 8, "score": 20,
    #    "showTkScore": true, "answerShowType": "2"}
    check("布尔 showTkScore 不能被当成分数（否则稳拿 1 分）",
          X._score_of({"showTkScore": True}), None)
    check("布尔混在真分数里时要跳过它，取真分数",
          X._score_of({"showTkScore": True, "score": 20}), 20)
    check("串起来的数字也认", X._score_of({"score": "85"}), 85)
    check("下钻 homeworkObj", X._score_of({"homeworkObj": {"score": 60}}), 60)

    # `guess()` 不用随机、也不写死 "A"：判断/某些题的 `index` 不是 A/B，
    # 写死会填不进去；随机则同一错题每轮答案都不同，缓存永远对不上。
    check("没答案时挑第一个选项的 index（不是写死的 A）",
          X.guess({"kind": "danxuan", "options": [{"index": "1", "text": "是"},
                                                  {"index": "2", "text": "否"}]}), "1")
    check("多选题也挑第一个 index",
          X.guess({"kind": "duoxuan", "options": [{"index": "B", "text": "x"}]}), "B")
    check("问答题给「无」", X.guess({"kind": "wenda", "options": []}), "无")

    plan, unsure = X.plan_for(
        [{"id": "q1", "title": "与原发性肝癌关系最密切的疾病是", "kind": "danxuan",
          "options": q_do["options"]},
         {"id": "q2", "title": "没见过的新题", "kind": "danxuan",
          "options": [{"index": "A", "text": "x"}, {"index": "B", "text": "y"}]}],
        {"questions": {X._norm("与原发性肝癌关系最密切的疾病是"):
                       {"answer": "A", "texts": ["肝炎后肝硬化"]}}})
    check("有答案的题用搬过来的答案", plan.get("q1"), "B")
    check("没答案的题也照给一个（空着平台不让交）", plan.get("q2"), "A")
    check("没把握的题数报 1", unsure, 1)

    # 题干归一化：页面上的题干带序号、全角空格、换行。
    check("题干归一化去掉所有空白",
          X._norm(" 1、急性胆囊炎时\n呈阳性的是 "), "1、急性胆囊炎时呈阳性的是")

    got = X.harvest([{"title": "题一", "sanswer": "D",
                      "options": [{"index": "D", "text": "Murphy 征"}]},
                     {"title": "题二", "sanswer": "", "options": []}],
                    {"questions": {}})
    check("只收带 sanswer 的题", got, 1)

    # 缓存 round-trip：harvest 写进去的东西，resolve 能原样搬回来。
    c = {"questions": {}}
    X.harvest([{"title": "题四", "sanswer": "B",
                "options": [{"index": "A", "text": "甲"}, {"index": "B", "text": "乙"}]}], c)
    check("harvest 存下正确选项的文字", c["questions"][X._norm("题四")]["texts"], ["乙"])
    check("存下来的答案能被 resolve 搬回另一张卷子",
          X.resolve(c["questions"][X._norm("题四")],
                    {"title": "题四", "options": [{"index": "A", "text": "乙"},
                                                 {"index": "B", "text": "甲"}]}), "A")

    # 顺序：问卷接口对没通过考核的课**是锁着的**，所以考核必须先跑。
    # 实测被顶回来的原话是「请先完成课程学习，再进行问卷作答」。
    main_body = _fn_body(xsrc, "main")
    i_exam = main_body.find("——— 考核 ———")
    i_survey = main_body.find("——— 问卷 ———")
    check_true("考核那一段排在问卷前面（问卷在考核没到 60 分时是锁着的）",
               -1 < i_exam < i_survey, f"{i_exam} / {i_survey}")
    check_true("注释里留了平台顶回来的原话",
               "请先完成课程学习，再进行问卷作答" in xsrc)
    check_true("问卷读不到题目时把平台原话打出来（别只说「读不到题目」）",
               'd.get("message")' in xsrc)
    # 收答案必须在**下一轮开头**：刚交完卷服务端还在批改，那时候
    # 打开「查看」页 `showHomework` 会回错（页面上 loadError=true、
    # 零道题），连整页 reload 都救不回来。实测为此白丢过三次交卷机会。
    ex_body = _fn_body(xsrc, "do_exam")
    i_harvest = ex_body.find("harvest_wait(sess, hid, cache")
    # `route="do"` 在 dry-run 那一段也出现过一次（那是在循环之前），所以只能
    # 从收答案那一点往后找 —— 拿 `find()` 从头比位置会命中 dry-run 那句。
    i_open = ex_body.find('route="do"', i_harvest)
    check_true("先补收上一轮的答案，再打开答题页",
               -1 < i_harvest < i_open, f"{i_harvest} / {i_open}")
    check_true("收不到答案时会反复重开「查看」页",
               "while True" in _fn_body(xsrc, "harvest_wait")
               and "sleep(8.0)" in _fn_body(xsrc, "harvest_wait"))
    check_true("没过也兜底收一次（留给下次，答案在 data/exam_answers.json）",
               "留给下次" in ex_body)
    check("题库缓存在 data/ 下，且与手机版那份不是同一个文件",
          X.CACHE_NAME, "exam_answers.json")
    check("及格线 60", X.PASS_SCORE, 60)

    # 问卷选题偏好：先整串相等再包含匹配（「很满意」里含「满意」）。
    check("整串相等优先于包含匹配",
          X.pick_option({"optionList": [{"id": "1", "content": "满意"},
                                        {"id": "2", "content": "很满意"}]})["id"], "2")
    check("一个偏好词都不沾时退到第一个选项",
          X.pick_option({"optionList": [{"id": "1", "content": "甲"},
                                        {"id": "2", "content": "乙"}]})["id"], "1")
    check("没有选项返回 None", X.pick_option({"optionList": []}), None)

    print("\n[17] 结课（applyFinishCourse / updateCourseFinish）")
    # 结课的 API 只从页面源码里考出来过（`debug/examjs/pc.js:734`），
    # 所以这里既有源码级断言，也有行为断言。
    check_true("desktop.py 里有 finish_course()", "def finish_course(" in dsrc)
    fc = _fn_body(dsrc, "finish_course")
    check_true("打的是 /user/updateCourseFinish",
               "/user/updateCourseFinish" in fc)
    check_true("表单字段是 courseId", "courseId=" in fc)
    check_true("走 post_form（问卷那族接口同一个域，必须在 elearning 页上调）",
               "post_form(" in fc)
    check_true("认回执里的 success 字段", 'd.get("success")' in fc)

    # 课程列表要把结课状态一起带出来 —— 不带就没法判断"这门要不要申请结课"。
    check_true("COURSES_JS 里带 isFinish",
               "isFinish: c.isFinishCourse" in dsrc)
    check_true("COURSES_JS 里带 finishDate（结课时间，实测回执里有）",
               "finishDate: c.finishCourseDate" in dsrc)

    # ★ 读课程列表之前必须**强制重载**：`goto()` 见地址已经是目标页就返回，
    #   而 Vue 手里那份 `courseList` 还是上一次进页面时的接口结果。
    #   2026-10-08 实测：6 门课结课都成功了（服务端 finishCourseDate 都写上了），
    #   再跑脚本时页面却还写着「申请结课」。
    check_true("读课程列表前强制重载（否则拿到的是上一次的结课状态）",
               "desktop.PERSONAL, settle=9.0, tries=2, force=True" in xsrc)
    check_true("结课默认不跑，要显式 --finish",
               '"--finish"' in xsrc and 'args.finish' in xsrc)
    check_true("已经结课的课跳过（别重复申请）",
               'course.get("isFinish")' in xsrc)
    # 结课那段要用**服务端**判门禁，不能拿页面上的 desc 当判据：
    # desc（连同 isPass）是服务端算好的快照、会滞后 —— 实测刚交完问卷那几门
    # 仍写着"未完成问卷调查"，而问卷接口 selectType=2 已经列着 isSubmit:true。
    check_true("不拿滞后的 desc 当结课门禁（让服务端自己判）",
               "不能拿 desc 当门禁" in xsrc)

    print("\n[18] 课程列表分页：不能只看第一页")
    # 2026-10-08 用户指出「下面可以切换页码，确保所有的都写进程序里了」。
    # 一查：页面 `pageSize=8`、`totalCount=17`、分页控件 1/2/3 —— 我们一直
    # 只读了第一页 8 门，**漏了 9 门**（其中 1 门至今未结课）。
    check_true("有 all_courses()（跨分页读全部课程）",
               "def all_courses(" in dsrc)
    check_true("有 _extend_pages()（页面读到第一页后自动接上后面的）",
               "def _extend_pages(" in dsrc)
    check("翻的是页面自己那个接口", D.Session.COURSE_API, "/user/getMyCourseList")

    ac = _fn_body(dsrc, "all_courses")
    # `pageIndex` 是 **0 基** —— 页面源码是 `Math.max(this.currentPage - 1, 0)`，
    # 所以第一页发 0，第二页发 1。发成 1 基会整个错开一页。
    check_true("pageIndex 从 0 开始（0 基，与页面源码一致）",
               "for page in range(max(1, max_pages))" in ac
               and 'f"projectId=&finishType=&pageIndex={page}"' in ac)
    check_true("走 post_form（同源，必须在 elearning 域上调）",
               "self.post_form(self.COURSE_API" in ac)
    check_true("收满 totalCount 就停", "len(out) >= total" in ac)
    check_true("空页就停（别无限翻）", "if not lst or fresh == 0" in ac)
    check_true("整页重复也算到头（防同一页被反复回）", "if not lst or fresh == 0" in ac)
    check_true("接口报成功才当数据用（不在那一页时会 404）",
               'not res.get("success")' in ac)
    check_true("不在「我的学习」页时静默退出，不当错误刷屏",
               "不当错误刷屏" in ac)
    check_true("翻页读到的条数会打进日志（用户能看见读了几门）",
               "翻页读到" in ac)

    # 页面那一路必须**自动接上**翻页，否则 desktop_exam.py / desktop_watch.py
    # 还是只看得到 8 门。
    cbody = _fn_body(dsrc, "courses")
    check_true("courses() 读到数据后交给 _extend_pages 接上后面的页",
               "_extend_pages(data, log=log)" in cbody)
    check_true("courses() 页面上读不到时先问接口再认输",
               "all_courses(log=log)" in cbody)
    ep = _fn_body(dsrc, "_extend_pages")
    check_true("判据是页面自己报的 totalCount 比读到的多",
               "total <= len(page_one)" in ep)
    check_true("翻页没读全时宁可留着页面这一页（别越读越少）",
               "len(more) >= len(page_one)" in ep)

    # 两路字段名必须一致，否则调用方得写两套判断。
    check_true("COURSE_META_JS 读的是 totalCount / pageSize / currentPage",
               "total: d.totalCount" in dsrc and "pageSize: d.pageSize" in dsrc)
    check_true("COURSES_JS 也带上 totalCount 之外的字段（isOverdue / hasCertificate）",
               "isOverdue: c.isOverdue" in dsrc
               and "hasCertificate: c.hasCertificate" in dsrc)
    mapped = D.Session._map_course({
        "id": "x", "name": "课", "userClassId": "uc", "hour": "9.0",
        "isFinishCourse": True, "finishCourseDate": "2026-10-08 14:38:47",
        "userClassScoreDesc": "视频课件已完成，考核85分，已完成问卷调查",
        "canStudyFlag": "1", "isOverdue": "0", "hasCertificate": True,
        "projectName": "2026年远程项目", "classAssessmentDesc": "完成所有视频课件学习+考核≥60分+完成问卷调查",
        "isPass": "1", "teacherName": "不该出现",
    })
    check("接口的 isFinishCourse 映射成页面的 isFinish",
          mapped.get("isFinish"), True)
    check("接口的 userClassScoreDesc 映射成页面的 desc",
          mapped.get("desc"), "视频课件已完成，考核85分，已完成问卷调查")
    check("finishCourseDate 映射成 finishDate",
          mapped.get("finishDate"), "2026-10-08 14:38:47")
    check("多带的字段也映射出来（hasCertificate）",
          mapped.get("hasCertificate"), True)
    check_true("映射出来的键与 COURSES_JS 那一路完全一致（不多不漏）",
               sorted(mapped.keys()) == sorted([
                   "id", "name", "userClassId", "hour", "isFinish", "finishDate",
                   "desc", "canStudy", "isOverdue", "overdueDate", "hasCertificate",
                   "projectName", "classAssessmentDesc", "isPass"]),
               str(sorted(mapped.keys())))

    # 行为验证：三板斧 —— 第 1 页 2 门、第 2 页 2 门、第 3 页空，totalCount=4。
    # 顺便验证第 2 页真的发的是 pageIndex=1，以及**不重复**的页不会把课上重。
    def _page_answer(expr):
        i = 0
        m = re.search(r"pageIndex=(\d+)", expr)
        if m:
            i = int(m.group(1))
        pages = {
            0: [{"id": "a", "name": "甲", "isFinishCourse": True},
                {"id": "b", "name": "乙", "isFinishCourse": False}],
            1: [{"id": "c", "name": "丙", "isFinishCourse": True},
                {"id": "b", "name": "乙（重复页）", "isFinishCourse": False}],
            2: [],
        }
        return json.dumps({"success": True,
                           "record": {"courseList": pages.get(i, []),
                                      "pageIndex": i, "pageSize": 2, "totalCount": 4}})

    ws = FakeWS([(POST_MARK, _page_answer)])
    sess = _session(ws)
    got = sess.all_courses()
    names = [c.get("name") for c in got]
    check("翻页把三页合并、重复的 id 只留一份", names, ["甲", "乙", "丙"])
    check_true("第 2 页发的确实是 pageIndex=1（0 基）",
               any("pageIndex=1" in e for m, e in ws.calls
                   if m == "Runtime.evaluate" and isinstance(e, str)))

    # 接口整段失败（不在 elearning 域上时会 404）→ 空表，不抛。
    ws2 = FakeWS([(POST_MARK,
                   json.dumps({"success": False, "message": "没登录"}))])
    check("接口报 success=false 时返回空表（不抛异常）",
          _session(ws2).all_courses(), [])

    print("\n[19] 调试端口问不到时要能自愈，不能摔 traceback")
    # 实测：浏览器关掉之后 `adb forward` 的映射还留着 —— TCP 连得上、对端
    # 不应答，`urllib` 抛的是 `http.client.RemoteDisconnected`。它既不是
    # `URLError` 也不是 `RuntimeError`，所以原来的 `tabs()` 直接把它放出去，
    # 一路穿到 `Session.open()`（那里只接 `RuntimeError`）→ **一个 traceback
    # 摔在用户脸上**，而不是自动把浏览器拉起来重来。
    tbody = _fn_body(dsrc, "tabs")
    check_true("tabs() 包住了 http_json（不让连接异常穿出去）",
               "try:" in tbody and "browser.http_json(\"/json/list\"" in tbody)
    check_true("问不到时返回空表", "return []" in tbody)
    check_true("把「问不到」的原因记进日志（不然没法查为什么没标签页）",
               "问不到标签页" in tbody)

    # 行为验证：真把 RemoteDisconnected 抛出来，看 tabs() 是空表还是炸。
    import http.client
    import browser as _browser

    real = _browser.http_json
    try:
        def _boom(*_a, **_kw):
            raise http.client.RemoteDisconnected(
                "Remote end closed connection without response")
        _browser.http_json = _boom
        try:
            got_tabs = D.tabs(port=9222)
            check("RemoteDisconnected 时 tabs() 返回空表", got_tabs, [])
        except Exception as exc:  # noqa: BLE001
            check_true("RemoteDisconnected 时 tabs() 不该抛", False,
                       f"{exc.__class__.__name__}: {exc}")
    finally:
        _browser.http_json = real

    print("\n[20] 没登录 ≠ 程序坏了：要给人话，不要摔 traceback")
    # 实测（2026-10-08）：浏览器被 Android 后台杀掉之后整个 cookie 罐是空的
    # （`Network.getAllCookies` 回 0 条），`--list` 于是摔了 7 行调用栈。
    # 「没登录」是要人去浏览器窗口里点一下的情况，不是崩溃。
    check_true("有 NotLoggedIn 这个专门类型",
               "class NotLoggedIn(RuntimeError)" in dsrc)
    check_true("NotLoggedIn 是 RuntimeError 的子类（老调用方还能接住）",
               issubclass(D.NotLoggedIn, RuntimeError))
    obody = _fn_body(dsrc, "open")
    check_true("open() 抛的是 NotLoggedIn，不是裸 RuntimeError",
               "raise NotLoggedIn(" in obody)
    check_true("没登录时先把登录页开出来（不然用户没地方登）",
               "self.goto(LOGIN, settle=4.0)" in obody)
    check_true("登录页开不出来也不能盖掉真正的原因",
               "登录页没打开" in obody)

    # 秒数要写成人话：`--login-wait` 收的是秒，`20 / 60:.0f` 会打出「0 分钟」。
    check("89 秒说成秒", D._human_wait(89), "89 秒")
    check("600 秒说成分钟", D._human_wait(600), "10 分钟")
    check_true("短等待不会退化成「0 分钟」", "0 分钟" not in D._human_wait(20))

    # 两个入口脚本都要接住它，并且返回一个**专门的**退出码（4），
    # 别跟「起不来」（2）/「有程序在用浏览器」（3）混在一起。
    for name, src in (("desktop_watch.py", wsrc), ("desktop_exam.py", xsrc)):
        check_true(f"{name} 接住 NotLoggedIn",
                   "except desktop.NotLoggedIn as exc:" in src)
        check_true(f"{name} 提示用户去登录一次", "登录一次" in src)
        check_true(f"{name} 用退出码 4", "return 4" in src)

    print("\n[21] 看完的课不许再点进去 + 「一共几门」要从平台读")
    # 用户 2026-10-08 报的：「修复看完的课还会重新点击的bug，确保总课程数为
    # 从网站上读取而非记录的数量」。实录 `debug/log/app.log` 21:35–21:38
    # 那一趟：17 门课的 `userClassScoreDesc` 全都写着「视频课件已完成，
    # 考核X分，已完成问卷调查」，程序照样一门一门 `enter_course()`，
    # 每门 22 秒（`settle`）—— 十几分钟纯白跑。

    # ---- 分数抠取 ----
    check("从平台那句话里抠出考核分数",
          X.score_in_desc("视频课件已完成，考核65分，已完成问卷调查"), 65)
    check("中间有空格也认", X.score_in_desc("视频课件已完成，考核 100 分"), 100)
    check("没写考核分数就是 None（不能瞎猜成 0 —— 0 分是要重做的）",
          X.score_in_desc("视频课件未完成"), None)
    check("没有 desc 也不炸", X.score_in_desc(""), None)

    # ---- 三件事的判定（样本原样抄自那趟实录） ----
    def _state(desc, finish=False):
        return X.course_state({"desc": desc, "isFinish": finish})

    st_done = _state("视频课件已完成，考核65分，已完成问卷调查")
    check_true("三件事都齐 → video/exam/quiz 全 True",
               st_done["video"] and st_done["exam"] and st_done["quiz"])
    st_low = _state("视频课件已完成，考核0分，未完成问卷调查")
    check_true("0 分不算过（要进课重做）", not st_low["exam"])
    st_mid = _state("视频课件已完成，考核59分，未完成问卷调查")
    check_true("59 分也不许当及格（底线是 60）", not st_mid["exam"])
    st_ok = _state("视频课件已完成，考核60分，已完成问卷调查")
    check_true("正好 60 分算过", st_ok["exam"])
    st_none = _state("视频课件未完成，考核0分，未完成问卷调查")
    check_true("视频这条腿单独判", not st_none["video"] and st_none["quiz"] is False)
    check_true("isFinishCourse 映射成 finish",
               _state("视频课件已完成，考核90分，已完成问卷调查",
                      finish=True)["finish"])

    # ---- 顺序钉子：筛掉的那一段必须在 enter_course 之前 ----
    xbody = _fn_body(xsrc, "main")
    i_skip = xbody.find("这门课不用再点进去")
    i_enter = xbody.find("sess.enter_course(")
    check_true("「不用再点进去」的判定出现在第一次 enter_course 之前",
               i_skip >= 0 and i_enter >= 0 and i_skip < i_enter,
               f"skip@{i_skip} enter@{i_enter}")
    check_true("有 --all 这个「还是全都进去」的开关", "--all" in xsrc
               and "args.all" in xbody)
    check_true("收答案那一支不能被筛掉（考过的课才最有答案可收）",
               "not args.harvest_only" in xbody)
    check_true("--questionnaire-only 不碰课程页（问卷在 elearning 域上做）",
               "not args.questionnaire_only" in xbody)

    # ---- 「一共几门」必须来自平台接口，不是 len(courses) ----
    check_true("Session 有 course_total 这个字段（平台报的 totalCount）",
               "self.course_total" in dsrc)
    acbody = _fn_body(dsrc, "all_courses")
    check_true("all_courses() 把 totalCount 记进 course_total",
               "self.course_total = total" in acbody)
    epbody = _fn_body(dsrc, "_extend_pages")
    check_true("_extend_pages() 也从页面分页信息里记一份",
               "self.course_total = total" in epbody)
    showbody = _fn_body(wsrc, "show_courses")
    check_true("看课那边报总数也用 course_total",
               "course_total" in showbody)
    check_true("并且明说这个数是平台读的、不是本地记录",
               "不是本地记录" in showbody)
    check_true("考核那边把「平台一共几门」和「几门要做」分开报",
               "平台接口的 totalCount" in xbody and "没点进去" in xbody)

    # ---- 行为验证：真的跑一遍 main()，看它进没进课 ----
    import tempfile

    ready = {"id": "c-done", "name": "全齐了的课",
             "desc": "视频课件已完成，考核90分，已完成问卷调查"}
    need_exam = {"id": "c-need", "name": "考核没过要进",
                 "desc": "视频课件已完成，考核0分，未完成问卷调查"}
    need_quiz = {"id": "c-quiz", "name": "只差问卷，不用进",
                 "desc": "视频课件已完成，考核75分，未完成问卷调查"}
    need_video = {"id": "c-vid", "name": "视频没看完要进",
                  "desc": "视频课件未完成，考核0分，未完成问卷调查"}

    class FakeExamSession:
        """只实现 `desktop_exam.main()` 真正会碰的那几个方法。"""

        def __init__(self, courses, **_kw):
            self._courses = courses
            self.entered: list[str] = []
            self.course_total = None
            self.closed = False

        def open(self, **_kw):
            return True

        def goto(self, *_a, **_kw):
            return True

        def courses(self, **_kw):
            # 平台接口自报的总数 —— 故意比列表长，验证日志用的是**它**。
            self.course_total = len(self._courses)
            return list(self._courses)

        def enter_course(self, cid, **_kw):
            self.entered.append(cid)
            return True

        def homework_list(self):
            return []

        def close_extra_tabs(self):
            return None

        def q_list(self, _t):
            return []

        def close(self):
            self.closed = True

    real_session = D.Session
    real_lock = D.LOCK_FILE
    real_cache_path = X._cache_path
    real_sink = X._sink
    tmpdir = tempfile.mkdtemp(prefix="zs_exam_test_")

    def _run(args):
        holder: dict = {}

        def _factory(**_kw):
            s = FakeExamSession([ready, need_exam, need_quiz, need_video])
            holder["s"] = s
            return s

        D.Session = _factory
        D.LOCK_FILE = Path(tmpdir) / "no-lock-here.lock"
        X._cache_path = lambda: Path(tmpdir) / "cache.json"
        X.set_log(lambda _m: None)
        try:
            X.main(args)
        except SystemExit:
            pass
        finally:
            D.Session = real_session
            D.LOCK_FILE = real_lock
            X._cache_path = real_cache_path
            X.set_log(real_sink)
        return holder.get("s")

    # 这一趟故意不用 `--list`：走到问卷/结课那几段，证明"不进课"的课
    # 照样会被问卷那一步处理（它们只是不用花 22 秒进课程页而已）。
    s1 = _run([])
    check("默认：考核过了的课不再进课程页（只进没过考核的两门）",
          sorted(s1.entered), ["c-need", "c-vid"])
    check_true("三件事全齐的课一门都没进", "c-done" not in s1.entered)
    check_true("会话正常关掉（finally 没被打断）", s1.closed)

    s2 = _run(["--all"])
    check("--all：全都进去（老行为，留个后门）",
          sorted(s2.entered), ["c-done", "c-need", "c-quiz", "c-vid"])

    s3 = _run(["--questionnaire-only"])
    check("--questionnaire-only：一门课程页都不进", s3.entered, [])

    s4 = _run(["--harvest-only"])
    check("--harvest-only：全进（答案只在考过的课上有）",
          sorted(s4.entered), ["c-done", "c-need", "c-quiz", "c-vid"])

    print("\n" + "=" * 68)
    print(f" {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
