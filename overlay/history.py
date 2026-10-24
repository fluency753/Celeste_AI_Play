"""从本次运行日志增量读取公开解说和动作说明；不读取内部推理。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from orchestrator.control import RunControl


class JsonTail:
    def __init__(self, path):
        self.path, self.offset, self.pending = path, 0, b""

    def read(self):
        try:
            with self.path.open("rb") as stream:
                if stream.seek(0, 2) < self.offset:
                    self.offset, self.pending = 0, b""
                stream.seek(self.offset)
                raw = self.pending + stream.read()
                self.offset = stream.tell()
        except OSError:
            return []
        lines = raw.split(b"\n")
        self.pending = lines.pop()
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except (ValueError, UnicodeError):
                continue
        return result


class PublicHistory:
    def __init__(self, directory: Path):
        self.events = JsonTail(directory / "codex_events.jsonl")
        self.actions = JsonTail(directory / "decisions.jsonl")
        self.items = {}
        self.lock = threading.Lock()

    def snapshot(self, metrics):
        with self.lock:
            for event in self.events.read():
                params = event.get("params") or {}
                item = params.get("item") or {}
                if item.get("type") == "contextCompaction" and event.get("method") in {"item/started", "item/completed"}:
                    ident = "system:compaction:" + str(item.get("id"))
                    previous = self.items.get(ident)
                    self.items[ident] = {"id": ident, "role": "system", "kind": "系统",
                        "text": "上下文压缩完成。" if event["method"] == "item/completed" else "正在压缩上下文，完成后继续处理对话…",
                        "wall": previous["wall"] if previous else (params.get("startedAtMs") or params.get("completedAtMs") or time.time()*1000) / 1000}
                    continue
                # 只收完整公开消息，delta 不会一段段刷屏，reasoning 从不进入这里。
                if event.get("method") != "item/completed" or item.get("type") != "agentMessage":
                    continue
                text = item.get("text", "").strip()
                if text:
                    ident = "public:" + str(item.get("id"))
                    self.items[ident] = {"id": ident, "text": text, "kind": "解说",
                                         "wall": (params.get("completedAtMs") or 0) / 1000}
            for action in self.actions.read():
                text = action.get("thought", "").strip()
                if text:
                    ident = f"act:{action.get('i')}:{action.get('attempt', 0)}"
                    self.items[ident] = {"id": ident, "text": text, "kind": "尝试",
                                         "wall": (metrics.get("started_wall") or 0) + action.get("t", 0)}
            if (self.events.path.parent / "control.sqlite3").exists():
                for message in RunControl(self.events.path.parent).snapshot()["messages"]:
                    if message["role"] == "user":
                        delivery = {"queued": "等待发送", "waiting": "等待提交", "submitted": "已提交", "error": "发送失败"}.get(message["status"], "")
                        self.items[message["id"]] = {"id": message["id"], "text": message["text"], "role": "user",
                            "kind": "你 · " + delivery, "wall": message["wall"]}
            ordered = sorted(self.items.values(), key=lambda item: (item["wall"], item["id"]))[-12:]
            self.items = {item["id"]: item for item in ordered}
            return ordered


_histories = {}
_lock = threading.Lock()


def read_history(directory, metrics):
    if directory is None:
        return []
    with _lock:
        if directory not in _histories:
            if len(_histories) >= 6:
                _histories.pop(next(iter(_histories)))
            _histories[directory] = PublicHistory(directory)
        reader = _histories[directory]
    return reader.snapshot(metrics or {})
