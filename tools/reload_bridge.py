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


def owner_alive(base):
    """在用主控仍连着桥接时不能另开连接（桥接只接受一个连接，会顶掉主控）。"""
    try:
        state = (request(base).get('control') or {})
    except RuntimeError:
        return False
    return bool(state.get('connected'))


def abort_direct(port, run_dir, timeout):
    """原主控已经退出：直接连接常驻核心，只撤销可证明安全的未提交交接。"""
    from orchestrator.bridge_reload import reload_bridge
    from tools.bridge_client import BridgeClient
    client = BridgeClient(port=port, timeout=min(timeout, 10))
    try:
        session = type('DirectSession', (), {'bridge_call': staticmethod(lambda cmd, **args: client.call(cmd, **args))})()
        return reload_bridge(session, run_dir, {'abort': True, 'timeout_s': timeout})
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hud-port', type=int, default=47792)
    parser.add_argument('--dll', help='新的 CelesteAIBridge.dll 构建产物；准备成功后由在用主控备份并替换')
    parser.add_argument('--runtime-dll', help='与 --dll 一同构建的 CelesteAIRuntime.dll，只用于核对游戏里的核心是否相同'
                                              '（默认在 DLL 同目录或仓库 game-runtime/bin 查找）')
    parser.add_argument('--expected-version', help='要求新桥接的 major.minor.patch 版本')
    parser.add_argument('--timeout', type=float, default=120, help='重载最长等待秒数（1–300）')
    parser.add_argument('--retry', action='store_true', help='用上次失败后保留的 lease 接回，不重复原动作')
    parser.add_argument('--abort', action='store_true',
                        help='撤销尚未提交的重载交接（只在常驻核心确认原桥接仍在、游戏未变化时生效），并还原已替换的桥接文件')
    parser.add_argument('--direct', action='store_true',
                        help='与 --abort 一起使用：原主控已退出时直接连接常驻核心（主控仍在时拒绝，避免顶掉连接）')
    parser.add_argument('--bridge-port', type=int, default=47774, help='--direct 时常驻核心的 IPC 端口')
    parser.add_argument('--run-dir', help='--direct 时原主控的运行目录（含 bridge_reload.json），用来还原已替换的桥接文件')
    args = parser.parse_args()
    from orchestrator.bridge_reload import options
    try:
        if args.direct and not args.abort:
            raise ValueError('--direct 只用于 --abort')
        data = {'timeout_s': args.timeout, 'retry': args.retry}
        if args.abort:
            data['abort'] = True
        if args.dll:
            data['dll'] = args.dll
        if args.runtime_dll:
            data['runtime_dll'] = args.runtime_dll
        if args.expected_version:
            data['expected_version'] = args.expected_version
        data = options(data)
        base = f'http://127.0.0.1:{args.hud_port}'
        if args.direct:
            if owner_alive(base):
                raise RuntimeError('在用主控仍连接着桥接；请不加 --direct，经主控撤销，避免另开连接顶掉主控')
            run_dir = Path(args.run_dir).resolve() if args.run_dir else None
            if run_dir is None:
                print('未指定 --run-dir：若原主控已替换桥接文件，本次不会还原，下次启动游戏会载入替换后的文件', file=sys.stderr)
            result = abort_direct(args.bridge_port, run_dir, args.timeout)
            print(f'已撤销尚未提交的重载交接（lease {result.get("lease")}），原桥接继续运行；'
                  + (f'已还原 {len(result.get("restored") or [])} 个文件。' if result.get('restored') else '没有需要还原的文件。'))
            return 0
        current = request(base)
        state = current.get('control') or {}
        if not state.get('connected') or not state.get('bridge_reload_control'):
            raise RuntimeError('当前主控未连接或没有常驻桥接重载能力；首次安装须维护重启，未连接桥接或替换文件'
                               + ('。原主控已退出时可用 --abort --direct 撤销未提交的交接' if args.abort else ''))
        run = current.get('run')
        accepted = request(base, {'run': run, 'kind': 'bridge_reload', 'text': json.dumps(data, ensure_ascii=False)})
        ident = accepted.get('id')
        if ident is None:
            raise RuntimeError('主控没有返回重载指令编号')
        if args.abort:
            print(f'撤销请求已排入在用主控（指令 {ident}），等待当前动作结束。', flush=True)
        else:
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
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
