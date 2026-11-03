"""Read-only, run-owned tool facts. Never expose model reasoning or result bodies."""
import json
import os
from pathlib import Path
import re
import threading
import time
from collections import deque
import math

from orchestrator.tool_facts import safe_text, request_summary, result_summary

ROOT = Path(__file__).resolve().parents[1]


def mapping(value):
    return value if isinstance(value, dict) else {}


class BoundedTail:
    def __init__(self, path, budget=4*1024*1024, line_limit=4*1024*1024):
        self.path, self.budget, self.line_limit = path, budget, line_limit
        self.offset, self.pending, self.discard = 0, b'', False
        self.identity, self.skipped, self.loading = None, 0, False

    def read(self):
        try:
            with self.path.open('rb') as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if identity != self.identity or stat.st_size < self.offset:
                    self.offset, self.pending, self.discard = 0, b'', False
                    self.identity = identity
                stream.seek(self.offset)
                raw = stream.read(self.budget)
                self.offset = stream.tell()
                self.loading = self.offset < stat.st_size
        except OSError:
            return []
        pieces = raw.split(b'\n')
        rows = []
        for index, part in enumerate(pieces):
            complete = index < len(pieces)-1
            if not self.discard:
                if len(self.pending)+len(part) > self.line_limit:
                    self.pending = b''
                    self.discard = True
                    self.skipped += 1
                else:
                    self.pending += part
            if complete:
                if not self.discard and self.pending:
                    try:
                        decoded = json.loads(self.pending)
                        if isinstance(decoded, dict): rows.append(decoded)
                        else: self.skipped += 1
                    except (ValueError, UnicodeError):
                        self.skipped += 1
                self.pending, self.discard = b'', False
        return rows


class ToolHistory:
    def __init__(self, directory, codex_home=None):
        self.directory = Path(directory).resolve()
        self.home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home()/'.codex').resolve()
        self.timing = BoundedTail(self.directory/'decision_timing.jsonl')
        self.claude = BoundedTail(self.directory/'agent_events.jsonl')
        self.app = BoundedTail(self.directory/'codex_events.jsonl')
        self.deliveries = BoundedTail(self.directory/'tool_deliveries.jsonl')
        self.receipts, self.delivery_states = {}, {}
        self.native = None
        self.app_facts = False
        self.rows, self.owned, self.attachments = {}, set(), {}
        self.owned_order = deque()
        self.image_messages, self.image_order = set(), deque()
        self.lock = threading.Lock()
        self.warning = ''
        self.session_id, self.locate_at = None, 0

    def _row(self, ident, name='', wall=0):
        if not isinstance(wall, (int, float)) or not math.isfinite(wall): wall = 0
        if ident not in self.rows:
            self._seq = getattr(self, '_seq', 0) + 1     # 读到的先后：没有时间的行按它排，不按随机的编号排
            self.rows[ident] = {'id': ident, 'tool': name, 'text': safe_text(name), 'wall': wall, 'seq': self._seq,
                                'status': 'running', 'parameters': None, 'result': None}
        row = self.rows[ident]
        if name and not row['tool']:
            row['tool'] = name
        if wall and not row['wall']:
            row['wall'] = wall
        return row

    def _complete(self, ident, name, args, contents, success):
        row = self._row(ident, name)
        row['text'], row['parameters'] = request_summary(name, args)
        row['result'] = result_summary(contents, success)
        row['status'] = 'failed' if success is False else 'returned' if success is True else 'unknown'

    def _app_event(self, event, session):
        params = mapping(event.get('params'))
        if params.get('threadId') not in (None, session):
            return
        method, item = event.get('method'), mapping(params.get('item'))
        fact = mapping(params.get('fact'))
        if method == 'tool/history' and fact.get('phase') in ('requested', 'returned'):
            ident = fact.get('id')
            if not isinstance(ident, str) or not ident:
                return
            self.app_facts = True
            row = self._row(ident, str(fact.get('tool') or ''), event.get('wall', 0))
            if fact.get('phase') == 'requested':
                row['text'] = safe_text(fact.get('text') or row['tool'], 220)
                parameters = fact.get('parameters')
                if isinstance(parameters, dict):
                    row['parameters'] = {safe_text(k, 80): safe_text(v, 1200) for k, v in list(parameters.items())[:32]}
            elif fact.get('phase') == 'returned':
                row['status'] = 'failed' if fact.get('success') is False else 'returned' if fact.get('success') is True else 'unknown'
                if isinstance(fact.get('result'), dict):
                    row['result'] = {safe_text(k, 80): (v if isinstance(v, (int, float, bool)) or v is None else safe_text(v, 120))
                                     for k, v in list(fact['result'].items())[:32]}
            return
        kind, ident = item.get('type'), item.get('id')
        if (method not in ('item/started', 'item/completed') or not isinstance(ident, str) or not ident
                or not isinstance(kind, str) or kind not in ('dynamicToolCall', 'commandExecution', 'fileChange', 'mcpToolCall', 'imageView')):
            return
        # Old dynamic projections lack thread identity; only accept IDs owned by this run's timing.
        if kind == 'dynamicToolCall' and not params.get('threadId') and ident not in self.owned:
            return
        self.app_facts = True
        wall = params.get('startedAtMs') if method == 'item/started' else params.get('completedAtMs')
        wall = wall / 1000 if isinstance(wall, (int, float)) else event.get('wall', 0)
        names = {'commandExecution': 'Bash', 'fileChange': 'apply_patch', 'imageView': 'view_image'}
        name = item.get('tool') or names.get(kind) or 'MCP'
        row = self._row(ident, str(name), wall)
        if method == 'item/started' and wall:
            row['wall'] = wall
        if kind != 'dynamicToolCall':
            args = item.get('arguments')
            if kind == 'commandExecution':
                args = {k: item[k] for k in ('command', 'cwd') if k in item}
            elif kind == 'fileChange':
                changes = item.get('changes')
                args = {'path': ', '.join(str(c.get('path', '')) for c in (changes if isinstance(changes, list) else []) if isinstance(c, dict))[:1200]}
            elif kind == 'imageView':
                args = {'path': item.get('path', '')}
            row['text'], row['parameters'] = request_summary(str(name), args)
        elif row['parameters'] is None:
            row['text'] = request_summary(str(name), {})[0]
        duration = item.get('durationMs')
        if isinstance(duration, (int, float)) and math.isfinite(duration) and duration >= 0:
            row.setdefault('duration_s', duration / 1000)
        if method == 'item/completed':
            status = item.get('status')
            success = item.get('success')
            if kind == 'commandExecution' and isinstance(item.get('exitCode'), int):
                success = item['exitCode'] == 0
                row['result'] = {'程序退出码': item['exitCode']}
            if status in ('failed', 'declined') or (kind == 'mcpToolCall' and item.get('error') is not None):
                success = False
            elif kind != 'dynamicToolCall' and status == 'completed' and success is None:
                success = True
            if kind != 'dynamicToolCall' or row['result'] is None:
                row['status'] = 'failed' if success is False else 'returned' if success is True else row['status'] if row['status'] in ('returned', 'failed') else 'unknown'
            if wall > row['wall']:
                row.setdefault('duration_s', wall - row['wall'])

    def _delivery_event(self, event, session):
        if event.get('thread') not in (None, session):
            return
        ident, receipt = event.get('call_id'), event.get('receipt')
        if isinstance(receipt, str) and receipt:
            if receipt not in self.receipts and len(self.receipts) >= 4096:
                self.receipts.pop(next(iter(self.receipts)))
            record = self.receipts.setdefault(receipt, {})
            # All phases carry audit metadata in current logs; tolerate out-of-order older phases.
            if isinstance(ident, str): record['id'] = ident
            if isinstance(event.get('images'), int): record['images'] = max(0, event['images'])
            if event.get('event') == 'recorded': record['status'] = 'recorded'
            elif record.get('status') != 'recorded':
                record['status'] = {'queued': 'queued', 'unconfirmed': 'unconfirmed', 'dropped': 'unconfirmed'}.get(event.get('event'), record.get('status', 'pending'))
            ident = record.get('id')
            if isinstance(ident, str):
                self.delivery_states[ident] = {k: record[k] for k in ('status', 'images') if k in record}
        elif isinstance(ident, str):
            if event.get('event') == 'observation_queued':
                self.delivery_states[ident] = {'images': event.get('images'), 'status': 'queued'}
            elif event.get('event') == 'observation_unconfirmed':
                self.delivery_states[ident] = {'status': 'unconfirmed'}

        while len(self.delivery_states) > 4096:
            self.delivery_states.pop(next(iter(self.delivery_states)))

    def _locate(self, state):
        ident = state.get('session_id')
        if not isinstance(ident, str) or not re.fullmatch(r'[0-9a-f-]{36}', ident):
            return
        if self.session_id != ident:
            self.native, self.session_id, self.locate_at = None, ident, 0
        if self.native is not None or time.monotonic()-self.locate_at < 5:
            return
        self.locate_at = time.monotonic()
        sessions = self.home/'sessions'
        for candidate in sessions.glob('*/*/*/*'+ident+'*.jsonl'):
            path = candidate.resolve()
            if not path.is_relative_to(sessions.resolve()):
                continue
            try:
                with path.open('rb') as stream:
                    line = stream.readline(256000)
                meta = mapping(mapping(json.loads(line)).get('payload'))
                cwd = Path(meta.get('cwd', '')).resolve()
                if meta.get('id') == ident and cwd.is_relative_to((ROOT/'runs').resolve()):
                    self.native = BoundedTail(path)
                    self.warning = ''
                    return
            except (OSError, ValueError, TypeError):
                continue
        self.warning = '无法校验该会话的原生日志，暂仅显示本次主控记录的调用时间；参数未知。'

    def snapshot(self, limit=50):
        with self.lock:
            for event in self.timing.read():
                ident = event.get('tool_id')
                if event.get('event') == 'tool_requested' and isinstance(ident, str) and ident:
                    if ident not in self.owned:
                        if len(self.owned_order) >= 4096:
                            expired = self.owned_order.popleft()
                            self.owned.discard(expired)
                            self.rows.pop(expired, None)
                        self.owned.add(ident)
                        self.owned_order.append(ident)
                    self._row(ident, str(event.get('tool') or '').split(' · ')[0], event.get('wall', 0))
                elif event.get('event') == 'tool_completed' and isinstance(ident, str) and ident in self.owned:
                    row = self.rows.get(ident)
                    if row is None: continue  # Old, evicted calls cannot break current history.
                    if row['status'] == 'running': row['status'] = 'unknown'
                    row['duration_s'] = event.get('tool_active_s')
                elif event.get('event') == 'turn_ended':
                    pending = event.get('pending_tools')
                    for ident in pending if isinstance(pending, list) else []:
                        if isinstance(ident, str) and ident in self.rows and self.rows[ident]['status'] == 'running':
                            self.rows[ident]['status'] = 'interrupted'
            try:
                state = mapping(json.loads((self.directory/'run_state.json').read_text(encoding='utf-8-sig')))
            except (OSError, ValueError):
                state = {}
            if state.get('engine') == 'codex':
                self._locate(state)
                if (self.directory/'codex_events.jsonl').exists() and not self.timing.loading:
                    for event in self.app.read():
                        self._app_event(event, state.get('session_id'))
                    for event in self.deliveries.read():
                        self._delivery_event(event, state.get('session_id'))
                if self.app_facts:
                    self.warning = ''
                for event in self.native.read() if self.native and not self.timing.loading else []:
                    payload = mapping(event.get('payload'))
                    item = mapping(payload.get('item'))
                    ident = item.get('id')
                    if (payload.get('type') == 'item_completed' and item.get('type') == 'DynamicToolCall'
                            and ident in self.owned and ident in self.rows):
                        row = self.rows[ident]
                        previous = dict(row)
                        self._complete(ident, item.get('tool', ''), item.get('arguments'), item.get('content_items'), item.get('success'))
                        # Legacy rollout only fills gaps; current run-owned facts remain authoritative.
                        if previous['parameters'] is not None:
                            row['parameters'], row['text'] = previous['parameters'], previous['text']
                        if previous['result'] is not None: row['result'] = previous['result']
                        if previous['status'] in ('returned', 'failed'): row['status'] = previous['status']
                    # New Codex image inputs are separate from the printable tool result.
                    event_key = (str(event.get('timestamp')), str(payload.get('id')))
                    if (payload.get('type') == 'message' and payload.get('role') == 'user'
                            and event_key not in self.image_messages):
                        target = None
                        matched_image = False
                        for block in payload.get('content') or []:
                            if not isinstance(block, dict): continue
                            text = block.get('text', '')
                            if isinstance(text, str) and text.startswith('（应用观测附件；属于本次工具结果，不是新任务）'):
                                try:
                                    target = json.loads(text.split('）', 1)[1]).get('call_id')
                                except ValueError:
                                    target = None
                            elif block.get('type') == 'input_image' and target in self.owned:
                                self.attachments[target] = self.attachments.get(target, 0)+1
                                matched_image = True
                                target = None
                        if matched_image:
                            if len(self.image_order) >= 4096:
                                self.image_messages.discard(self.image_order.popleft())
                            self.image_messages.add(event_key)
                            self.image_order.append(event_key)
            else:
                for event in self.claude.read():
                    message = mapping(event.get('message'))
                    content = message.get('content')
                    for block in content if isinstance(content, list) else []:
                        if not isinstance(block, dict): continue
                        if block.get('type') == 'tool_use':
                            ident = block.get('id')
                            if not isinstance(ident, str) or not ident: continue
                            row = self._row(ident, block.get('name', ''))
                            row['text'], row['parameters'] = request_summary(block.get('name', ''), block.get('input'))
                        elif block.get('type') == 'tool_result':
                            ident = block.get('tool_use_id')
                            if isinstance(ident, str) and ident in self.rows:
                                row = self.rows[ident]
                                row['result'] = result_summary(block.get('content'), not block.get('is_error', False))
                                row['status'] = 'failed' if block.get('is_error') else 'returned'
            retained = sorted(self.rows.values(), key=lambda row: (row['wall'], row.get('seq', 0), row['id']))[-240:]
            self.rows = {row['id']: row for row in retained}
            if (self.directory/'summary.json').exists():
                for row in retained:
                    if row['status'] == 'running': row['status'] = 'unknown'
            self.attachments = {key: value for key, value in self.attachments.items() if key in self.rows}
            selected = retained[-max(1, min(int(limit), 80)):]
            items = []
            for row in selected:
                item = dict(row)
                delivery = self.delivery_states.get(row['id'], {})
                item['attached_images'] = delivery.get('images', self.attachments.get(row['id']))
                if delivery.get('status'): item['attachment_status'] = delivery['status']
                items.append(item)
            tails = [self.timing, self.claude, self.app, self.deliveries] + ([self.native] if self.native else [])
            skipped = sum(t.skipped for t in tails)
            return {'items': items, 'loading': any(t.loading for t in tails), 'warning': self.warning,
                    'skipped_lines': skipped, 'scope': '仅本次运行最近记录；最多保留240条、展示50条。参数不证明模型已理解，Bash内部文件访问未细分。'}


_readers, _lock = {}, threading.Lock()


def read_tools(directory, limit=50):
    if directory is None:
        return {'items': [], 'warning': '尚无运行记录'}
    key = Path(directory).resolve()
    with _lock:
        if key not in _readers:
            if len(_readers) >= 6: _readers.pop(next(iter(_readers)))
            _readers[key] = ToolHistory(key)
        reader = _readers[key]
    return reader.snapshot(limit)
