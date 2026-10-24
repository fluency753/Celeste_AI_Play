"""HUD 的会话选择与聊天宿主交接；不会连接游戏或自动开始章节。"""
import json
import hashlib
from contextlib import closing
from pathlib import Path
import re
import shutil
import sqlite3
import time

from .chat import can_wake
from .control import RunControl
from .runtimes import RUNTIMES, runtime

ROOT = Path(__file__).resolve().parent.parent


def session_key(state):
    engine = state['engine']
    return ':'.join((engine, state.get('provider') or ('codex' if engine == 'codex' else 'anthropic'), state['session_id']))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def selected_run(anchor):
    if anchor is None:
        return None
    relative = read_json(anchor / "hud_session.json").get("relative")
    if not isinstance(relative, str):
        return anchor
    target = (anchor / relative).resolve()
    if (target == anchor.resolve() or (anchor / "chats").resolve() in target.parents) and can_wake(target):
        return target
    return anchor


def catalog(anchor, runs_root=None, archive_root=None):
    if anchor is None:
        return []
    root = runs_root or ROOT / "runs"
    candidates = list(root.glob("*/*/run_state.json"))
    candidates += list(anchor.glob("chats/*/run_state.json"))
    candidates.append(anchor / "run_state.json")
    records = {}
    for path in sorted(set(candidates), key=lambda p: str(p.parent)):
        state = read_json(path)
        ident = state.get("session_id")
        if state.get("engine") not in RUNTIMES or not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ident) or runtime(state['engine']).archived(ident, archive_root):
            continue
        stamp = state.get("conversation_origin", path.parent.name)
        if re.fullmatch(r"\d{8}-\d{6}", stamp):
            stamp = f"{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
        else:
            stamp = "聊天会话"
        records[session_key(state)] = {"id": ident, "key":session_key(state), "directory": str(path.parent), "model": state.get("model", ""), "engine":state["engine"], "provider":state.get("provider"),
                          "label": f"{state.get('session_name') or stamp} · {state.get('model', '')} · {ident[-8:]}"}
    # 选择过的会话优先使用它自己的聊天目录，保留该会话后来的消息和 Fast 设置。
    for path in anchor.glob("chats/*/run_state.json"):
        state = read_json(path)
        ident = session_key(state) if state.get('engine') in RUNTIMES and state.get('session_id') else None
        if ident in records:
            records[ident]["directory"] = str(path.parent)
            origin = state.get("conversation_source", path.parent.name)
            source_state = {**state, 'session_id':origin}
            if origin != state['session_id']:
                records.pop(session_key(source_state), None)
    return list(records.values())


def start_chat(directory):
    from .background import launch
    existing = RunControl(directory).snapshot()
    if existing.get('connected'):
        if not existing.get('chat_only'):
            raise ValueError('该目录已有试玩主控，不能另外唤起聊天')
        return
    started = time.time()
    host, _ = launch(["--run", str(directory)], service="chat")
    (directory / "chat_host.json").write_text(json.dumps({"directory": str(host), "wall": time.time()}), encoding="utf-8")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        snapshot = RunControl(directory).snapshot()
        if snapshot["connected"] and snapshot.get("wall", 0) >= started:
            return
        if read_json(host / "status.json").get("state") in {"exited", "failed"}:
            raise ValueError(f"目标聊天启动失败，日志位于 {host / 'output.log'}")
        time.sleep(.1)
    raise ValueError("目标聊天未及时连接，未切换 HUD")


def release_chat(directory):
    snapshot = RunControl(directory).snapshot()
    host = read_json(directory / "chat_host.json").get("directory")
    if snapshot["connected"]:
        if not snapshot.get("chat_only"):
            raise ValueError("试玩尚在进行，不能切走控制会话")
        if snapshot.get("phase") == "chatting":
            raise ValueError("agent 正在回复，请等回复结束后切换")
        if not host:
            raise ValueError("未找到聊天宿主，无法安全交接")
        RunControl(directory).enqueue("detach")
    if host:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = read_json(Path(host) / "status.json")
            if status.get("state") in {"exited", "failed"}:
                return
            time.sleep(.1)
        raise ValueError("旧聊天宿主尚未退出，未启动另一个主控")


def prepare_target(anchor, record):
    source = Path(record["directory"])
    state = read_json(source / 'run_state.json')
    state.setdefault('provider', 'codex' if state.get('engine') == 'codex' else 'anthropic')
    if source == anchor or (anchor / "chats").resolve() in source.resolve().parents:
        return source
    # 一个会话一个目录。复制已完成试玩的统计，避免把旧聊天当时的房间误当成当前游戏进度。
    target = anchor / 'chats' / hashlib.sha256(session_key({**state,'session_id':record['id']}).encode()).hexdigest()[:24]
    if (target / "run_state.json").exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    previous = read_json(source / "metrics.json")
    current_state, metrics = read_json(anchor / "run_state.json"), read_json(anchor / "metrics.json")
    current_state.update(session_id=record["id"], model=state["model"],
                         conversation_source=record["id"],
                         effort=previous.get("effort", state["effort"]),
                         conversation_origin=source.name,
                         knowledge_profile=state.get("knowledge_profile", "explore"))
    for key in ("engine", "provider", "runtime", "mcp_port", "requested_model", "agent", "save", "save_slot", "session_kind", "session_name", "prepared_dir"):
        if key in state:
            current_state[key] = state[key]
        elif key in {"save_slot", "session_kind", "session_name", "prepared_dir", "requested_model", "mcp_port"}:
            current_state.pop(key, None)
    for key in ("agent", "telemetry_source", "context_tokens", "context_window", "tool_calls", "compactions", "output_tokens", "thinking_tokens", "effort", "kb_progress", "study_s"):
        if key in previous:
            metrics[key] = previous[key]
    metrics["model"] = state["model"]
    metrics["agent"] = state["agent"]
    (target / "run_state.json").write_text(json.dumps(current_state, ensure_ascii=False), encoding="utf-8")
    (target / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False), encoding="utf-8")
    if (source / 'summary.json').exists():
        shutil.copy2(source / 'summary.json', target / 'summary.json')
    for name in ("public_events.jsonl", "codex_events.jsonl", "decisions.jsonl", "prepare_summary.json"):
        if (source / name).exists():
            shutil.copy2(source / name, target / name)
    if (source / "control.sqlite3").exists():
        with closing(sqlite3.connect(source / "control.sqlite3")) as old, closing(sqlite3.connect(target / "control.sqlite3")) as new:
            old.backup(new)
        with closing(sqlite3.connect(target / "control.sqlite3")) as db:
            db.execute("DELETE FROM state")
            db.execute("UPDATE actions SET status='error',detail='历史运行的未完成指令，不重新执行' WHERE status IN ('queued','waiting')")
            db.commit()
    return target


def switch_session(anchor, current, ident):
    if not can_wake(anchor):
        raise ValueError("本次试玩结束后才能切换聊天会话")
    rows = catalog(anchor)
    records = {record.get('key', record['id']): record for record in rows}
    if ident not in records:
        matches = [record for record in rows if record['id'] == ident]
        if len(matches) == 1:
            records[ident] = matches[0]
    if ident not in records:
        raise ValueError("会话不属于本项目的可用 agent，或已归档")
    state = read_json(current / "run_state.json")
    if state.get('session_id') == records[ident]['id'] and session_key(state) == records[ident].get('key',session_key(state)):
        return current
    target = prepare_target(anchor, records[ident])
    if target == current:
        return current
    release_chat(current)
    try:
        start_chat(target)
    except Exception:
        # 启动失败时保留旧选择，尽量恢复原聊天入口。
        start_chat(current)
        raise
    relative = str(target.relative_to(anchor))
    temporary = anchor / "hud_session.tmp"
    temporary.write_text(json.dumps({"relative": relative}), encoding="utf-8")
    temporary.replace(anchor / "hud_session.json")
    return target
