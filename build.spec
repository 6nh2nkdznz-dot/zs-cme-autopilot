# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

要点（这些都是踩过才知道的）：

1. **maa 的原生 DLL 必须显式收集**。它们在 site-packages/maa/bin/，
   PyInstaller 的依赖分析看不到（不是通过 import 加载的），
   漏了会在运行时抛 DLL load failed。

2. **MaaAgentBinary 必须放到位**。MaaFramework 的 ADB 控制器要从里面取
   maatouch / minicap / minitouch 来推送输入与截图组件。少了它连接会失败。

3. **OCR 模型随包发布**（约 21MB）。放在 assets/resource/model/ocr，
   与 paths.py 的 bundle_root() 约定一致。

4. **用户数据不打包**。config.json 作为只读模板放进去，首次运行由
   paths.config_path() 复制到 exe 同级的 data/。这样重打包不覆盖用户配置。

5. 用 onedir 而不是 onefile。onefile 每次启动都要把 ~60MB 原生库解压到临时
   目录，启动慢好几秒，而且临时目录被杀软盯上的概率更高。onedir 直接跑。

构建：
    python -m PyInstaller build.spec --noconfirm --clean
产物：
    dist/MaaElearning/MaaElearning.exe
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPECPATH 由 PyInstaller 注入，指向本文件所在目录
ROOT = SPECPATH  # noqa: F821

# --------------------------------------------------------------------------
# 资源
# --------------------------------------------------------------------------

datas = [
    # 管线 + 图像素材 + OCR 模型
    (os.path.join(ROOT, "assets", "resource"), "assets/resource"),
    # 默认配置（只读模板，首次运行复制到 data/）
    (os.path.join(ROOT, "config"), "config"),
    # 内置脚本：launcher 会 import 它们，同时也是给用户看的源码
    (os.path.join(ROOT, "scripts"), "scripts"),
]

# MaaAgentBinary：ADB 控制器的输入/截图组件
_agent = None
try:
    from maa.controller import AdbController

    _agent = AdbController.AGENT_BINARY_PATH
except Exception:
    pass

if _agent and os.path.isdir(_agent):
    datas.append((_agent, "MaaAgentBinary"))
else:
    raise SystemExit(
        "[build] 找不到 MaaAgentBinary，ADB 控制器跑不起来。\n"
        "        确认已安装 MaaFw: pip install MaaFw"
    )

# maa 自己的原生 DLL（MaaFramework.dll / onnxruntime_maa.dll / opencv_world4_maa.dll 等）
datas += collect_data_files("maa", includes=["bin/**", "bin/*"])

# --------------------------------------------------------------------------
# 隐藏导入
# --------------------------------------------------------------------------

hiddenimports = [
    "maa",
    "maa.agent",
    "maa.context",
    "maa.controller",
    "maa.custom_action",
    "maa.custom_recognition",
    "maa.pipeline",
    "maa.resource",
    "maa.tasker",
    "maa.toolkit",
    "maa.define",
    "maa.job",
    "maa.buffer",
    "maa.event_sink",
    "maa.library",
    # 本项目模块
    "paths",
    "detect",
    "controller",
    "progress",
    "course",
    "exam",
    "sync_result_page_rule",
    "ocr_text",
    "inspect_schema",
    "score",
    "quiz",
    "pending",
    "main",
    "snap",
    "imageio_util",
    # 存图降级链的落点。cv2 被 excludes 排掉了，所以 Pillow 是唯一实现，
    # 必须显式声明——它是通过函数内 import 加载的，静态分析未必抓得到。
    "PIL",
    "PIL.Image",
    "PIL.ImageFile",
    "PIL.PngImagePlugin",
    "PIL.JpegImagePlugin",
    "PIL._imaging",

    "calibrate_ocr_coords",
    "capture",
    "check_progress",
    "fill_exam",
    "harvest_answers",
    "migrate_answer_texts",
    "retake_exam",
    "run_deadlock_probe",
    "run_exam",
    "run_full_exam",
    "tap",
    "test_course",
    "test_harvest",
    "test_ocr_in_callback",
    "test_ocr_text",
    "test_page_detect",
    "test_quiz",
    "test_score",
    "validate_pipeline",
    "customtkinter",
    "darkdetect",
    "core",
    "course_progress",
    "run_exam_watch",
    "test_verify",
    "verify",
    "test_adb_input",
    "merge_course_progress",
    "test_course_runner",
    "circles",
    "test_circle",
    "test_video_end",
    "debug_view",
    "app_recover",
    "test_app_recover",
    "test_recognition",
    "screen_orient",
    "display_mode",
    "test_page_guard",]

hiddenimports += collect_submodules("maa")
hiddenimports += collect_submodules("PIL")

# --------------------------------------------------------------------------
# 排除：明显用不到的大件，能显著减小体积
# --------------------------------------------------------------------------

excludes = [
    # cv2 实测占 112MB。MaaFramework 自己已经链接了 opencv_world4_maa.dll，
    # 我们再带一份 opencv-python 纯属重复。项目里只用它存 PNG，
    # imageio_util 会自动降级到 Pillow。
    "cv2",
    "opencv_python",
    "matplotlib", "scipy", "pandas", "IPython", "jupyter",
    "notebook", "pytest", "setuptools", "pip", "wheel",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "torch", "torchvision", "tensorflow", "sklearn",
]


block_cipher = None

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "launcher.py")],
    pathex=[ROOT, os.path.join(ROOT, "scripts")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MaaElearning",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX 压原生 DLL 容易出玄学问题，关掉
    console=False,           # GUI 程序，不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MaaElearning",
)
