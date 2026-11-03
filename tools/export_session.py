"""导出一个会话跨运行的 HUD 公开文本、累计时间和模拟器额度明细。"""
import argparse
from datetime import datetime
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.session_export import TZ, DEFAULT_TIME_FORMAT, collect_session, write_export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path, help='同一会话的任一运行目录')
    parser.add_argument('--output', type=Path, help='新的输出目录，默认 local/exports/<会话ID>-<时间>')
    parser.add_argument('--local-only', action='store_true', help='只读本机日志，不访问已有 Codex 容器')
    parser.add_argument('--codex-container', help='已有 Codex 容器名称；默认按工作区标签推导，只读并核验归属')
    parser.add_argument('--time-format', default=DEFAULT_TIME_FORMAT, help='Markdown/CSV 时间显示格式，使用 Python strftime 语法（默认：%(default)s）')
    args = parser.parse_args()
    if args.local_only and args.codex_container:
        parser.error('--local-only 与 --codex-container 不能同时使用')
    try:
        reader = None
        if not args.local_only:
            from orchestrator.session_export_native import read_container_simulations
            reader = lambda state: read_container_simulations(state, args.codex_container)
        report = collect_session(args.run, native_reader=reader)
        output = args.output or Path(__file__).resolve().parents[1] / 'local' / 'exports' / (
            report['session']['session_id'] + '-' + datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f'))
        archive = write_export(report, output, time_format=args.time_format)
        summary = report['simulation_summary']
        print(f'已导出：{archive.parent}')
        print(f"公开文本 {len(report['public_text'])} 条；{len(report['runs'])} 段运行；"
              f"扣费回执 {summary['receipt_count']} 条；净消耗 {summary['net_spent']}；余额 {summary['final_balance']}")
        for warning in report['warnings']:
            print('提示：' + warning)
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f'导出失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
