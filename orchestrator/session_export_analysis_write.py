"""Structured analysis artifacts and a compact, source-qualified landing page."""
import csv
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path


def _write_workspace(workspace, output):
    (output / 'workspace.json').write_text(json.dumps(workspace, ensure_ascii=False, indent=2), encoding='utf-8')
    note = next((doc for doc in workspace['memory_documents'] if doc['path'] == 'notes.md'), None)
    if note is not None:
        (output / 'notes.md').write_text(note['text'], encoding='utf-8', newline='')
    lines = ['# Git 变更摘要', '', '## 工作区根目录', '', *_git_lines(workspace['git'], level=3)]
    for repository in workspace.get('git_repositories', []):
        if repository['path'] != '.':
            label = escape(repository['path'])
            lines.extend(['', f'## 子目录仓库 <code>{label}</code>', '', *_git_lines(repository, level=3)])
    (output / 'git_changes.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return note


def _git_lines(git, *, level):

    def utc(wall):
        if wall is None:
            return '未指定'
        try:
            return datetime.fromtimestamp(wall, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return f'{wall}（Unix 秒）'

    def code(value):
        return '<code>' + escape(json.dumps(value, ensure_ascii=False)[1:-1]).replace(' ', '&#32;') + '</code>'

    lines = [f"会话时间窗 {utc(git['since_wall'])} 至 {utc(git['until_wall'])}（UTC，左闭右开）。",
             f"导出时 HEAD {code(git['head'] or '未知')}，引用 {code(git['ref'] or '未知或分离 HEAD')}。"]

    def section(title, field, description, render):
        lines.extend(['', '#' * level + ' ' + title, '', description, ''])
        rows = git[field]
        if rows is None:
            lines.append('不可用。')
        elif not rows:
            lines.append('无记录。')
        else:
            lines.extend('- ' + render(row) for row in rows)
            if git[field + '_truncated']:
                lines.append('- 已达到记录条数上限，详见 SCHEMA.md。')

    section('当前 HEAD 可达提交', 'commits', '按提交者时间筛选，包含当前 HEAD 能追溯到的提交。',
            lambda row: f"{utc(row['wall'])} {code(row['commit'])} {code(row['subject'])}")
    section('本地 HEAD 变动', 'head_events', '按本地 reflog 的记录时间筛选，可包含检出、重置和提交。',
            lambda row: f"{utc(row['wall'])} {code(row['old_head'])} → {code(row['new_head'])}")
    section('窗口端点间净提交路径变化', 'window_changes',
            f"起点前 HEAD {code(git['window_start_head'] or '未知')}，终点前 HEAD {code(git['window_end_head'] or '未知')}。",
            lambda row: f"{code(row['status'])} {code(row['path'])}")
    section('导出时工作树', 'worktree_status',
            f"采样时间 {utc(git['worktree_status_wall'])}。XY 两列分别为索引和工作树状态，?? 为未跟踪。该清单仅描述采样时状态。",
            lambda row: f"{code(row['status'])} {code(row['path'])}")
    warnings = list(dict.fromkeys(git['warnings'] + [git['status_unavailable_reason']]))
    if any(warnings):
        lines.extend(['', '#' * level + ' 数据缺口', ''] + ['- ' + code(warning) for warning in warnings if warning])
    return lines


def write_analysis(report, output):
    analysis = report['analysis']
    for name, records in report.analysis_records.items():
        with (output / (name + '.jsonl')).open('w', encoding='utf-8', newline='') as stream:
            for row in records:
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
    fields = ('sequence', 'tool_id', 'session_id', 'run_id', 'turn_id', 'native_turn_id', 'call_id', 'record_kind',
              'tool', 'requested_wall', 'result_wall', 'timing_ended_wall', 'active_s', 'runtime_duration_s',
              'wall_span_s', 'thinking_before_s', 'status', 'exit_code', 'last_observed_room',
              'window_membership', 'details_omitted')
    with (output / 'tool_calls.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in report.analysis_records['tool_calls']:
            values = {key: row.get(key) for key in fields}
            writer.writerow({key: "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value
                             for key, value in values.items()})
    workspace = report.get('workspace')
    note = _write_workspace(workspace, output) if workspace is not None else None
    # Ship the field contract with the archive, rather than requiring repository access.
    contract = Path(__file__).resolve().parents[1] / 'docs' / 'SESSION_ANALYSIS_SCHEMA.md'
    (output / 'SCHEMA.md').write_text(contract.read_text(encoding='utf-8'), encoding='utf-8')
    counts, coverage = analysis['counts'], analysis['coverage']
    lines = ['# Player Agent 行为分析导出', '',
             f"会话：{report['session']['session_id']}；运行段：{len(report['runs'])}；导出时间：{report['exported_at']}", '',
             f"公开文本 {len(report['public_text'])} 条；工具索引 {counts['tool_calls']} 条（含 code-mode 包装调用 {coverage['code_mode_wrappers']} 条）；"
             f"模型回合 {counts['turns']}；动作 {counts['actions']}；状态证据 {counts['game_states']}；时间线 {counts['timeline']} 条。", '',
             f"工具状态：{json.dumps(coverage['tool_statuses'], ensure_ascii=False)}。参数可用 {coverage['tools_with_parameters']} 条，"
             f"结果细节可用 {coverage['tools_with_result_details']} 条。", '',
             '## 数据入口', '',
             '- tool_calls.csv：精简统一索引；tool_calls.jsonl：参数、结果摘要、状态、来源与关联 ID。',
             '- timeline.jsonl / turns.jsonl：有时间依据的事件和回合；未知时间排在末尾并标记。',
             '- actions.jsonl / game_states.jsonl：动作输入、执行结果和已有观测证据；不额外观察游戏。',
             '- metric_snapshots.jsonl：主控现有累计快照；session.json.analysis：房间工作量、重复输入和长等待索引。',
             '- public_text.csv / public_text.md、simulations.csv、session.json：保留原始导出入口；SCHEMA.md：完整字段与计量说明。', '',
             '## 范围与计量', '',
             f"分析窗口：{analysis['scope']['since_wall'] if analysis['scope']['since_wall'] is not None else '不限'} 至 {analysis['scope']['until_wall'] if analysis['scope']['until_wall'] is not None else '不限'}（Unix 秒，左闭右开）。",
             '工具/回合保留相交区间的完整边界证据；未知时间记录保留并标记，不能视为窗口内。状态证据与累计快照保留整个会话，用于引用核查。',
             '旧文件的累计统计和模拟调用仍为整个会话；公开消息按 --since/--until 筛选。快照差仅为 resumed_from 两端比较，不能视为精确阶段统计。',
             'thinking_before_s 来自主控计量差；active_s 为暂停扣除后的单调用占用，可并发重叠。code-mode 包装调用与内部工具不能重复累计。',
             'tool_s 是主控累计工具区间扣除游戏/自动推进后的计量，不能用单工具耗时求和替代。压缩计入 thinking_s，其他重叠关系见 SCHEMA.md。', '',
             '## 最长的已计量工具前思考', '',
             '| 秒 | 最近观测房间 | 后续工具 | tool_id |', '|---:|---|---|---|']
    for row in analysis['longest_thinking_before_tools'][:10]:
        lines.append(f"| {row['thinking_before_s']:.3f} | {row['last_observed_room']} | {row['tool']} | {row['tool_id']} |")
    lines += ['', '## 最近观测房间的工作量', '',
              '仅按最近已记录观测分组，可能混合不同局部问题；旧时间未知记录另有窗口标记。', '',
              '| 房间 | 实际工具 | 动作 | 工具前已计量思考秒 |', '|---|---:|---:|---:|']
    for row in sorted(analysis['room_workload'], key=lambda item: -item['thinking_before_tools_s'])[:8]:
        lines.append(f"| {row['last_observed_room']} | {row['tools']} | {row['actions']} | {row['thinking_before_tools_s']:.3f} |")
    if analysis['snapshot_deltas']:
        delta = analysis['snapshot_deltas'][-1]
        values = {key: delta['counters'].get(key, {}).get('delta') for key in ('thinking_s', 'tool_s', 'play_game_s', 'compact_s', 'net_wait_s')}
        lines += ['', '## 最近恢复链的快照比较（非精确窗口统计）', '',
                  f"基准 {delta['base_snapshot_id']} → 末次 {delta['end_snapshot_id']}；游戏时钟变化 {delta['game_clock_seconds_delta']} 秒。",
                  '累计秒数变化：`' + json.dumps(values, ensure_ascii=False) + '`。前段快照可能受后续聊天影响，不能当作已证明的恢复基线。']
    if report.get('save_naming'):
        lines += ['', '## 存档取名', '',
                  '本会话有保存的取名结果，见 session.json.save_naming。包含请求 ID、名字与来源；原结果没有可靠时间，时间留空。',
                  '取名请求与公开回复一并收进 public_text.csv / public_text.md，按消息本身时间筛选。']
    if workspace is not None:
        lines += ['', '## 工作区附件', '',
                  '- workspace.json：当前文件清单、笔记投影及结构化 Git 摘要。',
                  '- git_changes.md：根目录及子目录独立仓库的 Git 记录与导出时工作树状态。']
        lines.append('- notes.md：根目录笔记的当前脱敏投影' + ('（已截断）。' if note['truncated'] else '。')
                     if note is not None else '- 根目录 notes.md 缺失或无法读取，未生成笔记附件。')
    lines += ['', '## 数据缺口与提醒', '']
    lines += ['- ' + text for text in coverage['limitations']]
    warnings = list(dict.fromkeys(report['warnings'] + coverage['source_warnings'] +
                                 (workspace.get('warnings', []) if workspace is not None else [])))
    lines += ['- ' + text for text in warnings[:30]]
    if len(warnings) > 30:
        lines.append('- 更多来源警告见 session.json。')
    lines += ['', '导出仅读取已保存日志与已核验容器中的原生日志；不调用游戏、模型或模拟器，不导出私有推理或图片/音频正文。', '']
    return lines
