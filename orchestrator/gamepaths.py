"""找《蔚蓝》与 OBS 的安装目录（只读，不改任何配置）。

依次看：命令行显式给的目录、环境变量 CELESTE_DIR、部署向导写的 configs/local.deploy.json、正在运行的 Celeste.exe
所在目录、各个 Steam 库。手动部署（没跑部署向导）时也能找到游戏的 Saves 目录。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .config import REPO_ROOT

DEPLOY_ANSWERS = REPO_ROOT / "configs" / "local.deploy.json"


def _deploy_answers() -> dict:
    try:
        value = json.loads(DEPLOY_ANSWERS.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


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
    configured = [explicit, os.environ.get("CELESTE_DIR"), _deploy_answers().get("celeste_dir")]
    found: list[Path] = []
    for item in configured:
        if item and Path(item).is_dir() and Path(item) not in found:
            found.append(Path(item))
    if found:
        return found
    for path in [running_game_dir(), *steam_app_dirs("Celeste")]:
        if path and path not in found and _is_game_dir(path):
            found.append(path)
    return found


def celeste_dir(explicit: str | os.PathLike | None = None) -> Path:
    """游戏目录；找不到时抛 ValueError（说明怎么指定）。"""
    found = celeste_candidates(explicit)
    if found:
        return found[0]
    raise ValueError("找不到《蔚蓝》的安装目录：运行部署向导（setup.ps1），或设置环境变量 CELESTE_DIR 指向含 Celeste.exe 的目录"
                     "，或先启动游戏")


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
