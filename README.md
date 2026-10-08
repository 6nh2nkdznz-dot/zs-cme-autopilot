# 🏥 中山医院继续教育平台助手

💊 自动学完复旦大学附属中山医院远程继续医学教育平台（`elearning.zs-hospital.sh.cn`）的课程视频、课后考核与问卷，一路做到课程结课。

| ① | ② | ③ | ④ | ⑤ |
|:--:|:--:|:--:|:--:|:--:|
| 🎯 选课 | 📺 看视频 | 📝 考核 | 📋 问卷 | 🎓 结课 |
| 自动翻页读全 | 真播 1:1，不能加速 | 先蒙后收答案 | 挑最正面选项 | 归档发证书 |

| 项目 | 说明 |
|:--|:--|
| 🎯 目标平台 | `elearning.zs-hospital.sh.cn`（另一个域 `course.zs-hospital.sh.cn` 放课件和考核） |
| 🖥️ 运行环境 | Windows 10 / 11（64 位） |
| 🧩 需要装什么 | 什么都不用 —— 不装 Python、不装 MaaFramework，运行时全打包在 `_internal/` 里 |
| 📦 交付物 | `ZSCMEAutopilot.exe` + `_internal/`（整文件夹一起拷） |
| 🧪 自测 | 928 项全绿；管线 5 个文件 / 58 个节点全部通过校验 |
| 📊 实测战果 | 账号下 17 门课，16 门已结课（1 门因平台问卷过期做不了） |

---

## 🗺️ 两条路线，先选一条

平台有两套前端，同一个账号进去看到的是同一批课，但页面结构完全不同：

| | 🌐 桌面版（浏览器电脑模式） | 📱 手机版（模拟器 + 微信） |
|:--|:--|:--|
| 怎么进 | 把浏览器 UA 改成桌面 UA，打开 `https://elearning.zs-hospital.sh.cn/` | 在模拟器里装微信，从聊天链接点进去 |
| 要不要微信 | 🟢 完全不用 | 🔴 必须用，且模拟器里被登出会反复重登 |
| 封号风险 | 🟢 不碰微信，等于普通网页登录 | 🟠 模拟器里跑微信，风控面更大 |
| 判断视频进度 | 🟢 页面有原生 `<video>`，直接读 `currentTime` / `duration` | 🔴 只能截图 + OCR 认左下角时长 |
| 找页面元素 | 🟢 走 DOM / 接口，不用认坐标 | 🔴 全靠硬编码坐标 + OCR |
| 考核 | 🟢 平台公布答案，脚本收了重做 | 🟢 同上（题库两边共用不了，域名不同） |
| 稳定性 | 🟢 实测跑通 16 门课 | 🟠 模拟器的微信解码器会 SIGSEGV 崩，得靠看护拉起 |
| 推荐 | ⭐ 首选 | 留作备选 |

> 💡 桌面版能走通的根本原因：平台桌面版 `/login` 的「微信登录」在源码里是注释掉的，账号密码 / 手机号短信码都能登。手机版 `/mobile/` 则对非微信 UA 硬拒（页面只写「手机端仅支持微信访问」）。

---

## ✅ 前提条件

### 🧱 通用

| 要求 | 说明 |
|:--|:--|
| 操作系统 | Windows 10 / 11 64 位 |
| 安卓设备 | ⭐ MuMu 模拟器 12（推荐）或真机 |
| 显示器 | 建议 1920×1080 及以上 |
| 平台账号 | 能登录的实名账号 |

### 🌐 桌面版专有

| 要求 | 说明 |
|:--|:--|
| 模拟器已启动 | 进到安卓桌面就行，端口程序自己探测 |
| 安卓里有浏览器 | 认这 4 个包名：`com.android.chromium`（MuMu 自带）、`com.android.chrome`、`com.tencent.mtt`、`com.UCMobile` |
| 登录过一次 | 手机号 + 短信验证码，或账号 + 密码 + 图形验证码。登录态会存在浏览器里，之后不用重复登 |
| 🔌 真机才需要 | 打开「USB 调试」并用数据线连着电脑 |

> ⚠️ 运行期间别最小化模拟器窗口，也别让锁屏盖住它 —— 安卓端到后台会暂停视频解码，进度就不涨了。
> ⚠️ 运行期间不要同时手动操作浏览器，你的点击会和脚本的打架。
> ⚠️ 别用 `--restart`。那会重启浏览器并丢掉登录态；非必要不加这个参数。

### 📱 手机版专有

| 要求 | 说明 |
|:--|:--|
| 微信 | 必须装文件名带 `_arm64` 的最新版（本项目验证 8.0.79 可用） |
| 架构坑 | 官网「通用版」只有 `armeabi-v7a`，MuMu 上是 `x86`，装了会闪退 |
| 版本坑 | 版本旧了扫码也会被拦：「当前客户端版本过低，请前往应用商店升级」 |
| 人工步骤 | 模拟器里登录微信、在微信里打开平台并登录 —— 这两步脚本代替不了 |

---

## 🚀 操作步骤

### 第 0 步 · 部署

把整个 `ZSCMEAutopilot` 文件夹拷到目标机器。不能只拷 exe —— `_internal/` 里是 Python 运行时、MaaFramework 原生库、OCR 模型和自动化管线。

```
ZSCMEAutopilot/
├── 🖱️ ZSCMEAutopilot.exe      ← 双击这个
├── 📁 _internal/            ← 必须一起拷
├── 📁 data/                 ← 首次运行自动生成（题库、配置）
└── 📁 debug/                ← 首次运行自动生成（日志、截图）
```

### 第 1 步 · 启动模拟器

打开 MuMu 模拟器，等它进入桌面。端口不用操心，程序会自己向 `MuMuManager` 查询 `adb_port`（默认 16384，多开时递增）。

### 第 2 步 · 让浏览器登录一次

桌面版路线只要登录一次，之后登录态一直在。

🖱️ 图形界面：双击 `ZSCMEAutopilot.exe` → 点「📱 手机浏览器登录（桌面版）」。

⌨️ 命令行等价物：

```powershell
ZSCMEAutopilot.exe --run browser --login                  # 拉起浏览器并打开登录页
ZSCMEAutopilot.exe --run browser --buttons                # 只列出页面上扫到的按钮，不填不点
ZSCMEAutopilot.exe --run browser --enter-code 123456      # 把 6 位短信码填进去并点「立即登录」
ZSCMEAutopilot.exe --run browser --info                   # 只看当前页面现状，什么都不动
```

如果在 `data/config.json` 里填了 `browser.phone`，点那个按钮时会自动把手机号填进登录页并点「获取验证码」，你只要把收到的 6 位短信码填进去。

### 第 3 步 · 运行前自检（换机器必做）

```powershell
ZSCMEAutopilot.exe --selftest
```

依次验证 6 项，每项打 ✓ 或 ✗：

```
[1/6] 检查打包路径…      资源包 / 管线 / OCR 模型 / 配置 / Agent 二进制
[2/6] 检查存图能力…
[3/6] 自动探测 adb…      与运行实例版本匹配的引擎自带 adb
[4/6] 探测模拟器端点…    MuMuManager 报的 adb_port，并连上去验证
[5/6] 加载资源与管线…    管线节点 + OCR 模型
[6/6] 连接模拟器…        connected: True
```

任何一项 ✗ 都先解决再往下走。第 6 项会真的去连模拟器，所以跑之前要确保模拟器开着。

### 第 4 步 · 看视频

```powershell
ZSCMEAutopilot.exe --run desktop_watch --list                 # 只列课程，不动手
ZSCMEAutopilot.exe --run desktop_watch --dry-run              # 只报准备做什么
ZSCMEAutopilot.exe --run desktop_watch                        # 全部课，全部讲
ZSCMEAutopilot.exe --run desktop_watch --course 肝胆          # 只跑名字含「肝胆」的课
ZSCMEAutopilot.exe --run desktop_watch --lessons 2            # 每门课最多看 2 讲
```

程序会：翻页读全部课程 → 进课 → 找到第一个没看完的讲次 → 播到 97% → 平台确认学完 → 下一讲。

日志长这样：

```
[desk] 「我的学习」这一页只显示 8 门，平台一共 17 门 —— 按分页把后面的也读出来
[desk] 平台一共 17 门课，翻页读到 17 门
[desk] 走平台入口进课: userEnterClass courseId=40288abd9d7032f1…
[watch] 第3讲 服务端已学 12.4 分钟 / 45.2 分钟
[watch] 第3讲 ✓ 平台状态已是学完
[watch] ✓ 复核对上了：肝胆…的视频课件现在全部学完
```

> ⏳ 视频是真实时长，一讲 45~60 分钟，整门课可能 8~10 小时。平台按真实播放 1:1 记账，快进无效 —— 这是平台的设计，绕不过去。可以挂着过夜。

### 第 5 步 · 考核 + 问卷

```powershell
ZSCMEAutopilot.exe --run desktop_exam --list                  # 只列出考核和问卷，不动手
ZSCMEAutopilot.exe --run desktop_exam --harvest-only          # ⭐ 开跑前先收一遍答案，最划算
ZSCMEAutopilot.exe --run desktop_exam                         # 考核 + 问卷，全部课
ZSCMEAutopilot.exe --run desktop_exam --course 肝胆 --max-courses 1
ZSCMEAutopilot.exe --run desktop_exam --exam-only             # 只做考核
ZSCMEAutopilot.exe --run desktop_exam --questionnaire-only    # 只补问卷
```

⭐ 先跑 `--harvest-only`。平台只要这门课历史上交过一次卷，官方答案就一直挂在「查看」页上 —— 不用重新交卷就能收。一次卷都不交能收下 160+ 道题（当前缓存 167 道），而且题库是跨课共用的：收得越多，后面没考过的课要蒙的就越少，每少蒙一门就少浪费一次「重做次数」。

考核的流程是「先蒙一次 → 收官方答案 → 带着答案重做」：

```
[exam] 「本项目考核」第 1/3 次：20 道题（danxuan, duoxuan），手上有答案 16 道，蒙 4 道
[exam] 交卷回执：{}
[exam] 「本项目考核」35 分，还没到 60
[exam] 收下 20 道题的正确答案
[exam] 「本项目考核」第 2/3 次：20 道题（danxuan, duoxuan），手上有答案 19 道，蒙 1 道
[exam] ✓ 「本项目考核」85 分，过了
```

> 💡 答案缓存按题干索引，而且同时存正确选项的文字。因为平台每次发卷选项顺序都会重排 —— 同一道题在查看页正确项是 `A`，切到答题页可能变成 `B`，照抄字母必错。

### 第 6 步 · 申请结课

三件事都齐了之后：

```powershell
ZSCMEAutopilot.exe --run desktop_exam --finish
```

> ⚠️ 这一步默认不做，要显式加 `--finish`。结课会把课程归档发证书，之后就刷不了分了 —— 留给用户自己决定。

```
——— 结课 ———
[exam] ✓ 「肝胆…学习班2026-」结课申请成功
[exam] 「重症…2026-19-01-018（国」已经结课了（2026-10-08 13:56:32）
```

---

## 🎓 结课的四道关

平台的结课条件是页面上一句话：`完成所有视频课件学习+考核≥60分+完成问卷调查`。三件事有强制顺序：

| 关 | 做什么 | 命令 | 硬门槛 |
|:--:|:--|:--|:--|
| 1️⃣ | 📺 看完视频 | `desktop_watch` | 平台按真实播放时长 1:1 记账 |
| 2️⃣ | 📝 考核 ≥60 分 | `desktop_exam` | 视频没学完会弹「请先完成课程视频学习，再进行考核！」 |
| 3️⃣ | 📋 完成问卷 | `desktop_exam` | 考核不到 60 分会回「请先完成课程学习，再进行问卷作答」 |
| 4️⃣ | 🎓 申请结课 | `desktop_exam --finish` | 三件齐全才受理 |

> ⚠️ 别拿页面上的「考核85分，已完成问卷调查」当门禁。那是服务端算好的快照、会滞后 —— 实测刚交完问卷，那几门仍写着「未完成问卷调查」，而问卷接口那边 `isSubmit: true` 已经明明白白。判断一律看接口。

---

## 🖥️ 图形界面

双击 `ZSCMEAutopilot.exe`（源码运行 `python launcher.py`）会开一个深色窗口 —— `customtkinter` 做的，深色主题、卡片分区、圆角控件、状态灯：

| 区域 | 内容 |
|:--|:--|
| 左栏 · 顶部 | 数据目录 · 日志目录 · 调试视图 三个按钮 |
| 左栏 · 环境状态 | adb / 配置 / 连接 / 资源 四项指示灯，绿了才算就绪 |
| 左栏 · 要执行的任务 | 整门课轮播 · 每日签到 · 进入考核并答题 · 只看护当前视频 四个开关 |
| 左栏 · 底部 | 检查环境 → 手机浏览器登录（桌面版）→ 开始运行 |
| 右栏 | 运行日志，按级别着色（错误红 / 成功绿 / 警告黄），可复制可清空 |

「开始运行」和「立即停止」是同一个按钮同一个位置 —— 空闲时是蓝色「▶ 开始运行」，运行中变成红色「■ 立即停止」。点下去**当场停手**：正在看护的那一节也会立刻退出，不用等这节课播完。

界面只覆盖手机版那 4 个常驻任务。桌面版那套走命令行 —— exe 内置了通用入口，不用装 Python 就能跑任意脚本：

```powershell
ZSCMEAutopilot.exe --list                      # 看有哪些脚本
ZSCMEAutopilot.exe --run desktop_watch         # 跑桌面版看课
ZSCMEAutopilot.exe --run desktop_exam          # 跑桌面版考核 + 问卷
ZSCMEAutopilot.exe --selftest                  # 打包自检
ZSCMEAutopilot.exe --classic                   # 用旧的经典界面
```

---

## ⚙️ 配置

首次运行自动生成 `data/config.json`。大多数情况下一个字都不用改 —— adb 路径和模拟器地址默认留空就是全自动探测。

| 键 | 默认 | 说明 |
|:--|:--|:--|
| `adb.adb_path` | 空 | 留空 = 自动探测。只在探测失败时填完整路径（用双反斜杠） |
| `adb.address` | 空 | 留空 = 自动探测。填了优先用。端口从 `MuMuManager info -v all` 的 `adb_port` 读 |
| `browser.phone` | 空 | 手机号。填了就能一键填号并点「获取验证码」 |
| `browser.debug_port` | `9222` | 设备浏览器的调试端口转发到电脑上的哪个端口，一般不用改 |
| `screenshot.target_long_side` | `1280` | 长边归一。1080×1920 归一后是 720×1280，与框架 720p 基线一致 |

自动探测覆盖这些情况：adb 找「与正在运行的实例版本匹配」的引擎自带 adb → Android SDK → PATH → 注册表 → 运行中的进程 → 目录扫描；地址向 `MuMuManager` 查所有实例的 `adb_port`（多开也覆盖）。每一步都会真的验证（跑得起来 `adb version` / 连得上 / 是真安卓设备），验证通过才采用 —— 不假设安装目录，不靠猜。

---

## 📂 文件都在哪

| 路径 | 内容 |
|:--|:--|
| `data/config.json` | 生效的配置 |
| `data/exam_answers.json` | 🌐 桌面版题库缓存（按题干索引，存正确选项的文字） |
| `data/answer_cache.json` | 📱 手机版题库缓存 |
| `data/course_progress.json` | 📱 手机版看课进度 |
| `debug/` | 运行日志、截图、临时探针（整个目录不进仓库） |

> 💡 首次启动会自动接管旧数据：exe 会往上找上层目录里的 `data/`，题库为空时自动搬过来并在日志里打印搬运记录。只在题库为空时搬，绝不覆盖已有的。

---

## 🩺 排查问题

| 现象 | 原因与处理 |
|:--|:--|
| 自检第 3 项 ✗ 找不到 adb | 装 Android SDK platform-tools 并加进 PATH；或在 `data/config.json` 的 `adb.adb_path` 写死 |
| 自检第 4 项 ✗ 连接失败 | ① 模拟器是否已启动进入桌面；② `MuMuManager.exe info -v 0` 看 `adb_port`；③ 端口被占 |
| `--run browser --info` 说没登录 | 登录态丢了。跑 `--run browser --login` 登一次（别加 `--restart`） |
| 课程列表读不到 | 先确认页面上是「我的学习」（`module=learning`）那一页。课程列表只在 elearning 域有 |
| 只看到 8 门课 | 已经在翻了。平台一页 8 门，`courses()` 会自动按接口把剩下的读回来 |
| 视频进度不涨 | ① 模拟器是否被最小化 / 遮挡；② 页面是否掉登录了 |
| 考核交完分数没变 | 服务端批改是异步的，交完立刻查拿到的是上一次的结果。程序会自动重读最多 30 秒 |
| 问卷回「请先完成课程学习」 | 考核还没到 60 分。先把考核做过去 |
| 问卷回「问卷已过期」 | 平台侧的窗口关了，做不了。这不是脚本的问题 |
| 日志里全是识别失败 | 先 `--selftest` 确认资源加载正常；再确认平台登录没过期 |
| 窗口显示偏小 / 位置怪 | 程序启动时会自己设 DPI 感知，日志里会报实际生效值 |

---

## ⚠️ 已知限制

| | 说明 |
|:--|:--|
| 🐢 无法加速 | 平台按真实播放时长记账，一讲就是 45~60 分钟。这是平台设计，不是脚本慢 |
| 🚫 最后一门课做不完 | 账号里「三维超声心动图新技术的临床应用 2025-03-01-002(沪远)」的问卷有效期到 `2025-11-30`，平台已拒绝受理。结课要三件齐全，所以这门课结不了 —— 视频和考核虽然能做，但做完照样结不了，已确认不值得做 |
| 📱 手机版有历史问题 | 「进入答题」的真机验证、微信解码器崩溃后的自愈，都还差一次完整的成功记录 |
| 🖼️ 截图会超时 | `Page.captureScreenshot` 在这套环境上会 `TimeoutError`，所以桌面版一律不依赖截图 |

---

## 📱 附：手机版（旧路线）

如果哪天必须走微信，这套还在：`scripts/run_exam_watch.py`、`scripts/course.py`、`scripts/exam.py`、`scripts/harvest_answers.py`、`scripts/retake_exam.py`。

```powershell
ZSCMEAutopilot.exe --run run_exam_watch                    # 看护一门课（全部课节）
ZSCMEAutopilot.exe --run run_exam_watch --forget           # 忘掉本地进度，重看
ZSCMEAutopilot.exe --run run_exam_watch --max-lessons 2    # 最多看 2 节
ZSCMEAutopilot.exe --run run_full_exam                     # 跑完整卷并交卷
ZSCMEAutopilot.exe --run harvest_answers                   # 从结果页采集官方答案
ZSCMEAutopilot.exe --run pending list                      # 看待答题
```

它的看课流程是「滚回目录顶部 → 逐屏 OCR 解析出视频条目 → 点条目标题 → 看护到学完 → 点下一条」。因为目录里没有「已完成」标记，所以本地记一份进度（`data/course_progress.json`）：

```
[course] 本地记录已完成，跳过: 4吴琳-认知症的流行病学…
```

> 记录只是旁证，平台那边才是真的。如果某节实际去看了、拉进度发现没达标，程序会立刻撤销该条记录并重看。想全部重看加 `--forget`。

| 题型 | 选项 | 处理 |
|:--|:--|:--|
| 单选 | A/B/C/D | 题库 → 联网 → 挂起 |
| 多选 | A/B/C/D | 同上（置信度门槛更严） |
| 判断 | T/F | 同上，字母映射已适配 |
| 填空 / 简答 | — | 一律转人工，无法可靠自动作答 |

---

## 📜 许可与第三方组件

本项目自身以 [MIT 许可](LICENSE) 发布。

要分两种情况说：

**① 只拿源代码自己构建** —— 仓库里有两样东西不是我们写的：

| 内容 | 来源 | 许可 | 说明 |
|:--|:--|:--|:--|
| `assets/resource/model/ocr/` 下的 `det.onnx` / `rec.onnx` / `keys.txt` | [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 转 ONNX | Apache-2.0 | 文字识别模型。保留原作者署名，不适用本项目的 MIT 许可 |
| [MaaFramework](https://github.com/MaaXYZ/MaaFramework) | MaaXYZ | LGPL-3.0 | 自动化框架本体 |

MaaFramework 的**源码不在本仓库里**（`vendor/` 在 `.gitignore` 中），需要你自己 `pip install MaaFw==5.14.2` 装上。本项目只是调用它公开的接口 —— 包括继承 `CustomAction` / `CustomRecognition` 来写自己的动作。LGPL-3.0 第 0 节明写「继承库中定义的类属于使用接口」，因此本项目是 LGPL 定义的 Application，不受其传染，可以自行选择许可。

**② 用 Release 里的打包版，或者你要把打好的包分发出去** —— 这时候情况不一样了：`_internal/maa/bin/` 里装着 MaaFramework 的原生 DLL，等于**我们在分发它**，LGPL-3.0 的告知义务就落到分发者头上。

所以包里带了这些：

- [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) —— 逐项列出包内所有第三方组件、版本、许可与位置
- [`LICENSES/LGPL-3.0.txt`](LICENSES/LGPL-3.0.txt) 与 [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt) —— LGPL-3.0 与它引用的 GPL-3.0 全文

LGPL 要求使用者能替换掉那个库。本项目**没有把它静态链接进 exe** —— DLL 是原封不动放在 `_internal/maa/bin/` 里的独立文件，拿一份自行编译的 MaaFramework 覆盖同名文件即可，不需要重新编译本项目。


---

## ⚖️ 免责声明

平台账号为实名账号，连续挂机可能触发风控；用模拟器刷继续教育学时在服务条款层面是不被允许的。请自行评估风险，本项目仅供技术研究。

---

## 📚 延伸阅读

| 文档 | 内容 |
|:--|:--|
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 源码构建、管线结构、坐标表、接口契约、调试工具 |
| [`STATUS.md`](STATUS.md) | 开发过程复盘（历史快照）—— 几轮调试里踩过的坑与故障分析 |
| [`LICENSE`](LICENSE) | MIT 许可 |
| [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) | 打包版里每个第三方组件的来源、版本与许可 |
