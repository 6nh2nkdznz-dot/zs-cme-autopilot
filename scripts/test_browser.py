"""手机浏览器（桌面版）接管的自测。

## 为什么单独测这一块

这条路是「不开微信」的唯一出路（模拟器里跑微信有封号风险，而且模拟器的
x86 媒体栈上微信解码器反复崩）。它有两个**顺序性**的失败模式，错了都不报错、
只表现为「页面打不开 / 点什么都没反应」：

  1. CDP 的 `setUserAgentOverride` 必须在 `Page.navigate` **之前** ——
     反了 SPA 卡死（我第一版就栽在这里，还错怪了平台）
  2. `fill()` 点按钮必须**按名字点** —— 之前按"第一个像提交的"猜，
     结果点到「获取验证码」，页面什么都没发生

这两条都不会抛异常，所以必须有测试钉住。

运行:
    python scripts\\test_browser.py
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

import browser as B  # noqa: E402

PASS = 0
FAIL = 0


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
    """假 CDP 连接：只记下被调用过哪些方法、什么顺序、什么参数。"""

    def __init__(self, eval_result: str = "{}"):
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self._eval_result = eval_result

    def call(self, method: str, **params):
        self.calls.append((method, params))
        return {"id": len(self.calls), "result": {}}

    def evaluate(self, expression: str):
        self.calls.append(("Runtime.evaluate", {"expression": expression}))
        return self._eval_result

    def close(self) -> None:
        self.closed = True

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def main() -> int:
    print("=" * 68)
    print(" 手机浏览器（桌面版）接管自测")
    print("=" * 68)

    print("\n[1] 常量")
    check_true("桌面 UA 是 Windows 形态",
               "Windows NT" in B.UA_DESKTOP, B.UA_DESKTOP)
    check_true("桌面 UA 不是微信 UA",
               "MicroMessenger" not in B.UA_DESKTOP)
    check("登录页地址", B.LOGIN, "https://elearning.zs-hospital.sh.cn/learning/login")
    check("本地调试端口", B.LOCAL_PORT, 9222)
    check_true("候选浏览器里含模拟器自带的 com.android.chromium",
               "com.android.chromium" in B.BROWSER_PKGS)

    print("\n[2] set_desktop_ua 发的命令（刻意【不】覆盖视口）")
    ws = FakeWS()
    B.set_desktop_ua(ws)
    check("两条命令", ws.methods(),
          ["Emulation.setUserAgentOverride",
           "Emulation.setTouchEmulationEnabled"])
    ua = ws.calls[0][1]
    check("UA 参数", ua.get("userAgent"), B.UA_DESKTOP)
    check("platform 写成 Win32（页面对 UA 平台也做判断）",
          ua.get("platform"), "Win32")
    check("触摸模拟开着", ws.calls[1][1].get("enabled"), True)
    # 覆盖视口会把桌面宽度压扁、右侧内容被裁掉（登录页的输入框全在屏外），
    # 这条断言就是钉住「别再把它加回来」。
    check_true("不许调 setDeviceMetricsOverride（会把页面裁掉）",
               "Emulation.setDeviceMetricsOverride" not in ws.methods(),
               str(ws.methods()))

    print("\n[3] 【关键】顺序：UA 必须在导航之前")
    seq = FakeWS()
    real_connect_live = B.connect_live
    real_page_info = B.page_info
    real_sleep = B.time.sleep
    try:
        B.connect_live = lambda *a, **k: seq       # type: ignore[assignment]
        B.page_info = lambda ws: {"title": "x"}    # type: ignore[assignment]
        B.time.sleep = lambda *_a: None            # type: ignore[assignment]
        B.open_desktop("https://example.invalid/")
    finally:
        B.connect_live = real_connect_live         # type: ignore[assignment]
        B.page_info = real_page_info               # type: ignore[assignment]
        B.time.sleep = real_sleep                  # type: ignore[assignment]
    ms = seq.methods()
    check("命令顺序", ms,
          ["Emulation.setUserAgentOverride",
           "Emulation.setTouchEmulationEnabled",
           "Page.enable",
           "Page.navigate"])
    idx_ua = ms.index("Emulation.setUserAgentOverride")
    idx_nav = ms.index("Page.navigate")
    check_true("setUserAgentOverride 排在 Page.navigate 前面",
               idx_ua < idx_nav, f"UA 在第 {idx_ua} 位、导航在第 {idx_nav} 位")
    nav_params = [p for m, p in seq.calls if m == "Page.navigate"][0]
    check("导航到目标地址", nav_params.get("url"), "https://example.invalid/")

    print("\n[3b] CSS 坐标 -> 画布坐标的换算")
    check("屏幕到画布的比例", B.SCREEN_TO_CANVAS, 1.5)
    check("dpr 1.75 / scale 0.63（登录页实测）",
          B.dom_to_canvas(669, 716, dpr=1.75, vscale=0.6309), (492, 527))
    check("scale 变了结果也要跟着变",
          B.dom_to_canvas(669, 716, dpr=1.75, vscale=1.0), (780, 835))
    check("dpr 变了结果也要跟着变",
          B.dom_to_canvas(669, 716, dpr=1.0, vscale=0.6309), (281, 301))
    vw = FakeWS(json.dumps({"w": 980, "dpr": 1.75, "vscale": 0.6309}))
    check("viewport() 能把参数解出来", (B.viewport(vw) or {}).get("dpr"), 1.75)
    check_true("BOX_JS 里点名取的是最后一个同名元素",
               "leaf[leaf.length - 1]" in B.BOX_JS, B.BOX_JS[-200:])

    print("\n[4] 页面被微信墙挡住时要能看出来")
    wall = FakeWS(json.dumps({"wechatOnly": True, "title": "手机端仅支持微信访问"}))
    logs: list[str] = []
    outer = B.page_info
    try:
        B.page_info = lambda ws: json.loads(ws.evaluate(""))   # type: ignore[assignment]
        real_connect_live = B.connect_live
        B.connect_live = lambda *a, **k: wall                  # type: ignore[assignment]
        B.time.sleep = lambda *_a: None                         # type: ignore[assignment]
        B.open_desktop("https://x/", log=logs.append)
    finally:
        B.page_info = outer                                    # type: ignore[assignment]
        B.connect_live = real_connect_live                     # type: ignore[assignment]
        B.time.sleep = real_sleep                              # type: ignore[assignment]
    check_true("日志里点出「仅支持微信访问」",
               any("仅支持微信" in s for s in logs), str(logs))

    print("\n[5] fill 的参数替换")
    src = B.FILL_JS
    check_true("模板里有 __PHONE__ 占位", "__PHONE__" in src)
    check_true("模板里有 __WANT__ 占位", "__WANT__" in src)

    filled = FakeWS(json.dumps({
        "phone": True, "code": True, "clicked": "立即登录",
        "buttons": [{"t": "获取验证码", "dis": False}, {"t": "立即登录", "dis": False}],
    }))
    logs2: list[str] = []
    res = B.fill(filled, phone="13800000000", code="123456",
                 want="立即登录", log=logs2.append)
    js = [p["expression"] for m, p in filled.calls if m == "Runtime.evaluate"][0]
    check_true("JS 里嵌入了手机号", '"13800000000"' in js, js[:160])
    check_true("JS 里嵌入了验证码", '"123456"' in js)
    check_true("JS 里嵌入了要点的按钮名", '"立即登录"' in js)
    check_true("占位符都替换掉了",
               "__PHONE__" not in js and "__WANT__" not in js and "__SUBMIT__" not in js)
    check("返回值里带 clicked", res.get("clicked"), "立即登录")
    check_true("日志报告点了哪个", any("立即登录" in s for s in logs2), str(logs2))

    print("\n[6] fill 点不到指定按钮时要说清页面上有什么")
    empty = FakeWS(json.dumps({"phone": False, "code": False, "clicked": "",
                               "buttons": [{"t": "获取验证码", "dis": False}]}))
    logs3: list[str] = []
    B.fill(empty, phone="138", want="立即登录", log=logs3.append)
    check_true("说了没找到那个按钮",
               any("没找到按钮" in s for s in logs3), str(logs3))
    check_true("并把页面上有的按钮列出来",
               any("获取验证码" in s for s in logs3), str(logs3))
    check_true("没找到手机号输入框也说了",
               any("没找到手机号输入框" in s for s in logs3), str(logs3))

    print("\n[7] pick_page：挑不到就退回第一个标签页（不是返回 None）")
    real_http = B.http_json
    try:
        B.http_json = lambda *a, **k: [                       # type: ignore[assignment]
            {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://a"},
        ]
        got = B.pick_page("learning/login")
        check("退回 about:blank", (got or {}).get("url"), "about:blank")
        B.http_json = lambda *a, **k: [                       # type: ignore[assignment]
            {"type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://a"},
            {"type": "page", "url": "https://x/learning/login",
             "webSocketDebuggerUrl": "ws://b"},
        ]
        got2 = B.pick_page("learning/login")
        check("匹配得上就用匹配的那个",
              (got2 or {}).get("webSocketDebuggerUrl"), "ws://b")
        B.http_json = lambda *a, **k: [                       # type: ignore[assignment]
            {"type": "other", "url": "devtools://x"},
        ]
        check("没有 page 类型时返回 None", B.pick_page(), None)
    finally:
        B.http_json = real_http                               # type: ignore[assignment]

    print("\n[8] adb 路径 / 序列号（真机探测，失败不算错）")
    adb_path, serial = B._adb()
    print(f"       adb={adb_path!r}  serial={serial!r}")
    check_true("拿到 adb 路径时是指向真实文件",
               (not adb_path) or Path(adb_path).is_file(), adb_path)
    # 缓存必须生效：第二次不能再走探测（探测会打 [detect] 日志）
    cached = B._ADB_CACHE
    again = B._adb()
    check("第二次调用返回同一个结果", again, (adb_path, serial))
    if adb_path:
        check("探测成功时结果被缓存", cached, (adb_path, serial))

    print("\n[9] 真实设备上的浏览器（有设备才算）")
    if adb_path and serial:
        pkg = B.installed_browser()
        print(f"       installed_browser -> {pkg!r}")
        check_true("找到的包名在候选列表里",
                   pkg == "" or pkg in B.BROWSER_PKGS, pkg)
        if pkg:
            act = B.main_activity(pkg)
            print(f"       main_activity -> {act!r}")
            check_true("活动名非空且不带包名前缀",
                       bool(act) and "/" not in act, act)
    else:
        print("  SKIP  本机没有可用设备")

    print("\n[10] config_settings 读 browser 节")
    phone, port = B.config_settings()
    print(f"       当前 config -> phone={phone!r} port={port}")
    check_true("端口是 int 且在合理范围",
               isinstance(port, int) and 1024 <= port <= 65535, port)
    check_true("手机号要么是空的、要么是 11 位数字",
               phone == "" or (phone.isdigit() and len(phone) == 11), phone)

    print("\n[11] 界面上那个按钮确实接上了这条链路")
    ui = (Path(__file__).resolve().parent.parent / "launcher_ui.py").read_text(
        encoding="utf-8")
    check_true("左栏有「手机浏览器登录（桌面版）」按钮",
               "手机浏览器登录（桌面版）" in ui)
    check_true("按钮 command 指向 on_phone_login",
               "command=self.on_phone_login" in ui)
    check_true("on_phone_login 转到后台线程（界面不能卡）",
               "_start_worker(self._phone_login_job" in ui)
    check_true("job 里调用 browser.phone_login",
               "browser.phone_login(" in ui)
    check_true("job 读 config 里的手机号（browser.config_settings）",
               "browser.config_settings()" in ui)
    check_true("运行时按钮会被一起置灰（_set_busy 里有 btn_login）",
               "self.btn_login.configure(state=state)" in ui)
    check_true("配置模板里有 browser.phone",
               '"phone"' in (Path(__file__).resolve().parent.parent
                             / "config" / "config.json").read_text(encoding="utf-8"))

    print("\n" + "=" * 68)
    print(f" 合计 {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
