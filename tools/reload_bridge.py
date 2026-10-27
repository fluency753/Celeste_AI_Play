"""通过在用主控受控重载桥接逻辑，保留原录像；首次安装常驻核心仍须重启游戏。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def request(base, payload=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(base + '/api/control', data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    try:
        with opener.open(req, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError('主控拒绝重载请求：' + body) from exc
    except OSError as exc:
        raise RuntimeError('无法访问在用主控的 HUD 服务：' + str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hud-port', type=int, default=47792)
    parser.add_argument('--dll', help='新的 CelesteAIBridge.dll 构建产物；准备成功后由在用主控备份并替换')
    parser.add_argument('--expected-version', help='要求新桥接的 major.minor.patch 版本')
    parser.add_argument('--timeout', type=float, default=120, help='重载最长等待秒数（1–300）')
    parser.add_argument('--retry', action='store_true', help='用上次失败后保留的 lease 接回，不重复原动作')
    args = parser.parse_args()
    from orchestrator.bridge_reload import options
    try:
        data = {'timeout_s': args.timeout, 'retry': args.retry}
        if args.dll:
            data['dll'] = args.dll
        if args.expected_version:
            data['expected_version'] = args.expected_version
        data = options(data)
        base = f'http://127.0.0.1:{args.hud_port}'
        current = request(base)
        state = current.get('control') or {}
        if not state.get('connected') or not state.get('bridge_reload_control'):
            raise RuntimeError('当前主控未连接或没有常驻桥接重载能力；首次安装须维护重启，未连接桥接或替换文件')
        run = current.get('run')
        accepted = request(base, {'run': run, 'kind': 'bridge_reload', 'text': json.dumps(data, ensure_ascii=False)})
        ident = accepted.get('id')
        if ident is None:
            raise RuntimeError('主控没有返回重载指令编号')
        print(f'桥接重载已排入在用主控（指令 {ident}），等待安全动作边界；不停止录像。', flush=True)
        deadline = time.monotonic() + args.timeout + 15
        while time.monotonic() < deadline:
            current = request(base)
            if current.get('run') != run:
                raise RuntimeError('当前运行已切换，停止等待；不操作其他会话')
            action = next((row for row in (current.get('control') or {}).get('actions', []) if row.get('id') == ident), None)
            if action and action.get('status') in {'submitted', 'error'}:
                print(action.get('detail') or action['status'], file=sys.stderr if action['status'] == 'error' else sys.stdout)
                return 2 if action['status'] == 'error' else 0
            time.sleep(.25)
        raise RuntimeError('等待重载结果超时；请检查控制台，原交接不会被本命令取消或重复执行')
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
