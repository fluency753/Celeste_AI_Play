"""运行统计：思考时间、工具时间、游戏推进时间、上下文大小、压缩次数等。

统计口径与设计文档第 4.5 节一致：
- 一轮（主控发出消息到 Claude Code 报告这一轮结束）里的每一刻，要么算思考，要么算工具：
  有工具在运行（输出流里出现 tool_use、还没收到对应的 tool_result）时算工具时间，没有时算思考时间（含模型推理、
  输出与上下文压缩）。工具包括容器里的 Bash、Read、Write 等和游戏工具；同时运行几个工具只算一份时间。
- 工具时间里扣掉游戏真正推进的时间（act 的动作块、转场与剧情的自动推进），那部分记为游戏推进时间；
  自动重试时重算程序的用时算工具时间。两轮之间、读知识库期间、等待额度时都不计；读完知识库之后、开始游玩之前
  （准备阶段的其余时间：写笔记、写程序、在模拟器里规划第一个房间）照常计入思考与工具。
- 游戏推进时间：桥接逐帧推进所用的墙钟时间（动作块与自动推进段分开记）。
- 知识库读取（study_s）：从开局到知识库读完的墙钟时间（没读完就开始游玩时记到开始游玩为止）。
- 准备阶段：开始游玩之前（读知识库、写笔记与程序）不计入现实用时；
  现实用时从第一次真正执行游戏工具时开始（没有准备阶段时从启动时开始）。
- 等待额度：Max 用量到达上限后等待额度重置的时间单独记（waited_s），不计入现实用时与准备时间。
- 推理强度：effort 是当前生效的强度；智能体用 set_effort 申请的强度先记在 effort_pending，本轮结束后才生效，
  每次切换记进 effort_log。
- 续跑（--resume）时从上一段运行的统计接着累计。
每次更新都会原子地写出 metrics.json，供 HUD 读取。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class MetricsState:
    telemetry_source: str = "agent"
    chat_thread_id: str | None = None
    agent: str = ""
    model: str = ""
    effort: str = ""                  # 当前生效的推理强度标识（由模型能力列表决定）
    effort_pending: str | None = None  # 智能体用 set_effort 申请、本轮结束后才生效的强度
    effort_changes: int = 0           # 推理强度实际切换的次数
    effort_log: list | None = None    # 每次切换：[{"t": 现实用时, "from", "to", "chapter", "room", "reason"}, ...]
    status: str = "starting"         # starting / studying / thinking / tool / executing / auto / retrying / compacting / quota_wait / net_wait / done / stopped
    started_wall: float = 0.0
    elapsed_s: float = 0.0
    budget_s: float = 0.0
    thinking_s: float = 0.0
    tool_s: float = 0.0
    play_game_s: float = 0.0
    auto_game_s: float = 0.0
    decisions: int = 0
    decision_wait_s: float = 0.0      # 两次 act 之间无工具等待的累计；含请求、推理、输出与压缩
    turn_fast_requested: bool | None = None
    turn_service_tier_requested: str | None = None  # 请求值，不代表服务端确认的实际档位
    frames_played: int = 0
    frames_auto: int = 0
    deaths: int = 0
    context_tokens: int = 0
    context_window: int | None = None
    tool_calls: int | None = None
    compactions: int = 0
    compact_s: float = 0.0            # 压缩本身花的时间（已包含在思考时间里）
    stiff_frames: int = 0             # 思考僵直推进的帧数（探索模式开了 --stiffness 时）
    stiff_s: float = 0.0              # 思考僵直的游戏内秒数
    retries: int = 0                  # 自动重试次数（重算程序运行次数，不唤醒大模型）
    pause_denials: int = 0            # 规划轮次用完后被拒绝的工具调用次数（见 pausegate.py）
    plan_rounds_left: int | None = None  # 本章还剩的规划轮次（--limit-planning 时）；None = 不限（默认）
    plan_rounds_used: int = 0         # 用掉的规划轮次（准备阶段不算）
    replaying: bool = False           # 规划回放正在画面上播放（与智能体的思考同时进行，不改 status）
    replay_round: int = 0             # 正在回放这次规划的第几轮（0 = 执行前画最终路线）
    replay_rounds: int = 0            # 已收进的轮数
    replay_label: str = ""            # 这一轮的说明（智能体写在 Bash 的 description 里）
    replay_runs: int = 0              # 这一轮在模拟器里推演了多少条路线
    replays: int = 0                  # 播过的规划回放段数
    replay_s: float = 0.0             # 规划回放用时（游戏不推进；逐轮回放与智能体的思考、工具同时进行，照常计入它们）
    replay_act_s: float = 0.0         # 其中执行前在 act 里把镜头送回 Madeline 的用时（和游戏推进时间一样不计入工具时间）
    tool_call: str = ""               # 正在调用的工具（HUD 的"调用工具"一栏，例如"Bash · 搜索第二个房间"；没有时为空）
    tool_status: str = ""             # 实时展示：正在运行的程序用 show.status() 报告的进度（工具调用返回后清空）
    live: bool = False                # 智能体用 narrate 实时更新思考（所有模式都开；HUD 据此在规划回放时仍把思考语句放在主位）
    live_mode: bool = False           # 直播模式
    play_mode: str = ""               # 模式说明，例如"速通模式（开模拟器）"、"探索模式 + 直播"
    hud_mod: bool = False             # HUD 画在游戏画面里：OBS 浏览器源里的网页 HUD 不显示（录像里只有一份）
    narrates: int = 0                 # 实时展示：智能体调用 narrate 的次数
    narrate_reminders: int = 0        # 实时展示：思考状态累计半分钟没有 narrate、在下一次调用工具时提醒的次数
    sim_calls: int = 0                # 调用了预置模拟器的消息数（每条算一次，与规划轮次的记账相同）
    sim_energy: dict | None = None    # 后台限额模拟体力（HUD 上叫"推演额度"）；None 表示本局未开启
    sim_stats: bool = True            # HUD 是否显示推演相关的几栏（模拟器调用、推演路径、推演最速）：探索模式关掉限额预测时智能体不能推演，不显示
    sim_rollouts: int = 0             # 预置模拟器推演过的路线总条数（全程累计，写进总结）
    sim_round_rollouts: int = 0       # 本轮回放的临时路线数，画完清零；累计显示使用 sim_rollouts
    sim_best_frames: int = 0          # 当前房间里推演出的最快出房帧数，从进入房间的那一帧算起（0 = 还没有；换房间清零）
    replan_s: float = 0.0             # 重算程序用时（已包含在工具时间里）
    study_s: float = 0.0              # 准备阶段（读知识库等）用时，不计入 elapsed_s
    kb_progress: float | None = None  # 知识库已读比例（0~1）；None = 没有准备阶段
    chapter: str = ""
    chapter_side: str = "A"
    room: str = ""
    chapter_time_ticks: int = 0
    completed_ticks: int = 0          # 本次运行里已经通关的章节的计时之和（依次通关多章时，总计时 = 它 + 当前章节计时）
    splits: list | None = None        # 已完成各截的成绩：[{"chapter": sid, "ticks": n, "deaths": n}, ...]；按段通关时另有 from/to（起止检查点，to 为 null = 通关）
    chapter_base: dict | None = None  # 进入当前章节那一刻的各项累计值（HUD 显示本章的值 = 累计值 − 它）；None = 还没换过章
    piece_done: bool | None = None    # 当前这一截已完成、成绩已计入 completed_ticks（换到下一截之前，HUD 不再把当前计时加进总计时）
    dashes: int = 0
    max_dashes: int = 0
    thought: str = ""
    thought_updated_wall: float = 0.0
    formal_only: bool = False         # "仅正式思考"开关（--formal-thought）：游戏内 HUD 只显示当前这一句正式思考
    formal_thought: str = ""          # 正式思考：最近一次 act 的 thought（换章时清空）
    formal_thought_wall: float = 0.0
    temp_thought: str = ""            # 临时思考：开了"仅正式思考"时 narrate 写的内容（只进控制台，不上游戏内 HUD）
    temp_thought_wall: float = 0.0
    last_stop: str = ""
    usage_5h: float | None = None     # Max 套餐 5 小时窗口已用比例（0~1），来自 Claude Code 的 rate_limit_event
    usage_7d: float | None = None     # 7 天窗口已用比例
    usage_resets_at: int | None = None
    waited_s: float = 0.0             # 等待 Max 额度重置的总时间（不计入现实用时）
    quota_waits: int = 0              # 等待额度重置的次数
    quota_wait_until: float | None = None  # 正在等待时：预计恢复的时刻（墙钟，秒）
    net_wait_s: float = 0.0           # 模型连不上（断网等）时暂停等待的总时间（不计入现实用时与思考僵直）
    net_waits: int = 0                # 网络等待的次数
    resumed_from: str | None = None   # 续跑时：上一段运行的日志目录
    output_tokens: int = 0
    thinking_tokens: int = 0
    # HUD 用来在两次写入之间自己走表的时间点（墙钟，秒）
    phase_since_wall: float = 0.0     # 当前 status 开始的时刻
    thinking_since_wall: float = 0.0  # 当前这一段（思考，或运行容器工具）开始的时刻
    chapter_time_wall: float = 0.0    # chapter_time_ticks 最近一次更新的时刻
    written_wall: float = 0.0         # 本文件最近一次写出的时刻


class Metrics:
    def __init__(self, path: Path, agent: str, model: str, effort: str, budget_s: float, study: bool = False):
        self._path = path
        from .decision_timing import DecisionTiming
        self.timing = DecisionTiming(path.parent / "decision_timing.jsonl")
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._offset = 0.0              # 续跑时上一段运行已用的现实用时
        self._paused = 0.0              # 从 _t0 起暂停（等待额度）的总秒数
        self._pause_start: float | None = None
        self._effort_reason = ""
        self._in_turn = False           # 智能体正在进行一轮（主控发出消息之后、这一轮结束之前）
        self._tools: set[str] = set()   # 正在运行的工具调用（tool_use 的编号）
        self._tool_labels: dict[str, str] = {}   # 正在运行的工具调用 → HUD 上的说明
        self._seg_mark = time.monotonic()  # 上一次把时间结算进思考或工具的时刻
        self._seg_game = 0.0            # 那一刻的游戏推进时间累计
        self._unnarrated = 0.0          # 实时展示：上次 narrate（或上次提醒）之后累计的思考秒数
        self.playing = not study
        self.kb_done = not study        # 知识库已读完（之后的准备阶段照常计思考与工具时间）
        now = time.time()
        self.state = MetricsState(agent=agent, model=model, effort=effort,
                                  started_wall=now if self.playing else 0.0, budget_s=budget_s,
                                  status="studying" if study else "starting", kb_progress=0.0 if study else None,
                                  phase_since_wall=now, thinking_since_wall=now)
        self.write()

    def _running(self, now: float) -> float:
        """从 _t0 起、扣除暂停之后的秒数。"""
        paused = self._paused + (now - self._pause_start if self._pause_start is not None else 0.0)
        return max(0.0, now - self._t0 - paused)

    def elapsed(self) -> float:
        """现实用时（准备阶段与等待额度的时间不算）。"""
        return self._offset + self._running(time.monotonic()) if self.playing else 0.0

    # ---- 思考与工具时间 ----

    def _flush_locked(self) -> None:
        """把上一次结算到现在的这一段记进思考或工具时间（规则见模块说明）。"""
        now = time.monotonic()
        game = self.state.play_game_s + self.state.auto_game_s + self.state.replay_act_s
        dt = now - self._seg_mark
        if (self.playing or self.kb_done) and self._in_turn and self._pause_start is None and dt > 0:
            if self._tools:
                self.state.tool_s += max(0.0, dt - max(0.0, game - self._seg_game))
            else:
                self.state.thinking_s += dt
                self._unnarrated += dt
        self._seg_mark, self._seg_game = now, game
        self.state.thinking_since_wall = time.time()

    def narrated(self) -> None:
        """实时展示：智能体调用了 narrate，半分钟计时器从头算。"""
        with self._lock:
            self._flush_locked()
            self._unnarrated = 0.0

    def take_live_reminder(self, interval: float) -> float | None:
        """实时展示的半分钟计时器：上次 narrate（或上次提醒）之后在思考状态累计满 interval 秒时，
        返回累计的秒数并从头计时（这一次要提醒）；否则返回 None。工具运行、游戏推进的时间不算思考。"""
        with self._lock:
            self._flush_locked()
            if self._unnarrated < interval:
                return None
            waited, self._unnarrated = self._unnarrated, 0.0
            self.state.narrate_reminders += 1
            self._write_locked()
            return waited

    def turn_begin(self, *, fast: bool | None = None, service_tier: str | None = None,
                   effort: str | None = None, runtime: str | None = None) -> None:
        """主控给智能体发出一条消息：一轮开始。"""
        with self._lock:
            self._flush_locked()
            self.state.turn_fast_requested = fast
            self.state.turn_service_tier_requested = service_tier
            self.timing.begin(self.timing.clock(), self.state.thinking_s,
                              runtime=runtime, agent=self.state.agent, model=self.state.model,
                              effort=effort or self.state.effort,
                              fast_requested=fast, service_tier_requested=service_tier)
            self._in_turn = True
            self._write_locked()

    def freeze_counters(self) -> tuple[float, float, float]:
        """思考僵直用：结算到此刻的（思考秒数，工具秒数，压缩秒数）累计。用户暂停与等待额度的时间本来就不计入。"""
        with self._lock:
            self._flush_locked()
            return self.state.thinking_s, self.state.tool_s, self.state.compact_s

    def turn_acknowledged(self, turn_id: str) -> None:
        with self._lock:
            self.timing.acknowledge(self.timing.clock(), turn_id)

    def decision_begin(self) -> None:
        with self._lock:
            self._flush_locked()
            self.state.decision_wait_s = self.timing.decision_begin(self.timing.clock(), self.state.thinking_s)
            self._write_locked()

    def decision_end(self, result: dict | None) -> None:
        with self._lock:
            self._flush_locked()
            self.timing.decision_end(self.timing.clock(), self.state.thinking_s, result)

    def turn_end(self) -> None:
        """Claude Code 报告这一轮结束（或进程退出）。"""
        with self._lock:
            self._flush_locked()
            self.timing.end(self.timing.clock())
            self._in_turn = False
            self._tools.clear()
            self._tool_labels.clear()
            self.state.tool_call = ""
            self.state.tool_status = ""
            self._write_locked()

    def _tool_summary(self) -> str:
        labels = [self._tool_labels[t] for t in self._tool_labels if t in self._tools]
        if not labels:
            return ""
        if len(labels) == 1:
            return labels[0]
        name, _, detail = labels[0].partition(" · ")   # 并行的几个调用：数量写在前面（后面太长时会被省略）
        names = {lab.partition(" · ")[0] for lab in labels}
        head = f"{name} ×{len(labels)}" if len(names) == 1 else f"{name} 等 {len(labels)} 个"
        return f"{head} · {detail}" if detail else head

    def tool_begin(self, tool_id: str, game_tool: bool = False, label: str = "") -> None:
        """输出流里出现一次工具调用。容器工具（Bash 等）把状态切成"运行工具"；游戏工具的状态由游戏会话设置。
        label 是给 HUD 看的"调用工具"一栏（工具名与说明）。"""
        with self._lock:
            if tool_id in self._tools:
                return
            self._flush_locked()
            self.timing.tool_begin(tool_id, label, game_tool, self.timing.clock(),
                                   self.state.thinking_s, self.playing)
            self._tools.add(tool_id)
            if label:
                self._tool_labels[tool_id] = label
            self.state.tool_call = self._tool_summary()
            if not game_tool and (self.playing or self.kb_done) and self.state.status in ("thinking", "starting"):
                self.state.status = "tool"
                self.state.phase_since_wall = time.time()
            self._write_locked()

    def tool_end(self, tool_id: str) -> None:
        """输出流里出现这次工具调用的结果。"""
        with self._lock:
            if tool_id not in self._tools:
                return
            self._flush_locked()
            self.timing.tool_end(tool_id, self.timing.clock(), self.state.thinking_s)
            self._tools.discard(tool_id)
            self._tool_labels.pop(tool_id, None)
            self.state.tool_call = self._tool_summary()
            if not self._tools:
                self.state.tool_status = ""
            if not self._tools and (self.playing or self.kb_done) and self.state.status in ("tool", "executing", "auto", "retrying"):
                self.state.status = "thinking"
                self.state.phase_since_wall = time.time()
            self._write_locked()

    def kb_finished(self) -> None:
        """知识库读完：知识库读取的用时到此为止；之后的准备阶段照常计思考与工具时间，状态栏不再显示"读知识库"。"""
        with self._lock:
            if self.kb_done:
                return
            self._flush_locked()   # 读知识库期间不计入思考与工具
            self.state.study_s = self._running(time.monotonic())
            self.kb_done = True
            now = time.time()
            self.state.thinking_since_wall = now
            if self.state.status == "studying":
                self.state.status = "tool" if self._tools else "thinking"
                self.state.phase_since_wall = now
            self._write_locked()

    def start_play(self) -> None:
        """准备阶段结束，开始计现实用时。"""
        with self._lock:
            if self.playing:
                return
            self._flush_locked()  # 读完知识库之后的准备时间计入思考与工具，读知识库期间不计
            now = time.monotonic()
            if not self.kb_done:   # 没读完知识库就开始游玩（到了准备阶段上限）：知识库读取记到这里为止
                self.state.study_s = self._running(now)
            self.kb_done = True
            self._t0 = now
            self._paused = 0.0
            self._pause_start = now if self._pause_start is not None else None
            self.playing = True
            self.state.started_wall = time.time()
            self.state.thinking_since_wall = self.state.started_wall
            self.state.phase_since_wall = self.state.started_wall
            self.state.status = "thinking"
            self._write_locked()

    def pause(self, until_wall: float | None = None, *, status: str = "quota_wait", since: float | None = None) -> None:
        """等待额度重置（或网络恢复，status="net_wait"）：现实用时（或准备时间）停止走表。
        since（monotonic）：从更早的时刻算起暂停——例如模型第一次连不上的时刻；那之后已经记成思考的时间退回来。"""
        with self._lock:
            self._flush_locked()
            self.timing.pause()
            if self._pause_start is None:
                now_m = time.monotonic()
                start = now_m
                if since is not None and since < now_m:
                    back = now_m - since
                    if (self.playing or self.kb_done) and self._in_turn and not self._tools:
                        take = min(back, self.state.thinking_s)
                        self.state.thinking_s -= take
                        self._unnarrated = max(0.0, self._unnarrated - take)
                    start = max(since, self._t0)
                self._pause_start = start
            self._in_turn = False
            self._tools.clear()
            now = time.time()
            if self.state.status != status:
                self.state.phase_since_wall = now
            self.state.status = status
            self.state.quota_wait_until = until_wall
            self._write_locked()

    def resume(self, *, quota: bool = True, net: bool = False) -> float:
        """额度（net=True：网络）恢复：返回这次等待的秒数。"""
        with self._lock:
            if self._pause_start is None:
                return 0.0
            self.timing.resume()
            waited = time.monotonic() - self._pause_start
            self._paused += waited
            self._pause_start = None
            if net:
                self.state.net_wait_s += waited
                self.state.net_waits += 1
            elif quota:
                self.state.waited_s += waited
                self.state.quota_waits += 1
            self.state.quota_wait_until = None
            self.state.status = "thinking" if (self.playing or self.kb_done) else "studying"
            self.state.phase_since_wall = time.time()
            self._write_locked()
            return waited

    RESTORED = ("thinking_s", "tool_s", "play_game_s", "auto_game_s", "decisions", "frames_played", "frames_auto",
                "stiff_frames", "stiff_s",
                "deaths", "compactions", "compact_s", "retries", "replan_s", "study_s", "kb_progress",
                "completed_ticks", "splits", "output_tokens", "thinking_tokens", "waited_s", "quota_waits",
                "net_wait_s", "net_waits",
                "chapter", "room", "chapter_time_ticks", "dashes", "max_dashes", "thought", "formal_thought",
                "formal_thought_wall", "context_tokens",
                "effort_pending", "effort_changes", "effort_log", "pause_denials", "plan_rounds_used", "replays", "replay_s", "replay_act_s",
                "sim_calls", "sim_rollouts", "tool_calls", "context_window", "chapter_base")
    # 进入新的一章时 HUD 清零的累计值（知识库、上下文与压缩次数不清零；章节计时本来就从 0 开始）
    CHAPTER_COUNTERS = ("thinking_s", "tool_s", "deaths", "decisions", "tool_calls", "sim_calls", "sim_rollouts",
                        "retries", "frames_played", "frames_auto", "play_game_s", "auto_game_s", "narrates",
                        "output_tokens", "thinking_tokens", "replays", "replay_s", "elapsed_s")

    def restore(self, previous: dict, resumed_from: str) -> None:
        """续跑：接着上一段运行的统计累计（现实用时从上一段的值接着走）。"""
        with self._lock:
            for key in self.RESTORED:
                if key in previous and previous[key] is not None:
                    setattr(self.state, key, previous[key])
            self._offset = float(previous.get("elapsed_s") or 0.0)
            self._t0 = time.monotonic()
            self._paused = 0.0
            self.playing = True
            now = time.time()
            self.state.started_wall = float(previous.get("started_wall") or now)
            self.state.resumed_from = resumed_from
            self.state.status = "starting"
            self.state.phase_since_wall = self.state.thinking_since_wall = now
            self._in_turn = False
            self._tools.clear()
            self._seg_mark, self._seg_game = time.monotonic(), self.state.play_game_s + self.state.auto_game_s + self.state.replay_act_s
            self._write_locked()

    def update(self, **changes) -> None:
        with self._lock:
            now = time.time()
            if "status" in changes and changes["status"] != self.state.status:
                self.state.phase_since_wall = now
            if "chapter_time_ticks" in changes and changes["chapter_time_ticks"] != self.state.chapter_time_ticks:
                self.state.chapter_time_wall = now
            new_chapter = changes.get("chapter")
            if new_chapter and self.state.chapter and new_chapter != self.state.chapter:
                # 进入新的一章：记下这一刻的累计值，HUD 从这里起显示本章的值（统计与总结仍是整次运行的累计）
                self.state.elapsed_s = self.elapsed()
                self.state.chapter_base = {"chapter": new_chapter,
                                           **{k: getattr(self.state, k) for k in self.CHAPTER_COUNTERS}}
            for key, value in changes.items():
                setattr(self.state, key, value)
            self._write_locked()

    def mark_thinking(self) -> None:
        """一段思考开始（工具结果刚送回，或刚发出一条消息）。"""
        with self._lock:
            self.state.thinking_since_wall = time.time()
            self._write_locked()

    def add(self, **increments) -> None:
        with self._lock:
            for key, value in increments.items():
                setattr(self.state, key, getattr(self.state, key) + value)
            self._write_locked()

    def add_split(self, chapter: str, ticks: int, start: int | None = None, end: int | None = None, *, side: str | None = None) -> dict:
        """记下一截（一章，或按段通关时的几段）的成绩：章节计时与其间的死亡次数，并加进已完成的总计时。
        start / end：按段通关时这一截的起止检查点（end 为 None = 到通关）；整章不写。"""
        with self._lock:
            splits = list(self.state.splits or [])
            deaths_before = sum(s["deaths"] for s in splits)
            split = {"chapter": chapter, "ticks": int(ticks), "deaths": self.state.deaths - deaths_before}
            if side is not None:
                split['side'] = side
            if start is not None:
                split.update({"from": int(start), "to": end})
            splits.append(split)
            self.state.splits = splits
            self.state.piece_done = True
            self.state.completed_ticks = sum(s["ticks"] for s in splits)
            self._write_locked()
            return split

    def request_effort(self, level: str | None, reason: str = "") -> None:
        """智能体申请切换推理强度（None = 取消申请）；本轮结束后由 ClaudeRunner 生效。"""
        with self._lock:
            self.state.effort_pending = level
            self._effort_reason = reason if level else ""
            self._write_locked()

    def effort_applied(self, level: str, via: str) -> dict:
        """推理强度已经切换：记下切换前后、当时的房间与申请理由。"""
        with self._lock:
            entry = {"t": round(self.elapsed(), 1), "from": self.state.effort, "to": level,
                     "chapter": self.state.chapter, "room": self.state.room,
                     "reason": self._effort_reason, "via": via}
            self.state.effort = level
            self.state.effort_pending = None
            self.state.effort_changes += 1
            self.state.effort_log = [*(self.state.effort_log or []), entry]
            self._effort_reason = ""
            self._write_locked()
            return entry

    @property
    def tools_running(self) -> bool:
        return bool(self._tools)

    def set_tool_status(self, text: str) -> None:
        """实时展示：程序报告的进度（只在有工具在运行时显示）。"""
        with self._lock:
            text = text if self._tools else ""
            if text != self.state.tool_status:
                self.state.tool_status = text
                self._write_locked()

    def set_thought(self, thought: str, formal: bool = False) -> None:
        """formal：act 的 thought（正式思考），同时记成 formal_thought，开了"仅正式思考"时游戏内 HUD 只显示它。"""
        with self._lock:
            now = time.time()
            self.state.thought = thought
            self.state.thought_updated_wall = now
            if formal:
                self.state.formal_thought = thought
                self.state.formal_thought_wall = now
            self._write_locked()

    def set_temp_thought(self, thought: str) -> None:
        """开了"仅正式思考"时 narrate 写的临时思考：只记下来（控制台从公开历史里看），不换 HUD 上的思考语句。"""
        with self._lock:
            self.state.temp_thought = thought
            self.state.temp_thought_wall = time.time()
            self._write_locked()

    def snapshot(self) -> dict:
        with self._lock:
            self.state.elapsed_s = self.elapsed()
            return asdict(self.state)

    def write(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        self.state.written_wall = time.time()
        self.state.elapsed_s = self.elapsed()
        if not self.playing and not self.kb_done:
            self.state.study_s = self._running(time.monotonic())
        tmp = self._path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            return
        # Windows 上目标文件正被别的进程打开（HUD 服务每秒读十几次）时替换会被拒绝：稍等重试；
        # 实在不行就跳过这一次（下一次写入会补上），统计文件写不进去不能让运行中断
        for _ in range(40):
            try:
                os.replace(tmp, self._path)
                return
            except PermissionError:
                time.sleep(0.005)
            except OSError:
                return
