# 第三方组件声明

本项目的**源代码**以 [MIT 许可](LICENSE) 发布。

但**打包好的程序**（Release 里的 `ZSCMEAutopilot-*.zip`，以及你自己 `build.ps1` 打出来的
`ZSCMEAutopilot.exe` + `_internal/`）里含有别人写的组件。分发这些组件时，各自的许可条款随之生效，
本文件就是履行告知义务用的。

> 只在本地自己跑、不对外分发的话，这份文件跟你没多大关系；一旦要把 zip 传给别人或者传上网，请连同它一起。

---

## 一、MaaFramework —— LGPL-3.0

| | |
|:--|:--|
| 项目 | [MaaXYZ/MaaFramework](https://github.com/MaaXYZ/MaaFramework) |
| 版本 | 5.14.2（Python 包名 `MaaFw`） |
| 许可 | GNU Lesser General Public License v3.0 |
| 许可全文 | [`LICENSES/LGPL-3.0.txt`](LICENSES/LGPL-3.0.txt)，另需一并提供 [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt)（LGPL-3.0 是 GPL-3.0 的补充条款，正文里明写「incorporates the terms and conditions of version 3 of the GNU General Public License」，所以两份都要给） |
| 在包里的位置 | `_internal/maa/bin/` 下的 `MaaFramework.dll`、`MaaAdbControlUnit.dll`、`MaaWin32ControlUnit.dll`、`MaaToolkit.dll`、`MaaUtils.dll` 等 |
| 我们的用法 | 只调用它公开的接口，包括继承 `CustomAction` / `CustomRecognition` 写自己的动作 |

**关于「不受传染」的说明**：LGPL-3.0 第 0 节原文写着
`Defining a subclass of a class defined by the Library is deemed a mode of using an interface provided by the Library`
（继承库中定义的类属于「使用库提供的接口」）。所以本项目是 LGPL 定义的 **Application**，
不受 LGPL 传染，可以自行选择 MIT 许可。

**关于「可替换」的说明**：LGPL-3.0 要求使用者能够替换掉这个库。本项目的做法是
**不把它静态链接进 exe** —— 那些 DLL 是原封不动地放在 `_internal/maa/bin/` 目录里的独立文件。
你只要拿一份自行编译的 MaaFramework，把同名 DLL 覆盖进去，程序就会用你那份。
`MaaFramework.dll` 与其余 `Maa*.dll` 之间是动态加载关系，替换不需要重新编译本项目。

**源码获取**：MaaFramework 的源码不在本仓库里，请到
<https://github.com/MaaXYZ/MaaFramework> 按对应版本号（5.14.2）获取。

---

## 二、PaddleOCR 的 OCR 模型 —— Apache-2.0

| | |
|:--|:--|
| 项目 | [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) |
| 许可 | Apache License 2.0 |
| 在包里的位置 | `assets/resource/model/ocr/` 与 `_internal/assets/resource/model/ocr/` 下的 `det.onnx`（检测）、`rec.onnx`（识别）、`keys.txt`（字典） |
| 来源说明 | 这三份是 PaddleOCR 官方预训练模型转成 ONNX 后的产物 —— MaaFramework 官方文档《1.1-快速开始》第 179 行原文：「`my_resource/model/ocr` 中的文件，为 PaddleOCR 转 ONNX 后的模型文件」 |
| 改动 | 未做 fine-tuning，仅由 MaaFramework 官方提供的转换流程转成 ONNX |

**不适用本项目的 MIT 许可**，署名归 PaddlePaddle 所有。

---

## 三、随 Python 运行时一起打进来的库

这些是 PyInstaller 打进 `_internal/` 的依赖，各自保留原许可：

| 组件 | 许可 | 在包里的位置 |
|:--|:--|:--|
| Python 3.12 运行时与标准库 | PSF License 2.0 | `base_library.zip`、`_internal/` 下的 `python3*.dll` 等 |
| Tcl / Tk | BSD 风格（Tcl/Tk License） | `_internal/tcl8/`、`_tcl_data/`、`_tk_data/` |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) | MIT | `_internal/maa/bin/onnxruntime_maa.dll` |
| [OpenCV](https://opencv.org/) 4.x | Apache-2.0 | `_internal/maa/bin/opencv_world4_maa.dll` |
| [NumPy](https://numpy.org/) 2.5.3 | BSD-3-Clause | `_internal/numpy/` |
| [Pillow](https://python-pillow.org/) | MIT-CMU | `_internal/PIL/` |
| [customtkinter](https://github.com/TomSchimansky/CustomTkinter) | MIT | `_internal/customtkinter/` |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 | `_internal/certifi/` |
| [charset-normalizer](https://github.com/Ousret/charset_normalizer) | MIT | `_internal/charset_normalizer/` |
| OpenSSL 3（libcrypto / libssl） | Apache-2.0 | `_internal/libcrypto-3.dll`、`libssl-3.dll` |
| libffi | MIT | `_internal/libffi-8.dll` |
| MSVC 运行库（CONCRT140） | Microsoft 可再分发运行库条款 | `_internal/CONCRT140.dll` |

各组件完整许可全文随其上游发行版提供，请到上表链接处获取。

---

## 四、我们自己的部分

`scripts/`、`launcher.py`、`launcher_ui.py`、`assets/resource/pipeline/` 下的管线 JSON、
`build.ps1` / `build.spec`、以及全部文档 —— 这些是本项目写的，**MIT 许可**，见 [`LICENSE`](LICENSE)。

`data/exam_answers.json` 与 `data/answer_cache.json` 是程序运行中收集整理的答题记录，同样是 MIT。
