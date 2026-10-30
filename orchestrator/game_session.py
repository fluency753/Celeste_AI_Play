"""游戏会话：通过桥接推进游戏，处理非可控段，记录逐帧日志与决策日志。所有方法都是同步的，由服务端放到线程里调用。"""
from __future__ import annotations

import json
import hashlib
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from xml.etree.ElementTree import ParseError as ET_ERRORS

from .telemetry import publish
from .config import REPO_ROOT, RunConfig, chapter_name, chapter_name_by_id, CHAPTER_SIDS
from .metrics import Metrics
from .navigation import ChapterNavigation
from .action_policy import BLOCK_STOPS
from .detour import OFFICIAL_MAPS, enabled as detour_enabled, map_label
from .execution import workspace_capabilities
from .collision_snapshot import current_collision, collision_summary
from .collision_view import render_collision_view
from .observation import enrich_obs, fmt_ticks, skip_opportunity_hint
from .sandbox import run_program
from .goals import GoalManager
from .modes import act_frame_cap, act_frame_min, is_explore
from .thought import thought_problem
from .workspace_files import tas
from .workspace_setup import install_game_helpers
from .segments import Piece, checkpoint_name, checkpoint_room, chapters_of, piece_label, pieces_of, segments_of, split_segments, validate_segments

sys.path.insert(0, str(REPO_ROOT / "tools"))
from bridge_client import BridgeClient  # noqa: E402
from cutscene import (skip_by_pause_menu, leave_level_by_pause_menu, story_pending, story_input, story_decision,  # noqa: E402
                      cutscene_skippable)

LETTERS = set("LRUDJXZGTKYVCBPN")    # 桥接认识的全部按键
PLAY_LETTERS = set("LRUDJXZGTKYV")   # 游玩按键（思考僵直时只保持这些）
AGENT_LETTERS = PLAY_LETTERS | {"C"}  # 自动剧情模式可用的按键：游玩按键加 C 确认；agent 模式开放桥接的全部按键
DEFAULT_STOP_ON = ["death", "room", "complete"]
VALID_STOP_ON = {"death", "room", "uncontrollable", "controllable", "land", "refill", "complete", "story_input", "story_decision"}
END_SCENES = {"LevelExit", "AreaComplete", "Overworld", "OverworldLoader"}
ENDING_STUCK_STEPS = 40              # 最终章片尾连续 40 次（每次 30 帧，约 20 秒）画面状态不变就不再空等
FROZEN_GIVE_UP_FRAMES = 600          # 画面冻结等确认时，主控最多推进这么多帧（10 秒，期间每 30 帧按一次确认）就交回
AGENT_STORY_WAIT_FRAMES = 600        # 每次工具调用中，已确认播放阶段的等待总预算；未知状态不推进。
MAP_TRANSITION_WAIT_FRAMES = 1200    # auto 剧情模式：回地图途中（LevelExit、OverworldLoader、菜单切换）最多空帧等 20 秒，仍未就绪就交回


def _automatic_loader_transition(obs: dict) -> bool:
    return obs.get('scene') == 'OverworldLoader' and (obs.get('reload_boundary') or {}).get('mode') == 'wait'


def map_transition(obs: dict) -> bool:
    """离开关卡回地图或地图菜单切换的过渡帧：LevelExit、OverworldLoader，或 Overworld 的菜单正在切换
    （menu.next 非空）、还没有当前菜单。这是导航途中，不是任务结束，也不是可以交给智能体的就绪菜单。"""
    scene = obs.get('scene')
    if scene in {'LevelExit', 'OverworldLoader'}:
        return True
    menu = obs.get('menu') or {}
    return scene == 'Overworld' and (bool(menu.get('next')) or not menu.get('current'))


def map_identity(obs: dict):
    """观测里正在玩（或刚结算）的地图：(sid, 面, 是否已通关)；不在关卡或结算画面时为 None。"""
    scene = obs.get('scene')
    if scene == 'Level':
        level = obs.get('level') or {}
        return (level['sid'], level.get('side') or 'A', bool(level.get('completed'))) if level.get('sid') else None
    if scene == 'AreaComplete':
        done = obs.get('completion') or {}
        return (done['sid'], done.get('side') or 'A', True) if done.get('sid') else None
    return None


def awaiting_confirm(obs: dict, *, ending: bool = False) -> bool:
    """关卡画面冻结、角色不可操作，又不是剧情、对白、暂停菜单或转场：例如第一次拿到某章磁带后的"B 面已解锁"、
    吃心后的诗句。游戏在等确认键（MenuConfirm），不按就一直停着，章节计时却照走。
    ending=True：最终章通关后的片尾（completed 已为真）也算。"""
    level = obs.get("level") or {}
    return (obs.get("scene") == "Level" and not obs.get("controllable") and bool(level.get("frozen"))
            and not level.get("inCutscene") and not level.get("dialogOpen") and not level.get("dialogWaiting")
            and not level.get("paused") and not obs.get("pauseMenu") and not level.get("transitioning")
            and (ending or not level.get("completed")) and not story_input(obs))


IDLE_GIVE_UP_FRAMES = 1800           # 角色不可操作又不在剧情里（见 idle_uncontrolled）时，主控最多自动推进这么多帧（30 秒）就交回
# 玩家操控的不是 Madeline 的房间：(sid, side, room)。5A 镜子后的 void：操控 PlayerSeeker（读方向键与冲刺键），
# Madeline 睡在 Dummy 状态（inControl=false、不在剧情中），桥接按 Madeline 报告"不可操作"
SEEKER_ROOMS = {("Celeste/5-MirrorTemple", "A", "void")}
SEEKER_NOTE = ("这一段操控的是 PlayerSeeker：Madeline 处于 Dummy 状态，桥接按 Madeline 报告不可操作，但这不是剧情。"
               "主控把这一段当作可操作交给你，不再自动推进等 Madeline 恢复控制；按键照常执行（每次至少帧数、思考僵直照常），"
               "PlayerSeeker 的位置在观测的实体里。")
SEEKER_DEATH_NOTE = "这次的 death 事件是 PlayerSeeker 碰到了 Madeline：游戏不计入死亡统计，主控也不计死亡"


def seeker_control(obs: dict) -> bool:
    """SEEKER_ROOMS 里 Madeline 睡着（Dummy）、关卡没有暂停、转场或剧情：这时按键由 PlayerSeeker 读取，主控当作可操作。"""
    if obs.get("scene") != "Level":
        return False
    level = obs.get("level") or {}
    player = obs.get("player") or {}
    if (level.get("sid"), level.get("side"), level.get("room")) not in SEEKER_ROOMS:
        return False
    return (player.get("state") == "Dummy" and not player.get("dead") and not level.get("inCutscene")
            and not level.get("paused") and not obs.get("pauseMenu") and not level.get("transitioning"))


def idle_uncontrolled(obs: dict) -> bool:
    """关卡里 Madeline 活着、不可操作，却不在剧情、对白、暂停菜单或转场里，也不是画面冻结等确认：通常几帧就过去
    （重生、入场动画）。一直这样多半是游戏要你操作别的东西（像 5A 的 PlayerSeeker）或别的意外状态，
    "等到可操作"会空耗章节计时（5A 一次推进 18000 帧，计时白走 5 分钟）。"""
    level = obs.get("level") or {}
    player = obs.get("player") or {}
    return (obs.get("scene") == "Level" and not obs.get("controllable") and bool(player) and not player.get("dead")
            and not level.get("inCutscene") and not level.get("dialogOpen") and not level.get("dialogWaiting")
            and not level.get("paused") and not obs.get("pauseMenu") and not level.get("transitioning")
            and not level.get("frozen") and not level.get("completed") and not story_input(obs))


PROGRESS_EVERY = 6                   # 执行中每 6 帧（0.1 秒）推送一次进度给 HUD

# 自动重试：计划文件里声明重算程序，死亡或偏离预期时由主控在零网络沙箱里运行它，不唤醒大模型
RETRY_REASONS = {"death", "deviate", "done"}
DEFAULT_RETRY_ON = ("death", "deviate")
MAX_RETRIES = 5
MAX_RETRY_TIMEOUT_S = 60.0

# 思考僵直（探索模式 --stiffness）：冻结期间的思考与工具时间按比例变成下一次 act 前的僵直，期间保持上一帧的按键
STIFF_STOP_ON = ["death", "room", "complete", "uncontrollable"]   # 死亡（立即松开所有按键）、换房间、通关、进入剧情时僵直结束，这次的按键不执行
STIFF_MAX_FRAMES = 60 * 60 * 10                 # 与桥接单次推进的上限相同
TAS_HELD_LETTERS = set("LRUDJKXCZVGH")          # TAS 格式里可以"按住"的游玩按键（暂停 S、重试 Q、日志 N、确认 O 不算）


def held_keys(inputs, consumed: int) -> str | None:
    """按键段里第 consumed 帧（从 1 数）按住的键，例如 "R,G"；空串 = 什么都没按；解析不了时返回 None。"""
    done = 0
    try:
        for segment in inputs or []:
            if isinstance(segment, dict):
                n, keys = int(segment.get("n", 0)), str(segment.get("keys") or "")
            else:
                head, _, keys = str(segment).partition(",")
                n = int(head)
            done += max(0, n)
            if done >= consumed:
                return ",".join(k.strip() for k in keys.split(",") if k.strip())
    except (TypeError, ValueError):
        return None
    return None


def minutes_text(seconds: float) -> str:
    return f"{seconds / 60:.1f} 分钟"


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
    input_format: str = 'celeste_ai'
    input_source: dict | None = None


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
    try:input_format = tas.check_format(data.get('input_format', 'celeste_ai'))
    except ValueError as exc:raise ActError(str(exc)) from exc
    source = data.get('input_source')
    if source is not None:
        if not isinstance(source, dict) or any(type(source.get(k)) is not int or source[k] < 0 for k in ('offset','total')):
            raise ActError('input_source requires nonnegative integer offset/total')
        locations = source.get('locations')
        if not isinstance(locations, list) or any(not isinstance(row,list) or len(row)!=2 or any(type(v) is not int or v<0 for v in row) for row in locations):
            raise ActError('input_source.locations requires [line, frame_offset] rows')
    return PlanSpec(list(inputs), expect, float(tolerance), stop_on, retry, text, input_format, source)


class ActError(ValueError):
    """动作不合法：退回给智能体重写，不消耗游戏帧。"""


class EndingNeedsAgent(RuntimeError):
    """片尾之后游戏进入了另一张需要操作的关卡（例如首次通关 7A 后的终章）：不是主控走不完，而是轮到智能体游玩。"""


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
                raise ActError(f"未知按键 {t!r}；可用：{' '.join(sorted(allowed))}，以及 A:角度")
        frames.extend([",".join(tokens)] * n)
        if len(frames) > max_frames:
            raise ActError(f"一次最多推进 {max_frames} 帧，请拆成多次 act")
    if not frames:
        raise ActError("总帧数为 0")
    return frames


class GameSession:
    _ending_pending = False   # 见 __init__
    _ending_agent = False
    _detour = None            # 计划外地图（--allow-detour）：正在玩的非计划地图 {sid, side, completed, deaths}
    _detour_ending = False    # auto 剧情模式：计划外地图刚通关，片尾与结算还没走完（这次动作里由 _finish_detour 处理）
    _detour_agent = False     # 计划外地图的片尾主控没能走完，已交给智能体：不再自动推进，回到选章地图或进入新地图时清除
    _detour_notes = ()        # 进出计划外地图、计划外通关时给智能体的说明，附在这次动作或观测的结果后面
    _detour_latched = False   # 本次会话里开关打开过：之后关掉也继续区分计划内外，地图分类不随开关来回变
    _plan_progress_ok = False # 上一次完整观测停在计划章节 A 面的进行中会话里：这一步的进度事件可以跟上它的章节计时

    @property
    def navigation(self):
        # Lightweight consumers can construct a session without opening game logs.
        if not hasattr(self, '_navigation'):
            self._navigation = ChapterNavigation()
        return self._navigation

    @property
    def piece(self):
        return self.navigation.current

    @piece.setter
    def piece(self, value):
        self.navigation.current = value

    @property
    def chapter_queue(self):
        return self.navigation.queue

    @chapter_queue.setter
    def chapter_queue(self, value):
        self.navigation.queue = list(value)

    @property
    def chapter_finished(self):
        return self.navigation.switching

    @chapter_finished.setter
    def chapter_finished(self, value):
        self.navigation.switching = bool(value)

    @property
    def segment_index(self):
        return self.navigation.segment

    @segment_index.setter
    def segment_index(self, value):
        self.navigation.segment = value

    @property
    def _table(self) -> dict:
        """检查点表（按段通关）。"""
        return getattr(self.cfg, "checkpoints", None) or {}

    def __init__(self, cfg: RunConfig, run_dir: Path, metrics: Metrics):
        if (cfg.play_scope == 'visited' or getattr(cfg, 'completion_policy', 'stop') == 'continue') and cfg.cutscene_mode != 'agent':
            raise ValueError('自由探索或通关后继续需要 agent 剧情模式，由智能体操作结算与选章')
        self.cfg = cfg
        self.run_dir = run_dir
        self.metrics = metrics
        self.lock = threading.RLock()   # 可重入：推进前检查（录像状态）在持锁时也要向桥接发指令
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
        self._input_formats = None
        self._held_input = ("", "celeste_ai")   # 最近一次真正喂给游戏的那一帧按键与格式（思考僵直时保持它）
        self._freeze_base: tuple[float, float, float] | None = None   # 这次冻结开始时的（思考、工具、压缩）累计
        self.replay_stiffness: dict | None = None   # 重放（orchestrator/replay.py）：下一次 act 前按记录僵直
        # Navigation owns the current piece, pending target and entry transition.
        plan = list(cfg.plan) if cfg.plan else [Piece(c) for c in (cfg.chapters or ([cfg.chapter] if cfg.chapter is not None else []))]
        self._navigation = ChapterNavigation.start(plan,
            pending=cfg.cutscene_mode == 'agent' and cfg.resume_pending,
            resume_segment=cfg.resume_segment, previous_sid=self.metrics.state.chapter)
        if cfg.play_scope == 'visited':
            self._navigation = ChapterNavigation()
        self.metrics.update(piece_done=False)
        self._pending_complete = cfg.resume_completion_pending
        # 统计是绕行分类的持久化来源；当前许可关闭不改变已经归属的地图。
        self._detour_latched = cfg.play_scope != 'visited' and bool(
            cfg.resume_detour_classifying or metrics.state.detour_classifying or metrics.state.detour)
        previous_detour = metrics.state.detour if self._detour_latched else None
        self._detour = ({'sid': previous_detour['chapter'], 'side': previous_detour['side'],
                         'completed': bool(previous_detour.get('completed'))} if previous_detour else None)
        self._detour_ending = bool(previous_detour and previous_detour.get('completed')) and cfg.cutscene_mode != 'agent'
        if self._detour_latched:
            self.metrics.update(detour_classifying=True)
        self._ending_pending = False    # auto 剧情模式：最终章刚通关，片尾还没走完（这次动作里由 _finish_ending 处理）
        self._ending_agent = False      # 片尾主控没能走完，交给智能体：开放全部按键，回到选章地图才算完成
        self._idle_handover = False     # 不可操作又不在剧情里等满 IDLE_GIVE_UP_FRAMES，已交给智能体（状态变了就清掉）
        self._seeker_act = False        # 这次动作开始时在操控 PlayerSeeker：Madeline 被它碰到的 death 不计死亡
        self._seeker_death = False
        self._frames_log = open(run_dir / "frames.jsonl", "a", encoding="utf-8")
        self._decisions_log = open(run_dir / "decisions.jsonl", "a", encoding="utf-8")
        # 容器方式下，观测与轨迹同时写进智能体的工作区，供它自写的程序读取
        self.workspace: Path | None = cfg.workspace_dir if workspace_capabilities(cfg).workspace_write else None
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
            install_game_helpers(self.workspace)
            (self.workspace / "game" / "hud_status.json").unlink(missing_ok=True)

    # ---- 连接与观测 ----

    def connect(self) -> dict:
        self.client = BridgeClient(port=self.cfg.bridge_port, timeout=600)
        hello = self.client.call('hello')
        self.game_process_id = hello.get('process_id')
        from .bridge_reload import hold_message
        if held := hold_message(hello):
            # A previous controller's reload hold: never activate or send input into it.
            raise RuntimeError(held)
        from .action_policy import require_pico8_guard
        require_pico8_guard(hello)
        self.bridge_reload_supported = type(hello.get('bridge_reload')) is int and hello['bridge_reload'] == 1
        self.entry_guard_supported = hello.get('entry_guard') is True
        self.chapter_reset_guard_supported = hello.get('chapter_reset_guard') is True
        self.action_policy_supported = hello.get('action_policy')
        self.map_entry_set_supported = hello.get('map_entry_set') is True
        if self.cfg.play_scope == 'visited' and not self.map_entry_set_supported:
            raise RuntimeError('自由探索需要支持 map_entry_set 的桥接 1.10.0；未发送游戏输入')
        if detour_enabled(self.cfg) and not self.map_entry_set_supported:
            raise RuntimeError('计划外地图需要支持 map_entry_set 的桥接 1.10.0；未发送游戏输入')
        self.client.step_options = self._step_options
        self.client.after_step = self._after_step
        self.client.cancel_check = lambda: self.goals.immediate or (self.goals.stop_motion.is_set() and self.done_reason != "章节完成")
        from .chapter_recording import menu_options
        reply = self.client.call("activate", entities=False, tiles=False, continuous_music=self.cfg.continuous_music,
                                 **menu_options(self.cfg))
        obs = reply["obs"]
        self._restore_held_input(obs.get("frame"))
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
        save = (obs.get("save") or {}).get("name")
        if save != self.cfg.save:
            raise RuntimeError(f"当前存档是 {save!r}，与配置的 {self.cfg.save!r} 不一致，拒绝开始")
        slot = (obs.get('save') or {}).get('slot')
        if self.cfg.save_slot is not None and slot != self.cfg.save_slot:
            raise RuntimeError(f'当前存档槽位 {slot} 与指定槽位 {self.cfg.save_slot} 不一致')
        if self.cfg.play_scope == 'visited':
            self._bind_visited_maps(obs)
        if obs.get('scene') == 'Level' and not self.chapter_finished and self.piece is not None:
            sid = (obs.get('level') or {}).get('sid')
            # 开了计划外地图时可以停在别的官方地图里续跑：由 _note_map 记为计划外，计划不变
            if sid != CHAPTER_SIDS.get(self.piece.chapter) and not self._classifying():
                raise RuntimeError(f'当前地图 {sid} 与指定章节不一致，请自行返回选章地图重新选择')
        state = self.metrics.state
        if getattr(self.cfg, '_extended_plan', None) and (state.plan_banked_ticks or state.plan_session_ticks):
            # 续跑追加了新计划：上一个计划最后那段会话的计时不累加进新计划的第一截（没开过计划外地图时两项都是 0）
            self.metrics.update(plan_banked_ticks=0, plan_session_ticks=0)
        self._note_map(obs)
        self._pending_complete |=bool((obs.get('level') or {}).get('completed')) and not self._in_detour()
        if self.cfg.play_scope != 'visited' and self.piece is None and not self.chapter_finished:        # 没指定章节（进入选章页当前选中的章节）：按游戏里的章节整章玩
            chapter = next((i for i, s in CHAPTER_SIDS.items() if s == (obs.get('level') or {}).get('sid')), None)
            if chapter is not None:
                self.piece = Piece(chapter)
        if obs.get("scene") != "Level" and not (self.cfg.cutscene_mode == 'agent' and (
                story_pending(obs) or ((self._pending_complete or self.chapter_finished or self._in_detour()) and obs.get('scene') in END_SCENES)
                or (obs.get('scene') == 'Overworld' and (obs.get('menu') or {}).get('current') in {'OuiChapterSelect','OuiChapterPanel'}))):
            raise RuntimeError(f"当前不在关卡内（scene={obs.get('scene')}），请先进入存档")
        self.navigation.note_scene(obs)
        self.cfg.save_slot = slot
        self.start_sid = ((obs.get('level') or {}).get('sid') or
                          (CHAPTER_SIDS.get(self.cfg.chapter) if self.cfg.play_scope != 'visited' else None) or self.metrics.state.chapter)
        if self.start_sid:self.metrics.update(chapter=self.start_sid,chapter_side=(obs.get('level') or {}).get('side',self.metrics.state.chapter_side))
        if getattr(self.cfg, '_extended_plan', None):
            self.metrics.take_run_base()      # 续跑追加了新计划：新的一局，统计与关卡内时长从这里起算
        self._start_level_clock(obs)
        self._update_metrics_from_obs(obs)
        if self.workspace:
            # 上一次运行留下的观测与动作记录已经过时：先换成当前这一帧，避免智能体在准备阶段读到旧数据
            for stale in ("game/last_act.json", "game/retry.json"):
                (self.workspace / stale).unlink(missing_ok=True)
            reply = self.client.call("observe", image=False, scope="room", fields=True)
            self._write_workspace(self._track_motion(enrich_obs(reply["obs"])))
            self._write_sim_state()
        if self._pending_complete:
            self._persist_completion_pending()
        return obs

    def after_bridge_reload(self, obs: dict) -> None:
        """Publish a fresh baseline after the resident core has finished its hold."""
        with self.lock:
            self.reload_epoch = getattr(self, 'reload_epoch', 0) + 1
            self.reload_observation_required = True
            self._input_formats = None
            self._last_positions, self._last_room = {}, None
            self._collision_png = None
            self._held_input = ('', 'celeste_ai')
            held = self.run_dir / 'held_input.json'
            pending = held.with_suffix('.reload-tmp')
            try:
                pending.write_text(json.dumps({'keys': '', 'input_format': 'celeste_ai', 'frame': obs.get('frame')}), encoding='utf-8')
                pending.replace(held)
            finally:
                pending.unlink(missing_ok=True)
            self.last_obs = enrich_obs(obs)
            reset = getattr(self, 'reset_observation_delivery', None)
            if reset:
                reset()
            self._update_metrics_from_obs(self.last_obs)
            if self.workspace:
                game = self.workspace / 'game'
                game.mkdir(parents=True, exist_ok=True)
                for name in ('last_act.json', 'retry.json', 'sim_state.json', 'latest_obs.json', 'collision.json'):
                    (game / name).unlink(missing_ok=True)
                # Unlike post-act best-effort output, a reload must not report
                # a fresh baseline while leaving stale files behind on failure.
                self._write_json('game/latest_obs.json', {'decision': self.decision_index, 'obs': self.last_obs})
                self._write_collision_view(self.last_obs)
                self._write_sim_state()

    def acknowledge_reload_observation(self, epoch: int) -> None:
        with self.lock:
            if epoch == getattr(self, 'reload_epoch', 0):
                self.reload_observation_required = False

    def _require_reload_observation(self) -> None:
        if getattr(self, 'reload_observation_required', False):
            raise ActError('桥接已重载，当前房间和玩家状态可能变化；请先默认 observe 获取新观测，再重新决定动作。旧计划未执行。')

    def _step_options(self):
        from .action_policy import require_capability, step_options
        try:
            require_capability({'action_policy': getattr(self, 'action_policy_supported', None)})
        except ValueError as exc:
            raise ActError(str(exc)) from exc
        if self.cfg.play_scope == 'visited':
            if not getattr(self, 'map_entry_set_supported', False) or self.cfg.visited_maps is None or self.cfg.save_slot is None:
                raise ActError('自由探索地图许可尚未核定或桥接不支持，未发送输入')
            return step_options(self.cfg, maps=self.cfg.visited_maps, slot=self.cfg.save_slot)
        piece = self.navigation.target
        chapter = piece.chapter if piece else self.cfg.chapter
        if (chapter is not None or self.cfg.save_slot is not None) and not self.entry_guard_supported:
            raise ActError('进入地图的目标核验需要桥接 1.0.22，请更新桥接后继续；未发送输入')
        departure = self.chapter_finished or (self.done and self.done_reason == '分段完成')
        if detour_enabled(self.cfg):
            # 计划外地图：入场只核对存档槽位与官方地图集合（桥接 MapEntryGuard 的 Maps，sid 为空即不限定计划章节）；
            # 计划目标仍由 navigation 持有，入场后按实际观测区分计划内外
            if not getattr(self, 'map_entry_set_supported', False):
                raise ActError('计划外地图需要桥接 1.10.0 的地图集合门禁；未发送输入')
            return self._detour_exit_policy(step_options(self.cfg, maps=OFFICIAL_MAPS, slot=self.cfg.save_slot,
                                                         departure=departure))
        return self._detour_exit_policy(step_options(self.cfg, sid=CHAPTER_SIDS.get(chapter), slot=self.cfg.save_slot,
                                                     departure=departure))

    def _detour_exit_policy(self, options):
        """还在计划外地图里（开关可能已经关掉）：暂停菜单的"返回地图"始终可用，让智能体能离开它回到计划章节。"""
        if not self._in_detour():
            return options
        permissions = dict(options['action_policy']['permissions'], return_to_map=True)
        options['action_policy']['permissions'] = permissions
        options['allow_chapter_reset'] = all(permissions.values())
        return options

    def _bind_visited_maps(self, obs):
        from .free_roam import validate_maps
        maps = validate_maps(self.cfg.visited_maps if self.cfg.visited_maps is not None else
                             (obs.get('save') or {}).get('visited_maps'))
        level = obs.get('level') or {}
        identity = (level if obs.get('scene') == 'Level' else
                    (obs.get('reload_boundary') or {}).get('session') if obs.get('scene') in
                    {'LevelEnter', 'IntroVignette', 'LevelLoader', 'LevelExit', 'AreaComplete'} else None)
        if identity is not None and {'sid': identity.get('sid'), 'side': identity.get('side')} not in maps:
            raise RuntimeError('当前关卡不在已核定的自由探索许可内；未发送游戏输入')
        # Persist before exposing any action. A failed write must not publish an
        # in-memory authorization that recovery cannot reproduce.
        path = self.run_dir / 'run_state.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        state['visited_maps'] = maps
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        self.cfg.visited_maps = maps

    # ---- 思考僵直（探索模式 --stiffness） ----

    # ---- HUD 主计时：本局在关卡里真正执行的帧（见 MetricsState.game_clock） ----

    @staticmethod
    def _clock_level(obs: dict):
        """观测停在关卡里时：(sid, 面, 章节计时, 帧号)；否则 None。"""
        level = obs.get('level') or {}
        if obs.get('scene') != 'Level' or not level.get('sid'):
            return None
        return (level['sid'], level.get('side') or 'A', int(level.get('chapterTimeTicks') or 0), obs.get('frame'))

    def _start_level_clock(self, obs: dict) -> None:
        """连上游戏时接着本局已核实的帧数（续跑沿用，新的一局从 0 起算），从这一刻的状态开始计。"""
        clock = self.metrics.state.game_clock or {}
        self._clock_committed = int(clock.get('committed') or 0)
        self._clock_at = self._clock_level(obs)
        self._clock_dead = False
        self._clock_progress = self._clock_at[3] if self._clock_at else None
        self._publish_level_clock(self._clock_committed, self._clock_at[2] if self._clock_at else 0, self._clock_at is not None)

    def _publish_level_clock(self, frames: int, session_ticks: int, level: bool) -> None:
        """level：这一刻停在关卡里（网页 HUD 只在关卡里执行时外推，菜单里不走）。"""
        clock = {'frames': int(frames), 'committed': int(getattr(self, '_clock_committed', 0)),
                 'session_ticks': int(session_ticks), 'level': bool(level), 'wall': time.time()}
        self.metrics.update(game_clock=clock)

    def _count_level_frames(self, reply: dict) -> None:
        """每次推进之后（所有经本会话连接的推进：动作、自动推进、菜单宏、片尾）核实关卡里执行了几帧。
        有逐帧轨迹时按帧算：有 Madeline 的帧，加上死亡之后到新角色出现之前的帧（死亡动画仍在关卡里）；
        菜单、地图、加载与结算画面没有角色，不算。没有轨迹的推进（开局菜单宏）只在前后都停在同一关卡会话时整段计入。
        规划回放不推进游戏、不经过 step，本来就不计。"""
        if not hasattr(self, '_clock_committed'):
            return
        result = (reply or {}).get('result') or {}
        obs = (reply or {}).get('obs') or {}
        consumed = int(result.get('consumed') or 0)
        end = self._clock_level(obs)
        rows = result.get('trajectory')
        frames = 0
        if isinstance(rows, list) and rows:
            deaths = sorted(e.get('frame') for e in result.get('events') or []
                            if e.get('type') == 'death' and e.get('frame') is not None)
            dead, index = self._clock_dead, 0
            for row in rows:
                frame = row[0] if row else None
                while index < len(deaths) and frame is not None and deaths[index] <= frame:
                    dead, index = True, index + 1
                if len(row) > 1 and row[1] is not None:
                    frames, dead = frames + 1, False
                elif dead:
                    frames += 1
            self._clock_dead = dead
        elif consumed and self._clock_at and end and self._clock_at[:2] == end[:2] and end[2] >= self._clock_at[2]:
            frames = consumed
        self._clock_committed += frames
        self._clock_at = end
        self._clock_progress = end[3] if end else None
        self._publish_level_clock(self._clock_committed, end[2] if end else 0, end is not None)

    def _level_clock_progress(self, event: dict) -> dict:
        """执行中的进度事件只在关卡里发出：相邻两次相差不超过推送间隔的帧都在关卡里，先估进显示用的帧数，
        推进结束后由 _count_level_frames 按实际结果核实。"""
        if not hasattr(self, '_clock_committed'):
            return {}
        frame, previous = event.get('frame'), getattr(self, '_clock_progress', None)
        clock = self.metrics.state.game_clock or {}
        shown = int(clock.get('frames') or self._clock_committed)
        if frame is not None and previous is not None and 0 < frame - previous <= PROGRESS_EVERY:
            shown += frame - previous
        self._clock_progress = frame
        return {'game_clock': {'frames': shown, 'committed': self._clock_committed,
                               'session_ticks': int(event.get('chapterTimeTicks') or 0), 'level': True, 'wall': time.time()}}

    def _after_step(self, args: dict, reply: dict) -> None:
        """BridgeClient.after_step：核实关卡内帧数，再记下最后按住的键。"""
        self._count_level_frames(reply)
        self._remember_held_input(args, reply)

    def _remember_held_input(self, args: dict, reply: dict) -> None:
        """桥接每次推进之后：记下最后真正喂给游戏的那一帧按键（主控的自动推进也算），并存进运行目录供续跑接上。
        停在关卡以外（菜单、加载）时按空帧算；本项目格式只保留游玩按键（确认、返回、暂停、日志不算按住）。"""
        consumed = int(((reply or {}).get("result") or {}).get("consumed") or 0)
        if consumed <= 0:
            return
        keys = held_keys(args.get("inputs"), consumed)
        if keys is None:
            return
        fmt = args.get("input_format") or "celeste_ai"
        obs = (reply or {}).get("obs") or {}
        # 与 expand_inputs 一样规范写法（"+" 分隔、大小写），再只留游玩按键
        tokens = [k.strip().upper() for k in keys.replace("+", ",").split(",") if k.strip()]
        if obs.get("scene", "Level") != "Level":
            tokens, fmt = [], "celeste_ai"
        elif fmt == "celeste_ai":
            tokens = [k for k in tokens if k.startswith("A:") or set(k) <= PLAY_LETTERS]
        else:
            tokens = [k for k in tokens if set(k) <= TAS_HELD_LETTERS]
        self._set_held_input((",".join(tokens), fmt), obs.get("frame"))

    def _set_held_input(self, held: tuple[str, str], frame) -> None:
        """记下并存进运行目录（带当时的帧号，续跑时用来确认游戏停在同一帧）。"""
        self._held_input = held
        try:
            (self.run_dir / "held_input.json").write_text(
                json.dumps({"keys": held[0], "input_format": held[1], "frame": frame}), encoding="utf-8")
        except OSError:
            pass

    def _restore_held_input(self, frame) -> None:
        """续跑：上一段运行保持冻结结束、游戏还停在它最后推进到的那一帧时，接上那时按住的键（桥接仍按着它）。
        交还过控制、或续跑前主控用菜单宏推进过游戏时帧号对不上，按空帧算。"""
        previous = getattr(getattr(getattr(self, "metrics", None), "state", None), "resumed_from", None)
        if not previous:
            return
        ledger = Path(previous) / 'bridge_reload.json'
        if ledger.exists():
            try:
                finished = json.loads(ledger.read_text(encoding='utf-8')).get('phase') == 'finished'
            except (OSError, ValueError, AttributeError):
                finished = False
            if not finished:
                # A failed output refresh may leave the old key file at the
                # same frame. A successor must not replay that pre-reload key.
                self.reload_observation_required = True
                return
        try:
            summary = json.loads((Path(previous) / "summary.json").read_text(encoding="utf-8"))
            data = json.loads((Path(previous) / "held_input.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if summary.get("game_released") is not False or data.get("frame") is None or data.get("frame") != frame:
            return
        self._set_held_input((str(data.get("keys") or ""), str(data.get("input_format") or "celeste_ai")), frame)

    def stiffness_enabled(self) -> bool:
        return bool(getattr(self.cfg, "stiffness", False)) and is_explore(self.cfg)

    def _start_freeze(self) -> None:
        if self.stiffness_enabled():
            self._freeze_base = self.metrics.freeze_counters()

    def _unit_text(self) -> str:
        unit = float(getattr(self.cfg, "stiff_unit_s", 150.0))
        return f"{unit / 60:g} 分钟" if unit >= 60 and (unit * 2) % 60 == 0 else f"{unit:g} 秒"

    def freeze_stats(self) -> dict | None:
        """这次冻结到此刻的思考、工具时间与按规则应僵直多久：思考、工具时间各自满一个计时单位才算，之后按比例。
        上下文压缩（算在思考时间里）扣掉；用户暂停与等待额度本来就不计入思考与工具时间。"""
        if not self.stiffness_enabled() or getattr(self, "_freeze_base", None) is None:
            return None
        think, tool, compact = self.metrics.freeze_counters()
        base_think, base_tool, base_compact = self._freeze_base
        think_s = max(0.0, think - base_think - max(0.0, compact - base_compact))
        tool_s = max(0.0, tool - base_tool)
        unit = float(getattr(self.cfg, "stiff_unit_s", 150.0))
        seconds = ((self.cfg.stiff_think_s * think_s / unit if think_s >= unit else 0.0)
                   + (self.cfg.stiff_tool_s * tool_s / unit if tool_s >= unit else 0.0))
        cap = getattr(self.cfg, "stiff_max_s", None)
        if cap is not None:
            seconds = min(seconds, float(cap))     # 每次最多僵直（可调参数 stiff_max_s）
        frames = min(STIFF_MAX_FRAMES, int(round(seconds * 60)))
        return {"think_s": round(think_s, 1), "tool_s": round(tool_s, 1), "seconds": round(frames / 60, 3),
                "frames": frames}

    def freeze_line(self) -> str:
        """observe 返回开头的一行：这次冻结已累计的时间与预计僵直。"""
        stats = self.freeze_stats()
        if stats is None:
            return ""
        tail = (f"下一次 act 执行前预计僵直 {stats['seconds']:g} 秒（{stats['frames']} 帧）" if stats["frames"]
                else "目前不会僵直")
        return f"思考僵直：这次冻结已思考 {minutes_text(stats['think_s'])}、运行工具 {minutes_text(stats['tool_s'])}，{tail}。"

    def _stiffen(self, stats: dict | None) -> dict:
        """（调用方持有连接锁）按这次冻结的时间先让角色僵直：保持上一帧的按键推进若干帧，不计入 act 的帧数上限。
        死亡（立刻松开所有按键）、换房间、通关、进入剧情或主控请求停止时停下，interrupted=True 表示这次的按键不执行。
        stats 在智能体调用 act 时算好（不含等规划回放放完的时间）；重放时直接给出记录里的僵直。"""
        if not stats:
            return {}
        spent = f"这次冻结思考 {minutes_text(stats.get('think_s', 0))}、运行工具 {minutes_text(stats.get('tool_s', 0))}"
        if stats["frames"] <= 0:
            if getattr(self.cfg, "save_context", False):
                return {}                  # 节省上下文：没有僵直时不写说明（规则在系统提示词里）
            return {"note": f"思考僵直：{spent}，按规则这次不僵直（思考、工具时间各自满 {self._unit_text()}才计）。"}
        last = self.last_obs or {}
        if last.get("scene") != "Level" or not self._agent_controls(last) or story_pending(last):
            self._start_freeze()
            return {"note": f"思考僵直：{spent}，按规则应僵直 {stats['seconds']:g} 秒；角色当前不可操作，这次不僵直。"}
        keys, fmt = (stats["keys"], stats.get("input_format", "celeste_ai")) if "keys" in stats else self._held_input
        stops = list(STIFF_STOP_ON)
        if self.cfg.cutscene_mode == "agent":
            stops.append("story_input")
            if fmt != 'celeste_tas':
                stops.append("story_decision")
            if self._pending_complete:     # 已登记通关后 complete 每帧都在，不能用它截断（与 _stop_list 相同）
                stops.remove("complete")
        extra = {"input_format": fmt} if fmt != "celeste_ai" else {}
        self.metrics.update(status="executing")
        t0 = time.monotonic()
        try:
            reply = self.client.call("step", inputs=[f"{stats['frames']},{keys}" if keys else str(stats["frames"])],
                                     stop_on=stops, speed=self.cfg.play_speed, image=True, scope="room",
                                     fields=True, entityTrajectory=True, progress=PROGRESS_EVERY,
                                     on_event=self._on_progress, **self._stop_rooms(), **extra)
        except Exception:
            self._start_freeze()           # 不为同一段冻结反复尝试（例如桥接拒绝了按键格式），下一次 act 照常执行
            raise
        game_s = time.monotonic() - t0
        result, obs, image = reply["result"], enrich_obs(reply["obs"]), reply.get("image")
        used = int(result.get("consumed") or 0)
        # 先记游戏推进时间再重置冻结：僵直这段不能算进工具时间
        self.metrics.add(auto_game_s=game_s, stiff_frames=used, stiff_s=used / 60)
        self._start_freeze()               # 僵直已经执行：即使后面的按键出错，也不会再为同一段冻结僵直一次
        self._log_frames(result, [keys] * used, "思考僵直", auto=True, input_format=fmt)
        self._note_map(obs)
        self._handle_events(result)
        self._commit_completion(obs)
        self._note_obs_room(obs)
        self.goals.check(self.goal_values(obs))
        held = f"按住上一帧的按键（{keys}）" if keys else "（上一帧没有按键）"
        stop = result.get("stop")
        info = {**{k: stats.get(k) for k in ("think_s", "tool_s", "seconds", "frames")},
                "keys": keys, "input_format": fmt, "consumed": used, "stop": stop}
        if stop == "done" and not self.goals.stop_motion.is_set() and not self.done:
            return {"info": info, "note": f"思考僵直：{spent}，执行你的按键之前先{held}僵直了 {used} 帧（{used / 60:g} 秒）。"}
        why = ("主控已请求停止" if stop == "done" else
               {"death": "死亡，已立刻松开所有按键", "room": "进入了别的房间", "complete": "通关",
                "uncontrollable": "角色失去控制（剧情或转场）", "story_input": "遇到剧情输入节点",
                "story_decision": "遇到剧情决策机会"}.get(stop, f"停下（{stop}）"))
        return {"info": info, "interrupted": True, "result": result, "obs": obs, "image": image,
                "note": f"思考僵直：{spent}，先{held}僵直，第 {used} 帧{why}；这次 act 的按键没有执行。"}

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
        if getattr(self, "_freeze_base", None) is None:
            self._start_freeze()           # 本次运行第一次调用游戏工具：第一次冻结从这里算起（不算准备阶段）
        with self.lock:
            reply = self.client.call("observe", image=True, scope="room", fields=True)
            obs, image = enrich_obs(reply["obs"]), reply.get("image")
            self._commit_completion(obs)
            reload_observation = getattr(self, 'reload_observation_required', False)
            if not reload_observation and self.cfg.cutscene_mode == 'agent' and obs.get('input_format') != 'celeste_tas' and story_pending(obs) and not self.done:
                obs, image, _, notes = self._auto_advance(obs, image)
                self.goals.check(self.goal_values(obs))
            elif (not reload_observation and self.cfg.cutscene_mode == 'auto' and obs.get('scene') == 'Level' and (obs.get('level') or {}).get('inCutscene')
                  and not self._ending_agent and not self._ending_pending and not self.done):
                # 自动跳过剧情：剧情（含跳过的淡出）还没结束就先推进完，不让智能体看着黑屏或剧情画面判断
                obs, image, _, notes = self._auto_advance(obs, image)
                self.goals.check(self.goal_values(obs))
            if not reload_observation and self._detour_ending and not self.done and not self.goals.stop_motion.is_set():
                obs, image, more = self._finish_detour()
                notes = [*notes, *more]
            if not reload_observation and self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                obs, image, more = self._enter_next_chapter(obs)
                notes = [*notes, *more]
            self._note_map(obs)
            notes = [*notes, *self._take_detour_notes()]
            self._write_sim_state()
        if self.cfg.play_scope == 'visited':
            from .free_roam import instructions
            notes.append(instructions(self.cfg))
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
                    self._write_json(room_rel, {"decision": self.decision_index, "obs": obs,
                                               "snapshot": {"historical": True, "kind": "first_observation", "frame": obs.get('frame')}})
            if act_record is not None:
                self._write_json("game/last_act.json", act_record)
        except OSError:
            pass  # 工作区写入失败不影响游戏推进
        try:
            # 碰撞图最后写、单独兜底：它出错（文件被占用、画图异常）不能连带 last_act.json 与房间快照没写，
            # 也不能在已经推进了帧之后让这次 act 报错
            self._write_collision_view(obs)
        except Exception as exc:  # noqa: BLE001
            print(f"[collision] 写碰撞图失败：{exc}", flush=True)

    def collision_png(self, obs: dict) -> bytes | None:
        """这一帧的分色碰撞图：同一帧只画一次（写工作区与工具结果共用）。"""
        if not self.cfg.images:
            return None
        cached = getattr(self, '_collision_png', None)
        if cached is not None and cached[0] is obs:
            return cached[1]
        png = render_collision_view(obs)
        self._collision_png = (obs, png)
        return png

    def _write_collision_view(self, obs: dict) -> None:
        """Publish a same-frame snapshot and an immutable image reference, not a cross-frame cache."""
        directory = self.workspace / 'game' / 'collision'
        manifest = self.workspace / 'game' / 'collision.json'
        previous_image = None
        try:
            previous_image = json.loads(manifest.read_text(encoding='utf-8')).get('image')
        except (OSError, ValueError):
            pass
        png = self.collision_png(obs)
        image = None
        if png is not None:
            directory.mkdir(exist_ok=True)
            name = f"{obs['frame']}-{hashlib.sha256(png).hexdigest()[:16]}.png"
            destination = directory / name
            if not destination.exists():
                temporary = destination.with_suffix('.tmp')
                temporary.write_bytes(png)
                temporary.replace(destination)
            image = 'game/collision/' + name
        self._write_json('game/collision.json', {'collision': current_collision(obs),
                         'collisionNote': collision_summary(obs), 'image': image,
                         'imageRetention': 'current_and_previous; reread this manifest if the image no longer exists'})
        # One-generation grace only: older readers must reload the current manifest after a missing image.
        keep = {Path(p).name for p in (image, previous_image) if isinstance(p, str)}
        if directory.exists():
            for path in directory.glob('*.png'):
                if path.name not in keep:
                    path.unlink(missing_ok=True)

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

    def act_limit_settings(self) -> dict:
        from .tuning import BY_KEY
        key = 'explore_act_frames' if is_explore(self.cfg) else 'max_frames_per_act'
        item = BY_KEY[key]
        return {'key': key, 'frames': act_frame_cap(self.cfg), 'min_frames': act_frame_min(self.cfg),
                'min': item.low, 'max': item.high}

    def update_act_limit(self, data: dict) -> dict:
        """Apply the current mode's action cap only after its resume state is durable."""
        from .tuning import coerce, overrides, DEFAULTS
        if not isinstance(data, dict) or set(data) != {'frames'} or type(data['frames']) is not int:
            raise ValueError('动作帧数上限须为整数 frames')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():
                raise ActError('试玩已结束，不能修改动作帧数上限')
            settings = self.act_limit_settings()
            key = settings['key']
            frames = coerce(key, data['frames'])
            if frames == settings['frames']:
                return {**settings, 'changed': False}
            state_path = self.run_dir / 'run_state.json'
            state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
            tuning = {**overrides(self.cfg), key: frames}
            state['tuning'] = {name: value for name, value in tuning.items() if value != DEFAULTS[name]}
            temporary = state_path.with_suffix('.act-limit.tmp')
            try:
                temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
                temporary.replace(state_path)
            finally:
                temporary.unlink(missing_ok=True)
            setattr(self.cfg, key, frames)
            self.cfg.tuning = state['tuning']
            return {**self.act_limit_settings(), 'changed': True}

    def update_simulation(self, data: dict) -> dict:
        from dataclasses import fields
        from .sim_assist import AssistRules
        from .tuning import coerce, check_relations, overrides
        if not isinstance(data, dict) or set(data) - {'rules', 'balance'} or not data:
            raise ValueError('推演设置只接受 rules 和 balance')
        proposed = data.get('rules', {})
        allowed = {'assist_' + item.name for item in fields(AssistRules)}
        if not isinstance(proposed, dict) or set(proposed) - allowed:
            raise ValueError('包含未知推演参数')
        changes = {key: coerce(key, value) for key, value in proposed.items()}
        with self.lock:
            if not self.sim_energy or self.done or self.goals.stop_motion.is_set():
                raise ActError('当前未在进行有限模拟辅助试玩，不能修改推演设置')
            values = {key: getattr(self.cfg, key) for key in allowed}
            values.update(changes)
            check_relations({**getattr(self.cfg, 'tuning', {}), **values})
            rules = AssistRules(**{item.name: values['assist_' + item.name] for item in fields(AssistRules)})
            balance = data.get('balance')
            if 'balance' in data and (type(balance) is not int or not 0 <= balance <= rules.capacity):
                raise ValueError('当前额度须为0到体力上限之间的整数')
            previous_rules = self.assist_rules
            previous_balance = self.sim_energy.snapshot()['balance']
            snapshot = self.sim_energy.reconfigure(rules, balance)
            state_path = self.run_dir / 'run_state.json'
            try:
                state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
                tuning = {**overrides(self.cfg), **values}
                from .tuning import DEFAULTS
                state['tuning'] = {key: value for key, value in tuning.items() if value != DEFAULTS[key]}
                tmp = state_path.with_suffix('.tmp')
                tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
                tmp.replace(state_path)
            except Exception:
                self.sim_energy.reconfigure(previous_rules, previous_balance)
                raise
            for key, value in values.items():
                setattr(self.cfg, key, value)
            self.cfg.tuning = state['tuning']
            self.assist_rules = rules
            self.metrics.update(sim_energy=snapshot)
            return snapshot

    def simulate(self, request: dict) -> dict:
        self._require_reload_observation()
        from .sim_assist import validate_request, run_predictions
        if not isinstance(request, dict):raise ActError('模拟请求必须是对象')
        if not self.sim_energy:
            raise ActError("本局未开启限额模拟辅助")
        if self.done or self.goals.stop_motion.is_set():
            raise ActError("试玩已结束，不能继续预测")
        # Capture and predict under the same lock: predictions always refer to the actual current frame.
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():
                raise ActError("试玩已结束，不能继续预测")
            original_format = request.get('input_format', 'celeste_ai')
            try:tas.check_format(original_format)
            except ValueError as exc:raise ActError(str(exc)) from exc
            if original_format == 'celeste_tas':
                if set(request) != {'candidates','input_format'}:raise ActError('未知的模拟参数')
                raw_candidates=request['candidates']
                if not isinstance(raw_candidates,list) or not 1 <= len(raw_candidates) <= self.assist_rules.max_candidates:
                    raise ActError('TAS模拟候选数量超出限制')
                try:request = {'candidates':[tas.to_sim(plan,self.assist_rules.max_frames) for plan in raw_candidates]}
                except (TypeError, ValueError) as exc:raise ActError(str(exc)) from exc
            elif 'input_format' in request:
                request = {key:value for key,value in request.items() if key != 'input_format'}
            candidates, cost = validate_request(request, self.assist_rules)
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
        result.update(receipt=ident, cost=cost, energy=self.sim_energy.snapshot(), candidates=candidates,
                      input_format='celeste_ai', requested_input_format=original_format)
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
            plan_file: str | None = None, input_format: str | None = None) -> ActOutcome:
        self._require_reload_observation()
        self.programs_since_act = 0
        if self.done:
            raise ActError(f"本次试玩已结束（{self.done_reason}），请结束本轮。")
        if plan_file:
            if inputs:
                raise ActError("inputs 与 plan_file 只能二选一")
            plan = self.resolve_plan_file(plan_file)
        else:
            plan = PlanSpec(inputs=inputs) if isinstance(inputs, list) else PlanSpec(inputs=[])
        if input_format is not None:
            try:tas.check_format(input_format)
            except ValueError as exc:raise ActError(str(exc)) from exc
            if plan_file and isinstance(json.loads(plan.text), dict) and 'input_format' in json.loads(plan.text) and plan.input_format != input_format:
                raise ActError('input_format 与计划文件声明不一致')
            plan = replace(plan, input_format=input_format)
        thought = (thought or "").strip()
        if not thought:
            raise ActError("thought 不能为空：用一两句话说明这段操作的意图")
        problem = thought_problem(self.cfg, thought)
        if problem:
            raise ActError(problem)
        forced: list[str] = []          # 剧情交给智能体时强制加的截断条件（自动重试重算计划后也保留）
        if self.cfg.cutscene_mode == 'agent':
            forced.append('story_input')
            if plan.input_format != 'celeste_tas':
                forced.append('story_decision')
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

        # 冻结在智能体调用 act 时结束：先算好僵直（不含等规划回放放完、等 HUD 换思考的时间）
        stiff_stats, self.replay_stiffness = (getattr(self, 'replay_stiffness', None) or self.freeze_stats()), None
        self.decision_index += 1
        start_room = self.metrics.state.room
        attempts: list[dict] = []
        total_game_s, total_auto, notes = 0.0, 0, list(pre_notes)
        attempt = 0
        while True:
            label = thought if attempt == 0 else f"{thought}（自动重试 {attempt}）"
            result, obs, image, game_s, auto_frames, auto_notes = self._execute(
                plan, frames, label, stop, plan_file, attempt, stiff_stats)
            total_game_s += game_s
            total_auto += auto_frames
            notes.extend(auto_notes)
            attempts.append({"kind": "execute", "attempt": attempt, "stop": result["stop"],
                             "consumed": result["consumed"], "planned": result["planned"],
                             "inputs": list(plan.inputs), "input_format": plan.input_format,
                             "input_source": plan.input_source,
                             "deviation": result.get("deviation")})
            retry = plan.retry
            reason = result["stop"]
            if result.get("stiffness_interrupted"):
                break                      # 僵直被死亡、换房等打断：这次 act 的按键（含自动重试）都不执行
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
                if new_plan.input_format != plan.input_format:
                    raise ActError('重试计划不能改变 input_format')
                if new_plan.text == plan.text:
                    record["error"] = "重算程序没有改动计划文件"
                    break
                new_plan = self._pad_retry_plan(new_plan)
                frames = self._check_plan(new_plan)
                new_stop = self._stop_list(stop_on or new_plan.stop_on, forced)
            except ActError as err:
                record["error"] = f"重算后的计划不合法：{err}"
                break
            # 重试次数与条件以第一次提交的计划为准，重算出的计划不能放宽
            plan, stop = replace(new_plan, retry=retry), new_stop
        self._start_freeze()               # 动作结束、游戏重新冻结：下一次冻结从这里算起
        return ActOutcome(result, obs, image, total_game_s, total_auto, notes, attempts)

    def _stop_list(self, stop_on: list[str] | None, forced: list[str] = ()) -> list[str]:
        stop = list(stop_on) if stop_on else list(DEFAULT_STOP_ON)
        bad = [s for s in stop if s not in VALID_STOP_ON]
        if bad:
            raise ActError(f"未知的截断条件 {bad}；可用：{sorted(VALID_STOP_ON)}")
        # Completed remains true throughout the heart prompt and results. Once
        # reported, it must not truncate every subsequent agent confirmation.
        ending = ((self.cfg.cutscene_mode == 'agent' and self._pending_complete) or self._ending_agent
                  or self._detour_completed())
        if ending:
            stop = [item for item in stop if item != 'complete']
        for must in ("death", *(("complete",) if not ending else ()), *forced):
            if must not in stop:
                stop.append(must)
        return stop

    def _allowed_letters(self) -> set[str]:
        """这次 act 可用的按键：agent 剧情模式、片尾交给智能体时，以及开了计划外地图（要自己用 P、B 操作暂停菜单与
        选章界面）时开放桥接的全部按键；其余 auto 剧情模式只开放游玩按键与 C。"""
        if (self.cfg.cutscene_mode == 'agent' or self._ending_agent or self._detour_agent
                or detour_enabled(self.cfg) or self._in_detour()):   # 开关关掉时还在计划外地图里：照样能离开
            return LETTERS
        return AGENT_LETTERS

    def _least_frames(self, plan: PlanSpec, planned: int) -> int:
        """这次 act 最少要推进的帧数。只管正常游玩：角色不可操作时（菜单、结算、对白等剧情输入）不限制；
        TAS 文件按游标分段时，文件的最后一段剩多少就放多少（不能补帧，补了会和输入来源对不上）。"""
        if (self.last_obs or {}).get('scene') != 'Level' or not self._agent_controls(self.last_obs or {}):
            return 1
        source = plan.input_source
        if source is not None and source['offset'] + planned >= source['total']:
            return 1
        return act_frame_min(self.cfg)

    def _pad_retry_plan(self, plan: PlanSpec) -> PlanSpec:
        """自动重试重算出的计划不足最短帧数时，在末尾补空帧（死亡、换房等截断条件照样提前停下），免得重试链因此中断。"""
        if plan.input_format != 'celeste_ai' or plan.input_source is not None:
            return plan
        try:
            allowed = self._allowed_letters()
            planned = len(expand_inputs(plan.inputs, act_frame_cap(self.cfg), allowed))
        except ActError:
            return plan                      # 不合法的计划交给 _check_plan 报错
        least = self._least_frames(plan, planned)
        return replace(plan, inputs=list(plan.inputs) + [str(least - planned)]) if planned < least else plan

    def _check_plan(self, plan: PlanSpec) -> list[str]:
        if plan.input_format == 'celeste_tas':
            if self.cfg.cutscene_mode != 'agent':raise ActError('TAS输入须使用agent剧情模式，避免主控额外输入改变时序')
            try:frames = tas.expand(plan.inputs, act_frame_cap(self.cfg))
            except ValueError as exc:raise ActError(str(exc)) from exc
            with self.lock:
                if self._input_formats is None:self._input_formats = self.client.call('hello').get('input_formats', ['celeste_ai'])
            if 'celeste_tas' not in self._input_formats:raise ActError('TAS输入需要桥接1.0.20或更新版本')
        else:
            allowed = self._allowed_letters()
            frames = expand_inputs(plan.inputs, act_frame_cap(self.cfg), allowed)
        least = self._least_frames(plan, len(frames))
        if len(frames) < least:
            raise ActError(f"一次至少推进 {least} 帧（这次只有 {len(frames)} 帧）：请在末尾补上空帧或继续保持按键；"
                           "死亡、换房等截断条件照样会提前停下")
        if plan.input_source is not None and (len(plan.input_source['locations']) != len(frames) or plan.input_source['offset'] + len(frames) > plan.input_source['total']):
            raise ActError('input_source与本次实际计划帧数不一致')
        if plan.expect is not None and len(plan.expect) > len(frames):
            raise ActError(f'"expect" 有 {len(plan.expect)} 帧，比按键的 {len(frames)} 帧还多')
        return frames

    def _execute(self, plan: PlanSpec, frames: list[str], thought: str, stop: list[str], plan_file: str | None,
                 attempt: int, stiff_stats: dict | None = None):
        """执行一次计划：推进游戏、自动推进非可控段、记录日志与工作区数据。"""
        if attempt == 0 and self.playback is not None:
            # 规划回放（游戏不推进）：先等排队的轮次放完（不持有连接锁，回放线程要用），再把镜头送回 Madeline
            self.playback.drain()
            self._before_advance()
            with self.lock:
                self.playback.final()
        self.metrics.set_thought(thought, formal=attempt == 0)   # 正式思考不带"（自动重试 N）"的后缀
        try:  # 这段动作的思考在推进之前就进 HUD 的公开解说历史（动作结束后 decisions.jsonl 用同一个 id）
            publish(self.run_dir, f"act:{self.decision_index}:{attempt}", thought,
                    kind="正式思考" if getattr(self.cfg, "formal_thought", False) else "尝试")
        except OSError:
            pass
        self.metrics.update(status="executing")
        extra = {"expect": plan.expect, "tolerance": plan.tolerance} if plan.expect else {}
        if plan.input_format != 'celeste_ai':extra['input_format'] = plan.input_format
        self._before_advance()
        # HUD 每 0.1 秒取一次数据、淡入 0.2 秒：等它换上这段动作的思考语句再推进，
        # 录像里新语句就和这段动作的第一帧同时出现（游戏冻结时录像不写帧，这段等待不进录像）
        time.sleep(self.cfg.thought_lead_s)
        with self.lock:
            if self.goals.stop_motion.is_set():
                raise ActError("已请求停止，本次动作未执行")
            # 操控 PlayerSeeker 时 Madeline 被它碰到会"死亡"（游戏不计入死亡统计）：这次动作里的 death 不计数
            self._seeker_act, self._seeker_death = seeker_control(self.last_obs or {}), False
            resumed = self._resume_auto_handling() if attempt == 0 else []
            stiff = self._stiffen(stiff_stats) if attempt == 0 else {}
            if stiff.get("note"):
                resumed = [*resumed, stiff["note"]]
            skipped = bool(stiff.get("interrupted"))
            if skipped:
                # 僵直期间死亡、换房或通关：这次的按键不执行，把僵直这一段的结果交给智能体（事件已在僵直时处理）
                game_s, obs, image = 0.0, stiff["obs"], stiff["image"]
                # consumed 是你的按键执行了几帧（0）；trajectory 与 events 是僵直这一段的
                result = {**stiff["result"], "consumed": 0, "planned": len(frames), "deviation": None,
                          "stiffness_interrupted": True}
            else:
                t0 = time.monotonic()
                reply = self.client.call("step", inputs=plan.inputs, stop_on=stop, speed=self.cfg.play_speed, image=True,
                                         scope="room", fields=True, entityTrajectory=True, progress=PROGRESS_EVERY,
                                         on_event=self._on_progress, **self._stop_rooms(), **extra)
                game_s = time.monotonic() - t0
                result, obs, image = reply["result"], enrich_obs(reply["obs"]), reply.get("image")
            if stiff.get("info"):
                result["stiffness"] = stiff["info"]
            result['input_format'] = stiff["info"]["input_format"] if skipped else plan.input_format
            if plan.input_format == 'celeste_tas':
                used = result['consumed']
                source = plan.input_source or {}
                offset = source.get('offset', 0)
                result['input_cursor'] = {'source':source.get('path'), 'sha256':source.get('sha256'),
                                          'start':offset, 'next':offset + used, 'total':source.get('total',len(frames)),
                                          'source_lines':[source.get('start_line'),source.get('end_line')]}
                locations = source.get('locations') or []
                if used < len(locations):result['input_cursor']['next_location'] = locations[used]
                elif source.get('following_location'):result['input_cursor']['next_location'] = source['following_location']
                result['remaining_inputs'] = tas.compress(frames[used:])
            if result.get('stop') == 'recording_boundary' and plan.input_format != 'celeste_tas':
                result['remaining_inputs']=tas.compress(frames[result.get('consumed',0):])
            if self.sim_energy and not skipped:
                from .sim_assist import moving_frames
                previous = (self.last_obs or {}).get("player") or {}
                self.sim_energy.reward(moving_frames=moving_frames(result.get("trajectory") or [], previous.get("pos")))
            if not skipped:
                self._log_frames(result, frames, thought, auto=False, input_format=plan.input_format)
                self._note_map(obs)            # 先认清这一步停在哪张地图：计划外地图的通关、死亡与房间不算进计划
                self._handle_events(result)
                self._commit_completion(obs)
                self._note_obs_room(obs)
            self._seeker_act = False
            if not idle_uncontrolled(obs):
                self._idle_handover = False     # 已离开"不可操作又不在剧情里"的状态：以后照常自动推进
            self.goals.check(self.goal_values(obs))
            self.metrics.add(play_game_s=game_s, frames_played=result["consumed"],
                             decisions=1 if attempt == 0 else 0)

            auto_frames, notes = 0, list(resumed)
            if self._seeker_death:
                notes.append(SEEKER_DEATH_NOTE)
            ending_agent = self._ending_agent       # 片尾已交给智能体：主控不再自动推进
            # 计划外地图的片尾交给了智能体，或智能体自己打开了暂停菜单（auto 模式开了计划外地图）：同样不自动推进
            handed = ending_agent or self._detour_agent or self._agent_menu(obs)
            if not handed and result['stop'] not in BLOCK_STOPS and plan.input_format != 'celeste_tas' and not self.done and (self.cfg.cutscene_mode == 'agent' or not self.chapter_finished) and not self.goals.stop_motion.is_set() and (not self._agent_controls(obs)
                    or story_pending(obs)):
                obs, image, auto_frames, more = self._auto_advance(obs, image)
                notes.extend(more)        # 接在执行前的说明（续推剧情、思考僵直）后面，不覆盖它们
            if self._ending_pending and not self.done and not self.goals.stop_motion.is_set():
                obs, image, more = self._finish_ending()
                notes.extend(more)
            elif ending_agent and not self.done and self._at_chapter_select(obs):
                self.end_scene, self._ending_agent = obs.get('scene'), False
                self._mark_done("章节完成")
                notes.append("已回到选章地图，主控确认本章完成")
            if self._detour_ending and not self.done and not self.goals.stop_motion.is_set():
                obs, image, more = self._finish_detour()
                notes.extend(more)
            elif self._detour_agent and self._at_chapter_menu(obs):
                self._detour_agent = False      # 智能体自己走完了计划外地图的片尾：之后照常自动推进
            if self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                obs, image, more = self._enter_next_chapter(obs)
                notes.extend(more)
            self.goals.check(self.goal_values(obs))
            self._write_sim_state()
            self._note_map(obs)
            notes.extend(self._take_detour_notes())

            if result.get('block'):
                from .action_policy import block_note
                notes.append(block_note(result['block'], detour=detour_enabled(self.cfg)))

        self.metrics.update(last_stop=result["stop"])
        obs = self._track_motion(obs)
        self._update_metrics_from_obs(obs)
        self._write_workspace(obs, {
            "decision": self.decision_index, "attempt": attempt, "thought": thought, "inputs": plan.inputs,
            "plan_file": plan_file, "stop_on": stop, "tolerance": plan.tolerance if plan.expect else None,
            "input_format":plan.input_format, "input_source":plan.input_source,
            "result": result, "auto_frames": auto_frames, "auto_notes": notes,
        })
        self._decisions_log.write(json.dumps({
            "i": self.decision_index, "attempt": attempt, "t": round(self.metrics.elapsed(), 3),
            "effort": self.metrics.state.effort, "thought": thought,
            "fast_requested": self.metrics.state.turn_fast_requested,
            "service_tier_requested": self.metrics.state.turn_service_tier_requested,
            "decision_wait_s": round(self.metrics.state.decision_wait_s, 3) if attempt == 0 else None,
            "inputs": plan.inputs, "plan_file": plan_file, "stop_on": stop, "stop": result["stop"],
            "input_format":plan.input_format, "input_cursor":result.get('input_cursor'),
            "stiffness": result.get("stiffness"),
            "consumed": result["consumed"], "planned": result["planned"], "events": result["events"],
            "deviation": result.get("deviation"), "game_s": round(game_s, 3),
            "auto_frames": auto_frames, "auto_notes": notes,
        }, ensure_ascii=False) + "\n")
        self._decisions_log.flush()
        return result, obs, image, game_s, auto_frames, notes

    # ---- 非可控段：按剧情模式交接，自动处理转场、死亡与重生 ----

    @staticmethod
    def _settled(obs: dict) -> bool:
        """auto 剧情模式下可以把按键交给智能体了：角色可操作，而且剧情（含正在跳过的剧情）已经结束。
        有的剧情在跳过的淡出过程中角色已报告可操作（例如 5A 开场），这时还要接着推进到剧情结束。"""
        return bool(obs.get('controllable')) and not (obs.get('level') or {}).get('inCutscene')

    def _handed_over(self, obs: dict) -> bool:
        """Madeline 不可操作，但按键照样交给智能体、主控不自动推进：在操控 PlayerSeeker；或不可操作又不在剧情里、
        主控已等满 IDLE_GIVE_UP_FRAMES 交回（状态没变）。"""
        return seeker_control(obs) or (self._idle_handover and idle_uncontrolled(obs))

    def _agent_controls(self, obs: dict) -> bool:
        """按键由智能体操作：Madeline 可操作，或 _handed_over。"""
        return bool(obs.get('controllable')) or self._at_chapter_menu(obs) or self._handed_over(obs)

    def _resume_auto_handling(self) -> list[str]:
        """（调用方持有连接锁）自动跳过剧情时，上一次的自动推进可能被用户暂停打断，游戏停在转场、剧情或对白中途：
        先接着自动推进到可操作，再执行智能体的按键，不把按键落在剧情上。推进后仍不可操作（又被暂停）就不执行这次动作。"""
        last = self.last_obs or {}
        if (self.cfg.cutscene_mode != 'auto' or self._ending_agent or not last or self._settled(last)
                or self._handed_over(last) or self._detour_agent or self._agent_menu(last)):
            return []
        obs = enrich_obs(self.client.call("observe", image=False, entities=False, tiles=False)["obs"])
        if obs.get('scene') != 'Level' or self._settled(obs) or self._handed_over(obs) or self._agent_menu(obs):
            return []
        obs, _, _, notes = self._auto_advance(obs, None)
        self.last_obs = obs
        if self._ending_pending and not self.done:
            self.last_obs, _, more = self._finish_ending()
            notes = [*notes, *more]
            raise ActError('执行前主控接着推进了上次被打断的转场或剧情，章节已通关：' + '；'.join(map(str, notes))
                           + ('。这次动作没有执行，请结束本轮。' if self.done else '。这次动作没有执行，请先 observe 再按上面的说明处理片尾。'))
        if self._detour_ending and not self.done:
            self.last_obs, _, more = self._finish_detour()
            notes = [*notes, *more, *self._take_detour_notes()]
            raise ActError('执行前主控接着推进了上次被打断的转场或剧情，计划外地图已通关：' + '；'.join(map(str, notes))
                           + '。这次动作没有执行，请先 observe 再操作。')
        if self.done:
            raise ActError(f"本次试玩已结束（{self.done_reason}），请结束本轮。")
        if self.chapter_finished:
            if not self.goals.stop_motion.is_set():
                self.last_obs, _, more = self._enter_next_chapter()
                notes = [*notes, *more]
            raise ActError('执行前主控接着推进了上次被打断的转场或剧情，章节已通关：' + '；'.join(map(str, notes))
                           + '。这次动作没有执行，请先 observe 再操作。')
        if not obs.get('controllable'):
            if self._handed_over(obs):
                # PlayerSeeker，或不可操作又不在剧情里、主控已等满时限交回：照常执行智能体的按键
                return ['执行前主控接着自动推进，角色仍不可操作但不在剧情里，按你的输入执行', *notes]
            if awaiting_confirm(obs):
                # 画面冻结在等确认、主控按确认也没用：照常执行智能体的按键（它可以发 C 确认），不再拒绝
                return ['执行前主控接着自动推进，画面仍冻结在等确认，按你的输入执行', *notes]
            raise ActError('游戏还在转场或剧情中，主控的自动推进被打断（' + '；'.join(map(str, notes)) + '），这次动作没有执行；稍后再试')
        return ['执行前，主控先接着自动推进了上次被打断的转场或剧情', *notes]

    def _auto_advance(self, obs: dict, image: str | None, *, stop_for_skip: bool = True, story_mode: str | None = None,
                      wait_budget: int = AGENT_STORY_WAIT_FRAMES):
        """非可控段一律按正常速度推进（与原版一致，录像里不快进）。
        auto模式跳过剧情；agent模式在决策机会、实际输入节点或恢复控制时交回。
        显式 watch/advance/choose 后忽略本段的跳过机会，播至下一实际输入节点。"""
        mode = story_mode or self.cfg.cutscene_mode
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
        frozen_frames, confirms = 0, 0  # 连续停在"画面冻结等确认"的帧数、主控按确认的次数
        idle_frames = 0  # 连续"不可操作又不在剧情里"（idle_uncontrolled）的帧数
        map_wait = 0  # auto 模式回地图途中（map_transition）空帧等待的帧数
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
            if self.done or (mode != 'agent' and self.chapter_finished) or self.goals.stop_motion.is_set():
                break
            guard = getattr(self, 'control_guard', None)
            if guard and guard():
                notes.append('用户已暂停，主控停止自动播放。')
                break
            if (obs.get('level') or {}).get('paused') or obs.get('pauseMenu'):
                if mode == 'auto' and menu_closes < 3 and not self._agent_menu(obs):
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
            automatic_transition = _automatic_loader_transition(obs)
            remember(obs)
            node = story_input(obs)
            if mode == 'agent' and node:
                notes.append({'dialogue': '对白已完整显示，请决定 advance 或 skip。',
                              'choice': '对话选项已就绪，请明确 choose，主控不会代选。',
                              'tutorial': '教学输入已就绪，请阅读提示并用 act 操作；不需要 wait。',
                              'memo': '旧纸页面正在等待输入：用 advance 或 act 发送 C 翻页，也可用 U/D 翻页、B 关闭；主控不代按。',
                              'confirm': '当前剧情画面等待确认，请查看画面后用 advance 或 act 发送 C；主控不会代按。',
                              'breathing': '当前是呼吸小游戏，需用 act 操作跳跃 J、冲刺 X 或向上 U 控制羽毛；watch 不会替你操作。',
                              'postcard': '明信片已显示完毕，请用 advance 确认。'}.get(node,
                              f'当前输入节点 {node} 尚未支持，主控停止等待；请查看观测后明确操作。'))
                break
            if mode == 'agent' and stop_for_skip and story_decision(obs) == 'skip':
                notes.append(skip_opportunity_hint(obs))
                break
            self._commit_completion(obs)
            if self.done or (mode != 'agent' and self.chapter_finished):break
            if mode == 'agent' and (self._pending_complete or scene in END_SCENES) and not story_pending(obs) and not automatic_transition:
                notes.append('结尾或结算由你操作：观察当前界面，用 act 发送确认 C、返回 B 或所需的空帧；回到选章地图才算完成。'
                             if self._pending_complete else '当前菜单或转场由你输入操作，按本次目标自行导航；主控不代选。')
                break
            if scene in END_SCENES and not story_pending(obs) and not automatic_transition:
                if self._at_chapter_menu(obs) or self._ending_pending or self._detour_ending:
                    # 回到就绪的选章菜单只是导航，不是完成；最终章刚通关时片尾与结算交给 _finish_ending 收尾，
                    # 计划外地图刚通关时交给 _finish_detour
                    break
                if scene == 'AreaComplete' and self._in_detour():
                    # 计划外地图的结算画面（通关事件没被看到时，例如续跑）：不结束本次试玩，片尾与结算交给 _finish_detour
                    self._chapter_complete()
                    break
                if map_transition(obs):
                    # 不结束整次试玩的返回地图、地图上的菜单切换或加载：auto 模式只推进空帧等它就绪，
                    # 就绪后把菜单交回智能体；不代按菜单，也不因离开关卡结束试玩、清空章节队列
                    if map_wait >= MAP_TRANSITION_WAIT_FRAMES:
                        notes.append(f'回地图途中的转场（{scene}）约 {round(map_wait / 60)} 秒仍未就绪，主控停止等待并交回；'
                                     '本次试玩没有结束，请 observe 查看后再操作。')
                        break
                    reply = self.client.call("step", inputs=["10"], stop_on=["controllable", "complete", "story_input"],
                                             speed=self.cfg.auto_speed, image=False, entities=False, tiles=False,
                                             progress=PROGRESS_EVERY, on_event=self._on_progress, **self._stop_rooms())
                    log_step(["10"], reply["result"])
                    map_wait += reply["result"]["consumed"] + reply["result"].get("loadingFrames", 0)
                    obs = reply["obs"]
                    continue
                if scene == 'Overworld':
                    menu = obs.get('menu') or {}
                    notes.append(f'已在地图菜单（{menu.get("current")}），本次试玩没有结束；菜单由你用 act 操作，主控不代选。')
                    break
                # 结算画面却没有记下通关（正常不会出现）：章节确已结束，照旧结束本次试玩
                self._mark_done(f"离开关卡（{scene}）")
                break
            level = obs.get("level") or {}
            watching_while_controllable = (mode == 'agent' and not stop_for_skip and scene == 'Level'
                                          and obs.get('controllable')
                                          and (obs.get('storyPlaybackState') or {}).get('state') == 'playing')
            if (scene == "Level" and obs.get("controllable")
                    and (mode == 'agent' or not level.get('inCutscene')) and not watching_while_controllable):
                break
            if level.get("completed") and not story_pending(obs):
                self._chapter_complete()
                break
            if seeker_control(obs):
                notes.append(SEEKER_NOTE)  # 操控 PlayerSeeker：交给智能体，不等 Madeline 恢复控制
                break
            if mode == 'agent' and awaiting_confirm(obs):
                notes.append('画面冻结、角色不可操作，游戏可能在等确认键（例如拿到磁带后的"B 面已解锁"、吃心后的诗句）：'
                             '请观察后用 act 发送 C 确认；主控不代按。')
                break
            if mode == 'agent':
                playback = obs.get('storyPlaybackState') or {}
                known_transition = (automatic_transition or scene == 'LevelLoader' or level.get('transitioning')
                                    or (obs.get('player') or {}).get('dead'))
                if playback.get('state') != 'playing' and not known_transition:
                    if idle_uncontrolled(obs):
                        self._idle_handover = True
                    notes.append('当前不是已确认的剧情播放、死亡或转场阶段，主控停止自动等待；'
                                 '未知界面可能在等按键，请查看当前观测后明确操作，不能靠空帧猜测。'
                                 f'播放识别：{playback.get("state", "missing")} / {playback.get("source", "missing")}。')
                    break
                if total >= wait_budget:
                    notes.append(f'本次已等待 {total} 帧，达到剧情播放等待预算；主控交回当前观测。'
                                 '若仍为已确认的播放阶段，可明确 watch 继续；本次未额外发送确认键。')
                    break
            idle = idle_uncontrolled(obs)
            if not idle:
                idle_frames, self._idle_handover = 0, False
            elif idle_frames >= IDLE_GIVE_UP_FRAMES:
                self._idle_handover = True
                notes.append(f'角色不可操作（状态 {(obs.get("player") or {}).get("state")}）又不在剧情、对白、暂停或转场里，'
                             f'主控已等约 {round(idle_frames / 60)} 秒仍未恢复，为免空耗章节计时停止自动推进：请 observe 查看；'
                             '之后的 act 按键照常执行（主控不再先自动推进），解决不了就调用 stop_run 报告。')
                break
            if not level.get("inCutscene"):
                skip_tries = 0  # 一段剧情结束，下一段剧情可以重新尝试
            elif mode == 'auto' and not level.get("skippingCutscene") and skip_tries < 3:
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
            if node == 'breathing':
                notes.append('呼吸小游戏需要明确操作：用 act 的 J、X 或 U 控制羽毛；主控不以空帧代替游玩。')
                break
            waiting = awaiting_confirm(obs)
            if not waiting:
                frozen_frames = 0
            elif frozen_frames >= FROZEN_GIVE_UP_FRAMES:
                notes.append(f'画面冻结约 {round(frozen_frames / 60)} 秒，主控按了 {confirms} 次确认 C 仍未恢复，'
                             '为免空耗章节计时停止自动推进：请 observe 查看，可以用 act 发送按键（含 C 确认）尝试，'
                             '解决不了就调用 stop_run 报告。')
                break
            if mode == 'auto' and node in {'postcard', 'confirm'}:
                inputs = ["1,C", "29"]
            elif mode == 'auto' and waiting:
                inputs = ["1,C", "29"]       # 磁带、吃心等界面在等确认：照真人的做法按确认继续
                confirms += 1
            else:
                inputs = (["1,C", "29"] if mode == 'auto' and level.get("dialogWaiting") and not level.get("paused")
                          else [str(min(1 if watching_while_controllable else 30, wait_budget - total))]
                          if mode == 'agent' else ["30"])
            stops = ["controllable", "story_input"] if mode == 'agent' else ["controllable", "complete", "story_input"]
            if mode == 'agent' and stop_for_skip:
                stops.append('story_decision')
            if watching_while_controllable or (mode == 'auto' and level.get('inCutscene') and obs.get('controllable')):
                stops.remove('controllable')
            reply = self.client.call("step", inputs=inputs, stop_on=stops,
                                     speed=self.cfg.auto_speed, image=False, entities=False, tiles=False,
                                     progress=PROGRESS_EVERY, on_event=self._on_progress, **self._stop_rooms())
            log_step(inputs, reply["result"])
            obs = reply["obs"]
            if waiting:
                frozen_frames += reply["result"]["consumed"]
            if idle:
                idle_frames += reply["result"]["consumed"]
        if confirms and not awaiting_confirm(obs):
            notes.append(f"画面冻结等确认（如磁带、吃心后的提示），主控按了 {confirms} 次确认 C 继续")
        if map_wait:
            notes.append(f"回地图途中的菜单切换或加载：主控只推进了 {map_wait} 帧空帧等待，没有代按菜单")
        remember(obs)
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        if total:
            notes.append(f"主控自动推进了 {total} 帧（转场、剧情、死亡与重生，均为正常速度）")
            if not self.done and (mode == 'agent' or not self.chapter_finished):
                reply = self.client.call("observe", image=True, scope="room", fields=True)
                obs, image = enrich_obs(reply["obs"]), reply.get("image")
        if total:
            current = [item.get('text') for item in obs.get('story') or []]
            obs['storyPlayback'] = [item for item in history if item['text'] not in current]
        return obs, image, total, notes

    def _before_advance(self) -> None:
        """推进游戏之前的检查（录制时由 run.py 设成"确认游戏窗口没有最小化"）；出错不影响游玩。"""
        self._require_reload_observation()
        if self.before_advance is not None:
            from .recording import RecordingHalt
            try:
                self.before_advance()
            except RecordingHalt as exc:
                # 录像录不上又没能另起一段：不推进这些录不上的帧（主控已把试玩暂停）
                raise ActError(f"录像中断：{exc}。主控已暂停试玩，游戏保持冻结，等用户处理；这次按键没有执行，请结束本轮。") from exc
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
            **self._plan_time_progress(event.get("sid"), event.get("chapterTimeTicks")),
            **self._level_clock_progress(event),
        )
        values = self.goal_values()
        values.update(frame=event.get("frame", values.get("frame")), room=event.get("room", values.get("room")),
                      deaths=event.get("deaths", values.get("deaths")), dead=event.get("dead", False),
                      chapter_time_ticks=event.get('chapterTimeTicks', values.get('chapter_time_ticks')))
        values['game_elapsed'] = values['frame']/60 if values['frame'] is not None else None
        values["map"] = f"{event.get('sid', self.metrics.state.chapter)}:{values['side']}"
        self._goal_progress = dict(values)
        # 开过计划外地图时进度事件不带面别：只有上一次完整观测确认的计划章节 A 面会话还在接着走（_plan_time_progress）
        # 才按它推进段与终点；这一步里换了地图或会话时，等推进结束后的完整观测（_note_obs_room）再判断
        if ((not self._classifying() or self._plan_progress_ok)
                and self._note_room(event.get("sid"), event.get("room"), event.get("chapterTimeTicks"))):
            self.client.cancel_pending()      # 这一截在终点检查点结束：立即停下（桥接按 stop_rooms 已经停下时多发一次也无妨）
        self.goals.check(values)
        if self.goals.stop_motion.is_set() and not self.goals.cancel_sent:
            self.goals.cancel_sent = True
            self.client.cancel_pending()

    def _current_goal_progress(self):
        epoch = getattr(self, 'game_process_id', None)
        return self._goal_progress if self._goal_progress.get('game_epoch', epoch) == epoch else {}

    def goal_values(self, obs=None):
        observation = obs if obs is not None else self.last_obs or {}
        level = observation.get("level") or {}
        sid = level.get("sid", self.metrics.state.chapter)
        side = level.get("side", getattr(self.metrics.state, "chapter_side", "A"))
        complete = bool(level.get("completed")) and not story_pending(observation)
        if self.cfg.cutscene_mode == 'agent':
            complete = self._pending_complete and self._at_chapter_select(observation)
        # 没有观测（例如续跑刚连上、还没 observe）时帧号未知：给 None，不用 0 冒充真实帧号，
        # 否则游戏时长目标会锚在 0、把新进程启动以来的菜单与加载帧都算进去
        values = {"room": level.get("room", self.metrics.state.room), "map": f"{sid}:{side}", "side": side,
                "map_complete": complete, "deaths": level.get("deaths", 0),
                "frame": observation.get("frame"), "elapsed": self.metrics.elapsed(),
                "chapter_time_ticks":level.get('chapterTimeTicks'),
                "dead": bool((observation.get("player") or {}).get("dead"))}
        if obs is None:
            progress = self._current_goal_progress()
            if progress.get('frame') is not None and (values['frame'] is None or progress['frame'] >= values['frame']):
                values.update(progress)
            values["elapsed"] = self.metrics.elapsed()
        values['game_epoch'] = getattr(self, 'game_process_id', None)
        values['game_elapsed'] = values['frame']/60 if values['frame'] is not None else None
        return values

    def set_goal(self, spec):
        from .goals import validate_goal
        spec = validate_goal(spec)
        # An in-flight action owns the connection: use its latest progress and
        # do not wait for the whole action. Otherwise refresh a running menu's
        # frame before anchoring relative durations.
        if self.lock.acquire(blocking=False):
            try:
                obs = self.client.call('observe', image=False, entities=False, tiles=False)['obs']
                values = self.goal_values(obs)
                self._goal_progress = dict(values)
                self.goals.set(spec, values)
            finally:
                self.lock.release()
        else:
            self.goals.set(spec, self.goal_values())

    def time_snapshot(self):
        with self.lock:
            values = self.goal_values()
            frame = values['frame']          # 没有观测也没有进度时为 None
            ticks = values.get('chapter_time_ticks')
            return {'version':1, 'game':{
                'sample':'cached_observation_or_progress', 'frame':frame,
                'game_process_id':values.get('game_epoch'),
                'bridge_elapsed_seconds':frame/60 if frame is not None else None,
                'chapter_elapsed_seconds':ticks/10_000_000 if ticks is not None else None},
                'real':{'elapsed_seconds':values['elapsed'], 'playing':self.metrics.playing},
                'goal':self.goals.time_status(values)}

    def finish_user_stop(self):
        self.user_stopped = True
        # 停在计划外地图（哪怕已通关）不是计划通关：不清后续章节、不记分段成绩
        if not self.goals.immediate and not self._in_detour() and (self.cfg.cutscene_mode != 'agent' or self._at_chapter_select(self.last_obs or {})) and (self._pending_complete or
                bool(((self.last_obs or {}).get('level') or {}).get('completed'))):
            self.chapter_queue.clear()
            self._pending_complete=False
            if not self.done:self._chapter_complete()
        if not self.done and (self._ending_pending or self._ending_agent):
            self._ending_pending = self._ending_agent = False
            self._mark_done("章节完成")         # 已通关、片尾没走完：收尾时照常跳过片尾、确认结算
        if not self.done:
            self._mark_done(self.goals.snapshot().get("stop_reason", "用户停止"))

    def cutscene(self, action: str, choice: int | None = None) -> tuple:
        if action not in {'advance','choose','skip','watch'}:
            raise ActError('剧情自动播放到下一个输入节点，无需 wait；动作须为 advance/choose/skip/watch，教学请用 act')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():raise ActError('本次试玩已结束')
            self._before_advance()
            obs=self.client.call('observe',image=False,entities=False,tiles=False)['obs']
            if not story_pending(obs) and not (obs.get('level') or {}).get('skippingCutscene'):
                raise ActError('当前没有待处理的剧情')
            original_options = [(item.get('index'),item.get('text')) for item in (obs.get('level') or {}).get('dialogChoices') or []]
            auto_frames, auto_notes = 0, []
            if action in {'advance','choose'} and not story_input(obs):
                obs,_,auto_frames,auto_notes = self._auto_advance(obs,None,stop_for_skip=False,story_mode='agent')
            def refuse_input(reason, moved_frames=0):
                current = self._track_motion(enrich_obs(obs))
                self._update_metrics_from_obs(current); self._write_workspace(current)
                scope = ('本次确认/选择输入未发送' if not moved_frames
                         else f'已执行 {moved_frames} 帧选项移动，确认键未发送')
                if moved_frames:
                    self.metrics.add(frames_played=moved_frames, play_game_s=time.monotonic()-started)
                raise ActError(reason + f'；{scope}，此前已自动等待 {auto_frames} 帧，新观测已保存。'
                               + '；'.join(map(str, auto_notes)))
            options=(obs.get('level') or {}).get('dialogChoices') or []
            if action in {'advance','choose'} and obs.get('scene')=='Level' and 'dialogChoices' not in (obs.get('level') or {}):
                refuse_input('推进或选择剧情需要更新桥接0.12.3，避免把分支对话误当普通翻页')
            if action in {'advance','choose'} and ((obs.get('level') or {}).get('paused') or obs.get('pauseMenu')):
                refuse_input('当前暂停菜单尚未关闭，不能把菜单确认当作对话确认')
            if action == 'advance' and options:
                refuse_input('当前有对话选项，必须用 choose 明确指定 choice，或等待/跳过整段剧情')
            if action == 'advance' and story_input(obs) not in {'dialogue','postcard','memo','confirm'}:
                refuse_input('当前不是对白、明信片、旧纸或剧情画面确认节点；角色、教学或小游戏操作请用 act')
            if action == 'choose' and story_input(obs) != 'choice':
                refuse_input('主控尚未检测到可操作的对话选项，请检查剧情节点状态')
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
                    refuse_input('对话选项已改变，请重新观察后再选择')
                if type(choice) is not int or not any(item.get('index')==choice for item in options):
                    refuse_input('choice须为当前可见对话选项的index')
                signature=[(item.get('index'),item.get('text')) for item in options]
                for _ in range(len(options)*2+2):
                    level=obs.get('level') or {}
                    if [(item.get('index'),item.get('text')) for item in level.get('dialogChoices') or []] != signature:
                        refuse_input('对话选项已改变，请重新观察后再选择', used)
                    if not level.get('dialogChoiceReady'):
                        refuse_input('主控尚未检测到可操作的对话选项，请检查剧情节点状态', used)
                    current=level.get('dialogChoiceIndex')
                    if current==choice:
                        obs=step(['1,C','1']);break
                    obs=step(['1,D' if current < choice else '1,U','1'])
                else:refuse_input('未能定位对话选项，没有按确认', used)
            elif action=='advance':
                obs=step(['1,C','1'])
            self.metrics.add(play_game_s=time.monotonic()-started,frames_played=used)
            self._commit_completion(obs)
            self._note_obs_room(obs)
            if not self.done and (story_pending(obs) or not obs.get('controllable')):
                obs,_,more,notes=self._auto_advance(obs,None,stop_for_skip=False,story_mode='agent',
                                                  wait_budget=max(0, AGENT_STORY_WAIT_FRAMES-auto_frames))
                auto_frames += more; auto_notes.extend(notes)
            if self.chapter_finished and not self.done and not self.goals.stop_motion.is_set():
                obs,_,_=self._enter_next_chapter(obs)
            playback = obs.get('storyPlayback')
            reply=self.client.call('observe',image=True,scope='room',fields=True)
            obs=self._track_motion(enrich_obs(reply['obs']))
            if playback:obs['storyPlayback']=playback
            self._note_map(obs);auto_notes.extend(self._take_detour_notes())
            self._update_metrics_from_obs(obs);self._write_workspace(obs)
            info={'action':action,'frames':used,'auto_frames':auto_frames,'auto_notes':auto_notes,
                  'story_input':story_input(obs),'story_decision':story_decision(obs),
                  'skipped':skipped,'scene':obs.get('scene'),'finished':self.done}
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

    def exit_level(self, reason: str, finish_run: bool = False) -> dict:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
            raise ActError('退出原因须为1～120字')
        if type(finish_run) is not bool:
            raise ActError('finish_run 须为布尔值；只有用户明确结束整次试玩时才设为 true')
        with self.lock:
            if self.done or self.goals.stop_motion.is_set():
                raise ActError('本次试玩已停止，不能继续操作菜单')
            from .action_policy import permissions_for
            permissions = permissions_for(self.cfg)
            if self._in_detour() and not permissions.return_to_map:
                permissions = permissions.update({'return_to_map': True})   # 还在计划外地图里：总能返回地图离开它
            if not (permissions.return_to_map or (finish_run and permissions.save_quit)):
                raise ActError('主控禁止本次返回地图；没有执行菜单输入。请先报告并等待用户开放返回地图权限，不能改用保存退出绕过。'
                               if not finish_run else '主控禁止未通关返回地图或保存退出；没有执行菜单输入，请等待用户处理')
            self._before_advance()
            self.metrics.update(status='auto',thought=reason.strip())
            before = (self.last_obs or {}).get('frame',0)
            started = time.monotonic()
            exit_ids = (('menu_pause_return', 'intro_vignette_quit') if permissions.return_to_map else ()) + \
                       (('menu_pause_savequit',) if finish_run and permissions.save_quit else ())
            current = self.client.call('observe', image=False, entities=False, tiles=False)['obs']
            before = current.get('frame', before)
            if not finish_run:
                self._commit_completion(current)
                if self.done or self.goals.stop_motion.is_set():
                    self._update_metrics_from_obs(current)
                    self._write_workspace(self._track_motion(enrich_obs(current)))
                    raise ActError('任务或停止目标已结束，退出菜单输入未发送；请查看当前状态')
            already_exited = current.get('scene') == 'Overworld'
            from tools.menu_navigation import MenuActionBlocked, MenuPromptRequired
            try:
                obs = current if already_exited else leave_level_by_pause_menu(self.client, allowed_exit_ids=exit_ids)
                if obs.get('scene') != 'Overworld':
                    raise RuntimeError('退出尚未完成')
            except (RuntimeError, OSError) as exc:
                # A failure can follow real menu inputs. Publish the observed
                # state without retrying an exit or losing the pending target.
                observed = exc.obs if isinstance(exc, (MenuActionBlocked, MenuPromptRequired)) else None
                if not observed:
                    try:
                        observed = self.client.call('observe', image=False, entities=False, tiles=False)['obs']
                    except (RuntimeError, OSError):
                        observed = None
                consumed = max(0, observed.get('frame', before)-before) if observed else None
                if observed:
                    self.metrics.add(auto_game_s=time.monotonic()-started, frames_auto=consumed)
                    self.navigation.note_scene(observed)
                    self._update_metrics_from_obs(observed)
                    self._write_workspace(self._track_motion(enrich_obs(observed)))
                    self.goals.check(self.goal_values(observed))
                scope = f'已观测推进 {consumed} 帧' if consumed is not None else '无法确认已推进帧数'
                raise ActError(f'{exc}；{scope}。退出未确认成功，任务未结束，未重发退出；先 observe 核对当前界面再决定操作。') from exc
            self.metrics.add(auto_game_s=time.monotonic()-started,
                             frames_auto=max(0,obs.get('frame',before)-before))
            self.last_obs, self.end_scene = obs, obs['scene']
            self.navigation.note_scene(obs)
            if finish_run:
                self.chapter_queue.clear()
                self.user_stopped = True
                self._mark_done('agent 退出关卡')
            else:
                # Returning is navigation, never evidence of completing a map.
                # Only an already-observed native completion can commit here.
                self._commit_completion(obs)
            self._update_metrics_from_obs(obs)
            self._write_workspace(self._track_motion(enrich_obs(obs)))
            self.goals.check(self.goal_values(obs))
            self._start_freeze()
            result = {'exited':True,'scene':obs['scene'],'completed':False,'run_finished':self.done,
                      'finish_run':finish_run,'already_exited':already_exited,
                      'stop_requested':self.goals.stop_motion.is_set(),'navigation':self.chapter_plan(),
                      'save':self.cfg.save,'save_slot':self.cfg.save_slot,'reason':reason.strip()}
            (self.run_dir/'exit_level.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            return result

    def travel(self, chapter, checkpoint, thought: str) -> tuple[dict, str | None, list[str]]:
        """智能体请求主控代为换图（travel 工具，计划外地图开关打开时注册，见 travel.py）。核对目标在存档里已解锁，
        然后按玩家的那几步代按：关卡里用暂停菜单返回地图，在选章地图上选章节、打开面板、选 A 面和检查点、进入，
        停在第一帧可操作时交回。换图期间智能体不操作，地图上不再出现它边看边想的空转画面。
        思考僵直不在这里结算：换图前后的思考照常算进下一次 act。"""
        from .action_policy import controller_navigation, permissions_for
        from .travel import available, locked_reason, panel_position, read_progress, resolve
        from enter_save import travel_to  # noqa: WPS433（tools 目录已在 sys.path 里）
        self._require_reload_observation()
        if self.done:
            raise ActError(f"本次试玩已结束（{self.done_reason}），请结束本轮。")
        thought = (thought or "").strip()
        if not thought:
            raise ActError("thought 不能为空：用一两句话说明为什么去")
        problem = thought_problem(self.cfg, thought)
        if problem:
            raise ActError(problem)
        from .segments import load_table
        table = self._table or load_table()
        try:
            dest = resolve(table, chapter, checkpoint)
        except ValueError as exc:
            raise ActError(str(exc)) from exc
        with self.lock:
            if self.goals.stop_motion.is_set():
                raise ActError('已请求停止，本次换图未执行')
            plan_sids = {CHAPTER_SIDS.get(p.chapter) for p in (self.navigation.current, self.navigation.target) if p}
            if not detour_enabled(self.cfg) and dest.sid not in plan_sids:
                raise ActError('计划外地图已被用户关闭：travel 只能回到计划章节')
            if self._pending_complete or self._ending_pending or self._ending_agent or self._detour_ending:
                # 通关待确认或片尾还没走完：先按原来的流程收尾，主控要看见选章地图那一帧才能正确记成绩、结束或换截
                raise ActError('刚通关的章节还在收尾（片尾、结算或回地图没走完），换图未执行；先按当前剧情模式走完结算'
                               '（auto 模式先 observe，主控会代为走完），回到选章地图后再用 travel')
            current = self.client.call('observe', image=False, entities=False, tiles=False)['obs']
            level = current.get('level') or {}
            if self.cfg.cutscene_mode == 'agent' and current.get('scene') == 'AreaComplete':
                raise ActError('现在是结算画面：agent 剧情模式下结算由你自己确认，确认回到选章地图后再用 travel')
            if (current.get('scene') == 'Level' and level.get('sid') == dest.sid and (level.get('side') or 'A') == 'A'
                    and dest.sid in plan_sids and not permissions_for(self.cfg).restart_chapter):
                raise ActError('你正在计划章节里：用 travel 重进计划章节等于重启章节，本次没有这项权限，换图未执行')
            target_piece = next((p for p in (self.navigation.target, self.navigation.current)
                                 if p is not None and CHAPTER_SIDS.get(p.chapter) == dest.sid), None)
            if target_piece is not None and target_piece.end is not None and dest.checkpoint >= target_piece.end:
                raise ActError('计划这一截在这个检查点或之前结束，travel 不能直接进到终点，换图未执行；请从这一截的起点或其间的检查点进入')
            try:
                progress = read_progress((current.get('save') or {}).get('file'))
            except (OSError, ValueError, ET_ERRORS) as exc:
                raise ActError(f'读不到存档的解锁进度（{exc}），换图未执行') from exc
            locked = locked_reason(progress, dest)
            if locked:
                raise ActError(f'{locked}，换图未执行。存档里可去的：{available(table, progress)}')
            self._commit_completion(current)
            if self.done or self.goals.stop_motion.is_set():
                self._update_metrics_from_obs(current)
                raise ActError('任务或停止目标已结束，换图未执行；请查看当前状态')
            self._before_advance()
            self.metrics.set_thought(thought, formal=True)
            self.travels = getattr(self, 'travels', 0) + 1
            try:
                publish(self.run_dir, f"travel:{self.travels}", thought,
                        kind="正式思考" if getattr(self.cfg, "formal_thought", False) else "尝试")
            except OSError:
                pass
            self.metrics.update(status='auto')
            # 从计划章节 A 面的会话里离开：这段会话的章节计时在离开时就结算（不靠回来时"计时倒退"去认）
            departing_plan = (self._classifying() and current.get('scene') == 'Level' and level.get('sid') in plan_sids
                              and (level.get('side') or 'A') == 'A' and not level.get('completed'))
            plan_ticks = int(level.get('chapterTimeTicks') or 0) if departing_plan else 0
            passed_map = []

            def on_map(map_obs):
                passed_map.append(True)
                self.navigation.note_scene(map_obs)     # 已经离开原关卡（换下一截时的确认要求先离开）

            def panel_slot(map_obs):
                # 返回地图时游戏刚写完存档：重读一次，面板按存档里检查点的记录顺序列出（含这次离开前新到的）
                try:
                    return panel_position(table, read_progress((map_obs.get('save') or {}).get('file')), dest)
                except (OSError, ValueError, ET_ERRORS) as exc:
                    raise RuntimeError(f'回到地图后读不到存档里的检查点（{exc}）') from exc
            started = time.monotonic()
            print(f"[run] 智能体请求换图：{dest.label}", flush=True)
            try:
                with controller_navigation(self.client, self.cfg, sid=dest.sid, slot=self.cfg.save_slot):
                    travel_to(self.client, self.cfg.save, dest.chapter, panel_slot, save_slot=self.cfg.save_slot,
                              cutscene_mode=self.cfg.cutscene_mode, should_stop=self.goals.stop_motion.is_set,
                              on_map=on_map)
                failure = None
            except (RuntimeError, OSError) as exc:
                failure = exc
            self.metrics.add(auto_game_s=time.monotonic() - started)
            reply = self.client.call('observe', image=True, scope='room', fields=True)
            obs, image = enrich_obs(reply['obs']), reply.get('image')
            state = self.metrics.state
            if departing_plan and passed_map and not state.piece_done:
                banked = int(state.plan_banked_ticks or 0) + max(int(state.plan_session_ticks or 0), plan_ticks)
                self.metrics.update(plan_banked_ticks=banked, plan_session_ticks=0)
                print(f"[run] 离开计划章节：此前累计 {fmt_ticks(banked)}", flush=True)
            self.navigation.note_scene(obs)
            self._note_map(obs)
            entering = obs.get('scene') in ('LevelEnter', 'IntroVignette') and self.cfg.cutscene_mode == 'agent'
            arrived = entering or (obs.get('scene') == 'Level' and (obs.get('level') or {}).get('sid') == dest.sid
                                   and ((obs.get('level') or {}).get('side') or 'A') == 'A')
            if failure is not None or not arrived:
                why = str(failure) if failure is not None else '没有停在目标地图里'
                notes = [f'主控换图没有完成（{why}）；没有重试。请先看这次观测确认停在哪里，再决定下一步。']
            elif entering:
                notes = [f'主控已进入{dest.label}，停在入场画面：agent 剧情模式下由你自己确认（cutscene 或 act）。']
            else:
                room = (obs.get('level') or {}).get('room')
                notes = [f'主控已把你送到{dest.label}：现在在房间 {room}，第一帧可操作，游戏冻结中；继续游玩。']
                if dest.checkpoint > 0 and dest.room and room != dest.room:
                    notes.append(f'注意：进到的房间 {room} 不是这个检查点的房间 {dest.room}，请先看观测确认位置。')
            if self.chapter_finished and not self.done and obs.get('scene') == 'Level':
                piece = self.navigation.target
                if piece is not None and self._confirm_next_entry(obs, require_departure=True):
                    notes.append(f'已确认你进入{self._piece_place(piece)}。{self._piece_goal(piece)}')
            notes += self._take_detour_notes()
            self.goals.check(self.goal_values(obs))
            self._write_sim_state()
        record = {'travel': self.travels, 'wall': time.time(), 'chapter': dest.chapter, 'checkpoint': dest.checkpoint,
                  'room': dest.room, 'arrived': arrived and failure is None,
                  'at': {'scene': obs.get('scene'), 'sid': (obs.get('level') or {}).get('sid'),
                         'room': (obs.get('level') or {}).get('room')},
                  'error': None if failure is None else str(failure), 'thought': thought}
        try:
            with (self.run_dir / 'travel.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        except OSError:
            pass
        obs = self._track_motion(obs)
        self._update_metrics_from_obs(obs)
        self._write_workspace(obs)
        return obs, image, notes

    def _handle_events(self, result: dict) -> None:
        for e in result.get("events", []):
            if e["type"] == "death":
                if self._seeker_act:
                    # PlayerSeeker 碰到 Madeline：游戏用 Die(registerDeathInStats: false) 结束这一段，不计死亡
                    self._seeker_death = True
                    continue
                self.metrics.add(deaths=1)
            elif e["type"] == "complete":
                if self._in_detour():
                    # 计划外地图通关：只记绕行成绩，不登记计划的待确认通关、不结束本次试玩
                    if not self.done:
                        self._detour_complete()
                elif self.cfg.cutscene_mode == 'agent':
                    if not self._pending_complete:
                        self._pending_complete=True
                        self._persist_completion_pending()
                else:self._chapter_complete()

    def _commit_completion(self, obs):
        self._note_map(obs)
        if (self.cfg.cutscene_mode == 'agent' and not self._pending_complete and (obs.get('level') or {}).get('completed')
                and not self._in_detour()):
            self._pending_complete = True
            self._persist_completion_pending()
        ready = (self._at_chapter_select(obs) if self.cfg.cutscene_mode == 'agent' else
                 not story_pending(obs) and not _automatic_loader_transition(obs))
        if self._pending_complete and ready:
            self.end_scene = obs.get('scene')
            self._pending_complete=False
            self._chapter_complete()
            self._persist_completion_pending()

    def _persist_completion_pending(self):
        path = self.run_dir / 'run_state.json'
        if not path.exists():return
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            state['completion_pending'] = self._pending_complete
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(path)
        except (OSError, ValueError) as exc:
            print(f'[run] 未能保存待确认结算状态：{exc}', flush=True)

    @staticmethod
    def _at_chapter_menu(obs):
        menu = obs.get('menu') or {}
        return (obs.get('scene') == 'Overworld' and menu.get('current') in
                {'OuiChapterSelect','OuiChapterPanel'} and not menu.get('next'))

    @staticmethod
    def _at_chapter_select(obs):
        menu = obs.get('menu') or {}
        return GameSession._at_chapter_menu(obs) and menu.get('current') == 'OuiChapterSelect'

    def _chapter_complete(self) -> None:
        """当前章节通关：记下分段成绩；还有下一章就等进入下一章，否则本次运行完成。
        auto 剧情模式的最终章：先不结束，片尾在这次动作里走完（_finish_ending），走不完交给智能体。
        计划外地图（--allow-detour）的通关另记一条绕行成绩，不动分段成绩与计划（_detour_complete）。"""
        if self._in_detour():
            if not self.done:
                self._detour_complete()
            return
        if self.done or self.chapter_finished or self._ending_pending or self._ending_agent:
            return
        st = self.metrics.state
        values = self.goal_values()
        values["map_complete"] = True
        if self.goals.check(values):
            self.user_stopped = True
            self.chapter_queue.clear()
        split_options = {'side': st.chapter_side} if getattr(self.cfg, 'play_scope', 'chapter') == 'visited' else {}
        self.metrics.add_split(st.chapter, self._plan_ticks(), **self._split_range(), **split_options)
        if getattr(self.cfg, 'play_scope', 'chapter') == 'visited':
            if self.user_stopped or self.goals.immediate:
                self._mark_done('目标达成')
            elif getattr(self.cfg, 'completion_policy', 'stop') == 'stop':
                self._mark_done('章节完成')
        elif self.navigation.complete_current():
            self._save_chapter_plan()
        elif getattr(self.cfg, 'completion_policy', 'stop') == 'continue' and not self.user_stopped and not self.goals.immediate:
            self._save_chapter_plan()
        elif self.cfg.cutscene_mode != 'agent' and not self.goals.immediate and not self.user_stopped:
            self._ending_pending = True     # 用户的停止目标触发时照旧立即结束，片尾由 finish_at_results 收尾
        else:
            self._mark_done("章节完成")

    def _finish_ending(self) -> tuple[dict, str | None, list[str]]:
        """（调用方持有连接锁）auto 剧情模式的最终章刚通关：在这次动作里跳过片尾剧情、确认结算并回到选章地图，
        回到了就结束本次运行；走不完就把剩下的片尾交给智能体（开放确认、返回、暂停等按键），回到选章地图才算完成。"""
        self._ending_pending = False
        problem, playable = None, None
        try:
            self._run_ending(self.client.call("observe", image=False, entities=False, tiles=False)["obs"])
        except EndingNeedsAgent as exc:
            playable = str(exc)
        except RuntimeError as exc:
            problem = str(exc)
        reply = self.client.call("observe", image=True, scope="room", fields=True)
        obs, image = enrich_obs(reply["obs"]), reply.get("image")
        if self._at_chapter_select(obs) or self.goals.immediate:
            self.end_scene = obs.get('scene')
            self._mark_done("章节完成")
            return obs, image, ["通关后主控走完了片尾、确认了结算，已回到选章地图"]
        self._ending_agent = True
        if playable:
            print(f"[run] 最终章{playable}，交给智能体", flush=True)
            return obs, image, [f"章节已通关，章节计时已经停止；{playable}，角色可以操作，主控不代走。"
                                "请用 act 自己操作：走动、C 确认、B 返回、P 打开暂停菜单（菜单里可选跳过剧情）、方向键选择，"
                                "纯帧数等待动画；回到选章地图才算完成。"]
        print(f"[run] 最终章片尾主控没能走完（{problem}），交给智能体处理", flush=True)
        return obs, image, [f"章节已通关，章节计时已经停止；片尾主控没能自动走完（{problem or '没有回到选章地图'}）。"
                            "请观察画面后用 act 自行处理：C 确认、B 返回、P 打开暂停菜单（菜单里可选跳过剧情）、方向键选择，"
                            "纯帧数等待动画；回到选章地图才算完成，主控不再代按。"]

    # ---------------------------------------------------------------- 计划外地图（--allow-detour，见 detour.py）

    def _plan_map(self, sid, side) -> bool:
        """这张地图是不是计划里的：当前这一截（换下一截途中还有下一截）的章节 A 面。没有计划时一律算计划内。"""
        if getattr(self.cfg, 'play_scope', 'chapter') == 'visited':
            return True
        pieces = [p for p in (self.navigation.current, self.navigation.target) if p is not None]
        if not pieces:
            return True
        return side == 'A' and sid in {CHAPTER_SIDS.get(p.chapter) for p in pieces}

    def _in_detour(self) -> bool:
        """正在计划外地图里（或刚从它结算回到地图、还没进入别的地图）。开关关掉后仍按实际地图结束。"""
        return self._detour is not None

    def _classifying(self) -> bool:
        """区分计划内外：开关打开着，或本次会话里打开过（关掉后地图分类不跟着变，计划章节的 B/C 面仍是计划外）。"""
        return detour_enabled(self.cfg) or self._detour_latched

    def off_plan(self) -> bool:
        """总结用：最后停在计划外地图，当前章节计时不计入总计时。"""
        return self._in_detour()

    def _detour_completed(self) -> bool:
        return self._detour is not None and bool(self._detour.get('completed'))

    def _add_detour_note(self, text: str) -> None:
        self._detour_notes = [*self._detour_notes, text]

    def _take_detour_notes(self) -> list[str]:
        notes, self._detour_notes = list(self._detour_notes), []
        return notes

    def _agent_menu(self, obs: dict) -> bool:
        """auto 剧情模式开了计划外地图（或还在计划外地图里）时，智能体在正常游玩中自己按 P 打开的暂停菜单
        （不在剧情里）：由它操作，主控不当作跳过剧情留下的菜单关掉，也不自动推进。"""
        level = obs.get('level') or {}
        return (self.cfg.cutscene_mode == 'auto' and (detour_enabled(self.cfg) or self._in_detour())
                and obs.get('scene') == 'Level'
                and bool(level.get('paused') or obs.get('pauseMenu')) and not level.get('inCutscene')
                and not level.get('skippingCutscene'))

    # ---- 计划章节的计时：每段会话（返回地图后重进、重启章节、游戏重启）的章节计时累加，计划外地图不算 ----

    def _plan_ticks(self) -> int:
        """这一截的成绩计时：此前各段会话之和加当前这段（没有累加过时与原来相同，就是当前章节计时）。"""
        state = self.metrics.state
        return int(state.chapter_time_ticks) + int(getattr(state, 'plan_banked_ticks', 0) or 0)

    def _note_plan_time(self, ticks, live: bool) -> None:
        """完整观测停在计划章节 A 面：计时倒退说明开始了新的一段会话，把上一段最后看到的计时存起来
        （这一截已经完成时不再累加，例如通关后继续时重新进入）。"""
        state = self.metrics.state
        last = int(state.plan_session_ticks or 0)
        if ticks is not None:
            ticks = int(ticks)
            if ticks < last:
                banked = 0 if state.piece_done else int(state.plan_banked_ticks or 0) + last
                self.metrics.update(plan_banked_ticks=banked, plan_session_ticks=ticks)
                print(f"[run] 计划章节开始新的一段会话：此前累计 {fmt_ticks(banked)}", flush=True)
            elif ticks > last:
                self.metrics.update(plan_session_ticks=ticks)
        self._plan_progress_ok = live

    def _plan_time_progress(self, sid, ticks) -> dict:
        """进度事件：上一次完整观测确认还在计划章节 A 面的会话里时，跟上它的章节计时（离开关卡前最后几帧也算进去）。
        进度事件不带面别：sid 变了或计时倒退说明这一步里离开了这段会话，之后等完整观测再判断。"""
        if not self._plan_progress_ok or ticks is None:
            return {}
        pieces = [p for p in (self.navigation.current, self.navigation.target) if p is not None]
        if sid not in {CHAPTER_SIDS.get(p.chapter) for p in pieces} or int(ticks) < int(self.metrics.state.plan_session_ticks or 0):
            self._plan_progress_ok = False
            return {}
        return {'plan_session_ticks': int(ticks)}

    def _note_map(self, obs) -> None:
        """按实际观测（关卡或结算画面）认出正在玩的地图：不是计划章节 A 面的就是计划外地图。
        开关打开过就一直区分（关掉后也按实际地图结束绕行）；从没打开过时与原来相同，不做任何记录。"""
        if detour_enabled(self.cfg):
            self._detour_latched = True
            if not self.metrics.state.detour_classifying:
                self.metrics.update(detour_classifying=True)
        identity = map_identity(obs) if isinstance(obs, dict) else None
        if identity is None:
            self._plan_progress_ok = False
            return
        sid, side, completed = identity
        current = self._detour
        if self._plan_map(sid, side):
            if self._classifying():
                ticks = ((obs.get('level') or {}).get('chapterTimeTicks') if obs.get('scene') == 'Level'
                         else (obs.get('completion') or {}).get('chapterTimeTicks'))
                self._note_plan_time(ticks, live=obs.get('scene') == 'Level' and not completed)
            if current is not None:
                self._detour = None
                self._detour_ending = self._detour_agent = False
                self.metrics.update(detour=None)
                print(f"[run] 回到计划章节 {chapter_name(sid)}", flush=True)
                self._add_detour_note(f'已回到计划章节{chapter_name(sid)}：继续本次任务，计划章节的计时接着此前累计；'
                                      '计划章节通关后本次任务结束。')
            return
        self._plan_progress_ok = False
        if current is not None and (current['sid'], current['side']) == (sid, side) and (completed or not current['completed']):
            return                              # 同一次进入
        if not self._classifying():
            return
        if self._pending_complete:
            # agent 模式：计划章节已通关、还没确认就一次走进了计划外地图。先按计划确认那次通关，不把它算成绕行。
            # 这一步的进度事件已把章节与章节计时换成了新地图的：成绩按计划章节与它最后看到的计时记
            self._pending_complete = False
            plan_sid = CHAPTER_SIDS.get(self.piece.chapter) if self.piece is not None else None
            self.metrics.update(chapter=plan_sid or self.metrics.state.chapter,
                                chapter_time_ticks=int(self.metrics.state.plan_session_ticks or 0))
            self._chapter_complete()
            self._persist_completion_pending()
            if self.done:
                return
        self._detour = {'sid': sid, 'side': side, 'completed': completed}
        self._detour_agent = False
        # 第一次看到就已通关（续跑停在计划外地图的片尾或结算）：照常走完收尾，不补记成绩
        self._detour_ending = completed and self.cfg.cutscene_mode != 'agent'
        self.metrics.update(detour={'chapter': sid, 'side': side, **({'completed': True} if completed else {})})
        print(f"[run] 进入计划外地图 {map_label(sid, side)}", flush=True)
        self._add_detour_note(f'已进入计划外地图{map_label(sid, side)}：这里的通关不完成计划、不计入分段成绩与总计时；'
                              '计划章节仍是本次任务。')

    def _detour_complete(self) -> None:
        """计划外地图通关：另记一条绕行成绩（metrics.detours），不动分段成绩、总计时、piece_done 与章节队列，
        不结束本次试玩。auto 剧情模式在这次动作里照常跳过片尾、确认结算（_finish_detour）。"""
        detour = self._detour
        if not detour['completed']:
            detour['completed'] = True
            record = self.metrics.add_detour(detour['sid'], detour['side'], self.metrics.state.chapter_time_ticks)
            self.metrics.update(detour={'chapter': detour['sid'], 'side': detour['side'], 'completed': True})
            label = map_label(detour['sid'], detour['side'])
            print(f"[run] 计划外地图 {label} 通关：章节计时 {fmt_ticks(record['ticks'])}（不计入分段成绩与总计时）", flush=True)
            self._add_detour_note(f'计划外地图{label}已通关（章节计时 {fmt_ticks(record["ticks"])}）：不计入本次计划、分段成绩与总计时，'
                                  '本次任务没有结束。' + ('结算与返回选章由你用 act 操作，主控不代按。'
                                                 if self.cfg.cutscene_mode == 'agent' else ''))
        if self.cfg.cutscene_mode != 'agent' and not self._detour_agent:
            self._detour_ending = True

    def _finish_detour(self) -> tuple[dict, str | None, list[str]]:
        """（调用方持有连接锁）auto 剧情模式下计划外地图刚通关：与最终章相同，跳过片尾剧情、确认结算，回到选章地图后
        交给智能体；不结束本次运行、不选章节。走不完时交给智能体（开放确认、返回、暂停等按键）。"""
        self._detour_ending = False
        problem = None
        try:
            self._run_ending(self.client.call("observe", image=False, entities=False, tiles=False)["obs"], final=False)
        except RuntimeError as exc:
            problem = str(exc)
        reply = self.client.call("observe", image=True, scope="room", fields=True)
        obs, image = enrich_obs(reply["obs"]), reply.get("image")
        self._note_map(obs)
        if self.goals.immediate:
            return obs, image, []
        if self._at_chapter_menu(obs):
            return obs, image, ['计划外地图的片尾与结算已按 auto 剧情模式由主控跳过、确认，已回到选章地图；'
                                '本次计划尚未完成，接下来选哪张地图由你用 act 操作，主控不代选。']
        self._detour_agent = True
        print(f"[run] 计划外地图的片尾主控没能走完（{problem}），交给智能体处理", flush=True)
        return obs, image, [f"计划外地图已通关；片尾或结算主控没能自动走完（{problem or '没有回到选章地图'}）。"
                            "请观察画面后用 act 自行处理：C 确认、B 返回、P 打开暂停菜单（菜单里可选跳过剧情）、方向键选择，"
                            "纯帧数等待动画；回到选章地图后自行选择下一张地图，主控不再代按。"]

    def finish_at_results(self) -> None:
        """最终章走完片尾，展示并确认结算，停在选章地图，不进入下一章。"""
        if not self.done or self.done_reason != "章节完成" or self.chapter_queue:
            return
        if self.cfg.cutscene_mode == 'agent':
            # Cleanup observes the outcome; it never supplies the agent's missing
            # heart/results/menu inputs, including after an interrupted ending.
            if self.end_scene != 'Overworld':
                raise RuntimeError('智能体尚未自行返回选章地图，主控不会代按结算')
            return
        with self.lock:
            if self.goals.immediate:
                return
            self._run_ending(self.client.call("observe", image=False, entities=False, tiles=False)["obs"])

    def _run_ending(self, obs: dict, *, final: bool = True) -> None:
        """（调用方持有连接锁）最终章通关后走完片尾：剧情能跳过就用暂停菜单跳过，对白和等确认的界面按确认，
        展示并确认结算，停在选章地图。走不完抛 RuntimeError。
        final=False：计划外地图通关后的同一套 auto 收尾（_finish_detour）：不记 end_scene，回到选章地图就交回，
        不在地图上停留。"""
        started = time.monotonic()
        results_shown = False
        skip_tries = menu_closes = 0
        last_sig, unchanged = None, 0        # 片尾画面连续没有变化的次数（每次 30 帧）
        what = "最终章" if final else "计划外地图"
        # 刚通关的地图：片尾之后游戏自己进了另一张地图、角色已可操作（例如首次通关 7A 后进入终章，要走到小屋），
        # 这段是游玩不是片尾，立即交给智能体，不再空推等"画面没有变化"超时
        finished_sid = ((obs.get('level') or {}).get('sid')
                        or (CHAPTER_SIDS.get(self.piece.chapter) if self.piece is not None else None))

        def log_step(inputs, result):
            self._log_frames(result, expand_inputs(inputs, 10000, LETTERS), f"({what}结算)", auto=True)
            self.metrics.add(frames_auto=result["consumed"])

        for _ in range(600):
            scene = obs.get('scene')
            if final:
                self.end_scene = scene
            if self.goals.immediate:
                return
            if scene == 'Overworld':
                menu=obs.get('menu') or {}
                if menu.get('current') == 'OuiChapterSelect' and not menu.get('next'):
                    if not final:
                        self.metrics.add(auto_game_s=time.monotonic() - started)
                        print('[run] 计划外地图的结算已确认，停在选章地图交给智能体', flush=True)
                        return
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
            elif scene != 'Overworld':
                if scene not in {'Level','LevelExit','OverworldLoader'}:
                    raise RuntimeError(f"{what}结算出现意外场景：{scene}")
                level=obs.get('level') or {}
                if (level.get('paused') or obs.get('pauseMenu')) and menu_closes < 3:
                    menu_closes += 1                 # 跳过剧情留下的暂停菜单：关掉后接着推进
                    inputs = ['1,B', '20']
                else:
                    if cutscene_skippable(obs) and skip_tries < 3:
                        skip_tries += 1
                        self._before_advance()
                        skipped, obs, _, reason = skip_by_pause_menu(self.client, on_step=log_step)
                        if skipped:
                            continue
                        if reason == "no_skip":
                            skip_tries = 3           # 这段片尾不能跳过：按正常速度播放
                        elif reason != "no_pause":
                            continue
                        level = obs.get('level') or {}
                    if (scene == 'Level' and finished_sid and level.get('sid') not in (None, finished_sid)
                            and self._settled(obs) and not level.get('dialogOpen')):
                        self.metrics.add(auto_game_s=time.monotonic() - started)
                        raise EndingNeedsAgent(f"片尾之后进入了需要操作的关卡（{chapter_name(level.get('sid'))}）")
                    inputs=['1,C','29'] if (level.get('dialogWaiting') or awaiting_confirm(obs, ending=True)
                                            or story_input(obs) == 'postcard') else ['30']
                    sig = (obs.get('scene'), level.get('room'), level.get('inCutscene'), level.get('dialogOpen'),
                           level.get('dialogWaiting'), level.get('frozen'), level.get('paused'), obs.get('storyInput'))
                    unchanged = unchanged + 1 if sig == last_sig else 0
                    last_sig = sig
                    if unchanged >= ENDING_STUCK_STEPS:
                        raise RuntimeError(f"片尾画面约 {ENDING_STUCK_STEPS // 2} 秒没有变化，已停止自动推进")
            self._before_advance()
            reply = self.client.call("step", inputs=inputs, stop_on=[], speed=self.cfg.auto_speed,
                                     image=False, entities=False, tiles=False)
            log_step(inputs, reply["result"])
            obs = reply["obs"]
        raise RuntimeError("最终章节片尾尚未返回选章地图，已停止自动推进" if final
                           else "计划外地图的片尾尚未返回选章地图，已停止自动推进")

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
        # 在计划外地图里也照常发：桥接只按房间名停下，同名房间在计划外地图里只是提前停一步（_note_room 按完整观测
        # 判断不算终点，进度事件在离开计划会话后不再判断，见 _on_progress）；这样一次动作里从计划外地图回到计划章节、
        # 经过终点房间时不会错过
        return {"stop_rooms": [room]} if room and not self.chapter_finished and not self.done else {}

    def _note_obs_room(self, obs: dict) -> None:
        level = obs.get("level") or {}
        self._note_room(level.get("sid"), level.get("room"), level.get("chapterTimeTicks"))

    def _note_room(self, sid, room, ticks=None) -> bool:
        """换房间时：更新当前所在的段；进入这一截终点检查点的房间时完成这一截（返回 True）。"""
        piece = self.piece
        if (piece is None or room is None or self.done or self.chapter_finished or sid != CHAPTER_SIDS.get(piece.chapter)
                or self._in_detour()):     # 计划外地图（包括计划章节的 B/C 面）的房间不推进计划的段
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
        ticks = self._plan_ticks()
        self.metrics.add_split(st.chapter, ticks, **self._split_range())
        print(f"[run] {piece_label(self.piece, self._table)}完成：章节计时 {fmt_ticks(ticks)}", flush=True)
        if self.navigation.complete_current():
            self._save_chapter_plan()
        else:
            self._mark_done("分段完成")

    def finish_at_checkpoint(self) -> None:
        """最后一截在检查点结束（不是章节结尾）：用暂停菜单返回地图，不让游戏在关卡里无人看管地继续运行。"""
        if not self.done or self.done_reason != "分段完成" or self.chapter_queue:
            return
        if self.cfg.cutscene_mode == 'agent':
            # Reaching a user-selected checkpoint is itself the stopping target.
            # Do not add an unrequested pause/menu navigation during cleanup.
            self.end_scene = (self.last_obs or {}).get('scene')
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

    def _enter_next_chapter(self, previous_obs=None) -> tuple[dict, str | None, list[str]]:
        """（调用方持有连接锁）换到计划里的下一截，停在可操作的第一帧：上一截在章节结尾结束时走完结算画面、
        回到地图再进入下一章（下一截从章节开头开始时走菜单，否则在地图上直接进入它的起点检查点）；
        上一截在检查点结束时，下一截在别的章节的开头就用暂停菜单返回地图再走菜单，否则直接传送到它的起点检查点。"""
        if self.cfg.cutscene_mode == 'agent':
            return self._observe_next_chapter(previous_obs)
        from enter_save import enter_save  # noqa: WPS433（tools 目录已在 sys.path 里）

        split = (self.metrics.state.splits or [])[-1]
        previous = self.piece
        piece = Piece.of(self.chapter_queue[0])
        self._before_advance()
        self.metrics.update(status="auto", thought="", formal_thought="", formal_thought_wall=0.0)
        t0 = time.monotonic()
        enter_save(self.client, self.cfg.save, chapter=piece.chapter, checkpoint=piece.start, menu_speed=1,
                   save_slot=self.cfg.save_slot, cutscene_mode=self.cfg.cutscene_mode,
                   should_stop=self.goals.stop_motion.is_set)  # 正常速度，录像里不快进
        self.metrics.add(auto_game_s=time.monotonic() - t0)
        reply = self.client.call("observe", image=True, scope="room", fields=True)
        obs, image = enrich_obs(reply["obs"]), reply.get("image")
        if not self._confirm_next_entry(obs):
            if self.goals.stop_motion.is_set():
                return obs, image, ["用户停止了自动换图，游戏保持在当前帧。"]
            raise ActError('自动换图尚未进入目标章节，保留当前计划以便续跑')
        table = self._table
        done = piece_label(previous, table) if previous is not None else chapter_name(split["chapter"])
        how = ("系统已自动走完结算画面并进入" if previous is None or previous.end is None else "系统已把你送到")
        note = (f"【{done}完成】章节计时 {fmt_ticks(split['ticks'])}，死亡 {split['deaths']} 次。"
                f"{how}{self._piece_place(piece)}（新的计时从 0 开始），下面是这一截的第一个房间；继续游玩。"
                f"{self._piece_goal(piece)}")
        remaining = "、".join(piece_label(p, table) for p in self.chapter_queue)
        note += f"之后还有：{remaining}。" if remaining else "这是本次的最后一截。"
        return obs, image, [note]

    def _observe_next_chapter(self, previous_obs=None):
        """Agent owns menu navigation; only commit the configured next piece once observed."""
        reply = self.client.call('observe', image=True, scope='room', fields=True)
        obs, image = enrich_obs(reply['obs']), reply.get('image')
        if previous_obs and previous_obs.get('storyPlayback'):
            obs['storyPlayback'] = previous_obs['storyPlayback']
        piece = self.navigation.target
        if not self._confirm_next_entry(obs, require_departure=True):
            if getattr(self, 'travel_tool', False):
                return obs, image, [f'上一段已完成。接下来请用 travel 进入{self._piece_place(piece)}，由主控代为换图。']
            return obs, image, [f'上一段已完成。接下来请自行通过菜单进入{self._piece_place(piece)}；主控不会代按、跳过结算或传送。']
        return obs, image, [f'已确认你进入{self._piece_place(piece)}。{self._piece_goal(piece)}']

    def _confirm_next_entry(self, obs, *, require_departure=False):
        """All navigation paths publish metrics/persistence after the same commit."""
        if self._classifying() and obs.get('scene') == 'Level' and ((obs.get('level') or {}).get('side') or 'A') != 'A':
            return False     # 开了计划外地图时，计划章节的 B/C 面是计划外地图，不算进入下一截
        piece = self.navigation.confirm_entry(obs, self._table, require_departure=require_departure)
        if piece is None:
            return False
        level = obs['level']
        self.start_sid = level['sid']
        if self._classifying() or self.metrics.state.plan_banked_ticks:
            # 新的一截从这段会话开始累计计划章节的计时
            self.metrics.update(plan_banked_ticks=0, plan_session_ticks=int(level.get('chapterTimeTicks') or 0))
            self._plan_progress_ok = True
        self.metrics.update(chapter=level['sid'], room=level.get('room', ''), piece_done=False,
                            chapter_time_ticks=level.get('chapterTimeTicks', 0),
                            formal_thought="", formal_thought_wall=0.0)   # 新的一段：上一章最后的正式思考不留在 HUD 上
        self._save_chapter_plan()
        return True

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
                f"进入房间 {room} 即完成。")

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
        detour = self._detour
        return {"play_scope": self.cfg.play_scope, "visited_maps": getattr(self.cfg, 'visited_maps', None),
                "completion_policy": getattr(self.cfg, 'completion_policy', 'stop'),
                "allow_detour": detour_enabled(self.cfg),
                "detour": None if detour is None else {'chapter': detour['sid'], 'side': detour['side'],
                                                       'completed': detour['completed']},
                "done": [i for i in (ids.get(sp.get("chapter")) for sp in (self.metrics.state.splits or [])) if i is not None],
                "current": self.current_chapter_id(), "queue": chapters_of(self.chapter_queue),
                "segments": {"done": done, "current": None if current is None or self.chapter_finished else list(current),
                             "queue": queue},
                "switching": bool(self.chapter_finished), "editable": self.cfg.play_scope != 'visited' and not self.done and not self.chapter_finished}

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
        if self.cfg.play_scope == 'visited':
            raise ValueError('自由探索不使用顺序通关计划；请在许可地图内自行导航，停止条件由目标单独设置')
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
            new_current = Piece(piece.chapter, piece.start, None if end >= count else end)
            new_queue = pieces_of(sorted(rest), table)
            if getattr(self.cfg, 'completion_policy', 'stop') == 'continue' and any(not p.whole() for p in [new_current, *new_queue]):
                raise ValueError('通关后继续目前支持整章范围，不能改为部分章节计划；原计划未改变')
            self.navigation.replace_future(new_current, new_queue)
            self._save_chapter_plan()
        table_label = "、".join(piece_label(p, table) for p in self.chapter_queue)
        goal = self._piece_goal(self.piece)
        print(f"[run] 用户调整了要通关的范围：当前这一截 {piece_label(self.piece, table)}；之后 {table_label or '无'}", flush=True)
        if self.chapter_queue:
            return (f"用户调整了本次要通关的范围。{goal}之后依次是：{table_label}，不要自行进入其他章节"
                    + ("（计划外地图仍可按“计划外地图”一节绕行）。" if detour_enabled(self.cfg) else "。"))
        if getattr(self.cfg, 'completion_policy', 'stop') == 'continue':
            return f'用户调整了本次要通关的范围。{goal}计划完成后仍可在当前许可地图继续探索，直到独立停止目标触发或用户停止。'
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
        state['navigation_remaining'] = [[p.chapter, p.start, p.end] for p in
                                         current + [Piece.of(p) for p in self.chapter_queue]]
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

    def _log_frames(self, result: dict, frames: list[str], thought: str, auto: bool, input_format: str = 'celeste_ai') -> None:
        traj = result.get("trajectory") or []
        for i, row in enumerate(traj):
            rec = {"f": row[0], "keys": frames[i] if i < len(frames) else "", "auto": auto,
                   "thought": thought, "decision": self.decision_index, "input_format":input_format}
            if len(row) >= 8 and row[1] is not None:
                rec.update(x=row[1], y=row[2], vx=row[3], vy=row[4], st=row[5], dash=row[6], gnd=row[7])
            self._frames_log.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._frames_log.flush()

    def _update_metrics_from_obs(self, obs: dict) -> None:
        self._goal_progress = {}
        level = obs.get("level") or {}
        player = obs.get("player") or {}
        # 通关后继续（自由探索，或整章／顺序范围的计划已全部完成）：重新进入未通关的关卡就是新的一次游玩，
        # 当前计时重新计入总计时；再次通关时只按这次的成绩记一条。换到计划里下一截的途中（chapter_finished）
        # 由 _confirm_next_entry 负责清除，不在这里清。
        continuing = ((self.cfg.play_scope == 'visited'
                       or getattr(self.cfg, 'completion_policy', 'stop') == 'continue') and not self.chapter_finished
                      and not self._in_detour())   # 进入计划外地图不是重新游玩计划章节
        if continuing and self.metrics.state.piece_done and obs.get('scene') == 'Level' and not level.get('completed'):
            self.metrics.update(piece_done=False)
        if self.sim_energy:
            if level.get("sid") and level.get("room") is not None and not player.get("dead"):
                self.sim_energy.reward(room=f"{level['sid']}:{level.get('mode', level.get('side', 0))}:{level['room']}")
            self.metrics.update(sim_energy=self.sim_energy.snapshot())
        if (level.get("sid"), level.get("room")) != (self.metrics.state.chapter, self.metrics.state.room):
            self.metrics.update(sim_best_frames=0)   # HUD 的"推演最快"只看当前房间
            self.room_entry_frame = obs.get("frame")   # 进入这个房间的那一帧（推演最速从这里算起）
        self.metrics.update(
            chapter=level.get("sid", self.metrics.state.chapter),
            chapter_side=level.get("side",self.metrics.state.chapter_side),
            room=level.get("room", self.metrics.state.room),
            chapter_time_ticks=level.get("chapterTimeTicks", self.metrics.state.chapter_time_ticks),
            dashes=player.get("dashes", 0),
            max_dashes=player.get("maxDashes", 0),
        )
