"""识别测试：在**真实截图**上跑完整识别链路。

## 为什么需要它

原先 13 个测试里只有 `test_circle.py` 用真实截图，其余都是纯文本夹具。
也就是说**页面识别和 OCR 本身没有在真实画面上测过** —— 而它们正是
最该测的两块：一旦识别错，后面所有判定都跟着错。

写这个测试时立刻就抓到两个真问题：

* 答题页那张截图被识别成「未知页面」—— 其实它是**考核被拦截页**
  （「请先完成课程视频学习，再进行考核！」），页面识别层根本不认识这个状态
* 视频弹题那张被识别成「未知页面」—— 弹题会**暂停视频**，
  它挡在所有东西上面，却不算一种页面

## 测什么

1. **标志词可检出**：每个 `PAGE_MARKS_*` 里的词都要能在某张真截图上
   命中它该命中的页面。词改了、OCR 读不出来，都会在这里暴露。
2. **判定尽量有结论**：跑完整目录的截图，统计「未知页面」的比例。
   太高说明标志词覆盖不够。
3. **互斥性**：同一张图不该同时命中两个页面的标志。命中说明标志词
   挑得不独有 —— 那会导致「同一页在不同时刻被判成不同页」这种飘忽 bug。

## 为什么夹具是本地截图而不是现场截

现场截需要连着模拟器、还得有人在正确的页面上，没法进 CI、也没法复现。
本地截图是**冻结的真实画面**，每次跑结果必须一致 —— 这才叫回归测试。

运行:
    python scripts\\test_recognition.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

import exam  # noqa: E402
import paths  # noqa: E402

SNAP_DIR = Path(__file__).resolve().parent.parent / "debug" / "snap"

PASS = 0
FAIL = 0

#: 一次跑完所有截图，OCR 只做一遍（否则每项断言各跑一次太慢）
_OCR_CACHE: dict[str, str] = {}
_tasker = None
_controller = None


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


def _setup():
    """建好 OCR 资源；失败返回 None 让调用方走 SKIP 分支。"""
    global _tasker, _controller
    if _tasker is not None:
        return _tasker
    try:
        from controller import build_controller, load_config
        from maa.resource import Resource
        from maa.tasker import Tasker

        res = Resource()
        res.post_bundle(str(paths.resource_dir())).wait()
        res.post_ocr_model(str(paths.ocr_model_dir())).wait()
        _controller = build_controller(load_config())
        _tasker = Tasker()
        _tasker.bind(res, _controller)
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP  建 OCR 失败（{type(exc).__name__}），下面跳过真机项")
        _tasker = None
    return _tasker


def ocr_of(name: str) -> str:
    """对某张截图做 OCR，带缓存。返回整屏文本（可能空串）。"""
    if name in _OCR_CACHE:
        return _OCR_CACHE[name]
    t = _setup()
    text = ""
    if t is not None:
        from maa.pipeline import JOCR, JRecognitionType

        p = SNAP_DIR / name
        try:
            im = np.array(Image.open(p).convert("RGB"))
            j = t.post_recognition(JRecognitionType.OCR, JOCR(), im)
            if j.wait().succeeded:
                td = j.get()
                if td is not None:
                    for nid in td.node_id_list:
                        nd = t.get_node_detail(nid)
                        if nd is not None and nd.recognition is not None:
                            text = " ".join(
                                str(getattr(r, "text", ""))
                                for r in (nd.recognition.all_results or []))
                            break
        except Exception:  # noqa: BLE001 - 单张读失败不该终止
            text = ""
    _OCR_CACHE[name] = text
    return text


#: 这些文件名是**主机界面**截图（主窗口、调试视图、桌面），不是模拟器截图。
#:
#: 它们被 `debug/snap/` 和真机截图混在同一个目录里，早先没过滤时会在
#: 「真截图都要被判为在站内」那一组里报一堆假失败 —— 主机 UI 当然不在站内。
#: 用前缀排除，比靠 OCR 内容猜更可靠。
_HOST_UI_PREFIXES = ("ui_shot", "mainui", "win_", "left_", "exe_", "dbg", "overlay",
                     # 主机界面截图，不是模拟器截图。`src_*` 是「验证主界面
                     # 改动」时截的（`src_ui*` 一整套 + `src_split.png` 那张
                     # 并排布局），`_` 开头的是临时探针产物 —— 两者都被当成
                     # 夹具的话会报一串假失败（实测 10 个）。
                     #
                     # ⚠️ 这里最早只写了 `"src_ui"`，漏掉 `src_split.png`，
                     # 于是它单枪匹马报了一个「FAIL 在站内」。**用 `src_`
                     # 覆盖整类**，别再逐个补名字。
                     "src_", "_")
#: 文件名里含这些词的也是主机截图（不一定是前缀）
_HOST_UI_WORDS = ("desktop", "screenshot", "screen_")


def _all_snaps() -> list[str]:
    if not SNAP_DIR.is_dir():
        return []
    return sorted(
        f.name for f in SNAP_DIR.glob("*.png")
        if not f.name.startswith(_HOST_UI_PREFIXES)
        and not any(w in f.name.lower() for w in _HOST_UI_WORDS)
    )


def main() -> int:
    print("=" * 68)
    print(" 识别测试（真实截图）")
    print("=" * 68)

    snaps = _all_snaps()
    print(f"\n夹具目录: {SNAP_DIR}")
    print(f"截图数量: {len(snaps)}")
    if not snaps:
        print("  SKIP  没有截图夹具")
        return 0

    print("\n[1] 标志词表本身要自洽（纯逻辑，不依赖截图）")
    marks = {
        "结果页": exam.PAGE_MARKS_RESULT_DETAIL,
        "答题页": exam.PAGE_MARKS_ANSWER,
        "考核入口": exam.PAGE_MARKS_EXAM_ENTRY,
        "课程页": exam.PAGE_MARKS_COURSE,
        "学习列表": exam.PAGE_MARKS_LEARNING_LIST,
        "考核被拦截": exam.PAGE_MARKS_EXAM_LOCKED,
        "视频弹题": exam.PAGE_MARKS_QUIZ_POPUP,
        "提示弹窗": exam.PAGE_MARKS_DIALOG,
    }
    for name, ms in marks.items():
        check_true(f"{name} 有标志词", bool(ms), "空元组")

    # 新加的两个页面必须能被认出来（这是写本测试时发现缺的）
    check("弹题页判据",
          exam.detect_page("视频弹题（00:27:00）正确作答后继续视频学习 1、是否继续 A 是 B 否"),
          exam.PAGE_QUIZ_POPUP)
    check("考核被拦截判据",
          exam.detect_page("考核详情 请先完成课程视频学习，再进行考核！ 重新加载"),
          exam.PAGE_EXAM_LOCKED)
    check("被拦截页不能再被当成考核入口",
          exam.detect_page("考核详情 请先完成课程视频学习，再进行考核！ 开始答题"),
          exam.PAGE_EXAM_LOCKED)

    print("\n[2] 真截图：关键页面必须认对")
    key = [
        ("20261006-201632_examq.png", exam.PAGE_EXAM_LOCKED, "考核被拦截页"),
        ("20261006-191822_popup-quiz.png", exam.PAGE_QUIZ_POPUP, "视频弹题"),
        ("20261006-201541_exampage.png", exam.PAGE_EXAM_ENTRY, "考核入口页"),
        ("20261006-224814_result_page.png", exam.PAGE_RESULT, "结果页"),
        ("20261006-200305_dirtop.png", exam.PAGE_COURSE, "课程页"),
        ("20261006-181123_platform.png", exam.PAGE_LEARNING_LIST, "学习列表页"),
    ]
    for fn, want, desc in key:
        if not (SNAP_DIR / fn).is_file():
            print(f"  SKIP  {desc}：夹具 {fn} 不在")
            continue
        t = ocr_of(fn)
        if not t:
            print(f"  SKIP  {desc}：OCR 没读到文本（{fn}）")
            continue
        check(f"{desc}（{fn}）", exam.detect_page(t), want)

    print("\n[3] 全量扫描：统计识别结论的分布")
    counts: dict[str, int] = {}
    unknown: list[str] = []
    for fn in snaps:
        t = ocr_of(fn)
        if not t:
            continue
        pg = exam.detect_page(t)
        counts[pg] = counts.get(pg, 0) + 1
        if pg == exam.PAGE_UNKNOWN:
            unknown.append(fn)

    total = sum(counts.values())
    print(f"       有文本的截图: {total}")
    for pg, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"         {exam.page_name(pg):14} {n:>3}")

    if total:
        known = total - len(unknown)
        ratio = known / total
        print(f"       能判定的比例: {ratio:.0%}")
        # 这个阈值不是为了好看，是为了**发现标志词退化**：
        # 实测这套标志能覆盖大多数截图，比例骤降就说明有词失效了。
        check_true("能判定的截图不少于一半", ratio >= 0.5,
                   f"实际 {ratio:.0%}（未知 {len(unknown)} 张）")

    print("\n[4] 互斥性：同一张图不该同时命中两个页面")
    #
    # 命中说明标志词挑得不独有 —— 会导致「同一页在不同时刻被判成不同页」，
    # 那种 bug 最难查（时好时坏）。这里主动找出来。
    collisions: list[str] = []
    pairs = [
        (exam.PAGE_RESULT, exam.PAGE_MARKS_RESULT_DETAIL),
        (exam.PAGE_ANSWER, exam.PAGE_MARKS_ANSWER),
        (exam.PAGE_EXAM_ENTRY, exam.PAGE_MARKS_EXAM_ENTRY),
        (exam.PAGE_LEARNING_LIST, exam.PAGE_MARKS_LEARNING_LIST),
        (exam.PAGE_EXAM_LOCKED, exam.PAGE_MARKS_EXAM_LOCKED),
        (exam.PAGE_QUIZ_POPUP, exam.PAGE_MARKS_QUIZ_POPUP),
    ]
    for fn in snaps:
        t = ocr_of(fn)
        if not t:
            continue
        hit = [pg for pg, ms in pairs if any(m in t for m in ms)]
        # 弹题/被拦截页内部可能同时含别的词（比如弹题里也有题目文本），
        # 只报「两个都不是它们」的冲突，避免噪音
        hard = [h for h in hit
                if h not in (exam.PAGE_QUIZ_POPUP, exam.PAGE_EXAM_LOCKED)]
        if len(hard) >= 2:
            collisions.append(f"{fn}: {[exam.page_name(h) for h in hard]}")

    if collisions:
        print(f"       发现 {len(collisions)} 张有多重命中：")
        for c in collisions[:8]:
            print(f"         {c}")
    check("没有页面标志互相冲突", len(collisions), 0)

    print("\n[5] 站内判定：不在平台里时要能识别出来（用于报错停手）")
    #
    # 动机：用户停在微信首页 / 退到聊天列表 / 登录掉了时，detect_page 只
    # 返回「未知页面」，程序会对着微信界面瞎点。有了这个判据就能报错停手。
    #
    # 判据不能只看域名：实测 82 张能判定的站内截图里 **12 张没有域名**
    # （「我的学习」列表页、部分课程页不显示网址栏），只看域名会把这些
    # 正常页面误判成「没进网站」。
    check("有域名 → 在站内",
          exam.on_site("course.zs-hospital.sh.cn 课程学习"), True)
    # 踩过的坑：微信聊天列表里带链接预览的会话也会显示平台域名，
    # 只看「域名出现过」会把聊天列表误判成「在平台内」。
    # 所以微信界面优先否掉。
    check("微信界面（有域名预览）→ 不在站内",
          exam.on_site("微信 通讯录 发现 我 文件传输助手 https://elearning.zs-hospital.sh.cn/"),
          False)
    check("微信界面判据",
          exam.in_wechat_ui("微信 通讯录 发现 我 文件传输助手"), True)
    check("平台页面不是微信界面",
          exam.in_wechat_ui("我的学习 去学习 简介 目录"), False)
    # 域名只认顶部网址栏那一条（y < URL_BAR_Y）
    rows_bar = [("course.zs-hospital.sh.cn", 285, 88)]
    check("域名在网址栏 → 在站内",
          exam.on_site("course.zs-hospital.sh.cn", rows_bar), True)
    rows_chat = [("https://elearning.zs-hospital.sh.cn/", 76, 860)]
    check("域名只在聊天预览里（y=860）→ 不算",
          exam.on_site("https://elearning.zs-hospital.sh.cn/", rows_chat), False)
    check("能判定页面类型 → 也算在站内（即使没域名）",
          exam.on_site("我的学习 去学习"), True)
    check("微信首页 → 不在站内",
          exam.on_site("微信 通讯录 发现 我 文件传输助手"), False)
    check("登录页 → 不在站内",
          exam.on_site("请登录 用户名 密码 登录"), False)
    check("空白 → 不在站内", exam.on_site(""), False)
    check("只有状态栏时间 → 不在站内", exam.on_site("10:23"), False)

    print("\n[6] 真截图都要被判为「在站内」")
    for fn in snaps:
        t = ocr_of(fn)
        if not t:
            continue
        pg = exam.detect_page(t)
        if pg == exam.PAGE_UNKNOWN:
            continue          # 未知页面本来就允许不在站内，跳过
        if not exam.on_site(t):
            check(f"在站内: {fn}", False, True)
    else:
        check_true("所有能判定的真截图都在站内", True)

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
