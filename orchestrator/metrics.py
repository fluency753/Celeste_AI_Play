"""运行统计：思考时间、工具时间、游戏推进时间、上下文大小、压缩次数等。

统计口径与设计文档第 4.5 节一致：
- 思考时间：工具结果送回之后，到智能体发起下一次工具调用之间的墙钟时间（含模型推理与输出）。
- 工具时间：工具处理耗时减去其中真正推进游戏的耗时。
- 游戏推进时间：桥接逐帧推进所用的墙钟时间（动作块与自动推进段分开记）。
- 准备时间：开始游玩前读知识库、写笔记与程序的墙钟时间，单独记，不计入现实用时；
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
    agent: str = ""
    model: str = ""
    effort: str = ""                  # 当前生效的推理强度（low / medium / high / xhigh / max）
    effort_pending: str | None = None  # 智能体用 set_effort 申请、本轮结束后才生效的强度
    effort_changes: int = 0           # 推理强度实际切换的次数
    effort_log: list | None = None    # 每次切换：[{"t": 现实用时, "from", "to", "chapter", "room", "reason"}, ...]
    status: str = "starting"         # starting / studying / thinking / executing / auto / retrying / compacting / quota_wait / done / stopped
    started_wall: float = 0.0
    elapsed_s: float = 0.0
    budget_s: float = 0.0
    thinking_s: float = 0.0
    tool_s: float = 0.0
    play_game_s: float = 0.0
    auto_game_s: float = 0.0
    decisions: int = 0
    frames_played: int = 0
    frames_auto: int = 0
    deaths: int = 0
    context_tokens: int = 0
    compactions: int = 0
    compact_s: float = 0.0            # 压缩本身花的时间（已包含在思考时间里）
    retries: int = 0                  # 自动重试次数（重算程序运行次数，不唤醒大模型）
    replan_s: float = 0.0             # 重算程序用时（已包含在工具时间里）
    study_s: float = 0.0              # 准备阶段（读知识库等）用时，不计入 elapsed_s
    kb_progress: float | None = None  # 知识库已读比例（0~1）；None = 没有准备阶段
    chapter: str = ""
    room: str = ""
    chapter_time_ticks: int = 0
    completed_ticks: int = 0          # 本次运行里已经通关的章节的计时之和（依次通关多章时，总计时 = 它 + 当前章节计时）
    splits: list | None = None        # 已通关章节的分段成绩：[{"chapter": sid, "ticks": n, "deaths": n}, ...]
    dashes: int = 0
    max_dashes: int = 0
    thought: str = ""
    thought_updated_wall: float = 0.0
    last_stop: str = ""
    usage_5h: float | None = None     # Max 套餐 5 小时窗口已用比例（0~1），来自 Claude Code 的 rate_limit_event
    usage_7d: float | None = None     # 7 天窗口已用比例
    usage_resets_at: int | None = None
    waited_s: float = 0.0             # 等待 Max 额度重置的总时间（不计入现实用时）
    quota_waits: int = 0              # 等待额度重置的次数
    quota_wait_until: float | None = None  # 正在等待时：预计恢复的时刻（墙钟，秒）
    resumed_from: str | None = None   # 续跑时：上一段运行的日志目录
    output_tokens: int = 0
    thinking_tokens: int = 0
    # HUD 用来在两次写入之间自己走表的时间点（墙钟，秒）
    phase_since_wall: float = 0.0     # 当前 status 开始的时刻
    thinking_since_wall: float = 0.0  # 当前这段思考开始的时刻（上一次工具结果送回）
    chapter_time_wall: float = 0.0    # chapter_time_ticks 最近一次更新的时刻
    written_wall: float = 0.0         # 本文件最近一次写出的时刻


class Metrics:
    def __init__(self, path: Path, agent: str, model: str, effort: str, budget_s: float, study: bool = False):
        self._path = path
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._offset = 0.0              # 续跑时上一段运行已用的现实用时
        self._paused = 0.0              # 从 _t0 起暂停（等待额度）的总秒数
        self._pause_start: float | None = None
        self._effort_reason = ""
        self.playing = not study
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

    def start_play(self) -> None:
        """准备阶段结束，开始计现实用时。"""
        with self._lock:
            if self.playing:
                return
            now = time.monotonic()
            self.state.study_s = self._running(now)
            self._t0 = now
            self._paused = 0.0
            self._pause_start = now if self._pause_start is not None else None
            self.playing = True
            self.state.started_wall = time.time()
            self.state.thinking_since_wall = self.state.started_wall
            self.state.phase_since_wall = self.state.started_wall
            self.state.status = "thinking"
            self._write_locked()

    def pause(self, until_wall: float | None = None) -> None:
        """等待额度重置：现实用时（或准备时间）停止走表。"""
        with self._lock:
            if self._pause_start is None:
                self._pause_start = time.monotonic()
            now = time.time()
            if self.state.status != "quota_wait":
                self.state.phase_since_wall = now
            self.state.status = "quota_wait"
            self.state.quota_wait_until = until_wall
            self._write_locked()

    def resume(self) -> float:
        """额度恢复：返回这次等待的秒数。"""
        with self._lock:
            if self._pause_start is None:
                return 0.0
            waited = time.monotonic() - self._pause_start
            self._paused += waited
            self._pause_start = None
            self.state.waited_s += waited
            self.state.quota_waits += 1
            self.state.quota_wait_until = None
            self.state.status = "thinking" if self.playing else "studying"
            self.state.phase_since_wall = time.time()
            self._write_locked()
            return waited

    RESTORED = ("thinking_s", "tool_s", "play_game_s", "auto_game_s", "decisions", "frames_played", "frames_auto",
                "deaths", "compactions", "compact_s", "retries", "replan_s", "study_s", "kb_progress",
                "completed_ticks", "splits", "output_tokens", "thinking_tokens", "waited_s", "quota_waits",
                "chapter", "room", "chapter_time_ticks", "dashes", "max_dashes", "thought", "context_tokens",
                "effort_pending", "effort_changes", "effort_log")

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
            self._write_locked()

    def update(self, **changes) -> None:
        with self._lock:
            now = time.time()
            if "status" in changes and changes["status"] != self.state.status:
                self.state.phase_since_wall = now
            if "chapter_time_ticks" in changes and changes["chapter_time_ticks"] != self.state.chapter_time_ticks:
                self.state.chapter_time_wall = now
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

    def add_split(self, chapter: str, ticks: int) -> dict:
        """记下一章的成绩（章节计时与这一章里的死亡次数），并把它加进已完成章节的总计时。"""
        with self._lock:
            splits = list(self.state.splits or [])
            deaths_before = sum(s["deaths"] for s in splits)
            split = {"chapter": chapter, "ticks": int(ticks), "deaths": self.state.deaths - deaths_before}
            splits.append(split)
            self.state.splits = splits
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

    def set_thought(self, thought: str) -> None:
        with self._lock:
            self.state.thought = thought
            self.state.thought_updated_wall = time.time()
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
        if not self.playing:
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
