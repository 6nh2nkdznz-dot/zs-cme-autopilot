"""`VideoWatcher` 的「看完」判据自测。

## 为什么需要这个文件

实测踩到一个**静默失败**：一节 60 分钟的视频看完了，却被判成「未达标」。

```
[watch] 进度 98.8% (59:55 / 1:00:40)     ← 最高只到 98.8%
[watch] 进度 1.6%  (1:00:1 / 1:00:40)    ← 播到末尾后自动循环回开头
[watch] 进度停在 1.6% 已超 180s，判定卡住
[course] ✗ 未达标: 5叶尘宇-BPSD的管理和照护者心理调适.mp4
```

原因有两层：

1. **播放器到末尾不会停在 100%，而是循环回开头**。所以百分比永远到不了 99。
2. 原来只看百分比阈值，没有「播到末尾」和「播完一轮」这两个更直接的证据。

这类错误最难发现：日志一切正常，只是那节课被记成没看、下次重看。

全部用假的读数序列驱动，不碰真机。

运行:
    python scripts\\test_video_end.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

from progress import (  # noqa: E402
    ProgressReading,
    VideoWatcher,
    WatchConfig,
    looks_like_replay_button,
    looks_like_video_end,
    parse_progress,
)

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


class Feeder:
    """按预设序列喂读数；序列用完后一直重复最后一个。"""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.i = 0
        self.reads = 0

    def __call__(self) -> str:
        self.reads += 1
        t = self.texts[min(self.i, len(self.texts) - 1)]
        self.i += 1
        return t


def make(texts, **cfg) -> tuple[VideoWatcher, Feeder]:
    f = Feeder(texts)
    # 轮询间隔设 0，测试要跑得快
    c = WatchConfig(poll_seconds=0, expect_duration=cfg.pop("expect_duration", "60:40"),
                    screen_check_every=1, **cfg)
    w = VideoWatcher(read_progress=f, handle_popup=None, cfg=c, log=lambda m="": None)
    return w, f


def main() -> int:
    print("=" * 68)
    print(" VideoWatcher「看完」判据自测")
    print("=" * 68)

    print("\n[1] parse_progress 要能拆出位置和总时长")
    r = parse_progress("59:55 / 1:00:40")
    check_true("解析出读数", r is not None)
    check("位置", r.position, "59:55")
    check("总时长", r.duration, "1:00:40")
    check_true("百分比由时间算出", 98.0 < r.percent < 99.0, f"实际 {r.percent}")

    print("\n[2] 实测复现场景：98.8% 之后循环回开头 → 必须判为看完")
    w, f = make([
        "30:00 / 1:00:40",
        "59:55 / 1:00:40",     # 最高点，98.8%
        "0:01 / 1:00:40",      # 循环回开头
    ])
    got = w.watch()
    check_true("返回了读数（没判成卡住）", got is not None)
    check_true("标记为 finished", bool(got and got.finished))
    check_true("用的是「播到末尾」那一轮的读数（59:55）",
               bool(got and got.position == "59:55"), f"实际 {got and got.position}")

    print("\n[2b] 实测日志里的真实读数序列也要能收尾")
    #
    # 注意 `1:00:1` 是 OCR 少读一位的产物，它**本身有歧义**
    # （既可能是 `01:00:01` 也可能是 `1:00.1`），所以 `_to_seconds`
    # 按合法的 h:m:s 解析成 3601s。这里不依赖它来判循环，
    # 而是靠「位置≈总时长」那条路收尾 —— 这正是实测时的真实情况。
    w, f = make([
        "98.8% 59:55 / 1:00:40",
        "1.6% 1:00:1 / 1:00:40",
    ])
    c = w.cfg
    c.stall_timeout = 0.0
    got = w.watch()
    check_true("仍然收尾（靠「到末尾」判据）", bool(got and got.finished),
               f"实际 {got}")

    print("\n[3] 阈值不能设到 99 以上（实测最高才 98.8%）")
    check_true("默认阈值 < 99", WatchConfig().target_percent < 99.0,
               f"实际 {WatchConfig().target_percent}")

    print("\n[4] 直接到末尾（不经过循环）也算看完")
    w, f = make(["10:00 / 1:00:40", "1:00:10 / 1:00:40"])
    got = w.watch()
    check_true("标记 finished", bool(got and got.finished))
    check_true("位置是 1:00:10", bool(got and got.position == "1:00:10"))

    print("\n[5] 还差很远时不能误判看完")
    w, f = make(["5:00 / 1:00:40"])
    # 让它在停滞判定处返回，而不是因为「看完」
    cfg = w.cfg
    cfg.stall_timeout = 0.0
    got = w.watch()
    check_true("没有 finished 标记", not (got and got.finished),
               f"实际 finished={got and got.finished}")

    print("\n[6] 末尾容差边界")
    w, _ = make([], end_tolerance_seconds=45.0)
    check("距末尾 10s → 算到末尾",
          w._near_end(ProgressReading(99.0, "x", "1:00:30", "1:00:40")), True)
    check("距末尾 45s → 算到末尾（边界含）",
          w._near_end(ProgressReading(99.0, "x", "59:55", "1:00:40")), True)
    check("距末尾 46s → 不算",
          w._near_end(ProgressReading(99.0, "x", "59:54", "1:00:40")), False)
    check("中途不算",
          w._near_end(ProgressReading(50.0, "x", "30:00", "1:00:40")), False)
    check("缺总时长不误判",
          w._near_end(ProgressReading(50.0, "x", "30:00", "")), False)
    check("缺位置不误判",
          w._near_end(ProgressReading(50.0, "x", "", "1:00:40")), False)

    print("\n[7] 循环检测")
    w, _ = make([])
    near = ProgressReading(98.8, "59:55 / 1:00:40", "59:55", "1:00:40")
    start = ProgressReading(1.6, "0:01 / 1:00:40", "0:01", "1:00:40")
    check("末尾 → 开头 判为循环", w._is_wrap(start, near), True)
    check("中途 → 开头 不算循环",
          w._is_wrap(start, ProgressReading(50.0, "x", "30:00", "1:00:40")), False)
    check("没有前一轮 不算循环", w._is_wrap(start, None), False)
    check("末尾 → 末尾 不算循环（只是抖动）",
          w._is_wrap(near, near), False)

    print("\n[8] 贴片广告仍要被拦住（不能被「到末尾」判据放过）")
    w, f = make(["1:14 / 00:40"], expect_duration="60:40")
    check("40 秒 vs 目录 60:40 → 是广告",
          w._is_flash(ProgressReading(100.0, "1:14 / 00:40", "1:14", "00:40")), True)

    print("\n[9] 广告的「到末尾」不该被当成正片看完")
    w, f = make(["0:35 / 00:40", "0:40 / 00:40", "0:39 / 00:40"],
                expect_duration="60:40")
    c = w.cfg
    c.stall_timeout = 0.0
    got = w.watch()
    # 广告读数被 _is_flash 拦掉，latest 始终为 None
    check_true("广告期间不会返回「看完」", got is None or not got.finished,
               f"实际 {got}")

    print("\n[10] 播完的直接证据：结尾致谢文案 / 重新播放按钮")
    #
    # 用户实测指出的两条判据。原先只看「位置≈总时长」和「循环回开头」，
    # 但视频**不一定循环**、也可能**停在最后一帧不动** —— 那两种情况下
    # 位置读数不再更新、百分比也到不了阈值，一节 45~60 分钟的课就白看了。
    check("感谢聆听", looks_like_video_end("感谢聆听"), True)
    check("感谢观看", looks_like_video_end("谢谢观看 感谢大家"), True)
    check("感谢收看", looks_like_video_end("感谢收看"), True)
    check("英文致谢不算（没实测过，不冒认）",
          looks_like_video_end("Thanks for watching"), False)
    check("播放中不误判",
          looks_like_video_end("老年认知症 45:27 简介 目录"), False)
    check("空文本", looks_like_video_end(""), False)

    check("重新播放", looks_like_replay_button("重新播放"), True)
    check("控制条里的重播",
          looks_like_replay_button("| 29:30 | 照 | 重播 | 43:48 |"), True)
    check("播放/暂停键不算",
          looks_like_replay_button("| 29:30 | 照 | 43:48 | 高清"), False)
    check("空文本", looks_like_replay_button(""), False)

    print("\n[11] 整屏判据接进 watch()：靠文案收尾")
    w, f = make(["50:00 / 1:00:40"], read_screen=lambda: "感谢聆听")
    c = w.cfg
    c.stall_timeout = 0.0
    got = w.watch()
    check_true("文案命中即判完成", bool(got and got.finished), f"实际 {got}")

    print("\n[12] 整屏判据：靠「重新播放」按钮收尾")
    w, f = make(["50:00 / 1:00:40"],
                read_screen=lambda: "| 50:00 | 重新播放 | 1:00:40 |")
    c = w.cfg
    c.stall_timeout = 0.0
    got = w.watch()
    check_true("按钮命中即判完成", bool(got and got.finished), f"实际 {got}")

    print("\n[13] 「立即停止」：看护循环要能被打断")
    #
    # 实测用户反馈：点了「停止」，视频还继续播到下课。
    #
    # 起因是 `post_stop()` 只在**节点边界**生效，而「看护整门课」是
    # **一个**要跑几小时的节点 —— 全靠框架的话，点了等于没点。
    #
    # 所以看护循环自己查 `cfg.should_stop`：`main.py` 的 `_stopper()`
    # 把它接到 `tasker.stopping` 上（MaaFramework 官方样例 demo1.py:131
    # 也是这个路子：check stopping after your atomic operation）。
    calls = {"n": 0}

    def _never() -> bool:
        calls["n"] += 1
        return False

    # (a) 没人要求停止时，行为要和以前一模一样
    w, _f = make(["5:00 / 1:00:40"], should_stop=_never)
    w.cfg.stall_timeout = 0.0
    got = w.watch()
    check_true("没要求停止 → 照常看护（有返回值）", got is not None, f"实际 {got}")
    check_true("确实问过要不要停", calls["n"] > 0, f"实际问了 {calls['n']} 次")

    # (b) 一开始就要求停止 → **一次读数都不读**，当场返回
    f2 = Feeder(["5:00 / 1:00:40"])
    c2 = WatchConfig(poll_seconds=0, expect_duration="60:40", screen_check_every=1,
                     should_stop=lambda: True)
    logs2: list[str] = []
    w2 = VideoWatcher(read_progress=f2, handle_popup=None, cfg=c2, log=logs2.append)
    got2 = w2.watch()
    check("要停就立刻返回（不判完成）", got2, None)
    check("一次读数都没读", f2.reads, 0)
    check_true("日志说清了为什么退出",
               any("收到停止" in m for m in logs2), f"实际 {logs2}")

    # (c) 查询本身抛异常 → 当「没要求停止」，不能把整门课弄崩
    def _boom() -> bool:
        raise RuntimeError("实例已销毁")

    w3, _f = make(["5:00 / 1:00:40"], should_stop=_boom)
    w3.cfg.stall_timeout = 0.0
    got3 = w3.watch()
    check_true("查询出错也不崩、照常返回", got3 is not None, f"实际 {got3}")

    # (d) `_nap()` 可打断：要求停止时不等满
    #
    # 这一条针对的是**轮询间隔**（默认 15~20 秒）。用整段 `time.sleep`
    # 的话，点了停止最多要等一整个间隔才轮到下一次检查，用户会觉得没反应。
    w4, _f = make([], should_stop=lambda: True)
    t0 = time.monotonic()
    w4._nap(5.0)
    used = time.monotonic() - t0
    check_true("_nap 要停时几乎不等待", used < 0.5, f"实际等了 {used:.3f}s")

    # (e) 不要求停止时 `_nap()` 要睡满（否则会变成忙等，把 OCR 打爆）
    w5, _f = make([], should_stop=lambda: False)
    t0 = time.monotonic()
    w5._nap(0.4)
    used = time.monotonic() - t0
    check_true("_nap 正常等待要睡满", used >= 0.35, f"实际等了 {used:.3f}s")

    # (f) 没传 should_stop（老调用点）也要能用
    w6, _f = make([])
    t0 = time.monotonic()
    w6._nap(0.3)
    check_true("没传 should_stop 也能用",
               time.monotonic() - t0 >= 0.25 and w6._stopping() is False)

    print("\n" + "=" * 68)
    print(f" 结果: {PASS} 通过 / {FAIL} 失败")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
