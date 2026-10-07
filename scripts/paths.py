"""路径解析：同时支持「源码运行」与「PyInstaller 打包后运行」。

打包后有两套不同的根目录，混用会导致找不到文件：

* **只读资源**（pipeline / OCR 模型 / 默认配置）
  由 PyInstaller 解压到 `sys._MEIPASS`（onefile 时是临时目录）。
  绝对不能往这里写东西——onefile 退出即删。

* **用户数据**（config / 题库缓存 / 日志 / 截图 / 待人工确认的题目）
  放在 exe 同级的 `data/`，这样重打包、换机器都不丢。

源码运行时两者都落在项目根目录，行为一致。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物里。"""
    return getattr(sys, "frozen", False)


def bundle_root() -> Path:
    """只读资源的根。打包后是 _MEIPASS，源码运行时是项目根。"""
    if is_frozen():
        # PyInstaller 在 onefile / onedir 下都会设置 _MEIPASS
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """用户数据的根。打包后是 exe 所在目录，源码运行时是项目根。

    可用环境变量 `MAA_ELEARNING_DATA` 覆盖——测试时把数据/调试产物
    导到临时目录，避免污染真实题库与截图。
    """
    override = os.environ.get("MAA_ELEARNING_DATA")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p

    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# --- 只读资源 ---

def resource_dir() -> Path:
    """assets/resource（pipeline + image + model）。"""
    return bundle_root() / "assets" / "resource"


def pipeline_dir() -> Path:
    return resource_dir() / "pipeline"


def ocr_model_dir() -> Path:
    return resource_dir() / "model" / "ocr"


def default_config_path() -> Path:
    """随包发布的默认配置（只读模板）。"""
    return bundle_root() / "config" / "config.json"


def agent_binary_dir() -> Path:
    """MaaAgentBinary 目录。

    两种存在方式：
    * 打包后：spec 把它放到 bundle 根下；
    * 源码运行：随 MaaFw 的 wheel 装在 site-packages 根目录，
      路径是 <site-packages>/MaaAgentBinary（maa 包的上一级）。

    找不到时返回一个不存在的路径，调用方据此跳过 agent_path。
    """
    bundled = bundle_root() / "MaaAgentBinary"
    if bundled.is_dir():
        return bundled

    # 源码运行：借 maa 包自己的默认值（它算的就是 <site-packages>/MaaAgentBinary）
    try:
        from maa.controller import AdbController

        fallback = Path(AdbController.AGENT_BINARY_PATH)
        if fallback.is_dir():
            return fallback
    except Exception:
        pass

    return bundled


# --- 用户数据（可写） ---

def data_dir() -> Path:
    d = app_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    """实际生效的配置：exe 同级的 data/config.json。

    首次运行会从只读默认配置复制一份出来，之后用户改的都是这份。
    """
    target = data_dir() / "config.json"
    if not target.is_file():
        src = default_config_path()
        if src.is_file():
            import shutil

            shutil.copyfile(src, target)
    return target


def answer_cache_path() -> Path:
    return data_dir() / "answer_cache.json"


def debug_dir() -> Path:
    d = app_root() / "debug"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_dir() -> Path:
    d = debug_dir() / "log"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snap_dir() -> Path:
    d = debug_dir() / "snap"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending_dir() -> Path:
    d = debug_dir() / "pending"
    d.mkdir(parents=True, exist_ok=True)
    return d


def describe() -> str:
    """给启动器显示用的路径摘要。"""
    return (
        f"运行模式: {'打包 (frozen)' if is_frozen() else '源码'}\n"
        f"资源根  : {bundle_root()}\n"
        f"数据根  : {app_root()}\n"
        f"配置    : {config_path()}\n"
        f"资源包  : {resource_dir()}"
    )
