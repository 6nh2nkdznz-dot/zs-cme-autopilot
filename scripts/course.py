"""课程看护：逐个视频播放 + 自动切下一节。

## 实测的页面结构（MaaFramework 720x1280 画布）

课程页由三块组成：

```
y   0.. 90    标题栏「课程学习」
y  95..455    播放器（点中央唤起控件）
y 455..515    控件条（当前时间 / 进度条 / 总时长）—— 只显示约 3 秒
y 490..520    简介 / 目录 / 更多 三个 tab
y 520..1280   目录列表
```

目录列表的几何（实测，条目间距 102~104px）：

```
每一条目 = 章节标题行 + 视频行
   ①  灰色/蓝色圆点   x ≈ 48
   ②  标题「N作者-课程名.mp4」  x ≈ 55，与时长同一行
   ③  时长 mm:ss      x ≈ 659
```

**关键实测结论：**

* ⚠️ **目录里是有「已完成」标记的** —— 就是文件名左边的**蓝色圆圈**：

  ```
  完整蓝圈  ●  = 这一节已看完
  半蓝圈    ◐  = 看了一部分（没看完）
  空圈      ○  = 没看过
  ```

  这是**平台给的权威判据**，见 `circles.py`（阈值实测标定）。

  > **踩过的坑**：这里原先写的是「目录里没有已完成标记，蓝色圆点只表示
  > 当前播放中」——**假设是错的**。因为信了它，程序只能靠自维护的
  > `course_progress.json` 判「哪节看过」，而那份记录会与实际不符
  > （中途被中断、平台重置进度……），表现就是**去点已经看完的课**。
  > 用户实测纠正后才加上圆圈检测。
* 右下角有一个**蓝色 `+` 悬浮按钮**，会盖住最后一条的时长。
  → 点击要落在标题区（x < 500），并且避开 y > 1200 的区域。
* 点条目行的任意处即可切换课程。

## 状态机

```
枚举目录 ──→ 取第一个未播的 ──→ 点进该课 ──→ 看护到学完
   ↑                                              │
   └────────── 滚动加载下一批 ←── 取下一节 ←────────┘
```

「看护到学完」复用 progress.VideoWatcher，并把弹题处理挂在它的
handle_popup 上——弹题会阻塞播放，不处理进度永远不动。
"""

from __future__ import annotations

import re
import time

import channels
import circles
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# 目录条目的时长文本，如 "45:03" / "1:02:33"
_DURATION_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$")

# 播放器时间区（实测）
PLAYER_TIME_ROI = (30, 452, 62, 26)
PLAYER_DUR_ROI = (520, 450, 160, 30)

# 目录区域：tab 行以下
DIR_TOP_Y = 525
DIR_BOTTOM_Y = 1205          # 再往下会被右下角的 + 按钮干扰

# 条目行间距的容许范围（实测 102~104）
ITEM_GAP_MIN = 85
ITEM_GAP_MAX = 125

# 点击目录条目时的落点 x（避开圆点和右侧时长，也避开右下角悬浮按钮）
ITEM_TAP_X = 200


def _same_title(a: str, b: str) -> bool:
    """两个标题是否是同一节。无视标点与空白。

    OCR 对同一行可能给出不同标点（实测 .MP4 vs ，MP4），
    严格比对会让圆圈状态贴不到条目上。
    """
    import re as _re
    norm = lambda t: _re.sub(r"[\\s.,，。、;；:：!！?？·\\-_—]+", "", t or "").lower()
    return bool(a) and bool(b) and norm(a) == norm(b)


@dataclass
class Lesson:
    """目录里的一个视频条目。"""
    title: str
    duration: str
    tap_y: int                      # 该行在画布上的 y
    tap_x: int = ITEM_TAP_X
    played: bool = False
    #: 平台在目录里给的完成状态（圆圈），见 `circles.py`：
    #: `"done"` 完整蓝圈=已看完 / `"partial"` 半蓝圈 / `"none"` 空圈 /
    #: `""` 没读到。**这是权威判据**，本地记录只是缓存。
    platform_state: str = ""

    @property
    def seconds(self) -> float:
        """时长换算成秒，用于估算看护上限。"""
        parts = [int(p) for p in self.duration.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        return parts[-3] * 3600 + parts[-2] * 60 + parts[-1]

    def short(self, n: int = 30) -> str:
        return self.title if len(self.title) <= n else self.title[:n] + "…"


@dataclass
class CourseConfig:
    """看护参数。"""
    # 每个视频的看护上限，取「视频时长 + 这个余量」
    lesson_slack_seconds: float = 15 * 60
    # 单个视频最长看护时间（防止时长解析错误导致无限等）
    lesson_max_seconds: float = 2 * 3600
    # 整门课的兜底上限
    course_max_seconds: float = 12 * 3600
    # 最多播放几个视频（0 = 不限）
    max_lessons: int = 0
    # 进度轮询间隔
    poll_seconds: float = 20
    # 切换课程后等待播放器起播
    settle_seconds: float = 6
    # 单课目标百分比。
    #
    # **不能设 99 以上**：视频播到末尾会循环回开头，实测最高只观测到 98.8%，
    # 阈值 99.0 就永远判不了达标（60 分钟白看，日志还看不出异常）。
    # 取 97.0 留余量；同时 progress.py 里还有「播到末尾 / 播完一轮」两个兜底判据。
    target_percent: float = 97.0
    # 目录滚动后等待渲染
    scroll_settle: float = 2.0


def parse_duration(text: str) -> str | None:
    """从一段文本里认出时长。返回规范化后的 mm:ss 或 h:mm:ss。"""
    t = (text or "").strip()
    if not _DURATION_RE.match(t):
        return None
    parts = t.split(":")
    if len(parts) == 2:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    return f"{int(parts[0])}:{int(parts[1]):02d}:{int(parts[2]):02d}"


def enumerate_lessons(ocr_rows: list[tuple[str, int, int]]) -> list[Lesson]:
    """从 OCR 结果里挑出视频条目。

    ocr_rows 是 [(文本, x, y), ...]（绝对画布坐标）。
    判定一个条目：有一段文本是纯时长格式，且位于目录区域内。
    标题取同一行、x 更小的那段文本。
    """
    durations: list[tuple[str, int, int]] = []
    for text, x, y in ocr_rows:
        if not (DIR_TOP_Y <= y <= DIR_BOTTOM_Y):
            continue
        d = parse_duration(text)
        if d:
            durations.append((d, x, y))

    if not durations:
        return []

    # 按时长所在行，找同行的标题（y 差 < 12，x 明显更小）
    lessons: list[Lesson] = []
    for dur, dx, dy in durations:
        best: tuple[str, int] | None = None
        for text, x, y in ocr_rows:
            if abs(y - dy) > 12:
                continue
            if x >= dx - 40:
                continue          # 不比时长更靠左，不是标题
            if parse_duration(text):
                continue          # 另一个时长，别当标题
            if len(text.strip()) < 4:
                continue
            if best is None or x < best[1]:
                best = (text.strip(), x)
        title = best[0] if best else f"(未识别标题@{dy})"
        lessons.append(Lesson(title=title, duration=dur, tap_y=dy))

    lessons.sort(key=lambda l: l.tap_y)
    return lessons


def merge_lessons(old: list[Lesson], new: list[Lesson]) -> list[Lesson]:
    """合并两批枚举结果（滚动后会重复看到一部分）。

    以 (标题, 时长) 为身份去重；同时保留已经置上的 played 标记。
    返回按 tap_y 排序的合并结果——注意 tap_y 只对「当前这一屏」有效，
    所以合并后仍以最新一批的 y 为准。
    """
    by_key: dict[tuple[str, str], Lesson] = {}
    for l in old:
        by_key.setdefault((l.title, l.duration), l)
    for l in new:
        key = (l.title, l.duration)
        prev = by_key.get(key)
        if prev is None:
            by_key[key] = l
        else:
            # 位置以新的为准，播放状态保留
            prev.tap_y = l.tap_y
            prev.tap_x = l.tap_x
            by_key[key] = prev
    return sorted(by_key.values(), key=lambda l: l.tap_y)


# --------------------------------------------------------------------------
# 「全部学完」判定
# --------------------------------------------------------------------------

# 目录里的非视频行，不能当成章节：
#   * 章节标题行（如「老年认知症的流行病学与疾病轨迹进展」）—— 特征是没有时长，
#     但「视频插题：N」这种行也挂在目录下，且有编号无时长
#   * 「签到 / 打卡」等平台插入的浮层行
# 判定依赖标题文本，因为它们在 OCR 里就是一段普通文字。
_NON_LESSON_HINTS = ("视频插题", "插题", "签到", "打卡", "笔记", "答疑", "资料")

# 「全部学完」的判据：平台在课程页给出的进度文案。
# 实测尚未拿到完整样本，所以做成多组候选，任一命中即认为学完。
COURSE_DONE_HINTS = (
    "已学完", "已完成", "学习进度100", "进度：100", "已全部学完",
)
# 明确表示还没学完的文案
COURSE_UNFINISHED_HINTS = (
    "未完成", "未学完", "学习进度0", "继续学习",
)


def is_course_complete(ocr_text: str, lessons: list[Lesson] | None = None) -> bool:
    """判断当前课程是否「全部学完」。

    三条互补的证据（按可靠度排序）：

    1. **目录里的蓝色圆圈**（最可靠）：每个视频条目左边都有个圆圈，
       完整蓝圈=已看完、半蓝圈=看了一半、空圈=没看过。判定实现在
       `circles.py`，逐个条目都能查。
    2. **平台文案**：页面上出现「已学完 / 已完成 / 进度100」之类。
    3. **本地记录**（兜底）：本次运行把目录里每个条目都播过且都达标。

    > **踩过的坑**：这个函数的注释一度写着「实测目录里**没有**『已完成』
    > 视觉标记」，那是错的 —— 有，就是蓝色圆圈。因为这个错误假设，当时
    > 只能靠本地 `course_progress.json` 记进度，那个文件一旦和平台真实
    > 状态不一致（中途中断、平台重置进度），就会**去点已经看完的课**。
    > 现在圆圈是权威判据，本地记录只作为辅助。

    注意 `lessons` 传入时必须都是本次运行播过的，否则会误判。
    """
    text = ocr_text or ""

    # 明确说没学完，直接否定，不再看别的证据
    if any(h in text for h in COURSE_UNFINISHED_HINTS):
        # 「继续学习」也可能是列表页的按钮文案，单独出现不足以否定；
        # 但「未完成 / 未学完」是明确的
        if any(h in text for h in ("未完成", "未学完")):
            return False

    if any(h in text for h in COURSE_DONE_HINTS):
        return True

    # 兜底：本地记录显示所有条目都播完且达标
    if lessons:
        real = [l for l in lessons if not is_non_lesson(l.title)]
        if real and all(l.played for l in real):
            return True

    return False


def is_non_lesson(title: str) -> bool:
    """目录里有些浮层项（如「视频插题」）不是章节，别当课程统计。"""
    return any(h in (title or "") for h in _NON_LESSON_HINTS)


def filter_lessons(lessons: list[Lesson]) -> list[Lesson]:
    """剔除目录里的非章节浮层项。"""
    return [l for l in lessons if not is_non_lesson(l.title)]


class CourseRunner:
    """把「看护一个视频」和「切到下一节」串起来。

    依赖注入的设计：controller 与几个回调由调用方给，便于单测时不碰真机。
    """

    def __init__(
        self,
        controller,
        ocr_full: Callable[[], list[tuple[str, int, int]]],
        watch_one: Callable[[Lesson], bool],
        cfg: CourseConfig | None = None,
        log: Callable[[str], None] = print,
        is_done: Callable[[Lesson], bool] | None = None,
    ) -> None:
        self.controller = controller
        self._ocr_full = ocr_full
        self._watch_one = watch_one
        self.cfg = cfg or CourseConfig()
        self.log = log
        # 可选的「这节本地记过已完成」判定。
        #
        # ⚠️ 目录里**有**平台自己的完成标记（蓝色圆圈，见 circles.py），
        # 那才是权威判据；这里注入的本地记录只是缓存，用来省掉一次
        # 「点进去看进度」的开销。两者冲突时以圆圈为准。
        # 所以跨运行跳过已看过的课要靠调用方注入（见 course_progress.py）。
        self._is_done = is_done

    def _already_done(self, lesson: Lesson) -> bool:
        """这一节本地记录说学完了吗？

        ## 为什么必须包一层异常保护

        `is_done` 由调用方注入，实际会去读本地进度文件（`course_progress.json`）。
        文件损坏、被占用、权限不足都会抛异常——而这是看护循环的**热路径**，
        一崩就整门课中断（几小时白跑）。实测：注入一个会抛的 `is_done`，
        `run()` 直接把异常抛穿。

        所以这里吞掉异常并当「未记录」处理：最坏情况是重看一节，
        远好过整门课挂掉。
        """
        if self._is_done is None:
            return False
        try:
            return bool(self._is_done(lesson))
        except Exception as exc:  # noqa: BLE001 - 任何异常都不该中断看课
            self.log(f"[course] 查本地进度出错（当未记录处理）: "
                     f"{type(exc).__name__}: {exc}")
            return False

    # --- 基础动作 ---

    def _click(self, x: int, y: int) -> None:
        self.controller.post_click(int(x), int(y)).wait()
        time.sleep(0.6)

    def _swipe_up(self) -> None:
        """目录向下滚动一屏（手指从下往上划）。"""
        self.controller.post_swipe(360, 1100, 360, 620, 400).wait()
        time.sleep(self.cfg.scroll_settle)

    def _swipe_down(self) -> None:
        """目录回滚一屏。"""
        self.controller.post_swipe(360, 620, 360, 1100, 400).wait()
        time.sleep(self.cfg.scroll_settle)

    def _ensure_app_alive(self) -> bool:
        """确认宿主 App（微信）活着，崩了就重新拉起。

        ## 为什么必须在每节课前查

        整个看护都跑在微信里（平台是微信 WebView 的网页）。微信一崩，
        页面就没了，程序会继续对空白截图、所有识别失败、看护空转 ——
        而一门课要跑几小时，中途闪退概率不低。

        查一次很快（一条 `pidof`），代价远小于一节课白跑。

        恢复后**不需要**重新导航：调用方本来就会在每节课前
        `_ensure_visible` 重新定位条目，页面回来就能接着看。
        已看完的课节由**目录里的蓝色圆圈**跳过（见 `read_circle_states`
        与 `run()` 里的判定），所以代价只有重启那几十秒。
        """
        try:
            import app_recover
        except ImportError:
            return True          # 拿不到这个模块就跳过检查，别影响看课
        try:
            alive = app_recover.app_alive(log=self.log)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[course] 查微信状态出错（忽略）: {exc}")
            return True
        if alive is True:
            return True
        if alive is None:
            return True          # 查不出来就不动，别盲目重启

        self.log("[course] ⚠ 微信不在（可能闪退了），尝试恢复…")
        ok = app_recover.ensure_alive(log=self.log)
        if ok:
            self.log("[course] ✓ 微信已恢复，继续看护")
            time.sleep(self.cfg.scroll_settle)
        else:
            self.log("[course] ✗ 微信未恢复，本门课中止")
        return ok

    # --- 目录枚举 ---

    def _read_circles(self, rows) -> dict[str, str]:
        """给这一屏每个条目读「完成圆圈」的状态。返回 {title: state}。

        ## 为什么这是**权威判据**

        文件名左边的蓝色圆圈是**平台自己给的完成标记**：

            完整蓝圈 ● = 已看完      半蓝圈 ◐ = 看了一半      空圈 ○ = 没看过

        原先代码注释写的是「目录里没有已完成标记」——**那是错的**，
        是用户实测纠正的。因为信了它，程序只能凭自维护的
        `course_progress.json` 判「哪节看过」，而那份记录会与实际不符
        （中途中断、平台重置进度……），表现就是**去点已经看完的课**。

        检测实现在 `circles.py`，阈值是从真实截图标定出来的。
        """
        states: dict[str, str] = {}
        try:
            job = self.controller.post_screencap().wait()
            if not job.succeeded:
                return states
            img = job.get()
        except Exception as exc:  # noqa: BLE001 - 读不到就别判，不能中断枚举
            self.log(f"[course] 读圆圈失败（跳过本轮该项判断）: {exc}")
            return states

        # 通道顺序：抓屏返回的是 **BGR**，喂给 circles 之前**必须**转成 RGB。
        #
        # 实测证据（同一份数组，一次抓屏，见 `scripts/check_circle_now.py`）：
        #   * 原始数组直接当 RGB 存 PNG → 蓝色圆圈显示成**橙色**（红蓝互换）
        #   * 原始数组喂 `circle_shape` → 5 节课**全部**判成「没看过」
        #   * 交换首尾通道后再喂 → 5 节课全部判对，像素 [70,160,250] 是蓝色
        #
        # 这里翻过两次车：先用一个「找最蓝像素」的探测脚本得出「是 RGB」的
        # 错误结论（那次抓屏时画面上叠着视频播放器，采样区落到了别处），
        # 据此把转换删掉，结果判定全错。**不要再靠零散探测判通道顺序**，
        # 统一走 `channels.to_rgb()`。
        img = channels.to_rgb(img)
        for row in rows:
            # row 是 (text, x, y, ...)，y 取标题行的顶部
            text, _x, y = row[0], row[1], row[2]
            if not any("\u4e00" <= c <= "\u9fff" for c in text):
                continue
            if not text.lower().endswith((".mp4", ".MP4".lower())):
                # 只有视频条目才带圆圈
                if ".mp4" not in text.lower():
                    continue
            try:
                states[text] = circles.state_at(img, y)
            except Exception:  # noqa: BLE001
                continue
        return states

    def scan_lessons(self, max_scrolls: int = 6) -> list[Lesson]:
        """滚回顶部后逐屏枚举整个目录。

        为什么要回顶部：目录是虚拟列表，只有渲染出来的条目才能 OCR 到。
        从上往下扫才能保证顺序、也才能不漏条目。
        """
        # 先回到顶部（多划几次确保到位）
        for _ in range(max_scrolls):
            self._swipe_down()
        time.sleep(self.cfg.scroll_settle)

        collected: list[Lesson] = []
        idle = 0
        seen_states: dict[str, str] = {}
        for i in range(max_scrolls):
            rows = self._ocr_full()
            batch = enumerate_lessons(rows)
            # 顺便把这一屏每个条目的完成圆圈读出来
            seen_states.update(self._read_circles(rows))
            before = len(collected)
            collected = merge_lessons(collected, batch)
            added = len(collected) - before
            self.log(f"[course] 第 {i + 1} 屏：识别到 {len(batch)} 条，"
                     f"新增 {added}，累计 {len(collected)}")

            if added == 0:
                idle += 1
                if idle >= 2:
                    break        # 连续两屏没有新条目 → 到底了
            else:
                idle = 0

            self._swipe_up()

        # 把圆圈状态贴到条目上（平台标记优先于本地记录）
        for l in collected:
            st = seen_states.get(l.title)
            if st is None:
                # OCR 标题与圆圈读取的键不总能精确对上，做一次模糊匹配
                for k, v in seen_states.items():
                    if _same_title(k, l.title):
                        st = v
                        break
            if st is not None:
                l.platform_state = st

        done_n = sum(1 for l in collected if l.platform_state == circles.LessonState.DONE)
        part_n = sum(1 for l in collected if l.platform_state == circles.LessonState.PARTIAL)
        self.log(f"[course] 圆圈判定：已看完 {done_n}，看了一半 {part_n}，"
                 f"共 {len(collected)} 条")
        for idx, l in enumerate(collected, 1):
            mark = {"done": "●", "partial": "◐", "none": "○"}.get(
                l.platform_state, "?")
            self.log(f"[course]   {idx:>2}. {mark} {l.short(38)}  {l.duration}")
        return collected

    # --- 播放一节课 ---

    def play_lesson(self, lesson: Lesson, index: int, total: int) -> bool:
        """点进该课并看护到学完。返回是否达标。"""
        self.log(f"[course] === [{index}/{total}] {lesson.short(40)} "
                 f"({lesson.duration}) ===")

        # 点标题区切换课程。避开发烧的圆点和右侧时长，
        # x=200 落在大片空白上，实测可触发切换。
        self._click(lesson.tap_x, lesson.tap_y)
        time.sleep(self.cfg.settle_seconds)

        ok = self._watch_one(lesson)

        if ok:
            # **只在达标时**标记。`played` 的语义是「这一节已经学完」，
            # `all_complete` 靠它判定要不要进考核。
            #
            # 踩过的坑：这里原先是 `lesson.played = True` 无条件赋值，
            # 于是**没达标的条目也被当成学完** → 整门课明明没学完，
            # `all_complete` 却可能为真 → 误进考核。
            # 而且 `is_course_complete` 的兜底判据也用它，会一起错。
            lesson.played = True
            self.log(f"[course] ✓ 学完: {lesson.short(40)}")
        else:
            self.log(f"[course] ✗ 未达标: {lesson.short(40)}（继续下一节）")
        return ok

    # --- 主循环 ---

    def run(self) -> dict:
        """跑完整门课。返回统计信息（含 all_complete 标记）。"""
        started = time.monotonic()
        lessons = filter_lessons(self.scan_lessons())
        if not lessons:
            self.log("[course] 没识别到任何视频条目，中止。"
                     "请确认已在课程页且目录 tab 处于选中状态。")
            return {"total": 0, "done": 0, "failed": 0, "elapsed": 0.0,
                    "all_complete": False}

        self.log(f"[course] 目录共 {len(lessons)} 个视频，开始逐个播放")

        done = failed = pending = 0
        #: 本次运行每节的结果：title → "done" / "failed" / "pending"
        #:
        #: 为什么不能只看 `lesson.played` + `failed`：
        #: 「没滚到」的条目既没达标、也不算 failed，只查那两个字段的话
        #: **一个都没滚到时反而会判成 all_complete=True** ——
        #: 这是我自己在修「考核不触发」时引入的反向漏洞，被测试抓出来了。
        results: dict[str, str] = {}

        for i, lesson in enumerate(lessons, 1):
            if self.cfg.max_lessons and i > self.cfg.max_lessons:
                self.log(f"[course] 已达 max_lessons={self.cfg.max_lessons}，停止")
                break

            # 每节课前确认微信还活着。闪退了就地拉起来 ——
            # 否则后面所有识别都会失败，几个小时的看护白跑。
            if not self._ensure_app_alive():
                break

            elapsed = time.monotonic() - started
            if elapsed > self.cfg.course_max_seconds:
                self.log(f"[course] 已超过整门课上限 "
                         f"{self.cfg.course_max_seconds / 3600:.1f} 小时，停止")
                break

            if lesson.played:
                self.log(f"[course] 跳过已播放过的: {lesson.short(36)}")
                results[lesson.title] = "done"
                continue

            # **平台圆圈优先**：这是权威判据。
            #
            # 用户实测指出：目录里文件名左边的蓝色圆圈就是完成标记
            # （完整=已看完 / 半圈=看了一半 / 空圈=没看过）。
            # 原先代码误以为「目录没有完成标记」，只能靠本地记录判，
            # 于是**去点已经看完的课**。
            #
            # 三条分支必须分清楚，不能只靠「不 continue」来表达：
            #   DONE    → 跳过
            #   PARTIAL → **直接去重看**（必须绕开下面那条本地记录兜底）
            #   NONE/空 → 落到本地记录兜底
            state = lesson.platform_state
            if state == circles.LessonState.DONE:
                self.log(f"[course] 平台标记已看完（完整蓝圈），跳过: "
                         f"{lesson.short(34)}")
                lesson.played = True
                results[lesson.title] = "done"
                done += 1          # 已看完也算完成，否则 all_complete 会漏
                continue

            if state == circles.LessonState.PARTIAL:
                # 半圈 = 没看完，**必须重看**。
                # 注意这里不能落到下面的本地记录兜底 —— 本地记录可能是
                # 过期的「已完成」，会把该重看的课又跳过去（测试抓到过）。
                self.log(f"[course] 平台标记看了一半（半蓝圈），接着看: "
                         f"{lesson.short(34)}")
            elif self._already_done(lesson):
                # 只在**圆圈没读出来**（空字符串）时才用本地记录兜底。
                # 它只是缓存、可能过期；与圆圈冲突时一律以圆圈为准。
                self.log(f"[course] 本地记录已完成，跳过: {lesson.short(36)}")
                lesson.played = True
                results[lesson.title] = "done"
                done += 1
                continue

            # 目录可能已经滚动过，条目不在当前屏上就得先滚回来
            if not self._ensure_visible(lesson):
                # **不算 failed**。这只说明本次没滚到它（虚拟列表 + 滚动时机），
                # 不代表视频没学完。算成 failed 会让 `all_complete` 永远为假、
                # 考核入口永远不触发。但也不算学完——状态未知，留到下一轮。
                self.log(f"[course] 本次没滚到条目，留到下一轮: {lesson.short(36)}")
                pending += 1
                results[lesson.title] = "pending"
                continue

            if self.play_lesson(lesson, i, len(lessons)):
                done += 1
                results[lesson.title] = "done"
            else:
                failed += 1
                results[lesson.title] = "failed"

        # 「全部学完」的判据。三条都要满足：
        #
        #   1. 本次枚举到的条目里**没有 failed**（有没达标的，肯定不算学完）
        #   2. 本次枚举到的条目里**没有 pending**（没滚到 = 状态未知，不能算学完）
        #   3. 本地进度记录里，**这次没枚举到的**条目也都有记录
        #
        # 第 3 条是必需的：目录是虚拟列表，每次枚举到的条数不稳定
        # （实测同一门课时而 9 个、时而 10 个）。只看前两条的话，
        # 「这轮只枚举到 9 个、恰好那 9 个都学完了」会误判成整门课学完，
        # 而漏掉的那一节其实还没看。
        #
        # 反过来，如果本地记录也没覆盖到，就**不判学完**——宁可多跑一轮，
        # 也不要误进考核（平台要求学完才能考，误进会被拦住、白折腾）。
        stuck = failed + pending
        for l in lessons:
            if results.get(l.title) == "done":
                continue
            if self._already_done(l):
                results[l.title] = "done"
                l.played = True

        all_complete = (
            bool(lessons)
            and stuck == 0
            and all(v == "done" for v in results.values())
        )
        if all_complete:
            self.log("[course] 本次枚举到的条目全部达标")
        elif stuck:
            self.log(f"[course] 未达标 {failed} 个、没滚到 {pending} 个 → 不能算学完")

        info = {
            "total": len(lessons),
            "done": done,
            "failed": failed,
            "pending": pending,
            "elapsed": time.monotonic() - started,
            "all_complete": all_complete,
        }
        self.log(f"[course] 全部结束: 学完 {done}，未达标 {failed}，"
                 f"未滚到 {pending}，共 {len(lessons)} 个，"
                 f"耗时 {info['elapsed'] / 60:.1f} 分钟")
        self.log(f"[course] 本门课是否全部学完: {'是' if all_complete else '否'}")
        return info

    def _ensure_visible(self, lesson: Lesson, max_tries: int = 10) -> bool:
        """把目标条目滚进可点击区域，并刷新它的 y 坐标。

        ## 为什么必须双向找

        原实现「没找到就 `_swipe_up()`」——**只会往下滚**。而进入课程页时
        目录停在当前位置（不是顶部），目标若在**上方**就永远找不到。实测：

            [course] 本次没滚到条目，留到下一轮: 1花佩-老年认知症长期照护模式研究进展-花佩.mp4
            [course] 本次没滚到条目，留到下一轮: 2朱琳-老年认知症患者精神行为症状的综合评估.mp4

        这两节因此**永远看不了**，`all_complete` 永远为假 → **考核入口不触发**。

        现在改成「同一方向连续 3 次没命中就掉头」，保证上下都能找到。
        """
        target = (lesson.title, lesson.duration)
        direction = 1              # +1 = 往下翻（swipe_up），-1 = 往上翻
        barren = 0                 # 当前方向连续没命中的次数
        limit = max(1, max_tries) * 4

        for _ in range(limit):
            rows = self._ocr_full()
            cur = enumerate_lessons(rows)
            hit = next((c for c in cur
                        if c.title == target[0] and c.duration == target[1]), None)
            if hit is not None:
                if DIR_TOP_Y <= hit.tap_y <= DIR_BOTTOM_Y:
                    lesson.tap_y = hit.tap_y
                    return True
                # 在屏但位置不合适：往正确方向挪一点再试
                if hit.tap_y > DIR_BOTTOM_Y:
                    self._swipe_up()
                else:
                    self._swipe_down()
                barren = 0
                continue

            if barren >= 3:
                direction = -direction
                barren = 0
            if direction > 0:
                self._swipe_up()
            else:
                self._swipe_down()
            barren += 1

        self.log(f"[course] 来回找过仍没命中: {lesson.short(30)}")
        return False
