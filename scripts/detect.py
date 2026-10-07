"""模拟器与 adb 的自动探测。

为什么需要这个模块：**不能假设安装目录**。用户可能装在 `C:\\`、`D:\\`、
甚至非默认路径；MuMu 还有 12 / Nx 两套目录结构，版本号目录（如 `15.0`）
也会随平台升级变化。硬编码路径在别人机器上必然失效。

探测顺序（先精确后宽泛，每条都会验证）：

**adb.exe**
  1. 配置里显式指定的路径
  2. 环境变量 `ANDROID_HOME` / `ANDROID_SDK_ROOT` 下的 platform-tools
  3. `PATH` 里的 adb
  4. 注册表卸载项里的 InstallLocation
  5. **正在运行的模拟器进程**的路径 —— 最可靠，直接给出真实安装位置
  6. 各盘符下的常见目录名（用通配符搜，不假设层级深度）
  7. 全盘兜底搜索（有深度和数量上限，避免卡死）

**模拟器端点 (adb connect 的地址)**
  1. 配置里显式指定的地址
  2. **MuMuManager.exe info -v all** 给出的 adb_port —— 权威，支持多开
  3. 配置文件里记录的端口
  4. 扫描候选端口并**逐个验证**（能连上 + 能 shell + 是安卓 + 分辨率匹配）

验证优先于猜测：任何一个候选都要真的能跑 `adb version` 或真的能连上设备，
才认为可用。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 目标平台的屏幕特征，用于确认探测到的是「对的那台设备」
EXPECTED_SIZE = "1080x1920"
EXPECTED_DENSITY = "280"

# 每个子进程的超时。探测不该让程序卡住。
TIMEOUT_QUICK = 8
TIMEOUT_SHELL = 12

# 全盘兜底搜索的限制
MAX_SCAN_DEPTH = 4
MAX_SCAN_HITS = 40


def _log_default(msg: str) -> None:
    print(msg)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = TIMEOUT_QUICK) -> tuple[int, str]:
    """跑一个子进程，返回 (退出码, stdout+stderr)。失败不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            # 打包后不能弹黑框
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (proc.stdout or b"") + (proc.stderr or b"")
        return proc.returncode, out.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, str(exc)


def _is_working_adb(path: Path) -> bool:
    """真的能跑 `adb version` 才算可用 —— 只看文件存在不够。"""
    if not path.is_file():
        return False
    code, out = _run([str(path), "version"])
    return code == 0 and "Android Debug Bridge" in out


def _all_drive_roots() -> list[Path]:
    """枚举所有存在的盘符，不假设只有 C 和 D。"""
    roots: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:\\")
        try:
            if root.exists():
                roots.append(root)
        except OSError:
            continue
    return roots


# --------------------------------------------------------------------------
# adb 探测
# --------------------------------------------------------------------------

@dataclass
class AdbCandidate:
    path: Path
    source: str


def _registry_install_locations() -> list[Path]:
    """从卸载信息里取安装位置。

    实测要点：
    * `InstallLocation` **可能为空**（MuMu 12 就没有），所以必须有退路；
    * `UninstallString` / `DisplayIcon` 的路径**含空格且带引号**
      （`"C:\\Program Files\\Netease\\MuMu\\uninstall.exe"`），
      按空格切分会得到 `C:\\Program` 这种畸形路径；
    * 解析出来的路径必须**真实存在**才采信，否则后续递归扫描会在
      不存在的目录上白跑。
    """
    found: list[Path] = []
    if os.name != "nt":
        return found

    try:
        import winreg
    except ImportError:
        return found

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    pattern = re.compile(r"MuMu|Netease|网易|LDPlayer|雷电|BlueStacks|逍遥|MEmu|Nox", re.I)

    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as root:
                for i in range(winreg.QueryInfoKey(root)[0]):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as item:
                            disp = _reg_value(item, "DisplayName")
                            if not disp or not pattern.search(disp):
                                continue

                            # 按可靠性依次尝试三种来源
                            for value_name in ("InstallLocation", "UninstallString", "DisplayIcon"):
                                raw = _reg_value(item, value_name)
                                p = _dir_from_registry_value(raw)
                                if p and p not in found:
                                    found.append(p)
                    except OSError:
                        continue
        except OSError:
            continue

    return found


def _reg_value(key, name: str) -> str | None:
    """读一个注册表字符串值。读不到返回 None，不抛异常。"""
    try:
        import winreg

        value, _ = winreg.QueryValueEx(key, name)
        return str(value) if value else None
    except (OSError, ImportError):
        return None


def _dir_from_registry_value(raw: str | None) -> Path | None:
    """从注册表值里抠出**存在的**安装目录。

    处理三种形态：
      `C:\\Program Files\\Netease\\MuMu`                    （纯目录）
      `"C:\\Program Files\\Netease\\MuMu\\uninstall.exe"`   （带引号，含空格）
      `C:\\path\\MuMuNxMain.ico`                            （无引号，无空格）
    """
    if not raw:
        return None

    raw = raw.strip()

    # 带引号：取引号内的部分
    m = re.match(r'^"([^"]+)"', raw)
    if m:
        candidate = m.group(1)
    else:
        # 无引号。可能是纯目录，也可能是指向 exe/ico 的路径。
        # 先整体当路径试；若不存在再逐层回退。
        candidate = raw

    p = Path(candidate)

    # 指向文件时退到父目录
    if p.suffix.lower() in (".exe", ".ico", ".dll"):
        p = p.parent

    # 逐级向上找一个真实存在的目录
    for probe in [p, *p.parents]:
        try:
            if probe.is_dir():
                return probe
        except OSError:
            break
    return None


def _running_emulator_roots() -> list[Path]:
    """从**正在运行的**模拟器进程路径反推安装根目录。

    这是最可靠的一条：用户实际在跑的那份安装，路径必然是真实的。
    例：C:\\Program Files\\Netease\\MuMu\\nx_device\\15.0\\shell\\MuMuNxDevice.exe
        -> C:\\Program Files\\Netease\\MuMu
    """
    roots: list[Path] = []
    if os.name != "nt":
        return roots

    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return roots

    # 用 WMI 太重，这里直接枚举进程路径（需要 PROCESS_QUERY_LIMITED_INFORMATION）
    try:
        import winreg  # noqa: F401  仅确认可用性
    except ImportError:
        pass

    try:
        code, out = _run([
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-Process | Where-Object { $_.Path -match 'MuMu|Nemu|LDPlayer|dnplayer|HD-Player|MEmu|Nox' }"
            " | Select-Object -ExpandProperty Path -Unique",
        ], timeout=TIMEOUT_SHELL)
    except Exception:
        return roots

    if code != 0:
        return roots

    for line in out.splitlines():
        line = line.strip()
        if not line.lower().endswith(".exe"):
            continue
        p = Path(line)
        # 找到含 nx_device / nx_main / shell 的那一层，回退到安装根
        for part in ("nx_device", "nx_main", "shell"):
            if part in p.parts:
                idx = p.parts.index(part)
                if idx > 0:
                    roots.append(Path(*p.parts[:idx]))
                break
        else:
            roots.append(p.parent)

    return roots


def _scan_drives_for_adb(log) -> list[Path]:
    """兜底：在常见目录名下搜索 adb.exe。

    用**递归**而不是固定层级通配符——MuMu 的结构是
    `<root>\\nx_device\\<版本>\\shell\\adb.exe`，层级会随版本变化。
    """
    names = [
        "MuMuPlayer-12.0", "MuMu", "Netease", "LDPlayer", "LDPlayer9",
        "BlueStacks_nxt", "BlueStacks", "MEmu", "Nox", "Android",
    ]
    hits: list[Path] = []

    for root in _all_drive_roots():
        for name in names:
            for base in (root / name, root / "Program Files" / name,
                         root / "Program Files (x86)" / name):
                if not base.is_dir():
                    continue
                try:
                    for found in base.rglob("adb.exe"):
                        hits.append(found)
                        if len(hits) >= MAX_SCAN_HITS:
                            return hits
                except OSError:
                    continue
    if hits:
        log(f"[detect] 目录扫描命中 {len(hits)} 个候选")
    return hits


def find_adb_candidates(explicit: str | None = None, log=_log_default) -> list[AdbCandidate]:
    """按可靠性从高到低收集 adb 候选，已去重，未验证。"""
    out: list[AdbCandidate] = []
    seen: set[str] = set()

    def add(path: Path | str | None, source: str) -> None:
        if not path:
            return
        try:
            p = Path(path)
        except (TypeError, ValueError):
            return
        key = str(p).lower()
        if key in seen:
            return
        seen.add(key)
        out.append(AdbCandidate(p, source))

    # 1) 显式配置
    add(explicit, "配置文件指定")

    # 2) Android SDK 环境变量
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root:
            add(Path(root) / "platform-tools" / "adb.exe", f"{env}")
            add(Path(root) / "platform-tools" / "adb", f"{env}")

    # 3) PATH
    which = shutil.which("adb")
    if which:
        add(which, "PATH")

    # 4) 注册表安装位置（含 MuMuManager 用于查端口的那个目录）
    for base in _registry_install_locations():
        add(base / "shell" / "adb.exe", "注册表安装位置")
        add(base / "nx_main" / "adb.exe", "注册表安装位置")
        add(base / "adb.exe", "注册表安装位置")
        for c in _recursive_adb(base, "注册表安装位置(递归)"):
            add(c.path, c.source)

    # 5) 正在运行的模拟器进程 —— 最贴近用户实际环境
    for root in _running_emulator_roots():
        add(root / "nx_main" / "adb.exe", "运行中的模拟器")
        add(root / "shell" / "adb.exe", "运行中的模拟器")
        for c in _recursive_adb(root, "运行中的模拟器(递归)"):
            add(c.path, c.source)

    # 6) 各盘符常见目录
    for hit in _scan_drives_for_adb(log):
        add(hit, "目录扫描")

    return out


def _recursive_adb(base: Path, source: str, max_depth: int = 4,
                   limit: int = 24) -> list["AdbCandidate"]:
    """在 base 下有限深度地找 adb.exe。

    不用固定层级通配符——MuMu 的结构是
    `<root>\\nx_device\\<版本>\\shell\\adb.exe`，「版本」那层会随平台升级变化。
    这里限制深度和数量，避免在大目录下卡住。
    """
    out: list[AdbCandidate] = []
    base_depth = len(base.parts)
    try:
        for p in base.rglob("adb.exe"):
            if len(p.parts) - base_depth > max_depth:
                continue
            out.append(AdbCandidate(p, source))
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out


def _adb_priority(cand: AdbCandidate) -> tuple[int, int]:
    """候选优先级，数字越小越优先。

    第一名给**模拟器自带、且版本目录匹配**的 adb
    （`<root>\\nx_device\\15.0\\shell\\adb.exe`）——它和模拟器本体同源，
    兼容性最好。其次是 `nx_main` 等通用位置。
    """
    s = str(cand.path).lower()
    if "nx_device" in s and s.endswith("shell\\adb.exe"):
        return (0, len(s))
    if "shell\\adb.exe" in s:
        return (1, len(s))
    if "nx_main" in s:
        return (2, len(s))
    if "platform-tools" in s:
        return (3, len(s))
    return (4, len(s))


def read_install_config(base: Path) -> dict:
    """读 MuMu 的 install_config.json。

    这个文件比注册表可靠得多：它明确列出每个引擎的安装目录和 Android 版本，
    可以和 MuMuManager 报的运行实例精确对上。

    结构（注意 `android_version` 与 `install_dir` 都在 `player` 下面）：
        {
          "engines": {
            "nemux":  { "player": { "android_version": "12.0",
                                    "install_dir": "<root>\\\\nx_device\\\\12.0\\\\shell" } },
            "mumu15": { "player": { "android_version": "15.0",
                                    "install_dir": "<root>\\\\nx_device\\\\15.0\\\\shell" } }
          }
        }

    文件是 UTF-8，但网易写入的中文可能已损坏，所以用 errors="replace"，
    只依赖其中的 ASCII 路径字段。
    """
    for candidate in (base / "configs" / "install_config.json",
                      base / "install_config.json"):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def _mumu_engine_adb_paths(base: Path, android_version: str | None = None) -> list[Path]:
    """从 install_config.json 取引擎自带的 adb.exe 路径。

    给了 android_version 就只返回匹配那个引擎的路径；否则返回全部，
    按版本号倒序（新的在前）。
    """
    cfg = read_install_config(base)
    engines = cfg.get("engines")
    if not isinstance(engines, dict):
        return []

    picks: list[tuple[str, Path]] = []
    for _name, engine in engines.items():
        if not isinstance(engine, dict):
            continue
        player = engine.get("player")
        if not isinstance(player, dict):
            continue
        if str(player.get("installed", "")).lower() != "true":
            continue

        ver = str(player.get("android_version") or "")
        install_dir = player.get("install_dir")
        if not install_dir:
            continue

        picks.append((ver, Path(str(install_dir)) / "adb.exe"))

    if android_version:
        exact = [p for ver, p in picks if ver == android_version]
        if exact:
            return exact

    def ver_key(item: tuple[str, Path]) -> tuple:
        try:
            return tuple(int(x) for x in item[0].split("."))
        except ValueError:
            return (0,)

    return [p for _v, p in sorted(picks, key=ver_key, reverse=True)]


def find_adb(explicit: str | None = None, log=_log_default) -> Path:
    """返回第一个**验证可用**的 adb.exe。

    优先级：
      1. 配置里显式指定的路径
      2. **与运行中实例 Android 版本精确匹配**的引擎自带 adb
         （靠 MuMuManager 报的 android_version + install_config.json 对上）
      3. 其它引擎自带的 adb（版本号高的优先）
      4. 前述各处的通用探测结果

    先排序再验证——否则可能选中一个能跑但和模拟器引擎不匹配的 adb。
    """
    tried: list[str] = []

    # 1) 配置指定
    if explicit and Path(explicit).is_file():
        if _is_working_adb(Path(explicit)):
            log(f"[detect] adb 可用: {explicit}  (来源: 配置文件指定)")
            return Path(explicit)
        tried.append(f"{explicit}  [配置文件指定]")

    # 2) 与运行实例版本匹配的引擎 adb
    running_ver = running_android_version(log)
    if running_ver:
        log(f"[detect] 运行中实例的 Android 版本: {running_ver}")
    for base in _candidate_install_roots():
        for p in _mumu_engine_adb_paths(base, running_ver):
            if not p.is_file():
                continue
            if _is_working_adb(p):
                tag = "运行中实例匹配" if running_ver else "引擎自带"
                log(f"[detect] adb 可用: {p}  (来源: {tag})")
                return p
            tried.append(f"{p}  [{tag}]")

    # 3) 通用候选，按优先级排序
    cands = find_adb_candidates(explicit, log)
    for cand in sorted(cands, key=_adb_priority):
        if not cand.path.is_file():
            continue
        if _is_working_adb(cand.path):
            log(f"[detect] adb 可用: {cand.path}  (来源: {cand.source})")
            return cand.path
        tried.append(f"{cand.path}  [{cand.source}]")

    raise FileNotFoundError(
        "没有找到可用的 adb.exe。已尝试：\n"
        + ("\n".join(f"  - {t}" for t in tried[:15]) if tried else "  （无候选）")
        + "\n\n请任选其一：\n"
        "  1) 安装 Android SDK platform-tools 并加入 PATH；\n"
        "  2) 在配置文件的 adb.adb_path 里写死完整路径。"
    )


def _candidate_install_roots() -> list[Path]:
    """可能包含 install_config.json 的安装根目录。"""
    roots: list[Path] = []
    seen: set[str] = set()

    def add(p: Path | None) -> None:
        if p is None:
            return
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            roots.append(p)

    add(mumu_install_root_from_exe())
    for p in _registry_install_locations():
        add(p)
    for p in _running_emulator_roots():
        add(p)
    for root in _all_drive_roots():
        for name in ("MuMu", "MuMuPlayer-12.0"):
            add(root / "Program Files" / "Netease" / name)
            add(root / "Program Files" / name)
            add(root / name)
    return roots


def running_android_version(log=_log_default) -> str | None:
    """从 MuMuManager 拿到运行中实例的 Android 版本号（如 "15.0"）。

    用来挑「和当前引擎匹配的那份 adb」，避免选中另一个引擎的版本。
    """
    for inst in _mumu_instances(log):
        if inst.get("is_android_started") and inst.get("android_version"):
            return str(inst["android_version"])
    # 没有已启动的实例时，退而取第一个报告的版本
    for inst in _mumu_instances(log):
        if inst.get("android_version"):
            return str(inst["android_version"])
    return None


# --------------------------------------------------------------------------
# 模拟器端点探测
# --------------------------------------------------------------------------

@dataclass
class DeviceCandidate:
    address: str
    source: str
    verified: bool = False
    size: str = ""
    density: str = ""
    model: str = ""


def find_mumumanager() -> Path | None:
    """定位 MuMuManager.exe（用来查权威的 adb_port）。"""
    cands: list[Path] = []

    for base in _registry_install_locations():
        cands.append(base / "nx_main" / "MuMuManager.exe")
        cands.append(base / "MuMuManager.exe")

    for root in _running_emulator_roots():
        cands.append(root / "nx_main" / "MuMuManager.exe")
        cands.append(root / "MuMuManager.exe")

    for root in _all_drive_roots():
        for name in ("MuMu", "MuMuPlayer-12.0", "Netease"):
            for base in (root / "Program Files" / "Netease" / name,
                         root / name):
                cands.append(base / "nx_main" / "MuMuManager.exe")

    for c in cands:
        if c.is_file():
            return c
    return None


def mumu_install_root_from_exe() -> Path | None:
    """从运行中的 MuMuNxMain/MuMuNxDevice 进程路径反推安装根。

    这是最贴近用户实际环境的一条线索——用户正在跑的那份安装。
    """
    for root in _running_emulator_roots():
        # 安装根下应该有 nx_main 或 configs
        if (root / "nx_main").is_dir() or (root / "configs").is_dir():
            return root
    roots = _running_emulator_roots()
    return roots[0] if roots else None


def _mumu_instances(log=_log_default) -> list[dict]:
    """问 MuMuManager 要实例列表，返回原始 dict 列表。

    `MuMuManager info -v all` 输出以实例序号为键的 JSON；
    单实例时可能直接是扁平对象。两种都要吃得下。
    """
    mgr = find_mumumanager()
    if mgr is None:
        return []

    code, out = _run([str(mgr), "info", "-v", "all"], timeout=TIMEOUT_SHELL)
    if not out.strip():
        return []

    # 输出里可能混有非 JSON 行，截取第一个 { 到最后一个 }
    start, end = out.find("{"), out.rfind("}")
    if start < 0 or end <= start:
        # 退回单实例查询
        code, out = _run([str(mgr), "info", "-v", "0"], timeout=TIMEOUT_SHELL)
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end <= start:
            return []

    try:
        data = json.loads(out[start:end + 1])
    except json.JSONDecodeError:
        return []

    if not isinstance(data, dict):
        return []

    # 多实例：{ "0": {...}, "1": {...} }
    if data and all(isinstance(v, dict) for v in data.values()):
        return [v for v in data.values() if isinstance(v, dict)]
    # 单实例：扁平对象
    return [data]


def mumu_adb_ports(log=_log_default) -> list[tuple[str, str]]:
    """要所有实例的 (地址, 实例名)。

    这是最权威的端口来源——支持多开，不受默认端口假设影响。
    """
    mgr = find_mumumanager()
    if mgr is None:
        log("[detect] 未找到 MuMuManager.exe，跳过 MuMu 端口查询")
        return []

    log(f"[detect] MuMuManager: {mgr}")

    found: list[tuple[str, str]] = []
    for inst in _mumu_instances(log):
        port = inst.get("adb_port")
        host = inst.get("adb_host_ip") or "127.0.0.1"
        name = inst.get("name") or f"实例{inst.get('index', '?')}"
        started = inst.get("is_android_started")
        if port:
            found.append((f"{host}:{port}",
                          f"MuMuManager/{name}" + ("" if started else "(未启动)")))
    return found


def verify_device(adb: Path, address: str, log=_log_default) -> DeviceCandidate:
    """真正连上去确认这台设备可用，并读回屏幕参数。"""
    cand = DeviceCandidate(address=address, source="")

    code, out = _run([str(adb), "connect", address], timeout=TIMEOUT_SHELL)
    if code != 0:
        return cand

    code, out = _run(
        [str(adb), "-s", address, "shell",
         "getprop ro.product.model; wm size; wm density"],
        timeout=TIMEOUT_SHELL,
    )
    if code != 0 or "error" in out.lower() or "device offline" in out.lower():
        return cand

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("Physical size:"):
            cand.size = ln.split(":", 1)[1].strip()
        elif ln.startswith("Physical density:"):
            cand.density = ln.split(":", 1)[1].strip()
        elif not cand.model and ":" not in ln:
            cand.model = ln

    cand.verified = bool(cand.size)
    return cand


def find_device(
    adb: Path,
    explicit: str | None = None,
    fallbacks: list[str] | None = None,
    size_hint: str = EXPECTED_SIZE,
    log=_log_default,
) -> str:
    """确定要连接的模拟器地址。返回**已验证可用**的地址。

    优先级：配置 > MuMuManager（权威，支持多开）> 配置里的 fallback
            > `adb devices` 里像模拟器的条目
    """
    tried: list[str] = []
    seen: set[str] = set()

    def consider(address: str, source: str) -> str | None:
        if not address or address in seen:
            return None
        seen.add(address)
        log(f"[detect] 尝试验证 {address}  ({source})")
        cand = verify_device(adb, address, log)
        if cand.verified:
            cand.source = source
            log(f"[detect] ✓ {address}  {cand.model} {cand.size} @{cand.density}  [{source}]")
            if size_hint and cand.size and cand.size != size_hint:
                log(f"[detect]   注意: 分辨率 {cand.size} 与预期 {size_hint} 不一致，"
                    f"识别的坐标可能不准")
            return address
        tried.append(f"{address} [{source}]")
        return None

    # 1) 配置里的地址
    if explicit:
        got = consider(explicit, "配置文件")
        if got:
            return got

    # 2) MuMuManager —— 权威来源
    for address, source in mumu_adb_ports(log):
        got = consider(address, source)
        if got:
            return got

    # 3) 配置里的 fallback
    for address in (fallbacks or []):
        got = consider(address, "配置 fallback")
        if got:
            return got

    # 4) adb 自己看到的设备列表
    code, out = _run([str(adb), "devices"], timeout=TIMEOUT_QUICK)
    if code == 0:
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                addr = parts[0]
                # 只考虑像模拟器的条目，避免连到用户真实手机
                if addr.startswith("emulator-") or addr.startswith("127.0.0.1:") \
                        or addr.startswith("localhost:"):
                    got = consider(addr, "adb devices")
                    if got:
                        return got

    raise ConnectionError(
        "没有找到可用的模拟器。已验证失败的地址：\n"
        + ("\n".join(f"  - {t}" for t in tried) if tried else "  （无候选）")
        + "\n\n排查：\n"
        "  1) 模拟器是否已启动并进入桌面；\n"
        "  2) 若用 MuMu，可在其安装目录运行:\n"
        '     MuMuManager.exe info -v all    # 看 adb_port\n'
        "  3) 或在配置文件的 adb.address 里直接写死地址。"
    )


# --------------------------------------------------------------------------
# CLI：让人能单独跑一下看探测到什么
# --------------------------------------------------------------------------

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    print("=" * 64)
    print(" 环境探测")
    print("=" * 64)

    print("\n[1] adb 候选（按可靠性排序）")
    try:
        cands = find_adb_candidates(log=print)
    except Exception as exc:
        print(f"    收集失败: {exc}")
        cands = []
    if not cands:
        print("    （无）")
    for i, c in enumerate(cands[:15], 1):
        ok = "✓" if _is_working_adb(c.path) else " "
        print(f"  {ok} {i:>2}. {c.path}")
        print(f"       来源: {c.source}")

    print("\n[2] 可用 adb")
    try:
        adb = find_adb(log=print)
        print(f"    -> {adb}")
    except FileNotFoundError as exc:
        print(f"    ✗ {exc}")
        return 1

    print("\n[3] MuMuManager 报告的实例")
    ports = mumu_adb_ports(log=print)
    if not ports:
        print("    （无）")
    for addr, src in ports:
        print(f"    {addr}   [{src}]")

    print("\n[4] 模拟器端点验证")
    try:
        addr = find_device(adb, log=print)
        print(f"    -> {addr}")
    except ConnectionError as exc:
        print(f"    ✗ {exc}")
        return 1

    print("\n" + "=" * 64)
    print(" 探测完成")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
