"""One durable game-tool receipt, independent of whether a model received it."""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent


ACTION_TOOLS = {'act', 'cutscene', 'travel', 'exit_level'}


# act 结果里的逐帧轨迹很大（几百 KB 到几 MB），完整的在 /workspace/game/last_act.json，调用记录里只留行数和最后一帧
TRAJECTORY_KEYS = ('trajectory', 'entityTrajectory')


def compact_step(result):
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for key in TRAJECTORY_KEYS:
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = {'omitted': True, 'rows': len(rows), 'see': '/workspace/game/last_act.json',
                        'last': rows[-1] if rows and key == 'trajectory' else None}
    return out


class ToolJournal:
    def __init__(self, session):
        self.session = session
        self.current = ContextVar('game_tool_record', default=None)
        self.sequence = 0
        self.last_call = None
        self.last_action = None
        self.previous_actions = {}
        self.loaded = False

    @property
    def path(self):
        workspace = getattr(self.session, 'workspace', None)
        return Path(workspace) / 'game/last_tool.json' if workspace is not None else None

    @property
    def run_id(self):
        directory = getattr(self.session, 'run_dir', None)
        if directory is None:
            return None
        # 只写"智能体目录/运行目录名"，不把主机上的绝对路径暴露给容器里的智能体
        path = Path(directory).resolve()
        return f'{path.parent.name}/{path.name}'

    def snapshot(self):
        obs = getattr(self.session, 'last_obs', None) or {}
        level = obs.get('level') or {}
        return {'frame': obs.get('frame'), 'scene': obs.get('scene'),
                'sid': level.get('sid'), 'room': level.get('room'),
                'save_slot': getattr(self.session.cfg, 'save_slot', None),
                'decision': getattr(self.session, 'decision_index', None),
                'reload_epoch': getattr(self.session, 'reload_epoch', 0)}

    def _load(self):
        if self.loaded or self.path is None:
            return
        self.loaded = True
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                return
            previous = json.loads(raw.decode('utf-8'))
            if (previous.get('version') == 1 and previous.get('save') == self.session.cfg.save
                    and previous.get('agent') == self.session.cfg.agent
                    and self.session.cfg.save_slot is not None
                    and previous.get('save_slot') == self.session.cfg.save_slot):
                # Retain historical evidence on reconnect, including unconfirmed calls.
                # Its own run_id remains intact; it is not a fact about the new run.
                action = previous.get('last_action')
                if isinstance(action, dict) and isinstance(action.get('sequence'), int):
                    self.last_action = action
                    self.sequence = max(0, action['sequence'])
                call = previous.get('last_call') or {}
                if isinstance(call.get('sequence'), int):
                    self.sequence = max(self.sequence, call['sequence'])
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def new(self, tool, arguments):
        return {'id': uuid.uuid4().hex, 'tool': tool, 'arguments': deepcopy(arguments),
                'run_id': self.run_id, 'save': getattr(self.session.cfg, 'save', None),
                'save_slot': getattr(self.session.cfg, 'save_slot', None),
                'requested_at': time.time(), 'handler_entered': False}

    def _publish(self):
        path = self.path
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {'version': 1, 'run_id': self.run_id, 'save': self.session.cfg.save,
                    'save_slot': getattr(self.session.cfg, 'save_slot', None),
                    'agent': self.session.cfg.agent, 'last_call': self.last_call, 'last_action': self.last_action,
                    'meaning': 'returned 表示工具已返回，不代表动作执行成功或模型收到结果；以 detail 和 result 为准。'
                               'running/执行未确认不可直接重放；前后快照不是独占执行帧数。'}
        text = json.dumps(document, ensure_ascii=False, indent=1)   # 分行写，Read 工具能逐行读
        for attempt in range(3):
            temporary = path.with_name('.last_tool-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(text, encoding='utf-8')
                temporary.replace(path)
                return
            except PermissionError:
                # Windows 上容器正在读这个文件或杀毒软件在扫临时文件时，替换会短暂失败：稍等再试
                if attempt == 2:
                    raise
                time.sleep(0.05)
            finally:
                temporary.unlink(missing_ok=True)

    def start(self, record, *, handler_entered=True):
        if 'sequence' in record:
            return
        self._load()
        self.sequence += 1
        self.previous_actions[record['id']] = self.last_action
        record.update(sequence=self.sequence, started_at=time.time(), status='running',
                      handler_entered=handler_entered, before=self.snapshot(),
                      execution_status='unconfirmed' if handler_entered else 'not_started')
        self.last_call = record
        if handler_entered and record['tool'] in ACTION_TOOLS:
            self.last_action = record
        try:
            self._publish()
        except (OSError, ValueError, TypeError) as exc:
            # 记录只是恢复时的参考：写不进去也照常执行（连 stop_run 都不能因此被拒），
            # 完成时再写一次；那次也失败才在结果里提醒（见 finish）
            record['start_write_failed'] = str(exc) or type(exc).__name__
            print(f'[tools] 调用记录开始时未能写入，照常执行：{exc}', flush=True)

    @asynccontextmanager
    async def execution(self, lock):
        async with lock:
            record = self.current.get()
            if record is not None:
                self.start(record)
            try:
                yield
            finally:
                if record is not None:
                    record['after'] = self.snapshot()

    def start_current(self):
        record = self.current.get()
        if record is not None:
            self.start(record)

    def detail(self, value):
        record = self.current.get()
        if record is not None:
            record['detail'] = deepcopy(value)

    def not_executed(self):
        """Only explicit pre-dispatch/validation branches may assert no execution."""
        record = self.current.get()
        if record is not None:
            record.update(execution_status='not_started', not_executed=True)
            if self.last_action is record:
                self.last_action = self.previous_actions.get(record['id'])

    def finish(self, record, *, result=None, error=None):
        try:
            if 'sequence' not in record:
                self.start(record, handler_entered=False)
            record.update(status='raised' if error is not None else 'returned', finished_at=time.time())
            record.setdefault('after', self.snapshot())
            if error is not None:
                record['error'] = str(error) or type(error).__name__
                record['execution_status'] = ('unconfirmed' if record['handler_entered']
                                              and not record.get('not_executed') else 'not_started')
                record['result'] = {'is_error': True, 'content': [{'type': 'text', 'text': record['error']}]}
            else:
                record['execution_status'] = ('not_started' if record.get('not_executed') else
                                              'reported' if 'detail' in record else 'see_result')
                content = []
                for block in result.content:
                    if block.type == 'text':
                        content.append({'type': 'text', 'text': block.text})
                    elif block.type == 'image':
                        content.append({'type': 'image', 'mimeType': block.mime_type,
                                        'metadata': deepcopy(block.meta), 'data_omitted': True})
                record['result'] = {'is_error': bool(result.is_error), 'content': content}
            self._publish()
        except (OSError, ValueError, TypeError, ToolError) as exc:
            print(f'[tools] 调用记录未能更新：{exc}', flush=True)
            return ('调用已经处理，上面的实际结果仍有效；但 last_tool.json 完成记录写入失败，'
                    '文件可能仍为 running 或旧记录。恢复时先观察核验，不要据此重放动作。')
        finally:
            self.previous_actions.pop(record['id'], None)
        return None

    def refused(self, tool, arguments, reason):
        record = self.new(tool, arguments)
        # A pre-dispatch refusal is not a potentially executed action.
        return self.finish(record, error=ToolError(reason))


class RecordedMCPServer(MCPServer):
    """The SDK HTTP handler also calls this override, not just direct callers."""
    def __init__(self, session, **options):
        self.journal = ToolJournal(session)
        super().__init__(**options)

    async def call_tool(self, name, arguments, context=None):
        record = self.journal.new(name, arguments)
        token = self.journal.current.set(record)
        try:
            try:
                result = await super().call_tool(name, arguments, context)
            except BaseException as exc:
                warning = self.journal.finish(record, error=exc)
                if warning and isinstance(exc, Exception):
                    raise ToolError(str(exc) + '；' + warning) from exc
                raise
            warning = self.journal.finish(record, result=result)
            if warning:
                result.content.append(TextContent(type='text', text=warning))
            return result
        finally:
            self.journal.current.reset(token)
