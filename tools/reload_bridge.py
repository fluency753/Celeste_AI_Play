"""经在用主控或空闲维护连接重载桥接；Runtime 相同时不重启游戏。"""
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


def abort_direct(port, run_dir, timeout, game_dir=None):
    """原主控已经退出：直接连接常驻核心，只撤销可证明安全的未提交交接。"""
    from orchestrator.bridge_reload import reload_bridge
    from tools.bridge_client import BridgeClient
    import os
    from contextlib import nullcontext
    from orchestrator.game_process import controller_lease, idle_bridge_listener
    with controller_lease() if os.name == 'nt' else nullcontext():
        if os.name == 'nt':
            idle_bridge_listener(port)
        client = BridgeClient(port=port, timeout=min(timeout, 10))
        try:
            from orchestrator.idle_bridge_reload import MaintenanceConnection
            session = MaintenanceConnection(client)
            if game_dir:
                state = session.bridge_call('reload_status')
                state = state.get('reload', state)
                target = state.get('runtime_path') or state.get('payload_path')
                if not target or len(Path(target).resolve().parents) < 4 or Path(target).resolve().parents[3] != Path(game_dir).resolve():
                    raise RuntimeError('撤销目标游戏目录无法匹配；未还原文件或撤销交接')
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
    parser.add_argument('--timeout', type=float, help='重载最长等待秒数（1–300）；新请求默认120，加载等待重试沿用原值')
    parser.add_argument('--retry', action='store_true', help='用上次失败后保留的 lease 接回，不重复原动作')
    parser.add_argument('--abort', action='store_true',
                        help='撤销尚未提交的重载交接（只在常驻核心确认原桥接仍在、游戏未变化时生效），并还原已替换的桥接文件')
    parser.add_argument('--direct', action='store_true',
                        help='主控未运行时进行空闲维护（也支持 retry/abort）；先核对端口占用，不启动 Player')
    parser.add_argument('--bridge-port', type=int, default=47774, help='--direct 时常驻核心的 IPC 端口')
    parser.add_argument('--run-dir', help='--direct 时原主控的运行目录（含 bridge_reload.json），用来还原已替换的桥接文件')
    parser.add_argument('--game-dir', help='核对部署目标游戏目录；提供此值时，主控未连接可自动转为空闲维护')
    parser.add_argument('--save-slot', type=int, help='空闲维护时核对已选择的存档槽；主菜单没有选档时不虚构槽位')
    args = parser.parse_args()
    from orchestrator.bridge_reload import options
    try:
        data = {'retry': args.retry}
        if args.timeout is not None:
            data['timeout_s'] = args.timeout
        if args.abort:
            data['abort'] = True
        if args.dll:
            data['dll'] = args.dll
        if args.runtime_dll:
            data['runtime_dll'] = args.runtime_dll
        if args.expected_version:
            data['expected_version'] = args.expected_version
        if args.game_dir:
            data['game_dir'] = args.game_dir
        data = options(data, fill_defaults=False)
        if args.save_slot is not None and args.save_slot < 0:
            raise ValueError('save-slot 须为非负整数')
        base = f'http://127.0.0.1:{args.hud_port}'
        current = None
        if not args.direct:
            try:
                current = request(base)
            except RuntimeError:
                if not args.game_dir:
                    raise
        idle = args.direct or (args.game_dir and not (current or {}).get('control', {}).get('connected'))
        if idle:
            if owner_alive(base):
                raise RuntimeError('在用主控仍连接着桥接；请经主控更新，不另开维护连接')
            from orchestrator.idle_bridge_reload import maintenance_directory, reload_idle
            run_dir = Path(args.run_dir).resolve() if args.run_dir else None
            maintenance_dir = run_dir or maintenance_directory(args.bridge_port)
            metadata_path = maintenance_dir / 'idle_maintenance.json'
            from orchestrator.session_store import load_json
            metadata = load_json(metadata_path) if metadata_path.exists() else {}
            known_idle = bool(metadata) and (run_dir is not None or metadata.get('phase') != 'returned')
            if args.abort and not known_idle:
                result = abort_direct(args.bridge_port, run_dir, args.timeout if args.timeout is not None else 120, args.game_dir)
                print(f'已撤销尚未提交的重载交接（lease {result.get("lease")}），未代替原主控归还游戏控制权。')
            else:
                if args.retry and run_dir is None and metadata.get('phase') == 'returned':
                    raise RuntimeError('默认目录只保留了已完成维护；请用 --run-dir 明确要重试的交接，未连接游戏')
                result = reload_idle(args.bridge_port, data, directory=maintenance_dir, game_dir=args.game_dir,
                                     save_slot=args.save_slot, progress=lambda message: print(message, flush=True))
                if result.get('already_returned'):
                    print('指定记录中的维护此前已完成；未连接游戏或处理其他交接。')
                else:
                    print(f'空闲维护完成（{result.get("phase")}）；控制权已归还，未启动 Player 或重启游戏。')
            print('维护记录：' + str(maintenance_dir))
            return 0
        state = current.get('control') or {}
        if not state.get('connected') or not state.get('bridge_reload_control'):
            raise RuntimeError('当前主控未连接或没有常驻桥接重载能力；首次安装须维护重启，未连接桥接或替换文件'
                               + ('。原主控已退出时可用 --abort --direct 撤销未提交的交接' if args.abort else ''))
        run = current.get('run')
        if args.game_dir:
            from orchestrator.session_store import load_json
            run_state = load_json(Path(run) / 'run_state.json')
            if run_state.get('bridge_port') != args.bridge_port:
                raise RuntimeError('在用主控的桥接端口与部署目标不同；未提交重载')
        accepted = request(base, {'run': run, 'kind': 'bridge_reload', 'text': json.dumps(data, ensure_ascii=False)})
        ident = accepted.get('id')
        if ident is None:
            raise RuntimeError('主控没有返回重载指令编号')
        if args.abort:
            print(f'撤销请求已排入在用主控（指令 {ident}），等待当前动作结束。', flush=True)
        else:
            print(f'桥接重载已排入在用主控（指令 {ident}），等待安全动作边界；不停止录像。', flush=True)
        deadline = time.monotonic() + (args.timeout if args.timeout is not None else 300) + 15
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
