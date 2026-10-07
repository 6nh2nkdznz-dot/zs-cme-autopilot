"""考核答题的执行逻辑。

## 实测的答题页版式（MaaFramework 720x1280 画布）

```
y   78..125   页面标题「本项目考核」 + 右上角[提交] (694,101)
y  140..175   题型行「一、选择题（单选）」        ← 左上角，ROI [0,125,260,50]
y  180..230   底部信息栏「截止倒计时：55天…」
y  195..250   题干「1、脓毒症患者…（5分）」        ← ROI [0,195,720,55]
y  250        选项 A 行          圆圈在 x≈682，整行可点
y  305        选项 B 行
y  359        选项 C 行
y  413        选项 D 行          行间距约 54px
y 1220..1265  [上一题] (87,1232)  [答题卡] (341,1252)  [下一题] (600,1247)
```

**已实测确认的三件事：**

1. **整行可点**。点 (300,268) 就选中了 A，不必精确点右侧的小圆圈。
   选中后圆圈变蓝色实心带勾，OCR 能读到 `√`。
2. **一题一页**，靠底部「下一题」翻页。
3. **倒计时是 55 天**（不是分钟级），所以不存在超时压力。

## 平台规则（考核说明页原文）

```
1、客观题考核可以重复提交。
2、主观题考核需要老师手动批改，批改前学生可以重复提交，批改后学生无法修改。
3、题库考核若包含问答题，其规则参考主观题规则，若不包含问答题，其规则参考客观题。
4、学生重复提交考核时，系统会记录重做次数。
5、建议上传考核文档不超过100M。
```

→ 客观题可重复提交，所以「先提交看结果」是安全的。
   但 `重做次数` 会被记录，所以不要把提交当儿戏反复刷。

## 设计

答题分两层：

* **识别**：`QuizAnswer`（在 main.py 里）读出题干与选项，决定答案或挂起；
* **动作**：`AnswerQuestion`（本模块）按答案点选项、翻页、必要时交卷。

点击统一走 MaaTouch（`post_click`）。实测 `adb shell input tap`
在本平台的 WebView 上经常无效，签到按钮就是典型例子。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable

# ---- 实测坐标（MaaFramework 720x1280）----

#: 四个选项行的 y 中心。行间距约 54px。
#: 实测：单选题 A-D 占这四行；判断题只有 T/F 两项，占**前两行**。
OPTION_ROWS: tuple[int, ...] = (268, 322, 377, 432)

#: 点选项时的 x。取行内偏左位置，避开右侧的小圆圈——
#: 实测整行都能点，取 300 既能命中又不会误触到别的元素。
ROW_TAP_X = 300

#: 底部导航
PREV_TAP = (87, 1232)
CARD_TAP = (341, 1252)     # 答题卡
NEXT_TAP = (600, 1247)

#: 右上角提交
SUBMIT_TAP = (694, 101)

#: 考核说明页底部那个「进入答题」按钮。
#:
#: 实测（4 张说明页截图 20261007-161049/161743/162755 等）：
#: 蓝色按钮包围盒恒为 x 12..709 y 1232..1270，**中心 (360, 1251)**，
#: 三张截图跨度 0px —— 位置完全固定，所以可以当兜底坐标用。
#:
#: 管线里有一个同样坐标的兜底节点 `点进入按钮(坐标)`，但它**只点一次**。
#: 用户实测反馈「还是卡在进入答题的页面」（m06507），而我在自己这边
#: 复现不出来 —— 说明那**一次**点击有时候是被吃掉/落在未渲染区域上的。
#: 所以 `AppCore._run_step` 又多拿这份坐标做了「点了没反应就再点」的循环。
#: 两处必须是同一个数，`test_page_guard` 里钉住了。
ENTER_BUTTON_TAP = (360, 1251)

#: 考核列表页右上角「开始答题 / 再做一次」按钮中心。
EXAM_LIST_BUTTON_TAP = (645, 169)

#: 进入答题那个按钮可能出现的**所有文案**。
#:
#: 为什么按文案找而不是按坐标点（2026-10-07 日志取证后改的）：
#: 那次卡住的现场是「按坐标点报成功、但屏幕上什么都没发生」，而
#: `maafw.log` 里同一时刻 **MaaTouch 的 touch_down 一次都没被调用** ——
#: 说明坐标点击这条路在某些时刻**整个不生效且不报错**。
#: 所以改成「每一轮重新 OCR 找这几个词、点它所在的框」：
#: 位置随页面走，不再依赖任何写死的数。
ENTER_BUTTON_TEXTS = ("进入答题", "开始答题", "再做一次")

#: 找上面这些词时只认 y ≥ 这个值的文本块。
#:
#: 实测两个页面的按钮位置差很多：说明页在**底部**（text box `[317,1229,85,29]`），
#: 列表页在**右上角**（text box `[590,160,95,32]`）。而顶部标题栏
#: （y<120，含状态栏时间、`本项目考核`/`考核` 标题、右上角购物车）里
#: 会出现同名干扰词，所以从 y=120 往下找。
ENTER_BUTTON_Y_MIN = 120

#: 题型行的 ROI，用来判断页面还在不在答题状态
TYPE_ROI = (0, 125, 260, 50)

#: 题干 ROI
STEM_ROI = (0, 195, 720, 55)

#: 选项区 ROI（含四个行），用于读回选项文字判断题型
OPTIONS_ROI = (0, 245, 720, 215)

#: 判断题的选项字母。实测该平台判断题用 T/F（正确/错误），
#: 不是 A/B —— 这是个很容易漏的坑：只认 A-D 的话判断题会全部失败。
JUDGE_LETTERS = ("T", "F")
#: 判断题字母到行号的映射
JUDGE_ROW = {"T": 0, "F": 1}

#: 提交被拦时的「温馨提示」弹窗
DIALOG_CANCEL_TAP = (461, 739)     # 「取消」
DIALOG_GOTO_TAP = (365, 739)       # 「去做题」

#: 提交确认弹窗（平台在真正交卷前可能再问一次）
DIALOG_CONFIRM_HINTS = ("确定提交", "确认提交", "是否提交", "确认交卷")


# --------------------------------------------------------------------------
# 页面识别层
# --------------------------------------------------------------------------
#
# ## 为什么要有这一层
#
# 实测踩过的坑：交卷成功后页面已跳到结果页，脚本却以为提交失败，继续对着
# 结果页「答题」——把「本次成绩：45分」当题干、把「最高成绩：60分」当选项，
# 而且**不报错**，一路点到超时。
#
# 根因是每个动作各自猜「我在哪一页」，判断散落各处、互相不一致。
# 所以把页面判定收敛成**一层**：所有动作先问「我在哪」，再决定做什么。
#
# ## 两套实现，一套词表
#
# * 管线侧：`assets/resource/pipeline/05_pages.json`
#   用框架原生的 `And` / `Or`（含 **节点名称引用**，v5.7）+ 节点 `inverse`
#   字段组合出 `_页面_答题页` / `_页面_结果页` 等节点。
# * 脚本侧：本模块的 `detect_page()`。
#   驱动脚本（run_full_exam / retake_exam）是 Python，需要一个能直接调的版本。
#
# **两边的标志词必须一致**，否则会出现「管线说在答题页、脚本说在结果页」
# 这种自相矛盾。所以标志词定义在这里，改的时候两边一起改。

#: 结果页独有标志。**优先级最高** —— 结果页的逐题回顾里也会出现
#: 「一、选择题」「上一题」这类文字，不先排除结果页就会误判成答题页。
PAGE_MARKS_RESULT = ("本次成绩", "最高成绩", "您的答案", "正确答案", "答案解析")

#: **逐题答案对照**标志——只有结果页有。
#:
#: 注意别用整个 PAGE_MARKS_RESULT 去做「排除结果页」的判断：
#: 考核列表页**本身就显示「最高成绩：60」**，用「最高成绩」去排除的话
#: 会把考核列表页也排掉（实测踩过这个 bug）。
#: 排除结果页要用这一组**逐题对照**标志，它才是结果页真正独有的。
#: 管线侧 `_原子_无结果页标志` 用的也是这几个词，两边必须一致。
PAGE_MARKS_RESULT_DETAIL = ("您的答案", "正确答案", "答案解析")

#: 答题页独有标志：底部导航三件套。别的页面都不出现。
PAGE_MARKS_ANSWER = ("上一题", "下一题", "答题卡")

#: 考核列表 / 说明页的入口按钮
PAGE_MARKS_EXAM_ENTRY = ("开始答题", "再做一次", "进入答题", "成绩报告")

#: **考核被平台拦住**：视频没学完时点考核会看到这一页。
#:
#: 实测原文：「请先完成课程视频学习，再进行考核！」配「重新加载」。
#: 识别它的意义：程序撞到这一页就该**明确报告并停手**，
#: 而不是把它当考核页继续点 —— 那样只会白折腾。
PAGE_MARKS_EXAM_LOCKED = ("请先完成课程视频学习", "再进行考核")

#: **视频弹题**出现时的页面。
#:
#: 实测两种弹题（学习状态检测题、评分简答题）都带这行标题：
#:     「视频弹题（00:27:00）正确作答后继续视频学习」
#: 把它算作一种页面，是为了让「识别当前在哪一页」能如实回答
#: —— 弹题会**暂停视频**，这时不是普通的课程页。
PAGE_MARKS_QUIZ_POPUP = ("视频弹题",)

#: 课程页的 tab 行（需三者同现才算）
PAGE_MARKS_COURSE = ("简介",)

#: 学习列表页
PAGE_MARKS_LEARNING_LIST = ("我的学习", "去学习")

#: 提交被拦的提示弹窗
PAGE_MARKS_DIALOG = ("立即去做题",)

#: 「每日签到」浮层。
#:
#: 用户实测指出：**这个浮层每天每门课点进去都会弹**，而且它会**盖住底下的
#: 视频弹题**（实测点输入框点到浮层、点提交点到浮层的 X），把后续所有点击
#: 都吃掉。所以它必须被当成一种页面来识别，并且在**任何一步之前**先处理掉。
#:
#: 标志用「每日签到」（浮层标题，实测在 (311,564)）。
PAGE_MARKS_CHECKIN_POPUP = ("每日签到",)

#: 页面类型
PAGE_RESULT = "result"
PAGE_ANSWER = "answer"
PAGE_EXAM_ENTRY = "exam_entry"
PAGE_COURSE = "course"
PAGE_LEARNING_LIST = "learning_list"
PAGE_DIALOG = "dialog"
PAGE_EXAM_LOCKED = "exam_locked"
PAGE_QUIZ_POPUP = "quiz_popup"
PAGE_CHECKIN_POPUP = "checkin_popup"
PAGE_UNKNOWN = "unknown"

#: 页面类型的中文名，用于日志
PAGE_NAMES = {
    PAGE_RESULT: "结果页",
    PAGE_ANSWER: "答题页",
    PAGE_EXAM_ENTRY: "考核列表/说明页",
    PAGE_COURSE: "课程页",
    PAGE_LEARNING_LIST: "学习列表页",
    PAGE_EXAM_LOCKED: "考核被拦截",
    PAGE_QUIZ_POPUP: "视频弹题",
    PAGE_CHECKIN_POPUP: "每日签到浮层",
    PAGE_DIALOG: "提示弹窗",
    PAGE_UNKNOWN: "未知页面",
}

#: 站点域名。**站内页面基本都会显示它**（微信 WebView 顶部那条网址）。
SITE_HOST = "zs-hospital.sh.cn"

#: 任何**已知页面类型**都算「在站内」。
#:
#: 为什么不能只看域名：实测 82 张能判定的站内截图里有 **12 张没有域名** ——
#: 「我的学习」列表页和部分课程页不显示网址栏。
#: 只看域名会把这些正常页面误判成「没进网站」。
_SITE_PAGES = (
    PAGE_RESULT, PAGE_ANSWER, PAGE_EXAM_ENTRY, PAGE_COURSE,
    PAGE_LEARNING_LIST, PAGE_DIALOG, PAGE_EXAM_LOCKED, PAGE_QUIZ_POPUP,
)


def on_site(text: str, rows=None) -> bool:
    """当前画面是否**在继续教育平台里**。

    ## 判定顺序很重要（踩过坑）

    1. **先判是不是微信自己的界面** —— 是就直接返回 False
    2. 再判有没有已知页面类型
    3. 最后才看域名

    ## 为什么第 1 步必须在第 3 步前面

    实测：微信聊天列表里「文件传输助手」那条会话**带网址预览**
    （用户把平台链接发给了自己），预览文本里就含 `zs-hospital.sh.cn`。
    只看「域名有没有出现」会把**微信聊天列表误判成「在平台内」**：

        在微信界面: False     ← 应该 True
        在平台内  : True      ← 应该 False

    所以先否掉微信界面，域名才作数。

    ## rows 是可选的加固

    给了 OCR 位置信息（rows）时，域名还要求出现在**顶部网址栏**区域
    （y < `URL_BAR_Y`）才算 —— 聊天内容里的链接一般在下方。
    没给就只靠第 1 步挡着，够用了。
    """
    if not text:
        # 调用方只给了位置信息时，从 rows 拼出文本 —— 免得因为
        # 「两个参数只传了一个」而漏判（测试里就踩到了）
        if rows:
            text = " ".join(str(r[0]) for r in rows if r)
        if not text:
            return False

    # 1) 微信自己的界面 → 一定不在平台里
    if in_wechat_ui(text):
        return False

    # 2) 认得出平台页面 → 在站内（这些标志词只在站内出现）
    if detect_page(text) in _SITE_PAGES:
        return True

    # 3) 域名
    if SITE_HOST not in text:
        return False
    if rows is None:
        return True
    for row in rows:
        try:
            txt, y = str(row[0]), int(row[2])
        except (IndexError, TypeError, ValueError):
            continue
        if SITE_HOST in txt and y < URL_BAR_Y:
            return True
    return False


#: 网址栏所在的 y 上限。实测 WebView 顶部那条网址在 y ≈ 88。
URL_BAR_Y = 130


#: 微信**自己的界面**特征（底栏的 tab 名）。
#:
#: 用来区分两种「未知页面」：
#:   * 掉到微信界面了 → 需要回到平台
#:   * 平台页面但认不出来 → 重试就行
#:
#: ⚠️ **不要用「微信」当特征词** —— 它太泛（标题栏、聊天气泡里都有），
#: 而且底栏那个 tab 的名字本身 OCR 出来常常就只有「微信」两个字，
#: 跟别处区分不开。实测用「微信+通讯录+发现」三选三会漏判。
#:
#: 改用「通讯录 / 发现 / 我」这三个 —— 它们**只在微信底栏出现**，
#: 平台网页里没有。命中其中两个就算微信界面。
_WECHAT_TABS = ("通讯录", "发现", "我")
_WECHAT_TABS_MIN = 2

#: 微信聊天列表里那条存了平台链接的会话。
#:
#: 实测用户把平台地址发到了「文件传输助手」，所以这是**回到平台的正规入口**：
#: 打开这个会话、点里面那条链接即可 —— 就是正常用户操作，
#: 不伪造任何登录态。
WECHAT_SELF_CHAT = "文件传输助手"


def in_wechat_ui(text: str) -> bool:
    """画面是不是**微信自己的界面**（聊天列表 / 通讯录等），而不是平台网页。

    ## 判据只靠底栏 tab，不看域名

    早先这里加了「有平台域名 → 说明还在网页里」的早退分支，
    但**微信聊天列表里那条带链接预览的会话本身就会显示平台域名**
    （用户把平台链接发给了「文件传输助手」）。于是三个 tab 明明都在，
    却被域名分支否掉：

        命中 tab: ['通讯录', '发现', '我']
        在微信界面: False                    ← 错的

    域名该不该算「在平台内」是 `on_site` 的事，而且那边按**位置**
    （网址栏在顶部 y<130）判。这里只管「是不是微信界面」，职责单一。
    """
    if not text:
        return False
    return sum(1 for m in _WECHAT_TABS if m in text) >= _WECHAT_TABS_MIN


def detect_page(text: str, page: str = "") -> str:
    """判断当前在哪一页。

    参数:
        text: 整屏 OCR 文本
        page: 只判断「是不是这一页」时传目标页；留空则返回判定的页面类型

    返回 PAGE_* 常量（传了 page 时返回该页或 PAGE_UNKNOWN）。

    ## 判定顺序即优先级

    结果页放最前，因为它的标志最独特，而且**误判代价最大**——
    把结果页当成答题页会让脚本去「作答」成绩文字。

    答题页次之，然后才是入口页/课程页这些。
    """
    t = text or ""
    if not t:
        return PAGE_UNKNOWN

    def _is(target: str) -> bool:
        # 每个页面只用**该页独有的**标志词，不做「有 A 且没有 B」的排除。
        #
        # 为什么不做排除：管线侧试过用 And+inverse 表达「有 A 且没有 B」，
        # 但实测本环境（MaaFramework 5.14.2 + Python 绑定）
        # **And 里嵌 inverse 子识别不工作**：
        #     And(两个真 OCR)       -> 命中
        #     inverse 单独用         -> 命中
        #     And(真 OCR + inverse)  -> 失败
        # 所以两侧统一改成「只认独有标志」，天然避开这个限制，也更好推理。
        #
        # 前提是标志词真的独有——挑词时要小心：
        #   * 「最高成绩」不能当结果页标志：考核列表页也有
        #   * 「一、选择题 / 二、判断题」不能当答题页标志：结果页的逐题回顾里也有
        # 见各 PAGE_MARKS_* 的注释。
        if target == PAGE_RESULT:
            return any(m in t for m in PAGE_MARKS_RESULT_DETAIL)
        if target == PAGE_DIALOG:
            return any(m in t for m in PAGE_MARKS_DIALOG)
        if target == PAGE_ANSWER:
            return any(m in t for m in PAGE_MARKS_ANSWER)
        if target == PAGE_EXAM_ENTRY:
            return any(m in t for m in PAGE_MARKS_EXAM_ENTRY)
        if target == PAGE_COURSE:
            return any(m in t for m in PAGE_MARKS_COURSE)
        if target == PAGE_LEARNING_LIST:
            return any(m in t for m in PAGE_MARKS_LEARNING_LIST)
        if target == PAGE_EXAM_LOCKED:
            return any(m in t for m in PAGE_MARKS_EXAM_LOCKED)
        if target == PAGE_QUIZ_POPUP:
            return any(m in t for m in PAGE_MARKS_QUIZ_POPUP)
        if target == PAGE_CHECKIN_POPUP:
            return any(m in t for m in PAGE_MARKS_CHECKIN_POPUP)
        return False

    if page:
        return page if _is(page) else PAGE_UNKNOWN

    # 按优先级依次问。
    #
    # 顺序理由：
    #   * 结果页最前——标志最独特，误判代价最大（会把成绩文字当题目去作答）
    #   * 两个浮层（签到、弹题）排在最前——它们**盖在底下的页面之上**，
    #     不先处理就会把后续所有点击都吃掉。签到浮层甚至能盖住弹题。
    #   * 弹窗在答题页**之前**——它盖在答题页上，而且必须先处理
    #     （不关掉就没法正常答题）。晚了会被答题页抢先匹配到。
    for candidate in (PAGE_QUIZ_POPUP,        # 暂停视频，挡在最上面，先认它
                      PAGE_CHECKIN_POPUP,     # 每天每门课都弹，会盖住弹题
                      PAGE_RESULT, PAGE_DIALOG, PAGE_ANSWER,
                      PAGE_EXAM_LOCKED,       # 必须在 EXAM_ENTRY 前面，否则被抢
                      PAGE_EXAM_ENTRY, PAGE_COURSE, PAGE_LEARNING_LIST):
        if _is(candidate):
            return candidate
    return PAGE_UNKNOWN


def page_name(page: str) -> str:
    """页面类型 → 中文名。"""
    return PAGE_NAMES.get(page, page)


def looks_like_answer_page(text: str) -> bool:
    """这段页面文本像不像「答题页」。

    保留这个名字是因为多处已在用；内部走统一的 detect_page，
    避免这里和别处各写一套判断（那正是之前出问题的原因）。
    """
    return detect_page(text, PAGE_ANSWER) == PAGE_ANSWER


def label_to_row(label: str) -> int | None:
    """把答案字母换算成选项行号。

    A-D → 0-3，T/F → 0-1（判断题）。大小写不敏感。
    返回 None 表示这个字母本站不认。

    注意：OCR 偶尔会把一个选项读成「AB」这种多字符，所以必须先查长度，
    否则 `ord()` 会直接抛 TypeError 把整个答题循环打断。
    """
    lb = (label or "").strip().upper()
    if len(lb) != 1:
        # 多字符（如 "AB" / "ACD"）说明调用方把多个答案拼在一起了，
        # 应该逐个字母调用本函数，而不是整串传进来。
        return None
    if lb in JUDGE_ROW:
        return JUDGE_ROW[lb]
    if "A" <= lb <= "D":
        return ord(lb) - ord("A")
    # 有些平台用「对/错」「正确/错误」当字母，这里一并兼容
    if lb in ("对", "正确", "是", "√"):
        return 0
    if lb in ("错", "错误", "否", "×"):
        return 1
    return None


@dataclass
class ExamConfig:
    """答题参数。"""
    #: 拿不准时是否随便选一个。True = 先跑通流程（可重复提交，风险低）；
    #: False = 挂起等人工（更保守，但整卷会被卡住）。
    guess_when_unsure: bool = True
    #: 每题作答后等待秒数（等选中状态渲染）
    answer_settle: float = 1.2
    #: 翻页后等待秒数
    page_settle: float = 2.0
    #: 单题最多点几次（防止某题点不动时死循环）
    max_click_tries: int = 3
    #: 整卷最多答多少题（兜底，防止翻页判断失效时无限循环）
    max_questions: int = 200
    #: 随机种子。固定种子便于复现同一次「随机」作答。
    seed: int | None = None


@dataclass
class ExamProgress:
    """答题过程记录。"""
    answered: int = 0
    guessed: int = 0            # 没把握、随便选的题数
    suspended: int = 0          # 挂起等人工的题数
    questions: list[str] = field(default_factory=list)   # 题干，用于回看
    stopped_reason: str = ""


class ExamRunner:
    """把「识别 → 点选 → 翻页 → 交卷」串起来。

    依赖注入，便于单测时不碰真机。
    """

    def __init__(
        self,
        controller,
        read_stem: Callable[[], str],
        resolve_answer: Callable[[str], list[str] | None],
        cfg: ExamConfig | None = None,
        log: Callable[[str], None] = print,
        ocr: Callable[[object], str] | None = None,
        is_judge: Callable[[], bool] | None = None,
    ) -> None:
        self.controller = controller
        self._read_stem = read_stem
        self._resolve = resolve_answer
        self.cfg = cfg or ExamConfig()
        self.log = log
        #: 可选：对整张图做 OCR。弹窗检测要用。
        self._ocr = ocr
        #: 可选：判断当前页是不是判断题（选项是 T/F）。不给就按单选处理。
        self._is_judge = is_judge
        self.progress = ExamProgress()
        if self.cfg.seed is not None:
            random.seed(self.cfg.seed)

    # --- 基础动作 ---

    def _labels_fit(self, labels: list[str], judge: bool) -> bool:
        """校验答案字母与当前题型是否匹配。

        判断题只认 T/F，单选只认 A-D。跨题型混用会点空行，
        而点空行不会报错——只会让那道题变成未作答，最后提交被平台拦下。
        """
        if not labels:
            return False
        for lb in labels:
            up = (lb or "").strip().upper()
            if judge:
                if up not in JUDGE_LETTERS:
                    return False
            else:
                if not ("A" <= up <= "D"):
                    return False
        return True

    def _click(self, x: int, y: int, settle: float | None = None) -> None:
        self.controller.post_click(int(x), int(y)).wait()
        time.sleep(settle if settle is not None else self.cfg.answer_settle)

    def pick(self, label: str) -> bool:
        """按下选项字母点对应行。

        实测：整行可点（点行内任意 x 都能选中），不需要精确命中右侧小圆圈。
        判断题的字母是 T/F，映射到前两行。
        """
        row = label_to_row(label)
        if row is None:
            self.log(f"[exam] 选项「{label}」在屏幕上找不到对应那一行，这题跳过")
            return False
        self._click(ROW_TAP_X, OPTION_ROWS[row])
        return True

    def pick_random(self, judge: bool = False) -> str:
        """随便选一个。用于「先跑通流程」的场景。

        judge=True 时只选 T/F —— 判断题没有 A-D，乱选会点空。
        """
        label = random.choice(JUDGE_LETTERS if judge else tuple("ABCD"))
        self.pick(label)
        return label

    def next_page(self) -> None:
        self._click(*NEXT_TAP, settle=self.cfg.page_settle)

    def dismiss_dialog(self) -> bool:
        """关掉提交被拦时的「温馨提示」弹窗。返回是否检测到弹窗。

        实测文案：「单选题 第 2 未选，立即去做题？」+ [去做题] [取消]。
        程序自己答完时一般不会缺题，但识别失败的题会缺，所以要处理。
        """
        text = ""
        job = self.controller.post_screencap().wait()
        if job.succeeded:
            text = self._ocr(job.get()) if self._ocr else ""
        if "未选" not in text and "温馨提示" not in text:
            return False
        self.log("[exam] 平台弹窗说有题没答，先把弹窗关掉")
        self._click(*DIALOG_CANCEL_TAP, settle=1.5)
        return True

    def submit(self) -> None:
        self.log("[exam] 点右上角「提交」交卷")
        self._click(*SUBMIT_TAP, settle=3.0)

    # --- 主循环 ---

    def run(self, stop_after: int = 0) -> ExamProgress:
        """逐题作答。

        stop_after > 0 时答完这么多题就停（调试用），不交卷。
        返回答题记录。
        """
        limit = stop_after or self.cfg.max_questions
        self.log(f"[exam] 开始答题，这一轮最多答 {limit} 题")

        seen: set[str] = set()
        repeats = 0

        for i in range(1, limit + 1):
            stem = (self._read_stem() or "").strip()
            if not stem:
                self.progress.stopped_reason = "读不到题干"
                self.log(f"[exam] 第 {i} 题读不出题目内容，先停下")
                break

            # 同一道题反复出现 = 翻页没生效，别死循环
            if stem in seen:
                repeats += 1
                if repeats >= 3:
                    self.progress.stopped_reason = "翻页未生效（题干重复）"
                    self.log("[exam] 连着两题内容一模一样 → 翻页没成功，先停下")
                    break
            else:
                repeats = 0
                seen.add(stem)

            self.progress.questions.append(stem)
            self.log(f"[exam] ── 第 {i} 题：{stem[:44]}")

            judge = bool(self._is_judge and self._is_judge())
            labels = self._resolve(stem)

            # 试卷是混合题型（实测：1-10 单选，11-20 判断），
            # 所以拿到答案后还要校验字母和当前题型是否匹配——
            # 单选页给 T/F、判断页给 A-D 都会点空。
            if labels and not self._labels_fit(labels, judge):
                self.log(f"[exam] 题库存的答案 {labels} 跟这题的类型"
                         f"（{'判断题' if judge else '单选题'}）对不上，"
                         f"这题不用题库答案")
                labels = None

            if labels:
                for lb in labels:
                    self.pick(lb)
                self.progress.answered += 1
                self.log(f"[exam] 这题题库里有，选 {''.join(labels)}")
            elif self.cfg.guess_when_unsure:
                lb = self.pick_random(judge=judge)
                self.progress.answered += 1
                self.progress.guessed += 1
                self.log(f"[exam] 这题题库里没有，先随便选 {lb}"
                         f"（{'判断题' if judge else '单选'}，客观题可重复提交）")
            else:
                self.progress.suspended += 1
                self.log("[exam] 题库里没有、又不允许瞎猜 → 这题空着，留给你自己答")

            # 到达设定的调试上限就停，不交卷
            if stop_after and i >= stop_after:
                self.progress.stopped_reason = f"达到 stop_after={stop_after}"
                self.log(f"[exam] 已经答了 {stop_after} 题，按你设的调试上限停下（不交卷）")
                return self.progress

            self.next_page()

        if not self.progress.stopped_reason:
            self.progress.stopped_reason = "答完所有可见题目"
        return self.progress
