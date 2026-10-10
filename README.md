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
| 🧩 核心依赖 | **本项目依赖 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 这个自动化框架** —— 设备控制、截图、OCR、任务流水线都由它提供；我们写的是跑在它上面的业务脚本（见 [依赖 MaaFramework 框架](#-依赖-maaframework-框架)）。框架**不用你自己装**，运行时全打包在 `_internal/` 里 |
| 📦 交付物 | `ZSCMEAutopilot.exe` + `_internal/`（整文件夹一起拷） |
| 🧪 自测 | 1240 / 1243 项通过；管线 5 个文件 / 58 个节点全部通过校验 |

> 🧪 那 3 项没过的自测要求「模拟器里装着微信」—— 这台机器上微信已被卸载，测不了。其余全绿。

---

## 🧬 依赖 MaaFramework 框架

**本项目不是一个独立实现，它是 [MaaFramework](https://github.com/MaaXYZ/MaaFramework) 的上层应用**（框架由 [MaaXYZ](https://github.com/MaaXYZ) 开发，和 MAA 同源）。所有「和安卓打交道」的脏活都是框架在做：

| 谁做 | 做什么 |
|:--|:--|
| **MaaFramework**（`_internal/maa/bin/*.dll`） | 连 adb / 投屏截图 / 注入触摸 / 跑 OCR / 执行 `assets/resource/pipeline/*.json` 里那条任务流水线 / 按 `RecognitionDetail` 回报每一步的识别结果 |
| **本项目**（`scripts/`、`launcher_ui.py`） | 平台业务：页面怎么进、哪一步该点哪、视频播完没有、答案怎么重映射、界面长什么样 |

所以仓库里 `assets/resource/pipeline/` 下那 5 个 JSON、58 个节点不是配置文件而是**程序**（框架直接执行它们），`scripts/` 里的 Python 则通过 `maa` 这个 Python 绑定（`import maa`）驱动框架。

> 📦 **打包版已经把框架装进去了**，你不需要单独下载 MaaFramework。源码运行才需要自己准备 —— 见 [从源码构建](#-从源码构建)。

> 🌐 框架的文档、协议、schema 在开发时放在 `vendor/MaaFramework/`（**不进仓库**，见 `.gitignore`），需要用的时候从框架仓库自己拉一份。本项目用到的版本：**MaaFramework 5.14.2**。

---

## 📥 先拿到程序

去 [**Releases**](https://github.com/6nh2nkdznz-dot/zs-cme-autopilot/releases/latest) 下载 `ZSCMEAutopilot-v1.0.0-win64.zip`（约 79 MB），**解压**出 `ZSCMEAutopilot` 文件夹 —— 里面的 `ZSCMEAutopilot.exe` 就是主程序，双击它。

> ⚠️ **别把 exe 单独拷出来用。** 这是 PyInstaller 的 onedir 构建：exe 本身只有 6.7 MB，只是个引导壳，Python 运行时、MaaFramework 的原生 DLL、OCR 模型、自动化管线全在旁边的 `_internal/` 里（1200 多个文件）。单独拷 exe 双击会报错，要挪位置就**整个文件夹一起挪**。

> 💡 仓库里翻不到 exe 是故意的 —— `.gitignore` 排除了 `*.exe` / `_internal/` / `dist/` / `build/`，仓库只放源码，可执行文件只在 Releases 里。

---

## ⚡ 三步跑起来

| 步骤 | 图形界面 | 命令行等价物 |
|:--:|:--|:--|
| 1️⃣ | 打开 MuMu 模拟器，等它进到安卓桌面 | — |
| 2️⃣ | 点左栏底部的「🌐 浏览器登录（电脑模式）」，在弹出的登录页上自己登一次（手机号 + 短信码，或账号 + 密码 + 图形验证码） | `--run browser --login` |
| 3️⃣ | 路线保持默认的「**浏览器版（推荐）**」，勾上要做的任务，点「▶ 开始运行」 | 见 [第 3~6 步](#-命令行跟界面等价) |

**日常使用只需要图形界面，不用敲命令行** —— 左栏那四个任务开关就对应第 3~6 步，勾哪个跑哪个。每个开关右边还有个 **⚙** 能单独调这一项的参数。

登录态存在那个浏览器里，只要不重启浏览器就不用再登第二次。

> ⚠️ **登录态活不过浏览器进程。** 模拟器在后台放久了，安卓可能自己把浏览器杀掉，会话 cookie 就跟着没了（实测：被杀之后 `Network.getAllCookies` 回 0 条）。这时程序**不会摔一串报错**，而是自动把登录页开出来、打一句人话、用退出码 `4` 停下：
>
> ```
> [desk] 登录页已打开：https://elearning.zs-hospital.sh.cn/learning/login
> ======================================================================
> [exam] 等了 10 分钟 还是没登录 —— 先登录再跑
> 在模拟器那个浏览器窗口里登录一次，再重新跑一遍就行。
> ======================================================================
> ```
>
> 登完再点一次「开始运行」即可 —— 已经看完的讲次平台那边都记着，不会白看。

---

## 🗺️ 两条路线

平台有两套前端，同一个账号进去看到的是同一批课，但页面结构完全不同。**程序默认走浏览器版，微信版是留着备选的。**

| | 🌐 浏览器版（默认 ⭐） | 📱 微信版（备选） |
|:--|:--|:--|
| 怎么进 | 把模拟器浏览器的 UA 改成桌面 UA，打开 `https://elearning.zs-hospital.sh.cn/` | 在模拟器里开微信，从聊天链接点进平台 |
| 要不要微信 | 🟢 完全不用 | 🔴 必须用 |
| 封号风险 | 🟢 不碰微信，就是一次普通网页登录 | 🔴 模拟器 + 微信 + 自动点击，风控面大得多 |
| 判断视频进度 | 🟢 页面是原生 `<video>`，直接读 `currentTime` / `duration` | 🔴 只能截图 + OCR 认左下角时长 |
| 找页面元素 | 🟢 走 DOM / 接口，不用认坐标 | 🔴 全靠硬编码坐标 + OCR |
| 模拟器解码器 | 🟢 只用浏览器的 | 🔴 微信自己的 `MediaCodec_loop` 会 SIGSEGV 崩（见下） |
| 稳定性 | 🟢 实测跑通 16 门课 | 🟠 崩了得靠看护程序拉起来 |
| 界面默认 | ✅ 选中 | 要手动切过去 |

> 💡 浏览器版能走通的根本原因：平台**桌面版** `/login` 的「微信登录」在源码里是注释掉的，账号密码 / 手机号短信码都能登。而**手机版** `/mobile/` 对非微信 UA 是硬拒的（页面只写「手机端仅支持微信访问…用微信扫码识别后进行登录」）。
>
> ⚠️ **为什么把微信版降级**：在模拟器里跑微信，一方面风控面明显更大（平台账号是实名的），另一方面这台模拟器是 `x86_64`、走的是老 OMX 解码路径（`debug.stagefright.ccodec=0`），微信的 `MediaCodec_loop` 线程会在 `libstagefright.so` 的 `MediaCodec::setState` 上反复 SIGSEGV —— 实测一天崩 17 次，而且模拟器侧没有可调的解码开关。**浏览器版把这两个问题一起绕开了。**

---

## ✅ 前提条件

### 🧱 通用

| 要求 | 说明 |
|:--|:--|
| 操作系统 | Windows 10 / 11 64 位 |
| 安卓设备 | ⭐ MuMu 模拟器 12（推荐）或真机 |
| 显示器 | 建议 1920×1080 及以上 |
| 平台账号 | 能登录的实名账号 |

### 🌐 浏览器版专有

| 要求 | 说明 |
|:--|:--|
| 模拟器已启动 | 进到安卓桌面就行，端口程序自己探测 |
| 安卓里有浏览器 | 认这 4 个包名：`com.android.chromium`（MuMu 自带）、`com.android.chrome`、`com.tencent.mtt`、`com.UCMobile` |
| 登录过一次 | 手机号 + 短信验证码，或账号 + 密码 + 图形验证码。登录态存在浏览器里，之后不用重复登 |
| 🔌 真机才需要 | 打开「USB 调试」并用数据线连着电脑 |

> ⚠️ 运行期间别最小化模拟器窗口，也别让锁屏盖住它 —— 安卓端到后台会暂停视频解码，进度就不涨了。
> ⚠️ 运行期间不要同时手动操作那个浏览器，你的点击会和脚本的打架。
> ⚠️ **别随手加 `--restart`**：那会重启浏览器，登录态就没了。

### 📱 微信版专有（走这条才需要看）

| 要求 | 说明 |
|:--|:--|
| 微信 | 必须装文件名带 `_arm64` 的最新版（本项目验证 8.0.79 可用） |
| 架构坑 | 官网「通用版」只有 `armeabi-v7a`，MuMu 上是 `x86`，装了会闪退 |
| 版本坑 | 版本旧了扫码也会被拦：「当前客户端版本过低，请前往应用商店升级」 |
| 人工步骤 | 模拟器里登录微信、在微信里打开平台并登录 —— 这两步脚本代替不了 |

---

## 🖥️ 图形界面

双击 `ZSCMEAutopilot.exe`（源码运行 `python launcher.py`）会开一个深色窗口 —— `customtkinter` 做的，深色主题、卡片分区、圆角控件、状态灯。

窗口从左到右**四栏等宽**，各占约四分之一：

| 栏 | 内容 |
|:--|:--|
| ① 左栏 · 顶部 | 数据目录 · 日志目录 · 调试视图 三个按钮 |
| ① 左栏 · 环境状态 | adb / 配置 / 连接 / 资源 四项指示灯，绿了才算就绪 |
| ① 左栏 · **用哪条路线** | 「浏览器版（推荐）」/「微信版（有风险）」二选一，**默认浏览器版**，下面一行小字说明这条路线怎么工作 |
| ① 左栏 · 要执行的任务 | 跟着路线变（见下表），每行右边一个 **⚙** 可以单独设置这一项 |
| ① 左栏 · 底部 | ⚙ 全局设置 → 检查环境 → 🌐 浏览器登录（电脑模式）→ 开始运行 |
| ② **设置栏** | 点 ⚙ 就在这里就地展开那个任务的设置；没点之前显示 **「点击左边的设置以设置选项」** |
| ③ 运行日志 | 按级别着色（错误红 / 成功绿 / 警告黄），可复制可清空 |
| ④ 调试画面 | 模拟器实时画面 + 识别明细（识别到的文字块、判定结果） |

> 💡 **设置是"就地展开"而不是弹窗。** 点 ⚙ 之后设置渲染在②栏里，**不再盖住日志** —— 一边调参数一边能看见上次跑的输出。面板底部固定着「恢复默认 / 保存」两个按钮，设置再多也够得着（列表本身可以滚）。
> 💡 ②栏底部的红字是校验提示。填了非法值（比如时长填了「九十分钟」）会停在这一栏，不会把面板关掉、也不会写进配置。

### ⚙ 每一项都能单独设置

每个任务开关右边有个 **⚙** 按钮，点它就是那个任务自己的设置（**每项的设置互不影响** —— 「看课」里填的课程名不会跑到「刷时长」里去）：

| 任务 | 能设什么 |
|:--|:--|
| 📺 自动看课 | **自动切换课程**（开关）· 只做哪门课 · 每门课最多看几讲 · 最多做几门课 |
| ⏱️ 刷学习时长 | 刷到多久（**可填小数**）+ **时长单位**（分钟/小时）· 每门课都刷一遍 · 每门课最多刷几小时 · 只刷哪门课 |
| 📝 考核 + 问卷 | 只做哪门课 · 做哪一半（都做 / **仅考核** / **仅问卷**）· 已经做完的也重做 · **AI 接口地址** · **AI 密钥** · **AI 模型名** · 最多做几门课 |
| 🎓 申请结课 | —— **没有设置**。这一步只有「点一下申请」这一个动作，所以那个 ⚙ 干脆不显示 |

> 📺 **「自动切换课程」默认是关的** —— 也就是**只看一门**。看课每一讲就是 45~60 分钟真实时间（进度按平台服务端的播放记录算，快进无效），一晚上只够一门。想让它一门接一门往下跑就把它勾上。

左栏底部的 **⚙** 是全局设置，两项都跟具体任务无关：

| 全局项 | 说明 |
|:--|:--|
| **算力** | 文字识别（OCR）用哪个算力跑：`自动（推荐）` / `显卡加速` / `只用 CPU`，见 [GPU 加速推理](#-gpu-加速推理) |
| **显卡序号** | 留空（推荐）。只有自动挑错了才填 |
| **浏览器调试端口** | 默认 `9222`。除非端口被别的程序占了，否则别改 |

> 💡 **⚙ 变成 `⚙*`（带星号、高亮色）** 就说明这项有非默认设置 —— 任务标题旁边那行小字会把它列出来（**密钥只显示「已填」，不会把原文打出来**）。
> 💡 「恢复默认」只改面板里的值，点「保存」才写进 `config.json`。

### 🤖 AI 答题（可选）

考核题的平台题库**不公布标准答案**，程序原来只能靠「平台自己公布的答案」+ 一道保守的兜底猜测。填上 AI 接口之后，没答案的题会拿去问一次模型：

| 框 | 填什么 |
|:--|:--|
| **AI 接口地址** | 任何 **OpenAI 兼容**的 `chat/completions` 地址，例如 `https://api.deepseek.com/v1`、`http://127.0.0.1:11434/v1`（本地 Ollama）。直接粘浏览器地址栏那条也行，结尾的 `/chat/completions` 会自己补 |
| **AI 密钥** | 对应服务的 key。**本地模型留空也能用**。这一栏在界面上是打点显示的 |
| **AI 模型名** | 例如 `deepseek-chat`、`qwen2.5:14b` |

- **不填就不启用**，行为和以前完全一样。
- 平台公布的官方答案**永远优先**；AI 答的叫「猜」，会单独存在 `data/exam_answers.json` 的 `ai` 段里，和 `questions` 段分开放 —— 免得把猜的当成官方答案。
- AI 答过的题**下次不再问**（缓存键是归一化后的题干），所以是「一轮比一轮便宜」。
- 模型回「选 A」「答案是 **B**」「（C）」这类都认，但**只在开头取字母** —— 「选 A。因为 B 选项描述的是…」拿到的是 `A` 而不是 `AB`。
- 选错一次花的是你自己的钱，所以界面上跑完会报「AI 这一轮一共问了 N 道题，M 道解析出了选项字母」。

### 🔔 跑完会通知你

长时间任务不用守着屏幕：

| 什么时候 | 通知 |
|:--|:--|
| 刷学习时长**到点**（视频会自动暂停） | 「继续教育助手 · 时长刷够了」 |
| 刷时长**多门课全跑完** | 「继续教育助手 · 刷时长跑完了」 |
| 界面里**这一轮任务全部做完** | 「继续教育助手 · 全部任务完成」 |

> ⚠️ **通知发不出来的第一嫌疑是系统开关。** 如果 Windows 设置 › 系统 › 通知里的总开关是关的，系统会把横幅**静默丢掉** —— `Show()` 照样返回成功，屏幕上什么都没有。程序会先读注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\PushNotifications\ToastEnabled`，发现是 `0` 就自动改用**弹窗**（独立进程弹的，程序自己退出也不影响它留在屏幕上）。
> 💡 想自己验一下：`ZSCMEAutopilot.exe --run notify "标题" "正文"`；只看开关加 `--status`。

### 🚀 GPU 加速推理

OCR 默认就走显卡，**不用配**。实测（同一张 1280×720 截图，`tasker.post_recognition` 跑 `JRecognitionType.OCR`）：

| 算力 | 一张图耗时 |
|:--|--:|
| 只用 CPU | 352 毫秒 |
| **自动（默认，走 DirectML 显卡）** | **107 毫秒** |
| 强制显卡 | 107 毫秒 |

**快 3.3 倍。** 走的是 MaaFramework 的 DirectML 推理后端（`onnxruntime_maa.dll` 里编了 `DmlExecutionProvider`，旁边就是 `DirectML.dll`），不要求装 CUDA，A 卡 / N 卡 / 核显都能用。

> ⚠️ **「显卡序号」留空就行。** 那个序号是 Windows 显示适配器的序号，而这台机器上**序号 0 是 MuMu / GameViewer 的虚拟显示器适配器** —— 实测一张图要 **1409 毫秒，比只用 CPU 还慢 4 倍**。「自动」会自己挑中真显卡（序号 1）。真要手动指定，**从 1 开始试**。

> 💡 推理设备**必须在模型载入之前**设置（框架原话 *"Please set this option before loading the model."*），所以界面改完要**重跑任务**才生效，跑到一半改没用。

**任务开关会随路线重建** —— 两条路线的任务不是同一批：

| 路线 | 任务 | 默认 |
|:--|:--|:--|
| 🌐 浏览器版 | 自动看课（`desktop_watch`） | ✅ 勾上 |
| | 刷学习时长（`desktop_farm`） | ⬜ 留空 |
| | 考核 + 问卷（`desktop_exam`） | ✅ 勾上 |
| | 申请结课（`desktop_exam --finish`） | ⬜ 留空 |
| 📱 微信版 | 整门课轮播 / 每日签到 / 进入考核并答题 / 只看护当前视频 | 前两个勾上 |

> ⏱️ **「刷学习时长」默认不勾** —— 它会一直循环播放，最长可能跑十几个小时（每门课最多刷 8 小时是安全阀）。要不要刷、刷几门，你自己定。
> ⚠️「申请结课」默认**不勾**：结了课就归档发证书，之后刷不了分了，留给你自己决定。这一步也**未充分验证** —— 接口回执是成功的，但没等平台刷新完再复核过。

「开始运行」和「立即停止」是同一个按钮同一个位置 —— 空闲时是蓝色「▶ 开始运行」，运行中变成红色「■ 立即停止」。点下去**当场停手**：浏览器版所有十几秒的等待（看课一轮、刷时长一轮、等登录、进课的 settle）都切成 0.2 秒的小片、每片问一次「要停吗」，所以**零点几秒**就收手，顺手还会把浏览器里的视频按停（不按的话「停止」只意味着程序不再看护，画面会一直在那儿自己播）；微信版走 `tasker.post_stop()` + 看护循环自己查 `tasker.stopping`，正在看的那一节也会当场停手。被叫停**不算失败**，日志里会写明「已停止」，下次跑从没看完的接着来。

界面上的日志面板就是脚本的 stdout（`desktop_watch` / `desktop_exam` 原来一律 `print`，从界面跑的时候会写进这里）。

---

## 🚀 命令行（跟界面等价）

exe 内置了通用入口，不用装 Python 就能跑任意脚本：

```powershell
ZSCMEAutopilot.exe --list                      # 看有哪些脚本
ZSCMEAutopilot.exe --run desktop_watch         # 浏览器版看课
ZSCMEAutopilot.exe --run desktop_exam          # 浏览器版考核 + 问卷
ZSCMEAutopilot.exe --run browser --login       # 拉起浏览器并打开登录页
ZSCMEAutopilot.exe --selftest                  # 打包自检
ZSCMEAutopilot.exe --classic                   # 用旧的经典界面
```

### 第 0 步 · 部署

把整个 `ZSCMEAutopilot` 文件夹拷到目标机器。不能只拷 exe —— `_internal/` 里是 Python 运行时、MaaFramework 原生库、OCR 模型和自动化管线。

```
ZSCMEAutopilot/
├── 🖱️ ZSCMEAutopilot.exe      ← 双击这个
├── 📁 _internal/            ← 必须一起拷
├── 📁 data/                 ← 首次运行自动生成（题库、配置）
└── 📁 debug/                ← 首次运行自动生成（日志、截图）
```

### 第 1 步 · 让浏览器登录一次

浏览器版只要登录一次，之后登录态一直在。

```powershell
ZSCMEAutopilot.exe --run browser --login                  # 拉起浏览器并打开登录页
ZSCMEAutopilot.exe --run browser --buttons                # 只列出页面上扫到的按钮，不填不点
ZSCMEAutopilot.exe --run browser --enter-code 123456      # 把 6 位短信码填进去并点「立即登录」
ZSCMEAutopilot.exe --run browser --info                   # 只看当前页面现状，什么都不动
```

如果在 `data/config.json` 里填了 `browser.phone`，拉起时会自动把手机号填进登录页并点「获取验证码」，你只要把收到的 6 位短信码填进去。

> 💡 程序**不会**替你在登录页上点来点去 —— 短信验证码只有你能收到。它只负责把浏览器拉到登录页、必要时帮你填个手机号。

### 第 2 步 · 运行前自检（换机器必做）

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

### 第 3 步 · 看视频

```powershell
ZSCMEAutopilot.exe --run desktop_watch --list                 # 只列课程，不动手
ZSCMEAutopilot.exe --run desktop_watch --dry-run              # 只报准备做什么
ZSCMEAutopilot.exe --run desktop_watch                        # 全部课，全部讲
ZSCMEAutopilot.exe --run desktop_watch --course 肝胆          # 只跑名字含「肝胆」的课
ZSCMEAutopilot.exe --run desktop_watch --lessons 2            # 每门课最多看 2 讲
ZSCMEAutopilot.exe --run desktop_watch --max-courses 1        # 最多只看 1 门课
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

### 第 4 步 · 刷学习时长（可选）

平台除了「视频课件全部学完」，还单独统计一项**学习时长** —— 就是视频上方那行右边的数字：

```
本次学习 00分07秒     总计时长 73分11秒
                    └──────────┘ 刷的就是这个
```

```powershell
ZSCMEAutopilot.exe --run desktop_farm --dry-run                    # 只报现在多少分钟
ZSCMEAutopilot.exe --run desktop_farm                              # 刷到 90 分钟
ZSCMEAutopilot.exe --run desktop_farm --target 120                 # 刷到 120 分钟
ZSCMEAutopilot.exe --run desktop_farm --target 1.5 --unit hour     # 刷到 1.5 小时（= 90 分钟）
ZSCMEAutopilot.exe --run desktop_farm --course 三维                # 只刷名字含「三维」的课
ZSCMEAutopilot.exe --run desktop_farm --all-courses                # 每门课都刷一遍
ZSCMEAutopilot.exe --run desktop_farm --all-courses --max-courses 3
```

> ⏸️ **刷够了会自动把视频按停**，并发一条系统通知 —— 不用守着屏幕等它停（见 [跑完会通知你](#-跑完会通知你)）。`--dry-run` 只报数，不停也不通知。

一讲播完之后平台会弹：

```
该视频课件已观看完毕，是否继续学习下一课程节点？
                                    ［学习下一课节］［取消］
```

刷时长的做法是**点「取消」留在这一讲，再把视频倒回 0 秒重播**，如此循环。整件事只有三个动作：

```
读时长 → 有确认框就点「取消」 → 视频停了就倒带重播
```

实测日志：

```
[farm] 现在总计时长 37分00秒（2220 秒），目标 40分00秒，还差 3分00秒
[farm] 平台每 5 分钟才上报一次，所以进度条会一跳一跳的，不是卡住了（每 10 秒看一次）
[farm] 倒带返回 still-paused:NotAllowedError，改用点按起播
[desk] 起播第 2/3 次：✓ 播起来了（paused=False cur=1.1/4386.7 ready=4）
[farm] 被按停了 → 倒回 0 秒重播（第 1 次）
[farm] 平台记账了：总计时长 37分12秒，还差 2分48秒
[farm] ✓ 刷够了：总计时长 40分02秒（平台 40分02秒）
[farm] 一共刷了 3分02秒，重播 1 次，用时 3分06秒
```

> ✅ **1:1 精确记账**：刷进去 3 分 02 秒，墙上时间用了 3 分 06 秒。

> ⚠️ **「总计时长」是按课记的，不是账号级。** 实测同一账号在两门课上读到 **73分11秒**（基层医疗）和 **37分00秒**（三维超声心动图）。所以「刷到 90 分钟」是**每门课各自** 90 分钟 —— 想让 17 门课都到 90 分钟，得加 `--all-courses`（很慢，一门课最多 90 分钟，全刷可能要十几个小时）。不加就只刷第一门。

> 💡 **不用往下讲走。** 时长是按课记的，跟哪一讲无关，所以只循环当前这一讲。程序会顺手挑「这门课里还没学完的第一讲」—— 时长刷够了，那几讲也跟着学完了。

> ⏳ **进度会一跳一跳的。** 平台自己的客户端是**在浏览器里攒够 300 秒才上报一次**（`CourseLearnTimeService.js` 的 `defaultIntervalTime = 300`），所以别看到数字不动就以为卡住了。

### 第 5 步 · 考核 + 问卷

```powershell
ZSCMEAutopilot.exe --run desktop_exam --list                  # 只列出考核和问卷，不动手
ZSCMEAutopilot.exe --run desktop_exam --harvest-only          # ⭐ 开跑前先收一遍答案，最划算
ZSCMEAutopilot.exe --run desktop_exam                         # 考核 + 问卷，全部课
ZSCMEAutopilot.exe --run desktop_exam --course 肝胆 --max-courses 1
ZSCMEAutopilot.exe --run desktop_exam --exam-only             # 只做考核
ZSCMEAutopilot.exe --run desktop_exam --questionnaire-only    # 只补问卷
ZSCMEAutopilot.exe --run desktop_exam --all                   # 每门课都点进去看一眼（默认跳过已完成的）

# 挂上 AI 答题（OpenAI 兼容接口，见「AI 答题（可选）」那一节）
ZSCMEAutopilot.exe --run desktop_exam --ai-base https://api.deepseek.com/v1 --ai-key sk-xxx --ai-model deepseek-chat
ZSCMEAutopilot.exe --run ai_answer --status                   # 只检查接口配好没有，不问任何题
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

> 🔎 **已完成的课不会再点进去。** 「一共几门」和「哪几门还要做」都现问平台（课程列表接口的 `totalCount` 和 `userClassScoreDesc`），不看本地记录。平台说一门课的视频 / 考核 / 问卷都齐了就直接跳过它 —— 以前每门课都要白等 22 秒进课程页读一次考核清单，17 门课就是十几分钟。日志里会写清楚：
>
> ```
> [exam] 视频 / 考核 / 问卷平台都记着完成了 —— 这门课不用再点进去
> [exam] 平台一共 17 门课（平台接口的 totalCount），这次读到 17 门、其中 2 门有事要做、15 门已经全部完成（没点进去）
> ```
>
> 想让每门课都进去看一眼（比如怀疑平台的状态不对），加 `--all`。

### 第 6 步 · 申请结课

三件事都齐了之后：

```powershell
ZSCMEAutopilot.exe --run desktop_exam --finish
```

> ⚠️ 这一步默认不做，要显式加 `--finish`（图形界面上是「申请结课」那个开关，同样默认不勾）。结课会把课程归档发证书，之后就刷不了分了 —— 留给用户自己决定。
>
> 🧪 **未充分验证**：结课接口调通了、也拿到了成功回执，但没有从头到尾等平台把状态刷新成「已结课」再复核过。做完记得自己去「课程证书」页确认一眼。

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

> ⏱️ **注意「学习时长」不在这四道关里。** 它是平台另外一项统计（视频上方那个「总计时长」），结课条件里没写它 —— 但有些单位会拿它做考核，所以本项目单独提供了 `desktop_farm` 去刷（见 [第 4 步](#第-4-步--刷学习时长可选)）。

> ⚠️ 别拿页面上的「考核85分，已完成问卷调查」当门禁。那是服务端算好的快照、会滞后 —— 实测刚交完问卷，那几门仍写着「未完成问卷调查」，而问卷接口那边 `isSubmit: true` 已经明明白白。**要不要动手一律看接口。**
>
> 反过来用是安全的，而且程序现在就是这么用的：那句话说「三件事都齐了」时，就**不再点进这门课**。滞后只会让它少报"做完了"，于是多做一次活儿，**不会误跳过**。这一条省掉的是每门课 22 秒的进课等待 —— 账号里 17 门课全学完时，以前会白跑十几分钟。

---

## ⚙️ 配置

首次运行自动生成 `data/config.json`。大多数情况下一个字都不用改 —— adb 路径和模拟器地址默认留空就是全自动探测。

界面上每个任务右边的 **⚙** 和底部的 **⚙ 全局设置**改的就是这个文件；反过来，直接编辑文件也生效（界面读的是同一份）。**任务自己的设置在 `options` 节下按任务分开**，互不干扰：

| 键 | 默认 | 说明 |
|:--|:--|:--|
| `adb.adb_path` | 空 | 留空 = 自动探测。只在探测失败时填完整路径（用双反斜杠） |
| `adb.address` | 空 | 留空 = 自动探测。填了优先用。端口从 `MuMuManager info -v all` 的 `adb_port` 读 |
| `browser.phone` | 空 | 手机号。填了就能一键填号并点「获取验证码」 |
| `browser.debug_port` | `9222` | 设备浏览器的调试端口转发到电脑上的哪个端口，一般不用改 |
| `inference.mode` | `auto` | OCR 算力：`auto`（推荐）/ `gpu` / `cpu`。见 [GPU 加速推理](#-gpu-加速推理) |
| `inference.gpu_id` | 空 | 留空 = 框架自己挑显卡。**别随手填 0**（这台机器上 0 是虚拟显示器适配器） |
| `options.d_watch.*` | | 看课：`switch_course`（自动切换课程，默认 `false` = 只看一门）/ `course` / `lessons` / `max_courses` |
| `options.d_farm.*` | | 刷时长：`target`（可为小数）/ `unit`（`minute` 或 `hour`）/ `all_courses` / `max_hours` / `course` |
| `options.d_exam.*` | | 考核：`course` / `only`（`both`/`exam`/`questionnaire`）/ `all` / `ai_base` / `ai_key` / `ai_model` / `max_courses` |
| `screenshot.target_long_side` | `1280` | 长边归一。1080×1920 归一后是 720×1280，与框架 720p 基线一致 |

> 🔐 **`options.d_exam.ai_key` 是明文存在 `data/config.json` 里的。** 界面上那一栏打点显示、任务标题旁边那行小字也只写「已填」，但那只是**不主动露出来**，不是加密。别把 `config.json` 连密钥一起发给别人。
> ℹ️ `options.d_finish.*` 已经没有了 —— 申请结课没有可调项，界面上那个 ⚙ 也删了。旧配置里可能还留着 `d_finish` 一节，程序**会忽略它**（`to_argv` 对空声明返回空表）。

自动探测覆盖这些情况：adb 找「与正在运行的实例版本匹配」的引擎自带 adb → Android SDK → PATH → 注册表 → 运行中的进程 → 目录扫描；地址向 `MuMuManager` 查所有实例的 `adb_port`（多开也覆盖）。每一步都会真的验证（跑得起来 `adb version` / 连得上 / 是真安卓设备），验证通过才采用 —— 不假设安装目录，不靠猜。

---

## 📂 文件都在哪

| 路径 | 内容 |
|:--|:--|
| `data/config.json` | 生效的配置 |
| `data/exam_answers.json` | 🌐 浏览器版题库缓存（按题干索引，存正确选项的文字） |
| `data/answer_cache.json` | 📱 微信版题库缓存 |
| `data/course_progress.json` | 📱 微信版看课进度 |
| `debug/` | 运行日志、截图、临时探针（整个目录不进仓库） |

> 💡 首次启动会自动接管旧数据：exe 会往上找上层目录里的 `data/`，题库为空时自动搬过来并在日志里打印搬运记录。只在题库为空时搬，绝不覆盖已有的。

---

## 🩺 排查问题

| 现象 | 原因与处理 |
|:--|:--|
| 自检第 3 项 ✗ 找不到 adb | 装 Android SDK platform-tools 并加进 PATH；或在 `data/config.json` 的 `adb.adb_path` 写死 |
| 自检第 4 项 ✗ 连接失败 | ① 模拟器是否已启动进入桌面；② `MuMuManager.exe info -v 0` 看 `adb_port`；③ 端口被占 |
| `--run browser --info` 说没登录 | 登录态丢了。跑 `--run browser --login` 登一次（别加 `--restart`） |
| 程序说「还是没登录」并退出码 `4` | 浏览器被安卓后台杀过，会话 cookie 没了。程序已经把登录页开好了，在那上面登一次再重跑。**别加 `--restart`** —— 重启浏览器只会再丢一次登录态 |
| 程序说「调试端口问不到标签页」 | `adb forward` 的映射还在、浏览器已经没了。程序会自己把浏览器拉起来重连，看到这行不用管；连着出现才需要手动开一次浏览器 |
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
| 📱 微信版有历史问题 | 「进入答题」的真机验证、微信解码器崩溃后的自愈，都还差一次完整的成功记录 |
| 🖼️ 截图会超时 | `Page.captureScreenshot` 在这套环境上会 `TimeoutError`，所以浏览器版一律不依赖截图 |
| 🧪 3 项自测过不了 | 它们要模拟器里装着微信。换一台装着微信的机器就是全绿 |

---

## 📱 附：微信版（旧路线，不推荐）

> ⚠️ 这一段留着是因为那条路确实跑通过，但**默认不再用它**：模拟器里跑微信的风控面更大，而且这台模拟器的微信解码器会反复崩。新装环境直接走浏览器版就行。

界面左侧把路线切成「微信版（有风险）」就会跑这一套；命令行等价物是：

```powershell
ZSCMEAutopilot.exe --run run_exam_watch                    # 看护一门课（全部课节）
ZSCMEAutopilot.exe --run run_exam_watch --forget           # 忘掉本地进度，重看
ZSCMEAutopilot.exe --run run_exam_watch --max-lessons 2    # 最多看 2 节
ZSCMEAutopilot.exe --run run_full_exam                     # 跑完整卷并交卷
ZSCMEAutopilot.exe --run harvest_answers                   # 从结果页采集官方答案
ZSCMEAutopilot.exe --run pending list                      # 看待答题
```

相关脚本：`scripts/run_exam_watch.py`、`scripts/course.py`、`scripts/exam.py`、`scripts/harvest_answers.py`、`scripts/retake_exam.py`。

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

**它踩过的两个环境坑**（换环境时值得先查一眼）：

- **微信解码器 SIGSEGV** —— `MediaCodec_loop` 线程在 `libstagefright.so` 的 `MediaCodec::setState` 上崩，实测一天 17 次，且只有微信崩、别的进程一次都没有。模拟器是 `x86_64`、`debug.stagefright.ccodec=0`、无 root、MuMu 侧没有可调的解码开关 → **环境侧无解**，只能让程序扛（`scripts/app_recover.py` 崩了就把微信拉起来、回到列表、从没看完的那节接着看）。
- **屏幕会自己在横竖之间切** —— 每次动手前必须现查现锁（`app_recover.canvas_portrait()` 直接读 `screencap` 的 PNG 头，不解码整图），转不回来就停手，否则截图坐标和所有点击坐标都会错位。

---

## 🔧 从源码构建

想让程序跑起来，最省事的是[直接下打包版](#-先拿到程序)。要从源码跑（改代码、调试、自己打包）才需要往下看。

### 依赖

| 依赖 | 版本 | 怎么来 |
|:--|:--|:--|
| **MaaFramework** | **5.14.2** | ⭐ **核心依赖**。Python 绑定一条命令就够：`pip install MaaFw==5.14.2`（这个包把框架的原生 DLL 一起带下来，打包时会从 `maa/bin/` 收进去） |
| Python | 3.12（64 位） | [python.org](https://www.python.org/downloads/windows/) |
| 其余 Python 包 | — | `pip install customtkinter numpy pillow` |

> 💡 **清单的权威版本是 [`build.spec`](build.spec)**（`datas` / `hiddenimports` / `excludes` 三块），上表只是方便你一眼看全。它连 `cv2` 都显式排掉了 —— MaaFramework 自己已经链了 `opencv_world4_maa.dll`，再带一份 `opencv-python` 纯属重复（实测 112 MB）。

### 步骤

```powershell
git clone https://github.com/6nh2nkdznz-dot/zs-cme-autopilot.git
cd zs-cme-autopilot
pip install MaaFw==5.14.2 customtkinter numpy pillow

python launcher.py            # 开图形界面
python launcher.py --selftest # 或者只跑自检
```

打包成 exe：

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` 会：停掉正在跑的 `ZSCMEAutopilot` 进程 → 按 [`build.spec`](build.spec) 跑 PyInstaller 打成 **onedir** → 把 `dist\ZSCMEAutopilot\*` 整目录拷回项目根 → 逐文件 MD5 比对 `scripts\{core,course,main,exam,progress,debug_view,browser,app_recover,run_exam_watch,desktop}.py` 与 `_internal\scripts\*.py`，确认打进去的和源码一致（`launcher.py` / `launcher_ui.py` 是编进 exe 的，磁盘上没有对应的散装文件，所以不在这张比对表里）。

> ⚠️ **`build.ps1` 的退出码会是 1，那是假的** —— PyInstaller 的进度日志走 stderr，PowerShell 把 `$LASTEXITCODE` 弄成了 1。**认产物那一行 `[build] done: <字节数>`，以及后面的 MD5 比对结果。**

> ⚠️ `build.ps1` 必须保持**纯 ASCII**：Windows PowerShell 5.1 会把无 BOM 的 UTF-8 当 ANSI 读，脚本里一旦有中文就会拆坏引号、整个脚本没法解析。

### 跑测试

没有 pytest，每个测试文件都是可直接运行的脚本（有自己的 `check()`）：

```powershell
$env:PYTHONIOENCODING='utf-8'          # 不设会 UnicodeEncodeError
Get-ChildItem scripts\test_*.py | ForEach-Object { python $_.FullName }
```

每个文件结尾会打一行 `结果: N 通过 / M 失败`。

---

## 📜 许可与第三方组件

本项目自身以 [MIT 许可](LICENSE) 发布。仓库里有两样东西不是我们写的：

| 内容 | 来源 | 许可 | 说明 |
|:--|:--|:--|:--|
| `assets/resource/model/ocr/` 下的 `det.onnx` / `rec.onnx` / `keys.txt` | [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) 转 ONNX | Apache-2.0 | 文字识别模型。保留原作者署名，不适用本项目的 MIT 许可 |
| [MaaFramework](https://github.com/MaaXYZ/MaaFramework) | MaaXYZ | LGPL-3.0 | 自动化框架本体 |

关于 MaaFramework 有几点要说清楚：

1. 它的**源码不在本仓库里**（`vendor/` 在 `.gitignore` 中），自己构建要 `pip install MaaFw==5.14.2`。
2. 本项目只是调用它公开的接口 —— 包括继承 `CustomAction` / `CustomRecognition` 来写自己的动作。LGPL-3.0 第 0 节明写「继承库中定义的类属于使用接口」，因此本项目是 LGPL 定义的 Application，不受其传染，可以自行选择许可。
3. **但 Release 里的打包版不一样**：`_internal/maa/bin/` 装着 MaaFramework 的原生 DLL，**分发打包版就等于在分发它**，LGPL-3.0 的告知义务落到分发者头上。所以包里带了 [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)（逐项列出包内所有第三方组件、版本、许可与位置），以及 [`LICENSES/LGPL-3.0.txt`](LICENSES/LGPL-3.0.txt) 与 [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt)（LGPL-3.0 和它引用的 GPL-3.0 全文）。
4. LGPL 要求使用者能替换掉那个库。本项目**没有把它静态链接进 exe** —— DLL 是原封不动放在 `_internal/maa/bin/` 里的独立文件，拿一份自行编译的 MaaFramework 覆盖同名文件即可，不需要重新编译本项目。

---

## ⚖️ 免责声明

平台账号为实名账号，连续挂机可能触发风控；用模拟器刷继续教育学时在服务条款层面是不被允许的。请自行评估风险，本项目仅供技术研究。

---

## 📚 延伸阅读

| 文档 | 内容 |
|:--|:--|
| [`DEVELOPMENT.md`](DEVELOPMENT.md) | 源码构建、管线结构、坐标表、接口契约、调试工具 |
| [`STATUS.md`](STATUS.md) | 开发过程复盘（历史快照）—— 几轮调试里踩过的坑与故障分析 |
| [`docs/DPI-感知踩坑记录.md`](docs/DPI-感知踩坑记录.md) | 界面在高 DPI 屏上偏小 / 右栏被挤没的排查留档（含实测数字） |
| [`LICENSE`](LICENSE) | MIT 许可 |
| [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md) | 打包版里每个第三方组件的来源、版本与许可 |
