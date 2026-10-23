"""M6 验收与校准：OBS 锁步录制是否只录游玩帧、一帧不漏不重，声音与画面是否对齐。

不调用模型：模拟几轮"思考（停住几秒）→ 执行一段动作"，期间用锁步录制，结束后检查：
  1. frames.csv：游戏帧号连续（没有漏帧、重复帧），行数等于各段动作推进的帧数之和；
  2. 画面：每一帧左下角的帧号条码（--marker，默认打开）与 frames.csv 记录的游戏帧号一致——
     证明 OBS 采到的正是那一帧，而不是上一帧或下一帧；
  3. 声音：audio.wav 的长度正好是帧数 × 每帧样本数；每段动作开头（声音从暂停恢复的那一刻）
     在 WAV 里出现的位置给出声音相对画面的延迟，建议的 audio_offset_ms 写进 configs/local.recording.json；
  4. gameplay.mkv 合成成功，画面与声音时长一致。

前提：游戏开着（桥接 0.8.1 以上），test 存档停在关卡里、Madeline 可操作并站在平地上（建议 1A 房间 6）；
OBS 已用 python tools/obs_setup.py launch 启动，插件已连上桥接。

用法（仓库根目录）：
    python calibration/m6_recording_test.py                    # 默认 6 轮，每轮"思考" 2 秒
    python calibration/m6_recording_test.py --audio-offset-ms 40 --no-marker
"""
from __future__ import annotations

import argparse
import array
import csv
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.config import RunConfig  # noqa: E402
from orchestrator.game_session import GameSession  # noqa: E402
from orchestrator.metrics import Metrics  # noqa: E402
from orchestrator.recording import Recorder, options_from  # noqa: E402
from overlay.server import start_in_thread  # noqa: E402

FAILED = 0
CELL = 12   # 条码每格的像素（与 game-bridge/Source/Hooks.cs 一致）


def check(ok: bool, name: str, detail: str = "") -> None:
    global FAILED
    if not ok:
        FAILED += 1
    print(f"  [{'通过' if ok else '不通过'}] {name}{('：' + detail) if detail else ''}", flush=True)


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: int(v) for k, v in row.items()} for row in csv.DictReader(f)]


def probe(ffmpeg: str, path: Path) -> dict:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix))
    out = subprocess.run([ffprobe, "-v", "error", "-count_frames", "-show_entries",
                          "stream=codec_type,codec_name,width,height,nb_read_frames,duration,sample_rate",
                          "-of", "json", str(path)], capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {"error": out.stderr}


def decode_markers(ffmpeg: str, video: Path, width: int, height: int) -> list[int]:
    """读出每一帧左下角条码表示的帧号低 16 位（游戏窗口与画布 1:1 时的位置）。"""
    scale = width / 1920.0
    region_w, region_h = int(round((CELL * 16 + 8) * scale)), int(round((CELL + 8) * scale))
    y0 = height - region_h
    cmd = [ffmpeg, "-v", "error", "-i", str(video), "-vf", f"crop={region_w}:{region_h}:0:{y0},format=gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    size = region_w * region_h
    row = region_h - int(round((CELL / 2 + 4) * scale))          # 条码一行的中间
    values = []
    for offset in range(0, len(raw) - size + 1, size):
        frame = raw[offset:offset + size]
        value = 0
        for bit in range(16):
            x = int(round((4 + bit * CELL + CELL / 2) * scale))
            if frame[row * region_w + x] < 128:                  # 黑格 = 1
                value |= 1 << bit
        values.append(value)
    return values


def wav_onsets(path: Path, starts: list[int], samples_per_frame: int, threshold: float) -> list[float | None]:
    """每段动作开头之后，声音第一次超过阈值的位置（毫秒，相对这一段第一帧的开头）。"""
    data = path.read_bytes()   # 32 位浮点立体声 WAV（wave 模块不支持浮点），自己找 data 块
    pos = data.find(b"data")
    samples = array.array("f")
    samples.frombytes(data[pos + 8:pos + 8 + ((len(data) - pos - 8) // 8) * 8])
    rate = 48000
    out = []
    for start in starts:
        first = start * samples_per_frame
        found = None
        for i in range(first, min(first + samples_per_frame * 30, len(samples) // 2)):
            if abs(samples[2 * i]) > threshold or abs(samples[2 * i + 1]) > threshold:
                found = (i - first) * 1000.0 / rate
                break
        out.append(found)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", default="test")
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--think", type=float, default=2.0, help="每轮模拟思考的秒数")
    ap.add_argument("--no-marker", action="store_true", help="不画帧号条码（只检查帧数与声音）")
    ap.add_argument("--audio-offset-ms", type=float)
    ap.add_argument("--video-args")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--threshold", type=float, default=2e-3, help="判定声音恢复的振幅阈值（满幅为 1）")
    ap.add_argument("--port", type=int, default=47790)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = RunConfig(agent="m6-calib", save=args.save, model="opus")
    run_dir = REPO_ROOT / "runs" / "m6-calib" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "runs" / "latest.txt").write_text(str(run_dir), encoding="utf-8")
    start_in_thread(args.port)
    metrics = Metrics(run_dir / "metrics.json", "m6-calib", "opus", "high", 600)
    session = GameSession(cfg, run_dir, metrics)
    session.workspace = None
    session.connect()
    options = options_from(run_dir, args.ffmpeg, args.video_args, args.audio_offset_ms, marker=not args.no_marker)
    recorder = Recorder(session.bridge_call, options, log=lambda m: print(m, flush=True))

    plans = [
        (["1,J", "20,J", "40"], "原地跳一下。"),
        (["1,U,X", "3,U", "10,U", "45"], "向上冲刺。"),
        (["8,R", "8,L", "30"], "左右走两步，回到原地。"),
    ]
    acts = []   # (推进前的帧号, 推进后的帧号)
    try:
        recorder.start()
        for i in range(args.rounds):
            inputs, thought = plans[i % len(plans)]
            metrics.mark_thinking()
            metrics.update(status="thinking")
            t0 = time.monotonic()
            time.sleep(args.think)
            metrics.add(thinking_s=time.monotonic() - t0)
            before = session.bridge_call("hello")["frame"]
            t1 = time.monotonic()
            session.act(inputs, thought, None)
            after = session.bridge_call("hello")["frame"]
            acts.append((before, after))
            print(f"第 {i + 1} 轮：{' '.join(inputs)} → 推进 {after - before} 帧，现实用时 {time.monotonic() - t1:.2f} 秒"
                  f"（{(after - before) / max(1e-6, time.monotonic() - t1):.1f} 帧/秒）", flush=True)
        time.sleep(1.0)
    finally:
        result = recorder.stop()
        metrics.update(status="done")
        session.close()

    print("\n检查：", flush=True)
    rec_dir = options.dir.resolve()
    expected = sum(b - a for a, b in acts)
    check(result is not None and result["state"] == "finished", "录制正常结束",
          f"状态 {result and result['state']}，错误 {result and result['error']!r}")
    rows = read_csv(rec_dir / "frames.csv")
    frames = [r["game_frame"] for r in rows]
    check(len(rows) == expected, "成片帧数等于推进的帧数", f"{len(rows)} / {expected}")
    gaps = [(a, b) for a, b in zip(frames, frames[1:]) if b != a + 1]
    check(not gaps, "游戏帧号连续（不漏、不重）", f"断点 {gaps[:10]}" if gaps else f"{frames[0]}…{frames[-1]}" if frames else "")
    wanted = {f for a, b in acts for f in range(a + 1, b + 1)}
    check(set(frames) == wanted, "录到的正是每段动作推进的那些帧",
          f"多 {sorted(set(frames) - wanted)[:10]}，少 {sorted(wanted - set(frames))[:10]}" if set(frames) != wanted else "")
    late = result.get("late_ticks") if result else None
    print(f"  OBS 节拍：令牌 {result and result.get('tokens')}，等待超时的节拍 {late}", flush=True)

    info = probe(options.ffmpeg, rec_dir / "video.mkv")
    stream = (info.get("streams") or [{}])[0]
    width, height = int(stream.get("width") or 1920), int(stream.get("height") or 1080)
    check(int(stream.get("nb_read_frames") or -1) == len(rows), "video.mkv 帧数与 frames.csv 行数一致",
          f"{stream.get('nb_read_frames')} / {len(rows)}")

    if not args.no_marker:
        marks = decode_markers(options.ffmpeg, rec_dir / "video.mkv", width, height)
        diffs = [((m - (f & 0xFFFF)) + 0x8000) % 0x10000 - 0x8000 for m, f in zip(marks, frames)]
        exact = sum(1 for d in diffs if d == 0)
        histogram = {d: diffs.count(d) for d in sorted(set(diffs))}
        check(len(marks) == len(frames) and exact == len(frames), "每一帧画面里的帧号与记录一致",
              f"一致 {exact}/{len(frames)}，差值分布 {histogram}")

    spf = 48000 // 60
    wav_path = rec_dir / "audio.wav"
    wav_samples = (wav_path.stat().st_size - 44) // 8 if wav_path.exists() else 0
    check(wav_samples == len(rows) * spf, "audio.wav 长度 = 帧数 × 800 个样本", f"{wav_samples} / {len(rows) * spf}")
    index_of = {r["game_frame"]: r["index"] for r in rows}
    starts = [index_of[a + 1] for a, b in acts if (a + 1) in index_of]
    onsets = wav_onsets(wav_path, starts, spf, args.threshold) if wav_path.exists() else []
    measured = [o for o in onsets if o is not None]
    print(f"  每段动作开头声音恢复的位置（毫秒，相对第一帧开头；应在第二帧开头 16.7 附近）："
          f"{[None if o is None else round(o, 1) for o in onsets]}", flush=True)
    segments = []
    seg_path = rec_dir / "segments.csv"
    if seg_path.exists():
        with open(seg_path, newline="", encoding="utf-8") as f:
            segments = list(csv.DictReader(f))
    latencies = [float(s["measured_ms"]) for s in segments if s["method"] == "measured"]
    if segments:
        print(f"  插件分段 {len(segments)} 段，量到声音延迟的 {len(latencies)} 段；延迟 "
              f"{min(latencies):.1f}–{max(latencies):.1f} ms（中位数 {statistics.median(latencies):.1f}）" if latencies
              else f"  插件分段 {len(segments)} 段，都没量到声音延迟", flush=True)
        check(len(latencies) >= len(acts) - 1, "每段动作开头都量到了声音延迟", f"{len(latencies)} / {len(acts)}")
    if measured:
        error = [o - 1000.0 / 60 for o in measured]
        worst = max(error, key=abs)
        print(f"  相对第二帧开头的误差：中位数 {statistics.median(error):+.1f} ms，最大 {worst:+.1f} ms", flush=True)
        check(abs(statistics.median(error)) <= 4 and abs(worst) <= 1000.0 / 60, "声音与画面对齐（误差不超过一帧）",
              f"中位数 {statistics.median(error):+.1f} ms，最大 {worst:+.1f} ms")
    if latencies:
        print(f"  建议把 configs/local.recording.json 的 audio_offset_ms 设为 {statistics.median(latencies):.0f}"
              "（只在某段量不到时作为后备）", flush=True)

    info = probe(options.ffmpeg, rec_dir / "gameplay.mkv")
    kinds = {s.get("codec_type"): s for s in info.get("streams") or []}
    check({"video", "audio"} <= set(kinds), "gameplay.mkv 含画面与声音", f"{sorted(kinds)}")
    summary = {"acts": acts, "expected": expected, "rows": len(rows), "gaps": gaps[:50], "onsets_ms": onsets,
               "recording": result}
    (run_dir / "m6_check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果：{'全部通过' if FAILED == 0 else f'{FAILED} 项不通过'}；目录 {rec_dir}", flush=True)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
