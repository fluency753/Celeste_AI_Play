"""Read-only, run-owned tool facts. Never expose model reasoning or result bodies."""
import json
import os
from pathlib import Path
import re
import threading
import time
from collections import deque
import math

ROOT = Path(__file__).resolve().parents[1]


def mapping(value):
    return value if isinstance(value, dict) else {}


def safe_text(value, limit=700):
    text = str(value)
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{8,}', '[隐藏]', text)
    text = re.sub(r'[A-Za-z0-9+/=]{160,}', '[编码省略]', text)
    text = re.sub(r'''(?i)((?:api[_-]?key|access[_-]?token|password|authorization)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1[隐藏]', text)
    return text[:limit] + ('…' if len(text) > limit else '')


def request_summary(name, args):
    name = str(name or 'unknown').removeprefix('mcp__celeste__').removeprefix('celeste_')
    args = args if isinstance(args, dict) else {}
    allowed = ('detail', 'region', 'offset', 'limit', 'snapshot_id', 'path', 'file_path', 'plan_file',
               'action', 'choice', 'stop_on', 'input_format', 'timeout_s', 'description', 'command',
               'inputs', 'candidates', 'pattern', 'glob', 'include', 'output_mode', 'effort', 'reason')
    params = {key: args[key] for key in allowed if key in args}
    # Everything is display-only, with per-field and whole-record budgets.
    params = {key: safe_text(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
                             1200 if key == 'command' else 240) for key, value in params.items()}
    detail = args.get('detail', 'overview')
    observation = ({'overview': '完整房间布局', 'geometry': '区域碰撞精查', 'player': '完整玩家状态'}.get(detail, '观测（未知 detail）')
                   if isinstance(detail, str) else '观测（detail 参数异常）')
    label = {'observe': observation,
             'Read': '读取文件', 'read': '读取文件', 'Write': '写文件', 'write': '写文件',
             'Bash': '执行程序', 'bash': '执行程序', 'act': '游戏动作', 'simulate': '模拟预测',
             'cutscene': '剧情操作', 'narrate': '公开解说', 'set_effort': '调整推理强度',
             'stop_run': '报告阻塞', 'exit_level': '退出关卡'}.get(name, name)
    if name.lower() in {'read', 'write'}:
        label += ' · ' + safe_text(args.get('path') or args.get('file_path') or '路径未知', 150)
    if name == 'act':
        inputs = args.get('inputs')
        if isinstance(inputs, list):
            params['输入段数'] = len(inputs)
            if len(inputs) <= 2048 and all(isinstance(v, str) and re.fullmatch(r'[0-9]{1,12}', v.split(',')[0].strip()) for v in inputs):
                params['计划帧数'] = sum(int(v.split(',')[0]) for v in inputs)
    if name == 'simulate' and isinstance(args.get('candidates'), list):
        params['候选条数'] = len(args['candidates'])
    if name.lower() == 'write' and isinstance(args.get('content'), str):
        params['写入字符数'] = len(args['content'])
    return safe_text(label, 220), params


def result_summary(content, success):
    texts, images = [], 0
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') in {'text', 'inputText', 'input_text'}:
                if isinstance(block.get('text'), str): texts.append(block['text'])
            elif block.get('type') in {'image', 'inputImage', 'input_image'}:
                images += 1
    result = {'文字字符数': sum(len(t) for t in texts) if content is not None else None,
              '工具结果图片数': images if content is not None else None}
    for text in texts:
        if len(text) > 32000:
            continue
        candidate = text
        for marker in ('当前帧的统一碰撞数据（变化）：\n', '后续按快照返回变化）：\n'):
            if marker in text:
                candidate = text.split(marker, 1)[1]
        try:
            data = json.JSONDecoder().raw_decode(candidate)[0]
            if isinstance(data, dict):
                data = data['collision'] if isinstance(data.get('collision'), dict) else data
                for key, label in [('frame', '帧号'), ('room', '房间'), ('mode', '几何返回'),
                                   ('object_count', '对象数'), ('matched_total', '区域匹配数'),
                                   ('returned', '本页对象数'), ('returncode', '程序退出码'), ('timed_out', '程序超时')]:
                    if key in data and isinstance(data[key], (str, bool, int, float)):
                        result[label] = safe_text(data[key], 100)
        except (ValueError, TypeError):
            pass
    return result


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
        self.native = None
        self.rows, self.owned, self.attachments = {}, set(), {}
        self.owned_order = deque()
        self.image_messages, self.image_order = set(), deque()
        self.lock = threading.Lock()
        self.warning = ''
        self.session_id, self.locate_at = None, 0

    def _row(self, ident, name='', wall=0):
        if not isinstance(wall, (int, float)) or not math.isfinite(wall): wall = 0
        if ident not in self.rows:
            self.rows[ident] = {'id': ident, 'tool': name, 'text': safe_text(name), 'wall': wall,
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
                elif event.get('event') == 'tool_completed' and ident in self.owned:
                    row = self.rows.get(ident)
                    if row is None: continue  # Old, evicted calls cannot break current history.
                    if row['status'] == 'running': row['status'] = 'unknown'
                    row['duration_s'] = event.get('tool_active_s')
                elif event.get('event') == 'turn_ended':
                    for ident in event.get('pending_tools') or []:
                        if ident in self.rows and self.rows[ident]['status'] == 'running':
                            self.rows[ident]['status'] = 'interrupted'
            try:
                state = mapping(json.loads((self.directory/'run_state.json').read_text(encoding='utf-8-sig')))
            except (OSError, ValueError):
                state = {}
            if state.get('engine') == 'codex':
                self._locate(state)
                for event in self.native.read() if self.native and not self.timing.loading else []:
                    payload = mapping(event.get('payload'))
                    item = mapping(payload.get('item'))
                    ident = item.get('id')
                    if (payload.get('type') == 'item_completed' and item.get('type') == 'DynamicToolCall'
                            and ident in self.owned and ident in self.rows):
                        self._complete(ident, item.get('tool', ''), item.get('arguments'), item.get('content_items'), item.get('success'))
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
                            if ident in self.rows:
                                row = self.rows[ident]
                                row['result'] = result_summary(block.get('content'), not block.get('is_error', False))
                                row['status'] = 'failed' if block.get('is_error') else 'returned'
            retained = sorted(self.rows.values(), key=lambda row: (row['wall'], row['id']))[-240:]
            self.rows = {row['id']: row for row in retained}
            if (self.directory/'summary.json').exists():
                for row in retained:
                    if row['status'] == 'running': row['status'] = 'unknown'
            self.attachments = {key: value for key, value in self.attachments.items() if key in self.rows}
            selected = retained[-max(1, min(int(limit), 80)):]
            items = []
            for row in selected:
                item = dict(row)
                item['attached_images'] = self.attachments.get(row['id'])
                items.append(item)
            tails = [self.timing, self.claude] + ([self.native] if self.native else [])
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
