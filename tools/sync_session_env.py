"""检查/同步旧会话环境；不启动试玩、不发送消息、不连接游戏或 OBS。"""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.config import REPO_ROOT
from orchestrator.session_environment import synchronize, targets


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', action='append', help='runs 下的运行目录，可多次指定')
    group.add_argument('--all', action='store_true', help='本仓库全部已登记会话，按会话身份去重')
    parser.add_argument('--apply', action='store_true', help='备份后同步工具登记，并校验/更新容器；默认仅检查')
    args = parser.parse_args()
    output = REPO_ROOT/'local'/('session-env-sync-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    output.mkdir(parents=True)
    reports = []
    for directory, state in targets(args.run):
        try:
            result = await synchronize(directory, state, output, apply=args.apply)
        except Exception as exc:
            result = {'run': str(directory), 'session_id': state['session_id'], 'status': 'error', 'error': str(exc)}
        reports.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    (output/'report.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    print('报告：'+str(output/'report.json'))
    return int(any(r['status'] in {'error', 'outdated'} for r in reports))


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
