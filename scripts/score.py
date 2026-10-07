"""考核成绩读取与「低于阈值自动重做」的判定。

## 实测的结果页成绩区

```
本项目考核
考核类型：期末大作业
题目类型：题库作业
本次成绩：60分          ← 本次
最高成绩：60分          ← 历史最高
```

右上角还有个大号「60分」。

## 重做的前提（平台规则原文）

```
1、客观题考核可以重复提交。
4、学生重复提交考核时，系统会记录重做次数。
```

所以「低于阈值就重做」是平台允许的，但**重做次数会被记录**——
要设个上限，不能无限刷。

## 为什么阈值判定要同时看「本次」和「最高」

平台可能对同一份考核取最高分计入。所以：

* 本次没到阈值，但历史最高已达标 → 不必重做（已经够了）；
* 本次和最高都没到 → 才重做。

这样能避免无谓地增加重做次数。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 默认的「及格/达标」阈值。用户要求低于 80 分重做。
DEFAULT_PASS_SCORE = 80

# 「本次成绩：60分」/「本次成绩:60分」/「本次成绩 60分」
# 注意「分」字**可能没有**——实测考核列表页是「最高成绩： 60」，
# OCR 把「分」字和数字分成了两段。所以分字必须可选。
_THIS_SCORE_RE = re.compile(r"本次成绩\s*[：:]?\s*(\d{1,3})\s*分?")
# 「最高成绩：60分」/「最高成绩： 60」
_BEST_SCORE_RE = re.compile(r"最高成绩\s*[：:]?\s*(\d{1,3})\s*分?")
# 右上角大号分数「60分」——结果页有，考核列表页也可能有
_LOOSE_SCORE_RE = re.compile(r"\b(\d{1,3})\s*分\b")

#: 「未做」等表示还没成绩的文案。出现时要抑制纯数字回退，
#: 否则会把附近的年份/日期数字误读成分数。
_NO_SCORE_HINTS = ("未做", "未参加", "未答题", "暂无成绩", "未开始")


@dataclass
class ScoreInfo:
    """一次成绩读取的结果。"""
    this_time: int | None = None      # 本次成绩
    best: int | None = None           # 最高成绩
    raw: str = ""

    @property
    def parsed(self) -> bool:
        """是否至少读到一个分数。"""
        return self.this_time is not None or self.best is not None

    def effective(self) -> int | None:
        """用于达标判定的分数：取本次与最高里较高的那个。

        平台的「最高成绩」是历史最好成绩；如果历史已经达标，
        再重做只是徒增重做次数，没有意义。
        """
        vals = [v for v in (self.this_time, self.best) if v is not None]
        return max(vals) if vals else None

    def describe(self) -> str:
        t = "?" if self.this_time is None else str(self.this_time)
        b = "?" if self.best is None else str(self.best)
        return f"本次 {t} 分 / 最高 {b} 分"


def parse_score(text: str) -> ScoreInfo:
    """从页面文本里读成绩。

    先找带标签的「本次成绩/最高成绩」，找不到再退回通用的「N分」。
    注意通用回退容易误读（页面上别的地方也可能有「N分」），
    所以只在带标签的都没读到时才用；页面上出现「未做」时干脆不用回退，
    否则会把起止时间里的年份/日期读成分数。
    """
    if not text:
        return ScoreInfo(raw="")

    this_m = _THIS_SCORE_RE.search(text)
    best_m = _BEST_SCORE_RE.search(text)

    this_val = int(this_m.group(1)) if this_m else None
    best_val = int(best_m.group(1)) if best_m else None

    if this_val is None and best_val is None:
        # 明确「未做」时不要瞎猜
        if any(h in text for h in _NO_SCORE_HINTS):
            return ScoreInfo(raw=text.strip())

        loose = _LOOSE_SCORE_RE.search(text)
        if loose:
            val = int(loose.group(1))
            # 分数不可能超过 100，超了说明读到的不是分数（如「55天」被误读）
            if 0 <= val <= 100:
                this_val = val

    return ScoreInfo(this_time=this_val, best=best_val, raw=text.strip())


def should_retake(
    score: ScoreInfo,
    pass_score: int = DEFAULT_PASS_SCORE,
) -> tuple[bool, str]:
    """判断是否需要重做。返回 (是否重做, 原因)。

    无法解析成绩时返回 False —— **宁可不重做，也不要因为读错就乱重做**，
    重做次数是会记录在案的。
    """
    eff = score.effective()
    if eff is None:
        return False, f"读不到成绩（{score.describe()}），不重做"

    if eff >= pass_score:
        if score.best is not None and score.best >= pass_score \
                and (score.this_time is None or score.this_time < pass_score):
            return False, (f"历史最高 {score.best} 分已达标"
                           f"（阈值 {pass_score}），本次虽 {score.this_time} "
                           f"分但不必重做")
        return False, f"{eff} 分已达阈值 {pass_score}，无需重做"

    return True, f"{eff} 分低于阈值 {pass_score}，需要重做"


# --------------------------------------------------------------------------
# 重做入口的按钮文案
# --------------------------------------------------------------------------

#: 考核列表 / 结果页上「进入答题」类按钮的候选文案，按优先级排列。
#: 实测确认的文案：
#:   「开始答题」= 首次未考
#:   「进入答题」= 考核说明页底部
#:   「再做一次」= **已考过、要重做**（这个最容易被漏掉，
#:                 它不是「重做」而是「再做一次」）
ENTER_EXAM_TEXTS = (
    "开始答题",
    "进入答题",
    "再做一次",
    "重做",
    "重新答题",
    "再次答题",
    "继续答题",
    "开始考试",
    "参加考核",
)

#: 「考核列表页」上重做按钮的位置。实测「再做一次」在 (599,152)-(689,185)，
#: 中心约 (644,168)；「开始答题」在 (607,154) 附近，位置基本重合，
#: 所以两者可以共用同一个落点。
ENTER_TAP_TOP = (644, 168)

#: 「结果页/详情页」的标志文案
RESULT_PAGE_HINTS = ("本次成绩", "最高成绩", "正确答案", "您的答案")
#: 「考核列表页」的标志文案
LIST_PAGE_HINTS = ("开始答题", "进入答题", "再做一次", "重做", "重新答题", "未做")


def find_enter_button(text: str) -> str | None:
    """从页面文本里找出进入答题的按钮文案。找不到返回 None。"""
    if not text:
        return None
    for t in ENTER_EXAM_TEXTS:
        if t in text:
            return t
    return None


def looks_like_result_page(text: str) -> bool:
    """是否停在结果页（有逐题答案对照）。"""
    return any(h in (text or "") for h in RESULT_PAGE_HINTS)


def looks_like_exam_list(text: str) -> bool:
    """是否停在考核列表页。"""
    t = text or ""
    return ("本项目考核" in t or "考核详情" in t) and any(
        h in t for h in LIST_PAGE_HINTS
    )
