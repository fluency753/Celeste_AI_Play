"""纯享版录像：从完整版里去掉规划回放的帧，只留真实游玩的帧。

完整版 gameplay.mkv 是插件录下的全部成片帧：真实游玩（游戏推进）的帧，以及规划回放的帧（游戏不推进、只把推演过的
路线画进画面；frames.csv 里章节计时记 -1，回放跟不上节拍时插件补的帧记 -2）。纯享版 gameplay_pure.mkv 只留章节计时
不小于 0 的帧：
- 画面：按 frames.csv 的行号（就是 video.mkv 的帧序号）挑帧，重新编码（编码参数与录制相同）；
- 声音：audio.wav 里每个成片帧正好 1/60 秒（800 个样本），按同样的行号裁剪；去掉回放之后，前后两段游玩之间的音乐
  不再连续，接缝处前一段结尾淡出、后一段开头淡入各 5 毫秒，去掉爆音。

录制结束时主控自动生成；已有的录像可以用 python tools/make_pure.py <recording 目录> 补生成。
"""
from __future__ import annotations

import csv
import os
import shlex
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import Callable

import numpy as np

SAMPLES_PER_FRAME = 800       # 48000 / 60
FADE_SAMPLES = 240            # 5 毫秒
CHUNK_FRAMES = 3600           # 声音每次处理一分钟


def read_ticks(csv_path: Path) -> list[int]:
    """frames.csv 每一行的章节计时（按 index 排好，index 必须连续）。"""
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = [(int(r["index"]), int(r["chapter_ticks"])) for r in csv.DictReader(f)]
    rows.sort()
    if any(i != k for k, (i, _) in enumerate(rows)):
        raise ValueError("frames.csv 的 index 不连续")
    return [t for _, t in rows]


def keep_ranges(ticks: list[int]) -> list[tuple[int, int]]:
    """要保留的帧（章节计时 ≥ 0）连成的区间 [首帧, 末帧]。"""
    out: list[tuple[int, int]] = []
    start = None
    for i, t in enumerate(ticks):
        if t >= 0 and start is None:
            start = i
        elif t < 0 and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(ticks) - 1))
    return out


def _wav_data(path: Path) -> tuple[int, int]:
    """WAV 的 data 块：返回 (数据起点字节, 数据字节数)。"""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"不是 WAV 文件：{path}")
        while True:
            chunk = f.read(8)
            if len(chunk) < 8:
                raise ValueError(f"WAV 里没有 data 块：{path}")
            cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            if cid == b"data":
                start = f.tell()
                total = os.path.getsize(path) - start
                return start, min(size, total) if size else total
            f.seek(size + (size & 1), 1)


def _wav_header(data_bytes: int, rate: int = 48000, channels: int = 2) -> bytes:
    """32 位浮点 PCM 的 WAV 头（与插件写的 audio.wav 相同格式）。"""
    block = channels * 4
    fmt = struct.pack("<HHIIHH", 3, channels, rate, rate * block, block, 32)
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", data_bytes))


def cut_audio(src: Path, dst: Path, ranges: list[tuple[int, int]]) -> int:
    """按帧区间裁剪 audio.wav（每帧 800 个样本），区间之间淡出淡入；返回写出的帧数。"""
    offset, size = _wav_data(src)
    samples = np.memmap(src, dtype="<f4", mode="r", offset=offset, shape=(size // 8, 2))
    frames_out = sum(b - a + 1 for a, b in ranges)
    ramp = np.linspace(0.0, 1.0, FADE_SAMPLES, dtype=np.float32)[:, None]
    with open(dst, "wb") as out:
        out.write(_wav_header(frames_out * SAMPLES_PER_FRAME * 8))
        for k, (a, b) in enumerate(ranges):
            for c0 in range(a, b + 1, CHUNK_FRAMES):
                c1 = min(b, c0 + CHUNK_FRAMES - 1)
                s0, s1 = c0 * SAMPLES_PER_FRAME, (c1 + 1) * SAMPLES_PER_FRAME
                block = np.zeros((s1 - s0, 2), dtype=np.float32)
                have = max(0, min(s1, len(samples)) - s0)
                if have:
                    block[:have] = samples[s0:s0 + have]
                if k > 0 and c0 == a:                       # 这一段开头（前面去掉了回放）：淡入
                    block[:FADE_SAMPLES] *= ramp
                if k < len(ranges) - 1 and c1 == b:         # 这一段结尾（后面去掉了回放）：淡出
                    block[-FADE_SAMPLES:] *= ramp[::-1]
                out.write(block.astype("<f4").tobytes())
    del samples
    return frames_out


def _run(cmd: list[str], log_path: Path) -> None:
    with open(log_path, "ab") as log:
        log.write(("\n$ " + " ".join(cmd) + "\n").encode("utf-8"))
        log.flush()
        code = subprocess.run(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL).returncode
    if code != 0:
        raise RuntimeError(f"ffmpeg 退出码 {code}，见 {log_path}")


def make_pure(directory: Path, ffmpeg: str, video_args: str, log: Callable[[str], None] = print,
              fps: int = 60) -> dict:
    """生成纯享版 gameplay_pure.mkv；返回摘要（写进 recording.json 的 pure）。"""
    directory = Path(directory)
    out = directory / "gameplay_pure.mkv"
    t0 = time.monotonic()
    ticks = read_ticks(directory / "frames.csv")
    ranges = keep_ranges(ticks)
    kept = sum(b - a + 1 for a, b in ranges)
    summary = {"file": str(out), "frames": kept, "seconds": round(kept / fps, 2), "removed": len(ticks) - kept,
               "segments": len(ranges)}
    if kept == 0:
        return {**summary, "state": "skipped", "reason": "没有真实游玩的帧"}
    if kept == len(ticks):            # 没有规划回放：纯享版与完整版相同
        if out.exists():
            out.unlink()
        try:
            os.link(directory / "gameplay.mkv", out)
        except OSError:
            shutil.copyfile(directory / "gameplay.mkv", out)
        return {**summary, "state": "finished", "same_as_full": True}

    video_tmp, audio_tmp, script = directory / "video_pure.mkv", directory / "audio_pure.wav", directory / "pure_select.txt"
    log_path = directory / "ffmpeg_pure.log"
    try:
        # 画面：select 按帧序号挑出各段游玩，时间戳重排成连续的 60 帧/秒
        expr = "+".join(f"between(n,{a},{b})" for a, b in ranges)
        script.write_text(f"select='{expr}',setpts=N/({fps}*TB)", encoding="ascii")
        tail = ["-an", "-fps_mode", "passthrough", *shlex.split(video_args), str(video_tmp)]
        head = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-hwaccel", "auto", "-i", str(directory / "video.mkv")]
        try:   # 滤镜从文件读：ffmpeg 7 起写作 -/filter:v 文件（-filter_script 在新版里已删除），旧版退回 -filter_script
            _run(head + ["-/filter:v", str(script)] + tail, log_path)
        except RuntimeError:
            _run(head + ["-filter_script:v", str(script)] + tail, log_path)
        cut_audio(directory / "audio.wav", audio_tmp, ranges)
        _run([ffmpeg, "-hide_banner", "-loglevel", "warning", "-y", "-i", str(video_tmp), "-i", str(audio_tmp),
              "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest", str(out)], log_path)
    except Exception as exc:  # 纯享版失败不影响完整版
        log(f"[record] 纯享版生成失败：{exc}")
        return {**summary, "state": "failed", "error": str(exc)}
    finally:
        for p in (video_tmp, audio_tmp, script):
            try:
                p.unlink()
            except OSError:
                pass
    return {**summary, "state": "finished", "elapsed_s": round(time.monotonic() - t0, 1),
            "size": out.stat().st_size if out.exists() else 0}
