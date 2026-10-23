"""游戏会话：通过桥接推进游戏，处理非可控段，记录逐帧日志与决策日志。所有方法都是同步的，由服务端放到线程里调用。"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT, RunConfig
from .metrics import Metrics

sys.path.insert(0, str(REPO_ROOT / "tools"))
from bridge_client import BridgeClient  # noqa: E402
from cutscene import skip_by_pause_menu  # noqa: E402

LETTERS = set("LRUDJXZGTCBPN")       # 桥接认识的全部按键
AGENT_LETTERS = set("LRUDJXZGT")     # 智能体只能用游玩按键；暂停、确认、返回等菜单键由主控使用
DEFAULT_STOP_ON = ["death", "room", "complete"]
VALID_STOP_ON = {"death", "room", "uncontrollable", "controllable", "land", "refill", "complete"}
END_SCENES = {"LevelExit", "AreaComplete", "Overworld", "OverworldLoader"}


class ActError(ValueError):
    """动作不合法：退回给智能体重写，不消耗游戏帧。"""


@dataclass
class ActOutcome:
    result: dict
    obs: dict
    image_b64: str | None
    game_s: float
    auto_frames: int
    auto_notes: list[str]


def expand_inputs(inputs: list[str], max_frames: int, allowed: set[str] = AGENT_LETTERS) -> list[str]:
    """把 ["6,R,J", "10,R"] 展开成逐帧按键文本，并做与桥接一致的校验。"""
    if not isinstance(inputs, list) or not inputs:
        raise ActError("inputs 必须是非空数组，例如 [\"10,R\", \"1,R,J\"]")
    frames: list[str] = []
    for seg in inputs:
        if not isinstance(seg, str):
            raise ActError(f"每段必须是字符串：{seg!r}")
        head, _, keys = seg.partition(",")
        try:
            n = int(head.strip())
        except ValueError as exc:
            raise ActError(f"段 {seg!r} 必须以帧数开头") from exc
        if n < 0:
            raise ActError(f"段 {seg!r} 的帧数不能为负")
        tokens = [t.strip().upper() for t in keys.replace("+", ",").split(",") if t.strip()]
        for t in tokens:
            if t.startswith("A:"):
                try:
                    float(t[2:])
                except ValueError as exc:
                    raise ActError(f"角度写法错误：{t}") from exc
            elif not set(t) <= allowed:
                raise ActError(f"未知按键 {t!r}；可用：L R U D J X Z G T，以及 A:角度")
        frames.extend([",".join(tokens)] * n)
        if len(frames) > max_frames:
            raise ActError(f"一次最多推进 {max_frames} 帧，请拆成多次 act")
    if not frames:
        raise ActError("总帧数为 0")
    return frames


class GameSession:
    def __init__(self, cfg: RunConfig, run_dir: Path, metrics: Metrics):
        self.cfg = cfg
        self.run_dir = run_dir
        self.metrics = metrics
        self.lock = threading.Lock()
        self.client: BridgeClient | None = None
        self.done = False
        self.done_reason = ""
        self.start_sid: str | None = None
        self.decision_index = 0
        self._frames_log = open(run_dir / "frames.jsonl", "a", encoding="utf-8")
        self._decisions_log = open(run_dir / "decisions.jsonl", "a", encoding="utf-8")

    # ---- 连接与观测 ----

    def connect(self) -> dict:
        self.client = BridgeClient(port=self.cfg.bridge_port, timeout=600)
        reply = self.client.call("activate", entities=False, tiles=False)
        obs = reply["obs"]
        if obs.get("scene") != "Level":
            raise RuntimeError(f"当前不在关卡内（scene={obs.get('scene')}），请先进入存档")
        save = (obs.get("save") or {}).get("name")
        if save != self.cfg.save:
            raise RuntimeError(f"当前存档是 {save!r}，与配置的 {self.cfg.save!r} 不一致，拒绝开始")
        self.start_sid = obs["level"]["sid"]
        self._update_metrics_from_obs(obs)
        return obs

    def close(self) -> None:
        try:
            if self.client:
                self.client.call("deactivate")
                self.client.close()
        finally:
            self._frames_log.close()
            self._decisions_log.close()

    def observe(self) -> tuple[dict, str | None]:
        with self.lock:
            reply = self.client.call("observe", image=True)
        obs = reply["obs"]
        self._update_metrics_from_obs(obs)
        return obs, reply.get("image")

    # ---- 动作 ----

    def act(self, inputs: list[str], thought: str, stop_on: list[str] | None) -> ActOutcome:
        if self.done:
            raise ActError("章节已完成，无需再操作，请直接结束本轮。")
        thought = (thought or "").strip()
        if not thought:
            raise ActError("thought 不能为空：用一两句话说明这段操作的意图")
        if len(thought) > 120:
            raise ActError("thought 太长，请压缩到一两句话（120 字以内）")
        stop = list(stop_on) if stop_on else list(DEFAULT_STOP_ON)
        bad = [s for s in stop if s not in VALID_STOP_ON]
        if bad:
            raise ActError(f"未知的截断条件 {bad}；可用：{sorted(VALID_STOP_ON)}")
        for must in ("death", "complete"):
            if must not in stop:
                stop.append(must)
        frames = expand_inputs(inputs, self.cfg.max_frames_per_act)

        self.decision_index += 1
        self.metrics.set_thought(thought)
        self.metrics.update(status="executing")
        with self.lock:
            t0 = time.monotonic()
            reply = self.client.call("step", inputs=inputs, stop_on=stop, speed=self.cfg.play_speed, image=True)
            game_s = time.monotonic() - t0
            result, obs, image = reply["result"], reply["obs"], reply.get("image")
            self._log_frames(result, frames, thought, auto=False)
            self._handle_events(result)
            self.metrics.add(play_game_s=game_s, frames_played=result["consumed"], decisions=1)

            auto_frames, notes = 0, []
            if not self.done and not obs.get("controllable"):
                obs, image, auto_frames, notes = self._auto_advance(obs, image)

        self.metrics.update(last_stop=result["stop"])
        self._update_metrics_from_obs(obs)
        self._decisions_log.write(json.dumps({
            "i": self.decision_index, "t": round(self.metrics.elapsed(), 3), "thought": thought,
            "inputs": inputs, "stop_on": stop, "stop": result["stop"], "consumed": result["consumed"],
            "planned": result["planned"], "events": result["events"], "game_s": round(game_s, 3),
            "auto_frames": auto_frames, "auto_notes": notes,
        }, ensure_ascii=False) + "\n")
        self._decisions_log.flush()
        return ActOutcome(result, obs, image, game_s, auto_frames, notes)

    # ---- 非可控段：剧情、转场、死亡与重生由主控自动推进 ----

    def _auto_advance(self, obs: dict, image: str | None):
        """非可控段一律按正常速度推进（与原版一致，录像里不快进）。
        能跳过的剧情像玩家一样用暂停菜单跳过；不能跳过的剧情正常播放，对话框等待翻页时按确认键。"""
        total, notes = 0, []
        skip_tries = 0  # 本段剧情已尝试跳过的次数
        self.metrics.update(status="auto")
        t0 = time.monotonic()

        def log_step(inputs: list[str], result: dict) -> None:
            nonlocal total
            total += result["consumed"] + result.get("loadingFrames", 0)
            self.metrics.add(frames_auto=result["consumed"])
            self._log_frames(result, expand_inputs(inputs, 10_000, LETTERS), "(自动推进)", auto=True)
            self._handle_events(result)

        for _ in range(600):
            if self.done:
                break
            scene = obs.get("scene")
            if scene in END_SCENES:
                self._mark_done(f"离开关卡（{scene}）")
                break
            level = obs.get("level") or {}
            if scene == "Level" and obs.get("controllable"):
                break
            if level.get("completed"):
                self._mark_done("章节完成")
                break
            if not level.get("inCutscene"):
                skip_tries = 0  # 一段剧情结束，下一段剧情可以重新尝试
            elif not level.get("skippingCutscene") and skip_tries < 3:
                skip_tries += 1
                skipped, obs, _, reason = skip_by_pause_menu(self.client, on_step=log_step)
                if skipped:
                    notes.append("用暂停菜单跳过剧情")
                    continue
                if reason == "no_skip":
                    skip_tries = 3  # 菜单里没有"跳过"：这段剧情只能看完，不再反复打开菜单
                    notes.append("这段剧情不能跳过，按正常速度播放")
                elif reason != "no_pause":
                    continue
            inputs = ["1,C", "29"] if level.get("dialogWaiting") and not level.get("paused") else ["30"]
            reply = self.client.call("step", inputs=inputs, stop_on=["controllable", "complete"],
                                     speed=self.cfg.auto_speed, image=False, entities=False, tiles=False)
            log_step(inputs, reply["result"])
            obs = reply["obs"]
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        if total:
            notes.append(f"主控自动推进了 {total} 帧（转场、剧情、死亡与重生，均为正常速度）")
            if not self.done:
                reply = self.client.call("observe", image=True)
                obs, image = reply["obs"], reply.get("image")
        return obs, image, total, notes

    # ---- 日志与统计 ----

    def _handle_events(self, result: dict) -> None:
        for e in result.get("events", []):
            if e["type"] == "death":
                self.metrics.add(deaths=1)
            elif e["type"] == "complete":
                self._mark_done("章节完成")

    def _mark_done(self, reason: str) -> None:
        if not self.done:
            self.done = True
            self.done_reason = reason
            self.metrics.update(status="done")

    def _log_frames(self, result: dict, frames: list[str], thought: str, auto: bool) -> None:
        traj = result.get("trajectory") or []
        for i, row in enumerate(traj):
            rec = {"f": row[0], "keys": frames[i] if i < len(frames) else "", "auto": auto,
                   "thought": thought, "decision": self.decision_index}
            if len(row) >= 8 and row[1] is not None:
                rec.update(x=row[1], y=row[2], vx=row[3], vy=row[4], st=row[5], dash=row[6], gnd=row[7])
            self._frames_log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._frames_log.flush()

    def _update_metrics_from_obs(self, obs: dict) -> None:
        level = obs.get("level") or {}
        player = obs.get("player") or {}
        self.metrics.update(
            chapter=level.get("sid", self.metrics.state.chapter),
            room=level.get("room", self.metrics.state.room),
            chapter_time_ticks=level.get("chapterTimeTicks", self.metrics.state.chapter_time_ticks),
            dashes=player.get("dashes", 0),
            max_dashes=player.get("maxDashes", 0),
        )
