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

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
