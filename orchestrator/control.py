"""本次运行的用户消息、控制指令和确认状态，SQLite 支持页面与主控跨进程交互。"""
import json
import sqlite3
import time
from contextlib import contextmanager
from .efforts import valid_effort


class RunControl:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "control.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY,kind TEXT,text TEXT,status TEXT,detail TEXT,wall REAL);
                CREATE TABLE IF NOT EXISTS replies(id TEXT PRIMARY KEY,text TEXT,wall REAL);
                CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY,data TEXT);
                CREATE TABLE IF NOT EXISTS preferences(key TEXT PRIMARY KEY,data TEXT);
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, kind, text=""):
        if kind not in {"message", "pause", "resume", "fast", "effort", "detach", "goal", "goal_clear", "stop", "cutscene_mode",
                        "interrupt", "chapters", "sim_update", "act_limit", "chapter_reset", "action_permissions", "bridge_reload",
                        "detour"}:
            raise ValueError("未知控制指令")
        text = str(text).strip()
        if kind == 'act_limit':
            data = json.loads(text)
            if not isinstance(data, dict) or set(data) != {'frames'} or type(data['frames']) is not int:
                raise ValueError('动作帧数上限须为整数 frames')
        if kind == 'bridge_reload':
            from .bridge_reload import options
            text = json.dumps(options(json.loads(text or '{}'), fill_defaults=False), ensure_ascii=False)
        if kind == 'action_permissions':
            from .action_policy import validate_update
            validate_update(json.loads(text))
        if kind == 'chapter_reset' and text not in {'allow', 'deny'}:
            raise ValueError('章节重置策略须为 allow 或 deny')
        if kind == 'detour' and text not in {'on', 'off'}:
            raise ValueError('计划外地图许可须为 on 或 off')
        if kind == 'cutscene_mode' and text not in {'auto','agent'}:
            raise ValueError('剧情模式须为 auto 或 agent')
        if kind == "goal":
            from .goals import validate_goal
            validate_goal(json.loads(text))
        if kind == "chapters":
            # 运行中改之后要通关的范围：{"segments": [[章节, 段], ...]}（按段），或旧格式 {"queue": [章节编号, ...]}（整章）
            data = json.loads(text)
            if isinstance(data, dict) and "segments" in data:
                segments = data["segments"]
                if (not isinstance(segments, list) or len(segments) > 200
                        or any(not isinstance(s, list) or len(s) != 2 or any(type(v) is not int or not 0 <= v <= 50 for v in s)
                               for s in segments)):
                    raise ValueError("段须为 [章节编号, 段号] 的列表")
            else:
                queue = data.get("queue") if isinstance(data, dict) else None
                if not isinstance(queue, list) or len(queue) > 20 or any(type(c) is not int or not 0 <= c <= 20 for c in queue):
                    raise ValueError("章节须为章节编号的列表")
        if kind == "fast" and text not in {"on", "off"}:
            raise ValueError("Fast 模式须为 on 或 off")
        if kind == 'effort' and not valid_effort(text):
            raise ValueError('推理强度须为自动或有效档位')
        if kind == "message" and not 1 <= len(text) <= 4000:
            raise ValueError("提示须为 1–4000 个字符")
        with self._db() as db:
            cursor = db.execute("INSERT INTO actions VALUES(NULL,?,?,?,?,?)", (kind, text, "queued", "等待主控接收", time.time()))
            return cursor.lastrowid

    def fast_preference(self):
        with self._db() as db:
            row = db.execute("SELECT data FROM preferences WHERE key='fast'").fetchone()
            return json.loads(row[0]) if row else None

    def chapter_reset_preference(self):
        permissions = self.action_permissions_preference()
        return all(permissions.values()) if permissions is not None else None

    def action_permissions_preference(self):
        from .action_policy import ActionPermissions
        with self._db() as db:
            current = db.execute("SELECT data FROM preferences WHERE key='action_permissions'").fetchone()
            if current:
                return ActionPermissions.read(json.loads(current[0])).snapshot()
            row = db.execute("SELECT data FROM preferences WHERE key='allow_chapter_reset'").fetchone()
            return ActionPermissions.legacy(json.loads(row[0])).snapshot() if row else None

    def set_chapter_reset(self, enabled):
        from .action_policy import ActionPermissions
        self.set_action_permissions(ActionPermissions.legacy(enabled).snapshot())

    def set_action_permissions(self, data):
        from .action_policy import ActionPermissions
        value = ActionPermissions.read(data).snapshot()
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('action_permissions',?)", (json.dumps(value),))

    def detour_preference(self):
        """运行中改过的计划外地图许可（True/False）；没改过时为 None，按 run_state 与命令行决定。"""
        with self._db() as db:
            row = db.execute("SELECT data FROM preferences WHERE key='allow_detour'").fetchone()
            value = json.loads(row[0]) if row else None
            return value if type(value) is bool else None

    def set_detour(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('计划外地图许可须为布尔值')
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('allow_detour',?)", (json.dumps(enabled),))

    def set_fast(self, enabled):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('fast',?)", (json.dumps(bool(enabled)),))

    def effort_preference(self):
        with self._db() as db:
            row=db.execute("SELECT data FROM preferences WHERE key='effort'").fetchone()
            value=json.loads(row[0]) if row else None
            return value if valid_effort(value) else None

    def set_effort(self, level):
        if level is not None and not valid_effort(level):
            raise ValueError('无效的手动推理强度')
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('effort',?)",(json.dumps(level),))

    def pending(self):
        with self._db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM actions WHERE status='queued' ORDER BY id")]

    def cutscene_preference(self):
        with self._db() as db:
            row=db.execute("SELECT data FROM preferences WHERE key='cutscene_mode'").fetchone()
            return json.loads(row[0]) if row else None

    def set_cutscene_mode(self, value):
        if value not in {'auto','agent'}:raise ValueError('剧情模式无效')
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO preferences VALUES('cutscene_mode',?)",(json.dumps(value),))

    def mark(self, ident, status, detail):
        with self._db() as db:
            db.execute("UPDATE actions SET status=?,detail=? WHERE id=?", (status, detail, ident))

    def reply(self, ident, text):
        if text.strip():
            with self._db() as db:
                db.execute("INSERT OR REPLACE INTO replies VALUES(?,?,?)", (ident, text, time.time()))

    def heartbeat(self, **state):
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps({**state, "wall": time.time()}),))

    def snapshot(self):
        with self._db() as db:
            row = db.execute("SELECT data FROM state WHERE id=1").fetchone()
            state = json.loads(row[0]) if row else {}
            actions = [dict(row) for row in db.execute("SELECT * FROM actions ORDER BY id DESC LIMIT 60")]
            messages = [{**row, "id": "user:" + str(row["id"]), "role": "user"} for row in actions if row["kind"] == "message"]
            messages += [{**dict(row), "role": "assistant", "status": "submitted"} for row in
                         db.execute("SELECT * FROM replies ORDER BY wall DESC LIMIT 60")]
            messages.sort(key=lambda message: message["wall"])
            connected = not state.get("stopped") and time.time() - state.get("wall", 0) < 5
            preference=db.execute("SELECT data FROM preferences WHERE key='effort'").fetchone()
            manual_effort=json.loads(preference[0]) if preference else None
            story=db.execute("SELECT data FROM preferences WHERE key='cutscene_mode'").fetchone()
            mode=json.loads(story[0]) if story else state.get('cutscene_mode','auto')
            return {**state, "cutscene_mode":mode, "effort_manual":manual_effort, "connected": connected,
                    "fast_pending":bool(connected and state.get('fast_pending')),
                    "messages": messages[-60:], "actions": actions[:10]}
