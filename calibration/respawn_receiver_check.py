"""Real isolated Codex receiver and autonomous recovery; only the game socket is a fixture."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from calibration.story_wait_test import BridgeWire, RespawnBridge
from orchestrator.codex_agent import CodexRunner
from orchestrator.codex_rpc import CodexRPC
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.prompting import fill
from orchestrator.server import build_server
from tools.bridge_client import BridgeClient


async def check(model, directory):
    directory = directory.resolve()
    if not directory.is_relative_to(ROOT/'local'):
        raise ValueError('Evidence must be beneath ignored local/')
    directory.mkdir(parents=True, exist_ok=False)
    cfg = RunConfig(agent='respawn-receiver', save='test', model=model, engine='codex', runtime='host',
                    record=False, live=False, collision_image='off', cutscene_mode='agent', chapter=3)
    cfg.thought_lead_s = 0
    metrics = Metrics(directory/'metrics.json', cfg.agent, model, 'medium', 180)
    session = GameSession(cfg, directory, metrics)
    session.workspace = directory/'workspace'
    (session.workspace/'game/rooms').mkdir(parents=True)
    session.game_process_id = 778
    server = build_server(session)
    runner = CodexRunner(cfg, directory, session, metrics, None, game_server=server)
    current, bridge = None, None
    done = asyncio.Event()
    report = {'model': model, 'stages': [], 'passed': False,
              'boundary': 'Real CodexRunner/MCP/GameSession/BridgeClient; external game socket fixture, no live game or recording.'}

    def event(method, params):
        item = params.get('item') or {}
        if current is not None and method == 'item/completed' and item.get('type') == 'agentMessage':
            current['reply'] += item.get('text', '')
        if current is not None and method == 'turn/completed':
            current['status'] = (params.get('turn') or {}).get('status')
            done.set()

    allowed = {'celeste_observe', 'celeste_act', 'celeste_cutscene', 'celeste_stop_run'}

    async def request(method, params):
        if params.get('tool') not in allowed or len(current['calls']) >= 8:
            raise RuntimeError('Only bounded fixture game tools are allowed')
        before = bridge.frames
        response = await runner._request(method, params)
        current['calls'].append({'tool': params['tool'], 'arguments': params.get('arguments'),
                                 'frames_before': before, 'frames_after': bridge.frames, 'response': response})
        return response

    rpc = CodexRPC(directory/'rpc', on_event=event, on_request=request)
    runner.rpc = rpc
    try:
        await rpc.start()
        rules = fill((ROOT/'orchestrator/prompts/shared/interface_zh.md').read_text(encoding='utf-8'), cfg)
        thread = await rpc.request('thread/start', {
            'model': model, 'cwd': str(directory), 'sandbox': 'read-only', 'approvalPolicy': 'never',
            'selectedCapabilityRoots': [], 'config': {'model_reasoning_effort': 'medium'},
            'baseInstructions': '你是独立的隔离验收玩家。仅使用下列动态游戏工具；它们都连接测试夹具。'
                                '不得用Shell、文件、网络或其它工具，不连接真实游戏。每个阶段单独按实时结果行动。\n' + rules,
            'dynamicTools': [t for t in await runner.tool_specs() if t['name'] in allowed]})
        runner.session_id = report['thread'] = thread['thread']['id']
        for name in ('confirm_after_refused_stop', 'autonomous_menu_retry', 'transport_failure'):
            bridge = RespawnBridge(ready=97921, confirm=True, death_event=name != 'autonomous_menu_retry', screen_wipe=False)
            wire = BridgeWire(bridge)
            with patch('tools.bridge_client.socket.create_connection', return_value=wire):
                client = BridgeClient(port=1)
            client.after_step = session._after_step
            if session.client:
                session.client.close()
            session.client, session.last_obs = client, copy.deepcopy(bridge.obs)
            session._transition_wait = session._transition_observed = None
            session.agent_stop = None
            pauses = []
            session.request_pause = lambda reason: pauses.append(reason) or True
            if name == 'autonomous_menu_retry':
                bridge.obs['controllable'] = False
                bridge.obs['level']['paused'] = True
                bridge.obs['pauseMenu'] = {'focused': True, 'selection': 1, 'items': []}
                session.last_obs = copy.deepcopy(bridge.obs)
                await server.call_tool('act', {'inputs': ['1,D', '1', '1,C'], 'thought': '重试房间'})
            else:
                await server.call_tool('act', {'inputs': ['1,R'], 'thought': '测试路线'})
            if name == 'transport_failure':
                original_read = wire.readline

                def disconnected_read():
                    if wire.request.get('cmd') == 'step':
                        raise ConnectionResetError('isolated fixture disconnected after receiving step')
                    return original_read()

                wire.readline = disconnected_read
                prompt = ('此阶段请先调用 act 执行一次30帧空输入，实际核验恢复路径。'
                          '若工具报告桥接连接故障，不重发输入，按 stop_run 的分类和确认回执完成报告并说明返回状态。')
            elif name == 'confirm_after_refused_stop':
                prompt = ('目前刚自然死亡，角色暂缺。先调用 stop_run 检查主控响应；然后按用户要求用 act 显式发送 C 缩短死亡动画。'
                          '依据实际工具结果完成恢复；不要等待动画或请求用户处理。最后用中文说明结果。')
            else:
                prompt = ('这是新的隔离阶段。刚通过暂停菜单重试房间，角色暂缺、死亡数已增长，尚未推进任何重生帧。'
                          '请自主选择恢复方式，恢复到角色可操作即完成本阶段。不得请求用户处理正常动画，不重启章节。最后用中文说明实际结果。')
            current = {'stage': name, 'calls': [], 'reply': ''}
            report['stages'].append(current)
            done.clear()
            runner.turn_active, runner.turn_id = True, None
            turn = await rpc.request('turn/start', {'threadId': runner.session_id, 'effort': 'medium',
                'serviceTierForTurn': 'default', 'input': [{'type': 'text', 'text': prompt}]})
            runner.turn_id = turn['turn']['id']
            await asyncio.wait_for(done.wait(), 180)
            current.update(steps=copy.deepcopy(bridge.steps), frame=bridge.obs['frame'],
                           controllable=bridge.obs['controllable'], pauses=pauses.copy())
            if name == 'transport_failure':
                current['passed'] = bool(len(pauses) == 1 and session.agent_stop
                                         and session.agent_stop.get('category') == 'bridge_io' and len(bridge.steps) == 1
                                         and client.last_call_failure and current['calls'][0]['tool'] == 'celeste_act'
                                         and current['calls'][0]['response']['success'] is False)
            elif name == 'confirm_after_refused_stop':
                current['passed'] = (not pauses and not session.agent_stop and bridge.obs['controllable']
                                     and len(bridge.steps) == 2 and ',C' in bridge.steps[1][0]
                                     and current['calls'][0]['tool'] == 'celeste_stop_run'
                                     and 'recovery_available' in json.dumps(current['calls'][0]['response']))
            else:
                current['passed'] = bridge.obs['controllable'] and not pauses and not session.agent_stop
            current['passed'] = bool(current['passed'] and current.get('status') == 'completed' and current['reply'])
            print(json.dumps({'stage': name, 'passed': current['passed'], 'steps': current['steps'],
                              'reply': current['reply']}, ensure_ascii=False), flush=True)
        report['passed'] = all(stage['passed'] for stage in report['stages'])
        return report['passed']
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        await rpc.close()
        session.close(release=False)
        runner.close_events()
        (directory/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(check(args.model, args.output)) else 1)
