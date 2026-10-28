"""录制（M6）：让 OBS 锁步录制插件只录游戏真正推进过的帧。

流程：主控接管游戏后 start()：桥接把录制指令转给 OBS 插件，插件建录制输出并打开锁步——之后 OBS 每个视频
节拍向桥接要一帧，桥接推进一帧、画到屏幕上再回复，插件只把这些帧交给 ffmpeg；智能体思考、桥接空闲时不写。
结束时 stop()：插件关锁步、等 ffmpeg 编完，把画面与声音合成 gameplay.mp4（完整版，含规划回放）；
主控再从完整版里去掉规划回放的帧，生成只有真实游玩帧的 gameplay_pure.mp4（纯享版，见 purecut.py）。

前提：OBS 已用 tools/obs_setup.py 启动（CelesteAI 配置文件与场景集合），装了 celeste-lockstep 插件，
插件已经连上桥接的时钟端口（hello 的 clock.connected）。

输出目录（runs/<智能体>/<时间>/recording/）：
    gameplay.mp4   成片完整版（画面 + 声音，含规划回放）
    gameplay_pure.mp4  成片纯享版（只有真实游玩的帧）
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
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from . import gamewindow
from .purecut import make_pure
from .recording_files import finished_movie
from .recording_lifecycle import RecordingStop, pending_recording
from .config import REPO_ROOT

CONFIG = REPO_ROOT / "configs" / "recording.json"
LOCAL_CONFIG = REPO_ROOT / "configs" / "local.recording.json"   # 本机覆盖（不入库）
AUDIO_TRACKS = 2   # 独立游戏音频（game_nrt）的音轨数：完整版 audio.wav 与纯享版 audio_play.wav


class RecordingError(RuntimeError):
    pass


class RecordingOwnershipError(RecordingError):
    """A foreign live capture must not be stopped or released by this session."""


class RecordingHalt(RecordingError):
    """这一段录像失败、又没能另起一段：不能再推进游戏（推进的帧录不上）。"""


def next_segment_dir(directory: Path) -> Path:
    """同一运行目录里的下一段录像：recording → recording_2 → recording_3 …（跳过已有会话的目录）。"""
    directory = Path(directory)
    match = re.fullmatch(r'(.+)_(\d+)', directory.name)
    base, number = (match.group(1), int(match.group(2))) if match else (directory.name, 1)
    while True:
        number += 1
        candidate = directory.with_name(f'{base}_{number}')
        if not (candidate / 'session.json').exists():
            return candidate


def salvage_movie(directory: Path, ffmpeg: str, log: Callable[[str], None] = print) -> Path | None:
    """插件中途失败（例如独立音频某帧出错）时不合成成片，但画面 video.mkv 与声音 audio.wav 已正常关闭：
    照插件的做法合成 gameplay.mp4，保住失败之前录下的部分。失败返回 None（原文件不动）。"""
    directory = Path(directory)
    video, audio = directory / 'video.mkv', directory / 'audio.wav'
    if not (video.is_file() and audio.is_file() and video.stat().st_size > 0):
        return None
    partial, out = directory / 'gameplay.salvage.mp4', directory / 'gameplay.mp4'
    cmd = [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y', '-i', str(video), '-i', str(audio),
           '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k', '-movflags', '+faststart',
           str(partial)]
    try:
        with open(directory / 'ffmpeg_mux.log', 'w', encoding='utf-8') as stream:
            code = subprocess.run(cmd, stdout=stream, stderr=subprocess.STDOUT, timeout=1800).returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f'[record] 合成失败那段的成片出错：{exc}')
        return None
    if code != 0 or not partial.is_file():
        log(f'[record] 合成失败那段的成片出错（ffmpeg 返回 {code}，见 ffmpeg_mux.log）')
        return None
    partial.replace(out)
    log(f'[record] 插件没有合成成片：已用失败前录下的画面与声音合成 {out.name}')
    return out


def _has_files(directory: Path) -> bool:
    return directory.is_dir() and any(directory.iterdir())


def log_storage(saved: dict, log: Callable[[str], None] = print) -> None:
    """恢复目录只增不删：每次恢复或收尾时报告占用，方便用户自己决定何时整理。"""
    from .recording_recovery import storage_bytes
    try:
        size = storage_bytes(saved)
    except Exception:  # noqa: BLE001（只是提示）
        return
    if size:
        log(f'[record] 恢复目录 .recovery 当前占用 {size / 2**30:.2f} GiB（原件备份、各次采集、校验过的前缀与累计版本，'
            '主控不会自动删除）')


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


def analyze_frames(csv_path: Path, boundaries=None) -> dict:
    """检查 frames.csv：成片帧数、游戏帧号是否连续（不连续说明有游玩帧没录上）；规划回放的帧另计。"""
    rows, malformed = [], 0
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    rows.append((int(row["index"]), int(row["game_frame"]), int(row["chapter_ticks"]), int(row["obs_ts_ns"])))
                except (KeyError, TypeError, ValueError):
                    malformed += 1    # 插件写到一半就停了（失败或收尾时）的行：不算，单独计数
    except OSError as exc:
        return {"rows": 0, "error": str(exc)}
    # 规划回放的帧（游戏不推进、只重画画面）章节计时记为 -1，回放跟不上节拍时插件补的帧记为 -2：
    # 都不参与连续性检查，单独计数
    replay = sum(1 for r in rows if r[2] in (-1, -2))
    filled = sum(1 for r in rows if r[2] == -2)
    rows_all, rows = rows, [r for r in rows if r[2] >= 0]
    gaps, repeats, resets = [], 0, []
    boundary_indices = {item['index'] for item in boundaries or []}
    for (previous_index, prev, _, _), (index, cur, _, _) in zip(rows, rows[1:]):
        if cur <= prev and any(previous_index < boundary <= index for boundary in boundary_indices):
            resets.append({'index': index, 'from': prev, 'to': cur})
        elif cur == prev:
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
        **({'frame_resets': resets} if resets else {}),
        **({"malformed_rows": malformed} if malformed else {}),
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
        self._stop = RecordingStop()
        self.segments: list[dict] = []   # 本次运行里之前失败、已收尾的录像段（见 roll）
        self._recovery_start = None
        self._roll_pending = None

    def bind_run(self, directory: Path):
        directory = directory.resolve()
        if directory not in self.run_dirs:
            self.run_dirs.append(directory)

    def _session_data(self, state='recording'):
        options = asdict(self.opts)
        options['dir'] = str(self.opts.dir.resolve())
        payload = {'version': 1, 'id': self.session_id, 'state': state, 'options': options,
                   'runs': [str(p) for p in self.run_dirs]}
        if self._recovery_start is not None:
            payload['recovery_start'] = self._recovery_start
        return payload

    @staticmethod
    def capture_directory(saved):
        from .recording_recovery import capture_directory, RecoveryError
        try:
            return capture_directory(saved)
        except (RecoveryError, OSError, ValueError) as exc:
            raise RecordingError(f'录制恢复记录无效：{exc}') from exc

    def _save_session(self, state):
        manifest = self.opts.dir.resolve() / 'session.json'
        payload = self._session_data(state)
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
    def validate_release(saved, hello):
        clock = hello.get('clock') or {}
        plugin = clock.get('plugin') or {}
        active = clock.get('lockstep') or plugin.get('recording') is True or plugin.get('state') in {'recording', 'finishing'}
        expected = Recorder.capture_directory(saved) if saved else None
        actual = Path(plugin['dir']).resolve() if plugin.get('dir') else None
        if active and (expected is None or actual != expected):
            raise RecordingOwnershipError('OBS 正在采集或收尾另一段录像，不能交还该录制的游戏控制')

    @staticmethod
    def validate_resume(saved, hello):
        clock = hello.get('clock') or {}
        plugin = clock.get('plugin') or {}
        active = plugin.get('state') == 'recording' or plugin.get('recording') is True
        held = saved is not None and saved.get('state') in {'recording', 'recovering'}
        if pending_recording(saved) and not held:
            raise RecordingError('原录像正在收尾，请先完成收尾，不能恢复采集')
        if plugin.get('state') == 'finishing':
            raise RecordingError('OBS 正在收尾录像，请完成收尾后再开始采集')
        if active and not held:
            raise RecordingOwnershipError('OBS 正在录制其他会话，不能另开录制；请先收尾原录像')
        if not held:
            return False
        expected = Recorder.capture_directory(saved)
        actual = Path(plugin.get('dir') or '').resolve()
        if active and actual != expected:
            raise RecordingOwnershipError('OBS 正在录制其他会话，拒绝接续或替换它')
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
    def validate_recovery(saved, hello):
        """Only a stopped capture belonging to this logical recording can recover."""
        if not saved or saved.get('state') not in {'recording', 'recovering', 'finishing', 'failed', 'finished'}:
            raise RecordingError('没有可恢复的原录制会话；不会覆盖或另开录像')
        Recorder.validate_release(saved, hello)
        plugin = (hello.get('clock') or {}).get('plugin') or {}
        if plugin.get('state') == 'finishing':
            raise RecordingError('原编码器仍在收尾，保持暂停，完成后重试恢复')

    @classmethod
    def resume_mode(cls, saved, hello):
        """Choose live adoption or stopped-encoder recovery without side effects."""
        try:
            if cls.validate_resume(saved, hello):
                return 'live'
        except RecordingOwnershipError:
            raise
        except RecordingError:
            if not saved:
                raise
            cls.validate_recovery(saved, hello)
            return 'recover'
        if saved:
            cls.validate_recovery(saved, hello)
            return 'recover'
        return 'new'

    @classmethod
    def recover(cls, call, saved, run_dir, log=print, timeout=20.0):
        """Restart capture into a private chunk while retaining the logical ID/files."""
        from .recording_recovery import prepare, RecoveryError
        hello = call('hello')
        cls.validate_recovery(saved, hello)
        try:
            if cls.validate_resume(saved, hello):
                return cls.adopt(call, saved, run_dir, log)
        except RecordingOwnershipError:
            raise
        except RecordingError:
            pass
        if not (hello.get('clock') or {}).get('connected'):
            raise RecordingError('OBS 插件尚未连接；原录制文件保持，连接后重试恢复')
        recorder = cls(call, RecordOptions(**{**saved['options'], 'dir': Path(saved['options']['dir'])}), log)
        recorder.session_id = saved['id']
        recorder.run_dirs = [Path(p) for p in saved.get('runs', [])]
        recorder.bind_run(Path(run_dir))
        previous_request = saved.get('recovery_start')
        if previous_request and previous_request.get('phase') == 'requested':
            # A queued OBS start can outlive a lost acknowledgement. Only its
            # actual recording/failed state resolves that uncertainty.
            until = time.monotonic() + timeout
            while True:
                hello = call('hello')
                cls.validate_release(saved, hello)
                plugin = (hello.get('clock') or {}).get('plugin') or {}
                actual = Path(plugin['dir']).resolve() if plugin.get('dir') else None
                expected = cls.capture_directory(saved)
                if actual == expected and plugin.get('state') == 'recording':
                    return cls.adopt(call, saved, run_dir, log)
                if actual == expected and plugin.get('state') in {'failed', 'finished'}:
                    break
                if time.monotonic() >= until:
                    hello = cls._retire_lost_start(call, saved, expected, timeout, log)
                    break
                time.sleep(.2)
        clock = hello.get('clock') or {}
        plugin = clock.get('plugin') or {}
        if clock.get('lockstep') or plugin.get('recording') or hello.get('record_audio_active'):
            # validate_release above prevents a stop from touching foreign work.
            call('record', action='stop', marker=False)
            deadline = time.monotonic() + timeout
            while True:
                hello = call('hello')
                cls.validate_release(saved, hello)
                clock, plugin = hello.get('clock') or {}, (hello.get('clock') or {}).get('plugin') or {}
                if not clock.get('lockstep') and not plugin.get('recording') and plugin.get('state') in {'idle', 'finished', 'failed'}:
                    break
                if time.monotonic() >= deadline:
                    raise RecordingError('原编码器尚未停止，未替换文件或启动新捕获')
                time.sleep(.2)
        try:
            ledger = prepare(saved)
        except (RecoveryError, OSError, ValueError) as exc:
            raise RecordingError(f'原录像校验／恢复准备失败：{exc}') from exc
        capture = cls.capture_directory(saved)
        recorder._recovery_start = {'capture_dir': str(capture), 'phase': 'prepared'}
        recorder._save_session('recovering')
        try:
            recorder._start_capture(capture, timeout)
        except (OSError, TimeoutError) as exc:
            raise RecordingError('恢复采集的启动结果尚未确认，原录像和交接保留；请正常 --resume 重试，不重复 start') from exc
        except RecordingError:
            raise
        except RuntimeError as exc:
            raise RecordingError(f'恢复采集未能启动：{exc}；原录像保留，修复后正常 --resume 重试') from exc
        log(f'[record] 恢复原录像：{recorder.opts.dir}（会话 {recorder.session_id}，原有 {ledger.get("prefix_frames", 0)} 帧保留）')
        log_storage(saved, log)
        return recorder

    @staticmethod
    def _capture_idle(hello):
        clock = hello.get('clock') or {}
        plugin = clock.get('plugin') or {}
        return (clock.get('connected') is True and not clock.get('lockstep') and plugin.get('recording') is not True
                and plugin.get('state') in {'idle', 'finished', 'failed'})

    @classmethod
    def _retire_lost_start(cls, call, saved, expected, timeout, log):
        """A persisted start whose directory OBS never reported: resolve it without a second capture.

        Started (files exist, OBS idle now): the normal path closes that attempt
        and opens a new one. Never delivered: a stop is queued behind any start
        OBS might still hold (it also ends bridge audio left by a start that
        reached only the bridge), then the empty attempt is swapped for a fresh
        directory, so a retried start cannot overwrite or race the old one.
        Returns the latest hello."""
        from .recording_recovery import replace_unstarted, RecoveryError
        hello = call('hello')
        cls.validate_release(saved, hello)
        if not cls._capture_idle(hello):
            raise RecordingError('上次恢复启动的结果尚未确认：OBS 仍在采集、收尾或未连接，未重复 start。'
                                 '请确认 OBS 已打开并连上桥接、没有其他录像在进行，再正常 --resume 重试')
        if _has_files(expected):
            return hello  # The attempt did start and has ended; prepare closes it as a chunk.
        call('record', action='stop', marker=False)
        deadline = time.monotonic() + timeout
        while True:
            hello = call('hello')
            cls.validate_release(saved, hello)
            if cls._capture_idle(hello) and not hello.get('record_audio_active'):
                break
            if time.monotonic() >= deadline:
                raise RecordingError('上次恢复启动的结果尚未确认，OBS 尚未回到空闲，未重复 start；请稍后正常 --resume 重试')
            time.sleep(.2)
        plugin = (hello.get('clock') or {}).get('plugin') or {}
        actual = Path(plugin['dir']).resolve() if plugin.get('dir') else None
        if actual == expected or _has_files(expected):
            return hello  # A late start ran before the queued stop; treat it as a closed chunk.
        try:
            fresh = replace_unstarted(saved)
        except (RecoveryError, OSError) as exc:
            raise RecordingError(f'上次恢复启动未到达 OBS，但无法改用新的采集目录：{exc}；原录像保留，未重复 start') from exc
        log(f'[record] 上次恢复启动没有到达 OBS（采集目录一直为空、OBS 空闲）：改用新的采集目录 {fresh.name} 重新开始，'
            f'原空目录 {expected.name} 保留不用')
        return hello

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
    def finish_saved(cls, call, saved, run_dir, log=print, on_capture_stopped=None, *, on_encoder_finished=None):
        """用户放弃续跑时收尾；已经由 OBS 停下的同目录录像也可整理。
        on_capture_stopped：采集一停就调用（交还游戏），不等 ffmpeg 收尾与纯享版。"""
        hello = call('hello')
        cls.validate_release(saved, hello)
        plugin = (hello.get('clock') or {}).get('plugin') or {}
        if not plugin.get('dir') or Path(plugin['dir']).resolve() != cls.capture_directory(saved):
            raise RecordingError('OBS 当前录像与待收尾会话不匹配，未停止任何录像')
        recorder = cls(call, RecordOptions(**{**saved['options'], 'dir': Path(saved['options']['dir'])}), log)
        recorder.session_id = saved['id']
        recorder.run_dirs = [Path(p) for p in saved.get('runs', [])]
        recorder.bind_run(Path(run_dir))
        recorder.started = True
        if not (hello.get('clock') or {}).get('lockstep') and plugin.get('recording') is not True and plugin.get('state') in {'finishing', 'finished', 'failed'}:
            recorder._stop.requested = True
        return recorder.stop(on_capture_stopped=on_capture_stopped, on_encoder_finished=on_encoder_finished)

    def retain(self):
        """编码器和音频由 OBS/Mod 持有；只断开 Python 所有权，不发停止命令。"""
        if self._roll_pending is not None:
            raise RecordingError('录制恢复尚未确认，不能保留旧捕获的终态；请先重试恢复或收尾')
        hello = self.call('hello')
        if hello.get('frozen') is False:
            # 菜单与地图不冻结（桥接 1.0.7 起照常运行）：保留录制会把中断期间的菜单画面一直录进同一份录像
            raise RecordingError('游戏不在冻结的关卡里（菜单或地图照常运行），不保留录制')
        saved = self._session_data()
        self.validate_resume(saved, hello)
        from .recording_recovery import prefix_counts, RecoveryError
        try:
            prefix = prefix_counts(saved)
        except (RecoveryError, OSError, ValueError) as exc:
            raise RecordingError(f'原录制累计计量读取失败：{exc}') from exc
        self._save_session('recording')
        self.started = False
        self.log(f'[record] 保留录制会话等待续跑：{self.opts.dir}')
        return {'state': 'retained', 'dir': str(self.opts.dir.resolve()), 'id': self.session_id,
                'frames': prefix['frames'] + ((hello.get('clock', {}).get('plugin') or {}).get('frames') or 0), 'pending': True}

    def segment_failed(self) -> str | None:
        """这一段录像是否已经录不上画面：OBS 插件报失败、桥接的独立录像音频出错，或插件与桥接断开。
        返回原因；没问题返回 None。OBS 指向其他目录时拒绝推进，不操作它。
        OBS 断开或已重启时恢复不了采集：立即抛 RecordingHalt 并说明怎么处理，不先停止、等待收尾。"""
        if self._roll_pending is None and (not self.started or self._stop.requested):
            return None
        hello = self.call('hello')
        self.audio_diagnostics = hello.get('record_audio_diagnostics', self.audio_diagnostics)
        clock = hello.get('clock') or {}
        plugin = clock.get('plugin') or {}
        if clock.get('connected') is False:
            raise RecordingHalt('OBS 插件与桥接的时钟连接已断开（OBS 可能已关闭或崩溃）：游戏保持冻结，这次动作未执行。'
                                'OBS 重新连上且原采集仍在时可直接继续试玩；OBS 已重启或采集已停止时，'
                                '请中断主控后正常 --resume，以原会话恢复')
        restarted = (plugin.get('state') == 'idle' and not plugin.get('dir') and not clock.get('lockstep')
                     and plugin.get('recording') is not True)
        if self._roll_pending is not None:
            if not self._roll_pending['closed'] and restarted:
                # The failed capture's encoder state went away with OBS; it can
                # only be closed and recovered from the files by a new owner.
                raise RecordingHalt('OBS 已重启，失败的那段采集在本次运行里无法收尾：游戏保持冻结，这次动作未执行。'
                                    '请中断主控后正常 --resume，以原会话恢复（已录下的文件都保留）')
            return self._roll_pending['reason']
        if plugin.get('dir') and Path(plugin['dir']).resolve() != self.capture_directory(self._session_data()):
            raise RecordingOwnershipError('OBS 已指向其他录制，当前会话没有可核验的采集；未操作其他录像')
        if plugin.get('state') == 'failed':
            return plugin.get('error') or 'OBS 插件报告录制失败'
        active = plugin.get('state') == 'recording' or plugin.get('recording') is True
        if active and not plugin.get('dir'):
            raise RecordingOwnershipError('OBS 正在采集但未报告归属目录；拒绝推进或操作未知录像')
        if not active and (plugin.get('state') == 'idle' or not plugin.get('dir')):
            raise RecordingHalt('OBS 编码器已退出或重启，当前没有可核验的采集；请中断主控后正常 --resume，以原会话恢复。动作未执行')
        if not active or not clock.get('lockstep'):
            return '原录制采集或锁步已停止，需要恢复原会话'
        if self.opts.audio_source == 'game_nrt' and hello.get('record_audio_error'):
            return '独立录像音频出错：' + str(hello['record_audio_error'])
        return None

    def roll(self, reason: str) -> dict | None:
        """Close failed capture and recover the same logical recording before more input."""
        self.log(f'[record] 录像捕获失败（{reason}）：保留原录像，恢复同一录制会话')
        if self._roll_pending is None:
            self._roll_pending = {'reason': reason, 'closed': False, 'old': None}
        pending = self._roll_pending
        if not pending['closed']:
            try:
                old = self.stop(timeout=300.0, _rolling=True)
            except Exception as exc:  # noqa: BLE001
                raise RecordingHalt(f'收尾失败的那段录像出错：{exc}') from exc
            if old is not None and old.get('pending'):
                raise RecordingHalt('失败的捕获还没收尾完，不能恢复采集')
            pending.update(closed=True, old=old)
            self.segments.append({'dir': str(self.opts.dir.resolve()), 'state': (old or {}).get('state'),
                                  'frames': (old or {}).get('frames'), 'reason': pending['reason'],
                                  'salvaged': (old or {}).get('salvaged')})
        old = pending['old']
        try:
            # recover may have persisted a requested start before its ACK was lost.
            # The disk manifest and ledger own that new capture, not this object's
            # cached stop/result from the capture we just closed.
            resumed = self.recover(self.call, self._roll_saved(),
                                   self.run_dirs[-1] if self.run_dirs else self.opts.dir.parent, self.log)
        except Exception as exc:  # noqa: BLE001
            raise RecordingHalt(f'恢复原录像失败：{exc}') from exc
        previous = self.segments
        self.__dict__.update(resumed.__dict__)
        self.segments = previous
        return old

    def _roll_saved(self):
        try:
            saved = json.loads((self.opts.dir / 'session.json').read_text(encoding='utf-8'))
            if saved.get('id') != self.session_id:
                raise ValueError('录制会话身份不匹配')
            return saved
        except (OSError, ValueError, TypeError) as exc:
            raise RecordingError(f'无法读取恢复中的录制会话：{exc}') from exc

    def clock(self) -> dict:
        hello = self.call("hello")
        self.audio_diagnostics = hello.get('record_audio_diagnostics', self.audio_diagnostics)
        return hello.get("clock") or {}

    def _plugin(self, allow_previous_terminal=False) -> tuple[dict, dict]:
        clock = self.clock()
        plugin = clock.get("plugin") or {}
        if plugin.get('dir') and Path(plugin['dir']).resolve() != self.capture_directory(self._session_data()):
            if (allow_previous_terminal and not clock.get('lockstep') and plugin.get('recording') is not True
                    and plugin.get('state') in {'idle', 'finished', 'failed'}):
                # Start is queued asynchronously in OBS. Its first reply can still describe the old job.
                return clock, {}
            raise RecordingOwnershipError('OBS 录制目录已变化，拒绝停止或归档其他会话的录像')
        return clock, plugin

    def start(self, timeout: float = 20.0) -> dict:
        if self.started or self._stop.requested or self.result is not None:
            raise RecordingError('录制对象已经使用，请为新录像创建新的 Recorder')
        directory = self.opts.dir.resolve()
        if not str(directory).isascii():
            raise RecordingError(f"录制目录只能用英文路径（OBS 插件用 ANSI 接口写文件）：{directory}")
        directory.mkdir(parents=True, exist_ok=True)
        hello = self.call("hello")
        clock = hello.get("clock") or {}
        self.validate_resume(None, hello)
        if (directory / 'session.json').exists():
            raise RecordingError('录制目录已有会话，请另选目录或接续原录像')
        return self._start_capture(directory, timeout, hello=hello)

    def _start_capture(self, directory, timeout, *, hello=None):
        hello = hello if hello is not None else self.call('hello')
        self.validate_resume(None, hello)
        clock = hello.get('clock') or {}
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
        if self._recovery_start is not None:
            self._recovery_start.update(phase='requested', requested_wall=time.time())
            self._save_session('recovering')
        try:
            self.call("record", action="start", dir=str(directory), ffmpeg=self.opts.ffmpeg, video_args=self.opts.video_args,
                      audio_offset_ms=self.opts.audio_offset_ms, marker=self.opts.marker,
                      continuous_music=self.opts.continuous_music,
                      audio_source=self.opts.audio_source,
                      audio_sync="fixed" if self.opts.continuous_music else "onset")
        except RuntimeError:
            # BridgeClient reports a negative bridge reply as RuntimeError.
            # No OBS start was queued if the bridge rejected its audio setup.
            if self._recovery_start is not None:
                self._recovery_start['phase'] = 'prepared'
                self._save_session('recovering')
            raise
        deadline, plugin = time.monotonic() + timeout, {}
        while time.monotonic() < deadline:
            clock, plugin = self._plugin(allow_previous_terminal=True)
            if plugin.get("state") == "failed":
                self.call("record", action="stop")  # release the secondary mixer after a failed plugin start
                raise RecordingError(f"OBS 插件开始录制失败：{plugin.get('error')}")
            if plugin.get("state") == "recording" and clock.get("lockstep"):
                self.started = True
                self._recovery_start = None
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

    def stop(self, timeout: float = 900.0, on_capture_stopped: Callable[[], None] | None = None,
             *, on_encoder_finished: Callable[[], None] | None = None,
             _rolling=False, _closed_capture=None) -> dict | None:
        """停止录制并等 ffmpeg 收尾；返回结果摘要（也写进 recording/recording.json）。"""
        if self._roll_pending is not None and self._roll_pending['closed'] and not _rolling:
            saved = self._roll_saved()
            hello = self.call('hello')
            self.validate_release(saved, hello)
            plugin = (hello.get('clock') or {}).get('plugin') or {}
            request = saved.get('recovery_start') or {}
            actual = Path(plugin['dir']).resolve() if plugin.get('dir') else None
            if (request.get('phase') == 'requested'
                    and (actual != self.capture_directory(saved)
                         or plugin.get('state') not in {'recording', 'finishing', 'finished', 'failed'})):
                raise RecordingError('恢复启动结果尚未确认，不能用旧捕获的收尾结果代替；保持交接，稍后重试收尾')
            clock = hello.get('clock') or {}
            capture = self.capture_directory(saved)
            inactive = (not clock.get('lockstep') and plugin.get('recording') is not True
                        and plugin.get('state') not in {'recording', 'finishing'}
                        and not hello.get('record_audio_active'))
            empty = not any((capture / name).exists() and (capture / name).stat().st_size
                            for name in ('video.mkv', 'audio.wav', 'audio_play.wav', 'frames.csv', 'segments.csv'))
            # finish_saved validates the ledger capture and polls actual OBS state.
            # It cannot reuse the old capture's cached successful stop. An unknown
            # queued start remains pending instead of being reported as cleaned up.
            if request.get('phase') == 'prepared' and capture != self.opts.dir.resolve() and inactive and empty:
                # A definitively rejected start never owned an encoder at this
                # empty destination. Publish the closed prefix without stopping
                # whichever terminal capture OBS still happens to describe.
                ending = Recorder(self.call, self.opts, self.log)
                ending.session_id, ending.run_dirs = self.session_id, list(self.run_dirs)
                ending.segments = list(self.segments)
                ending._recovery_start = request
                ending._stop.requested = True
                result = ending.stop(timeout, on_capture_stopped,
                                     on_encoder_finished=on_encoder_finished,
                                     _closed_capture=(clock, {'state': 'finished', 'frames': 0}))
            else:
                result = self.finish_saved(self.call, saved,
                                           self.run_dirs[-1] if self.run_dirs else self.opts.dir.parent,
                                           self.log, on_capture_stopped, on_encoder_finished=on_encoder_finished)
            if result and not result.get('pending'):
                self._roll_pending = None
                self._recovery_start = None
                self.started = False
                self._stop = RecordingStop()
                self._stop.requested = True
                self._stop.encoder_notified = on_encoder_finished is not None
                self.result = result
            return result
        if self.result and self.result.get('state') in {'finished', 'failed'}:
            self._stop.notify_encoder(on_encoder_finished)
            return self.result
        if not self.started and not self._stop.requested:
            return None
        if not self._stop.requested:
            # A failed command must leave ownership intact so callers can retry cleanup.
            self._plugin()  # Recheck ownership before the stop side effect, not only when adopting.
            self.call("record", action="stop", marker=False)
            self._stop.requested = True
        deadline = time.monotonic() + timeout
        clock, plugin = _closed_capture if _closed_capture is not None else self._plugin()
        while True:
            complete = self._stop.observe(clock, plugin)
            self.started = not self._stop.capture_stopped
            self._stop.notify(on_capture_stopped)
            if complete:
                break
            if clock.get("connected") is False:
                # OBS 关了或崩溃：插件状态停在断开前的那一刻，再等也不会变；留作未收尾的录像（续跑或控制台处理）
                self.log(f"[record] OBS 插件已断开，录像无法收尾，留待续跑或控制台处理；最后的状态：{plugin}")
                break
            if (self._stop.capture_stopped and plugin.get('state') == 'idle' and not plugin.get('dir')
                    and not clock.get('lockstep')):
                # A fresh OBS reports idle without a directory: the encoder job and
                # its terminal state are gone, so waiting for finished cannot help.
                self.log('[record] OBS 已重启，原编码器的收尾状态已丢失：录像留作未收尾，正常 --resume 时以原会话恢复')
                break
            if time.monotonic() >= deadline:
                self.log(f"[record] 等待 ffmpeg 收尾超时（{timeout:g} 秒），当前状态：{plugin}")
                break
            time.sleep(0.3)
            clock, plugin = self._plugin()
        if complete:
            # No bridge calls below this boundary: the game may now exit while
            # recovery, muxing and pure-cut processing continue offline.
            if on_encoder_finished is not None:
                self._save_session('finishing')
            self._stop.notify_encoder(on_encoder_finished)
        directory = self.opts.dir.resolve()
        recovered = None
        if complete and self.capture_directory(self._session_data()) != directory:
            from .recording_recovery import finalize, RecoveryError
            # Capture already stopped; persisting this phase allows a new owner
            # to finish interrupted media publication without touching OBS.
            self._save_session('finishing')
            try:
                recovered = finalize(self._session_data('finishing'))
            except (RecoveryError, OSError, ValueError) as exc:
                raise RecordingError(f'录制采集已停止，但累计文件发布未完成：{exc}；保留原文件，可重试收尾') from exc
            log_storage(self._session_data('finishing'), self.log)
        files = {p.name: p.stat().st_size for p in sorted(directory.iterdir()) if p.is_file()} if directory.exists() else {}
        result = {
            "state": plugin.get("state") if complete else ('finishing' if self._stop.capture_stopped else 'recording'),
            "pending": not complete,
            "error": plugin.get("error") or "",
            "frames": recovered['frames'] if recovered is not None else plugin.get("frames"),
            "tokens": plugin.get("tokens"),
            "late_ticks": plugin.get("late"),
            "dir": str(directory),
            "files": files,
            "check": analyze_frames(directory / "frames.csv", recovered.get('boundaries') if recovered else None),
            "audio_sync": ({"method": "game_nrt", "sample_rate": 48000, "samples_per_frame": 800,
                            "tracks": {"audio.wav": "完整版：规划回放帧里音乐继续（音效暂停）",
                                       "audio_play.wav": "纯享版：只随真实游玩帧推进"}}
                           if self.opts.audio_source == "game_nrt" else analyze_segments(directory / "segments.csv")),
            "audio_diagnostics": self.audio_diagnostics,
            "options": {"video_args": self.opts.video_args, "audio_offset_ms": self.opts.audio_offset_ms,
                        "marker": self.opts.marker, "continuous_music": self.opts.continuous_music,
                        "audio_source": self.opts.audio_source},
        }
        if self.segments:
            result["previous_segments"] = list(self.segments)
        if recovered is not None:
            result['recovery'] = recovered
        movie = finished_movie(directory)
        if recovered is not None and result['frames']:
            # The plugin muxes only its private chunk. Rebuild the logical movie
            # from the validated cumulative files rather than reuse an old MP4.
            movie = salvage_movie(directory, self.opts.ffmpeg, self.log)
            if movie is None:
                raise RecordingError('累计录像已发布，但成片合成失败；原始文件保持，可重试收尾')
            if result['state'] == 'failed':
                result['salvaged'] = str(movie)
        if result["state"] == "failed" and complete and movie is None and (result["frames"] or 0) > 0:
            movie = salvage_movie(directory, self.opts.ffmpeg, self.log)
            if movie is not None:
                result["salvaged"] = str(movie)
                result['files'] = {p.name: p.stat().st_size for p in sorted(directory.iterdir()) if p.is_file()}
        if movie is not None and (result["state"] == "finished" or result.get("salvaged")):
            self.log("[record] 生成纯享版（去掉规划回放的帧）……")
            result["pure"] = make_pure(directory, self.opts.ffmpeg, self.opts.video_args, self.log)
            result['files'] = {p.name:p.stat().st_size for p in sorted(directory.iterdir()) if p.is_file()}
        # Only observed terminal states close the manifest; a timed-out encoder is still pending.
        if _closed_capture is not None and complete:
            self._recovery_start = None
        self._save_session(result['state'] if complete else ('finishing' if self._stop.capture_stopped else 'recording'))
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
                 + (f"；成片 {movie}" if movie is not None else ""))
        pure = result.get("pure") or {}
        if pure.get("state") == "finished":
            self.log(f"[record] 纯享版：{pure['frames']} 帧（{pure['seconds']:.1f} 秒，去掉规划回放 {pure['removed']} 帧）；{pure['file']}")
        return result
