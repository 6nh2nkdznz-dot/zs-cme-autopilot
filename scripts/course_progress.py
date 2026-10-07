"""课程学习进度的本地持久化。

## 为什么需要

实测目录里**没有「已完成」标记**（见 `course.is_course_complete` 的说明），
所以「哪些课还没看完」这个信息平台不给——每节课都必须点进去、唤起控件、
读进度条才知道。代价是每节课要花几秒，而且**中途重启会把整门课重看一遍**。

整门课好几小时，一旦程序被中断（或按用户的恢复策略「回主页重来」），
已经看过的几十节课全部重看，不可接受。

所以把「看护达标」的课记在本地，下次直接跳过。

## 存什么

按**课程名**分组，记下该课程里已达标课节的标题与时长：

    {
      "老年认知症患者护理人文关怀": {
        "lessons": {"第1讲 概述|45:12": "2026-10-07 01:23:45"},
        "updated": "2026-10-07 01:23:45"
      }
    }

用「标题 + 时长」当键，而不是下标——目录顺序可能变，下标不可靠。
时长加进去是因为同名课节偶有出现（如「视频插题」），多一个字段更稳。

## 一个必须说清的风险

本地记录**只是旁证**，平台那边才是真的。如果用户在别处重新学、或者
平台重置了进度，本地记录会让我们**跳过一节其实没看的课**。

所以：
* 记录只在**用户自己勾选「跳过本地已完成的课」**时启用（默认开）；
* 记录里的课如果这次实际去看了、发现没达标，**立刻删掉该条**并重看；
* 提供 `--forget` 清空入口（见本模块 `clear`）。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

#: 进度文件在 data 目录下的名字
PROGRESS_NAME = "course_progress.json"


def normalize_course(name: str) -> str:
    """归一化课程名，让不同来源写出的同一个名字能对上。

    ## 为什么需要

    同一个课程名在不同地方写法不同：

    * 列表卡片上是**截断**的：`老年认知症患者护理人文关怀实..`
    * 手工记录/别处可能是完整的：`老年认知症患者护理人文关怀`

    不归一化的话，按名字查记录会 miss——明明记过却当成没记过，
    于是重复看一遍。末尾的 `..` / `…` 是 UI 截断标记，必须去掉。
    """
    s = " ".join((name or "").split())
    s = re.sub(r"[.．。·]+$", "", s)      # 去掉末尾的截断点
    s = re.sub(r"[…]+$", "", s)           # 去掉省略号
    return s.strip()


#: 标题里的标点 / 空白：OCR 对同一行可能给出**不同标点**，比对时要无视。
#:
#: ⚠️ **只用在标题上，不要用在时长上**。时长本来就含冒号（`53:08`），
#: 一并去掉会变成 `5308`，生成出另一套键——实测踩过：
#: 同一节课因此留下三条记录（`.MP4|53:08`、`，MP4|53:08`、`MP4|5308`），
#: `done_count` 虚高、跳过判定也乱了。
_PUNCT_RE = re.compile(r"[\s.,，。、;；:：!！?？·\-—_()（）\[\]【】\"'“”‘’]+")

#: 时长只做空白归一化，保留冒号
_DUR_RE = re.compile(r"\s+")


def lesson_key(title: str, duration: str = "") -> str:
    """课节的身份键：标题 + 时长。

    不用下标——目录顺序会变。加时长是因为偶有同名条目。

    ## 标题做标点归一化，时长不做

    实测同一节课的标题在不同轮次 OCR 出**不同标点**：

        第一次： 4吴琳-认知症的流行病学与疾病轨迹进展.MP4     ← 半角句点
        后来：   4吴琳-认知症的流行病学与疾病轨迹进展，MP4    ← 全角逗号

    严格比对会判成「不是同一节」，于是**已看完的课被重看**。
    所以标题去标点；**时长保留冒号**（否则 `53:08` → `5308`，
    又变成另一个键，等于没修）。
    """
    t = _PUNCT_RE.sub("", title or "")
    d = _DUR_RE.sub("", duration or "")
    return f"{t}|{d}" if d else t


def rebuild_key(key: str) -> str:
    """把一个**存下来的**键重建成规范形式（迁移用）。

    存下来的键长得像 `标题|时长`。历史上有两类脏数据：

    1. 时长里的冒号被误删（`…MP4|5308`，应是 `…MP4|53:08`）
    2. 标题里的标点没去掉（旧数据）

    ## 只重建能确定的

    时长只在**原串含冒号**时才去空白；不含冒号的（如 `5308`）无法可靠还原
    成 `53:08` 还是 `5:30:8`，**不猜**——那种条目直接在迁移里丢弃，
    代价只是重看一节，比起把时长改错（会永久跳过）划算得多。
    """
    if "|" not in key:
        return _PUNCT_RE.sub("", key)
    title, _, dur = key.partition("|")
    new_title = _PUNCT_RE.sub("", title)
    new_dur = _DUR_RE.sub("", dur)
    return f"{new_title}|{new_dur}" if new_dur else new_title


class CourseProgress:
    """按课程记「已看护达标」的课节。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, dict] = {}
        self.load()

    # --- 读写 ---

    def load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self._data = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            # 进度文件坏了不该让整门课跑不动，但要说一声
            print(f"[progress] 进度文件解析失败，本次当作空: {self.path}")
            self._data = {}
            return

        if self._migrate():
            self.save()

    def _migrate(self) -> bool:
        """把历史脏键重建成规范形式。返回是否改动过（改了就要落盘）。

        ## 清理什么

        键的格式换过几轮，实测留下过这些重复记录（同一节课三条）：

            …进展.MP4|53:08      ← 正常
            …进展，MP4|53:08     ← 标点漂移（已被 lesson_key 归一化解决）
            …进展MP4|5308        ← 时长冒号被误删（归一化过头引入的 bug）

        前两种现在会落到同一个键上，合并即可。第三种无法可靠还原
        （`5308` 是 `53:08` 还是 `5:30:8`？），**丢弃**——代价是重看一节，
        比把时长改错（会永久跳过整节）划算。

        丢了才知道发生过，所以打印出来。
        """
        changed = False
        new_data: dict[str, dict] = {}

        for course, entry in self._data.items():
            lessons = (entry or {}).get("lessons") or {}
            new_lessons: dict[str, str] = {}
            dropped = []

            for lk, ts in lessons.items():
                fixed = rebuild_key(lk)
                # 时长段本该含冒号；修复后仍不含说明原串就是坏的 → 丢弃
                if "|" in fixed and ":" not in fixed.split("|", 1)[1]:
                    dropped.append(lk)
                    continue
                # 同一节合并时保留**较新**的时间戳
                if fixed in new_lessons and new_lessons[fixed] >= ts:
                    continue
                new_lessons[fixed] = ts

            if dropped:
                print(f"[progress] 丢弃 {len(dropped)} 条无法还原的记录"
                      f"（时长缺冒号，会重看）")
                for d in dropped:
                    print(f"[progress]   - {d}")
            if new_lessons != lessons:
                changed = True
            new_data[course] = {
                "lessons": new_lessons,
                "updated": (entry or {}).get("updated", ""),
            }

        self._data = new_data
        return changed

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"[progress] 进度写盘失败（不影响本次学习）: {exc}")

    # --- 查询与更新 ---

    def is_done(self, course: str, title: str, duration: str = "") -> bool:
        entry = self._data.get(self._resolve_key(course)) or {}
        return lesson_key(title, duration) in (entry.get("lessons") or {})

    def mark_done(self, course: str, title: str, duration: str = "") -> None:
        ck = self._resolve_key(course)
        entry = self._data.setdefault(ck, {"lessons": {}, "updated": ""})
        entry.setdefault("lessons", {})[lesson_key(title, duration)] = _now()
        entry["updated"] = _now()
        self.save()

    def unmark(self, course: str, title: str, duration: str = "") -> None:
        """撤销一条记录。

        用在「本来以为看完了、实际拉进度发现没达标」的情况——
        这时留着记录会导致下次继续跳过它。
        """
        entry = self._data.get(self._resolve_key(course))
        if not entry:
            return
        if (entry.get("lessons") or {}).pop(lesson_key(title, duration), None):
            entry["updated"] = _now()
            self.save()

    def done_count(self, course: str) -> int:
        entry = self._data.get(self._resolve_key(course)) or {}
        return len(entry.get("lessons") or {})

    def guess_course_name(self) -> str:
        """反查「上一次在看哪门课」——取记录里最新更新且有课节的那一门。

        ## 为什么需要

        **课程页和视频页的顶部都读不到课程名**（实测：只有「课程学习」
        和站点域名）。课程名只在「我的学习」**列表卡片**上出现。

        所以用户已经手动点进某门课、直接按开始时，拿不到课程名 →
        本轮进度一条都不记，下次还得重看。

        这时有一条线索：**上一次记过的进度**——用户接着看的通常就是那门课。
        取 `updated` 最新的那门即可。

        反查不到返回空串，调用方会退化成「记不了进度但照常看课」，
        不影响看视频本身。
        """
        best, best_ts = "", ""
        for course, entry in self._data.items():
            if not ((entry or {}).get("lessons") or {}):
                continue          # 空课程不算
            ts = (entry or {}).get("updated", "") or ""
            if ts >= best_ts:
                best, best_ts = course, ts
        return best

    def clear(self, course: str = "") -> int:
        """清空全部，或只清某一门。返回清掉了几条。"""
        if not course:
            n = sum(len((v.get("lessons") or {})) for v in self._data.values())
            self._data = {}
            self.save()
            return n
        entry = self._data.pop(self._resolve_key(course), None)
        self.save()
        return len((entry or {}).get("lessons") or {})

    # --- 内部 ---

    def _resolve_key(self, course: str) -> str:
        """把课程名解析到已存在的键上（精确 → 前缀）。返回要用的键。

        ## 为什么要宽松匹配

        同一个课程名在不同地方**长度不同**——这不是标点差异，是**真截断**：

            列表卡片（宽度有限）： 老年认知症患者护理人文关怀实..
            手工/别处（完整名）：  老年认知症患者护理人文关怀

        去掉截断点后两个串内容仍不同（差一个「实」字，那是下一个字的一半）。
        精确匹配会 miss → 明明记过却当成没记过 → 课被重看一遍。
        实测踩过：第一节看完了，进度却没被识别。

        匹配顺序：
          1. 完全相等
          2. 一方是另一方的前缀 → 取**最短**的那个键

        ## 为什么取最短而不是最长

        两个键可能互为前缀：

            老年认知症患者护理人文关怀      ← 最短（卡片被截断后显示的就是它）
            老年认知症患者护理人文关怀实    ← 多一个「实」字（下一个字露出一半）

        取最长的话，同一门课的数据会分别落到两个键下，
        `done_count` 各算一份、看起来像记了两节，也容易误判。
        取最短能让数据集中，且命名稳定——不依赖某次 OCR 恰好多露出半个字。
        """
        q = normalize_course(course)
        if not q:
            return "(未知课程)"
        if q in self._data:
            return q
        best = ""
        for k in self._data:
            if k.startswith(q) or q.startswith(k):
                if not best or len(k) < len(best):
                    best = k
        return best or q


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")
