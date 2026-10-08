"""课程视频进度判定与看护。

背景（你已确认）：
  * 进度**必须真实播放到时长，快进无效**；
  * 播放中**会弹验证**（如「请点击确认还在学习」）；
  * 视频是**全屏/横屏**播放的。

因此本模块做三件事：
  1. 从播放器画面 OCR 出进度百分比（识别区域由你登录后的截图确定）；
  2. 判定「本课是否已学完」，学完才切下一课；
  3. 看护播放过程：检测弹窗 → 点掉 → 继续，直到进度达标。

为什么用 OCR 而不是模板匹配读进度：
  进度数字每分钟都在变，做模板要几十张图；OCR 一遍就够，且用
  Custom 识别可以直接把百分比当结构化结果返回给 Pipeline。
  代价是需要你提供一张播放页截图来确定 ROI。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# 进度解析
# --------------------------------------------------------------------------

@dataclass
class ProgressReading:
    """一次进度读取结果。"""
    percent: float
    raw_text: str
    position: str = ""   # 例如 03:21
    duration: str = ""   # 例如 12:45
    finished: bool = False


# 匹配 75% / 75 % / 100%
_PCT_RE = re.compile(r"(\d{1,3})\s*%")
# 匹配 03:21 / 1:02:33
_TIME_RE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?)")


def parse_progress(text: str) -> ProgressReading | None:
    """从 OCR 文本里解析进度。

    播放器通常同时显示「03:21 / 12:45」和百分比，优先用百分比；
    没有百分比时用 已播/总长 现算。
    """
    if not text:
        return None
    flat = text.replace("\u3000", " ")

    pct: float | None = None
    m = _PCT_RE.search(flat)
    if m:
        pct = float(m.group(1))

    times = _TIME_RE.findall(flat)
    pos, dur = (times[0], times[1]) if len(times) >= 2 else ("", "")

    if pct is None and pos and dur:
        pct = _ratio_percent(pos, dur)
    if pct is None:
        return None

    pct = max(0.0, min(100.0, pct))
    return ProgressReading(
        percent=pct,
        raw_text=flat.strip(),
        position=pos,
        duration=dur,
        finished=pct >= 99.0,
    )


def _ratio_percent(pos: str, dur: str) -> float | None:
    try:
        d = _to_seconds(dur)
        if d <= 0:
            return None
        return _to_seconds(pos) / d * 100.0
    except (ValueError, IndexError):
        return None


# --------------------------------------------------------------------------
# 看护循环
# --------------------------------------------------------------------------

@dataclass
class WatchConfig:
    """看护参数。"""
    #: 达到即认为学完。
    #:
    #: **实测教训**：这个值不能设到 99 以上。视频播到末尾时**不会停在 100%，
    #: 而是自动循环回开头**，所以一节课最高只观测到 98.8%：
    #:
    #:     [watch] 进度 98.8% (59:55 / 1:00:40)     ← 最高就到这
    #:     [watch] 进度 1.6%  (1:00:1 / 1:00:40)    ← 循环回开头了
    #:
    #: 阈值设 99.0 就**永远判不了达标**——60 分钟的视频白看，而且日志看不出异常。
    #: 取 97.0 留出余量，同时靠下面的「播到末尾」判定兜底。
    target_percent: float = 97.0
    poll_seconds: float = 15.0        # 轮询间隔
    stall_timeout: float = 180.0      # 进度多久不动就认为卡住/暂停
    max_watch_seconds: float = 3 * 3600
    verify_popup_every: float = 5.0   # 弹窗检测间隔（比进度轮询更勤）
    #: 目录里写的这节课时长（如 `60:40`）。用来识别**贴片广告**——
    #: 广告也会走到 100%，不校验时长就会把正片误标成已学完（实测踩过）。
    #: 同时也用来判定「播到末尾」。
    expect_duration: str = ""
    #: 播放器报的总时长低于「目录时长 × 此系数」就认定是广告。
    #: 取 0.6 是因为平台的时长可能含片头片尾，但广告差一个数量级。
    duration_tolerance: float = 0.6
    #: 播放位置距总时长小于这个秒数就认为「已播完」。
    #:
    #: 比百分比阈值可靠：百分比受 OCR 误差和总时长读数影响，
    #: 而「位置≈时长」是直接证据。实测 59:55 / 1:00:40 只差 45 秒，
    #: 说明播放器确实走到了末尾。
    end_tolerance_seconds: float = 45.0
    #: 无参可调用对象，返回**整屏** OCR 文本。
    #:
    #: 用来找播完的直接证据（结尾致谢文案、重新播放按钮）。
    #: 实测：视频不一定循环，也可能停在最后一帧不动，那时只看位置/百分比
    #: 判不出「看完」，一节 45~60 分钟的课就白看了。
    read_screen: Callable[[], str] | None = None
    #: 每隔几次轮询读一次整屏（整屏 OCR 比读进度贵，不必每次）
    screen_check_every: int = 3
    #: 无参可调用对象，返回 True 表示外面要求停止（用户点了「立即停止」）。
    #:
    #: ## 为什么看护循环必须自己查它
    #:
    #: `MaaTasker.post_stop()` **只在节点边界生效**，而「看护整门课」是
    #: **一个**要跑几小时的节点 —— 光靠它，点了停止得等整门课跑完才真的停。
    #: 用户看到的「点了没反应」就是这个。
    #:
    #: MaaFramework 官方样例给的正是这个解法
    #: （`sample/python/demo1.py:131`：check stopping after your atomic
    #: operation, and return immediately）—— 自定义动作里自己查
    #: `context.tasker.stopping`。`main.py` 的 `_stopper()` 负责把它接进来。
    should_stop: Callable[[], bool] | None = None


def _to_seconds(t: str) -> float:
    r"""把 `03:21` / `1:02:33` 换算成秒。解析不了返回 0。

    ## 为什么不能「补零到 3 段」了事

    实测踩过：OCR 少读一位时会出现 `1:00:1` 这种串。无脑按 **h:m:s** 解析
    会得到 **1 小时 0 分 1 秒**，而它其实应该是 `01:00:01` 被截了一位
    （也可能是 `1:00.1` 这类）。**位置一旦比总时长还大，所有基于位置的判据
    全部失效**，而日志上看不出异常。

    所以逐段做范围校验：
      * 分、秒必须 < 60，否则这个串不是合法时间 → 返回 0（宁可判不出）；
      * 3 段时首段当小时，2 段时首段当分钟。

    返回 0 表示「解析不出」，调用方都按「信息缺失、不做判断」处理，
    不会因此误判成「播完了」。
    """
    if not t or ":" not in t:
        return 0.0
    parts = t.split(":")
    if not 1 <= len(parts) <= 3:
        return 0.0
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0.0
    if any(n < 0 for n in nums):
        return 0.0
    # 分、秒必须落在 0..59
    if any(n >= 60 for n in nums[1:]):
        return 0.0
    if len(parts) == 3:
        return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
    if len(parts) == 2:
        return float(nums[0] * 60 + nums[1])
    return float(nums[0])


class VideoWatcher:
    """看护一个视频直到学完。

    read_progress: 无参可调用对象，返回当前进度文本（由调用方接 OCR）。
    handle_popup:  无参可调用对象，返回 True 表示处理掉了弹窗。
    """

    def __init__(
        self,
        read_progress: Callable[[], str],
        handle_popup: Callable[[], bool] | None = None,
        cfg: WatchConfig | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self._read = read_progress
        self._popup = handle_popup
        self.cfg = cfg or WatchConfig()
        self.log = log

    def _stopping(self) -> bool:
        """外面有没有要求停止。

        回调本身出错就当「没要求停止」—— 不能因为问一句「要停吗」
        把整门课的看护弄崩。
        """
        if self.cfg.should_stop is None:
            return False
        try:
            return bool(self.cfg.should_stop())
        except Exception:  # noqa: BLE001
            return False

    def _nap(self, seconds: float) -> None:
        """**可打断**的等待。

        ## 为什么不能直接 `time.sleep(poll_seconds)`

        一轮轮询里最长的一段就是这个 sleep（默认 15~20 秒）。用整段 sleep
        的话，点了「立即停止」最多要等一整个间隔才轮到下一次检查 ——
        用户会觉得按钮没反应。

        所以切成 0.25 秒的小段，每段问一次要不要停。停就立刻返回，
        剩下的时间不睡了。
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            if self._stopping():
                return
            rest = deadline - time.monotonic()
            if rest <= 0:
                return
            time.sleep(min(0.25, rest))

    def _is_flash(self, reading: ProgressReading) -> bool:
        """这一读数是不是**贴片广告/预告**，而不是正片。

        ## 为什么必须判

        实测踩过一次严重的假阳性：

            目录里写「5叶尘宇-BPSD的管理和照护者心理调适.mp4  60:40」
            点开后读到的却是 (1:14 / 00:40) → 100% → 判定「已学完」

        播放器先放了 **40 秒的贴片广告**，而广告也会走到 100%。
        于是这条 60 分钟的视频被记成「已完成」、永久跳过——
        **看起来一切正常，实际根本没看**。这类静默错误最难发现。

        判据：播放器报的总时长明显短于目录里的时长。
        偏差阈值取 `duration_tolerance`（默认 0.6），
        因为平台的「时长」可能含片头片尾，不完全等于播放器报的秒数；
        但广告差了一个数量级，0.6 足够区分。
        """
        expect = _to_seconds(self.cfg.expect_duration)
        got = _to_seconds(reading.duration)
        if expect <= 0 or got <= 0:
            return False        # 缺信息时不判，避免误伤
        if got >= expect * self.cfg.duration_tolerance:
            return False
        self.log(
            f"[watch] ⚠ 疑似贴片广告：播放器报 {reading.duration}"
            f"（{got:.0f}s），目录写 {self.cfg.expect_duration}"
            f"（{expect:.0f}s）→ 不认这个进度"
        )
        return True

    def _near_end(self, reading: ProgressReading) -> bool:
        """播放位置是否已经到末尾了？

        ## 为什么需要这个判据（而不是只看百分比）

        实测：视频播到末尾**不会停在 100%，而是自动循环回开头**：

            [watch] 进度 98.8% (59:55 / 1:00:40)     ← 最高只到 98.8%
            [watch] 进度 1.6%  (1:00:1 / 1:00:40)    ← 循环回开头

        只看百分比的话，阈值设 99 就永远判不了达标，60 分钟白看。
        「位置 ≈ 总时长」是更直接的证据：只差 45 秒，说明确实播完了。
        """
        total = _to_seconds(reading.duration)
        pos = _to_seconds(reading.position)
        if total <= 0 or pos <= 0:
            return False
        return (total - pos) <= self.cfg.end_tolerance_seconds

    def _is_wrap(self, reading: ProgressReading, prev: ProgressReading | None) -> bool:
        """是否刚从「接近末尾」跳回「开头」——即播完一轮循环了。

        这是**已经看完一整个视频**的强证据：播放器会循环，说明它走到了末尾。
        """
        if prev is None:
            return False
        if not self._near_end(prev):
            return False
        total = _to_seconds(reading.duration)
        pos = _to_seconds(reading.position)
        # 回到前 15% 以内，且确实比之前小很多
        return total > 0 and pos < total * 0.15 and pos < _to_seconds(prev.position)

    def _screen_says_done(self) -> str:
        """读整屏，看是否有「播完」的直接证据。返回命中的说明，没有则空串。

        两类证据（实测用户指出）：
          * 结尾致谢文案（感谢聆听 / 感谢观看 …）
          * 控制条上的「重新播放」——播放器播完后播放键会变成它

        为什么不能只靠位置/百分比：视频**不一定循环**，也可能停在最后一帧
        不动，那时位置读数不再更新，百分比也到不了阈值 —— 判不出「看完」，
        一节 45~60 分钟的课白看。
        """
        if self.cfg.read_screen is None:
            return ""
        try:
            text = self.cfg.read_screen()
        except Exception:  # noqa: BLE001 - 读屏失败不该中断看护
            return ""
        if looks_like_video_end(text):
            hit = next(h for h in END_TEXT_HINTS
                       if h.replace(" ", "") in text.replace(" ", ""))
            return f"结尾文案「{hit}」"
        if looks_like_replay_button(text):
            hit = next(h for h in REPLAY_TEXT_HINTS if h in text)
            return f"出现「{hit}」按钮"
        return ""

    def watch(self) -> ProgressReading | None:
        """阻塞看护，直到进度达标 / 播到末尾 / 卡住超时 / 总时长超限。"""
        started = time.monotonic()
        last_pct = -1.0
        last_change = time.monotonic()
        last_popup_check = 0.0
        latest: ProgressReading | None = None
        prev_reading: ProgressReading | None = None
        flash_seen = 0
        tick = 0        # 轮询次数，用来控制整屏 OCR 的频率

        self.log(f"[watch] 开始看护，目标 {self.cfg.target_percent}%")

        while True:
            # 用户点了「立即停止」就**当场退出** —— 不再读这一轮 OCR，
            # 也不进下面的等待。放在最顶上是有意的：这是唯一能保证
            # 「按钮按下去到真的停手」之间只差一次循环的地方。
            #
            # 返回 latest（而不是 None）：调用方拿到的是一份"没判到达标"的
            # 读数，于是这节会被如实记成「没看完」，不会被误标成已完成。
            if self._stopping():
                self.log("[watch] 收到停止，退出看护")
                return latest

            now = time.monotonic()

            if now - started > self.cfg.max_watch_seconds:
                self.log("[watch] 总时长超限，停止看护")
                return latest

            # 弹窗检测更勤，因为验证弹窗会暂停播放
            if self._popup and now - last_popup_check >= self.cfg.verify_popup_every:
                last_popup_check = now
                try:
                    if self._popup():
                        self.log("[watch] 检测到弹窗并已处理")
                        last_change = time.monotonic()
                except Exception as exc:
                    self.log(f"[watch] 弹窗处理异常（忽略，继续）: {exc}")

            try:
                reading = parse_progress(self._read())
            except Exception as exc:
                self.log(f"[watch] 读取进度失败（忽略本轮）: {exc}")
                reading = None

            if reading is not None:
                tick += 1
                # 贴片广告的 100% 不算数——否则正片会被误标为已学完
                if self._is_flash(reading):
                    flash_seen += 1
                    if flash_seen == 1:
                        self.log("[watch] 等广告放完再看正片进度…")
                    last_change = now     # 别让停滞判定误触发
                    if flash_seen > 60:
                        self.log("[watch] 广告持续过久，放弃本课")
                        return None
                    self._nap(self.cfg.poll_seconds)
                    continue

                # 播完一轮循环回开头 → 确实看完了
                if self._is_wrap(reading, prev_reading):
                    self.log(f"[watch] ✓ 已播完一轮（{prev_reading.raw_text} → "
                             f"{reading.raw_text}，位置回到开头）")
                    # 用**上一轮**的读数当结果：那才是「播到末尾」的现场，
                    # 当前读数已经回到开头了。
                    prev_reading.finished = True
                    return prev_reading

                # 「位置 ≈ 总时长」也直接算看完，不依赖百分比
                if self._near_end(reading):
                    rest = _to_seconds(reading.duration) - _to_seconds(reading.position)
                    self.log(f"[watch] ✓ 已到末尾 {reading.raw_text}"
                             f"（距总时长 {rest:.0f}s）")
                    # 标记 finished，让调用方不必再看百分比阈值 ——
                    # 否则「已到末尾但百分比只有 97.x」会被调用方判成未达标。
                    reading.finished = True
                    return reading

                # 播完的直接证据：结尾文案 / 重新播放按钮。
                #
                # 放在位置判据**之后**：位置判据更便宜（只读小区域），
                # 这里要读整屏 OCR。但两者都要有——视频可能不循环、
                # 也可能停在最后一帧，那时只有这两种证据能判出来。
                if tick % max(1, self.cfg.screen_check_every) == 0:
                    why = self._screen_says_done()
                    if why:
                        self.log(f"[watch] ✓ 画面显示已播完（{why}）")
                        reading.finished = True
                        return reading

                latest = reading
                prev_reading = reading
                if reading.percent >= self.cfg.target_percent:
                    self.log(f"[watch] 已达标 {reading.percent:.1f}% ({reading.raw_text})")
                    return reading

                if abs(reading.percent - last_pct) >= 0.5:
                    last_pct = reading.percent
                    last_change = now
                    self.log(f"[watch] 进度 {reading.percent:.1f}% ({reading.raw_text})")
                elif now - last_change > self.cfg.stall_timeout:
                    # 进度不动：可能是暂停按钮没按到，或播放结束但没上报
                    self.log(
                        f"[watch] 进度停在 {reading.percent:.1f}% 已超 "
                        f"{self.cfg.stall_timeout:.0f}s，判定卡住"
                    )
                    return reading

            self._nap(self.cfg.poll_seconds)


# --------------------------------------------------------------------------
# 视频播完的直接证据
# --------------------------------------------------------------------------

#: 视频结尾常见的致谢文案。**这是播完最直接的证据**——比百分比可靠得多。
#:
#: 实测教训：原先只看「位置≈总时长」和「播完循环回开头」两个判据，
#: 但视频**不一定循环**，也可能停在最后一帧不动（位置读数不再更新）。
#: 那两种情况下就判不出「看完了」，一节 45~60 分钟的课白看。
#:
#: 所以补上这类文本判据。写成元组方便按需扩充——不同课程结尾文案不同。
END_TEXT_HINTS: tuple[str, ...] = (
    "感谢聆听",
    "感谢观看",
    "感谢收看",
    "谢谢聆听",
    "谢谢观看",
    "谢谢收看",
    "感谢您的观看",
    "感谢您的聆听",
    "谢谢大家",
    "再见",
    "THE END",
    "The End",
)


def looks_like_video_end(ocr_text: str) -> bool:
    """画面文本里是否出现了「视频播完」的致谢文案。"""
    if not ocr_text:
        return False
    flat = ocr_text.replace(" ", "").replace("\u3000", "")
    return any(h.replace(" ", "") in flat for h in END_TEXT_HINTS)


#: 「重新播放」按钮的候选文案。播放器播完后，播放/暂停键会变成这个。
REPLAY_TEXT_HINTS: tuple[str, ...] = (
    "重新播放",
    "重新观看",
    "重播",
)


def looks_like_replay_button(ocr_text: str) -> bool:
    """控制条上是否出现了「重新播放」——播放器播完后播放键会变成它。"""
    if not ocr_text:
        return False
    flat = ocr_text.replace(" ", "").replace("\u3000", "")
    return any(h in flat for h in REPLAY_TEXT_HINTS)


# --------------------------------------------------------------------------
# 弹窗关键词
# --------------------------------------------------------------------------

# 这些是「播放中验证」类弹窗的常见文案。真实页面文案需要你截图后
# 我据实调整——猜文案是最容易出错的地方，所以这里只放高置信度的通用词。
POPUP_KEYWORDS = (
    "还在学习",
    "是否继续",
    "继续学习",
    "确认",
    "继续观看",
    "点击继续",
)


def looks_like_popup(ocr_text: str) -> bool:
    """判断一段 OCR 文本是否像验证弹窗。"""
    if not ocr_text:
        return False
    return any(kw in ocr_text for kw in POPUP_KEYWORDS)


# --------------------------------------------------------------------------
# 签到状态
# --------------------------------------------------------------------------

# 「本月连续签到0次，本月累计签到1次」/「连续签到1次，本月累计签到2次」
_CHECKIN_RE = re.compile(
    r"连续签到\s*(\d+)\s*次.*?累计签到\s*(\d+)\s*次", re.S
)

# 签到按钮的两种文案（实测）
CHECKIN_DONE_TEXT = "已经签到"
CHECKIN_TODO_TEXT = "立即签到"


@dataclass
class CheckinStatus:
    """签到状态。"""
    consecutive: int = 0
    total: int = 0
    raw: str = ""
    #: 是否真的从文案里解析出了次数。
    #: 注意别用 raw 非空来判断——页面上任何文字都会让 raw 非空，
    #: 那样「无关文字」会得到「连续0 累计0」这种看似有效的结果。
    matched: bool = False

    def same_as(self, other: "CheckinStatus") -> bool:
        """两次读数是否一致。用于判断点击有没有生效。

        两边都没解析出次数时返回 False——「都没读到」不能当成「没变化」，
        否则会把识别失败误判成「点击无效」。
        """
        if not (self.matched and other.matched):
            return False
        return (self.consecutive == other.consecutive
                and self.total == other.total)


def parse_checkin(text: str) -> CheckinStatus:
    """从签到文案里解析次数。

    实测：签到成功后「连续签到」和「累计签到」都会 +1，
    所以可以用「次数有没有变」来判断点击是否生效——比只看按钮文案稳，
    因为按钮文案在页面切换时会消失。
    """
    if not text:
        return CheckinStatus(raw="")
    flat = text.replace("\u3000", " ").replace(" ", "")
    m = _CHECKIN_RE.search(flat)
    if not m:
        # 没解析出次数。matched=False 让调用方能区分「读到0次」和「没读到」。
        return CheckinStatus(raw=text.strip(), matched=False)
    return CheckinStatus(
        consecutive=int(m.group(1)),
        total=int(m.group(2)),
        raw=text.strip(),
        matched=True,
    )


def checkin_button_state(text: str) -> str:
    """判断签到按钮处于哪个状态。

    返回 "done"（已签到）/ "todo"（可签到）/ "unknown"。
    """
    if not text:
        return "unknown"
    flat = text.replace(" ", "")
    if CHECKIN_DONE_TEXT in flat:
        return "done"
    if CHECKIN_TODO_TEXT in flat:
        return "todo"
    return "unknown"
