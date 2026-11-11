"""从当前会话最近的运行日志增量读取公开消息。"""
from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from orchestrator.conversations import session_key
from orchestrator.session_store import load_json


TAIL_BYTES = 512 * 1024
PREVIOUS_RUNS = 4


def read_object(path):
    try:
        value = load_json(path, encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def user_messages(directory):
    path = directory / "control.sqlite3"
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.2)) as db:
            return db.execute("SELECT id,text,status,wall FROM actions WHERE kind='message' ORDER BY id DESC LIMIT 60").fetchall()
    except sqlite3.Error:
        return []


def is_auto_retry(ident):
    """Only the act publisher owns this structured retry identity."""
    return isinstance(ident, str) and re.fullmatch(r'act:[0-9]+:[1-9][0-9]*', ident) is not None


def is_act_message(item):
    return (item.get('role') in (None, 'assistant') and isinstance(item.get('id'), str)
            and re.fullmatch(r'act:[0-9]+:[0-9]+', item['id']) is not None)


def visible_messages(items):
    visible, last_text = [], None
    for item in items:
        if is_act_message(item):
            if is_auto_retry(item['id']):
                continue
            text = item.get('text')
            if isinstance(text, str) and text and text == last_text:
                continue
            last_text = text if isinstance(text, str) and text else None
        else:
            last_text = None
        visible.append(item)
    return visible


class JsonTail:
    def __init__(self, path):
        self.path, self.offset, self.pending = path, 0, b""
        self.discarding = False

    def read(self):
        try:
            with self.path.open("rb") as stream:
                end = stream.seek(0, 2)
                if end < self.offset:
                    self.offset, self.pending = 0, b""
                    self.discarding = False
                if end - self.offset + len(self.pending) > TAIL_BYTES:
                    self.offset, self.pending = end - TAIL_BYTES, b""
                    stream.seek(self.offset - 1)
                    self.discarding = stream.read(1) != b"\n"
                stream.seek(self.offset)
                raw = self.pending + stream.read(end - self.offset)
                self.offset = stream.tell()
        except OSError:
            return []
        if self.discarding:
            _, newline, raw = raw.partition(b"\n")
            if not newline:
                return []
            self.discarding = False
        lines = raw.split(b"\n")
        self.pending = lines.pop()
        result = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except (ValueError, UnicodeError):
                continue
        return result


class PublicHistory:
    def __init__(self, directory: Path):
        self.events = JsonTail(directory / "codex_events.jsonl")
        self.public = JsonTail(directory / "public_events.jsonl")
        self.actions = JsonTail(directory / "decisions.jsonl")
        self.items = {}
        self.action_walls = {}  # Recent begin timestamps survive hidden bubbles until their completion rows arrive.
        self.lock = threading.Lock()

    def snapshot(self, metrics, limit=12):
        with self.lock:
            for event in self.public.read():
                if event.get("id") and isinstance(event.get("text"), str):
                    previous = self.items.get(event["id"])
                    if previous:
                        event["wall"] = previous["wall"]
                    elif is_act_message(event) and event['id'] in self.action_walls:
                        event['wall'] = self.action_walls[event['id']]
                    if is_act_message(event):
                        self.action_walls[event['id']] = event['wall']
                        if len(self.action_walls) > 256:
                            self.action_walls.pop(next(iter(self.action_walls)))
                    self.items[event["id"]] = event
            # Completed recordings made before the shared event stream keep their history.
            for event in ([] if self.public.path.exists() else self.events.read()):
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
            # 开了"仅正式思考"时 act 的 thought 是正式思考（控制台里与 narrate 写的临时思考区分开）
            act_kind = "正式思考" if (metrics or {}).get("formal_only") else "尝试"
            for action in self.actions.read():
                text = action.get("thought", "").strip()
                if text:
                    ident = f"act:{action.get('i')}:{action.get('attempt', 0)}"
                    previous = self.items.get(ident)   # 动作开始时已作为公开事件记下：保留那时的时间
                    kind = previous["kind"] if previous and previous.get("kind") in ("正式思考", "尝试") else act_kind
                    self.items[ident] = {"id": ident, "text": text, "kind": kind,
                                         "wall": previous["wall"] if previous else self.action_walls.get(
                                             ident, (metrics.get("started_wall") or 0) + action.get("t", 0))}
            for action_id, text, status, wall in user_messages(self.events.path.parent):
                ident = f"user:{action_id}"
                delivery = {"queued": "等待发送", "waiting": "等待提交", "submitted": "已提交", "error": "发送失败"}.get(status, "")
                self.items[ident] = {"id": ident, "text": text, "role": "user",
                    "kind": "用户 · " + delivery, "wall": wall}
            # 保留最近 80 条（控制台的对话栏要看得更多）；HUD 只取最后 12 条
            ordered = sorted(self.items.values(), key=lambda item: (item["wall"], item["id"]))
            ordered = visible_messages(ordered)[-80:]
            self.items = {item["id"]: item for item in ordered}
            return ordered[-max(1, min(int(limit), 80)):]


_histories = {}
_lock = threading.Lock()


def run_history(directory, metrics, limit):
    with _lock:
        if directory not in _histories:
            if len(_histories) >= 12:
                _histories.pop(next(iter(_histories)))
            _histories[directory] = PublicHistory(directory)
        reader = _histories[directory]
    return reader.snapshot(metrics or {}, limit)


def history_identity(state):
    ident, provider = state.get("session_id"), state.get("provider")
    if (state.get("engine") in ("claude", "codex") and isinstance(ident, str) and ident
            and (provider is None or isinstance(provider, str))):
        return session_key(state), state.get("agent")
    return None


def read_history(directory, metrics, limit=12):
    if directory is None:
        return []
    limit = max(1, min(int(limit), 80))
    directory = directory.resolve()
    items = run_history(directory, metrics, limit)
    if len(items) == limit:
        return items

    state = read_object(directory / "run_state.json")
    identity = history_identity(state)
    if identity is None:
        return items
    seen = {directory}
    for _ in range(PREVIOUS_RUNS):
        link = state.get("resumed_from")
        if not isinstance(link, str) or not Path(link).is_absolute():
            break
        try:
            previous = Path(link).resolve()
        except (OSError, RuntimeError, ValueError):
            break
        if previous.parent != directory.parent or previous in seen:
            break
        state = read_object(previous / "run_state.json")
        if history_identity(state) != identity:
            break
        seen.add(previous)
        earlier = run_history(previous, read_object(previous / "metrics.json"), limit - len(items))
        items = [{**item, "id": f"{previous.name}:{item['id']}"} for item in earlier] + items
        if len(items) == limit:
            break
    return items
