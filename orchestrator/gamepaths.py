"""找《蔚蓝》与 OBS 的安装目录（只读，不改任何配置）。

依次看：命令行显式给的目录、环境变量 CELESTE_DIR、控制台里填的游戏位置（configs/local.console.json）、
部署向导写的 configs/local.deploy.json、正在运行的 Celeste.exe 所在目录、各个 Steam 库。
手动部署（没跑部署向导）时也能找到游戏的 Saves 目录。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .config import REPO_ROOT

DEPLOY_ANSWERS = REPO_ROOT / "configs" / "local.deploy.json"
CONSOLE_PREFS = REPO_ROOT / "configs" / "local.console.json"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _deploy_answers() -> dict:
    return _read(DEPLOY_ANSWERS)


def _console_prefs() -> dict:
    return _read(CONSOLE_PREFS)


def steam_libraries() -> list[Path]:
    """本机的 Steam 库根目录（含 steamapps 的目录）；不是 Windows 时为空。"""
    if sys.platform != "win32":
        return []
    roots: list[Path] = []
    try:
        import winreg
        for hive, key_path, value in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                      (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    roots.append(Path(winreg.QueryValueEx(key, value)[0]))
            except OSError:
                continue
    except ImportError:
        pass
    roots.append(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam")
    found: list[Path] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        extra = []
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
            extra = [Path(m.group(1).replace("\\\\", "\\")) for m in re.finditer(r'"path"\s+"([^"]+)"', text)]
        except OSError:
            pass
        for library in [root, *extra]:
            if library not in found and (library / "steamapps").is_dir():
                found.append(library)
    return found


def steam_app_dirs(name: str) -> list[Path]:
    return [library / "steamapps" / "common" / name for library in steam_libraries()
            if (library / "steamapps" / "common" / name).is_dir()]


def running_game_dir() -> Path | None:
    """正在运行的 Celeste.exe 所在目录（Windows）。"""
    if sys.platform != "win32":
        return None
    try:
        from .gamewindow import game_executable
        path = game_executable()
    except Exception:  # noqa: BLE001（找不到就算了）
        return None
    return Path(path).parent if path else None


def _is_game_dir(path: Path) -> bool:
    return (path / "Celeste.exe").is_file() or (path / "Celeste.dll").is_file()


def celeste_candidates(explicit: str | os.PathLike | None = None) -> list[Path]:
    """按优先顺序列出可能的游戏目录。明确配置的（参数、环境变量、部署配置）只要目录存在就算；
    自动找到的（正在运行的游戏、Steam 库）要确实有 Celeste.exe。"""
    return [path for path, _ in _celeste_sources(explicit)]


SOURCE_NAMES = {"explicit": "命令行参数", "env": "环境变量 CELESTE_DIR", "console": "控制台里填的位置",
                "deploy": "部署向导", "running": "正在运行的游戏", "steam": "Steam 库"}


def _celeste_sources(explicit=None) -> list[tuple[Path, str]]:
    configured = [(explicit, "explicit"), (os.environ.get("CELESTE_DIR"), "env"),
                  (_console_prefs().get("celeste_dir"), "console"), (_deploy_answers().get("celeste_dir"), "deploy")]
    found: list[tuple[Path, str]] = []
    for item, source in configured:
        if item and Path(item).is_dir() and Path(item) not in [p for p, _ in found]:
            found.append((Path(item), source))
    if found:
        return found
    for path, source in [(running_game_dir(), "running"), *[(p, "steam") for p in steam_app_dirs("Celeste")]]:
        if path and path not in [p for p, _ in found] and _is_game_dir(path):
            found.append((path, source))
    return found


def celeste_dir_info() -> dict:
    """控制台显示用：现在用的游戏目录与它从哪里来。"""
    found = _celeste_sources()
    if not found:
        return {"path": None, "source": None, "source_name": None}
    path, source = found[0]
    return {"path": str(path), "source": source, "source_name": SOURCE_NAMES[source],
            "everest": (path / "Celeste.dll").is_file(), "saves": (path / "Saves").is_dir()}


def is_game_dir(path) -> bool:
    return bool(path) and _is_game_dir(Path(path))


def celeste_dir(explicit: str | os.PathLike | None = None) -> Path:
    """游戏目录；找不到时抛 ValueError（说明怎么指定）。"""
    found = celeste_candidates(explicit)
    if found:
        return found[0]
    raise ValueError("找不到《蔚蓝》的安装目录：在控制台的「存档与章节」页填写游戏位置，或运行部署向导（setup.ps1），"
                     "或设置环境变量 CELESTE_DIR 指向含 Celeste.exe 的目录，或先启动游戏")


def obs_candidates(explicit: str | os.PathLike | None = None, configured: str | None = None) -> list[Path]:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    raw = [explicit, configured, os.environ.get("OBS_DIR"), _deploy_answers().get("obs_dir"),
           Path(program_files) / "obs-studio", *steam_app_dirs("OBS Studio")]
    found: list[Path] = []
    for item in raw:
        if not item:
            continue
        path = Path(item)
        if path not in found and (path / "bin" / "64bit" / "obs64.exe").is_file():
            found.append(path)
    return found
