"""Read-only public HUD transcript and metered-simulation session exports."""
from __future__ import annotations

import csv
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import zipfile

TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
DEFAULT_TIME_FORMAT = '%m-%d %H:%M:%S'
TIME_FIELDS = ('elapsed_s', 'thinking_s', 'tool_s', 'play_game_s', 'auto_game_s',
               'study_s', 'compact_s', 'replay_s', 'waited_s', 'net_wait_s')
TIME_LABELS = ('累计有效现实时间', '思考', '工具', '游戏操作墙钟耗时', '自动推进墙钟耗时',
               '知识库准备', '上下文压缩', '规划回放', '额度等待', '网络等待')


def timestamp(wall):
    if isinstance(wall, (int, float)) and not isinstance(wall, bool) and math.isfinite(wall) and wall > 0:
        try:
            return datetime.fromtimestamp(wall, TZ).isoformat(timespec='milliseconds')
        except (OSError, ValueError, OverflowError):
            pass
    return None


def read_object(path, warnings):
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        warnings.append(f'{path}: {exc}')
        return {}


def rows(path, warnings):
    """Read only complete lines within the file length captured at open."""
    if not path.exists():
        return
    with path.open('rb') as stream:
        remaining = stream.seek(0, 2)
        stream.seek(0)
        while remaining:
            raw = stream.readline(min(remaining, 4 * 1024 * 1024 + 1))
            if not raw:
                warnings.append(f'{path}: 读取期间文件已截短')
                break
            remaining -= len(raw)
            if len(raw) > 4 * 1024 * 1024:
                while remaining and not raw.endswith(b'\n'):
                    raw = stream.readline(min(remaining, 4 * 1024 * 1024))
                    if not raw:
                        warnings.append(f'{path}: 读取期间文件已截短')
                        remaining = 0
                        break
                    remaining -= len(raw)
                warnings.append(f'{path}: 跳过超长行')
                continue
            if not raw.endswith(b'\n'):
                warnings.append(f'{path}: 忽略未完成尾行')
                continue
            try:
                value = json.loads(raw)
                if isinstance(value, dict):
                    yield value
            except (ValueError, UnicodeError):
                warnings.append(f'{path}: 跳过无效 JSON 行')


def identity(state):
    engine = state.get('engine')
    return engine, state.get('provider') or ('codex' if engine == 'codex' else 'anthropic'), state.get('session_id')


def message_time_bound(value):
    """Parse an ISO date/time; a missing UTC offset means Beijing time."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('公开消息时间范围须使用 ISO 日期时间，例如 2026-11-03T23:02:46+08:00')
    try:
        bound = datetime.fromisoformat(value.strip())
        return bound.replace(tzinfo=TZ) if bound.tzinfo is None else bound.astimezone(TZ)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f'无效的公开消息时间范围：{value!r}；请使用 ISO 日期时间') from exc


def collect_session(run, *, runs_root=None, native_reader=None, since=None, until=None):
    """No game/model connection, writes or UI history limits; native_reader is an optional read boundary."""
    since, until = message_time_bound(since), message_time_bound(until)
    if since is not None and until is not None and since >= until:
        raise ValueError('公开消息时间范围的起点必须早于终点')
    since_wall = since.timestamp() if since is not None else None
    until_wall = until.timestamp() if until is not None else None
    run = Path(run).resolve()
    warnings = []
    selected = read_object(run / 'run_state.json', warnings)
    key = identity(selected)
    if key[0] not in ('codex', 'claude') or not isinstance(key[2], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', key[2]):
        raise ValueError('运行目录没有可识别的执行器与会话 ID')
    root = Path(runs_root).resolve() if runs_root else next((p for p in run.parents if p.name == 'runs'), run.parent.parent)
    candidates = {run / 'run_state.json', *root.glob('*/*/run_state.json'), *root.glob('*/*/chats/*/run_state.json')}
    chain = []
    for path in sorted(candidates):
        state = read_object(path, warnings)
        if identity(state) == key:
            chain.append((path.parent, state))
    snapshots = {}
    for directory, _ in chain:
        # The HUD consumes metrics, including post-play chat; summary is only a fallback.
        path = directory / 'metrics.json'
        data = read_object(path, warnings) if path.exists() else {}
        if not data:
            path = directory / 'summary.json'
            data = read_object(path, warnings)
        snapshots[directory] = path, data
    def snapshot_wall(pair):
        path, data = snapshots[pair[0]]
        wall = data.get('written_wall')
        return wall if timestamp(wall) else path.stat().st_mtime if path.exists() else 0

    chain.sort(key=lambda pair: (snapshot_wall(pair), str(pair[0])))
    texts, seen, segments, receipts, native_calls, timing, source_files = [], set(), [], {}, {}, {}, []

    def text_event(directory, event, source):
        text = event.get('text')
        if not isinstance(text, str) or not text.strip():
            return
        ident = str(event.get('id', ''))
        # Native public message IDs survive resume; local user/action IDs do not.
        scope = 'native' if ident.startswith('public:') else str(directory)
        dedup = (scope, ident, text)
        if dedup in seen:
            return
        seen.add(dedup)
        wall = event.get('wall')
        texts.append({'id': ident, 'run': str(directory), 'role': event.get('role', 'assistant'),
                      'kind': event.get('kind', '解说'), 'text': text, 'wall': wall if timestamp(wall) else None,
                      'time': timestamp(wall), 'source': source})

    for directory, state in chain:
        metrics_file, metrics = snapshots[directory]
        segments.append({'run': str(directory), 'save': state.get('save'), 'model': state.get('model'),
                         'snapshot_source': str(metrics_file), 'metrics': metrics})
        for name in ('run_state.json', 'public_events.jsonl', 'decisions.jsonl', 'control.sqlite3',
                     'sim_energy.json', 'decision_timing.jsonl', metrics_file.name):
            path = directory / name
            if path.exists():
                source_files.append({'path': str(path), 'size_at_capture': path.stat().st_size})
        events = list(rows(directory / 'public_events.jsonl', warnings))
        for event in events:
            text_event(directory, event, str(directory / 'public_events.jsonl'))
        public_ids = {event.get('id') for event in events}
        for decision in rows(directory / 'decisions.jsonl', warnings):
            ident = f"act:{decision.get('i')}:{decision.get('attempt', 0)}"
            if ident not in public_ids:
                began = metrics.get('started_wall')
                wall = began + decision.get('t', 0) if isinstance(began, (int, float)) and began > 0 else None
                text_event(directory, {'id': ident, 'text': decision.get('thought'), 'wall': wall,
                                      'kind': '正式思考' if metrics.get('formal_only') else '尝试'}, str(directory / 'decisions.jsonl'))
        # Older runs may predate public_events; never export user inputs/tool bodies from protocol traces.
        if not (directory / 'public_events.jsonl').exists():
            for event in rows(directory / 'codex_events.jsonl', warnings):
                params = event.get('params') or {}
                item = params.get('item') or {}
                if event.get('method') == 'item/completed' and item.get('type') == 'agentMessage':
                    text_event(directory, {'id': 'public:' + str(item.get('id')), 'text': item.get('text'),
                                          'wall': (params.get('completedAtMs') or 0) / 1000}, str(directory / 'codex_events.jsonl'))
            if key[0] == 'claude':
                for event in rows(directory / 'agent_events.jsonl', warnings):
                    message = event.get('message') or {}
                    if event.get('type') == 'assistant':
                        for index, block in enumerate(message.get('content') or []):
                            if isinstance(block, dict) and block.get('type') == 'text':
                                text_event(directory, {'id': 'claude:' + str(message.get('id')) + ':' + str(index),
                                                      'text': block.get('text'), 'wall': event.get('wall')}, str(directory / 'agent_events.jsonl'))
        db = directory / 'control.sqlite3'
        if db.exists():
            try:
                with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as connection:
                    connection.row_factory = sqlite3.Row
                    connection.execute('PRAGMA query_only=ON')
                    connection.execute('BEGIN')
                    for row in connection.execute("SELECT * FROM actions WHERE kind='message' ORDER BY id"):
                        delivery = {'queued': '等待发送', 'waiting': '等待提交', 'submitted': '已提交', 'error': '发送失败'}.get(row['status'], '')
                        text_event(directory, {**dict(row), 'id': 'user:' + str(row['id']), 'role': 'user',
                                              'kind': '你 · ' + delivery}, str(db) + '#actions')
                    for row in connection.execute('SELECT * FROM replies ORDER BY wall'):
                        text_event(directory, {**dict(row), 'id': 'public:' + str(row['id'])}, str(db) + '#replies')
            except sqlite3.Error as exc:
                warnings.append(f'{db}: {exc}')
        wallet = read_object(directory / 'sim_energy.json', warnings) if (directory / 'sim_energy.json').exists() else {}
        for receipt in wallet.get('receipts', []):
            if not isinstance(receipt, dict) or not isinstance(receipt.get('id'), str):
                continue
            ident = receipt['id']
            if type(receipt.get('cost')) is not int or receipt['cost'] < 0 or receipt.get('status') not in ('used', 'refunded', 'reserved'):
                warnings.append(f'{directory}: 跳过无效扣费回执 {ident}')
                continue
            if ident in receipts and receipts[ident].get('cost') != receipt.get('cost'):
                warnings.append(f'回执 {ident} 在不同运行记录中成本冲突')
            receipts[ident] = {**receipt, 'ledger_source': str(directory / 'sim_energy.json')}
        for event in rows(directory / 'decision_timing.jsonl', warnings):
            ident = event.get('tool_id')
            if not isinstance(ident, str):
                continue
            row = timing.setdefault(ident, {'run': str(directory)})
            if event.get('event') == 'tool_requested':
                row.update(wall=event.get('wall'), tool=event.get('tool'))
            elif event.get('event') == 'tool_completed':
                row['duration_s'] = event.get('tool_active_s')
        # Claude has full tool results locally. Only simulate's structured fields are retained.
        if key[0] == 'claude':
            requested = {}
            for event in rows(directory / 'agent_events.jsonl', warnings):
                for block in (event.get('message') or {}).get('content') or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use' and str(block.get('name', '')).endswith('simulate'):
                        requested[block.get('id')] = block.get('input', {})
                    elif block.get('type') == 'tool_result' and block.get('tool_use_id') in requested:
                        ident = block['tool_use_id']
                        native_calls[ident] = simulation_fact(ident, requested[ident], block.get('content'), not block.get('is_error', False))
    if native_reader is not None and key[0] == 'codex':
        try:
            native = native_reader(selected)
            for row in native.get('calls', []):
                native_calls[row['call_id']] = row
            warnings.extend(native.get('warnings', []))
            source_files.extend(native.get('sources', []))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            warnings.append(f'模拟调用详情读取不可用：{exc}')
    simulations, matched = [], set()
    for ident, call in native_calls.items():
        detail = timing.get(ident, {})
        wall = detail.get('wall') or call.get('wall')
        row = {**call, 'run': detail.get('run'), 'wall': wall, 'time': timestamp(wall),
               'duration_s': detail.get('duration_s', call.get('duration_s'))}
        receipt = receipts.get(call.get('receipt'))
        if receipt:
            matched.add(receipt['id'])
            if call.get('cost') != receipt.get('cost'):
                warnings.append(f"回执 {receipt['id']} 的工具返回成本与账本不一致")
            row.update(ledger_status=receipt.get('status'), ledger_cost=receipt.get('cost'), ledger_source=receipt['ledger_source'])
        elif call.get('receipt'):
            warnings.append(f"模拟工具返回回执 {call['receipt']} 未在账本中找到")
        simulations.append(row)
    for ident, receipt in receipts.items():
        if ident not in matched:
            simulations.append({'receipt': ident, 'ledger_cost': receipt.get('cost'), 'ledger_status': receipt.get('status'),
                                'ledger_source': receipt['ledger_source'], 'time': None, 'detail_missing': True})
    represented = set(native_calls)
    for ident, detail in timing.items():
        if str(detail.get('tool', '')).split(' · ')[0] in ('celeste_simulate', 'mcp__celeste__simulate') and ident not in represented:
            simulations.append({'call_id': ident, **detail, 'time': timestamp(detail.get('wall')), 'detail_missing': True})
    simulations.sort(key=lambda row: (row.get('wall') is None, row.get('wall') or 0, row.get('receipt', '')))
    text_filter = None
    if since is not None or until is not None:
        # Canonical events must win over later reply copies even at a range boundary.
        unknown = sum(row['wall'] is None for row in texts)
        texts = [row for row in texts if row['wall'] is not None
                 and (since_wall is None or row['wall'] >= since_wall)
                 and (until_wall is None or row['wall'] < until_wall)]
        text_filter = {'since': since.isoformat() if since is not None else None,
                       'until': until.isoformat() if until is not None else None,
                       'bounds': '[since, until)', 'excluded_unknown_time': unknown,
                       'statistics_scope': 'entire_session'}
        if unknown:
            warnings.append(f'时间筛选排除 {unknown} 条缺少有效时间的公开消息；无法判断是否位于指定范围内')
    texts.sort(key=lambda row: (row['wall'] is None, row['wall'] or 0, row['id']))
    latest = segments[-1]['metrics']
    totals = {status: sum(r.get('cost', 0) for r in receipts.values() if r.get('status') == status) for status in ('used', 'reserved', 'refunded')}
    balance = (latest.get('sim_energy') or {}).get('balance')
    if receipts and len(matched) < len(receipts):
        warnings.append(f'{len(receipts) - len(matched)} 条扣费回执没有可关联的调用详情；未知时间/输入保持为空，不按顺序猜配')
    return {'schema_version': 1, 'exported_at': datetime.now(TZ).isoformat(), 'timezone': 'Asia/Shanghai',
            'scope': '网页 HUD 对话栏公开文本、主控累计时间及模拟器调用/额度；不含工具结果正文、推理或截图文字识别。',
            'session': {'engine': key[0], 'provider': key[1], 'session_id': key[2], 'agent': selected.get('agent'), 'save': selected.get('save')},
            'runs': [str(directory) for directory, _ in chain], 'sources': source_files,
            'time_summary': {**{k: latest.get(k) for k in TIME_FIELDS}, 'started_at': timestamp(latest.get('started_wall')),
                             'snapshot_at': timestamp(latest.get('written_wall')), 'game_clock': latest.get('game_clock'),
                             'chapter_time_ticks': latest.get('chapter_time_ticks'), 'completed_ticks': latest.get('completed_ticks'),
                             'splits': latest.get('splits'), 'source': segments[-1]['snapshot_source']},
            'segments': [{k: v for k, v in segment.items() if k != 'metrics'} | {'cumulative_seconds': {k: segment['metrics'].get(k) for k in TIME_FIELDS}} for segment in segments],
            'public_text': texts, 'public_text_filter': text_filter, 'simulations': simulations,
            'simulation_summary': {'receipt_count': len(receipts), 'matched_receipts': len(matched), 'net_spent': totals['used'],
                                   'reserved': totals['reserved'], 'refunded': totals['refunded'], 'final_balance': balance,
                                   'observed_calls': len(native_calls), 'failed_calls': sum(call.get('success') is False for call in native_calls.values()),
                                   'balance_queries': sum(call.get('query_only') is True for call in native_calls.values()),
                                   'successful_predictions': latest.get('sim_calls'), 'predicted_routes': latest.get('sim_rollouts')},
            'warnings': list(dict.fromkeys(warnings))}


def simulation_fact(ident, args, content, success, **metadata):
    args = args if isinstance(args, dict) else {}
    texts = [content] if isinstance(content, str) else [b['text'] for b in (content or []) if isinstance(b, dict) and isinstance(b.get('text'), str)]
    data = {}
    for text in texts:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                data = obj
                break
        except ValueError:
            pass
    energy, info = data.get('energy') or {}, data.get('info') or {}
    query = args.get('candidates') is None
    cost = data.get('cost', 0 if query and success is True else None)
    balance = energy.get('balance', data.get('balance'))
    before = balance + cost if type(balance) is int and type(cost) is int else None
    # Explicit projection: never include tool bodies, private state/trajectories or reasoning.
    return {'call_id': ident, **metadata, 'success': success, 'query_only': query,
            'candidates': args.get('candidates'), 'input_format': args.get('input_format', 'celeste_ai'),
            'receipt': data.get('receipt'), 'cost': cost, 'balance_after': balance, 'balance_before_derived': before,
            'capacity': energy.get('capacity', data.get('capacity')), 'rules': energy.get('rules', data.get('rules')),
            'frame': info.get('frame'), 'room': info.get('room'), 'chapter': info.get('chapter'),
            'results': [{'stop': result.get('stop'),
                         'frames': len(result['frames']) if isinstance(result.get('frames'), list) else result.get('frames')}
                        for result in data.get('results', []) if isinstance(result, dict)]}


def write_export(report, output, *, time_format=DEFAULT_TIME_FORMAT):
    """Write only new artifacts; refuse source-tree/previous-export overwrites."""
    if not isinstance(time_format, str) or not time_format:
        raise ValueError('时间格式不能为空')
    # Validate before creating artifacts; Python strftime owns the format syntax.
    datetime.now(TZ).strftime(time_format)

    def display_time(value):
        return datetime.fromisoformat(value).astimezone(TZ).strftime(time_format) if value else None

    output = Path(output).resolve()
    for run in report['runs']:
        if output == Path(run) or output in Path(run).parents or output.is_relative_to(Path(run)):
            raise ValueError('导出目录不能覆盖或位于源运行目录内')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'session.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8', newline='')
    for name, data in (('public_text.csv', report['public_text']), ('simulations.csv', report['simulations'])):
        fields = list(dict.fromkeys(k for row in data for k in row)) or ['time']
        with (output / name).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in data:
                safe = {k: display_time(v) if k == 'time' else json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()}
                writer.writerow({k: "'" + v if isinstance(v, str) and v.lstrip().startswith(('=', '+', '-', '@')) else v for k, v in safe.items()})
    summary = report['simulation_summary']
    lines = ['# 会话公开记录', '', f"会话：{report['session']['session_id']}；存档：{report['session']['save']}",
             f"导出时间：{display_time(report['exported_at'])}；所有时间为北京时间。",
             f'时间显示格式：`{time_format}`（Python strftime）；JSON 保留完整时间。', '', report['scope'], '']
    text_filter = report.get('public_text_filter')
    if text_filter:
        lines += [f"公开消息时间范围：{text_filter['since'] or '不限起点'} 至 {text_filter['until'] or '不限终点'}（含起点、不含终点）。",
                  f"排除时间未知消息 {text_filter['excluded_unknown_time']} 条。筛选仅作用于公开消息；时间统计、模拟调用与额度仍为整个会话。", '']
    lines += [f"公开文本 {len(report['public_text'])} 条；合并 {len(report['runs'])} 段运行。", '',
             f"已取得详情的模拟调用 {summary['observed_calls']} 次，其中失败 {summary['failed_calls']} 次，余额查询 {summary['balance_queries']} 次。", '',
             f"模拟成功预测 {summary['successful_predictions']} 次，{summary['predicted_routes']} 条路线；"
             f"账本净消耗 {summary['net_spent']}，退款 {summary['refunded']}，预留 {summary['reserved']}，末次余额 {summary['final_balance']}。", '',
             '## 时间统计', '', '取最后一段主控累计值；不将恢复前后的累计值相加。游戏时钟帧数除以 60；章节 ticks 除以 10000000，含义不同。', '']
    lines += [f"统计起点：{display_time(report['time_summary']['started_at']) or '未知'}；末次快照：{display_time(report['time_summary']['snapshot_at']) or '未知'}。", '']
    for key, label in zip(TIME_FIELDS, TIME_LABELS):
        value = report['time_summary'][key]
        lines.append(f'- {label}（{key}）：{round(value, 3) if isinstance(value, (int, float)) else "未知"} 秒')
    clock = report['time_summary']['game_clock'] or {}
    if isinstance(clock.get('frames'), (int, float)):
        lines.append(f"- HUD 主游戏时钟：{clock['frames']} 帧 / 60 = {clock['frames'] / 60:.3f} 秒")
    if report['warnings']:
        lines += ['', '## 来源缺口与校验提醒', ''] + ['- ' + warning for warning in report['warnings']]
    lines += ['', '## 全部公开文本', '']
    for row in report['public_text']:
        fence = '`' * max(3, max((len(m.group()) + 1 for m in re.finditer(r'`+', row['text'])), default=3))
        lines += [f"### {display_time(row['time']) or '时间未知'} · {row['kind']} · {row['role']}", '', fence + 'text', row['text'], fence, '']
    lines += ['## 模拟器调用', '', '完整候选输入和结果停止原因见 simulations.csv / session.json。扣费前余额仅在同次返回包含成本和余额时由两者相加还原；不推算跨调用奖励。', '',
              '| 时间 | 调用/回执 | 状态 | 扣费 | 返回余额 | 房间 | 耗时秒 |', '|---|---|---|---:|---:|---|---:|']
    for row in report['simulations']:
        state = row.get('ledger_status') or ('失败' if row.get('success') is False else '查询' if row.get('query_only') else '详情缺失')
        values = [display_time(row.get('time')) or '未知', row.get('call_id') or row.get('receipt') or '未知', state,
                  row.get('ledger_cost', row.get('cost')), row.get('balance_after'), row.get('room'), row.get('duration_s')]
        lines.append('| ' + ' | '.join(str(value) if value is not None else '未知' for value in values) + ' |')
    (output / 'README.md').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='')
    files = sorted(output.iterdir())
    manifest = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8', newline='')
    archive = output / 'export.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in [*files, output / 'manifest.json']:
            bundle.write(path, path.name)
    return archive
