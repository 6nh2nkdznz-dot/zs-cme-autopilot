"""微信闪退检测与恢复的自测。

## 为什么单独测这一块

微信闪退后整个看护会**空转**（对空白截图、所有识别失败），而一门课要跑
几小时。这类「不出声的失败」最值得有测试盯着。

这里测的是**判定逻辑**，不真去杀微信 —— 那会干扰正在跑的任务。
真实设备上的 `app_alive` 用当前状态验证（微信活着应返回 True）。

运行:
    python scripts\\test_app_recover.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

import app_recover as A  # noqa: E402

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


def main() -> int:
    print("=" * 68)
    print(" 微信闪退检测与恢复自测")
    print("=" * 68)

    print("\n[1] 命令拼装")
    check("包名", A.WECHAT_PKG, "com.tencent.mm")
    check("pidof 子命令", A._PIDOF, ["shell", "pidof", "com.tencent.mm"])
    check_true("启动命令用 monkey（不依赖 Activity 名）",
               "monkey" in A._LAUNCH, f"实际 {A._LAUNCH}")
    check_true("启动命令带包名", A.WECHAT_PKG in A._LAUNCH)
    check_true("启动命令带 LAUNCHER 类别",
               "android.intent.category.LAUNCHER" in A._LAUNCH)

    print("\n[2] 序列号解析：必须在多设备环境下选对（踩过的坑）")
    serial = A._adb_serial()
    print(f"       当前解析结果: {serial!r}")
    # config 里 address 是空的（自动探测模式），所以不能指望它。
    # 解析结果要么是 loopback（MuMu），要么为空（纯命令行环境）。
    check_true("非空时是 MuMu 的 loopback 形态或设备名",
               serial == "" or ":" in serial or serial.startswith("emulator"),
               f"实际 {serial!r}")

    print("\n[3] 真实设备：微信应该活着")
    alive = A.app_alive(log=lambda m="": None)
    print(f"       app_alive() = {alive}")
    if alive is None:
        print("       SKIP  当前环境查不出状态（没有 adb / 没有设备）")
    else:
        check("微信活着", alive, True)
        print("\n[4] 已活着时 ensure_alive 不该重启它")
        # 用一个会记录调用的假 launch 来验证「没被调用」
        called: list[int] = []
        real_launch = A.launch_app

        def spy_launch(log=print):
            called.append(1)
            return real_launch(log=log)

        A.launch_app = spy_launch          # type: ignore[assignment]
        try:
            ok = A.ensure_alive(log=lambda m="": None, settle=0.1)
        finally:
            A.launch_app = real_launch     # type: ignore[assignment]
        check("返回 True", ok, True)
        check("没有多余地重启微信", called, [])

    print("\n[5] app_alive 的判定只看输出，不看退出码")
    #
    # 踩过的坑：`pidof` 找到了进程、输出里就是 PID，但退出码不是 0，
    # 于是被判成「进程不存在」，白重启了一次微信。
    # 这段用假 adb 模拟「rc≠0 但有输出」来盯住这个行为。
    real_run = A._run
    real_path = A._adb_path
    real_serial = A._adb_serial
    try:
        A._adb_path = lambda: "fake-adb"                 # type: ignore[assignment]
        A._adb_serial = lambda *a, **k: "127.0.0.1:1"    # type: ignore[assignment]
        A._run = lambda *a, **k: (False, "40013")        # type: ignore[assignment]
        check("rc=False 但有 PID 输出 → 判为活着", A.app_alive(log=lambda m="": None), True)
        A._run = lambda *a, **k: (True, "")              # type: ignore[assignment]
        check("无输出 → 判为不在", A.app_alive(log=lambda m="": None), False)
        A._run = lambda *a, **k: (False, "")             # type: ignore[assignment]
        check("rc=False 且无输出 → 判为不在", A.app_alive(log=lambda m="": None), False)
        A._adb_path = lambda: ""                          # type: ignore[assignment]
        check("拿不到 adb → None（不是 False，避免盲目重启）",
              A.app_alive(log=lambda m="": None), None)
    finally:
        A._run = real_run                                # type: ignore[assignment]
        A._adb_path = real_path                          # type: ignore[assignment]
        A._adb_serial = real_serial                      # type: ignore[assignment]

    print("\n[6] 崩溃记录的解析（回答「微信为什么自己退出」）")
    #
    # 用户问过两次这个问题。答案是：**不是我们关的** ——
    # 微信自己的原生视频解码线程崩了（2026-10-07 一整天 17 次，
    # 全是 MediaCodec_loop + libstagefright.so + 同一个崩溃地址）。
    # 下面用真实 logcat 行的副本钉住解析。
    real_crash = [
        "--------- beginning of crash",
        "10-07 12:33:12.468  2207  4534 F libc    : Fatal signal 11 "
        "(SIGSEGV), code 2 (SEGV_ACCERR), fault addr 0x73fbeb2d718e in tid "
        "4534 (MediaCodec_loop), pid 2207 (com.tencent.mm)",
        "10-07 15:46:58.099  4565 25135 F libc    : Fatal signal 11 "
        "(SIGSEGV), code 2 (SEGV_ACCERR), fault addr 0x73fbeb2d715e in tid "
        "25135 (MediaCodec_loop), pid 4565 (com.tencent.mm)",
        "10-07 16:00:00.000  1111  2222 F libc    : Fatal signal 11 "
        "(SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x1 in tid "
        "2222 (RenderThread), pid 1111 (com.android.chromium)",
    ]
    real_run6 = A._run
    real_path6 = A._adb_path
    real_serial6 = A._adb_serial
    try:
        A._adb_path = lambda: "fake-adb"                  # type: ignore[assignment]
        A._adb_serial = lambda *a, **k: "127.0.0.1:1"     # type: ignore[assignment]
        A._run = lambda *a, **k: (True, "\n".join(real_crash))  # type: ignore[assignment]
        recs = A.crashes()
        check("只收微信自己的崩溃（chromium 那条不算）", len(recs), 2)
        check("时间戳解析成 HH:MM:SS", recs[0]["when"], "12:33:12")
        check("信号解析", recs[0]["signal"], "11 (SIGSEGV)")
        check("线程解析（这就是指纹）", recs[0]["thread"], "MediaCodec_loop")
        check("包名", recs[0]["pkg"], "com.tencent.mm")
        check("保留原始行（给开发者看）",
              recs[0]["line"].startswith("10-07 12:33:12.468"), True)

        # explain_crash 必须在**确认**崩溃时才下结论，且要点出线程指纹
        lines: list[str] = []
        got = A.explain_crash(log=lines.append)
        check("查到崩溃 → 返回 True", got, True)
        joined = "\n".join(lines)
        check_true("说清是解码器崩的、不是我们关的",
                   "不是我们把它关掉的" in joined, joined[:200])
        check_true("点了 MediaCodec_loop 这个指纹",
                   "MediaCodec_loop" in joined, joined[:200])
        check_true("给了处理办法（重新拉起、接着看）",
                   "接着看" in joined, joined[:200])

        # 查不到就**不许猜**
        A._run = lambda *a, **k: (True, "")               # type: ignore[assignment]
        lines2: list[str] = []
        check("没有任何崩溃记录 → False", A.explain_crash(log=lines2.append), False)
        check_true("查不到时明确说「查不到」，不猜原因",
                   "没查到" in "\n".join(lines2), "\n".join(lines2)[:200])
    finally:
        A._run = real_run6                                # type: ignore[assignment]
        A._adb_path = real_path6                          # type: ignore[assignment]
        A._adb_serial = real_serial6                      # type: ignore[assignment]

    print("\n[7] crash_recovery：先取证、再恢复，顺序不能反")
    #
    # 用户报「微信会一直自己退出」时，程序原来的表现是「微信没了 →
    # 所有识别失败 → 看护空转」，日志里一句原因都没有。所以这一层
    # 必须在**动手之前**先把原因读出来。
    real_explain = A.explain_crash
    real_alive = A.app_alive
    real_usable = A.ensure_usable
    real_ensure = A.ensure_alive
    try:
        events: list[str] = []

        # 日志行和事件写在同一个列表里，比较时只挑事件那几条
        # （`[app] …` 是给人看的输出）。
        def steps() -> list[str]:
            return [e for e in events if not e.startswith("[app]")]

        A.reset_crash_rounds()
        A.explain_crash = lambda log=print: events.append("explain") or True   # type: ignore[assignment]
        A.app_alive = lambda log=print: events.append("alive") or True         # type: ignore[assignment]
        A.ensure_usable = lambda **k: events.append("usable") or True          # type: ignore[assignment]
        ok = A.crash_recovery(log=events.append, settle=0.0)
        check("进程活着且没卡 → 返回 True", ok, True)
        check("先 explain 再探活，最后才 ensure_usable",
              steps(), ["explain", "alive", "usable"])
        check("崩溃计数 +1", A.crash_rounds(), 1)
        check_true("把「本轮崩过」写进日志",
                   any("本轮已经遇到 1 次微信崩溃" in e for e in events),
                   str(events))

        # 进程真的没了 → 走 ensure_alive（拉起），不再走 ensure_usable
        events.clear()
        A.explain_crash = lambda log=print: events.append("explain") or True   # type: ignore[assignment]
        A.app_alive = lambda log=print: events.append("alive") or False        # type: ignore[assignment]
        A.ensure_alive = lambda **k: events.append("ensure_alive") or True     # type: ignore[assignment]
        A.ensure_usable = lambda **k: events.append("usable") or True          # type: ignore[assignment]
        ok = A.crash_recovery(log=events.append, settle=0.0)
        check("进程没了 → 返回 True（已被拉起）", ok, True)
        check("走的是 ensure_alive 而不是 ensure_usable",
              steps(), ["explain", "alive", "ensure_alive"])
        check("再崩一次计数到 2", A.crash_rounds(), 2)
        A.reset_crash_rounds()
        check("reset 后归零", A.crash_rounds(), 0)
    finally:
        A.explain_crash = real_explain      # type: ignore[assignment]
        A.app_alive = real_alive            # type: ignore[assignment]
        A.ensure_usable = real_usable       # type: ignore[assignment]
        A.ensure_alive = real_ensure        # type: ignore[assignment]
        A.reset_crash_rounds()

    print("\n[8] crash_watchdog：看护期间「看不见的失败」要当场报出来")
    #
    # 「播放整门课」那个节点要跑几小时，微信崩了它照样「在跑」。
    # 所以轮询里要主动查，而不是等下一节课。
    real_crashes = A.crashes
    real_alive = A.app_alive
    real_stuck = A.stuck_empty
    try:
        # 8a 没有新增 → 不吭声，baseline 原样返回
        lines: list[str] = []
        A.crashes = lambda limit=30: [{"when": "12:00:00"}]           # type: ignore[assignment]
        A.app_alive = lambda log=print: True                          # type: ignore[assignment]
        A.stuck_empty = lambda log=print: False                       # type: ignore[assignment]
        got = A.crash_watchdog(baseline=1, log=lines.append)
        check("没有新增 → baseline 不变", got, 1)
        check("没有异常 → 一句话都不说", lines, [])

        # 8b 多了一条崩溃 → 报出来，并把 baseline 推到新值
        lines.clear()
        A.crashes = lambda limit=30: [                                 # type: ignore[assignment]
            {"when": "12:00:00", "signal": "11 (SIGSEGV)",
             "thread": "MediaCodec_loop", "pkg": "com.tencent.mm", "line": "x"},
            {"when": "12:34:56", "signal": "11 (SIGSEGV)",
             "thread": "MediaCodec_loop", "pkg": "com.tencent.mm", "line": "y"},
        ]
        got = A.crash_watchdog(baseline=1, log=lines.append)
        check("新增一条 → 返回 2", got, 2)
        joined = "\n".join(lines)
        check_true("说清新增了几次", "新增 1 次崩溃" in joined, joined)
        check_true("带上最后一条的时间", "12:34:56" in joined, joined)
        check_true("带上线程指纹", "MediaCodec_loop" in joined, joined)

        # 8c 缓冲区被冲掉（条数变少）→ 不能当成「又崩了一次」
        A.crashes = lambda limit=30: []                                # type: ignore[assignment]
        lines.clear()
        got = A.crash_watchdog(baseline=5, log=lines.append)
        check("缓冲区被冲掉 → baseline 不往下调", got, 5)

        # 8d 进程没了但没查到崩溃记录 → 也要说，且不许编原因
        A.crashes = lambda limit=30: []                                # type: ignore[assignment]
        A.app_alive = lambda log=print: False                          # type: ignore[assignment]
        lines.clear()
        A.crash_watchdog(baseline=0, log=lines.append)
        joined = "\n".join(lines)
        check_true("进程没了 → 报出来", "微信进程没了" in joined, joined)
        check_true("没查到崩溃记录时不编原因",
                   "缓冲区被冲掉" in joined, joined)

        # 8e 进程在但卡空白页 → 这是「看起来像退出」的另一种形态
        A.app_alive = lambda log=print: True                           # type: ignore[assignment]
        A.stuck_empty = lambda log=print: True                         # type: ignore[assignment]
        lines.clear()
        A.crash_watchdog(baseline=0, log=lines.append)
        check_true("卡空白页 → 报出来",
                   "卡在空白中转页" in "\n".join(lines), "\n".join(lines))

        # 8f 探活的噪音不要刷进用户日志
        A.stuck_empty = lambda log=print: False                        # type: ignore[assignment]
        lines.clear()
        A.crash_watchdog(baseline=0, log=lines.append)
        check("一切正常时零输出（探活细节被吞掉）", lines, [])
    finally:
        A.crashes = real_crashes            # type: ignore[assignment]
        A.app_alive = real_alive            # type: ignore[assignment]
        A.stuck_empty = real_stuck          # type: ignore[assignment]

    print("\n[9] 横竖屏：转横了必须发现、并尽量转回来")
    #
    # 实测（2026-10-07 19:40 那轮）：设备自己转成横屏（1920x1080），
    # 画布还是 720x1280 → 课程目录**一节课都认不出来**，
    # 日志里只留一句「屏幕上没找到任何视频条目」，看着像「目录页读错了」。
    # 而且框架不会重算缩放，转回来之前每一步都是错的。
    real_path9 = A._adb_path
    real_serial9 = A._adb_serial
    real_run9 = A._run
    try:
        import struct as _struct

        def png(w: int, h: int) -> bytes:
            head = b"\x89PNG\r\n\x1a\n" + _struct.pack(">I", 13) + b"IHDR"
            return head + _struct.pack(">II", w, h)

        # 屏幕方向直接读原始 PNG 的头 —— 不走框架，所以不会被缓存骗到
        A._adb_path = lambda: "fake-adb"                  # type: ignore[assignment]
        A._adb_serial = lambda *a, **k: "127.0.0.1:1"     # type: ignore[assignment]

        import subprocess as _sp

        real_subrun = _sp.run
        _sp.run = lambda *a, **k: type("R", (), {"stdout": png(1080, 1920)})()  # type: ignore[assignment]
        check("竖屏图 → True", A.canvas_portrait(), True)
        _sp.run = lambda *a, **k: type("R", (), {"stdout": png(1920, 1080)})()  # type: ignore[assignment]
        check("横屏图 → False", A.canvas_portrait(), False)
        _sp.run = lambda *a, **k: type("R", (), {"stdout": b"not a png"})()     # type: ignore[assignment]
        check("不是 PNG → None（判断不了就别拦）", A.canvas_portrait(), None)
        _sp.run = lambda *a, **k: type("R", (), {"stdout": png(720, 720)})()    # type: ignore[assignment]
        check("正方形 → None", A.canvas_portrait(), None)
        _sp.run = real_subrun                              # type: ignore[assignment]

        # ensure_portrait：已经竖着就什么都不做（不许白发转屏命令）
        sent: list[list[str]] = []
        states = [True]
        A._run = lambda *a, **k: (True, "")               # type: ignore[assignment]
        real_canvas = A.canvas_portrait
        A.canvas_portrait = lambda: states[0]             # type: ignore[assignment]
        _sp.run = lambda cmd, **k: (sent.append(list(cmd)), type("R", (), {"stdout": b""})())[1]  # type: ignore[assignment]
        got = A.ensure_portrait(log=lambda m="": None)
        check("已经竖屏 → True", got, True)
        check("已经竖屏时一条命令都不发", sent, [])

        # 横屏 → 发两条 settings 命令，转过来了就 True
        states.clear()
        seq = [False, False, True]
        A.canvas_portrait = lambda: seq.pop(0) if seq else True   # type: ignore[assignment]
        sent.clear()
        got = A.ensure_portrait(log=lambda m="": None)
        check("横屏转回竖屏 → True", got, True)
        joined = " ".join(" ".join(c) for c in sent)
        check_true("先关自动旋转（否则刚转回来又被转走）",
                   "accelerometer_rotation 0" in joined, joined)
        check_true("再锁 user_rotation 0", "user_rotation 0" in joined, joined)

        # 转不回来 → 必须返回 False，让上层停手（不能硬着头皮乱点）
        seq = [False] + [False] * 20
        A.canvas_portrait = lambda: seq.pop(0) if seq else False  # type: ignore[assignment]
        A._run = lambda *a, **k: (True, "")               # type: ignore[assignment]
        lines9: list[str] = []
        got = A.ensure_portrait(log=lines9.append)
        check("转不回来 → False", got, False)
        # ★ 报错只写原因、不写排查方案（用户 2026-10-08 的要求）。
        # 这一句原来缀着「请手动把模拟器转成竖屏（或把它的窗口横竖比调回
        # 竖的），然后重开本程序」，现在只留事实。所以这条断言反过来了：
        # 以前要求「说清要人工做什么」，现在要求**不许**教人怎么做。
        joined9 = "\n".join(lines9)
        check_true("说清是转不回来", "转不回来" in joined9, joined9)
        check_true("不夹带「请…」式的排查方案",
                   "请" not in joined9 and "手动" not in joined9, joined9)
        A.canvas_portrait = real_canvas                   # type: ignore[assignment]
    finally:
        A._adb_path = real_path9                          # type: ignore[assignment]
        A._adb_serial = real_serial9                      # type: ignore[assignment]
        A._run = real_run9                                # type: ignore[assignment]

    print("\n[10] 微信在后台时也要捞到前台")
    #
    # 实测那一轮：微信进程活着、前台却是 `app.lawnchair`（桌面），
    # 截图全是桌面 → 所有页面判定失败 → 上层只报「不在平台页面里」，
    # 完全看不出真因是「微信在后台」。
    real_alive10 = A.app_alive
    real_stuck10 = A.stuck_empty
    real_fg10 = A.foreground
    real_launch10 = A.launch_app
    try:
        A.app_alive = lambda log=print: True              # type: ignore[assignment]
        A.stuck_empty = lambda log=print: False           # type: ignore[assignment]
        fg_seq = ["Window{1 u0 app.lawnchair/app.lawnchair.LawnchairLauncher}",
                  "Window{2 u0 com.tencent.mm/com.tencent.mm.ui.LauncherUI}"]
        A.foreground = lambda: fg_seq.pop(0) if fg_seq else "com.tencent.mm/x"  # type: ignore[assignment]
        launched: list[int] = []
        A.launch_app = lambda log=print: launched.append(1) or True  # type: ignore[assignment]
        lines10: list[str] = []
        got = A.ensure_usable(log=lines10.append, settle=0.0)
        check("在后台 → 返回 True（已捞回来）", got, True)
        check("确实拉了一次", launched, [1])
        check_true("日志说清「在后台」而不是含糊说「不在页面」",
                   "微信在后台" in "\n".join(lines10), "\n".join(lines10))

        # 已经在前台 → 一次都不许拉
        A.foreground = lambda: "Window{3 u0 com.tencent.mm/com.tencent.mm.ui.LauncherUI}"  # type: ignore[assignment]
        launched.clear()
        lines10.clear()
        A.ensure_usable(log=lines10.append, settle=0.0)
        check("已经在前台 → 不拉", launched, [])
        check("已经在前台 → 零输出", lines10, [])
    finally:
        A.app_alive = real_alive10                        # type: ignore[assignment]
        A.stuck_empty = real_stuck10                      # type: ignore[assignment]
        A.foreground = real_fg10                          # type: ignore[assignment]
        A.launch_app = real_launch10                      # type: ignore[assignment]

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
