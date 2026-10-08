"""手机浏览器（桌面版）的接管：改 UA、跳登录页、填手机号/验证码。

## 为什么需要这个

在模拟器里跑微信有被封号的风险（用户实测提出），而平台其实**有两套前端**：

    手机版 /mobile/  → 只认微信，非微信 UA 直接拒绝
    桌面版 /         → 普通浏览器就能开

桌面版靠 UA 分流，所以在手机的浏览器上把 UA 覆盖成桌面 UA，就能
**完全不开微信**地看到同一批课程 —— 既没有封号风险，也不碰模拟器那个
反复崩的微信解码器（`MediaCodec_loop` / `libstagefright.so`）。

## 顺序是唯一的坑（实测踩过）

CDP 的 `Emulation.setUserAgentOverride` **必须在第一次 `Page.navigate` 之前**设好：

    先在 about:blank 上改 UA / 视口  →  再 Page.navigate

顺序反了（页面已经按手机 UA 初始化完，再改 UA），SPA 会直接卡死：
`Runtime.evaluate` 连续超时，看起来像"改 UA 也绕不过"，其实是**刷新时机错了**。
用户一句「你是不是在切换 UA 之后没有点击刷新」点破了这件事。

## 为什么用 CDP 而不是截图点

1. 改 UA 只有 CDP 能做 —— `am start --user-agent` 到不了 Android Chromium
   （它内部转成 `IntentDispatcher`，UA 被丢掉）。
2. 填手机号/验证码用 JS 直接写 + 派发 `input`/`change` 事件，比"点输入框
   再发按键"可靠得多：网页输入框在 WebView 里对合成点击经常没反应
   （这个项目在微信里已经踩过好几轮）。

## 怎么和 adb 配合

模拟器直接就能用；**真机要开「USB 调试」并用数据线连上电脑**，然后：

    adb forward tcp:9222 localabstract:chrome_devtools_remote

`launch_debug()` 会自动做 `forward`。真机没连 adb 时 CDP 是够不着的 ——
那时只能靠用户手动在浏览器里切换"桌面版网站"。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

#: 桌面版 UA（Windows Chrome）。实测用它进桌面版首页 `wechatOnly: False`。
UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HOME = "https://elearning.zs-hospital.sh.cn/"
LOGIN = "https://elearning.zs-hospital.sh.cn/learning/login"

#: 设备上可能装的浏览器包名。模拟器自带的是 `com.android.chromium`
#: （`versionName=110.0.5481.154.1`，`codePath=/system/product/app/TrichromeChrome`）。
BROWSER_PKGS = (
    "com.android.chromium",
    "com.android.chrome",
    "com.tencent.mtt",
    "com.UCMobile",
)

#: 我们把设备的调试端口转发到本机这个端口
LOCAL_PORT = 9222


def _quiet(_msg: str) -> None:
    """默认日志：什么都不打（给库函数用，避免污染调用方的输出）。"""


# ---------------------------------------------------------------- adb 小工具


def _adb() -> tuple[str, str]:
    """返回 `(adb 路径, 序列号)`，**结果缓存**。

    缓存是必须的：`detect.find_adb()` 每次都会打
    `[detect] 运行中实例的 Android 版本: …` / `[detect] adb 可用: …`
    好几行，而本模块一条命令就可能触发好几次探测，日志会被刷满
    （实测：一次 `installed_browser()` 打了 4 行）。
    这里直接调 `detect.find_adb(log=_quiet)`，把探测噪音吞掉；
    序列号仍走 `app_recover`（它处理过「config 里地址是空的、
    adb 同时看到两台设备」这个坑：`adb: more than one device/emulator`）。
    """
    global _ADB_CACHE
    if _ADB_CACHE is not None:
        return _ADB_CACHE
    path = ""
    try:
        import detect

        path = str(detect.find_adb(log=_quiet) or "")
    except Exception:  # noqa: BLE001 - 探测失败下面还会兜底
        path = ""
    try:
        import app_recover as A

        serial = A._adb_serial()
    except Exception:  # noqa: BLE001
        serial = ""
    found = (path, serial)
    if found[0]:
        _ADB_CACHE = found
    return found


#: `_adb()` 的缓存（`(adb 路径, 序列号)`）；探测失败时不缓存，留着重试
_ADB_CACHE: tuple[str, str] | None = None


def shell(args: list[str], timeout: float = 30.0) -> tuple[bool, str]:
    """跑一条 `adb shell` 命令，返回 `(成功?, 文本)`。文本已 strip。"""
    adb, serial = _adb()
    if not adb:
        return False, "没找到 adb"
    cmd = [adb] + (["-s", serial] if serial else []) + ["shell"] + args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    out = (p.stdout or b"").decode("utf-8", "replace").strip()
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    return p.returncode == 0, (out or err)


def forward(local: int = LOCAL_PORT) -> bool:
    """`adb forward tcp:<local> localabstract:chrome_devtools_remote`。"""
    adb, serial = _adb()
    if not adb:
        return False
    cmd = [adb] + (["-s", serial] if serial else []) + [
        "forward", f"tcp:{local}", "localabstract:chrome_devtools_remote",
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def installed_browser(log: Callable[[str], None] = _quiet) -> str:
    """在设备上挑一个装了、且能响应「打开网址」的浏览器包名。

    先按 `BROWSER_PKGS` 的顺序问 `pm list packages`，再用
    `cmd package resolve-activity` 确认它真的能当浏览器用 ——
    有些 ROM 装了包但活动被禁用，光看包名会误判。
    """
    ok, out = shell(["pm", "list", "packages"])
    if not ok:
        return ""
    installed = {ln.strip().removeprefix("package:") for ln in out.splitlines()
                 if ln.strip().startswith("package:")}
    for pkg in BROWSER_PKGS:
        if pkg not in installed:
            continue
        ok2, act = shell([
            "cmd", "package", "resolve-activity", "--brief",
            "-a", "android.intent.action.VIEW", "-d", "about:blank",
        ])
        # resolve-activity 不给指定包时看的是"系统默认浏览器"，这里只用来
        # 判断「这个包是不是能处理 VIEW」：能，它就会出现在候选里。
        if ok2 and pkg in act:
            log(f"[browser] 用设备上的浏览器 {pkg}")
            return pkg
    # 没有明确的默认浏览器时，退一步：装了就用第一个
    for pkg in BROWSER_PKGS:
        if pkg in installed:
            log(f"[browser] 用设备上的浏览器 {pkg}（未确认默认浏览器）")
            return pkg
    return ""


def main_activity(pkg: str) -> str:
    """问设备这个包的主 Activity 叫什么。

    写死 `com.google.android.apps.chrome.Main` 只在部分版本上成立；
    真机上的 Chrome / 各家浏览器活动名都不一样，所以问系统：
    `cmd package resolve-activity --brief -n <pkg>/` 会回 `<pkg>/<activity>`。
    """
    ok, out = shell(["cmd", "package", "resolve-activity", "--brief", "-n", f"{pkg}/"])
    if ok and "/" in out:
        cand = out.splitlines()[-1].strip()
        if cand.startswith(pkg) and "/" in cand:
            return cand.split("/", 1)[1]
    return "com.google.android.apps.chrome.Main"


# ---------------------------------------------------------------- 启动 / 连接


def debug_ready(port: int = LOCAL_PORT, host: str = "127.0.0.1") -> bool:
    """本机 `http://127.0.0.1:<port>/json/version` 通不通。"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json/version", timeout=5):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def launch_debug(
    *,
    url: str = "about:blank",
    port: int = LOCAL_PORT,
    restart: bool = True,
    timeout: float = 40.0,
    log: Callable[[str], None] = print,
) -> bool:
    """让设备上的浏览器带着**调试端口**起来，并把端口转发到本机。

    分三步：
      1. `adb forward tcp:<port> localabstract:chrome_devtools_remote`
      2. （`restart=True` 时）`am force-stop` 后重新开，带上
         `--es com.android.chrome.REMOTE_DEBUGGING_PORT <port>`
      3. 轮询 `/json/version` 直到通

    `restart=False` 只做 1 + 3，用于「浏览器已经带着调试端口在跑」的情况
    （比如上一轮跑完没关）。
    """
    if not forward(port):
        log("[browser] ⚠ adb forward 失败")
        return False

    pkg = installed_browser(log=log)
    if not pkg:
        log("[browser] ✗ 设备上没找到可用的浏览器")
        return False

    if restart:
        shell(["am", "force-stop", pkg])
        time.sleep(2.0)
        act = main_activity(pkg)
        ok, out = shell([
            "am", "start", "-n", f"{pkg}/{act}",
            "--es", "com.android.chrome.REMOTE_DEBUGGING_PORT", str(port),
            "-a", "android.intent.action.VIEW", "-d", url,
        ])
        if not ok:
            log(f"[browser] ⚠ 启动浏览器返回非 0：{out}")
        log(f"[browser] 已启动 {pkg}/{act}（调试端口 {port}）")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if debug_ready(port):
            log(f"[browser] ✓ 调试通道已就绪（127.0.0.1:{port}）")
            return True
        time.sleep(1.5)
    log("[browser] ✗ 调试通道一直没起来。可能是这个浏览器不认 "
        "REMOTE_DEBUGGING_PORT，或设备是网页版 Chrome 之外的内核。")
    return False


class WS:
    """够用的 WebSocket 客户端：文本帧、不分片、客户端掩码。

    标准库没有 WebSocket，而这台机器上也没装 `websocket-client`
    （`pip list` 只有 `MaaFw / MaaAgentBinary / numpy / pillow / requests`），
    所以自己实现一份。只实现 CDP 用得到的那部分：发文本、收文本。
    """

    def __init__(self, url: str, timeout: float = 30.0):
        rest = url.split("://", 1)[1]
        hostport, path = rest.split("/", 1)
        host, port = hostport.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手时连接被关闭")
            buf += chunk
        if b"101" not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError(
                "WebSocket 握手失败: " + buf.split(b"\r\n", 1)[0].decode("latin1"))
        self._buf = buf.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _recv_exact(self, n: int) -> bytes:
        out = self._buf[:n]
        self._buf = self._buf[n:]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise RuntimeError("连接已关闭")
            out += chunk
        return out

    def send_text(self, text: str) -> None:
        payload = text.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_text(self) -> str:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            data = self._recv_exact(length)
            if opcode == 0x8:
                raise RuntimeError("服务端关闭了连接")
            if opcode in (0x1, 0x2):
                return data.decode("utf-8", "replace")

    def call(self, method: str, **params):
        """发一条 CDP 命令，返回它的响应（忽略中间的事件帧）。"""
        self._id += 1
        self.send_text(json.dumps({"id": self._id, "method": method,
                                   "params": params}))
        while True:
            msg = json.loads(self.recv_text())
            if msg.get("id") == self._id:
                return msg

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    # -- 语义化的便捷方法 ------------------------------------------------

    def evaluate(self, expression: str):
        """跑一段 JS，返回它的值（`returnByValue`）。"""
        r = self.call("Runtime.evaluate", expression=expression,
                      returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("result", {}).get("value")


def http_json(path: str, port: int = LOCAL_PORT, host: str = "127.0.0.1"):
    url = f"http://{host}:{port}{path}"
    with urllib.request.urlopen(url, timeout=15) as fh:
        return json.load(fh)


def pick_page(url_part: str = "", port: int = LOCAL_PORT):
    """挑一个标签页。`url_part` 为空时挑第一个 `type == "page"`。

    `url_part` 匹配不上时**退回第一个标签页**，而不是返回 `None`：
    调用方常常在「刚启动浏览器、只有一个 about:blank」的时刻连过来
    （`open_desktop()` 就是这样），这时按 URL 挑必然挑空。
    """
    tabs = [t for t in http_json("/json", port=port) if t.get("type") == "page"]
    if url_part:
        matched = [t for t in tabs if url_part in t.get("url", "")]
        if matched:
            return matched[0]
    return tabs[0] if tabs else None


def connect(url_part: str = "", port: int = LOCAL_PORT, timeout: float = 30.0) -> WS:
    """连到某个标签页的 CDP。挑不到标签页就抛 `RuntimeError`。"""
    page = pick_page(url_part, port=port)
    if not page:
        raise RuntimeError(
            "没有可用的标签页 —— 先 launch_debug() 把浏览器带调试端口拉起来")
    return WS(page["webSocketDebuggerUrl"], timeout=timeout)


def connect_live(
    url_part: str = "",
    *,
    port: int = LOCAL_PORT,
    timeout: float = 20.0,
    probe_timeout: float = 4.0,
    log: Callable[[str], None] = _quiet,
) -> WS:
    """连到**真正活着**的标签页。

    为什么要多做一步「问一句」：`/json` 里列的标签页可能是**僵尸** ——
    浏览器被 `am force-stop` 重启过之后，旧 target 还挂在列表里，
    WebSocket 也能握手成功，但 `Runtime.evaluate` **一律超时**
    （实测反复踩到：`--info` 直接卡死 20 秒然后抛 TimeoutError）。

    所以这里逐个标签页试一句 `Runtime.evaluate("1+1")`：答得上来才算数。
    全部答不上来就抛 `RuntimeError`，让调用方去重启浏览器。
    """
    tabs = [t for t in http_json("/json", port=port) if t.get("type") == "page"]
    if url_part:
        # 先看匹配的，再看其余的 —— 但要保持顺序，不能丢掉没匹配上的。
        tabs = ([t for t in tabs if url_part in t.get("url", "")]
                + [t for t in tabs if url_part not in t.get("url", "")])
    if not tabs:
        raise RuntimeError("没有可用的标签页 —— 先 launch_debug() 把浏览器拉起来")

    last: Exception | None = None
    for tab in tabs:
        ws = None
        try:
            ws = WS(tab["webSocketDebuggerUrl"], timeout=probe_timeout)
            ws.call("Runtime.evaluate", expression="1+1", returnByValue=True)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if ws is not None:
                ws.close()
            continue
        ws.timeout = timeout
        if ws.sock is not None:
            ws.sock.settimeout(timeout)
        log(f"[browser] 用标签页: {tab.get('url', '')[:60]}")
        return ws
    raise RuntimeError(f"所有标签页都问不动（僵尸 target）: {last}")

# ---------------------------------------------------------------- 桌面模式


def set_desktop_ua(
    ws: WS,
    *,
    log: Callable[[str], None] = _quiet,
) -> None:
    """把连接设成「桌面 UA + 触摸模拟」。

    **调用顺序要紧**：本函数必须在任何 `Page.navigate` 之前调用。
    它自己不导航，就是为了让调用方把顺序摆对（见模块 docstring）。
    """
    ws.call("Emulation.setUserAgentOverride", userAgent=UA_DESKTOP,
            platform="Win32", acceptLanguage="zh-CN,zh;q=0.9")
    # 【不要】再调 Emulation.setDeviceMetricsOverride。
    #
    # 实测（2026-10-07）：Android Chrome 上它会**和页面自己的视口打架** ——
    # 桌面版首页声明 width=1230 的布局宽度，平台就用 720 覆盖，浏览器只好
    # 把 pageScaleFactor 压到 0.585 去适配，结果右侧那一列被裁掉：登录页
    # 只看得到左边的插画，「输入手机号 / 输入验证码 / 立即登录」全在屏幕外。
    #
    # 不覆盖视口时浏览器自己算（首页 1230x1947、登录页 980x1551，dpr 1.75，
    # visualViewport.scale 0.50 / 0.63），整页都在屏内、版式正常。
    # 坐标换算见 `dom_to_canvas()`。
    ws.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=1)
    log("[browser] ✓ 已切成桌面 UA（顺序：先 UA，后导航）")


# 画布像素 = 设备像素 / SCREEN_TO_CANVAS。
# 框架的 `screenshot.target_long_side` 是 1280，而屏幕是 1080x1920，
# 于是 1080 / 720 = 1.5 —— 截图和点击坐标都活在这套 720x1280 的画布里。
SCREEN_TO_CANVAS = 1.5


def dom_to_canvas(css_x: float, css_y: float, *, dpr: float, vscale: float) -> tuple[int, int]:
    """把页面里的 CSS 坐标换算成**框架画布坐标**（能直接喂给 post_click）。

    实测换算链（`debug/_measure.py` / `debug/_calib.py` 量的）：

        CSS 坐标 --×dpr--> 设备像素 --×visualViewport.scale--> 屏幕像素
                 --÷1.5--> 框架画布坐标

    三个因子都是**先量再算**的，不许写死：`dpr` 跟设备密度有关（这台是 1.75），
    `visualViewport.scale` 是浏览器为了把桌面宽度塞进手机屏而压的（首页 0.50、
    登录页 0.63），换页面就变。量它们的 JS 见 `VIEWPORT_JS`。
    """
    sx = float(css_x) * dpr * vscale
    sy = float(css_y) * dpr * vscale
    return (int(round(sx / SCREEN_TO_CANVAS)), int(round(sy / SCREEN_TO_CANVAS)))


VIEWPORT_JS = r"""
JSON.stringify({
  w: innerWidth, h: innerHeight,
  dpr: +devicePixelRatio.toFixed(4),
  vscale: +visualViewport.scale.toFixed(4),
  vw: Math.round(visualViewport.width), vh: Math.round(visualViewport.height),
  sw: document.documentElement.scrollWidth,
  sh: document.documentElement.scrollHeight,
})
"""


def viewport(ws: "WS") -> dict:
    """量当前页面的视口参数，喂给 `dom_to_canvas()`。"""
    return json.loads(ws.evaluate(VIEWPORT_JS) or "{}")


BOX_JS = r"""
(() => {
  const want = __WANT__;
  const hit = (key) => {
    const all = [...document.querySelectorAll('input,button,a,div,span,li,p')];
    const leaf = all.filter(e => e.children.length === 0 &&
                                  ((e.textContent || '').trim() === key ||
                                   e.placeholder === key));
    if (!leaf.length) return null;
    // 同名多个时取**最后一个**：登录页那个「立即登录」在 DOM 里出现了两遍，
    // 前面那份是隐藏的模板，点它什么都不会发生（第一版就栽在这）。
    const e = leaf[leaf.length - 1];
    const r = e.getBoundingClientRect();
    return {text: key, x: Math.round(r.x), y: Math.round(r.y),
            w: Math.round(r.width), h: Math.round(r.height),
            cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2)};
  };
  return JSON.stringify(want.map(hit));
})()
"""


def boxes(ws: "WS", texts: list[str]) -> list[dict | None]:
    """按文案量页面元素的位置（CSS 坐标），量不到给 `None`。"""
    js = BOX_JS.replace("__WANT__", json.dumps(list(texts), ensure_ascii=False))
    return json.loads(ws.evaluate(js) or "[]")


def open_desktop(
    url: str = HOME,
    *,
    settle: float = 12.0,
    port: int = LOCAL_PORT,
    retries: int = 2,
    log: Callable[[str], None] = print,
) -> WS:
    """把浏览器摆成「桌面版 + 停在 url」，返回仍连着的 WS（调用方负责 close）。

    做四件事，顺序不能变：
      1. 连到 `about:blank` 标签页（没有空白页就退回第一个标签页）
      2. `set_desktop_ua`（改 UA）
      3. `Page.navigate(url)`
      4. 等 `settle` 秒让 SPA 渲染完

    `retries`：标签页可能是**上一个已经死掉的**（实测见过：`/json` 里还列着，
    但连上去之后 `Runtime.evaluate` 一律超时 —— 那是浏览器被 `am force-stop`
    重启过、旧 target 成了僵尸）。所以每失败一次就重新挑一次标签页。
    """
    last: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        if attempt > 1:
            # 僵尸 target 只能靠"重新起一个"来甩掉。
            log(f"[browser] 第 {attempt} 次尝试：重新拉起浏览器")
            launch_debug(url="about:blank", port=port, log=_quiet)
        ws = None
        try:
            ws = connect_live("about:blank", port=port, log=log)
            set_desktop_ua(ws, log=log)
            ws.call("Page.enable")
            ws.call("Page.navigate", url=url)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if ws is not None:
                ws.close()
            continue
        time.sleep(settle)
        try:
            info = page_info(ws)
        except Exception as exc:  # noqa: BLE001
            last = exc
            ws.close()
            continue
        if info.get("wechatOnly"):
            log("[browser] ⚠ 页面说「仅支持微信访问」—— UA 没生效")
        else:
            log(f"[browser] ✓ 桌面版已打开: {info.get('title', '')[:40]}")
        return ws
    raise RuntimeError(f"连不上浏览器调试通道：{last}")


INFO_JS = r"""
JSON.stringify({
  url: location.href, title: document.title,
  innerW: window.innerWidth, innerH: window.innerHeight,
  wechatOnly: document.body ? document.body.innerText.includes('仅支持微信') : null,
  loggedIn: !document.body.innerText.includes('登录') ||
            document.body.innerText.includes('退出'),
  text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 300),
})
"""


def page_info(ws: WS) -> dict:
    """问页面几个关键事实：地址、标题、有没有被微信墙挡住、有没有登录。"""
    val = ws.evaluate(INFO_JS)
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return {"text": val}
    return {}


#: 找输入框 + 点按钮 + 派发事件的 JS。三个动作都做成函数，按需拼。
FILL_JS = r"""
(() => {
  const setVal = (el, v) => {
    const proto = Object.getPrototypeOf(el);
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    // 直接赋 el.value 在很多框架里不会触发 v-model 的更新，
    // 必须走原型上的 setter，再手动派发 input/change。
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const find = (...keys) => [...document.querySelectorAll('input')].find(el =>
      keys.some(k => ((el.placeholder || '') + (el.name || '') + (el.id || ''))
                     .includes(k)));
  const hit = (...texts) => [...document.querySelectorAll('button,a,span,div')]
      .find(el => texts.some(t => (el.innerText || '').trim() === t));

  const out = {phone: false, code: false, clicked: '', buttons: []};
  const PHONE = __PHONE__, CODE = __CODE__, SUBMIT = __SUBMIT__, WANT = __WANT__;
  if (PHONE) {
    const el = find('手机', '电话', 'phone', 'mobile');
    if (el) { el.focus(); setVal(el, PHONE); out.phone = true; }
  }
  if (CODE) {
    const el = find('验证码', 'code', 'captcha');
    if (el) { el.focus(); setVal(el, CODE); out.code = true; }
  }
  // 页面上的可点按钮都报回去 —— 不猜哪个是"提交"，
  // 让调用方按名字点，免得点错（实测踩过：想点「立即登录」，
  // 结果最先命中的是「获取验证码」）。
  out.buttons = [...document.querySelectorAll('button,a,span,div')]
      .map(el => ({t: (el.innerText || '').trim(),
                   dis: el.disabled === true}))
      .filter(o => o.t && o.t.length <= 8 &&
                   /登录|验证码|提交|确定/.test(o.t))
      .filter((o, i, a) => a.findIndex(x => x.t === o.t) === i)
      .slice(0, 8);
  if (WANT) {
    const b = [...document.querySelectorAll('button,a,span,div')]
        .filter(el => (el.innerText || '').trim() === WANT);
    if (b.length) { b[b.length - 1].click(); out.clicked = WANT; }
  }
  return JSON.stringify(out);
})()
"""


def fill(
    ws: WS,
    *,
    phone: str = "",
    code: str = "",
    want: str = "",
    log: Callable[[str], None] = _quiet,
) -> dict:
    """往登录页的输入框里写手机号 / 验证码，可选点一个**指定名字**的按钮。

    返回值形如 `{"phone": True, "code": True, "clicked": "立即登录",
    "buttons": [{"t": "获取验证码", "dis": False}, …]}`。

    `want` 是**要点的按钮文案**（如 `"获取验证码"` / `"立即登录"`）。
    为什么必须按名字点、不给个 `submit=True` 猜：实测踩过 ——
    想点「立即登录」，结果先命中的是「获取验证码」，页面什么都没发生。
    所以这里改成「点名的那个」，并且把页面上有哪些按钮一起报回来，
    日志里能直接看出该点哪个。

    为什么不用「点输入框 + 发按键」：这个项目在微信 WebView 里已经反复踩过
    「合成点击进不到网页输入框」，而 JS 直接写 + 派发事件是走页面自己的
    事件流，稳定得多。
    """
    js = (FILL_JS
          .replace("__PHONE__", json.dumps(phone, ensure_ascii=False))
          .replace("__CODE__", json.dumps(code, ensure_ascii=False))
          .replace("__SUBMIT__", "false")
          .replace("__WANT__", json.dumps(want, ensure_ascii=False)))
    val = ws.evaluate(js)
    try:
        res = json.loads(val) if isinstance(val, str) else {}
    except ValueError:
        res = {}
    if phone:
        log(f"[browser] {'✓ 已填入手机号' if res.get('phone') else '✗ 没找到手机号输入框'}")
    if code:
        log(f"[browser] {'✓ 已填入验证码' if res.get('code') else '✗ 没找到验证码输入框'}")
    if want:
        if res.get("clicked"):
            log(f"[browser] ✓ 已点击「{res['clicked']}」")
        else:
            have = "、".join(str(b.get("t")) for b in (res.get("buttons") or []))
            log(f"[browser] ✗ 没找到按钮「{want}」；页面上有：{have or '（没扫到）'}")
    return res


def buttons(ws: WS) -> list[dict]:
    """当前页面扫到的可点按钮（`[{"t": 文案, "dis": 是否禁用}]`）。"""
    val = ws.evaluate(FILL_JS
                      .replace("__PHONE__", '""').replace("__CODE__", '""')
                      .replace("__SUBMIT__", "false").replace("__WANT__", '""'))
    try:
        res = json.loads(val) if isinstance(val, str) else {}
    except ValueError:
        res = {}
    return list(res.get("buttons") or [])


def phone_login(
    *,
    phone: str = "",
    code: str = "",
    want: str = "",
    url: str = LOGIN,
    restart: bool = False,
    reuse: bool = True,
    port: int = LOCAL_PORT,
    log: Callable[[str], None] = print,
) -> bool:
    """一键：带调试端口拉起浏览器 → 改桌面 UA → 跳验证码登录页 →（可选）填号。

    这是界面上那个「手机浏览器登录（桌面版）」按钮背后的动作。
    返回 True 表示**页面已经停在桌面版的登录页**（不代表登录成功 ——
    短信验证码得由用户自己看手机填）。

    `phone` + `want="获取验证码"` 就是「填好手机号并把验证码发出去」，
    用户收到短信后只差输入 6 位数字，不用再手打 11 位手机号。

    `reuse=True`（默认）时先看看**当前那个标签页**是不是已经在桌面版上：
    是的话就**不重启、不重新导航**，直接在原地填表 —— 否则每点一次按钮
    都把页面刷新一遍，用户刚输入的验证码会被清掉。
    """
    log("[browser] 准备手机浏览器的桌面版登录…")

    ws = None
    if reuse and not restart:
        ws = _attach_existing(port=port, log=log)

    if ws is None:
        if not launch_debug(url="about:blank", restart=restart, port=port, log=log):
            return False
        try:
            ws = open_desktop(url, port=port, log=log)
        except Exception as exc:  # noqa: BLE001 - 连不上要把原因说清楚
            log(f"[browser] ✗ 连不上浏览器调试通道: {exc}")
            return False

    try:
        if phone or code or want:
            fill(ws, phone=phone, code=code, want=want, log=log)
        log("[browser] 登录页已就绪。接下来：")
        if phone:
            log("[browser]   1. 手机上会收到短信验证码")
            log("[browser]   2. 把 6 位数字填进「输入验证码」")
            log("[browser]      （也可以不用手打：回来点这个按钮之前把手机号填进"
                " data/config.json 的 browser.phone，或者让程序用 "
                "`--run browser --enter-code 123456` 代填）")
            log("[browser]   3. 点「立即登录」")
        else:
            log("[browser]   1. 在 data/config.json 里填上 browser.phone"
                "（下次会自动填号并发验证码），或者直接在页面里输入手机号")
            log("[browser]   2. 点「获取验证码」，手机收短信")
            log("[browser]   3. 输入验证码、点「立即登录」")
        log("[browser] 登录状态会留在浏览器里，之后程序不用再管登录。")
    finally:
        ws.close()
    return True


def _attach_existing(*, port: int = LOCAL_PORT, log: Callable[[str], None] = print):
    """已经在桌面版上就直接复用那个标签页（返回 WS），否则返回 None。"""
    try:
        ws = connect_live(url_part="/learning/login", port=port, log=log)
    except Exception:  # noqa: BLE001
        return None
    try:
        info = page_info(ws)
    except Exception:  # noqa: BLE001 - 僵尸 target：连得上但问不动
        ws.close()
        return None
    if info.get("wechatOnly") or not info.get("url", "").startswith("http"):
        ws.close()
        return None
    log(f"[browser] 已经在桌面版上，直接复用当前页面: {info.get('url', '')[:60]}")
    return ws


def enter_code(
    code: str,
    *,
    submit: bool = True,
    port: int = LOCAL_PORT,
    log: Callable[[str], None] = print,
) -> bool:
    """把短信验证码填进当前登录页，可选直接点「立即登录」。

    为什么要这个入口：验证码只能由用户看手机读出来，但**填进去这件事不必
    让用户去点手机屏幕**（模拟器里点网页输入框本来就不可靠）。用户只要把
    6 位数字告诉程序，剩下交给 JS —— `fill()` 走的是页面自己的事件流。
    """
    if not code:
        log("[browser] 没给验证码")
        return False
    try:
        ws = connect_live(url_part="/learning/login", port=port, log=log)
    except Exception as exc:  # noqa: BLE001
        log(f"[browser] ✗ 连不上登录页: {exc}")
        return False
    try:
        res = fill(ws, code=code, want="立即登录" if submit else "", log=log)
        if not res.get("code"):
            log("[browser] ✗ 验证码没填进去 —— 可能页面已经不在登录页了")
            return False
        return True
    finally:
        ws.close()


def config_settings() -> tuple[str, int]:
    """从 `data/config.json` 读 `browser.phone` 和 `browser.debug_port`。

    读不到就返回 `("", LOCAL_PORT)` —— 这两个都是可选优化，
    没有它们功能照样能用（只是手机号要手输）。
    """
    try:
        from controller import load_config

        cfg = load_config()
    except Exception:  # noqa: BLE001
        return "", LOCAL_PORT
    sec = cfg.get("browser") or {}
    phone = str(sec.get("phone") or "").strip()
    try:
        port = int(sec.get("debug_port") or LOCAL_PORT)
    except (TypeError, ValueError):
        port = LOCAL_PORT
    return phone, port


def show_info(*, port: int = LOCAL_PORT) -> int:
    """CLI：把当前页面的事实打出来（地址 / 标题 / 微信墙 / 登录态）。"""
    try:
        ws = connect_live(port=port, log=print)
    except Exception as exc:  # noqa: BLE001
        print(f"连不上：{exc}")
        return 1
    try:
        print(json.dumps(page_info(ws), ensure_ascii=False, indent=2))
    finally:
        ws.close()
    return 0


def main() -> int:
    import argparse

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(
        description="手机浏览器桌面版：改 UA / 跳登录页 / 填手机号验证码")
    ap.add_argument("--login", action="store_true",
                    help="拉起浏览器并打开桌面版登录页（默认动作）")
    ap.add_argument("--url", default="", help="改成打开这个地址")
    ap.add_argument("--phone", default="", help="自动填入手机号")
    ap.add_argument("--code", default="", help="自动填入短信验证码")
    ap.add_argument("--click", default="",
                    help="填完点这个文案的按钮，如「获取验证码」/「立即登录」")
    ap.add_argument("--enter-code", default="",
                    help="把 6 位短信验证码填进当前登录页并点「立即登录」")
    ap.add_argument("--no-submit", action="store_true",
                    help="配合 --enter-code：只填码，不点「立即登录」")
    ap.add_argument("--buttons", action="store_true",
                    help="只列出当前页面扫到的按钮，不填不点")
    ap.add_argument("--no-restart", action="store_true",
                    help="浏览器已经在带调试端口跑着，不要重启它")
    ap.add_argument("--port", type=int, default=0,
                    help="本机调试端口（默认取 config 里的 browser.debug_port）")
    ap.add_argument("--no-config", action="store_true",
                    help="不要从 data/config.json 读手机号/端口")
    ap.add_argument("--info", action="store_true", help="只看当前页面事实，不动它")
    args = ap.parse_args()

    if args.info:
        return show_info(port=args.port or LOCAL_PORT)

    if args.buttons:
        ws = connect_live(port=args.port or LOCAL_PORT, log=print)
        try:
            for b in buttons(ws):
                print(f"  {'（禁用）' if b.get('dis') else '      '} {b.get('t')}")
        finally:
            ws.close()
        return 0

    if args.enter_code:
        ok = enter_code(args.enter_code, submit=not args.no_submit,
                        port=args.port or LOCAL_PORT)
        return 0 if ok else 1

    url = args.url or (LOGIN if args.login else HOME)
    phone = args.phone
    port = args.port
    if not phone and not args.no_config:
        cfg_phone, cfg_port = config_settings()
        phone = cfg_phone
        port = cfg_port
    ok = phone_login(phone=phone, code=args.code, want=args.click,
                     url=url, restart=not args.no_restart, port=port or LOCAL_PORT)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
