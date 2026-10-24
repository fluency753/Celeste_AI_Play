"""录制（M6）：让 OBS 锁步录制插件只录游戏真正推进过的帧。

流程：主控接管游戏后 start()：桥接把录制指令转给 OBS 插件，插件建录制输出并打开锁步——之后 OBS 每个视频
节拍向桥接要一帧，桥接推进一帧、画到屏幕上再回复，插件只把这些帧交给 ffmpeg；智能体思考、桥接空闲时不写。
结束时 stop()：插件关锁步、等 ffmpeg 编完，把画面与声音合成 gameplay.mkv。

前提：OBS 已用 tools/obs_setup.py 启动（CelesteAI 配置文件与场景集合），装了 celeste-lockstep 插件，
插件已经连上桥接的时钟端口（hello 的 clock.connected）。

输出目录（runs/<智能体>/<时间>/recording/）：
    gameplay.mkv   成片（画面 + 声音）
    video.mkv      只有画面
    audio.wav      只有声音（32 位浮点立体声，每个游玩帧 1/60 秒）
    frames.csv     每个成片帧对应的游戏帧号、章节计时与 OBS 时间戳（对账用）
    segments.csv   连续推进的各段与每段开头量到的声音延迟（声音按它对齐）
    ffmpeg_*.log   ffmpeg 的输出
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gamewindow
from .config import REPO_ROOT

CONFIG = REPO_ROOT / "configs" / "recording.json"
LOCAL_CONFIG = REPO_ROOT / "configs" / "local.recording.json"   # 本机覆盖（不入库）


class RecordingError(RuntimeError):
    pass


def load_config() -> dict:
    config: dict = {}
    for path in (CONFIG, LOCAL_CONFIG):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key] = {**config[key], **value}
            else:
                config[key] = value
    return config


def find_ffmpeg(explicit: str | None = None) -> str:
    """命令行参数 → 配置 → PATH → winget 的默认链接位置。"""
    for candidate in (explicit, load_config().get("ffmpeg")):
        if candidate:
            if Path(candidate).exists():
                return str(Path(candidate))
            raise RecordingError(f"找不到 ffmpeg：{candidate}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
    if winget.exists():
        return str(winget)
    raise RecordingError("找不到 ffmpeg：请安装（winget install Gyan.FFmpeg），或用 --ffmpeg 指定路径")


@dataclass
class RecordOptions:
    dir: Path
    ffmpeg: str
    video_args: str
    audio_offset_ms: float = 0.0
    marker: bool = False     # 校准用：在画面左下角画帧号条码


def options_from(run_dir: Path, ffmpeg: str | None = None, video_args: str | None = None,
                 audio_offset_ms: float | None = None, marker: bool = False) -> RecordOptions:
    config = load_config()
    return RecordOptions(
        dir=run_dir / "recording",
        ffmpeg=find_ffmpeg(ffmpeg),
        video_args=video_args or config.get("video_args") or "",
        audio_offset_ms=float(audio_offset_ms if audio_offset_ms is not None else config.get("audio_offset_ms", 0.0)),
        marker=marker,
    )


def analyze_frames(csv_path: Path) -> dict:
    """检查 frames.csv：成片帧数、游戏帧号是否连续（不连续说明有游玩帧没录上）；规划回放的帧另计。"""
    rows = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append((int(row["index"]), int(row["game_frame"]), int(row["chapter_ticks"]), int(row["obs_ts_ns"])))
    except (OSError, KeyError, ValueError) as exc:
        return {"rows": 0, "error": str(exc)}
    # 规划回放的帧（游戏不推进、只重画画面）章节计时记为 -1，回放跟不上节拍时插件补的帧记为 -2：
    # 都不参与连续性检查，单独计数
    replay = sum(1 for r in rows if r[2] in (-1, -2))
    filled = sum(1 for r in rows if r[2] == -2)
    rows_all, rows = rows, [r for r in rows if r[2] >= 0]
    gaps, repeats = [], 0
    for (_, prev, _, _), (index, cur, _, _) in zip(rows, rows[1:]):
        if cur == prev:
            repeats += 1
        elif cur != prev + 1:
            gaps.append({"index": index, "from": prev, "to": cur, "missing": cur - prev - 1})
    return {
        "rows": len(rows_all),
        "replay_frames": replay,
        "replay_filled": filled,
        "first_game_frame": rows[0][1] if rows else None,
        "last_game_frame": rows[-1][1] if rows else None,
        "missing_frames": sum(g["missing"] for g in gaps if g["missing"] > 0),
        "gaps": gaps[:50],
        "repeats": repeats,
    }


def analyze_segments(csv_path: Path) -> dict:
    """segments.csv：插件把录像分成一段段连续推进的帧，每段开头量一次声音延迟（见插件说明）。"""
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError as exc:
        return {"segments": 0, "error": str(exc)}
    latencies = sorted(float(r["measured_ms"]) for r in rows if r.get("method") == "measured")
    return {
        "segments": len(rows),
        "measured": len(latencies),
        "latency_ms_median": latencies[len(latencies) // 2] if latencies else None,
        "latency_ms_range": [latencies[0], latencies[-1]] if latencies else None,
    }


class Recorder:
    """call 是向桥接发指令的函数（GameSession.bridge_call，或独立脚本里的 BridgeClient.call）；
    开局录制时先用菜单宏的连接，主控接管后换成 GameSession.bridge_call（录制状态在桥接与插件里，与连接无关）。"""

    def __init__(self, call: Callable[..., dict] | None, options: RecordOptions, log: Callable[[str], None] = print):
        self.call = call
        self.opts = options
        self.log = log
        self.started = False
        self.result: dict | None = None

    def clock(self) -> dict:
        return self.call("hello").get("clock") or {}

    def _plugin(self) -> tuple[dict, dict]:
        clock = self.clock()
        return clock, clock.get("plugin") or {}

    def start(self, timeout: float = 20.0) -> dict:
        directory = self.opts.dir.resolve()
        if not str(directory).isascii():
            raise RecordingError(f"录制目录只能用英文路径（OBS 插件用 ANSI 接口写文件）：{directory}")
        directory.mkdir(parents=True, exist_ok=True)
        clock = self.clock()
        if not clock.get("connected"):
            raise RecordingError("OBS 插件没有连上桥接的时钟端口：先运行 python tools/obs_setup.py launch 启动 OBS"
                                 "（插件构建见使用说明 4.4 节）")
        gamewindow.ensure_visible(self.log)  # 游戏窗口最小化时 OBS 采不到画面
        self.call("record", action="start", dir=str(directory), ffmpeg=self.opts.ffmpeg, video_args=self.opts.video_args,
                  audio_offset_ms=self.opts.audio_offset_ms, marker=self.opts.marker)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            clock, plugin = self._plugin()
            if plugin.get("state") == "failed":
                raise RecordingError(f"OBS 插件开始录制失败：{plugin.get('error')}")
            if plugin.get("state") == "recording" and clock.get("lockstep"):
                self.started = True
                self.log(f"[record] 开始录制：{directory}（ffmpeg {self.opts.video_args}；声音偏移 "
                         f"{self.opts.audio_offset_ms:g} ms{'；帧号条码' if self.opts.marker else ''}）")
                return clock
            time.sleep(0.2)
        raise RecordingError(f"等待 OBS 插件开始录制超时（{timeout:g} 秒）：{plugin}")

    def check_window(self) -> None:
        """录制中每次推进游戏前调用：游戏窗口被最小化了就不激活地还原（否则这段录像没有游戏画面）。"""
        if self.started:
            gamewindow.ensure_visible(self.log)

    def stop(self, timeout: float = 900.0) -> dict | None:
        """停止录制并等 ffmpeg 收尾；返回结果摘要（也写进 recording/recording.json）。"""
        if not self.started:
            return None
        self.started = False
        self.call("record", action="stop", marker=False)
        deadline = time.monotonic() + timeout
        clock, plugin = self._plugin()
        while time.monotonic() < deadline:
            if plugin.get("state") in ("finished", "failed") and not clock.get("lockstep"):
                break
            time.sleep(0.3)
            clock, plugin = self._plugin()
        else:
            self.log(f"[record] 等待 ffmpeg 收尾超时（{timeout:g} 秒），当前状态：{plugin}")
        directory = self.opts.dir.resolve()
        files = {p.name: p.stat().st_size for p in sorted(directory.iterdir()) if p.is_file()} if directory.exists() else {}
        result = {
            "state": plugin.get("state"),
            "error": plugin.get("error") or "",
            "frames": plugin.get("frames"),
            "tokens": plugin.get("tokens"),
            "late_ticks": plugin.get("late"),
            "dir": str(directory),
            "files": files,
            "check": analyze_frames(directory / "frames.csv"),
            "audio_sync": analyze_segments(directory / "segments.csv"),
            "options": {"video_args": self.opts.video_args, "audio_offset_ms": self.opts.audio_offset_ms,
                        "marker": self.opts.marker},
        }
        self.result = result
        try:
            (directory / "recording.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        check = result["check"]
        seconds = (result["frames"] or 0) / 60.0
        self.log(f"[record] 录制结束：{result['state']}，{result['frames']} 帧（{seconds:.1f} 秒），"
                 f"其中规划回放 {check.get('replay_frames') or 0} 帧（补帧 {check.get('replay_filled') or 0}），缺帧 {check.get('missing_frames')}，重复 {check.get('repeats')}"
                 + (f"，错误：{result['error']}" if result["error"] else "")
                 + (f"；成片 {directory / 'gameplay.mkv'}" if "gameplay.mkv" in files else ""))
        return result
