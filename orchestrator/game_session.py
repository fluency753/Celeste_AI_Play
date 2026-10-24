"""游戏会话：通过桥接推进游戏，处理非可控段，记录逐帧日志与决策日志。所有方法都是同步的，由服务端放到线程里调用。"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .config import REPO_ROOT, RunConfig, chapter_name, chapter_name_by_id, CHAPTER_SIDS
from .metrics import Metrics
from .observation import enrich_obs, fmt_ticks
from .sandbox import run_program
from .goals import GoalManager

sys.path.insert(0, str(REPO_ROOT / "tools"))
from bridge_client import BridgeClient  # noqa: E402
from cutscene import skip_by_pause_menu, leave_level_by_pause_menu, story_pending  # noqa: E402

LETTERS = set("LRUDJXZGTKYVCBPN")    # 桥接认识的全部按键
AGENT_LETTERS = set("LRUDJXZGTKYV")  # 智能体只能用游玩按键；暂停、确认、返回等菜单键由主控使用
AGENT_KEYS_TEXT = "L R U D J X Z G T K Y V，以及 A:角度"
DEFAULT_STOP_ON = ["death", "room", "complete"]
VALID_STOP_ON = {"death", "room", "uncontrollable", "controllable", "land", "refill", "complete"}
END_SCENES = {"LevelExit", "AreaComplete", "Overworld", "OverworldLoader"}
PROGRESS_EVERY = 6                   # 执行中每 6 帧（0.1 秒）推送一次进度给 HUD

# 自动重试：计划文件里声明重算程序，死亡或偏离预期时由主控在零网络沙箱里运行它，不唤醒大模型
RETRY_REASONS = {"death", "deviate", "done"}
DEFAULT_RETRY_ON = ("death", "deviate")
MAX_RETRIES = 5
MAX_RETRY_TIMEOUT_S = 60.0


@dataclass
class RetrySpec:
    command: str
    max: int = 3
    timeout_s: float = 20.0
    on: tuple[str, ...] = DEFAULT_RETRY_ON


@dataclass
class PlanSpec:
    inputs: list[str]
    expect: list | None = None
    tolerance: float = 1.0
    stop_on: list[str] | None = None
    retry: RetrySpec | None = None
    text: str = ""          # 计划文件原文，用来判断重算程序有没有改动计划


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_plan(data, text: str = "") -> PlanSpec:
    """计划文件：按键段数组 ["12,R", "1,R,J"]，或对象 {"inputs", "expect", "tolerance", "stop_on", "retry"}。"""
    if isinstance(data, list):
        data = {"inputs": data}
    if not isinstance(data, dict):
        raise ActError('计划文件必须是按键段数组，或含 "inputs" 的对象')
    inputs = data.get("inputs")
    if not isinstance(inputs, list) or not all(isinstance(x, str) for x in inputs):
        raise ActError('计划文件的 "inputs" 必须是按键段数组，例如 ["12,R", "1,R,J"]')
    expect = data.get("expect")
    if expect is not None:
        if not isinstance(expect, list):
            raise ActError('"expect" 必须是数组：每帧一项，null（不检查）、[x, y] 或 [x, y, "状态名"]')
        for i, row in enumerate(expect):
            if row is None:
                continue
            if (not isinstance(row, list) or len(row) < 2 or not _number(row[0]) or not _number(row[1])
                    or (len(row) >= 3 and row[2] is not None and not isinstance(row[2], str))):
                raise ActError(f'"expect" 第 {i + 1} 项格式不对：应为 null、[x, y] 或 [x, y, "状态名"]')
    tolerance = data.get("tolerance", 1.0)
    if not _number(tolerance) or tolerance < 0:
        raise ActError('"tolerance" 必须是非负数（像素）')
    stop_on = data.get("stop_on")
    if stop_on is not None and (not isinstance(stop_on, list) or not all(isinstance(x, str) for x in stop_on)):
        raise ActError('"stop_on" 必须是字符串数组')
    retry = None
    raw = data.get("retry")
    if raw is not None:
        if not isinstance(raw, dict) or not isinstance(raw.get("command"), str) or not raw["command"].strip():
            raise ActError('"retry" 必须是对象，并给出 "command"（在 /workspace 下运行的命令）')
        if len(raw["command"]) > 500:
            raise ActError('"retry.command" 太长（最多 500 字符），请写成脚本')
        count = raw.get("max", 3)
        timeout_s = raw.get("timeout_s", 20)
        on = raw.get("on", list(DEFAULT_RETRY_ON))
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_RETRIES:
            raise ActError(f'"retry.max" 必须是 1～{MAX_RETRIES} 的整数')
        if not _number(timeout_s) or not 1 <= timeout_s <= MAX_RETRY_TIMEOUT_S:
            raise ActError(f'"retry.timeout_s" 必须在 1～{MAX_RETRY_TIMEOUT_S:g} 秒之间')
        if not isinstance(on, list) or not on or not set(on) <= RETRY_REASONS:
            raise ActError(f'"retry.on" 必须是 {sorted(RETRY_REASONS)} 的非空子集')
        retry = RetrySpec(raw["command"].strip(), count, float(timeout_s), tuple(on))
    return PlanSpec(list(inputs), expect, float(tolerance), stop_on, retry, text)


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
    attempts: list[dict] = field(default_factory=list)   # 每次执行与每次重算的摘要（有自动重试时多于一项）


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
                raise ActError(f"未知按键 {t!r}；可用：{AGENT_KEYS_TEXT}")
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
        self.user_stopped = False
        self.goals = GoalManager(run_dir)
        self._goal_progress = {}
        self.start_sid: str | None = None
        self.end_scene: str | None = None
        self.decision_index = 0
        self.container: str | None = None   # 智能体容器（自动重试时在里面运行重算程序）
        self.before_advance = None          # 每次推进游戏前调用（录制时：确认游戏窗口没有最小化）
        self.playback = None                # 规划回放（playback.Playback）：每次执行计划之前把推演过的路线画进游戏
        self.last_obs: dict | None = None   # 最近一次观测（回放用房间边界）
        self.room_entry_frame: int | None = None   # 进入当前房间的那一帧（HUD 的推演最速从这里算起）
        self._last_positions: dict = {}
        self._last_room = None
        # 依次通关多章：当前章节完成后，主控自动进入队列里的下一章
        self.chapter_queue: list[int] = list(cfg.chapters[1:]) if cfg.chapters else []
        self.chapter_finished = False
        self._pending_complete = False
        self._frames_log = open(run_dir / "frames.jsonl", "a", encoding="utf-8")
        self._decisions_log = open(run_dir / "decisions.jsonl", "a", encoding="utf-8")
        # 容器方式下，观测与轨迹同时写进智能体的工作区，供它自写的程序读取
        self.workspace: Path | None = cfg.workspace_dir if cfg.runtime == "container" else None
        from .sim_assist import SimEnergy
        self.sim_energy = SimEnergy(run_dir / "sim_energy.json") if cfg.sim_assist else None
        if self.sim_energy:
            self.metrics.update(sim_energy=self.sim_energy.snapshot())
        if self.workspace:
            (self.workspace / "game" / "rooms").mkdir(parents=True, exist_ok=True)
            # 给观众看的展示接口（HUD 进度、轨迹记录），每次运行都换成主控这一版；上次运行留下的进度清掉
            shutil.copyfile(REPO_ROOT / "orchestrator" / "workspace_files" / "show.py", self.workspace / "game" / "show.py")
            (self.workspace / "game" / "hud_status.json").unlink(missing_ok=True)

    # ---- 连接与观测 ----

    def connect(self) -> dict:
        self.client = BridgeClient(port=self.cfg.bridge_port, timeout=600)
        self.client.cancel_check = lambda: self.goals.immediate or (self.goals.stop_motion.is_set() and self.done_reason != "章节完成")
        reply = self.client.call("activate", entities=False, tiles=False, continuous_music=self.cfg.continuous_music)
        obs = reply["obs"]
        self._pending_complete = bool((obs.get('level') or {}).get('completed'))
        if obs.get("scene") != "Level" and not (self.cfg.cutscene_mode == 'agent' and story_pending(obs)):
            raise RuntimeError(f"当前不在关卡内（scene={obs.get('scene')}），请先进入存档")
        save = (obs.get("save") or {}).get("name")
        if save != self.cfg.save:
            raise RuntimeError(f"当前存档是 {save!r}，与配置的 {self.cfg.save!r} 不一致，拒绝开始")
        slot = (obs.get('save') or {}).get('slot')
        if self.cfg.save_slot is not None and slot != self.cfg.save_slot:
            raise RuntimeError(f'当前存档槽位 {slot} 与指定槽位 {self.cfg.save_slot} 不一致')
        self.cfg.save_slot = slot
        self.start_sid = (obs.get('level') or {}).get('sid') or CHAPTER_SIDS.get(self.cfg.chapter) or self.metrics.state.chapter
        if self.start_sid:self.metrics.update(chapter=self.start_sid)
        self._update_metrics_from_obs(obs)
        if self.workspace:
            # 上一次运行留下的观测与动作记录已经过时：先换成当前这一帧，避免智能体在准备阶段读到旧数据
            for stale in ("game/last_act.json", "game/retry.json"):
                (self.workspace / stale).unlink(missing_ok=True)
            reply = self.client.call("observe", image=False, scope="room", fields=True)
            self._write_workspace(self._track_motion(enrich_obs(reply["obs"])))
            self._write_sim_state()
        return obs

    def close(self, release: bool = True) -> None:
        """试玩结束交还控制；release=False 仅供明确需要保留接管的内部调试使用。"""
        client, self.client = self.client, None
        try:
            if client:
                try:
                    if release:
                        client.call("deactivate")
                finally:
                    client.close()
        finally:
            self._frames_log.close()
            self._decisions_log.close()

    def bridge_call(self, cmd: str, **args) -> dict:
        """主控其他部分（如录制）向桥接发指令：与智能体的动作互斥，共用同一个连接（桥接只接受一个连接）。"""
        with self.lock:
            return self.client.call(cmd, **args)

    def observe(self) -> tuple[dict, str | None]:
        with self.lock:
            reply = self.client.call("observe", image=True, scope="room", fields=True)
            self._write_sim_state()
        obs = self._track_motion(enrich_obs(reply["obs"]))
        self._update_metrics_from_obs(obs)
        self._write_workspace(obs)
        return obs, reply.get("image")

    def _track_motion(self, obs: dict) -> dict:
        """与上一次暂停时比较，给移动过的实体标上位移 moved = [dx, dy]（同一房间内按编号对照）。"""
        level = obs.get("level") or {}
        key = (level.get("sid"), level.get("room"))
        last = self._last_positions if getattr(self, "_last_room", None) == key else {}
        current = {}
        for e in obs.get("entities") or []:
            if "id" not in e:
                continue
            current[e["id"]] = (e["x"], e["y"])
            if e["id"] in last:
                dx, dy = e["x"] - last[e["id"]][0], e["y"] - last[e["id"]][1]
                if dx or dy:
                    e["moved"] = [round(dx, 2), round(dy, 2)]
        self._last_positions, self._last_room = current, key
        return obs

    # ---- 工作区数据 ----

    def _plan_path(self, plan_file: str) -> Path:
        if not self.workspace:
            raise ActError("本次运行没有工作区，不能使用 plan_file，请直接传 inputs")
        rel = plan_file.strip()
        if rel.startswith("/workspace/"):
            rel = rel[len("/workspace/"):]
        path = (self.workspace / rel).resolve()
        if self.workspace.resolve() not in path.parents:
            raise ActError(f"plan_file 必须在 /workspace 下：{plan_file}")
        return path

    def resolve_plan_file(self, plan_file: str) -> PlanSpec:
        """读取工作区里的计划文件（格式见 parse_plan 与知识库 game_data.md）。"""
        path = self._plan_path(plan_file)
        if not path.is_file():
            raise ActError(f"找不到计划文件 {plan_file}")
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError) as exc:
            raise ActError(f"计划文件不是合法的 JSON：{exc}") from exc
        return parse_plan(data, text)

    def _write_json(self, rel: str, data) -> None:
        path = self.workspace / rel
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _write_workspace(self, obs: dict, act_record: dict | None = None) -> None:
        self.last_obs = obs
        if not self.workspace:
            return
        try:
            self._write_json("game/latest_obs.json", {"decision": self.decision_index, "obs": obs})
            level = obs.get("level") or {}
            if level.get("room") is not None and obs.get("tiles"):
                sid = str(level.get("sid", "unknown")).replace("/", "_")
                room_rel = f"game/rooms/{sid}__{level['room']}.json"
                if not (self.workspace / room_rel).exists():
                    self._write_json(room_rel, {"decision": self.decision_index, "obs": obs})
            if act_record is not None:
                self._write_json("game/last_act.json", act_record)
        except OSError:
            pass  # 工作区写入失败不影响游戏推进

    def _write_sim_state(self) -> None:
        """预置模拟器的起始状态：当前这一帧的完整场景（调用方持有连接锁）。取不到时删掉旧文件，免得智能体用错帧。"""
        if not (self.cfg.sim and self.workspace):
            return
        path = self.workspace / "game" / "sim_state.json"
        try:
            reply = self.client.call("sim_state", margin=4)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(reply["state"], ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            path.unlink(missing_ok=True)

    # ---- 动作 ----

    def simulate(self, request: dict) -> dict:
        from .sim_assist import validate_request, run_predictions
        if not self.sim_energy:
            raise ActError("本局未开启限额模拟辅助")
        if self.done or self.goals.stop_motion.is_set():
            raise ActError("试玩已结束，不能继续预测")
        candidates, cost = validate_request(request)
        # Capture and predict under the same lock: predictions always refer to the actual current frame.
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():
                raise ActError("试玩已结束，不能继续预测")
            directory = self.run_dir / "sim_private"
            directory.mkdir(exist_ok=True)
            ident = self.sim_energy.reserve(cost)
            self.metrics.update(sim_energy=self.sim_energy.snapshot())
            try:
                state = self.client.call("sim_state", margin=4)["state"]
                (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
                result = run_predictions(self.cfg, directory, candidates)
            except Exception:
                self.sim_energy.finish(ident, refund=True)
                self.metrics.update(sim_energy=self.sim_energy.snapshot())
                raise
            self.sim_energy.finish(ident)
        result.update(receipt=ident, cost=cost, energy=self.sim_energy.snapshot(), candidates=candidates)
        # Successful predictions count immediately, independently of visualization and runtime.
        self.metrics.add(sim_calls=1, sim_rollouts=len(candidates))
        if self.workspace:
            try:
                (self.workspace / "game" / "sim_assist").mkdir(parents=True, exist_ok=True)
                self._write_json("game/sim_assist/latest.json", result)
                if self.playback:
                    self.playback.collect_prediction(result["results"], result["info"].get("frame", 0))
            except Exception:  # optional visualization must not turn a successful prediction into failure
                result["replay_warning"] = "本次预测已完成，但轨迹回放记录失败"
        # Keep ordinary replies short; frame-by-frame details remain available on demand.
        summary = {k: v for k, v in result.items() if k != "results"}
        summary["results"] = [{"stop": r.get("stop"), "detail": r.get("detail"),
                               "frames": len(r.get("frames", [])),
                               "last_frame": (r.get("frames") or [None])[-1],
                               "events": r.get("events", []), "warnings": r.get("warnings", [])}
                              for r in result["results"]]
        if self.workspace:
            summary["trajectory_file"] = "/workspace/game/sim_assist/latest.json"
        return summary

    def act(self, inputs: list[str] | None, thought: str, stop_on: list[str] | None,
            plan_file: str | None = None) -> ActOutcome:
        if self.done:
            raise ActError(f"本次试玩已结束（{self.done_reason}），请结束本轮。")
        if ((self.last_obs or {}).get('level') or {}).get('dialogChoices'):
            raise ActError('当前有对话选项，请使用cutscene的choose动作明确选择')
        if plan_file:
            if inputs:
                raise ActError("inputs 与 plan_file 只能二选一")
            plan = self.resolve_plan_file(plan_file)
        else:
            plan = PlanSpec(inputs=inputs) if isinstance(inputs, list) else PlanSpec(inputs=[])
        thought = (thought or "").strip()
        if not thought:
            raise ActError("thought 不能为空：用一两句话说明这段操作的意图")
        if len(thought) > 120:
            raise ActError("thought 太长，请压缩到一两句话（120 字以内）")
        stop = self._stop_list(stop_on or plan.stop_on)
        frames = self._check_plan(plan)
        pre_notes: list[str] = []
        if plan.retry and self.cfg.pause_limit:
            # 每次尝试都要由智能体自己想过、带一句新的思考语句发起，所以不自动重试
            plan = replace(plan, retry=None)
            pre_notes.append("本次运行关闭了自动重试，计划里的 retry 被忽略")
        if plan.retry and not self.container:
            raise ActError("本次运行不能自动重试（没有智能体容器），请去掉计划里的 retry")
        plan_path = None
        if plan_file:
            rel = self._plan_path(plan_file).relative_to(self.workspace.resolve())
            plan_path = "/workspace/" + rel.as_posix()   # 容器里看到的路径

        self.decision_index += 1
        start_room = self.metrics.state.room
        attempts: list[dict] = []
        total_game_s, total_auto, notes = 0.0, 0, list(pre_notes)
        attempt = 0
        while True:
            label = thought if attempt == 0 else f"{thought}（自动重试 {attempt}）"
            result, obs, image, game_s, auto_frames, auto_notes = self._execute(
                plan, frames, label, stop, plan_file, attempt)
            total_game_s += game_s
            total_auto += auto_frames
            notes.extend(auto_notes)
            attempts.append({"kind": "execute", "attempt": attempt, "stop": result["stop"],
                             "consumed": result["consumed"], "planned": result["planned"],
                             "deviation": result.get("deviation")})
            retry = plan.retry
            reason = result["stop"]
            if (self.done or retry is None or attempt >= retry.max or reason not in retry.on
                    or not obs.get("controllable") or (obs.get("level") or {}).get("room") != start_room):
                break
            # 在零网络沙箱里运行智能体的重算程序，它应当改写同一个计划文件
            attempt += 1
            self.metrics.update(status="retrying")
            self._write_json("game/retry.json", {
                "decision": self.decision_index, "attempt": attempt, "max": retry.max, "reason": reason,
                "plan_file": plan_path,
                "deviation": result.get("deviation"), "events": result.get("events"),
                "consumed": result["consumed"], "planned": result["planned"],
            })
            program = run_program(self.container, retry.command, retry.timeout_s, env={
                "CELESTE_RETRY_ATTEMPT": attempt, "CELESTE_RETRY_REASON": reason,
                "CELESTE_RETRY_FILE": "/workspace/game/retry.json", "CELESTE_PLAN_FILE": plan_path,
            })
            self.metrics.add(retries=1, replan_s=program.seconds)
            record = {"kind": "replan", "attempt": attempt, "reason": reason, "seconds": round(program.seconds, 2),
                      "returncode": program.returncode, "timed_out": program.timed_out}
            attempts.append(record)
            if not program.ok:
                record["error"] = (program.stderr or program.stdout)[-600:]
                break
            try:
                new_plan = self.resolve_plan_file(plan_file)
                if new_plan.text == plan.text:
                    record["error"] = "重算程序没有改动计划文件"
                    break
                frames = self._check_plan(new_plan)
                new_stop = self._stop_list(stop_on or new_plan.stop_on)
            except ActError as err:
                record["error"] = f"重算后的计划不合法：{err}"
                break
            # 重试次数与条件以第一次提交的计划为准，重算出的计划不能放宽
            plan, stop = replace(new_plan, retry=retry), new_stop
        return ActOutcome(result, obs, image, total_game_s, total_auto, notes, attempts)

    def _stop_list(self, stop_on: list[str] | None) -> list[str]:
        stop = list(stop_on) if stop_on else list(DEFAULT_STOP_ON)
        bad = [s for s in stop if s not in VALID_STOP_ON]
        if bad:
            raise ActError(f"未知的截断条件 {bad}；可用：{sorted(VALID_STOP_ON)}")
        for must in ("death", "complete"):
            if must not in stop:
                stop.append(must)
        return stop

    def _check_plan(self, plan: PlanSpec) -> list[str]:
        frames = expand_inputs(plan.inputs, self.cfg.max_frames_per_act)
        if plan.expect is not None and len(plan.expect) > len(frames):
            raise ActError(f'"expect" 有 {len(plan.expect)} 帧，比按键的 {len(frames)} 帧还多')
        return frames

    def _execute(self, plan: PlanSpec, frames: list[str], thought: str, stop: list[str], plan_file: str | None,
                 attempt: int):
        """执行一次计划：推进游戏、自动推进非可控段、记录日志与工作区数据。"""
        if attempt == 0 and self.playback is not None:
            # 规划回放（游戏不推进）：先等排队的轮次放完（不持有连接锁，回放线程要用），再把镜头送回 Madeline
            self.playback.drain()
            self._before_advance()
            with self.lock:
                self.playback.final()
        self.metrics.set_thought(thought)
        self.metrics.update(status="executing")
        extra = {"expect": plan.expect, "tolerance": plan.tolerance} if plan.expect else {}
        self._before_advance()
        # HUD 每 0.1 秒取一次数据、淡入 0.2 秒：等它换上这段动作的思考语句再推进，
        # 录像里新语句就和这段动作的第一帧同时出现（游戏冻结时录像不写帧，这段等待不进录像）
        time.sleep(self.cfg.thought_lead_s)
        with self.lock:
            if self.goals.stop_motion.is_set():
                raise ActError("已请求停止，本次动作未执行")
            t0 = time.monotonic()
            reply = self.client.call("step", inputs=plan.inputs, stop_on=stop, speed=self.cfg.play_speed, image=True,
                                     scope="room", fields=True, entityTrajectory=True, progress=PROGRESS_EVERY,
                                     on_event=self._on_progress, **extra)
            game_s = time.monotonic() - t0
            result, obs, image = reply["result"], enrich_obs(reply["obs"]), reply.get("image")
            if self.sim_energy:
                from .sim_assist import moving_frames
                previous = (self.last_obs or {}).get("player") or {}
                self.sim_energy.reward(moving_frames=moving_frames(result.get("trajectory") or [], previous.get("pos")))
            self._log_frames(result, frames, thought, auto=False)
            self._handle_events(result)
            self._commit_completion(obs)
            self.goals.check(self.goal_values(obs))
            self.metrics.add(play_game_s=game_s, frames_played=result["consumed"],
                             decisions=1 if attempt == 0 else 0)

            auto_frames, notes = 0, []
            if not self.done and not self.chapter_finished and not self.goals.stop_motion.is_set() and not obs.get("controllable"):
                obs, image, auto_frames, notes = self._auto_advance(obs, image)
            if self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                obs, image, more = self._enter_next_chapter()
                notes.extend(more)
            self.goals.check(self.goal_values(obs))
            self._write_sim_state()

        self.metrics.update(last_stop=result["stop"])
        obs = self._track_motion(obs)
        self._update_metrics_from_obs(obs)
        self._write_workspace(obs, {
            "decision": self.decision_index, "attempt": attempt, "thought": thought, "inputs": plan.inputs,
            "plan_file": plan_file, "stop_on": stop, "tolerance": plan.tolerance if plan.expect else None,
            "result": result, "auto_frames": auto_frames, "auto_notes": notes,
        })
        self._decisions_log.write(json.dumps({
            "i": self.decision_index, "attempt": attempt, "t": round(self.metrics.elapsed(), 3),
            "effort": self.metrics.state.effort, "thought": thought,
            "fast_requested": self.metrics.state.turn_fast_requested,
            "service_tier_requested": self.metrics.state.turn_service_tier_requested,
            "decision_wait_s": round(self.metrics.state.decision_wait_s, 3) if attempt == 0 else None,
            "inputs": plan.inputs, "plan_file": plan_file, "stop_on": stop, "stop": result["stop"],
            "consumed": result["consumed"], "planned": result["planned"], "events": result["events"],
            "deviation": result.get("deviation"), "game_s": round(game_s, 3),
            "auto_frames": auto_frames, "auto_notes": notes,
        }, ensure_ascii=False) + "\n")
        self._decisions_log.flush()
        return result, obs, image, game_s, auto_frames, notes

    # ---- 非可控段：按剧情模式交接，自动处理转场、死亡与重生 ----

    def _auto_advance(self, obs: dict, image: str | None):
        """非可控段一律按正常速度推进（与原版一致，录像里不快进）。
        auto模式跳过剧情或推进普通对话；agent模式交回剧情控制权，分支选项始终要求明确选择。"""
        total, notes = 0, []
        skip_tries = 0  # 本段剧情已尝试跳过的次数
        self.metrics.update(status="auto")
        self._before_advance()
        t0 = time.monotonic()

        def log_step(inputs: list[str], result: dict) -> None:
            nonlocal total
            total += result["consumed"] + result.get("loadingFrames", 0)
            self.metrics.add(frames_auto=result["consumed"])
            self._log_frames(result, expand_inputs(inputs, 10_000, LETTERS), "(自动推进)", auto=True)
            self._handle_events(result)

        for _ in range(600):
            if self.done or self.chapter_finished or self.goals.stop_motion.is_set():
                break
            scene = obs.get("scene")
            if self.cfg.cutscene_mode == 'agent' and story_pending(obs):
                notes.append('剧情等待你的操作：可观看等待、推进、选择对话选项或跳过。')
                break
            self._commit_completion(obs)
            if self.done or self.chapter_finished:break
            if scene in END_SCENES:
                self._mark_done(f"离开关卡（{scene}）")
                break
            level = obs.get("level") or {}
            if scene == "Level" and obs.get("controllable"):
                break
            if level.get("completed"):
                self._chapter_complete()
                break
            if not level.get("inCutscene"):
                skip_tries = 0  # 一段剧情结束，下一段剧情可以重新尝试
            elif self.cfg.cutscene_mode == 'auto' and not level.get("skippingCutscene") and skip_tries < 3:
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
            if level.get('dialogChoices') and not level.get('skippingCutscene'):
                notes.append('当前需要明确选择对话选项，主控不会代选。')
                break
            inputs = ["1,C", "29"] if self.cfg.cutscene_mode == 'auto' and level.get("dialogWaiting") and not level.get("paused") else ["30"]
            reply = self.client.call("step", inputs=inputs, stop_on=["controllable", "complete"],
                                     speed=self.cfg.auto_speed, image=False, entities=False, tiles=False,
                                     progress=PROGRESS_EVERY, on_event=self._on_progress)
            log_step(inputs, reply["result"])
            obs = reply["obs"]
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        if total:
            notes.append(f"主控自动推进了 {total} 帧（转场、剧情、死亡与重生，均为正常速度）")
            if not self.done and not self.chapter_finished:
                reply = self.client.call("observe", image=True, scope="room", fields=True)
                obs, image = enrich_obs(reply["obs"]), reply.get("image")
        return obs, image, total, notes

    def _before_advance(self) -> None:
        """推进游戏之前的检查（录制时由 run.py 设成"确认游戏窗口没有最小化"）；出错不影响游玩。"""
        if self.before_advance is not None:
            try:
                self.before_advance()
            except Exception as exc:  # noqa: BLE001
                print(f"[run] 推进前检查出错（忽略）：{exc}", flush=True)

    # ---- 日志与统计 ----

    def _on_progress(self, event: dict) -> None:
        """执行中桥接每隔几帧推送一次进度：更新统计，HUD 据此逐帧显示章节计时、冲刺与房间。"""
        if event.get("event") != "progress":
            return
        self.metrics.update(
            chapter_time_ticks=event.get("chapterTimeTicks", self.metrics.state.chapter_time_ticks),
            room=event.get("room", self.metrics.state.room),
            chapter=event.get("sid", self.metrics.state.chapter),
            dashes=event.get("dashes", 0),
            max_dashes=event.get("maxDashes", 0),
        )
        values = self.goal_values()
        values.update(frame=event.get("frame", values.get("frame")), room=event.get("room", values.get("room")),
                      deaths=event.get("deaths", values.get("deaths")), dead=event.get("dead", False))
        values["map"] = f"{event.get('sid', self.metrics.state.chapter)}:{values['side']}"
        self._goal_progress = dict(values)
        self.goals.check(values)
        if self.goals.stop_motion.is_set() and not self.goals.cancel_sent:
            self.goals.cancel_sent = True
            self.client.cancel_pending()

    def goal_values(self, obs=None):
        observation = obs if obs is not None else self.last_obs or {}
        level = observation.get("level") or {}
        sid = level.get("sid", self.metrics.state.chapter)
        side = level.get("side", "A")
        values = {"room": level.get("room", self.metrics.state.room), "map": f"{sid}:{side}", "side": side,
                "map_complete": bool(level.get("completed")) and not story_pending(observation), "deaths": level.get("deaths", 0),
                "frame": observation.get("frame", 0), "elapsed": self.metrics.elapsed(),
                "dead": bool((observation.get("player") or {}).get("dead"))}
        if obs is None:
            values.update(self._goal_progress)
            values["elapsed"] = self.metrics.elapsed()
        return values

    def finish_user_stop(self):
        self.user_stopped = True
        if not self.goals.immediate and (self._pending_complete or
                bool(((self.last_obs or {}).get('level') or {}).get('completed'))):
            self.chapter_queue.clear()
            self._pending_complete=False
            if not self.done:self._chapter_complete()
        if not self.done:
            self._mark_done(self.goals.snapshot().get("stop_reason", "用户停止"))

    def cutscene(self, action: str, frames: int = 30, choice: int | None = None) -> tuple:
        if action not in {'wait','advance','choose','skip'} or type(frames) is not int or not 1 <= frames <= 180:
            raise ActError('剧情动作须为 wait/advance/choose/skip，frames为1～180')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():raise ActError('本次试玩已结束')
            self._before_advance()
            obs=self.client.call('observe',image=False,entities=False,tiles=False)['obs']
            if not story_pending(obs) and not (obs.get('level') or {}).get('skippingCutscene'):
                raise ActError('当前没有待处理的剧情')
            options=(obs.get('level') or {}).get('dialogChoices') or []
            if action in {'advance','choose'} and obs.get('scene')=='Level' and 'dialogChoices' not in (obs.get('level') or {}):
                raise ActError('推进或选择剧情需要更新桥接0.12.3，避免把分支对话误当普通翻页')
            if action in {'advance','choose'} and ((obs.get('level') or {}).get('paused') or obs.get('pauseMenu')):
                raise ActError('当前暂停菜单尚未关闭，不能把菜单确认当作对话确认')
            if action == 'advance' and options:
                raise ActError('当前有对话选项，必须用 choose 明确指定 choice，或等待/跳过整段剧情')
            used=0
            started=time.monotonic()
            def log(inputs,result):
                nonlocal used
                used += result.get('consumed',0)+result.get('loadingFrames',0)
                self._log_frames(result,expand_inputs(inputs,10000,LETTERS),'(agent处理剧情)',auto=False)
                self._handle_events(result)
            def step(inputs):
                reply=self.client.call('step',inputs=inputs,stop_on=[],speed=1,image=False,
                                       entities=False,tiles=False,progress=PROGRESS_EVERY,on_event=self._on_progress)
                log(inputs,reply['result'])
                return reply['obs']
            skipped=False
            if action=='skip':
                skipped,obs,_,why=skip_by_pause_menu(self.client,on_step=log)
            elif action=='choose':
                if type(choice) is not int or not any(item.get('index')==choice for item in options):
                    raise ActError('choice须为当前可见对话选项的index')
                signature=[(item.get('index'),item.get('text')) for item in options]
                for _ in range(len(options)*2+2):
                    level=obs.get('level') or {}
                    if [(item.get('index'),item.get('text')) for item in level.get('dialogChoices') or []] != signature:
                        raise ActError('对话选项已改变，请重新观察后再选择')
                    if not level.get('dialogChoiceReady'):
                        raise ActError('对话选项仍在动画中，请先 wait 再选择')
                    current=level.get('dialogChoiceIndex')
                    if current==choice:
                        obs=step(['1,C','1']);break
                    obs=step(['1,D' if current < choice else '1,U','1'])
                else:raise ActError('未能定位对话选项，没有按确认')
            else:
                obs=step(['1,C',str(max(1,frames-1))] if action=='advance' else [str(frames)])
            self.metrics.add(play_game_s=time.monotonic()-started,frames_played=used)
            self._commit_completion(obs)
            if not self.done and not story_pending(obs) and not obs.get('controllable'):
                obs,_,_,_=self._auto_advance(obs,None)
            if self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                obs,_,_=self._enter_next_chapter()
            reply=self.client.call('observe',image=True,scope='room',fields=True)
            obs=self._track_motion(enrich_obs(reply['obs']))
            self._update_metrics_from_obs(obs);self._write_workspace(obs)
            info={'action':action,'frames':used,'skipped':skipped,'scene':obs.get('scene'),'finished':self.done}
            if action=='skip' and not skipped:info['note']=why
            return obs,reply.get('image'),info

    def exit_level(self, reason: str) -> dict:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
            raise ActError('退出原因须为1～120字')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():
                raise ActError('本次试玩已停止，不能继续操作菜单')
            self._before_advance()
            self.metrics.update(status='auto',thought=reason.strip())
            before = (self.last_obs or {}).get('frame',0)
            started = time.monotonic()
            obs = leave_level_by_pause_menu(self.client)
            if obs.get('scene') != 'Overworld':
                raise ActError('退出尚未完成，请重新观察当前菜单状态')
            self.metrics.add(auto_game_s=time.monotonic()-started,
                             frames_auto=max(0,obs.get('frame',before)-before))
            self.last_obs, self.end_scene = obs, obs['scene']
            self.chapter_queue.clear()
            self.user_stopped = True
            self._mark_done('agent 退出关卡')
            result = {'exited':True,'scene':obs['scene'],'completed':False,'run_finished':True,
                      'save':self.cfg.save,'save_slot':self.cfg.save_slot,'reason':reason.strip()}
            (self.run_dir/'exit_level.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            return result

    def _handle_events(self, result: dict) -> None:
        for e in result.get("events", []):
            if e["type"] == "death":
                self.metrics.add(deaths=1)
            elif e["type"] == "complete":
                if self.cfg.cutscene_mode == 'agent':self._pending_complete=True
                else:self._chapter_complete()

    def _commit_completion(self, obs):
        if self._pending_complete and not story_pending(obs):
            self._pending_complete=False
            self._chapter_complete()

    def _chapter_complete(self) -> None:
        """当前章节通关：记下分段成绩；还有下一章就等进入下一章，否则本次运行完成。"""
        if self.done or self.chapter_finished:
            return
        st = self.metrics.state
        values = self.goal_values()
        values["map_complete"] = True
        if self.goals.check(values):
            self.user_stopped = True
            self.chapter_queue.clear()
        self.metrics.add_split(st.chapter, st.chapter_time_ticks)
        if self.chapter_queue:
            self.chapter_finished = True
        else:
            self._mark_done("章节完成")

    def finish_at_results(self) -> None:
        """最终章走完片尾，展示并确认结算，停在选章地图，不进入下一章。"""
        if not self.done or self.done_reason != "章节完成" or self.chapter_queue:
            return
        with self.lock:
            if self.goals.immediate:
                return
            obs = self.client.call("observe", image=False, entities=False, tiles=False)["obs"]
            started = time.monotonic()
            results_shown = False
            for _ in range(600):
                self.end_scene = obs.get('scene')
                if self.goals.immediate:
                    return
                if self.end_scene == 'Overworld':
                    menu=obs.get('menu') or {}
                    if menu.get('current') == 'OuiChapterSelect' and not menu.get('next'):
                        self._before_advance()
                        reply=self.client.call('step',inputs=['60'],stop_on=[],speed=1,
                                               image=False,entities=False,tiles=False)
                        self._log_frames(reply['result'],expand_inputs(['60'],10000,LETTERS),'(选章地图停留)',auto=True)
                        self.metrics.add(frames_auto=reply['result']['consumed'])
                        self.end_scene=reply['obs'].get('scene')
                        self.metrics.add(auto_game_s=time.monotonic() - started)
                        print('[run] 章节结算已确认，停在选章地图，不进入下一章',flush=True)
                        return
                    inputs=['1,B','29'] if menu.get('current')=='OuiChapterPanel' and not menu.get('next') else ['10']
                if obs.get("scene") == "AreaComplete":
                    # 先展示结算，再确认；确认只在AreaComplete内发送，地图中绝不按确认。
                    inputs=['120'] if not results_shown else ['1,C','29']
                    results_shown=True
                elif self.end_scene != 'Overworld':
                    if self.end_scene not in {'Level','LevelExit','OverworldLoader'}:
                        raise RuntimeError(f"最终章结算出现意外场景：{self.end_scene}")
                    level=obs.get('level') or {}
                    inputs=['1,C','29'] if level.get('dialogWaiting') else ['30']
                self._before_advance()
                reply = self.client.call("step", inputs=inputs, stop_on=[], speed=self.cfg.auto_speed,
                                         image=False, entities=False, tiles=False)
                self._log_frames(reply["result"], expand_inputs(inputs, 10000, LETTERS), "(最终章结算)", auto=True)
                self.metrics.add(frames_auto=reply["result"]["consumed"])
                obs = reply["obs"]
            raise RuntimeError("最终章节片尾尚未返回选章地图，已停止自动推进")

    def _enter_next_chapter(self) -> tuple[dict, str | None, list[str]]:
        """（调用方持有连接锁）走完结算画面、回到地图、进入下一章，停在可操作的第一帧。"""
        from enter_save import enter_save  # noqa: WPS433（tools 目录已在 sys.path 里）

        split = (self.metrics.state.splits or [])[-1]
        chapter = self.chapter_queue.pop(0)
        self._before_advance()
        self.metrics.update(status="auto", thought="")
        t0 = time.monotonic()
        enter_save(self.client, self.cfg.save, chapter=chapter, menu_speed=1, save_slot=self.cfg.save_slot, cutscene_mode=self.cfg.cutscene_mode,
                   should_stop=self.goals.stop_motion.is_set)  # 正常速度，录像里不快进
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        reply = self.client.call("observe", image=True, scope="room", fields=True)
        obs, image = enrich_obs(reply["obs"]), reply.get("image")
        self.chapter_finished = False
        level = obs.get("level") or {}
        if self.goals.stop_motion.is_set():
            self.chapter_finished = not level or level.get("sid") == split["chapter"]
            return obs, image, ["用户停止了自动换图，游戏保持在当前帧。"]
        self.start_sid = level.get("sid", self.start_sid)
        self.metrics.update(chapter=level.get("sid", ""), room=level.get("room", ""),
                            chapter_time_ticks=level.get("chapterTimeTicks", 0))
        note = (f"【{chapter_name(split['chapter'])}通关】章节计时 {fmt_ticks(split['ticks'])}，死亡 {split['deaths']} 次。"
                f"系统已自动走完结算画面并进入{chapter_name(level.get('sid', ''))}（新章节的计时从 0 开始），"
                f"下面是新章节第一个房间；继续游玩。")
        remaining = "、".join(chapter_name_by_id(c) for c in self.chapter_queue)
        if remaining:
            note += f"之后还有：{remaining}。"
        else:
            note += "这是本次的最后一章。"
        return obs, image, [note]

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
        self._goal_progress = {}
        level = obs.get("level") or {}
        player = obs.get("player") or {}
        if self.sim_energy:
            if level.get("sid") and level.get("room") is not None and not player.get("dead"):
                self.sim_energy.reward(room=f"{level['sid']}:{level.get('mode', level.get('side', 0))}:{level['room']}")
            self.metrics.update(sim_energy=self.sim_energy.snapshot())
        if (level.get("sid"), level.get("room")) != (self.metrics.state.chapter, self.metrics.state.room):
            self.metrics.update(sim_best_frames=0)   # HUD 的"推演最快"只看当前房间
            self.room_entry_frame = obs.get("frame")   # 进入这个房间的那一帧（推演最速从这里算起）
        self.metrics.update(
            chapter=level.get("sid", self.metrics.state.chapter),
            room=level.get("room", self.metrics.state.room),
            chapter_time_ticks=level.get("chapterTimeTicks", self.metrics.state.chapter_time_ticks),
            dashes=player.get("dashes", 0),
            max_dashes=player.get("maxDashes", 0),
        )
