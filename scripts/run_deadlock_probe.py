"""在自定义识别**回调内部**做 OCR：复现自死锁，并验证正确写法。

## 怎么跑

    python scripts\\run_deadlock_probe.py bad     # 复现死锁（有超时兜底）
    python scripts\\run_deadlock_probe.py good    # 验证正确写法
    python scripts\\run_deadlock_probe.py both    # 两个都跑

## 背景

自定义识别回调 `CustomRecognition.analyze(context, argv)` 运行在 **tasker 自己的
执行线程**上。所以回调里这样做会自死锁：

    job = context.tasker.post_recognition(OCR, JOCR(), img)   # 投给同一个 tasker 的队列
    job.wait()                                                 # 在队列自己的线程上等它

队列正忙着跑当前回调，那个 job 永远轮不到 → 无限阻塞。
实测表现是「任务挂住几分钟、无日志、无 pending 记录」。

正确写法是用 Context 的同步直调接口，它们不经过 tasker 队列：

    context.run_recognition_direct(JRecognitionType.OCR, JOCR(), img)

本脚本通过真实管线节点触发回调，两条路径各跑一次，用子进程 + 超时隔离，
确保死锁那一路不会把整个测试拖死。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
MAIN = HERE / "main.py"
NODE = {
    "bad": "__deadlock_probe_bad",
    "good": "__deadlock_probe_good",
}


def run_node(mode: str, timeout: float) -> tuple[bool, str, float]:
    """在子进程里跑一个探针节点。返回 (进程是否按时结束, 输出, 耗时)。"""
    node = NODE[mode]
    cmd = [sys.executable, str(MAIN), "--task", node]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, cwd=str(HERE.parent),
        )
        elapsed = time.time() - started
        return True, (proc.stdout or "") + (proc.stderr or ""), elapsed
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - started
        out = ""
        for part in (exc.stdout, exc.stderr):
            if part:
                out += part if isinstance(part, str) else part.decode("utf-8", "replace")
        return False, out, elapsed


def detect_deadlock(output: str) -> tuple[bool, float | None]:
    """从探针输出判断回调内那次 OCR 是否死锁。

    **不能按「进程是否超时」判断** —— 探针自己带超时保护（在子线程里等），
    所以即使回调内死锁，进程也会正常结束。真正的判据是探针打的标记行：

        [probe] ✗ 12s 未返回 → **自死锁**
        [probe] ✓ 返回（0.02s）: '...'

    返回 (是否死锁, 首次耗时秒)。没找到标记时返回 (False, None)。
    """
    deadlocked = False
    first_sec: float | None = None

    for line in (output or "").splitlines():
        s = line.strip()
        if "自死锁" in s and s.startswith("[probe]"):
            deadlocked = True
        if s.startswith("[probe] ✓ 返回") and first_sec is None:
            m = re.search(r"（([\d.]+)s）", s)
            if m:
                first_sec = float(m.group(1))

    return deadlocked, first_sec


def summarize(output: str) -> str:
    """从输出里挑出探针自己打的诊断行。"""
    keep = []
    for line in (output or "").splitlines():
        s = line.strip()
        if s.startswith("[probe]"):
            keep.append(s)
    return "\n".join(keep) if keep else "(没有 [probe] 诊断输出)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["bad", "good", "both"], default="both", nargs="?")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="每个探针的进程级超时（秒）")
    args = ap.parse_args()

    modes = ["bad", "good"] if args.mode == "both" else [args.mode]
    results: dict[str, tuple[bool, float | None, bool]] = {}

    for mode in modes:
        title = {
            "bad": "A) 回调内用 tasker.post_recognition().wait()  ← 预期自死锁",
            "good": "B) 回调内用 context.run_recognition_direct()  ← 预期正常",
        }[mode]
        print("=" * 70)
        print(f" {title}")
        print("=" * 70)

        exited, out, elapsed = run_node(mode, args.timeout)
        print(summarize(out))
        deadlocked, first_sec = detect_deadlock(out)
        if not exited:
            print(f"  → 进程 {elapsed:.1f}s 未结束，已强制终止")
        else:
            print(f"  → 进程按时结束（{elapsed:.1f}s）")
        results[mode] = (deadlocked, first_sec, exited)
        print()

    print("=" * 70)
    print(" 结论")
    print("=" * 70)
    ok_all = True

    if "bad" in results:
        deadlocked, first_sec, _ = results["bad"]
        if deadlocked:
            print("  tasker.post_recognition 在回调内: ✗ 自死锁（符合预期，"
                  "证明这条路不能用）")
        else:
            print("  tasker.post_recognition 在回调内: 竟然没死锁 —— "
                  "需重新分析（也许绑定版本已修）")
            ok_all = False

    if "good" in results:
        deadlocked, first_sec, _ = results["good"]
        if deadlocked:
            print("  context.run_recognition_direct:  ✗ 也死锁了 —— 需要继续排查")
            ok_all = False
        else:
            sec = f"{first_sec:.2f}s" if first_sec is not None else "?"
            print(f"  context.run_recognition_direct:  ✓ 正常返回（{sec}）")

    print()
    if ok_all:
        print("  ✓ 结论：回调内必须用 context.run_recognition_direct()")
        return 0
    print("  ✗ 结论与预期不符，见上面各行")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
