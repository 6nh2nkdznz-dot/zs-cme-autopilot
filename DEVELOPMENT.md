# 开发文档

面向要改代码/管线的人。**使用说明见 [README.md](README.md)。**

---

## 0. 本地 MaaFramework 文档快照

`vendor/MaaFramework/` 下是框架官方文档与 schema 的本地快照，
不用联网就能查字段定义。**写管线前先查这里，比猜快得多。**

```
vendor/MaaFramework/
├── tools/
│   ├── pipeline.schema.json          182 KB ★ 管线字段全集（权威）
│   ├── interface.schema.json          69 KB   ProjectInterface 协议
│   ├── interface_config.schema.json   5 KB
│   └── interface_import.schema.json   39 KB
├── sample/
│   ├── interface.json                 23 KB   官方 PI 示例
│   └── resource/pipeline/*.json               官方管线示例
├── docs/zh_cn/*.md                            中文文档
└── SNAPSHOT.txt                               抓取时间与方式
```

### 怎么取的（以及为什么不用 git clone）

本机 **只有 `api.github.com` 可达**，`github.com` / `codeload.github.com` /
`raw.githubusercontent.com` 全部超时，所以 `git clone` 和下载 tarball 都不行。

改用 **GitHub Contents API**（返回 base64 文件体，本地解码）：
`scripts/fetch_maafw_docs.ps1`

> ⚠️ 两个坑：
> 1. 该 API 未认证时限 **60 次/小时**。抓 60 个文件就会撞限流，
>    得等约 1 小时重置。所以脚本只取 `.md` / `.json` / `.jsonc` / `.txt`。
> 2. **那份 .ps1 必须是纯 ASCII**。Windows PowerShell 5.1 会把无 BOM 的
>    `.ps1` 当 ANSI 读，中文字符串会乱码并导致**语法错误**
>    （实测报「字符串缺少终止符」）。

### 查 schema 的工具

```powershell
python scripts\inspect_schema.py                  # 列出节点级全部字段
python scripts\inspect_schema.py --enums          # 动作/识别类型的合法取值
python scripts\inspect_schema.py --field anchor   # 看某字段完整定义
python scripts\inspect_schema.py --grep 滚动       # 按关键词搜字段说明
python scripts\inspect_schema.py --defs           # 列出 196 个 $defs
```

> schema 是 **JSON Schema draft 2020-12**：节点字段藏在
> `patternProperties["^(?!\\$).*"]` 里（且是个 `$ref`，要顺着解析一层），
> 可复用定义在 `$defs` 下。直接找 `properties` 会一无所获。

---

## 0.1 框架能力清单（从 schema 抄的，共 24 个节点字段 / 22 个动作 / 10 个识别）

### 节点级字段

| 字段 | 作用 |
|---|---|
| `recognition` | 识别算法，默认 `DirectHit` |
| `action` | 执行动作，默认 `DoNothing` |
| `next` / `on_error` | 后继 / 失败后转的节点列表 |
| `timeout` | 识别超时（毫秒），**-1 = 无限等待** |
| `rate_limit` | 识别速率上限，默认 1000 |
| `pre_delay` / `post_delay` | 动作前后延迟，默认 200 |
| `pre_wait_freezes` / `post_wait_freezes` | 等画面静止（**比固定延迟稳**） |
| `repeat` / `repeat_delay` / `repeat_wait_freezes` | 动作重复 |
| `roi` / `roi_offset` | 识别区域 |
| `target` / `target_offset` | 动作落点 |
| **`anchor`** | **锚点名。可设字符串 / 数组 / 对象（设 A 清 B）** |
| `max_hit` | 该节点最多命中几次 |
| `inverse` | 反转识别结果 |
| **`enabled`** | 可禁用节点（调试用） |
| `attach` | 附加 JSON，**用于在节点间传状态** |
| `focus` | 透传焦点数据，可走 log/toast/notification/dialog/modal |
| `is_sub` / `interrupt` | 已废弃，用 `[JumpBack]` 替代 |

### `anchor` + `[Anchor]`：**考核导航已按此重写**

这是本项目**已经用上**的能力，不是设想。`target` 除了填坐标，还能填：

* **节点名** —— 用那个节点识别到的位置
* **`[Anchor]锚点名`** —— 用最后设置该锚点的节点识别到的位置

**为什么必须用**：本平台页面会滚动、会弹微信横幅、播放器展开状态不同，
导致布局位移。实测「更多」tab 出现过 **y≈497 和 y≈942** 两种位置 ——
写死坐标会**静默点空**（不报错，只是失效）。

#### 写法约定（`20_exam.json` 就是这么写的）

```
"定位更多Tab": {
    "recognition": "OCR", "expected": ["更多"],
    "anchor": "moreTab",              ← 记位置
    "action": "DoNothing",            ← 自己不点，否则会重复点
    "next": ["点击更多Tab"]
},
"点击更多Tab": {
    "recognition": "DirectHit",
    "action": "Click",
    "target": "[Anchor]moreTab"       ← 点刚才记下的位置
}
```

**关键点**：设锚点的节点必须 `DoNothing`。因为锚点在「识别命中并执行动作后」
设置，如果它自己也 Click，配上 `[Anchor]` 回跳就会重复点击。

#### 实测验证（2026-10-06）

```
[task] 开始执行: __anchor_probe__set
[task] 完成          ← 锚点链正确点击了「更多」tab，面板展开
```

同时验证了「引用未设置的锚点 → 候选节点被跳过、任务不报错」的语义。

#### 别踩的坑

* 不要给整屏 OCR 节点加 `roi` —— 布局一变就漏。实测「更多」两个位置
  差了 445px，任何固定 `roi` 都盖不住。
* `next` 是「**按顺序识别，只执行第一个命中的**」，所以**顺序即优先级**。
  曾把「进入答题」和「开始答题/再做一次」混在一个节点里，
  结果在说明页就误点了。
* 同一路段（说明页 vs 列表页）要用**不同节点分流**，别塞进一个 `Or`。

### 动作（22 种）

```
DoNothing  Click  LongPress  Swipe  MultiSwipe
TouchDown  TouchMove  TouchUp          ← 可做精细手势
Scroll                                 ← 鼠标滚轮语义（dx/dy，120 倍数）
ClickKey  LongPressKey  KeyDown  KeyUp  InputText
StartApp  StopApp  StopTask
Command  Shell  Screencap
Custom
```

> `Scroll` 是鼠标滚轮语义，安卓侧靠 ADB 触摸不一定适用（未实测）。
> 目前滚动用的是 `controller.post_swipe`。若要在管线里滑，优先试 `Swipe`。

### 识别（10 种）

```
DirectHit  TemplateMatch  FeatureMatch  ColorMatch  OCR
NeuralNetworkClassify  NeuralNetworkDetect
And  Or  Custom
```

### `next` 的两种写法

字符串前缀（简写）与结构化对象等价：

```
"[JumpBack]节点名"          ⇔  {"name": "节点名", "jump_back": true}
"[Anchor]锚点名"            ⇔  {"name": "锚点名", "anchor": true}
```

`NodeAttr` 的字段：`name`（必填）、`jump_back`、`anchor`。

---

## 1. 项目结构

```
maa-elearning/
├── launcher.py                 # 图形启动器（打包入口，含 --selftest）
├── build.spec                  # PyInstaller 配置
├── README.md                   # 使用说明（环境要求 + 操作步骤）
├── DEVELOPMENT.md              # 本文件
│
├── config/
│   └── config.json             # 只读默认配置模板
│
├── assets/
│   ├── interface.json          # ProjectInterface 协议（给通用 UI 用）
│   └── resource/
│       ├── default_pipeline.json
│       ├── model/ocr/          # ppocr_v5 中文模型（det.onnx / rec.onnx / keys.txt）
│       └── pipeline/
│           ├── 00_navigation.json   # 导航 + 播放器 + 整门课轮播（11 节点）
│           └── 10_quiz.json         # 弹题处理（8 节点）
│
├── scripts/
│   ├── paths.py                # 路径抽象（源码 / frozen 双模式）
│   ├── detect.py               # 模拟器与 adb 的自动探测（不硬编码任何路径）
│   ├── controller.py           # 连接封装，调用 detect
│   ├── main.py                 # 引擎：OCR 探针 + 自定义识别/动作注册
│   ├── progress.py             # 进度解析 + 单视频看护循环
│   ├── course.py               # 课程目录解析 + 整门课轮播（自动切下一节）
│   ├── quiz.py                 # 答题：题库 → 联网取证 → 人工确认闭环
│   ├── pending.py              # 待答题 / 题库的 CLI 管理
│   ├── imageio_util.py         # 存图降级链（cv2 → PIL）
│   ├── validate_pipeline.py    # 管线静态校验（改完管线必跑）
│   ├── check_progress.py       # 进度读取自检（常驻进程）
│   ├── test_quiz.py            # 答题引擎自测（35 项）
│   ├── test_course.py          # 目录解析自测（28 项）
│   ├── tap.py                  # 手动交互（click/text/key/swipe/size）
│   ├── snap.py                 # 单张截图
│   └── capture.py              # 按页面名批量截图
│
├── data/                       # 运行时生成：config.json / answer_cache.json
└── debug/                      # 运行时生成：snap / log / pending
```

---

## 2. 已验证的环境事实

这些都实测确认过，不是推断：

```
ADB 地址      127.0.0.1:16384          MuMu 12
Android       15
物理分辨率    1080x1920 @ density 280
MaaFW 画布    720x1280（竖屏）         长边归一到 1280
root          true
arm 桥接      ro.dalvik.vm.native.bridge = libnb.so
              abilist: x86_64,arm64-v8a,x86
浏览器        com.android.chromium（系统 Chromium，无 Chrome/UC/夸克）
微信          8.0.79 arm64（armeabi-v7a 版会闪退）
```

**坐标换算**：`adb screencap` 出的是原生 1080×1920，MaaFramework 画布是
720×1280，比例 **0.6667**。写节点时的 `roi` / `target` 一律用后者。
`check_progress.py` 之类的脚本里直接对 MaaFW 截图操作，用的也是 720×1280。

---

## 3. 页面结构（实测坐标）

全部为 MaaFramework 720×1280 画布坐标。

### 我的学习（课程列表）

```
页面标题「我的学习」     (318, 110)
Tab 全部/未结课/已结课   y = 189，x = 51 / 124 / 248
筛选                     (611, 185)
「去学习」按钮            中心 x = 313，第一行 y = 461（卡片高约 313px）
「结课要求」              (116, 461)
「申请结课」              (407, 461)
「报名信息」              (563, 461)
底部导航                 首页(72,1252) 课程(251,1250) 学习(449,1262) 我的(611,1249)
```

### 课程页（目录）

```
页面标题「课程学习」     (318, 29)
目录 tab                 (341, 494)
视频条目                 左侧圆点 x ≈ 48，标题 x ≈ 55
时长                     右侧 x ≈ 659
「每日签到」弹窗          X 在 (521, 613)，「立即签到」在 (360, 806)
```

### 播放器

```
视频区                   y 约 95..455
唤起控件的点击点          (360, 200)
暂停按钮                 (35, 466)
当前时间                 ROI [30, 452, 62, 26]
总时长                   ROI [565, 452, 65, 26]
进度条                   x 80..357, y 480
```

> ⚠️ 时间条的 y 是 **452**，不是 480。y=480 会扫到下面的
> 「简介/目录/更多」tab 行，OCR 会读出目录文字。这个坑踩过。

### 视频弹题

```
「视频弹题」横幅          ROI [0, 78, 420, 45]
A 选项行                 中心 (340, 300)
提交 / 继续学习           (359, 1249)
```

### 签到

课程页会自动弹「每日签到」弹窗：

```
标题「每日签到」          (311, 564)
说明「本月连续签到N次…」    (227, 686)
橙色按钮                  (321,752)-(399,776)  中心 (360, 764)
关闭 X                    (521, 613)
```

「更多」面板里也有独立入口：**签到图标 (121, 737)**，进到签到页后：

```
标题「签到」              (329, 78)
「连续签到N次，本月累计签到M次」  (225, 168)
按钮「已经签到」           (322, 228)
日历「10月6日 / 星期二 / 2026年」
```

**按钮文案会变**：`立即签到` ↔ `已经签到`。签到成功后：
连续签到 +1、累计签到 +1、日历当天打勾。

### 目录列表（整门课轮播用）

课程页纵向分区：

```
y   0.. 90    标题栏「课程学习」
y  95..455    播放器（点中央唤起控件）
y 455..515    控件条（当前时间 / 进度条 / 总时长）—— 只显示约 3 秒
y 490..520    简介 / 目录 / 更多
y 520..1280   目录列表
```

单条目的结构（实测条目间距 **102~104px**）：

```
① 圆点         x ≈ 48     蓝=当前播放，灰=其它
② 标题         x ≈ 55     「N作者-课程名.mp4」，与时长同一行
③ 时长         x ≈ 659    mm:ss
```

实测样例（一屏 6 条）：

```
53:08  y=626    4吴琳-认知症的流行病学与疾病轨迹进展.MP4
60:40  y=729    5叶尘宇-BPSD的管理和照护者心理调适.mp4
43:48  y=832    6李晶晶-护理人文关怀在老年认知症中的应用进展.mp4
60:24  y=934    7张晓红-精神运动康复在老年认知中的应用.mp4
45:27  y=1037   8高冰馨-老年认知症人文关怀导向下的非药物干预.mp4
55:05  y=1140   9苏伟-建立与认知症患者的有效沟通技巧.mp4
```

**两个必须绕开的坑：**

1. **右下角有蓝色 `+` 悬浮按钮**（约在 x=656, y=1216），会盖住最后一条的时长。
   → 点击落点固定用 `x=200`（标题左侧空白处），并把目录下界设为
   `DIR_BOTTOM_Y=1205`，超出就往上滚。
2. **目录里没有「已完成」标记**。蓝色圆点只表示「当前播放中」。
   → 无法靠标记跳过已学完的，只能顺序播放 + 进程内记 `played`。
   **中断后重跑会从头播**，这是平台信息不足导致的，不是实现取舍。

---

## 4. 管线节点

### 00_navigation.json

| 节点 | 作用 |
|---|---|
| `结束` | 统一终止节点（DirectHit + DoNothing） |
| `进入我的学习` | 入口。先判断是否已在学习页，否则点学习 tab |
| `已在学习页` | OCR「我的学习」 |
| `点击学习Tab` | OCR「学习」→ 点击 |
| `等待课程列表` | 等「去学习」出现 |
| `点第一个去学习` | OCR「去学习」→ 点击 |
| `等待课程页` | 等「目录」；`[JumpBack]关闭签到弹窗` |
| `关闭签到弹窗` | OCR「每日签到」→ 点 X |
| `视频列表已就绪` | OCR `\d{2}:\d{2}` |
| `播放整门课` | Custom 动作 `WatchCourse`（枚举目录 → 逐个播放 → 自动切课） |
| `唤起播放器控件` | 点 (360,200) |
| `唤醒看护视频` | Custom 动作 `WatchVideo`（只看当前一课，不切课） |

### 10_quiz.json

| 节点 | 作用 |
|---|---|
| `检测视频弹题` | OCR 横幅 |
| `选择A选项` | 点 (340,300) |
| `点底部弹题按钮` | Or 匹配「提交」/「继续学习」 |
| `弹题是否已关闭` | `inverse` 检测横幅消失，未消失则 `on_error` 重试 |
| `再点一次底部按钮` | `max_hit: 2` 防死循环 |
| `最终确认弹题关闭` | 二次确认 |
| `弹题卡住` | 存证并停下，不硬猜 |

---

## 5. 自定义模块

在 `main.py` 的 `register_custom_modules()` 里**进程内**注册
（不走 Agent 进程 —— `AgentServer.start_up` 需要 UI 提供 `sock_id`，
自带主程序的场景用不上）。

| 名称 | 类型 | 作用 |
|---|---|---|
| `VideoProgress` | Custom 识别 | OCR 时间区，解析百分比 |
| `QuizAnswer` | Custom 识别 | 识别题目并决定答案（考试用，待完成） |
| `WatchVideo` | Custom 动作 | 看护**当前**视频直到学完，不切课 |
| `WatchCourse` | Custom 动作 | **整门课轮播**：枚举目录 → 逐个播放 → 看完自动点下一节 |
| `LogProgress` | Custom 动作 | 只打日志不点击（调试用） |

### 两处共用的构件

`WatchVideo` 与 `WatchCourse` 都需要「读进度」和「处理弹题」，
所以抽成了 `_make_player_helpers(context, param)`，返回两个闭包：

* `read_progress()` —— 点控件 → 等 0.45s → 截图 → OCR 时间 + 总时长
* `handle_popup()` —— 检测弹题 → 选 A → 提交 →（必要时）继续学习

还返回 `ocr_rows()`（带绝对坐标的整屏 OCR）给目录枚举用。
写两份必然会漂移，所以刻意合并了。

### 整门课轮播的状态机（course.py）

```
scan_lessons()                  滚回顶部，逐屏 OCR 枚举整个目录
   ↓                            用完 merge_lessons() 去重（相邻屏有重叠）
for lesson in lessons:
   _ensure_visible(lesson)      目录会随播放自己滚动，每次点击前重新定位
   play_lesson(lesson)
       _click(200, tap_y)       点标题区切课（避开圆点、右侧时长、右下角 + 按钮）
       settle 6s
       watch_one(lesson)        复用 progress.VideoWatcher，内含弹题处理
```

**关键的容错设计**：

* `_ensure_visible` 每轮重新 OCR 定位。目录会随播放进度变化，
  缓存的 y 坐标会失效——直接拿旧坐标点会点错课。
* `merge_lessons` 以 `(标题, 时长)` 为身份去重，并保留 `played` 标记。
* 单课看护上限按「视频时长 + 15 分钟余量」，避免时长解析错误时无限等。
* 整门课有 12 小时兜底上限。


### WatchVideo 的核心循环

```
周期性地：
  点 (360,200) 唤起控件 → 等 0.45s 淡入 → 截图
  → OCR 时间区 + 总时长区 → parse_progress()
  → 同时检测「视频弹题」→ 有则选 A + 提交（+ 继续学习）
```

**为什么做成代码而不是纯 Pipeline**：控件只显示约 3 秒、单片 45~60 分钟、
弹题会阻塞播放必须就地处理——用 Pipeline 表达极其啰嗦。

### 弹题安全闸

`_looks_like_state_check()` 只在题干出现「请选择A」「学习状态检测」
「如需继续学习」这类提示时才自动作答。不满足则存证到 `debug/pending/`
并停下，**不瞎猜**（答题有及格线，猜错会把课挂掉）。

单元测试用例（4/4 通过）：

```
PASS  ...(此题为学习状态检测，如需继续学习，请选择A)...  → True
PASS  ...2、下列哪项是认知症的核心症状 A 记忆减退...      → False
PASS  ...请选择A                                        → True
PASS  (空)                                              → False
```

---

## 6. 模拟器自动探测（detect.py）


**不能假设安装目录。** 用户的 MuMu 可能装在任意盘符；引擎目录
（`nx_device\12.0` / `15.0`）随平台升级变化；多开时端口从 16384 递增。
所以这一层完全靠探测，配置里默认留空。

### adb.exe 的探测顺序

先按可靠性排序，再**逐个实际验证**（跑得起来 `adb version` 才算数）：

1. 配置里显式指定
2. **与运行中实例 Android 版本精确匹配**的引擎自带 adb  ← 最重要
3. 其它引擎自带的 adb（版本号高的优先）
4. Android SDK（`ANDROID_HOME` / `ANDROID_SDK_ROOT` / `%LOCALAPPDATA%`）
5. `PATH`
6. 注册表卸载项的 `InstallLocation` / `UninstallString` / `DisplayIcon`
7. **正在运行的模拟器进程路径**（最贴近用户实际环境）
8. 各盘符常见目录名的有限深度递归扫描

第 2 条怎么做到的：`MuMuManager info -v all` 会报运行实例的
`android_version`（如 `"15.0"`），而安装根下的 `configs/install_config.json`
里 `engines.*.player` 同时有 `android_version` 和 `install_dir`，
两者一对就能精确定位到**用户正在用的那份 adb**。

### 端点地址的探测顺序

1. 配置里显式指定
2. **`MuMuManager info -v all` 的 `adb_port`** ← 权威，天然支持多开
3. 配置里的 `address_fallbacks`
4. `adb devices` 里像模拟器的条目（`emulator-*` / `127.0.0.1:*`）

每个候选都要**真的连上去**并读回：
`getprop ro.product.model` + `wm size` + `wm density`。
读不到就换下一个。分辨率与预期不符时只警告，因为可能是用户换了模拟器设置。

### 单独跑探测

```powershell
python scripts\detect.py
```

会打印每个候选、来源、验证结果。排查「找不到模拟器」类问题先跑这个。

### 探测踩过的坑

A1. **注册表的 `InstallLocation` 可能为空**（MuMu 12 就没有），
    必须退回解析 `UninstallString` / `DisplayIcon`。

A2. **注册表里的路径含空格且带引号**
    （`"C:\\Program Files\\Netease\\MuMu\\uninstall.exe"`）。
    按空格切分会得到 `C:\\Program` 这种畸形路径。要先剥引号，
    再对结果做**存在性回退**（逐级向上找真实存在的目录）。

A3. **`android_version` 在 `player` 里面**，不在引擎对象的直接子级：
    `engines.mumu15.player.android_version`。读错层级会导致版本匹配
    静默失效——不报错，只是选错 adb。

A4. **同一个模拟器在 `adb devices` 里可能出现两次**
    （`127.0.0.1:16384` 和 `emulator-5554`，型号相同）。
    坐标空间会因此有歧义，脚本一律显式 `-s <地址>`。

### 其它

A5. **固定层级的通配符不靠谱**。`MuMu\*\shell\adb.exe` 只匹配一层，
    实际结构是 `MuMu\nx_device\15.0\shell\adb.exe`（两层）。
    改用**有限深度递归**（`max_depth=4` + 数量上限），
    既不假设层级也不至于在深目录里卡死。

---

## 7. 已踩过的坑（管线 / API / 输入 / 时序 / 打包）

### 7.-1 动作后置校验：`scripts/verify.py`

**这是本项目最该早有的东西，补得最晚。**

反复栽在同一件事上：**动作发出去了就以为成功**，从不用结果验证。

```
点「提交」  → 只确认"点了"，没确认结果页出现
            → 以为失败，再点一次；结果页没有提交按钮，全打空
            → 连点 9 次才发现早就交上了

点「下一题」→ 没确认题干变了
点「选项」  → 没确认圆圈选中了
```

这类失败**不报错**，只是静默做无用功，所以最难查。

`verify.py` 给三件东西：

* `verify(expected, read_state=...)` —— 声明期望状态，实测比对，
  不一致就报「我做了 X，期望 Y，实际 Z」。**允许重试几次**再判，
  因为点击到渲染有延迟，只看一次会把「还没来得及变」误判成「没生效」
  —— 那正是当初连点 9 次的原因。
* `recover_to_home(...)` —— 用户指定的恢复策略：识别出错就回主页重来。
  优先按底部「学习」tab（所有主页面都在，一步到位），
  不行再退 back（次数取决于当前层级，猜不准）。
* `retry_with_recovery(...)` —— 做事 → 校验 → 不通过就恢复现场重做。

自测：`python scripts\test_verify.py`（20 项，全用假回调，不碰真机）。

> ⚠️ **写这个模块时我自己又犯了一次同样的错**：`run_exam_watch.py` 里
> 判定写成 `verify_ok=lambda: True`，等于不管动作结果一律判成功。
> 日志明明写着「进入第一门课失败或超时」，最后却报告「结果: 完成」。
> 所以**判定必须真的从动作传出结果**，不能写死。

### 7.0 页面识别层：先问「我在哪」，再决定做什么

**这是本项目最该有的一个抽象，补得最晚。** 实测踩过的坑：交卷成功后页面已跳到
结果页，脚本却以为提交失败，继续对着结果页「答题」——把「本次成绩：45分」当题干、
把「最高成绩：60分」当选项，而且**不报错**，一路点到超时。

根因是**每个动作各自猜自己在哪一页**，判断散落各处、互相不一致。所以收敛成一层：

* **管线侧**：`assets/resource/pipeline/05_pages.json`
  `_页面探测` 入口按优先级依次问，命中哪页由 LogProgress 打出 `页面=xxx`。
* **脚本侧**：`scripts/exam.py` 的 `detect_page(text, page="")`。
* **两边标志词必须一致**，否则会出现「管线说在答题页、脚本说在结果页」的自相矛盾。

自测：`python scripts\test_page_detect.py`（26 项，夹具都是真机 OCR 输出）。

#### 判定优先级（顺序即优先级）

```
结果页  → 误判代价最大（会把成绩文字当题目去作答）
弹窗    → 盖在答题页上，必须先处理
答题页  → 底部导航（上一题/下一题/答题卡）
考核入口 → 开始答题/再做一次/进入答题/成绩报告
课程页  → 简介
```

#### 挑标志词的两条教训

1. **别用会被滚动带走的内容。** 早先结果页要求「成绩区 + 逐题对照」两个都命中，
   但滚到卷子中后段时「本次成绩/最高成绩」已滚出屏幕，于是判成「未知页面」。
2. **别用别页也有的词。** 「最高成绩」考核列表页也有；「一、选择题/二、判断题」
   结果页的逐题回顾里也有——用它们判定会误伤。

#### 7.0.2 找按钮：按文案重新定位，不认死坐标

「进入答题」卡住那次（用户实测两次，m06507 / m06982）暴露的是同一个模式：
**按钮的位置逐轮在变，而代码盯着一个写死的坐标。**

实测两轮里同一个按钮的位置：

| 页面 | OCR 框 | 中心 |
| --- | --- | --- |
| 考核**说明页** | `[317, 1229, 85, 29] 进入答题` | (359, 1243) |
| 考核**列表页** | `[590, 160, 95, 32] 开始答题` | (637, 176) |

所以收敛成 `scripts/exam.py` 的两个常量 + `scripts/core.py` 的一个循环：

* `ENTER_BUTTON_TEXTS = ("进入答题", "开始答题", "再做一次")` —— 按钮**可能出现的所有文案**；
* `ENTER_BUTTON_Y_MIN = 120` —— 只认 y ≥ 120 的文本块，躲开顶部标题栏
  （标题栏里会出现同名干扰词，比如列表页标题也叫「考核」）；
* `AppCore._find_text_point(tasker, texts, w_max=400, h_max=80, y_min=0)`
  —— 在当前屏 OCR 结果里找这几个词，返回它所在框的中心；
* `AppCore._click_until(expect, texts, y_min, max_points=6, tries=3, gap=2.5)`
  —— **每一轮重新 OCR 定位**，一轮内在按钮框里试
  几何中心 / `0.45` 高 / `0.62` 高三个点（底部那条绿色固定条会压住按钮下沿，
  往上挪 1/4 行高就露出来了）。

**判断「这一下到底有没有到页面上」的办法：比较整屏文本签名。**
没变 → 这次点击根本没到页面上，换点/换通道；
变了但页面还没到 → 给它时间，别乱点（`break` 出内层循环进下一轮）。

写死坐标的兜底节点（`点进入按钮(坐标)` / `点答题入口(坐标)`）**仍然留着**，
但只作为管线里的最后一道，主路径一律走 `[Anchor]`；两者的一致性由
`scripts/test_page_guard.py` 的 `[8b]` 钉住（管线坐标必须等于 `exam` 里的常量）。

### 7.0.1 实测到的两个框架限制（MaaFramework 5.14.2 + Python 绑定）

文档说 `And` / `Or` 的 `all_of` / `any_of` 支持字符串形式的**节点名称引用**
（v5.7 起）。**实测不工作**：

```
And(内联定义)                  -> 命中
And(纯节点引用)                -> 失败
And(内联 + 引用)               -> 失败
```

而且 **`And` 里嵌 `inverse` 子识别也不工作**：

```
And(两个真 OCR)                -> 命中
inverse 单独用                  -> 命中
And(真 OCR + inverse)          -> 失败
```

所以「有 A 且没有 B」这个表达在本环境**用不了**。`05_pages.json` 因此改成
**纯 Or 链 + 精确独有标志词**：不用 And、不用 inverse，每页只认该页独有的词。

`inverse` 单独用是好的，只是不能放进 `And` 里。


### 7.1 自定义识别回调里做 OCR：**必须用 `context.run_recognition_direct()`**

**这是本项目最难查的一个 bug** —— 现象是任务挂住几分钟、无日志、无 pending 记录。

自定义识别回调 `CustomRecognition.analyze(context, argv)` 运行在
**tasker 自己的执行线程**上。所以回调里这样写会**自死锁**：

```python
job = context.tasker.post_recognition(OCR, JOCR(), img)   # 投给同一个 tasker 的队列
job.wait()                                                 # 在队列自己的线程上等它
```

队列正忙着跑当前回调，那个 job 永远轮不到。正确写法：

```python
context.run_recognition_direct(JRecognitionType.OCR, JOCR(), img)   # 同步直调，不走队列
context.run_action_direct(...)                                      # 动作同理
```

实测对照（`scripts\run_deadlock_probe.py`）：

```
tasker.post_recognition 在回调内    → 12s 超时，判定自死锁
context.run_recognition_direct      → 0.02s 返回
```

`_ocr_image()` / `_ocr_rows()` 已按「有 context 走直调、无 context 退回 tasker」
实现，回调外的 CLI 探针仍能正常工作。

> 注意区分：`context.tasker.get_recognition_detail()` 只是**读已存结果**，
> 同步查询、不排队，回调里调用**不会**死锁。死锁只发生在「投队列再等」。

### 7.2 自定义动作用 `argv.reco_detail` 读不到识别结果

两个原因叠在一起：

**① `argv.reco_detail` 是「当前节点自己的」识别结果。**
所以识别和动作必须在**同一个节点**上：

```jsonc
// ✗ 错：动作节点拿到的是它自己 DirectHit 的 detail
"识别": { "recognition": "Custom", "custom_recognition": "QuizAnswer", "next": ["动作"] },
"动作": { "recognition": "DirectHit", "action": "Custom", "custom_action": "AnswerQuestion" }

// ✓ 对：合并成一个节点，reco_detail 就是 QuizAnswer 的结果
"作答": { "recognition": "Custom", "custom_recognition": "QuizAnswer",
          "action": "Custom", "custom_action": "AnswerQuestion" }
```

实测症状：`algorithm=DirectHit hit=True` —— 一看就知道拿错了对象，
因为本该是 `Custom`。

**② `RecognitionDetail` 没有 `detail` 字段**，结果在 `raw_detail` 里，
而且还包了一层：

```python
raw_detail = {
  "all":  [ {"box": [...], "detail": {"labels": ["C"], "qtype": "single"}}, ... ],
  "best": {"box": [...], "detail": {"labels": ["C"]}}
}
```

所以不要写死路径，递归往里找带 `labels` 的那一层（`AnswerQuestion._pick_detail`）。

### 7.3 贴片广告会被当成正片（静默错误）

**这是本项目最危险的一个 bug**，因为它**看起来一切正常**。

实测：点开目录里的 `5叶尘宇-BPSD的管理和照护者心理调适.mp4`（目录写 `60:40`），
播放器先放 **40 秒的贴片广告**，而广告也会走到 100%。于是：

```
[watch] 已达标 100.0% (1:14 / 00:40)      ← 100% 了
[course] 已记入本地进度: 5叶尘宇-...        ← 60 分钟的正片被标成"已学完"
```

后果：这条视频被永久跳过，**而且日志上一点异常都没有**。

修法：把目录里的时长传进 `WatchConfig.expect_duration`，
播放器报的总时长低于「目录时长 × `duration_tolerance`（默认 0.6）」就判为广告。

```python
# progress.py VideoWatcher._is_flash
expect = _to_seconds(self.cfg.expect_duration)
got    = _to_seconds(reading.duration)
if got < expect * self.cfg.duration_tolerance:
    return True     # 是广告，不认这个进度
```

阈值取 0.6 而不是 1.0：平台的「时长」可能含片头片尾，不严格等于播放器报的秒数；
但广告差一个数量级，0.6 足够区分。缺信息时（目录时长或播放器时长读不到）
**不判广告**，避免误伤。

### 7.4 时长 ROI 太窄，把 `1:00:40` 读成 `00:40`

上一条的**真正诱因**。实测同一时刻不同 ROI 的读取结果：

```
dur_roi = [565, 452, 65, 26]   -> '00:40'            ← 开头 "1:" 被切掉了
dur_roi = [520, 450, 160, 30]  -> '1:00:40 4 高清'    ← 完整
```

原来是按 `53:08` 这种 **mm:ss** 量出来的宽度；视频一旦超过一小时，
文本变成 `1:00:40`，左边多出的 `1:` 就落在 ROI 之外。

**教训**：ROI 要按**最长可能的内容**留余量，不能只按当前样本卡到刚好。
这里把宽度从 65 加到 160、左边从 565 挪到 520。

### 7.5 adb 必须带 `-s`，否则静默失败

`adb shell input text` 不带 `-s <serial>` 时，如果本机有多个设备：

```
adb.exe: more than one device/emulator     ← 退出码 1，什么都不输入
```

实测本机同时有 `127.0.0.1:16384`（MuMu）和 `emulator-5554`。

**这个 bug 骗过了一次隔离测试**：那次我在命令行手工写了 `-s`，所以「能用」；
而代码里地址取自 `config['adb']['address']`——自动探测模式下**那一项是空字符串**，
于是 `-s` 根本没拼上。

正确来源是 `controller.info['adb_serial']`：控制器实际连上的地址。

命令拼装抽成纯函数 `build_adb_input_cmd(context, text)` 单独测
（`scripts\test_adb_input.py`，13 项），因为「漏了 -s」靠肉眼看不出来，
而后果是弹题永远填不上、视频永久暂停。


两个原因叠在一起：

**① `argv.reco_detail` 是「当前节点自己的」识别结果。**
所以识别和动作必须在**同一个节点**上：

```jsonc
// ✗ 错：动作节点拿到的是它自己 DirectHit 的 detail
"识别": { "recognition": "Custom", "custom_recognition": "QuizAnswer", "next": ["动作"] },
"动作": { "recognition": "DirectHit", "action": "Custom", "custom_action": "AnswerQuestion" }

// ✓ 对：合并成一个节点，reco_detail 就是 QuizAnswer 的结果
"作答": { "recognition": "Custom", "custom_recognition": "QuizAnswer",
          "action": "Custom", "custom_action": "AnswerQuestion" }
```

**② `RecognitionDetail` 没有 `detail` 字段**，结果在 `raw_detail` 里，
而且还包了一层：

```python
raw_detail = {
  "all":  [ {"box": [...], "detail": {"labels": ["C"], "qtype": "single"}}, ... ],
  "best": {"box": [...], "detail": {"labels": ["C"]}}
}
```

所以不要写死路径，递归往里找带 `labels` 的那一层（`AnswerQuestion._pick_detail`）。


### 管线写法

1. **管线文件里不能放纯注释项**。每个顶层 key 都必须是节点对象，
   `"_note": "..."` 这种字符串值会让**整个文件**解析失败，报
   `value is not object [key=_note]`。注释只能写在节点的 `_comment` 字段里。
   **这个坑我犯了两次**，所以才有了 `validate_pipeline.py`。

2. **`recognition: "Any"` 不存在**。合法值只有
   `DirectHit / TemplateMatch / FeatureMatch / ColorMatch / OCR /
   NeuralNetworkClassify / NeuralNetworkDetect / And / Or / Custom`。
   写错**不报错**，只是节点永远不命中。

3. **OCR 识别不到文字时，节点算「未命中」**，接着 `next` 超时 →
   整个任务以失败结束。要么给 `on_error` 指向 `DirectHit` 终止节点，
   要么在入口节点就挂好 `on_error`。

4. **PowerShell 的 `Set-Content -Encoding utf8` 会写 BOM**，
   带 BOM 的 JSON 框架直接解析失败。校验器用 `utf-8-sig` 容忍。

### API 用法

5. **`tasker.post_recognition()` 取不到详情**。它返回 `TaskJob`，
   而 `MaaTaskerGetRecognitionDetail` 要的是 `reco_id`，直接传 `job_id` 会
   `failed to get_reco_result`。正确路径：

   ```python
   job.wait().get()              # -> TaskDetail
   TaskDetail.node_id_list       # -> [node_id, ...]
   tasker.get_node_detail(nid).recognition
   ```

6. **`post_input_text` 不能输 URL**。含 `/` 的字符串会被
   `adb shell input text` 打断，整串丢失。纯字母数字域名可以。

### 输入通道

7. **`adb shell input tap` 在微信 WebView 上经常无效，MaaTouch 才是可靠的。**
   实测同一个坐标 `(359,1249)`（弹题提交）与 `(360,764)`（签到按钮）：
   adb tap 点不动，MaaTouch 一次命中。管线一律用 MaaTouch
   （`AdbController` 的默认 input 方式）。
   排查输入问题时，**先用 adb tap 做对照实验**，能快速区分
   「坐标错了」和「通道不通」。

   > 我在这里犯过一个判断错误：早期看到「两次截图逐字节相同」就下了
   > 「这个按钮对合成事件完全无响应」的结论，其实当时 adb tap 和
   > MaaTouch 两次都没生效（其中一次被时序问题吃掉）。隔离变量后才定位准。
   > **教训：一次实验里改两个变量，结论就是废的。**

7.1 **但 MaaTouch 也会「报成功、什么都没发生」。** 2026-10-07 17:39 那次
   「进入答题」卡住的日志取证：

   * `点击进入按钮` 事件是 `Node.Action.Succeeded`；
   * 同一个时间窗里 `maafw.log` **一条 `MtouchHelper.cpp][L232][...touch_down]`
     都没有**（最后一条触摸是 17:39:20.773 `[x=974][y=239]`，
     一直到 17:42:09 拆控制器再无记录）；
   * 于是它的 `next` 列表 30 次全部 `Node.Recognition.Failed`，
     任务以 `Node.PipelineNode.Failed` 结束 —— 界面停在原地。

   结论：**框架不报错不等于点到了。** 所以现在的做法是
   `core.AppCore._tap_raw()`（`adb shell input tap`，设备像素）作为
   **第二条通道**，只在「点完屏幕文本签名一点没变」时启用 ——
   两条通道各有各点得动的地方，是交替用，不是替换。

   还读到的两个框架事实：
   * `InputAgent`（`Manager/InputAgent.cpp:16-35`）的方法顺序是
     `MuMuPlayerExtras / AndrowsExtras / Maatouch / MinitouchAndAdbKey /
     AdbShell`，取**第一个初始化成功**的，之后 `units_.clear()`。
   * `AdbShellInput`（`Input/AdbShellInput.cpp:68-84`）的
     `touch_down/move/up` 全是 `LogError << "AdbShellInput not supports"`；
     它的 `click()`（L32）走 `adb shell input tap` 且**会打日志** ——
     日志里一条都没有，所以活动的输入设备确实是 MaaTouch。

7.2 **横屏会让所有坐标静默错位。** 画布固定 720x1280（竖屏），
   框架只在**第一张截图**时算缩放
   （`ControllerAgent.cpp:1252 postproc_screenshot`）。设备中途转横屏后
   它不会重算，于是画布还是竖的、设备已经是 1920x1080：
   实测点「去学习」的 `(312,794)` 在设备上落到了右边的系统键位置，
   **把设备点回了桌面，微信 WebView 会话就此丢掉**（程序无法恢复）。

   所以 `core.AppCore` 有两道门：
   * `run_tasks()` 开跑前 `screen_orient.ensure_portrait()`，
     锁不回竖屏就**直接不跑**；
   * `_click_until()` 每轮检查 `_canvas_portrait()`，横屏就抛
     `OrientationLost`，由 `run_tasks()` 兜住并说清「转回竖屏后重开」。

8. **同一台模拟器可能有两条 adb 连接**（`127.0.0.1:16384` 和
   `emulator-5554`）。坐标空间会因此有歧义，脚本一律显式指定
   `-s 127.0.0.1:16384`。

### 7.3 「微信自己退出了」的真因：微信的原生视频解码器崩了

用户问过两次「微信为什么会自己退出」，而且明确要求「不知道就读日志」。
读了 `adb logcat -b crash` 之后答案是**确定的**，而且**不是我们的问题**：

```text
10-07 12:33:12.468  2207  4534 F libc : Fatal signal 11 (SIGSEGV),
code 2 (SEGV_ACCERR), fault addr 0x73fbeb2d718e
in tid 4534 (MediaCodec_loop), pid 2207 (com.tencent.mm)

backtrace:
  #00 libdl.so (__cfi_slowpath+26)
  #01 /system/lib64/libstagefright.so (android::MediaCodec::setState+1606)
  #02 /system/lib64/libstagefright.so (android::MediaCodec::onMessageReceived+10842)
  #03 libstagefright_foundation.so (android::AHandler::deliverMessage+172)
  ...
```

**2026-10-07 一天记到 17 次**，全部特征完全一致：

| 特征 | 实测值 |
| --- | --- |
| 进程 | 只有 `com.tencent.mm`（别的进程**一次都没崩过**） |
| 信号 | 全是 `SIGSEGV`，`code 2 (SEGV_ACCERR)` |
| 线程 | 全是 `MediaCodec_loop` |
| 崩溃地址 | 全在 `0x73fbeb2d714e` ～ `0x73fbeb2d718e`（**差 64 字节内**） |
| 调用栈 | 全是 `libstagefright.so` → `MediaCodec::setState` |

崩溃时间点：`12:33:12 / 15:46:58 / 15:52:08 / 15:57:20 / 16:02:32 / 16:15:14 /
16:22:27 / 16:29:07 / 16:53:41 / 17:12:31 / 17:20:03 / 17:44:06 / 17:58:42 /
18:12:13 / 18:21:17 / 18:26:27 / 18:37:03`。崩溃地址整天几乎不变
（**同一个 bug 反复触发**），间隔 5～11 分钟 —— 和「一路播视频课」的节奏一致。

**为什么可以断定不是我们关的**：

* 全仓（`scripts/` + pipeline JSON）**没有任何 `force-stop` / 停进程的代码**；
* 唯一能「顶出微信」的是返回键，三处调用（`core._safe_back()`、
  `run_exam_watch.back()`、`retake_exam.back()`）**全都带 `on_site` 守卫**；
* 框架日志（`maafw.log`）里**根本没有 Android 侧 activity/进程的记录**
  （搜 `com.tencent` / `lawnchair` / `chromium` 命中 0 行）——
  **查「App 为什么没了」必须用 `adb logcat`，只看框架日志永远查不出来。**

**为什么会崩**：我们让微信 WebView 里的视频课一路播下去，平台那种视频播放
最容易踩到**模拟器 x86_64 媒体栈**的坑（`libstagefright.so` 是 Android 系统库，
不是微信自带的）。这是「微信 + 模拟器解码器」的问题，**跟点击坐标、
返回键、输入通道都无关**。

**所以处理办法是**：崩了就重新拉起、从没看完的那节接着看
（`app_recover.ensure_alive()`；已看完的课节由 `course_progress.json` 跳过，
代价是几十秒而不是整门课重看）。

**读日志的做法**：`app_recover.crashes()` 解析 `adb logcat -d -b crash`，
用 `_CRASH_RE` 抓 `F libc : Fatal signal … in tid N (thread), pid N (pkg)`
这一行；`app_recover.explain_crash()` 在微信不在时自动打出来
（`ensure_alive()` 里调用）。**查不到就明确说「查不到」，绝不猜** ——
以前没日志就是靠猜，白折腾了好几轮。

#### 7.3.1 知道原因之后：让程序自己扛住（`app_recover.py`）

真因查明之后有两条路：换环境（真机 / 改模拟器渲染后端），或者**让程序自己扛**。
模拟器侧已经查过，能改的很少（`renderer_mode` 只有 `vk` / `dx` 两个合法值，
`graphics_card` / `video_decode` / `codec` 全是 `key not readable`；
Android 侧 `debug.stagefright.*` 需要 root，而 `which su` 是空的）——
**所以程序侧必须扛住**，这部分是这轮做的。

三个层次，缺一不可：

| 位置 | 做什么 | 为什么必须在这里 |
| --- | --- | --- |
| `AppCore._run_node()`（`core.py`） | 把 `job.wait()` 换成 `job.done` 轮询，每 30s 一次 `crash_watchdog()`，每 5 分钟一行心跳 | 「播放整门课」**一个节点跑几小时**，`job.wait()` 阻塞期间**什么都查不了** |
| `run_exam_watch.run_node()` | 同上（它原来 `timeout=0` 那一支直接 `job.wait()`） | 看课主路径走的是这个函数，不是 `core` 那个 |
| `run_exam_watch.go_home()` / `AppCore._ensure_app()` | 恢复前先 `crash_recovery()` | 微信一没，`recover_to_home` 会一路「tab 没到、back 也没用」，最后报一个**和真因毫无关系的错** |

`crash_recovery()` 的语义是**「先取证、再恢复」，顺序不能反**：

1. `explain_crash()` —— 先把 logcat 里的崩溃记录读出来写进日志，
   并把「本轮已经遇到 N 次崩溃」记进进程内计数 `_CRASH_ROUNDS`
   （崩溃一次不可怕，**反复崩**才说明这台模拟器根本播不动，
   上层拿这个数判断要不要放弃并告诉用户换环境）；
2. 再按状态恢复：`app_alive()` 返回 `False` → `ensure_alive()` 拉起；
   返回 `None`（adb 不通）→ **什么都不做**（盲目重启比不动更危险）；
   活着但 `stuck_empty()` → `ensure_usable()` 重开。

`crash_watchdog(baseline=…)` 是看护用的单次体检，返回**新的**条数：

* 有新增 → 报「新增 N 次崩溃 + 最后一次的时间/信号/线程」，返回新值；
* 没新增 → 查进程在不在、是不是卡空白页，有问题才说话；
* **`baseline` 只涨不跌** —— logcat 缓冲区被冲掉时条数会**变少**，
  这时把 baseline 调小就会把「缓冲区冲掉了」误判成「又崩了一次」。

**开跑前先记环境**（`describe_environment()`，`run_tasks()` 里调）：
一行 `[env] 运行环境：abi=… sdk=… board=…`，`abi == "x86_64"` 时额外提醒
「这是 x86_64 模拟器，解码器不稳，真机不会有这个问题」。
理由很实际：查这次崩溃时**一半时间花在反推环境上**，而这些信息一条都不在日志里。

**测试**（`scripts/test_app_recover.py`，44 项）：`[7]` 用假的
`explain_crash` / `app_alive` / `ensure_usable` / `ensure_alive` 记录调用顺序，
钉住「先 explain → 再探活 → 最后才恢复」；`[8]` 用假的 `crashes()` 钉住
watchdog 的五种情形（无新增零输出、新增要报时间+线程、**缓冲区被冲掉不误报**、
进程没了要说、卡空白页要说）。**正常路径必须一行都不输出** ——
日志里留下的应该只有「发生了什么」。

> 踩过的坑：`core._run_node` 改成轮询之后，`test_page_guard.py` 里那个假的
> `_Job` 只有 `wait()` / `succeeded`，没有 `done`，于是整个测试脚本
> 直接 `AttributeError` 崩掉、输出里连「N 通过 / M 失败」都没有——
> 最容易被误读成「全挂」。**假对象必须把真对象用到的属性都补上**，
> 假 `TaskJob` 要 `succeeded` / `done` / `wait()` 三样。

### 7.4 平台有两套前端；用「桌面版」可以完全不开微信

用户在模拟器里跑微信有被封号的风险，问「有没有别的办法」。为此把平台的
架构摸清了（2026-10-07，全部实测）。

**两套前端，靠 UA 分流：**

| | 地址 | 谁在用 |
|---|---|---|
| 手机版 | `https://elearning.zs-hospital.sh.cn/mobile/#/home/homePage` | 微信 WebView（原来跑的就是它）；**非微信浏览器一律拒绝** |
| 桌面版 | `https://elearning.zs-hospital.sh.cn/` | 普通浏览器；导航栏在**顶部**（首页 / 选课中心 / 学前必读 / 课程表 / 我的学习 / 更多），右上角「登录 / 注册」，没有底部 tab |

**手机版对非微信 UA 是硬拒**。在模拟器自带的 Chromium（`com.android.chromium`，
110.0.5481.154）里以 Android UA 打开 `/mobile/`，页面 `innerText` 只有一句：

```
手机端仅支持微信访问，请页面截图保存至相册，用微信扫码识别后进行登录
```

被拦之后**渲染线程还会被占死**：用 CDP 的 `Runtime.evaluate` 问 DOM，第一次能问到，
之后连续三次全部超时，重启 Chromium 后依然只能问一次。

**但桌面版在同一个浏览器里能完整打开** —— 条件只有一个，而且很容易踩错：

> **`Emulation.setUserAgentOverride` 必须在第一次 `Page.navigate` 之前设好。**
> 顺序反了（页面已经按手机 UA 初始化完，再改 UA）SPA 会直接卡死，
> `Runtime.evaluate` 一律超时 —— 我第一版就栽在这里，得出了「改 UA 也绕不过」
> 的错误结论。

正确顺序（实测一次就进）：

```python
page = pick_page("about:blank")            # 先停在空白页
ws.call("Emulation.setUserAgentOverride", userAgent=UA_DESKTOP, platform="Win32",
        acceptLanguage="zh-CN,zh;q=0.9")
ws.call("Emulation.setDeviceMetricsOverride", width=720, height=1280,
        deviceScaleFactor=1, mobile=False)
ws.call("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=1)
ws.call("Page.enable")
ws.call("Page.navigate", url="https://elearning.zs-hospital.sh.cn/")   # 【最后】才导航
```

结果：`wechatOnly: False`，`innerText` = `首页 选课中心 学前必读 课程表（继教项目）
我的学习 更多 登录 注册 远程继教项目推荐 …`（4 列网格卡片）。

**把视口钉成 720x1280 是关键一步**：框架画布就是 720x1280，视口与之对齐之后，
OCR 读到的坐标即设备上可直接点击的坐标，**不用再 ×1.5 换算**（`_raw_scale()`）。
真实手机上开「桌面模式」时视口是浏览器窗口的 CSS 尺寸，两者不一定相等 ——
所以程序里必须显式设视口，不能指望默认值。

> **关于视口，有两个坑要记住**（都实测过）：
>
> 1. `Emulation.setDeviceMetricsOverride` 的 `width/height` 在 Android Chrome 上
>    **不等于 CSS 视口**。三种写法都试过（`mobile=False` + `screenWidth/Height`、
>    `mobile=True`、干脆不设），`innerWidth` 全是 **980**（不设时 `dpr` 是 1.75、
>    `visualViewport.width` 是 720），页面是个 `width=device-width` 的 SPA，
>    Chrome 自己算出了 980x1742 这个视口。**别指望靠它拿到 720 CSS 像素。**
> 2. **但其实不影响点击**：`width=device-width` 的页面渲染宽度 == 屏幕宽度
>    （1080 设备像素），而框架画布 720 == 1080/1.5，所以**画布坐标和屏幕像素
>    严格 1:1** —— 点画布 (360, y) 就是点屏幕正中，与 CSS 视口是多少无关。
>    真正要保证的只有两件事：页面别被缩放（桌面版有 `width=device-width`，没问题）、
>    以及**框架画布宽高比要和屏幕一致**（横屏时会变成 1280x720，那条已由
>    `ensure_portrait()` 拦住）。

**播放器是保利威（Polyv）**：`/mobile/` 的 1181 字节 shell 里有

```html
<script type=text/javascript src=/static/app/libs/geetest/gt.js></script>
<script src=//player.polyv.net/script/player.js></script>
```

桌面版首页 55470 字节里 `polyv` / `player.js` / `video` / `m3u8` / `hls` 全是 0 次
（播放器是登录后进课程页才现加载）。登录：`/learning/login` 是
**验证码登录（手机号 + 短信验证码）/ 密码登录**，layui + 极验
（`/static/js/sdk/geetest/gt.js`）；`/login` 老页面是「账号 + 密码 + 图形验证码
（`/index/authImg/login`）」，那里的"微信登录"在源码里是**注释掉的**。
→ **平台本来就不需要微信**，手机上"点开就登录"只是微信 WebView 的会话。

**另一条备选路线（也已跑通，不用微信）**：`Win32Controller` 驱动 Windows 上的浏览器。

```python
from maa.controller import Win32Controller
from maa.toolkit import Toolkit
w = [x for x in Toolkit.find_desktop_windows()
     if "Chrome_WidgetWin_1" in x.class_name and "医院远程" in x.window_name][0]
ctrl = Win32Controller(w.hwnd)        # 默认 Background 截图 + Seize 输入，独占不抢前台
ctrl.post_connection().wait().succeeded     # → True
ctrl.post_screencap().wait().get()          # → (1040, 720, 3)
ctrl.post_click(447, 111).wait().succeeded  # → True，页面真的换了
```

注意：**别用 `--user-agent` 命令行参数去开 Android Chromium**，它收不到
（内部转成 `IntentDispatcher`）；要改 UA 只能走 CDP 的
`Emulation.setUserAgentOverride`。

查设备侧真相的工具：`debug/cdp.py`（`--all` 列标签页、默认打印当前页 DOM、
`--nav`/`--eval`），配合 `adb forward tcp:9222 localabstract:chrome_devtools_remote`，
前提是 Chromium 用 `am start ... --es com.android.chrome.REMOTE_DEBUGGING_PORT 9222` 启动。
**别用 `python -m websocket`**：这台机器上没装 `websocket-client`（pip list 里只有
`MaaFw / MaaAgentBinary / numpy / pillow / requests`），所以才手写了 `cdp.py`。

### 7.6 「手机浏览器登录（桌面版）」这个功能

上面 7.4 的两条路（浏览器桌面版 / 真机微信）都验证过之后，用户要求把
**「改浏览器 UA → 跳验证码登录页」做成程序里的一个按钮**。现在左栏第三个按钮
就是它，背后的模块是 `scripts/browser.py`。

**它做的事（顺序不能动）：**

```
1. adb forward tcp:9222 localabstract:chrome_devtools_remote
2. am force-stop <浏览器>  →  am start ... --es com.android.chrome.REMOTE_DEBUGGING_PORT 9222
3. 轮询 http://127.0.0.1:9222/json/version 直到通
4. CDP 连到 about:blank 标签页
5. Emulation.setUserAgentOverride(桌面 UA) + setDeviceMetricsOverride + setTouchEmulationEnabled
6. Page.navigate(https://elearning.zs-hospital.sh.cn/learning/login)   ← 必须在 5 之后
7. （config 里有 browser.phone 时）JS 填手机号 + 点「获取验证码」
```

**第 5 步和第 6 步的先后是唯一的坑**：反了 SPA 会卡死，`Runtime.evaluate` 一律超时，
看起来像「平台不给进」。详见 7.4。

**为什么填表用 JS 而不是"点输入框 + 发按键"**：这个项目在微信 WebView 里反复
踩过「合成点击进不到网页输入框」；JS 走的是页面自己的事件流，稳定得多。
写值必须**走原型上的 setter 再派发 `input`/`change`** —— 直接 `el.value = x`
在很多框架里不会更新 `v-model`：

```javascript
const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
if (desc && desc.set) desc.set.call(el, v); else el.value = v;
el.dispatchEvent(new Event('input',  {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
```

**点按钮必须按文案点名**（`fill(..., want="立即登录")`）。第一版写成「点第一个
像提交的」，实测点到了「获取验证码」—— 页面什么都没发生、还没报错。
现在 `fill()` 会顺手把页面上扫到的按钮一起报回来，日志里能直接看出该点哪个：

```
[browser] ✓ 已填入手机号
[browser] ✓ 已点击「立即登录」
fill -> {'phone': True, 'clicked': '立即登录',
         'buttons': [{'t': '验证码登录'}, {'t': '密码登录'},
                     {'t': '获取验证码'}, {'t': '立即登录'}]}
```

**登录页实测有两种**（切 tab 的按钮就在页面上）：

| tab | 字段 | 说明 |
|---|---|---|
| 验证码登录（默认） | `输入手机号` / `输入验证码` | 手机收短信，字段上只有「获取验证码」和「立即登录」 |
| 密码登录 | `输入账号、手机号` / `输入密码`(type=password) | 旁边还有「注册」「忘记密码」—— **没有账号可以自己注册** |

短信码只能人工填（程序读不到用户的短信，也不该读）。但手机号可以自动填好、
验证码可以自动发出去，用户只差输 6 位数字。

**真机的额外前提**：USB 调试打开 + 数据线连着电脑。没连 adb 时 CDP 够不着，
只能靠用户在浏览器菜单里手动勾「桌面版网站」。

**代码里几个容易踩的点：**

- `_adb()` **必须缓存**：`detect.find_adb()` 每次都会打
  `[detect] 运行中实例的 Android 版本: …` / `[detect] adb 可用: …`，
  而一条命令就可能触发好几次探测（实测一次 `installed_browser()` 打了 4 行）。
  现在直接调 `detect.find_adb(log=_quiet)` 把噪音吞掉。
- `pick_page()` 在 URL 匹配不上时**退回第一个标签页**而不是返回 `None`：
  调用方常在「刚启动浏览器、只有一个 `about:blank`」的时刻连过来，
  这时按 URL 挑必然挑空（实测 `RuntimeError: 没有可用的标签页`）。
- 浏览器活动名**不能写死**：模拟器上是
  `com.android.chromium/com.google.android.apps.chrome.Main`，真机上各家不同。
  用 `cmd package resolve-activity --brief -n <pkg>/` 问系统。
- 设备上有没有浏览器也**不能靠猜包名**：先 `pm list packages` 看装了哪些，
  再用 `cmd package resolve-activity --brief -a android.intent.action.VIEW`
  确认它真能处理网址。

**这个功能没有替代「微信路线」**：桌面版的页面版式不同（顶部导航、4 列网格、
没有底部 tab），现有管线（`assets/resource/pipeline/*.json`）全是按手机版量的。
看课要真正搬到桌面版，导航层还得重写一遍 —— 见第 10 节「待完成」。

### 时序

9. **播放器控件只显示约 3 秒**。`main.py --ocr` 每次都要重载 ppOCR 模型
   （约 2 秒），等它截图时控件已经隐藏，读数必然为空。
   读数必须在**常驻进程内**完成「点击 → 0.45s → 截图 → OCR」。
   这就是 `check_progress.py` 存在的原因。

### 打包

10. **`import cv2` 会让产物多出 112MB**。PyInstaller 把整个 opencv-python
    打进去，而 MaaFramework 自己已链接 `opencv_world4_maa.dll`。
    项目只拿 cv2 存 PNG，所以 `build.spec` 排除了它。**260MB → 162MB**。

11. **排掉 cv2 后必须显式打包 Pillow**。Pillow **不是** MaaFw 的依赖
    （`MaaFw` 只依赖 `maaagentbinary, numpy, strenum`），而且是通过函数内
    import 加载的，静态分析抓不到。第一版排掉 cv2 后产物里既没 cv2 也没 PIL，
    存图直接失败——**靠 `--selftest` 才发现的**，肉眼看 GUI 看不出来。

12. **`Path(__file__)` 推算的路径打包后全失效**。冻结后 `__file__` 指向
    `_MEIPASS`，而用户数据必须写到 exe 旁边。见 `scripts/paths.py`。

13. **MaaAgentBinary 不在 `maa` 包内部**，而在 site-packages 根目录
    （`maa` 的上一级）。PyInstaller 自动分析找不到，必须手动加进 `datas`。

14. **用 onedir 而不是 onefile**。onefile 每次启动都要把 ~60MB 原生库解压到
    临时目录，慢好几秒，还更容易被杀软拦。

15. **Windows 控制台默认 GBK**。OCR 出来的中文/符号会抛
    `UnicodeEncodeError`。所有脚本入口都强制 `stdout.reconfigure(utf-8)`。

---

## 8. 开发流程

### 改管线后

```powershell
python scripts\validate_pipeline.py     # 先静态校验，比让框架报错快
python scripts\main.py --check          # 再确认能加载
```

### 确定某个元素的坐标

```powershell
python scripts\main.py --ocr                      # 全屏 OCR，打印文本+坐标
python scripts\main.py --ocr --roi 0,452,300,30   # 指定区域
```

输出会附带可直接粘进管线的 `roi` 建议值。

### 调试播放器进度

```powershell
python scripts\check_progress.py -n 5 -i 15
```

在同一进程内连读 5 次，每次落盘截图，方便核对「OCR 读数」与「当时画面」。

### 手动交互

```powershell
python scripts\tap.py size                       # 查当前画布尺寸
python scripts\tap.py click 360 200
python scripts\tap.py text "some ascii"
python scripts\tap.py key back
python scripts\tap.py swipe 360 1000 360 400
```

### 源码模式运行启动器

```powershell
python launcher.py
python launcher.py --selftest
```

---

## 9. 构建

```powershell
python -m pip install -r requirements-dev.txt
powershell -File build.ps1
```

`build.ps1` 做四件事：停掉正在跑的 exe → PyInstaller 打包 → **把整个
`dist/MaaElearning/` 同步到项目根** → 校验打进去的脚本和源码一致。

⚠️ **别手工只复制 `MaaElearning.exe`**。onedir 版的 Python 代码在
`_internal\scripts\` 里，exe 只是个壳 —— 只复制 exe 的话，改了脚本重新
打包、跑起来**还是旧代码**。实测被这个坑过一次：改动明明进了 `dist`，
项目根的 `_internal\scripts\core.py` 还是半小时前的版本。

产物：`dist/MaaElearning/`（整体 162 MB）

**构建后必做**：

```powershell
.\MaaElearning.exe --selftest
```

必须 6 项全 ✓ 才算构建成功。第 5 项需要 MuMu 已启动。

### 运行时依赖

```
MaaFw           5.14.2     # 含原生 DLL
Pillow          12.x       # 存图（cv2 被排除后的唯一实现）
numpy                      # MaaFw 依赖
```

开发额外需要 `pyinstaller`。源码模式若装了 `opencv-python` 也可以，
`imageio_util` 会优先用它。

---

## 10. 待完成

1. **课后考试** —— 已确认考知识点的题都在课后考试里，但还没见过考试页面的
   版式，所以 `QuizAnswer` 的 `stem_roi` / `option_rois` 无法配置。
   需要一张考试页截图。

2. **签到点击** —— 「立即签到」按钮对合成事件无响应（实测 adb tap 与
   MaaTouch 都无法触发，两次截图逐字节相同）。尚未查清原因。
   对比参考：弹题里的点击**是有效的**，所以不是所有弹层都拦合成事件。
   复现方法：隔天早上签到弹窗自动出现时跑
   `python scripts\tap.py click 360 806`，看「本月连续签到N次」是否 +1。

3. **多课程连续播放** —— 目前只处理「第一门课的第一个视频」。
   完整的多视频/多课程轮转需要先确认平台对「学完」的判定与页面反馈。

4. **不开微信的路线**（见 7.4）—— 已实测：在模拟器自带的 Chromium 里
   **先**用 CDP 覆盖成桌面 UA（顺序错了 SPA 会卡死，`Runtime.evaluate` 一律超时），
   **再**导航到 `https://elearning.zs-hospital.sh.cn/`，桌面版完整打开、
   `wechatOnly: False`；视口钉成 720x1280 后 OCR 坐标可直接用。
   **还差**：登录一次（`/learning/login` 的验证码或密码登录）、
   进课程页确认**视频能播、能记进度**，然后把导航层从手机版改写成桌面版。
