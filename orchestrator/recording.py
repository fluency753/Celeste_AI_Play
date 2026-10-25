"""录制（M6）：让 OBS 锁步录制插件只录游戏真正推进过的帧。

流程：主控接管游戏后 start()：桥接把录制指令转给 OBS 插件，插件建录制输出并打开锁步——之后 OBS 每个视频
节拍向桥接要一帧，桥接推进一帧、画到屏幕上再回复，插件只把这些帧交给 ffmpeg；智能体思考、桥接空闲时不写。
结束时 stop()：插件关锁步、等 ffmpeg 编完，把画面与声音合成 gameplay.mkv（完整版，含规划回放）；
主控再从完整版里去掉规划回放的帧，生成只有真实游玩帧的 gameplay_pure.mkv（纯享版，见 purecut.py）。

前提：OBS 已用 tools/obs_setup.py 启动（CelesteAI 配置文件与场景集合），装了 celeste-lockstep 插件，
插件已经连上桥接的时钟端口（hello 的 clock.connected）。

输出目录（runs/<智能体>/<时间>/recording/）：
    gameplay.mkv   成片完整版（画面 + 声音，含规划回放）
    gameplay_pure.mkv  成片纯享版（只有真实游玩的帧）
    video.mkv      只有画面
    audio.wav      只有声音（32 位浮点立体声，每个成片帧 1/60 秒；独立游戏音频时规划回放帧里音乐继续、音效暂停）
    audio_play.wav 纯享版音轨（独立游戏音频才有：只随真实游玩帧推进，纯享版直接用它）
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
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from . import gamewindow
from .purecut import make_pure
from .config import REPO_ROOT

CONFIG = REPO_ROOT / "configs" / "recording.json"
LOCAL_CONFIG = REPO_ROOT / "configs" / "local.recording.json"   # 本机覆盖（不入库）
AUDIO_TRACKS = 2   # 独立游戏音频（game_nrt）的音轨数：完整版 audio.wav 与纯享版 audio_play.wav


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
    continuous_music: bool = False
    audio_source: str = "obs_mix"


def options_from(run_dir: Path, ffmpeg: str | None = None, video_args: str | None = None,
                 audio_offset_ms: float | None = None, marker: bool = False,
                 continuous_music: bool | None = None) -> RecordOptions:
    config = load_config()
    return RecordOptions(
        dir=run_dir / "recording",
        ffmpeg=find_ffmpeg(ffmpeg),
        video_args=video_args or config.get("video_args") or "",
        audio_offset_ms=float(audio_offset_ms if audio_offset_ms is not None else config.get("audio_offset_ms", 0.0)),
        marker=marker,
        continuous_music=bool(config.get("continuous_music", False) if continuous_music is None else continuous_music),
        audio_source=str(config.get("audio_source", "obs_mix")),
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
        self.audio_diagnostics = None
        self.session_id = uuid.uuid4().hex
        self.run_dirs = []

    def bind_run(self, directory: Path):
        directory = directory.resolve()
        if directory not in self.run_dirs:
            self.run_dirs.append(directory)

    def _save_session(self, state):
        options = asdict(self.opts)
        options['dir'] = str(self.opts.dir.resolve())
        manifest = self.opts.dir.resolve() / 'session.json'
        payload = {'version': 1, 'id': self.session_id, 'state': state, 'options': options,
                   'runs': [str(p) for p in self.run_dirs]}
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(manifest)
        for directory in self.run_dirs:
            if not directory.is_dir():
                continue
            pointer = directory / 'recording_session.json'
            temporary = pointer.with_suffix('.tmp')
            temporary.write_text(json.dumps({'id': self.session_id, 'manifest': str(manifest)}), encoding='utf-8')
            temporary.replace(pointer)

    @staticmethod
    def link_saved(saved, directory):
        pointer = Path(directory) / 'recording_session.json'
        temporary = pointer.with_suffix('.tmp')
        temporary.write_text(json.dumps({'id': saved['id'], 'manifest': str(Path(saved['options']['dir']) / 'session.json')}), encoding='utf-8')
        temporary.replace(pointer)

    @staticmethod
    def saved_session(run_dir):
        pointer = Path(run_dir) / 'recording_session.json'
        if not pointer.exists():
            return None
        try:
            link = json.loads(pointer.read_text(encoding='utf-8'))
            manifest = json.loads(Path(link['manifest']).read_text(encoding='utf-8'))
            if manifest['version'] != 1 or manifest['id'] != link['id']:
                raise ValueError('录制会话身份不匹配')
            return manifest
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RecordingError(f'无法读取原录制会话：{exc}') from exc

    @staticmethod
    def validate_resume(saved, hello):
        clock = hello.get('clock') or {}
        plugin = clock.get('plugin') or {}
        active = plugin.get('state') == 'recording' or plugin.get('recording') is True
        held = saved is not None and saved.get('state') == 'recording'
        if active and not held:
            raise RecordingError('OBS 正在录制其他会话，不能另开录制；请先收尾原录像')
        if not held:
            return False
        expected = Path(saved['options']['dir']).resolve()
        actual = Path(plugin.get('dir') or '').resolve()
        if not active or not clock.get('connected') or not clock.get('lockstep') or not plugin.get('dir') or actual != expected:
            raise RecordingError('原录像已断开或目录不匹配，拒绝覆盖或悄悄另起录像')
        if saved['options']['audio_source'] == 'game_nrt' and not hello.get('record_audio_active'):
            raise RecordingError('原录像的独立音频已停止，不能无缝续录')
        return True

    @classmethod
    def adopt(cls, call, saved, run_dir, log=print):
        if not cls.validate_resume(saved, call('hello')):
            raise RecordingError('原录制会话已经结束，不能继续写入')
        options = {**saved['options'], 'dir': Path(saved['options']['dir'])}
        recorder = cls(call, RecordOptions(**options), log)
        recorder.session_id = saved['id']
        recorder.run_dirs = [Path(p) for p in saved.get('runs', [])]
        recorder.bind_run(Path(run_dir))
        recorder.started = True
        recorder._save_session('recording')
        log(f'[record] 接续原录像：{recorder.opts.dir}（不重启编码器或音频，不重置帧号）')
        return recorder

    @staticmethod
    def abandon_saved(saved, reason):
        """原录像接不上也收不了尾（OBS 关闭或崩溃、独立音频停了等）：把会话标成 abandoned，不再等它续接，
        之后续跑另起一段录像，控制台也能照常交还游戏。已经写下的文件原样留在录像目录里。"""
        manifest = Path(saved['options']['dir']) / 'session.json'
        payload = {**saved, 'state': 'abandoned', 'abandoned_reason': str(reason)}
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(manifest)

    @classmethod
    def finish_saved(cls, call, saved, run_dir, log=print, on_capture_stopped=None):
        """用户放弃续跑时收尾；已经由 OBS 停下的同目录录像也可整理。
        on_capture_stopped：采集一停就调用（交还游戏），不等 ffmpeg 收尾与纯享版。"""
        hello = call('hello')
        plugin = (hello.get('clock') or {}).get('plugin') or {}
        if not plugin.get('dir') or Path(plugin['dir']).resolve() != Path(saved['options']['dir']).resolve():
            raise RecordingError('OBS 当前录像与待收尾会话不匹配，未停止任何录像')
        recorder = cls(call, RecordOptions(**{**saved['options'], 'dir': Path(saved['options']['dir'])}), log)
        recorder.session_id = saved['id']
        recorder.run_dirs = [Path(p) for p in saved.get('runs', [])]
        recorder.bind_run(Path(run_dir))
        recorder.started = True
        return recorder.stop(on_capture_stopped=on_capture_stopped)

    def retain(self):
        """编码器和音频由 OBS/Mod 持有；只断开 Python 所有权，不发停止命令。"""
        hello = self.call('hello')
        if hello.get('frozen') is False:
            # 菜单与地图不冻结（桥接 1.0.7 起照常运行）：保留录制会把中断期间的菜单画面一直录进同一份录像
            raise RecordingError('游戏不在冻结的关卡里（菜单或地图照常运行），不保留录制')
        saved = {'state': 'recording', 'options': {'dir': str(self.opts.dir), 'audio_source': self.opts.audio_source}}
        self.validate_resume(saved, hello)
        self._save_session('recording')
        self.started = False
        self.log(f'[record] 保留录制会话等待续跑：{self.opts.dir}')
        return {'state': 'retained', 'dir': str(self.opts.dir.resolve()), 'id': self.session_id,
                'frames': (hello.get('clock', {}).get('plugin') or {}).get('frames'), 'pending': True}

    def clock(self) -> dict:
        hello = self.call("hello")
        self.audio_diagnostics = hello.get('record_audio_diagnostics', self.audio_diagnostics)
        return hello.get("clock") or {}

    def _plugin(self) -> tuple[dict, dict]:
        clock = self.clock()
        return clock, clock.get("plugin") or {}

    def start(self, timeout: float = 20.0) -> dict:
        directory = self.opts.dir.resolve()
        if not str(directory).isascii():
            raise RecordingError(f"录制目录只能用英文路径（OBS 插件用 ANSI 接口写文件）：{directory}")
        directory.mkdir(parents=True, exist_ok=True)
        hello = self.call("hello")
        clock = hello.get("clock") or {}
        self.validate_resume(None, hello)
        if (directory / 'session.json').exists():
            raise RecordingError('录制目录已有会话，请另选目录或接续原录像')
        if self.opts.audio_source not in ("obs_mix", "game_nrt"):
            raise RecordingError(f"未知录音来源：{self.opts.audio_source}")
        if self.opts.audio_source == "game_nrt" and (not hello.get("record_audio_nrt")
                                                      or hello.get("record_audio_tracks") != AUDIO_TRACKS):
            raise RecordingError("独立录像音频需要更新桥接 Mod 和 OBS 锁步插件（完整版与纯享版两条音轨：桥接 0.12.7 起支持）")
        if self.opts.audio_source == "game_nrt" and (not (clock.get("plugin") or {}).get("audio_nrt")
                                                      or (clock.get("plugin") or {}).get("audio_tracks") != AUDIO_TRACKS):
            raise RecordingError("OBS 锁步插件尚不支持独立录像音频的两条音轨，请更新插件并重启 OBS")
        if not clock.get("connected"):
            raise RecordingError("OBS 插件没有连上桥接的时钟端口：先运行 python tools/obs_setup.py launch 启动 OBS"
                                 "（插件构建见使用说明 4.4 节）")
        gamewindow.ensure_visible(self.log)  # 游戏窗口最小化时 OBS 采不到画面
        self.call("record", action="start", dir=str(directory), ffmpeg=self.opts.ffmpeg, video_args=self.opts.video_args,
                  audio_offset_ms=self.opts.audio_offset_ms, marker=self.opts.marker,
                  continuous_music=self.opts.continuous_music,
                  audio_source=self.opts.audio_source,
                  audio_sync="fixed" if self.opts.continuous_music else "onset")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            clock, plugin = self._plugin()
            if plugin.get("state") == "failed":
                self.call("record", action="stop")  # release the secondary mixer after a failed plugin start
                raise RecordingError(f"OBS 插件开始录制失败：{plugin.get('error')}")
            if plugin.get("state") == "recording" and clock.get("lockstep"):
                self.started = True
                try:
                    self._save_session('recording')
                except Exception:
                    self.call('record', action='stop')
                    self.started = False
                    raise
                audio = "独立游戏音频（不输出到声卡）" if self.opts.audio_source == "game_nrt" else f"声音偏移 {self.opts.audio_offset_ms:g} ms"
                self.log(f"[record] 开始录制：{directory}（ffmpeg {self.opts.video_args}；{audio}"
                         f"{'；帧号条码' if self.opts.marker else ''}）")
                return clock
            time.sleep(0.2)
        self.call("record", action="stop")
        raise RecordingError(f"等待 OBS 插件开始录制超时（{timeout:g} 秒）：{plugin}")

    def check_window(self) -> None:
        """录制中每次推进游戏前调用：游戏窗口被最小化了就不激活地还原（否则这段录像没有游戏画面）。"""
        if self.started:
            gamewindow.ensure_visible(self.log)

    def stop(self, timeout: float = 900.0, on_capture_stopped: Callable[[], None] | None = None) -> dict | None:
        """停止录制并等 ffmpeg 收尾；返回结果摘要（也写进 recording/recording.json）。"""
        if not self.started:
            return None
        self.started = False
        self.call("record", action="stop", marker=False)
        deadline = time.monotonic() + timeout
        clock, plugin = self._plugin()
        released = False
        while time.monotonic() < deadline:
            if (not released and on_capture_stopped is not None and not clock.get('lockstep')
                    and plugin.get('state') in {'idle','finishing','finished','failed'}
                    and plugin.get('recording') is not True):
                on_capture_stopped()
                released = True
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
            "audio_sync": ({"method": "game_nrt", "sample_rate": 48000, "samples_per_frame": 800,
                            "tracks": {"audio.wav": "完整版：规划回放帧里音乐继续（音效暂停）",
                                       "audio_play.wav": "纯享版：只随真实游玩帧推进"}}
                           if self.opts.audio_source == "game_nrt" else analyze_segments(directory / "segments.csv")),
            "audio_diagnostics": self.audio_diagnostics,
            "options": {"video_args": self.opts.video_args, "audio_offset_ms": self.opts.audio_offset_ms,
                        "marker": self.opts.marker, "continuous_music": self.opts.continuous_music,
                        "audio_source": self.opts.audio_source},
        }
        if result["state"] == "finished" and "gameplay.mkv" in files:
            self.log("[record] 生成纯享版（去掉规划回放的帧）……")
            result["pure"] = make_pure(directory, self.opts.ffmpeg, self.opts.video_args, self.log)
        self.result = result
        self._save_session(result['state'] or 'failed')
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
        pure = result.get("pure") or {}
        if pure.get("state") == "finished":
            self.log(f"[record] 纯享版：{pure['frames']} 帧（{pure['seconds']:.1f} 秒，去掉规划回放 {pure['removed']} 帧）；{pure['file']}")
        return result
