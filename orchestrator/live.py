"""实时展示：智能体的程序在运行期间用 /workspace/game/show.py 的 status() 写下进度（hud_status.json），
主控每 0.2 秒看一次，有新内容就转给 HUD 的"调用工具"一栏；这次工具调用返回后清空（见 Metrics.tool_end）。"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class LiveStatus:
    def __init__(self, path: Path, metrics, interval: float = 0.2):
        self.path = path
        self.metrics = metrics
        self.interval = interval
        self._stop = threading.Event()
        self._seen: int | None = None
        self._thread = threading.Thread(target=self._run, name="live-status", daemon=True)

    def start(self) -> "LiveStatus":
        self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                mtime = self.path.stat().st_mtime_ns
            except OSError:
                continue
            if mtime == self._seen:
                continue
            self._seen = mtime
            if not self.metrics.tools_running:
                continue          # 工具已经返回：不再显示这次的进度
            try:
                text = str(json.loads(self.path.read_text(encoding="utf-8")).get("status") or "")
            except (OSError, ValueError, AttributeError):
                continue
            self.metrics.set_tool_status(" ".join(text.split())[:100])
