"""显式维护旧容器会话的模拟请求编号字段；不启动模型、游戏或录制。"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.config import REPO_ROOT
from orchestrator.codex_simulation_migration import migrate_run


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='先正常停止聊天/试玩宿主；持排他锁备份并定长迁移')
    args = parser.parse_args()
    output = REPO_ROOT / 'local' / ('simulate-schema-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    output.mkdir(parents=True)
    try:
        report = await migrate_run(args.run, output, apply=args.apply)
    except (OSError, ValueError, RuntimeError) as error:
        report = {'status': 'error', 'error': str(error)}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    print('报告：' + str(output / 'report.json'))
    return int(report['status'] not in {'current', 'updated'})


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
