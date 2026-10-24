"""test 存档上的模拟器、规划回放与录像联合验收（不调用模型）。"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import REPO_ROOT, RunConfig
from orchestrator.container import docker, ensure_container
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.playback import Playback
from orchestrator.recording import Recorder, options_from


def check_tracks(directory: Path, recording: dict, hello: dict) -> dict:
    """独立游戏音频（game_nrt）的两条音轨：完整版每个成片帧 800 个样本，回放期间音乐继续走（完整版的音乐位置比纯享版
    多出回放的长度）；纯享版只有游玩帧、纯享版用它。回放帧不要求有声：序章刚进关时音乐开头是一段安静的前奏。"""
    if (recording.get("options") or {}).get("audio_source") != "game_nrt":
        return {"audio_source": (recording.get("options") or {}).get("audio_source")}
    import numpy as np
    from orchestrator.purecut import _wav_data, read_ticks

    def wav(name: str):
        offset, size = _wav_data(directory / name)
        return np.fromfile(directory / name, dtype="<f4", offset=offset, count=size // 4).reshape(-1, 800, 2)

    ticks = np.array(read_ticks(directory / "frames.csv"))
    full, play = wav("audio.wav"), wav("audio_play.wav")
    assert len(full) == len(ticks), f"完整版音轨 {len(full)} 帧，成片 {len(ticks)} 帧"
    assert len(play) == int((ticks >= 0).sum()), f"纯享版音轨 {len(play)} 帧，游玩帧 {(ticks >= 0).sum()}"
    replay = full[ticks < 0]
    rms = lambda blocks: float(np.sqrt(np.mean(np.square(blocks)))) if blocks.size else 0.0
    silent = int((np.abs(replay).max(axis=(1, 2)) == 0).sum()) if replay.size else 0
    full_ms, play_ms = hello.get("record_music_full_ms"), hello.get("record_music_ms")
    expected = int((ticks < 0).sum()) * 1000 / 60
    if full_ms is not None and play_ms is not None:   # 有音乐时：回放期间完整版的音乐继续走，纯享版不走
        assert abs((full_ms - play_ms) - expected) < 60, f"完整版音乐领先 {full_ms - play_ms} 毫秒，回放 {expected:.0f} 毫秒"
    assert recording["pure"].get("audio") == "audio_play.wav", recording["pure"]
    return {"full_frames": len(full), "play_frames": len(play), "replay_frames": int((ticks < 0).sum()),
            "replay_silent_frames": silent, "replay_rms": round(rms(replay), 5), "play_rms": round(rms(play), 5),
            "music_full_ms": full_ms, "music_play_ms": play_ms, "pure_audio": recording["pure"]["audio"]}


def main() -> None:
    cfg = RunConfig(agent="dev-sim", save="test", model="playback-check")
    cfg.sim = True
    cfg.playback = True
    cfg.continuous_music = True
    run_dir = REPO_ROOT / "runs" / "playback-check" / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    (REPO_ROOT / "runs" / "latest.txt").write_text(str(run_dir), encoding="utf-8")
    metrics = Metrics(run_dir / "metrics.json", cfg.agent, cfg.model, cfg.effort, 600)
    session = GameSession(cfg, run_dir, metrics)
    recorder = Recorder(session.bridge_call, options_from(run_dir))
    playback = None
    try:
        session.container = ensure_container(cfg)
        before = session.connect()
        playback = Playback(cfg, session, metrics, None)
        session.playback = playback
        metrics.update(live=True)
        metrics.set_thought("测试规划回放：对比几种起跳位置，确认轨迹展示不会推进游戏。")
        recorder.start()
        program = '''import sys
sys.path.insert(0, "/sim")
from sim import Sim
sim = Sim()
plans = [[f"{n},R", "1,R,J", "15,R,J", "25"] for n in (1, 3, 5, 7, 9, 11)]
for result in sim.run_many(plans):
    print(result.stop, len(result.frames))
sim.close()
'''
        result = docker("exec", "-u", "agent", "-w", "/workspace", session.container, "python3", "-c", program)
        print(result.stdout, flush=True)
        playback.drain()
        assert metrics.state.sim_rollouts == 6, "模拟器推演记录没有完整回放"
        frozen = session.bridge_call("observe", image=False, entities=False, tiles=False)["obs"]
        assert frozen["frame"] == before["frame"], "规划回放推进了游戏"
        assert frozen["player"]["pos"] == before["player"]["pos"], "规划回放改变了角色位置"
        session.act(["20"], "回放完成，检查真实动作和纯享版录像。", None)
        hello = session.bridge_call("hello")   # 录像两条音轨里音乐走到的位置（停止录制前）
        recording = recorder.stop()
        check = recording["check"]
        assert recording["state"] == "finished", recording
        assert check["replay_frames"] > 0, "录像缺少规划回放帧"
        assert check["missing_frames"] == 0 and check["repeats"] == 0, check
        assert recording["pure"]["state"] == "finished", recording["pure"]
        assert recording["pure"]["frames"] == 20, recording["pure"]
        audio = check_tracks(Path(recording["dir"]), recording, hello)
        summary = {"playback": True, "rollouts": metrics.state.sim_rollouts,
                   "game_unchanged_during_playback": True, "audio": audio, "recording": recording}
        (run_dir / "check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"PASS: 6 条推演、回放不推进游戏、录像含回放、纯享版仅 20 帧；声音 {json.dumps(audio, ensure_ascii=False)}；{run_dir}", flush=True)
    finally:
        if recorder.started:
            recorder.stop()
        if playback is not None:
            playback.close()
        session.close()


if __name__ == "__main__":
    main()
