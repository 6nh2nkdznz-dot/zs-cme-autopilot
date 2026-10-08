# DPI 感知踩坑记录

> 这份记录留档的是「界面在高 DPI 屏上偏小 / 右栏被挤没 / 窗口尺寸量不准」这一轮排查。
> **结论已经落进代码**，这里是把当时的实测数字和踩过的坑记下来，免得下次有人看到
> 「窗口量出来是 1635x937」又要从头查一遍。

---

## 1. 现象

同一台机器（Windows，显示缩放 **150%**）、同一份代码，只改「有没有 DPI 感知」：

| | 感知 | 不感知 |
|:--|:--|:--|
| 窗口物理尺寸 | **2430x1350**（1620x900 逻辑） | **1635x937** |
| 右栏宽度 | 1919px | —— |
| 布局 | 左半边日志 1359px ＋ 右半边调试画面 560px，**并排显示** | 右栏被挤到只剩日志，**右边那半调试画面整块看不见** |
| CTk `ScalingTracker` | 1.5 | **1.0**（真实应为 1.5） |

同时这些量出来的值也全是错的：

| 量的东西 | 不感知时 | 真实 |
|:--|:--|:--|
| 工作区尺寸 | 1707 x 1019 | 2560 x 1528 |
| `winfo_fpixels("1i")` | 96 | 144 |

1635 x 937 x 1.5 ≈ 2452 x 1405 —— 差的就是那 1.5 倍。也就是说进程拿到的是**被系统虚拟化过的逻辑坐标**。

## 2. 根因：`SetProcessDpiAwareness(1)` 写在了 `ctk.CTk()` 之后

最早 `main()` 里确实有这么一句：

```python
root = ctk.CTk()                      # ← CTk 在构造函数里就把 DPI 读完了
...
windll.shcore.SetProcessDpiAwareness(1)   # ← 设了也白设
```

**CTk 在构造函数里就把 DPI 读完了**，之后再声明感知对已经算好的缩放没有任何影响。
所以这句「看起来已经设了」的代码一直是个安慰剂，顺序错误藏了很久没被发现。

## 3. 修法：抢在建任何窗口之前

现在是**两个地方各设一次**，都是 `SetProcessDpiAwareness(1)`（`1 = SYSTEM_DPI_AWARE`）：

### `launcher.py:49-68` —— import 阶段

```python
def _enable_dpi_awareness() -> str:
    """让进程「DPI 感知」。**在 import 阶段就调用**（见文件末尾那行）。

    为什么必须在**建任何窗口之前**：Windows 一旦看到本进程创建过窗口，
    就不再接受 DPI 感知级别的变更（`SetProcessDpiAwareness` 返回
    `E_ACCESSDENIED`，而且**不抛异常**）。不感知的代价很实在：
    150% 缩放下工作区被虚拟化成 1707x1019（真实 2560x1528）、
    `winfo_fpixels("1i")` 报 96 而非 144，CTk 的 `ScalingTracker` 于是把
    窗口缩放算成 1.0 —— 字体不放大、右栏被挤到只剩日志，**并排的调试画面
    整块看不见**（实测窗口 1635x937 而不是 2430x1350）。

    返回一句人话状态，供日志显示。
    """
    try:
        from ctypes import windll

        hr = windll.shcore.SetProcessDpiAwareness(1)  # 1 = SYSTEM_DPI_AWARE
        return "已启用 DPI 感知" if hr == 0 else f"DPI 感知没设上（{hr}）"
    except Exception as exc:  # noqa: BLE001 - 非 Windows 或权限不足时忽略
        return f"DPI 感知不可用（{type(exc).__name__}）"


#: import 阶段就把 DPI 感知设好 —— 此时还不可能有任何窗口。
#: 放在这里而不是 `main()` 里，是因为 `main()` 之前已经有模块 import 了
#: customtkinter / tkinter，任何一个提前建窗口都会让设置永久失败。
DPI_STATE = _enable_dpi_awareness()
```

★ **它是模块级的 `DPI_STATE = _enable_dpi_awareness()`（`launcher.py:74`）**，不是函数调用 —— 「import 这个模块」这个动作本身就完成了设置。这样 `--selftest` / `--run` / `--list` 这些命令行入口也顺带把 DPI 设好了。

**为什么入口留在 `launcher.py`**（`launcher.py:650-654`、`launcher.py:718` 的注释）：`--selftest` / `--run` / `--list` / `--classic` 都实现在 `launcher.py` 里。实测踩过一次 —— **把入口换成 `launcher_ui` 之后 `--selftest` 直接开出了一个窗口**。所以入口留在 `launcher.py`，DPI 也就跟着在它的 import 阶段设好；`launcher_ui.main()` 里那次只是「双保险」，且它会把 `E_ACCESSDENIED` 正确地翻译成「已在更早处生效」。

### `launcher_ui.py:1685-1727` —— `main()` 第一行

```python
def _enable_dpi_awareness() -> str:
    """让进程「DPI 感知」，**越早调用越好**。
    ...
    实测代价（同一台机器、同一份代码）：
      - 感知：窗口 2430x1350 物理（1620x900 逻辑），右栏 1919px，
        **左半边日志 1359px + 右半边调试画面 560px 并排显示**；
      - 不感知：窗口 1635x937 物理，`ScalingTracker=1.0`，
        右栏被挤到只剩日志，**右边那半调试画面整块看不见**。
    ...
    返回一句人话状态，供日志显示。
    """
    try:
        from ctypes import windll

        hr = windll.shcore.SetProcessDpiAwareness(1)  # 1 = SYSTEM_DPI_AWARE
        if hr == 0:
            return "已启用 DPI 感知"
        # E_ACCESSDENIED：**通常不是问题** —— `launcher.py` 在 import 阶段
        # 已经设过一次，那一次成功就够了。这里判「有没有设上」而不是
        # 只看 HRESULT，免得把已经正常的状态报成故障。
        if _dpi_now() >= 120:
            return f"DPI 感知已在更早处生效（{_dpi_now()} dpi）"
        return f"DPI 感知没设上（HRESULT={hr}），界面会偏小"
    except Exception as exc:  # noqa: BLE001 - 非 Windows 或权限不足时忽略
        return f"DPI 感知不可用（{type(exc).__name__}）"
```

配套的 `_dpi_now()`（`launcher_ui.py:1730-1737`）读 `windll.user32.GetDpiForSystem()`，**96 = 100%、144 = 150%**，探测失败返回 `0`。

`main()`（`launcher_ui.py:1740-1753`）第一行就是它，并把结果打进日志：

```python
def main() -> int:
    # **顺序要紧**：DPI 感知要在建窗口之前。见 `_enable_dpi_awareness`。
    dpi_state = _enable_dpi_awareness()
    ctk.set_appearance_mode("dark")
    ...
    root = ctk.CTk()
    app = App(root)
    app.logger(f"[ui] {dpi_state}（系统 DPI {_dpi_now()}，"
               f"窗口缩放 {root.winfo_fpixels('1i') / 96:.2f}×）")
    root.mainloop()
    return 0
```

启动日志里应该看到 `[ui] 已启用 DPI 感知（系统 DPI 144，窗口缩放 1.50×）` —— **这一行就是验收判据**。如果看到 `1.00×`，说明感知没生效（多半是被某个提前建窗口的 import 挡住了）。

## 4. ★ 为什么用 `SYSTEM_DPI_AWARE(1)` 而不是 PerMonitorV2

`shcore.SetProcessDpiAwareness` 的三档：

| 值 | 含义 |
|:--|:--|
| `0` | `PROCESS_DPI_UNAWARE` —— 就是我们踩的那个坑 |
| `1` | **`PROCESS_SYSTEM_DPI_AWARE`** ← 本项目用的 |
| `2` | `PROCESS_PER_MONITOR_DPI_AWARE` |

理论上 `PerMonitorV2`（要 `user32.SetProcessDpiAwarenessContext(-4)`）在「窗口被拖到另一块不同缩放的显示器」时表现更好。本项目没走那条路，**故意**：

* 这是一个**单窗口工具**，用户不会把它在几块不同缩放的屏幕之间来回拖；
* `SYSTEM_DPI_AWARE` 已经解决了全部实际问题（窗口尺寸、`wraplength`、居中、CTk 缩放）；
* 少一档回退链就少一处「在某台机器上静默失效」的可能 —— 这个坑本身就是「设了但没生效、还看不出来」造成的。

## 5. 依赖 DPI 感知的下游代码（**改动感知等级必须连带复验这些**）

| 位置 | 依赖方式 |
|:--|:--|
| `launcher_ui.py:542-543` | `from customtkinter import ScalingTracker`<br>`scale = ScalingTracker.get_window_scaling(self.root) or 1.0` |
| `launcher_ui.py:664-680` | Win32 侧坐标换算：**「这个进程看到的像素」**；150% 缩放 / DPI 144 时<br>`dpi = ctypes.windll.user32.GetDpiForWindow(self.root.winfo_id())`<br>`return int(physical_px * 96 / dpi)` |
| `launcher_ui.py:256`、`launcher_ui.py:471`、`scripts/debug_view.py:326` | 长文字一律用**原生 `tk.Label`**：`CTkLabel` 在高 DPI 下把高度锁死在 42px，折到第 4 行整行看不见 |
| `launcher_ui.py:519` | 底部「调试视图」按钮的位置按物理像素算（缩放 1.5），不感知时它会整个落到窗口外 |
| `scripts/test_window_size.py:9` | 把 `launcher_ui.py` 里的算式**抄成纯函数**，在多种屏幕/DPI 组合下跑断言 |

**所以「把 `_enable_dpi_awareness()` 去掉」不是无害的清理**，它会让上面这一整列重新错位。

## 6. 遗留：没有留下 DPI 探针脚本

查过了，`debug/` 下**没有** DPI 相关的探针（`Get-ChildItem debug -Filter '_probe*'` 只有 `_probe_codec / _probe_crash / _probe_desktop / _probe_exam / _probe_farm / _probe_gpu / _probe_mumu / _probe_mumu2 / _probe_nav / _probe_node / _probe_time(2/3/4) / _probe_ui / _probe_win32`，其中 `_probe_win32.py` 是 Win32 **控制器**探针，不是 DPI 的）。

当时的排查是靠「把 `winfo_fpixels('1i')` / 工作区尺寸 / 窗口实际大小打进日志，改代码前后各跑一次对照」做的 —— 证据就是上面那两张表。**要复现，最小做法是拿 `_dpi_now()` 和 `root.winfo_fpixels('1i') / 96` 在启动日志里对比**（`launcher_ui.py:1751` 那行已经在打了）。
