"""速通模式的整章地图：进入某章某一面时，把官方地图数据导出成 JSON 写进工作区 game/map/，智能体可以直接读。

导出用本机的预置模拟器（sim/dist 里的 celeste-sim，Windows 上是 celeste-sim.exe）的 map 命令，地图数据是
sim/maps（游戏 Content/Maps 的副本，与发布目录同级、不入库）。没有模拟器或地图数据时不导出，也不影响游玩。
探索模式不提供（探索要自己看）。格式说明见 kb/speedrun/chapter_map.md。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .modes import is_explore

TIMEOUT_SECONDS = 60


def relative_path(sid: str, side: str) -> str:
    """工作区里的位置：章节编号里的 / 换成 _，再接面别，例如 game/map/Celeste_1-ForsakenCity_A.json。"""
    return f"game/map/{str(sid).replace('/', '_')}_{side}.json"


def maps_dir(cfg) -> Path:
    return cfg.sim_dir.parent / 'maps'


def exporter(cfg) -> Path | None:
    """能导出地图的模拟器程序：本机系统对应的那一个，并且旁边有地图数据；否则 None。"""
    name = 'celeste-sim.exe' if os.name == 'nt' else 'celeste-sim'
    exe = cfg.sim_dir / name
    return exe if exe.is_file() and maps_dir(cfg).is_dir() else None


def wanted(cfg) -> bool:
    return not is_explore(cfg)


def export(cfg, workspace: Path, sid: str, side: str) -> tuple[Path | None, str | None]:
    """导出一章一面的地图；返回 (写入的文件, 错误说明)。已经导出过的直接返回。"""
    target = Path(workspace) / relative_path(sid, side)
    if target.is_file():
        return target, None
    exe = exporter(cfg)
    if exe is None:
        return None, '本机没有预置模拟器或官方地图数据（sim/dist、sim/maps），不提供整章地图'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    env = {**os.environ, 'CELESTE_SIM_MAPS': str(maps_dir(cfg)), 'DOTNET_EnableDiagnostics': '0'}
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        done = subprocess.run([str(exe), 'map', str(sid), '--side', str(side), '--out', str(temporary)],
                              capture_output=True, text=True, timeout=TIMEOUT_SECONDS, env=env, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        return None, f'导出整章地图失败：{exc}'
    if done.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        return None, f'导出整章地图失败：{(done.stderr or done.stdout or "").strip()[-300:]}'
    temporary.replace(target)
    return target, None
