"""只读 Codex 会话遥测：仅导出用量、工具事件和公开 commentary，不导出推理正文。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path


def _wall(value: str | None) -> float:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class SessionTelemetry:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = b""
        self.lock = threading.Lock()
        self.last_poll = 0.0
        self.reset()

    def reset(self) -> None:
        self.data = {"source": "codex_session", "model": None, "effort": None,
                     "context_tokens": None, "context_window": None, "compactions": 0,
                     "tool_calls": 0, "tool_s": 0.0, "thinking_s": 0.0,
                     "public_commentary": "", "commentary_wall": 0.0, "status": "idle"}
        self.pending: dict[str, tuple[str, float]] = {}
        self.calls: set[str] = set()
        self.tool_intervals: list[tuple[float, float]] = []
        self.reasoning_items: set[str] = set()

    def consume(self, record: dict) -> None:
        kind, payload = record.get("type"), record.get("payload") or {}
        stamp = _wall(record.get("timestamp"))
        if kind == "turn_context":
            self.data.update(model=payload.get("model"), effort=payload.get("effort"))
        elif kind == "compacted":
            self.data["compactions"] += 1
        elif kind == "event_msg":
            event = payload.get("type")
            if event == "thread_settings_applied":
                settings = payload.get("thread_settings") or {}
                self.data.update(model=settings.get("model"), effort=settings.get("reasoning_effort"))
            elif event in ("task_started", "task_complete", "turn_aborted"):
                self.pending.clear()
                self.data["status"] = "thinking" if event == "task_started" else "idle"
            elif event == "token_count" and payload.get("info"):
                info = payload["info"]
                last = info.get("last_token_usage") or {}
                if "input_tokens" in last:
                    self.data["context_tokens"] = last["input_tokens"] + last.get("output_tokens", 0)
                self.data["context_window"] = info.get("model_context_window")
            elif event == "item_completed":
                item = payload.get("item") or {}
                if item.get("type") == "Reasoning" and item.get("id") not in self.reasoning_items:
                    # 只计耗时；绝不读取 summary_text / raw_content。
                    self.reasoning_items.add(item.get("id"))
                    elapsed = (payload.get("completed_at_ms", 0) - payload.get("started_at_ms", 0)) / 1000
                    self.data["thinking_s"] += max(0, elapsed)
        elif kind == "response_item":
            item_type = payload.get("type")
            if item_type in ("function_call", "custom_tool_call"):
                call_id = payload.get("call_id")
                if call_id and call_id not in self.calls:
                    self.calls.add(call_id)
                    self.data["tool_calls"] += 1
                    self.pending[call_id] = (payload.get("name") or "tool", stamp)
            elif item_type in ("function_call_output", "custom_tool_call_output"):
                call = self.pending.pop(payload.get("call_id"), None)
                if call and stamp >= call[1] > 0:
                    self.tool_intervals.append((call[1], stamp))
            elif item_type == "message" and payload.get("role") == "assistant" and payload.get("phase") == "commentary":
                parts = payload.get("content") or []
                text = " ".join(p.get("text", "") for p in parts
                                if isinstance(p, dict) and p.get("type") in ("output_text", "text"))
                if text.strip():
                    text = re.sub(r":codex-annotation\{[^}]*\}", "", text)
                    text = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", text)
                    self.data.update(public_commentary=" ".join(text.split())[:120], commentary_wall=stamp)

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        with self.lock:
            if now - self.last_poll >= 0.25:
                self.last_poll = now
                try:
                    if self.path.stat().st_size < self.offset:
                        self.offset, self.partial = 0, b""
                        self.reset()
                    with self.path.open("rb") as stream:
                        stream.seek(self.offset)
                        chunk = stream.read()
                        self.offset = stream.tell()
                    lines = (self.partial + chunk).split(b"\n")
                    self.partial = lines.pop()
                    for line in lines:
                        try:
                            self.consume(json.loads(line))
                        except (ValueError, TypeError):
                            continue
                except OSError:
                    return {"source": "codex_session", "available": False}
            intervals = self.tool_intervals + [(start, now) for _, start in self.pending.values() if start > 0]
            total, end = 0.0, 0.0
            for start, stop in sorted(intervals):
                total += max(0, stop - max(start, end))
                end = max(end, stop)
            tool = next(reversed(self.pending.values()))[0] if self.pending else ""
            return {**self.data, "available": True, "tool_s": round(total, 2),
                    "tool_call": tool, "status": "tool" if tool else self.data["status"]}


_readers: dict[str, SessionTelemetry] = {}


def apply_session(metrics: dict | None, config: dict) -> tuple[dict | None, dict]:
    """在 HTTP 响应层合并，不改写游戏统计，不覆盖独立 agent 的遥测。"""
    if not metrics or metrics.get("telemetry_source") not in ("codex_session", "external"):
        return metrics, config
    unavailable = {**metrics, "chat": {"available": False}, "model": None, "effort": None,
                   "context_tokens": None, "compactions": None}
    if metrics.get("telemetry_source") == "external":
        return unavailable, config
    thread = metrics.get("chat_thread_id") or ""
    if not thread or any(c not in "0123456789abcdef-" for c in thread.lower()):
        return unavailable, config
    reader = _readers.get(thread)
    if reader is None:
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        path = next((home / "sessions").rglob(f"*{thread}*.jsonl"), None)
        if path is None:
            return unavailable, config
        reader = _readers[thread] = SessionTelemetry(path)
    chat = reader.snapshot()
    result = {**metrics, "chat": chat}
    for key in ("model", "effort", "context_tokens", "compactions"):
        result[key] = chat.get(key)
    if chat.get("available"):
        # 兼容已经打开的旧版 HUD；新版页面使用 chat 字段单独呈现来源与口径。
        config = {**config, "names": {**config.get("names", {}), metrics["agent"]: chat.get("model") or "当前聊天"}}
        config["context_window"] = chat.get("context_window")
    return result, config
