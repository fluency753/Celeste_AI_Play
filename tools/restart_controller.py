"""检查或暂停重启主控，接续原会话与录制，不连接桥接、不恢复试玩。"""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'restart'])
    parser.add_argument('--hud-port', type=int, default=47792)
    parser.add_argument('--host', help='仅旧主控无宿主记录时，指定其 local/background 目录')
    parser.add_argument('--bridge-port', type=int, help='仅旧主控无端口记录时指定')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--retry', action='store_true', help='接续已记录更新，不重复启动')
    parser.add_argument('--json', action='store_true', help='输出精简 JSON；完整交接另有持久记录')
    args = parser.parse_args()
    from orchestrator.controller_restart import inspect, restart
    try:
        if not 1 <= args.timeout <= 300:
            raise ValueError('timeout 必须为 1–300 秒')
        if args.action == 'check':
            if args.retry:
                raise ValueError('check 不接受 --retry')
            result = inspect(args.hud_port, host=args.host, bridge_port=args.bridge_port)
        else:
            result = restart(port=args.hud_port, host=args.host, bridge_port=args.bridge_port,
                             timeout=args.timeout, retry=args.retry)['after']
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"{'主控已接回' if args.action == 'restart' else '主控检查'}："
                  f"{'已连接' if result['connected'] else '未连接'} / {'暂停' if result['paused'] else '运行'} / "
                  f"游戏{'冻结' if result['bridge'].get('frozen') else '未确认冻结'} / Bridge {result['bridge'].get('version', '未知')}")
            print(f"会话 {result['session']['session_id']}；原录制 "
                  + ((result['recording'] or {}).get('id') or '无录制'))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        print(f'主控更新未完成：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
