"""推理设备（CPU / 显卡）的选择。

MaaFramework 的文字识别默认走 CPU。它的 `Resource.set_inference()` 可以换成
DirectML（Windows 上走显卡）—— 实测**能快 3 倍多**，但有几个坑必须写下来，
否则「加个 GPU 开关」会变成一个让程序变慢十几倍的开关。

## 实测数据

本机：NVIDIA RTX 5070 Ti Laptop + 板载 Intel UHD + 两个**虚拟显示器适配器**
（`MuMu Virtual Display Adapter` / `GameViewer Virtual Display Adapter`）。
测试：同一张 1280x720 截图，PP-OCRv5 全图识别，各跑 8 次取平均。

| 设置                          | 平均耗时   |
|-------------------------------|-----------|
| `EP=CPU` device=CPU(-2)       | 352.6 ms  |
| `EP=CPU` device=Auto(-1)      | 351.1 ms  |
| **`EP=Auto` device=Auto**     | **107.7 ms** |
| **`EP=DirectML` device=Auto** | **106.8 ms** |
| `EP=DirectML` device=1        | 105.5 ms  |
| `EP=DirectML` device=0        | 1409.8 ms |
| `EP=DirectML` device=2        | 1525.4 ms |
| `EP=DirectML` device=3        | 1531.6 ms |

## 三条结论

1. **`Auto` 走的就是显卡。** 一开始怀疑「Auto 比强制 CPU 快，会不会只是线程数开对了」，
   于是做了对照：`EP=CPU` 无论 device 给 `Auto` 还是 `CPU`，都是 **352 ms** 上下；
   而 `EP=Auto` 和 `EP=DirectML` 都是 **107 ms**。CPU 路径里外里就是慢 3.3 倍，
   差的是执行提供者，不是线程。
2. **默认的 `use_directml()`（device 不填 = `Auto`）就是对的**，它自己会挑中
   device=1 那块真显卡。★ **绝对不要**让用户随手填适配器序号：这台机器上
   **device=0 是虚拟显示器适配器，慢 13 倍**（1409 ms，比 CPU 还慢 4 倍）。
   所以配置里的 `gpu_id` 默认留空 = 交给框架自选。
3. **必须在载模型之前设。** `maa/define.py` 里 `MaaResOptionEnum` 的注释原文是
   "Please set this option before loading the model." —— 所以本模块的 `apply()`
   要放在 `post_bundle()` / `post_ocr_model()` **之前**调用。

## 另一个坑：载入成功 ≠ 生效

`resource.post_ocr_model(...).wait().succeeded` 在 **Auto / CPU / DirectML 三种设置下
全部返回 True，而且耗时 0.00 秒** —— 模型是惰性加载的，载入那一刻根本没建会话。
所以**不能用它判断设备有没有生效**，要验证只能真跑一次识别计时
（`debug/_bench_gpu*.py` 就是干这个的）。
"""

from __future__ import annotations

from typing import Any

#: 允许的取值。写成元组是为了给界面和测试一个唯一事实来源。
MODES = ("auto", "gpu", "cpu")

#: 给人看的名字。界面的下拉框直接用它。
LABELS = {
    "auto": "自动（推荐）",
    "gpu": "显卡加速",
    "cpu": "只用 CPU",
}

#: 每个模式的说明。设置界面里那行小字用它。
HINTS = {
    "auto": "交给框架自己挑。实测它挑的就是显卡，比 CPU 快 3 倍多。",
    "gpu": "强制走 DirectML 显卡。显卡驱动异常时可以切回「自动」。",
    "cpu": "只用处理器。兼容性最好，但识别一张图约 350 毫秒（显卡约 107 毫秒）。",
}

DEFAULT_MODE = "auto"


def read(cfg: dict[str, Any] | None = None) -> tuple[str, int | None]:
    """从配置里读出 `(mode, gpu_id)`，顺手把非法值挡掉。

    配置写错（比如手写成 `"GPU"`）不应该让程序起不来，所以一律回退到默认值。
    `gpu_id` 为 `None` 表示「交给框架自选」——**这才是推荐值**，见模块开头的实测表。
    """
    if cfg is None:
        from controller import load_config

        try:
            cfg = load_config()
        except Exception:
            cfg = {}

    section = cfg.get("inference") or {}
    if not isinstance(section, dict):
        section = {}

    mode = section.get("mode")
    mode = str(mode).strip().lower() if mode is not None else DEFAULT_MODE
    if mode not in MODES:
        mode = DEFAULT_MODE

    raw_id = section.get("gpu_id")
    gpu_id: int | None = None
    if raw_id not in (None, "", -1, "-1"):
        try:
            gpu_id = int(raw_id)
        except (TypeError, ValueError):
            gpu_id = None
        if gpu_id is not None and gpu_id < 0:
            gpu_id = None

    return mode, gpu_id


def apply(resource, cfg: dict[str, Any] | None = None, *, log=None) -> str:
    """把推理设备设到 `resource` 上。**必须在 `post_bundle()` / `post_ocr_model()` 之前调。**

    返回一句给人看的描述（用于日志 / 界面回显）。**不抛异常**：显卡驱动有问题时
    最坏退回到框架默认（实测就是显卡路径），不该因为一个性能开关把整个程序弄挂。
    所以所有失败都只记一行日志。
    """
    def say(msg: str) -> None:
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass

    mode, gpu_id = read(cfg)

    if mode == "cpu":
        try:
            if resource.use_cpu():
                return "只用 CPU"
        except Exception as exc:  # 显卡/驱动异常、老版本框架没有这个方法
            say(f"[infer] 切 CPU 失败（忽略，用框架默认）: {exc}")
        return "CPU（设置失败，用框架默认）"

    # auto 与 gpu 都走 DirectML：区别只在 device_id 给不给。
    # `auto` 也显式调 directml，是因为 `use_auto_ep()` 和 `use_directml()` 实测同速，
    # 但 directml 的日志更好懂（能看出到底走没走显卡）。
    try:
        if gpu_id is None:
            ok = resource.use_directml()
            what = "显卡（DirectML，适配器由框架自选）"
        else:
            ok = resource.use_directml(gpu_id)
            what = f"显卡（DirectML，适配器 {gpu_id}）"
            if gpu_id == 0:
                say("[infer] ⚠ 适配器 0 常常是虚拟显示器（MuMu/GameViewer），"
                    "实测比 CPU 还慢 4 倍。建议把 gpu_id 留空让框架自选。")
        if ok:
            return what
        say("[infer] 这台机器上用不了显卡（框架返回 False），退回默认")
    except Exception as exc:
        say(f"[infer] 切显卡失败（忽略，用框架默认）: {exc}")

    return "框架默认"


def describe(cfg: dict[str, Any] | None = None) -> str:
    """不真正设置，只是说说「配置里写的是什么」。给界面和自检用。"""
    mode, gpu_id = read(cfg)
    label = LABELS.get(mode, mode)
    if mode != "cpu" and gpu_id is not None:
        return f"{label}（适配器 {gpu_id}）"
    return label
