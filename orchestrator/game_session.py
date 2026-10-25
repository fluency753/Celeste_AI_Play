"""游戏会话：通过桥接推进游戏，处理非可控段，记录逐帧日志与决策日志。所有方法都是同步的，由服务端放到线程里调用。"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from .telemetry import publish
from .config import REPO_ROOT, RunConfig, chapter_name, chapter_name_by_id, CHAPTER_SIDS
from .metrics import Metrics
from .observation import enrich_obs, fmt_ticks
from .sandbox import run_program
from .goals import GoalManager
from .modes import act_frame_cap
from .segments import Piece, checkpoint_name, checkpoint_room, chapters_of, piece_label, pieces_of, segments_of, split_segments, validate_segments

sys.path.insert(0, str(REPO_ROOT / "tools"))
from bridge_client import BridgeClient  # noqa: E402
from cutscene import skip_by_pause_menu, leave_level_by_pause_menu, story_pending, story_input  # noqa: E402

LETTERS = set("LRUDJXZGTKYVCBPN")    # 桥接认识的全部按键
AGENT_LETTERS = set("LRUDJXZGTKYV")  # 智能体只能用游玩按键；暂停、确认、返回等菜单键由主控使用
AGENT_KEYS_TEXT = "L R U D J X Z G T K Y V，以及 A:角度"
DEFAULT_STOP_ON = ["death", "room", "complete"]
VALID_STOP_ON = {"death", "room", "uncontrollable", "controllable", "land", "refill", "complete", "story_input"}
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


def parse_plan(data, text: str = "", max_retries: int = MAX_RETRIES,
               max_timeout_s: float = MAX_RETRY_TIMEOUT_S) -> PlanSpec:
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
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= max_retries:
            raise ActError(f'"retry.max" 必须是 1～{max_retries} 的整数')
        if not _number(timeout_s) or not 1 <= timeout_s <= max_timeout_s:
            raise ActError(f'"retry.timeout_s" 必须在 1～{max_timeout_s:g} 秒之间')
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
    piece = None            # 当前这一截（segments.Piece；__init__ 里按计划设置）
    segment_index = 0       # 当前所在的段

    @property
    def _table(self) -> dict:
        """检查点表（按段通关）。"""
        return getattr(self.cfg, "checkpoints", None) or {}

    def __init__(self, cfg: RunConfig, run_dir: Path, metrics: Metrics):
        self.cfg = cfg
        self.run_dir = run_dir
        self.metrics = metrics
        self.lock = threading.Lock()
        self.client: BridgeClient | None = None
        self.done = False
        self.done_reason = ""
        self.user_stopped = False
        self.agent_stop = None
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
        # 依次通关多章或按段通关（segments.py）：当前这一截完成后，主控自动换到队列里的下一截。
        # 队列里的每一项是 Piece（整章也可以直接写章节编号）；chapter_finished = 这一截已完成、正在等着换过去
        plan = list(cfg.plan) if cfg.plan else [Piece(c) for c in (cfg.chapters or ([cfg.chapter] if cfg.chapter is not None else []))]
        self.piece: Piece | None = Piece.of(plan[0]) if plan else None
        self.chapter_queue: list = plan[1:]
        self.segment_index = self.piece.start if self.piece else 0   # 当前所在的段（进入下一个检查点的房间时加一）
        if self.piece is not None and cfg.resume_segment is not None:
            self.segment_index = max(self.piece.start, cfg.resume_segment)   # 续跑：上次已经走到的段
        self.metrics.update(piece_done=False)
        self.chapter_finished = False
        self._pending_complete = False
        self._frames_log = open(run_dir / "frames.jsonl", "a", encoding="utf-8")
        self._decisions_log = open(run_dir / "decisions.jsonl", "a", encoding="utf-8")
        # 容器方式下，观测与轨迹同时写进智能体的工作区，供它自写的程序读取
        self.workspace: Path | None = cfg.workspace_dir if cfg.runtime == "container" else None
        from .sim_assist import SimEnergy
        from .sim_assist import AssistRules
        self.assist_rules = AssistRules.of(cfg)
        self.sim_energy = SimEnergy(run_dir / "sim_energy.json", rules=self.assist_rules) if cfg.sim_assist else None
        self.programs_since_act = 0         # 探索模式：两次 act 之间运行过几次规划程序（explore_program_runs）
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
        self.story_input_supported = 'storyInput' in obs   # 桥接 1.0.3 起才有；中途把剧情切给智能体时也要检查
        if not self.cfg.checkpoints:       # 没有 configs/checkpoints.json：用桥接的检查点表（控制台按段调整要用）
            try:
                if self.client.call("hello").get("checkpoints"):
                    from .segments import table_from_areas
                    self.cfg.checkpoints = table_from_areas(self.client.call("areas").get("areas") or [])
            except Exception:  # noqa: BLE001（只影响按段调整，不影响游玩）
                pass
        if self.cfg.cutscene_mode == 'agent' and 'storyInput' not in obs:
            raise RuntimeError('剧情自动推进需要新版桥接的 storyInput，请更新桥接 Mod 并重启游戏')
        self._pending_complete = bool((obs.get('level') or {}).get('completed'))
        if self.piece is None:        # 没指定章节（进入选章页当前选中的章节）：按游戏里的章节整章玩
            chapter = next((i for i, s in CHAPTER_SIDS.items() if s == (obs.get('level') or {}).get('sid')), None)
            if chapter is not None:
                self.piece = Piece(chapter)
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
        """当前观测。剧情交给智能体时，若剧情还没到输入节点，先自动播放到节点（与 act 之后相同：检查停止目标，
        章节通关时进入下一章）；主控的说明放在 observe_notes 里，由 observe 工具一并返回。"""
        notes: list = []
        with self.lock:
            reply = self.client.call("observe", image=True, scope="room", fields=True)
            obs, image = enrich_obs(reply["obs"]), reply.get("image")
            if self.cfg.cutscene_mode == 'agent' and story_pending(obs) and not self.done:
                obs, image, _, notes = self._auto_advance(obs, image)
                self.goals.check(self.goal_values(obs))
                if self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                    obs, image, more = self._enter_next_chapter()
                    notes = [*notes, *more]
            self._write_sim_state()
        self.observe_notes = [str(n) for n in notes]
        obs = self._track_motion(enrich_obs(obs))
        self._update_metrics_from_obs(obs)
        self._write_workspace(obs)
        return obs, image

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
        return parse_plan(data, text, self.cfg.retry_max, self.cfg.retry_timeout_max_s)

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
        candidates, cost = validate_request(request, self.assist_rules)
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

    def program_run_denied(self) -> str | None:
        """探索模式：两次 act 之间运行规划程序（Bash）的次数到了 explore_program_runs 就拒绝；没到就记一次。"""
        limit = getattr(self.cfg, "explore_program_runs", None)
        if not limit or getattr(self.cfg, "mode", "speedrun") != "explore":
            return None
        if self.programs_since_act >= limit:
            return (f"两次 act 之间最多运行 {limit} 次规划程序，这一次已经用完。"
                    "请先用 act 做一次真实尝试，之后可以再运行。")
        self.programs_since_act += 1
        return None

    def act(self, inputs: list[str] | None, thought: str, stop_on: list[str] | None,
            plan_file: str | None = None) -> ActOutcome:
        self.programs_since_act = 0
        if self.done:
            raise ActError(f"本次试玩已结束（{self.done_reason}），请结束本轮。")
        if ((self.last_obs or {}).get('level') or {}).get('dialogChoices'):
            raise ActError('当前有对话选项，请使用cutscene的choose动作明确选择')
        if (self.cfg.cutscene_mode == 'agent' and story_input(self.last_obs or {}) in {'dialogue','postcard'}
                and not (self.last_obs or {}).get('controllable')):
            raise ActError('当前对白已等待确认，请使用 cutscene 的 advance 或 skip；教学节点才用 act')
        if plan_file:
            if inputs:
                raise ActError("inputs 与 plan_file 只能二选一")
            plan = self.resolve_plan_file(plan_file)
        else:
            plan = PlanSpec(inputs=inputs) if isinstance(inputs, list) else PlanSpec(inputs=[])
        thought = (thought or "").strip()
        if not thought:
            raise ActError("thought 不能为空：用一两句话说明这段操作的意图")
        if len(thought) > self.cfg.thought_max_chars:
            raise ActError(f"thought 太长，请压缩到一两句话（{self.cfg.thought_max_chars} 字以内）")
        forced: list[str] = []          # 剧情交给智能体时强制加的截断条件（自动重试重算计划后也保留）
        if self.cfg.cutscene_mode == 'agent':
            forced.append('story_input')
            if (self.last_obs or {}).get('controllable'):
                forced.append('uncontrollable')
            elif story_input(self.last_obs or {}) == 'tutorial':
                forced.append('controllable')
        stop = self._stop_list(stop_on or plan.stop_on, forced)
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
                new_stop = self._stop_list(stop_on or new_plan.stop_on, forced)
            except ActError as err:
                record["error"] = f"重算后的计划不合法：{err}"
                break
            # 重试次数与条件以第一次提交的计划为准，重算出的计划不能放宽
            plan, stop = replace(new_plan, retry=retry), new_stop
        return ActOutcome(result, obs, image, total_game_s, total_auto, notes, attempts)

    def _stop_list(self, stop_on: list[str] | None, forced: list[str] = ()) -> list[str]:
        stop = list(stop_on) if stop_on else list(DEFAULT_STOP_ON)
        bad = [s for s in stop if s not in VALID_STOP_ON]
        if bad:
            raise ActError(f"未知的截断条件 {bad}；可用：{sorted(VALID_STOP_ON)}")
        for must in ("death", "complete", *forced):
            if must not in stop:
                stop.append(must)
        return stop

    def _check_plan(self, plan: PlanSpec) -> list[str]:
        frames = expand_inputs(plan.inputs, act_frame_cap(self.cfg))
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
        try:  # 这段动作的思考在推进之前就进 HUD 的公开解说历史（动作结束后 decisions.jsonl 用同一个 id）
            publish(self.run_dir, f"act:{self.decision_index}:{attempt}", thought, kind="尝试")
        except OSError:
            pass
        self.metrics.update(status="executing")
        extra = {"expect": plan.expect, "tolerance": plan.tolerance} if plan.expect else {}
        self._before_advance()
        # HUD 每 0.1 秒取一次数据、淡入 0.2 秒：等它换上这段动作的思考语句再推进，
        # 录像里新语句就和这段动作的第一帧同时出现（游戏冻结时录像不写帧，这段等待不进录像）
        time.sleep(self.cfg.thought_lead_s)
        with self.lock:
            if self.goals.stop_motion.is_set():
                raise ActError("已请求停止，本次动作未执行")
            resumed = self._resume_auto_handling() if attempt == 0 else []
            t0 = time.monotonic()
            reply = self.client.call("step", inputs=plan.inputs, stop_on=stop, speed=self.cfg.play_speed, image=True,
                                     scope="room", fields=True, entityTrajectory=True, progress=PROGRESS_EVERY,
                                     on_event=self._on_progress, **self._stop_rooms(), **extra)
            game_s = time.monotonic() - t0
            result, obs, image = reply["result"], enrich_obs(reply["obs"]), reply.get("image")
            if self.sim_energy:
                from .sim_assist import moving_frames
                previous = (self.last_obs or {}).get("player") or {}
                self.sim_energy.reward(moving_frames=moving_frames(result.get("trajectory") or [], previous.get("pos")))
            self._log_frames(result, frames, thought, auto=False)
            self._handle_events(result)
            self._commit_completion(obs)
            self._note_obs_room(obs)
            self.goals.check(self.goal_values(obs))
            self.metrics.add(play_game_s=game_s, frames_played=result["consumed"],
                             decisions=1 if attempt == 0 else 0)

            auto_frames, notes = 0, list(resumed)
            if not self.done and not self.chapter_finished and not self.goals.stop_motion.is_set() and (not obs.get("controllable")
                    or story_pending(obs)):
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

    def _resume_auto_handling(self) -> list[str]:
        """（调用方持有连接锁）自动跳过剧情时，上一次的自动推进可能被用户暂停打断，游戏停在转场、剧情或对白中途：
        先接着自动推进到可操作，再执行智能体的按键，不把按键落在剧情上。推进后仍不可操作（又被暂停）就不执行这次动作。"""
        last = self.last_obs or {}
        if self.cfg.cutscene_mode != 'auto' or not last or last.get('controllable'):
            return []
        obs = enrich_obs(self.client.call("observe", image=False, entities=False, tiles=False)["obs"])
        if obs.get('scene') != 'Level' or obs.get('controllable'):
            return []
        obs, _, _, notes = self._auto_advance(obs, None)
        self.last_obs = obs
        if self.done:
            raise ActError(f"本次试玩已结束（{self.done_reason}），请结束本轮。")
        if self.chapter_finished:
            if not self.goals.stop_motion.is_set():
                self.last_obs, _, more = self._enter_next_chapter()
                notes = [*notes, *more]
            raise ActError('执行前主控接着推进了上次被打断的转场或剧情，章节已通关：' + '；'.join(map(str, notes))
                           + '。这次动作没有执行，请先 observe 再操作。')
        if not obs.get('controllable'):
            raise ActError('游戏还在转场或剧情中，主控的自动推进被打断（' + '；'.join(map(str, notes)) + '），这次动作没有执行；稍后再试')
        return ['执行前，主控先接着自动推进了上次被打断的转场或剧情', *notes]

    def _auto_advance(self, obs: dict, image: str | None):
        """非可控段一律按正常速度推进（与原版一致，录像里不快进）。
        auto模式跳过剧情；agent模式只在实际输入节点或恢复角色控制时交回。"""
        total, notes = 0, []
        history = []

        def remember(observation):
            for item in observation.get('story') or []:
                text = item.get('text', '')
                if not text:
                    continue
                entry = {k: item[k] for k in ('kind', 'text', 'speakerPortrait') if k in item}
                if (history and history[-1].get('kind') == entry.get('kind')
                        and history[-1].get('speakerPortrait') == entry.get('speakerPortrait')):
                    previous = history[-1]['text']
                    if previous.startswith(text):
                        continue
                    if text.startswith(previous):
                        history[-1] = entry
                        continue
                if not history or entry != history[-1]:
                    history.append(entry)
        skip_tries = 0  # 本段剧情已尝试跳过的次数
        menu_closes = 0  # 自动跳过剧情时关掉残留暂停菜单的次数
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
            guard = getattr(self, 'control_guard', None)
            if guard and guard():
                notes.append('用户已暂停，主控停止自动播放。')
                break
            if (obs.get('level') or {}).get('paused') or obs.get('pauseMenu'):
                if self.cfg.cutscene_mode == 'auto' and menu_closes < 3:
                    # 自动跳过剧情时暂停菜单只会是跳过剧情留下的（智能体按不了暂停键）：关掉后接着推进
                    menu_closes += 1
                    reply = self.client.call("step", inputs=["1,B", "20"], stop_on=[], speed=self.cfg.auto_speed,
                                             image=False, entities=False, tiles=False)
                    log_step(["1,B", "20"], reply["result"])
                    obs = reply["obs"]
                    continue
                notes.append('游戏暂停菜单已打开，主控停止自动播放。')
                break
            scene = obs.get("scene")
            remember(obs)
            node = story_input(obs)
            if self.cfg.cutscene_mode == 'agent' and node:
                notes.append({'dialogue': '对白已完整显示，请决定 advance 或 skip。',
                              'choice': '对话选项已就绪，请明确 choose，主控不会代选。',
                              'tutorial': '教学输入已就绪，请阅读提示并用 act 操作；不需要 wait。',
                              'postcard': '明信片已显示完毕，请用 advance 确认。'}[node])
                break
            self._commit_completion(obs)
            if self.done or self.chapter_finished:break
            if scene in END_SCENES:
                self._mark_done(f"离开关卡（{scene}）")
                break
            level = obs.get("level") or {}
            if (scene == "Level" and obs.get("controllable")
                    and (self.cfg.cutscene_mode == 'agent' or not level.get('inCutscene'))):
                break
            if level.get("completed") and not story_pending(obs):
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
            if node == 'choice':
                notes.append('当前需要明确选择对话选项，主控不会代选。')
                break
            inputs = ["1,C", "29"] if self.cfg.cutscene_mode == 'auto' and level.get("dialogWaiting") and not level.get("paused") else ["30"]
            stops = ["controllable", "story_input"] if self.cfg.cutscene_mode == 'agent' else ["controllable", "complete", "story_input"]
            if self.cfg.cutscene_mode == 'auto' and level.get('inCutscene') and obs.get('controllable'):
                stops.remove('controllable')
            reply = self.client.call("step", inputs=inputs, stop_on=stops,
                                     speed=self.cfg.auto_speed, image=False, entities=False, tiles=False,
                                     progress=PROGRESS_EVERY, on_event=self._on_progress, **self._stop_rooms())
            log_step(inputs, reply["result"])
            obs = reply["obs"]
        remember(obs)
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        if total:
            notes.append(f"主控自动推进了 {total} 帧（转场、剧情、死亡与重生，均为正常速度）")
            if not self.done and not self.chapter_finished:
                reply = self.client.call("observe", image=True, scope="room", fields=True)
                obs, image = enrich_obs(reply["obs"]), reply.get("image")
        if total:
            current = [item.get('text') for item in obs.get('story') or []]
            obs['storyPlayback'] = [item for item in history if item['text'] not in current]
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
        if self._note_room(event.get("sid"), event.get("room"), event.get("chapterTimeTicks")):
            self.client.cancel_pending()      # 这一截在终点检查点结束：立即停下（桥接按 stop_rooms 已经停下时多发一次也无妨）
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

    def cutscene(self, action: str, choice: int | None = None) -> tuple:
        if action not in {'advance','choose','skip'}:
            raise ActError('剧情自动播放到下一个输入节点，无需 wait；动作须为 advance/choose/skip，教学请用 act')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():raise ActError('本次试玩已结束')
            self._before_advance()
            obs=self.client.call('observe',image=False,entities=False,tiles=False)['obs']
            if not story_pending(obs) and not (obs.get('level') or {}).get('skippingCutscene'):
                raise ActError('当前没有待处理的剧情')
            original_options = [(item.get('index'),item.get('text')) for item in (obs.get('level') or {}).get('dialogChoices') or []]
            auto_frames, auto_notes = 0, []
            if action != 'skip' and not story_input(obs):
                obs,_,auto_frames,auto_notes = self._auto_advance(obs,None)
            options=(obs.get('level') or {}).get('dialogChoices') or []
            if action in {'advance','choose'} and obs.get('scene')=='Level' and 'dialogChoices' not in (obs.get('level') or {}):
                raise ActError('推进或选择剧情需要更新桥接0.12.3，避免把分支对话误当普通翻页')
            if action in {'advance','choose'} and ((obs.get('level') or {}).get('paused') or obs.get('pauseMenu')):
                raise ActError('当前暂停菜单尚未关闭，不能把菜单确认当作对话确认')
            if action == 'advance' and options:
                raise ActError('当前有对话选项，必须用 choose 明确指定 choice，或等待/跳过整段剧情')
            if action == 'advance' and story_input(obs) not in {'dialogue','postcard'}:
                raise ActError('当前不是对白确认节点；角色或教学仍可操作时请用 act')
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
                if original_options and original_options != [(item.get('index'),item.get('text')) for item in options]:
                    raise ActError('对话选项已改变，请重新观察后再选择')
                if type(choice) is not int or not any(item.get('index')==choice for item in options):
                    raise ActError('choice须为当前可见对话选项的index')
                signature=[(item.get('index'),item.get('text')) for item in options]
                for _ in range(len(options)*2+2):
                    level=obs.get('level') or {}
                    if [(item.get('index'),item.get('text')) for item in level.get('dialogChoices') or []] != signature:
                        raise ActError('对话选项已改变，请重新观察后再选择')
                    if not level.get('dialogChoiceReady'):
                        raise ActError('主控尚未检测到可操作的对话选项，请检查剧情节点状态')
                    current=level.get('dialogChoiceIndex')
                    if current==choice:
                        obs=step(['1,C','1']);break
                    obs=step(['1,D' if current < choice else '1,U','1'])
                else:raise ActError('未能定位对话选项，没有按确认')
            else:
                obs=step(['1,C','1'])
            self.metrics.add(play_game_s=time.monotonic()-started,frames_played=used)
            self._commit_completion(obs)
            self._note_obs_room(obs)
            if not self.done and not self.chapter_finished and (story_pending(obs) or not obs.get('controllable')):
                obs,_,more,notes=self._auto_advance(obs,None)
                auto_frames += more; auto_notes.extend(notes)
            if self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                obs,_,_=self._enter_next_chapter()
            playback = obs.get('storyPlayback')
            reply=self.client.call('observe',image=True,scope='room',fields=True)
            obs=self._track_motion(enrich_obs(reply['obs']))
            if playback:obs['storyPlayback']=playback
            self._update_metrics_from_obs(obs);self._write_workspace(obs)
            info={'action':action,'frames':used,'auto_frames':auto_frames,'auto_notes':auto_notes,
                  'story_input':story_input(obs),'skipped':skipped,'scene':obs.get('scene'),'finished':self.done}
            if action=='skip' and not skipped:info['note']=why
            return obs,reply.get('image'),info

    def stop_run(self, reason: str) -> dict:
        """智能体报告任务阻塞：不算通关、不操作游戏，主控把试玩暂停（章节内保持冻结，菜单正常运行），等用户处理后继续或停止。
        暂停由 HUD 控制器提供（request_pause）；没有控制器时（例如测试）只记下报告。"""
        reason = " ".join((reason or "").split())
        if not reason:
            raise ActError('报告任务阻塞必须说明原因')
        if len(reason) > 200:
            raise ActError('阻塞原因请压缩到 200 字以内')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():
                raise ActError('本次试玩已结束或正在按目标收尾')
            reports = list((self.agent_stop or {}).get('reports') or [])
            reports.append({'reason': reason, 'wall': time.time(), 'decision': self.decision_index})
            self.agent_stop = {'reason': reason, 'source': 'agent', 'reports': reports}
            (self.run_dir/'agent_stop.json').write_text(json.dumps(self.agent_stop,ensure_ascii=False,indent=2),encoding='utf-8')
        pause = getattr(self, 'request_pause', None)
        paused = bool(pause and pause(reason))
        return {'reason': reason, 'source': 'agent', 'paused': paused, 'finished': False, 'completed': False}

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
        self.metrics.add_split(st.chapter, st.chapter_time_ticks, **self._split_range())
        if self.chapter_queue:
            self.chapter_finished = True
            self._save_chapter_plan()
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

    # ---------------------------------------------------------------- 按段通关：一截的终点与换到下一截

    def _end_room(self) -> str | None:
        """当前这一截终点检查点的房间（进入它即完成这一截）；到章节通关为止时为 None。"""
        piece = self.piece
        if piece is None or piece.end is None:
            return None
        return checkpoint_room(self._table, piece.chapter, piece.end)

    def _stop_rooms(self) -> dict:
        """推进时让桥接在进入终点检查点的房间那一帧停下（桥接 1.0.6 起的 stop_rooms；旧桥接忽略，靠进度事件停下）。"""
        room = self._end_room()
        return {"stop_rooms": [room]} if room and not self.chapter_finished and not self.done else {}

    def _note_obs_room(self, obs: dict) -> None:
        level = obs.get("level") or {}
        self._note_room(level.get("sid"), level.get("room"), level.get("chapterTimeTicks"))

    def _note_room(self, sid, room, ticks=None) -> bool:
        """换房间时：更新当前所在的段；进入这一截终点检查点的房间时完成这一截（返回 True）。"""
        piece = self.piece
        if piece is None or room is None or self.done or self.chapter_finished or sid != CHAPTER_SIDS.get(piece.chapter):
            return False
        points = self._table.get(piece.chapter) or []
        stop = piece.end if piece.end is not None else len(points)
        for k in range(self.segment_index + 1, min(stop, len(points))):
            if str(points[k].get("level")) == str(room):
                self.segment_index = k
                self._save_chapter_plan()
                break
        end_room = self._end_room()
        if end_room is None or str(room) != end_room:
            return False
        if ticks is not None:
            self.metrics.update(chapter_time_ticks=ticks)
        self._piece_complete()
        return True

    def _split_range(self) -> dict:
        """分段成绩里这一截的范围（整章不写）。"""
        piece = self.piece
        if piece is None or piece.whole():
            return {}
        return {"start": piece.start, "end": piece.end}

    def _piece_complete(self) -> None:
        """这一截在终点检查点结束：记下分段成绩；还有下一截就等着换过去，否则本次运行完成。"""
        st = self.metrics.state
        self.metrics.add_split(st.chapter, st.chapter_time_ticks, **self._split_range())
        print(f"[run] {piece_label(self.piece, self._table)}完成：章节计时 {fmt_ticks(st.chapter_time_ticks)}", flush=True)
        if self.chapter_queue:
            self.chapter_finished = True
            self._save_chapter_plan()
        else:
            self._mark_done("分段完成")

    def finish_at_checkpoint(self) -> None:
        """最后一截在检查点结束（不是章节结尾）：用暂停菜单返回地图，不让游戏在关卡里无人看管地继续运行。"""
        if not self.done or self.done_reason != "分段完成" or self.chapter_queue:
            return
        with self.lock:
            if self.goals.immediate:
                return
            self._before_advance()
            started = time.monotonic()
            obs = self.client.call("observe", image=False, entities=False, tiles=False)["obs"]
            for _ in range(20):            # 换房间的转场走完再打开暂停菜单
                if obs.get("scene") != "Level" or obs.get("controllable") or (obs.get("level") or {}).get("canPause"):
                    break
                obs = self.client.call("step", inputs=["6"], stop_on=["controllable"], speed=1,
                                       image=False, entities=False, tiles=False)["obs"]
            if obs.get("scene") == "Level":
                obs = leave_level_by_pause_menu(self.client)
            self.end_scene = obs.get("scene")
            self.metrics.add(auto_game_s=time.monotonic() - started)
            print(f"[run] 本次的最后一截已完成，已用暂停菜单返回地图（{self.end_scene}）", flush=True)

    def _enter_next_chapter(self) -> tuple[dict, str | None, list[str]]:
        """（调用方持有连接锁）换到计划里的下一截，停在可操作的第一帧：上一截在章节结尾结束时走完结算画面、
        回到地图再进入下一章（下一截从章节开头开始时走菜单，否则在地图上直接进入它的起点检查点）；
        上一截在检查点结束时，下一截在别的章节的开头就用暂停菜单返回地图再走菜单，否则直接传送到它的起点检查点。"""
        from enter_save import enter_save  # noqa: WPS433（tools 目录已在 sys.path 里）

        split = (self.metrics.state.splits or [])[-1]
        previous = self.piece
        piece = Piece.of(self.chapter_queue.pop(0))
        self._before_advance()
        self.metrics.update(status="auto", thought="")
        t0 = time.monotonic()
        enter_save(self.client, self.cfg.save, chapter=piece.chapter, checkpoint=piece.start, menu_speed=1,
                   save_slot=self.cfg.save_slot, cutscene_mode=self.cfg.cutscene_mode,
                   should_stop=self.goals.stop_motion.is_set)  # 正常速度，录像里不快进
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        reply = self.client.call("observe", image=True, scope="room", fields=True)
        obs, image = enrich_obs(reply["obs"]), reply.get("image")
        self.chapter_finished = False
        level = obs.get("level") or {}
        entered = bool(level) and level.get("sid") == CHAPTER_SIDS.get(piece.chapter) and (
            piece.start == 0 or str(level.get("room")) == str(checkpoint_room(self._table, piece.chapter, piece.start)))
        if self.goals.stop_motion.is_set() and not entered:
            self.chapter_queue.insert(0, piece)            # 还没进入下一截：停在当前帧，续跑时再换过去
            self.chapter_finished = True
            return obs, image, ["用户停止了自动换图，游戏保持在当前帧。"]
        self.piece, self.segment_index = piece, piece.start
        self.start_sid = level.get("sid", self.start_sid)
        self.metrics.update(chapter=level.get("sid", ""), room=level.get("room", ""), piece_done=False,
                            chapter_time_ticks=level.get("chapterTimeTicks", 0))
        self._save_chapter_plan()
        table = self._table
        done = piece_label(previous, table) if previous is not None else chapter_name(split["chapter"])
        how = ("系统已自动走完结算画面并进入" if previous is None or previous.end is None else "系统已把你送到")
        note = (f"【{done}完成】章节计时 {fmt_ticks(split['ticks'])}，死亡 {split['deaths']} 次。"
                f"{how}{self._piece_place(piece)}（新的计时从 0 开始），下面是这一截的第一个房间；继续游玩。"
                f"{self._piece_goal(piece)}")
        remaining = "、".join(piece_label(p, table) for p in self.chapter_queue)
        note += f"之后还有：{remaining}。" if remaining else "这是本次的最后一截。"
        return obs, image, [note]

    def _piece_place(self, piece: Piece) -> str:
        if piece.start == 0:
            return chapter_name_by_id(piece.chapter)
        room = checkpoint_room(self._table, piece.chapter, piece.start)
        return (f"{chapter_name_by_id(piece.chapter)}的检查点"
                f"“{checkpoint_name(self._table, piece.chapter, piece.start)}”（房间 {room}）")

    def _piece_goal(self, piece: Piece) -> str:
        """告诉智能体这一截到哪里为止。"""
        if piece.end is None:
            return "这一截到章节通关为止。"
        room = checkpoint_room(self._table, piece.chapter, piece.end)
        return (f"这一截的终点是检查点“{checkpoint_name(self._table, piece.chapter, piece.end)}”："
                f"进入房间 {room} 即完成，之后由系统接管。")

    # ---------------------------------------------------------------- 运行中调整要通关的范围（控制台）

    def current_chapter_id(self) -> int | None:
        if self.piece is not None:
            return self.piece.chapter
        sid = self.metrics.state.chapter or getattr(self, "start_sid", None)
        return next((i for i, s in CHAPTER_SIDS.items() if s == sid), None)

    def _current_segment(self) -> tuple[int, int] | None:
        chapter = self.current_chapter_id()
        return None if chapter is None else (chapter, self.segment_index)

    def _rest_of_piece(self) -> list[tuple[int, int]]:
        """当前这一截里、当前所在段之后的段（它们之后会连续游玩，也能在控制台取消）。"""
        piece = self.piece
        if piece is None or self.chapter_finished:
            return []
        return [s for s in piece.segments(self._table) if s[1] > self.segment_index]

    def chapter_plan(self) -> dict:
        """控制台显示用：已完成、正在玩与之后要玩的段（[章节, 段]），以及章节编号的简表（兼容旧页面）。"""
        table = self._table
        done = [list(s) for split in (self.metrics.state.splits or []) for s in split_segments(split, table)]
        queue = [list(s) for s in self._rest_of_piece() + segments_of(self.chapter_queue, table)]
        current = self._current_segment()
        ids = {s: i for i, s in CHAPTER_SIDS.items()}
        return {"done": [i for i in (ids.get(sp.get("chapter")) for sp in (self.metrics.state.splits or [])) if i is not None],
                "current": self.current_chapter_id(), "queue": chapters_of(self.chapter_queue),
                "segments": {"done": done, "current": None if current is None or self.chapter_finished else list(current),
                             "queue": queue},
                "switching": bool(self.chapter_finished), "editable": not self.done and not self.chapter_finished}

    def set_chapter_queue(self, queue: list[int]) -> str:
        """（兼容旧控制台）当前章节之后要依次通关的整章；当前这一截不变。"""
        if not isinstance(queue, list) or any(type(c) is not int or c not in CHAPTER_SIDS for c in queue) \
                or len(set(queue)) != len(queue):
            raise ValueError("章节须为不重复的章节编号")
        current = self.current_chapter_id()
        if current is not None and any(c <= current for c in queue):
            raise ValueError(f"只能选{chapter_name_by_id(current)}之后的章节（按官方顺序依次通关）")
        table = self._table
        rest = self._rest_of_piece()
        return self.set_plan(rest + [s for c in sorted(queue) for s in Piece(c).segments(table)])

    def set_plan(self, segments) -> str:
        """运行中改之后要玩的段（[章节, 段] 的列表，按官方顺序；空 = 当前这一段完成后结束）。返回告诉智能体的说明。
        只能选当前所在段之后的段；和当前这一截紧接着的段并进这一截连续游玩，其余的分成截排进队列。"""
        table = self._table
        segments = validate_segments(segments, table) if segments else []
        with self.lock:
            if self.done:
                raise ValueError("本次试玩已结束，不能再改")
            if self.chapter_finished:
                raise ValueError("正在换到下一截，稍后再改")
            current = self._current_segment()
            if current is None or self.piece is None:
                raise ValueError("还不知道当前位置，稍后再改")
            if any(s <= current for s in segments):
                raise ValueError(f"只能选当前所在段（{chapter_name_by_id(current[0])}第 {current[1]} 段）之后的段")
            chapter, k = current
            rest = set(segments)
            end = k + 1
            while (chapter, end) in rest:          # 紧接着当前段的段：并进当前这一截
                rest.discard((chapter, end))
                end += 1
            count = len(table.get(chapter) or []) or 1
            piece = self.piece
            self.piece = Piece(piece.chapter, piece.start, None if end >= count else end)
            self.chapter_queue = pieces_of(sorted(rest), table)
            self._save_chapter_plan()
        table_label = "、".join(piece_label(p, table) for p in self.chapter_queue)
        goal = self._piece_goal(self.piece)
        print(f"[run] 用户调整了要通关的范围：当前这一截 {piece_label(self.piece, table)}；之后 {table_label or '无'}", flush=True)
        if self.chapter_queue:
            return f"用户调整了本次要通关的范围。{goal}之后依次是：{table_label}，不要自行进入其他章节。"
        return f"用户调整了本次要通关的范围。{goal}这是本次的最后一截，完成后本次运行结束。"

    def _save_chapter_plan(self) -> None:
        """把当前计划写进 run_state.json：plan_pieces（已完成的各截 + 当前这一截 + 队列，续跑按已完成的截数接着走）、
        all_chapters（涉及的章节）与 piece_pending（这一截已完成、还没换到下一截）。"""
        path = self.run_dir / "run_state.json"
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        done = []
        ids = {s: i for i, s in CHAPTER_SIDS.items()}
        for split in self.metrics.state.splits or []:
            chapter = ids.get(split.get("chapter"))
            if chapter is not None:
                done.append(Piece(chapter, int(split.get("from") or 0), split.get("to")))
        current = [] if self.chapter_finished or self.piece is None else [self.piece]
        pieces = done + current + [Piece.of(p) for p in self.chapter_queue]
        state["plan_pieces"] = [[p.chapter, p.start, p.end] for p in pieces]
        state["all_chapters"] = chapters_of(pieces)
        state["piece_pending"] = bool(self.chapter_finished)
        state["segment_index"] = None if self.chapter_finished else self.segment_index
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

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
