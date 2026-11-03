"""Structured analysis artifacts and a compact, source-qualified landing page."""
import csv
import json
from pathlib import Path


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
    if report.get('workspace') is not None:
        (output / 'workspace.json').write_text(json.dumps(report['workspace'], ensure_ascii=False, indent=2), encoding='utf-8')
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
    lines += ['', '## 数据缺口与提醒', '']
    lines += ['- ' + text for text in coverage['limitations']]
    warnings = list(dict.fromkeys(report['warnings'] + coverage['source_warnings']))
    lines += ['- ' + text for text in warnings[:30]]
    if len(warnings) > 30:
        lines.append('- 更多来源警告见 session.json。')
    lines += ['', '导出仅读取已保存日志与已核验容器中的原生日志；不调用游戏、模型或模拟器，不导出私有推理或图片/音频正文。', '']
    return lines
