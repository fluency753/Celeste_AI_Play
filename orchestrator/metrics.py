"""运行统计：思考时间、工具时间、游戏推进时间、上下文大小、压缩次数等。

统计口径与设计文档第 4.5 节一致：
- 思考时间：工具结果送回之后，到智能体发起下一次工具调用之间的墙钟时间（含模型推理与输出）。
- 工具时间：工具处理耗时减去其中真正推进游戏的耗时。
- 游戏推进时间：桥接逐帧推进所用的墙钟时间（动作块与自动推进段分开记）。
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
    effort: str = ""
    status: str = "starting"          # starting / thinking / executing / auto / compacting / done / stopped
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
    chapter: str = ""
    room: str = ""
    chapter_time_ticks: int = 0
    dashes: int = 0
    max_dashes: int = 0
    thought: str = ""
    thought_updated_wall: float = 0.0
    last_stop: str = ""
    usage_5h: float | None = None     # Max 套餐 5 小时窗口已用比例（0~1），来自 Claude Code 的 rate_limit_event
    usage_7d: float | None = None     # 7 天窗口已用比例
    usage_resets_at: int | None = None
    output_tokens: int = 0
    thinking_tokens: int = 0


class Metrics:
    def __init__(self, path: Path, agent: str, model: str, effort: str, budget_s: float):
        self._path = path
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.state = MetricsState(agent=agent, model=model, effort=effort,
                                  started_wall=time.time(), budget_s=budget_s)
        self.write()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self.state, key, value)
            self._write_locked()

    def add(self, **increments) -> None:
        with self._lock:
            for key, value in increments.items():
                setattr(self.state, key, getattr(self.state, key) + value)
            self._write_locked()

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
        self.state.elapsed_s = self.elapsed()
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
