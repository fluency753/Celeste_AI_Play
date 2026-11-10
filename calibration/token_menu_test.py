"""Pause repayment through production tools/BridgeClient, with only the wire replaced."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from calibration.story_wait_test import observation, BridgeWire
from orchestrator.agent import ClaudeRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server
from orchestrator.token_drift import TokenDrift
from tools.bridge_client import BridgeClient


class MenuBridge:
    def __init__(self):
        self.obs = observation(controllable=True, cutscene=False, canPause=True)
        self.obs['save'] = {'slot': 15, 'name': 'fixture'}
        self.steps = []
        self.respawn = 0
        self.interrupt = None
        self.lose_ack = False
        self.resume_during_menu = False
        self.dialogue_on_resume = False
        self.confirm_finishes_story = False
        self.seeker = False

    def call(self, command, **args):
        if command == 'hello':
            return {'input_formats': ['celeste_ai', 'celeste_tas']}
        if command != 'step':
            return {'obs': copy.deepcopy(self.obs)}
        stops = args.get('stop_on', [])
        start = self.obs['frame']
        planned = sum(int(s.split(',')[0]) for s in args['inputs'])
        used, stop, deaths = 0, 'done', 0
        trajectory = []
        if self.obs['controllable'] and 'controllable' in stops:
            stop = 'controllable'
        elif not self.obs['controllable'] and 'uncontrollable' in stops:
            stop = 'uncontrollable'
        else:
            for segment in args['inputs']:
                count, _, keys = segment.partition(',')
                keys = keys.upper().split(',') if keys else []
                for _ in range(int(count)):
                    self.obs['frame'] += 1
                    used += 1
                    paused = self.obs['level']['paused']
                    if paused:
                        menu = self.obs.get('pauseMenu') or {}
                        if 'D' in keys:
                            menu['selection'] = min(1, menu.get('selection', 0) + 1)
                        if 'U' in keys:
                            menu['selection'] = max(0, menu.get('selection', 0) - 1)
                        if self.resume_during_menu:
                            self.resume_during_menu = False
                            self.obs['level'].update(paused=False, inCutscene=False)
                            self.obs['storyDecision'] = None
                        elif any(k in keys for k in ('P', 'S', 'B')):
                            self.obs['level']['paused'] = False
                            if self.dialogue_on_resume:
                                self.obs['level'].update(inCutscene=True, dialogOpen=True, dialogWaiting=True)
                                self.obs['storyInput'] = 'dialogue'
                        elif any(k in keys for k in ('C', 'O')):
                            self.obs['level']['paused'] = False
                            self.respawn = 3
                            self.obs['player']['dead'] = True
                            deaths += 1
                            if 'death' in stops:
                                stop = 'death'
                    elif self.respawn:
                        self.respawn -= 1
                        self.obs['player']['dead'] = self.respawn > 0
                    elif any(k in keys for k in ('P', 'S', 'N')):
                        self.obs['level']['paused'] = True
                    elif self.confirm_finishes_story and any(k in keys for k in ('C', 'O')):
                        self.obs['level'].update(inCutscene=False, dialogOpen=False, dialogWaiting=False)
                        self.obs['storyInput'] = None
                    else:
                        self.obs['player']['pos'][0] += ('R' in keys) - ('L' in keys)
                        if self.interrupt and 'R' in keys:
                            stop, self.interrupt = self.interrupt, None
                            if stop == 'room':
                                self.obs['level']['room'] = 'next'
                            else:
                                self.respawn = 3
                                self.obs['player']['dead'] = True
                                deaths += 1
                    paused = self.obs['level']['paused']
                    self.obs['pauseMenu'] = ({'focused': True, 'selection': (self.obs.get('pauseMenu') or {}).get('selection', 0), 'items': [
                        {'index': 0, 'id': 'menu_pause_resume', 'text': '继续', 'selectable': True, 'disabled': False}]} if paused else None)
                    self.obs['controllable'] = not paused and not self.respawn and not self.seeker
                    self.obs['player']['inControl'] = self.obs['controllable']
                    self.obs['player']['state'] = 'IntroRespawn' if self.respawn else 'Dummy' if self.seeker else 'Normal'
                    self.obs['level']['chapterTimeTicks'] += 166667 if not paused else 0
                    trajectory.append([self.obs['frame'], *self.obs['player']['pos'], 0, 0,
                                       self.obs['player']['state'], 1, True])
                    if stop == 'done':
                        if self.obs['controllable'] and 'controllable' in stops:
                            stop = 'controllable'
                        elif not self.obs['controllable'] and 'uncontrollable' in stops:
                            stop = 'uncontrollable'
                    if stop != 'done':
                        break
                if stop != 'done':
                    break
        self.steps.append({'inputs': args['inputs'], 'input_format': args.get('input_format', 'celeste_ai'),
                           'before': start, 'after': self.obs['frame'], 'consumed': used, 'stop': stop})
        if self.lose_ack:
            self.lose_ack = False
            raise ConnectionError('fixture lost repayment ACK')
        return {'obs': copy.deepcopy(self.obs), 'result': {'consumed': used, 'planned': planned,
                'stop': stop, 'registeredDeaths': deaths, 'trajectory': trajectory, 'events': []}}


def client_for(session, bridge):
    client = BridgeClient.__new__(BridgeClient)
    client.sock = client.file = BridgeWire(bridge)
    client.next_id, client.last_call_failure = 1, None
    client.cancel_check = client.step_options = None
    client.step_filter = session._token_step_filter
    client.after_step = session._after_step
    return client


class ExitBridge(MenuBridge):
    def __init__(self, exit_id):
        super().__init__()
        self.exit_id = exit_id

    def call(self, command, **args):
        menu = self.obs.get('pauseMenu') or {}
        if command == 'step' and (self.obs['scene'] == 'Overworld' or
                menu.get('selection') == 1 and any(',C' in s for s in args['inputs'])):
            frame = self.obs['frame']
            planned = sum(int(s.split(',')[0]) for s in args['inputs'])
            enter = self.obs['scene'] == 'Overworld' and any(',C' in s for s in args['inputs'])
            if enter:
                self.obs = observation(controllable=True, cutscene=False, canPause=True)
                self.obs['level']['room'] = 'entry'
                self.obs['save'] = {'slot': 15, 'name': 'fixture'}
            else:
                self.obs = {'scene': 'Overworld', 'save': {'slot': 15, 'name': 'fixture'},
                            'controllable': True, 'player': None, 'level': None}
            self.obs['frame'] = frame + 1
            self.steps.append({'inputs': args['inputs'], 'before': frame, 'after': frame+1,
                               'consumed': 1, 'stop': 'controllable'})
            return {'obs': copy.deepcopy(self.obs), 'result': {'consumed': 1, 'planned': planned,
                    'stop': 'controllable', 'events': [], 'trajectory': []}}
        reply = super().call(command, **args)
        if self.obs.get('pauseMenu'):
            self.obs['pauseMenu']['items'].append({'index': 1, 'id': self.exit_id, 'text': '退出',
                                                  'selectable': True, 'disabled': False})
            reply['obs'] = copy.deepcopy(self.obs)
        return reply


class MenuDebtTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent='fixture', save='fixture', save_slot=15, model='fixture', engine='claude',
                             mode='explore', knowledge_profile='explore', runtime='host', sim=False,
                             token_drift=True, cutscene_mode='agent', collision_image='off', thought_lead_s=0)
        self.m = Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        self.s = GameSession(self.cfg, self.root, self.m)
        self.s.game_process_id = 123
        self.bridge = MenuBridge()
        self.s.client = client_for(self.s, self.bridge)
        self.s.last_obs = copy.deepcopy(self.bridge.obs)
        self.s._held_input = ('L', 'celeste_ai')
        self.s._start_level_clock(self.s.last_obs)
        self.server = build_server(self.s)
        self.r = ClaudeRunner(self.cfg, self.root, self.s, self.m, 'fixture', session_id='fixture')
        self.r.turn_active = True
        self.r._begin_model_turn(runtime='claude')
        self.r._start_token_worker()
        self.total = 0

    async def asyncTearDown(self):
        await self.r._finish_token_worker()
        self.r.close_events()
        self.s.close()
        self.temp.cleanup()

    async def tool(self, name='act', **args):
        result = await self.server.call_tool(name, args)
        return '\n'.join(getattr(x, 'text', '') for x in result.content)

    async def act(self, inputs, **extra):
        return await self.tool(inputs=inputs, thought='隔离验证菜单结算', **extra)

    async def usage(self, count):
        self.total += count
        self.r._record_output_usage(self.total, count)
        async with self.s.tool_lock:
            await self.r._flush_token_drift_locked()

    async def pause(self, **extra):
        return await self.act(['5,R', '1,S' if extra else '1,P', '20,L'], **extra)

    async def test_menu_is_billed_without_zero_steps_and_resume_pays_before_new_plan(self):
        await self.pause()
        self.assertEqual(self.bridge.steps[-1]['consumed'], 6)
        self.assertEqual(self.s.token_menu.state['keys'], 'R')
        await self.usage(1000)
        self.assertEqual((self.r.token_drift.deferred, len(self.bridge.steps)), (2, 1))
        await self.act(['1,D', '8'])
        self.assertEqual(self.s.token_menu.state['keys'], 'R')
        reply = await self.act(['1,P', '20,L'])
        self.assertEqual(self.bridge.steps[-2]['consumed'], 1)
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertEqual(self.bridge.obs['player']['pos'][0], 17)
        self.assertEqual((self.r.token_drift.deferred, self.m.state.token_frames), (0, 2))
        self.assertIn('补推进 2 帧', reply)
        self.assertIn('帧 118', reply)
        self.assertIsNone(self.s.token_menu.state)

    async def test_retry_does_not_clear_debt_and_recovers_before_repayment(self):
        await self.pause()
        await self.usage(1500)
        await self.act(['1,C', '10,R'])
        self.assertEqual(self.r.token_drift.deferred, 3)
        reply = await self.act(['10'])
        self.assertEqual(self.bridge.steps[-2]['consumed'], 3)
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['3,R'])
        self.assertEqual(self.m.state.deaths, 1)
        self.assertEqual(self.r.token_drift.deferred, 0)
        self.assertIn('补推进 3 帧', reply)

    async def test_repayment_death_preserves_unconsumed_debt(self):
        await self.pause()
        await self.usage(1500)
        self.bridge.interrupt = 'death'
        await self.act(['1,P', '20,L'])
        self.assertEqual((self.m.state.token_frames, self.r.token_drift.deferred), (1, 2))
        self.assertTrue(self.s.token_observation_required)
        await self.usage(500)
        self.assertEqual(self.r.token_drift.deferred, 3)

    async def test_hud_pause_and_compaction_exclude_new_usage_without_erasing_debt(self):
        await self.pause()
        await self.usage(1000)
        self.m.pause()
        await self.usage(1000)
        self.m.resume(quota=False)
        await self.usage(1000)
        self.r._public_compaction('fixture')
        await self.usage(1000)
        self.r._public_compaction('fixture', completed=True)
        await self.usage(1000)
        self.assertEqual(self.r.token_drift.deferred, 2)
        await self.usage(500)
        self.assertEqual(self.r.token_drift.deferred, 3)

    async def test_tas_menu_cannot_change_format_or_turn_menu_keys_into_gameplay(self):
        await self.pause(input_format='celeste_tas')
        await self.usage(1000)
        before = len(self.bridge.steps)
        refused = await self.act(['1,P'])
        self.assertIn('沿用暂停前输入格式', refused)
        self.assertEqual(len(self.bridge.steps), before)
        await self.act(['1,S', '20,L'], input_format='celeste_tas')
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertEqual(self.bridge.steps[-1]['input_format'], 'celeste_tas')

    async def test_resume_keeps_debt_and_input_identity_and_rejects_wrong_game(self):
        await self.pause()
        await self.usage(1500)
        self.r.token_drift.discard('runner_finished')
        (self.root/'summary.json').write_text(json.dumps({'game_released': False}), encoding='utf-8')
        next_dir = self.root/'next'
        next_dir.mkdir()
        metrics = Metrics(next_dir/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        metrics.state.resumed_from = str(self.root)
        ledger = TokenDrift(next_dir/'token_drift.json', metrics, 500, self.root/'token_drift.json')
        session = GameSession(self.cfg, next_dir, metrics)
        try:
            session.game_process_id = 123
            session.token_menu.restore(self.bridge.obs)
            self.assertEqual(ledger.deferred, 3)
            self.assertEqual(session.token_menu.held(self.bridge.obs), ('R', 'celeste_ai'))
            session.game_process_id = 999
            with self.assertRaisesRegex(ValueError, '来源未确认'):
                session.token_menu.held(self.bridge.obs)
            self.assertEqual(ledger.deferred, 3)
        finally:
            session.close()

    async def test_lost_repayment_ack_is_unknown_and_never_replayed(self):
        await self.pause()
        await self.usage(1500)
        original = self.bridge.call
        def call(command, **args):
            if command == 'step' and not self.bridge.obs['level']['paused']:
                self.bridge.lose_ack = True
            return original(command, **args)
        self.bridge.call = call
        with self.assertRaises(Exception):
            await self.act(['1,P', '20,L'])
        self.assertTrue(self.r.token_drift.unknown)
        self.assertTrue(self.r.paused)
        before = len(self.bridge.steps)
        refused = await self.act(['1,R'])
        self.assertIn('执行范围未确认', refused)
        self.assertEqual(len(self.bridge.steps), before)

    async def test_real_skip_helper_recovery_exception_settles_and_delivers_latest_state(self):
        await self.pause()
        await self.usage(1000)
        self.bridge.obs['level']['inCutscene'] = True
        self.bridge.obs['storyDecision'] = 'skip'
        self.bridge.obs['pauseMenu']['items'].append({'index': 1, 'id': 'menu_pause_skip_cutscene',
                'text': '跳过剧情', 'selectable': True, 'disabled': False})
        self.bridge.resume_during_menu = True
        before_frames, before_s = self.m.state.frames_played, self.m.state.play_game_s
        reply = await self.tool('cutscene', action='skip')
        self.assertEqual(self.r.token_drift.deferred, 0)
        self.assertEqual(self.m.state.token_frames, 2)
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertIn('本次后续输入未发送', reply)
        self.assertIn('帧 109', reply)
        self.assertIn('已补推进 2 帧', reply)
        self.assertTrue(self.s.last_obs['controllable'])
        self.assertEqual(self.m.state.frames_played - before_frames, 1)
        self.assertGreater(self.m.state.play_game_s - before_s, 0)
        rows = [json.loads(line) for line in (self.root/'frames.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['f'] for r in rows if r['f'] >= 107], [107, 108, 109])
        self.assertEqual([r['auto'] for r in rows if r['f'] >= 107], [False, True, True])

    async def test_shutdown_and_crash_resume_transfer_inflight_menu_usage_to_debt(self):
        async with self.s.tool_lock:
            self.total = 1000
            self.r._record_output_usage(1000, 1000)  # usage arrives while P is in flight
            self.s.client.call('step', inputs=['1,P'], stop_on=['uncontrollable'])
            self.assertEqual(self.r.token_drift.pending, 2)
            (self.root/'summary.json').write_text(json.dumps({'game_released': False}), encoding='utf-8')
            next_dir = self.root/'next'
            next_dir.mkdir()
            metrics = Metrics(next_dir/'metrics.json', 'fixture', 'fixture', 'medium', 600)
            metrics.state.resumed_from = str(self.root)
            session = GameSession(self.cfg, next_dir, metrics)
            try:
                session.game_process_id = 123
                session.token_menu.restore(self.bridge.obs)
                restored = TokenDrift(next_dir/'token_drift.json', metrics, 500,
                                      self.root/'token_drift.json', menu_context=session.token_menu.state is not None)
                self.assertEqual((restored.pending, restored.deferred, restored.state['dropped_frames']), (0, 2, 0))
            finally:
                session.close()
        await self.r._finish_token_worker()
        self.assertEqual((self.r.token_drift.pending, self.r.token_drift.deferred), (0, 2))

    async def test_auto_story_and_ending_boundary_preserve_acknowledged_time_and_frames(self):
        from orchestrator.token_menu import TokenMenuBoundary
        for ending in (False, True):
            with self.subTest(ending=ending):
                await self.pause()
                await self.usage(1000)
                # Resume into a cutscene; it is still not eligible for repayment.
                self.bridge.dialogue_on_resume = True
                await self.act(['1,P'])
                self.bridge.obs['level'].update(dialogOpen=False, dialogWaiting=False)
                self.bridge.obs['storyInput'] = None
                self.bridge.obs['storyDecision'] = 'skip'
                self.bridge.resume_during_menu = True
                original = self.bridge.call
                def call(command, **args):
                    result = original(command, **args)
                    if self.bridge.obs.get('pauseMenu'):
                        item = {'index': 1, 'id': 'menu_pause_skip_cutscene', 'text': '跳过剧情',
                                'selectable': True, 'disabled': False}
                        self.bridge.obs['pauseMenu']['items'].append(item)
                        result['obs'] = copy.deepcopy(self.bridge.obs)
                    return result
                self.bridge.call = call
                before_s, before_frames = self.m.state.auto_game_s, self.m.state.frames_auto
                async with self.s.tool_lock:
                    with self.assertRaises(TokenMenuBoundary):
                        if ending:
                            self.s._run_ending(copy.deepcopy(self.bridge.obs))
                        else:
                            self.s._auto_advance(copy.deepcopy(self.bridge.obs), None, story_mode='auto')
                    self.assertGreater(self.m.state.auto_game_s - before_s, 0)
                    self.assertEqual(self.m.state.frames_auto - before_frames, 2)
                    await self.r._flush_token_drift_locked()
                self.assertEqual(self.r.token_drift.deferred, 0)
                self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
                self.bridge.call = original
                self.bridge.dialogue_on_resume = False

    async def test_corrupt_matching_snapshot_cannot_replay_menu_keys(self):
        await self.pause()
        await self.usage(1000)
        saved = json.loads((self.root/'token_menu.json').read_text(encoding='utf-8'))
        saved['context']['keys'] = 'P'
        (self.root/'token_menu.json').write_text(json.dumps(saved), encoding='utf-8')
        (self.root/'summary.json').write_text(json.dumps({'game_released': False}), encoding='utf-8')
        next_dir = self.root/'next'
        next_dir.mkdir()
        metrics = Metrics(next_dir/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        metrics.state.resumed_from = str(self.root)
        session = GameSession(self.cfg, next_dir, metrics)
        try:
            session.game_process_id = 123
            session.token_menu.restore(self.bridge.obs)
            self.assertTrue(session.token_menu.state.get('invalid'))
            with self.assertRaises(ValueError):
                session.token_menu.held(self.bridge.obs)
            self.assertEqual(self.r.token_drift.deferred, 2)
        finally:
            session.close()

    async def check_exit_navigation(self, exit_id):
        from tools.cutscene import leave_level_by_pause_menu
        self.bridge = ExitBridge(exit_id)
        self.s.client = client_for(self.s, self.bridge)
        self.s.last_obs = copy.deepcopy(self.bridge.obs)
        await self.pause()
        await self.usage(1000)
        async with self.s.tool_lock:
            obs = await asyncio.to_thread(leave_level_by_pause_menu, self.s.client, allowed_exit_ids=(exit_id,))
            self.assertEqual(obs['scene'], 'Overworld')
            await self.r._flush_token_drift_locked()
            self.assertEqual(self.r.token_drift.deferred, 2)
            self.s.client.call('step', inputs=['2,D'])  # authorized UI navigation remains possible
            self.assertEqual(self.bridge.obs['scene'], 'Overworld')
            self.s.client.call('step', inputs=['1,C'])
            await self.r._flush_token_drift_locked()
        self.assertEqual(self.r.token_drift.deferred, 0)
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertEqual(self.bridge.obs['player']['pos'][0], 12)

    async def test_return_to_map_keeps_debt_without_blocking_navigation(self):
        await self.check_exit_navigation('menu_pause_return')

    async def test_exit_tool_recovery_boundary_accounts_partial_helper_and_pays(self):
        self.cfg.live = True
        self.cfg.action_permissions = {'restart_chapter': False, 'return_to_map': True, 'save_quit': False}
        self.server = build_server(self.s)
        self.bridge = ExitBridge('menu_pause_return')
        self.s.client = client_for(self.s, self.bridge)
        self.s.last_obs = copy.deepcopy(self.bridge.obs)
        await self.pause()
        await self.usage(1000)
        self.bridge.resume_during_menu = True
        before_s, before_frames = self.m.state.auto_game_s, self.m.state.frames_auto
        reply = await self.tool('exit_level', reason='隔离验证返回地图中断')
        self.assertIn('本次后续输入未发送', reply)
        self.assertIn('帧 109', reply)
        self.assertEqual(self.m.state.frames_auto-before_frames, 1)
        self.assertEqual(self.m.state.token_frames, 2)
        self.assertGreater(self.m.state.auto_game_s-before_s, 0)
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertFalse(self.s.done)

    async def test_save_quit_keeps_debt_without_blocking_navigation(self):
        await self.check_exit_navigation('menu_pause_savequit')

    async def test_legacy_pause_recovery_requires_original_identity_and_unbroken_pause_chain(self):
        await self.pause()
        await self.usage(1000)
        (self.root/'summary.json').write_text(json.dumps({'game_released': False}), encoding='utf-8')
        (self.root/'run_state.json').write_text(json.dumps({'save_slot': 15}), encoding='utf-8')
        decision = {'auto_notes': ['游戏暂停菜单已打开，主控停止自动播放。'],
                    'events': [{'type': 'uncontrollable', 'frame': 106}]}
        (self.root/'decisions.jsonl').write_text(json.dumps(decision)+'\n', encoding='utf-8')
        context = self.s.token_menu.legacy_context(self.root, self.bridge.obs)
        self.assertEqual((context['keys'], context['held_frame']), ('R', 105))
        ledger_path = self.root/'token_drift.json'
        ledger = json.loads(ledger_path.read_text(encoding='utf-8'))
        ledger['epoch'][0] = 999
        ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        self.assertIsNone(self.s.token_menu.legacy_context(self.root, self.bridge.obs))
        ledger['epoch'][0] = 123
        ledger_path.write_text(json.dumps(ledger), encoding='utf-8')
        with (self.root/'decisions.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'events': [{'type': 'controllable', 'frame': 107}]})+'\n')
        self.assertIsNone(self.s.token_menu.legacy_context(self.root, self.bridge.obs))

    async def test_controllable_story_can_finish_before_debt_and_unused_frames_stay_unsent(self):
        await self.pause()
        await self.usage(1000)
        self.bridge.dialogue_on_resume = True
        await self.act(['1,P', '20,L'])
        self.assertTrue(self.bridge.obs['controllable'])
        self.assertTrue(self.bridge.obs['level']['inCutscene'])
        self.assertEqual(self.r.token_drift.deferred, 2)
        self.bridge.confirm_finishes_story = True
        reply = await self.tool('cutscene', action='advance')
        self.assertEqual(self.bridge.steps[-2]['inputs'], ['1,C'])
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertEqual(self.r.token_drift.deferred, 0)
        self.assertEqual(self.bridge.obs['player']['pos'][0], 17)
        self.assertIn('帧 110', reply)

    async def test_player_seeker_pause_preserves_input_and_repayment_is_not_zero(self):
        from calibration.seeker_control_test import seeker_obs
        obs = seeker_obs()
        obs['player'] = {**self.bridge.obs['player'], **obs['player']}
        self.bridge.obs = obs
        self.bridge.obs['save'] = {'slot': 15, 'name': 'fixture'}
        self.bridge.seeker = True
        self.s.last_obs = copy.deepcopy(self.bridge.obs)
        await self.pause()
        self.assertEqual(self.bridge.steps[-1]['consumed'], 6)
        self.assertEqual(self.s.token_menu.state['keys'], 'R')
        await self.usage(1000)
        await self.act(['1,P', '20,L'])
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['2,R'])
        self.assertEqual(self.m.state.token_frames, 2)
        self.assertEqual(self.r.token_drift.deferred, 0)

    async def test_story_pending_limit_reports_unsent_plan_and_does_not_retry_done(self):
        from unittest.mock import patch
        await self.pause()
        await self.usage(1000)
        self.bridge.dialogue_on_resume = True
        await self.act(['1,P'])
        self.s.container = 'isolated-fixture'
        self.cfg.pause_limit = False
        self.s.workspace = self.root/'workspace'
        self.s.workspace.mkdir(exist_ok=True)
        (self.s.workspace/'game').mkdir(exist_ok=True)
        (self.s.workspace/'plan.json').write_text(json.dumps({'inputs': ['20,L'],
            'retry': {'command': 'python planner.py', 'max': 2, 'on': ['done']}}), encoding='utf-8')
        before = len(self.bridge.steps)
        with patch('orchestrator.game_session.run_program') as program:
            reply = await self.tool(plan_file='plan.json', thought='继续处理当前剧情')
        self.assertEqual(len(self.bridge.steps)-before, 1)
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['1,L'])
        self.assertIn('剩余输入没有执行', reply)
        self.assertIn('19,L', reply)
        self.assertNotIn('按键全部执行完', reply)
        self.assertEqual(self.r.token_drift.deferred, 2)
        program.assert_not_called()

    async def resumed_runner(self, process_id, same_game=False):
        """Resume this run in a new directory after the previous owner left 3 owed frames."""
        await self.pause()
        await self.usage(1500)
        self.assertEqual(self.r.token_drift.deferred, 3)
        (self.root/'summary.json').write_text(json.dumps({'game_released': False}), encoding='utf-8')
        next_dir = self.root/'next'
        next_dir.mkdir()
        metrics = Metrics(next_dir/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        metrics.state.resumed_from = str(self.root)
        session = GameSession(self.cfg, next_dir, metrics)
        session.game_process_id = process_id
        bridge = MenuBridge()            # the restarted game sits in a fresh, unpaused level
        bridge.obs['frame'] = 500
        if same_game:                    # frozen hand-over: still paused on the same frame
            bridge.obs = copy.deepcopy(self.bridge.obs)
        session.client = client_for(session, bridge)
        session.last_obs = copy.deepcopy(bridge.obs)
        session._held_input = ('R' if same_game else '', 'celeste_ai')
        session._start_level_clock(session.last_obs)
        session.token_menu.restore(bridge.obs)
        await self.r._finish_token_worker()   # the previous owner has ended
        runner = ClaudeRunner(self.cfg, next_dir, session, metrics, 'fixture', session_id='fixture2')
        runner.turn_active = True
        runner._begin_model_turn(runtime='claude')
        runner._start_token_worker()
        self.addAsyncCleanup(session.close)
        self.addAsyncCleanup(runner.close_events)
        self.addAsyncCleanup(runner._finish_token_worker)
        return session, runner, bridge, build_server(session), next_dir

    async def test_game_restart_before_resume_drops_owed_frames_and_play_continues(self):
        session, runner, bridge, server, next_dir = await self.resumed_runner(999)
        self.assertEqual(runner.token_drift.deferred, 0)
        self.assertIsNone(session.token_menu.state)
        ledger = json.loads((next_dir/'token_drift.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(ledger['dropped_frames'], 3)
        rows = [json.loads(line) for line in (next_dir/'token_advances.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(any(r['event'] == 'discarded' and r.get('origin') == 'deferred' and r['frames'] == 3 for r in rows))
        result = await server.call_tool('act', {'inputs': ['10,R'], 'thought': '续跑后继续'})
        text = '\n'.join(getattr(x, 'text', '') for x in result.content)
        self.assertIn('3 帧待补推进已作废', text)
        self.assertEqual([step['inputs'] for step in bridge.steps], [['10,R']])
        self.assertFalse(runner.paused)
        result = await server.call_tool('act', {'inputs': ['5,L'], 'thought': '提示只出现一次'})
        self.assertNotIn('作废', '\n'.join(getattr(x, 'text', '') for x in result.content))
        public = (next_dir/'public_events.jsonl').read_text(encoding='utf-8')
        self.assertIn('待补推进已作废', public)

    async def test_resume_with_moved_frame_drops_instead_of_replaying_keys(self):
        session, runner, bridge, server, next_dir = await self.resumed_runner(123)
        self.assertEqual(runner.token_drift.deferred, 0)
        await server.call_tool('act', {'inputs': ['4,L'], 'thought': '续跑后继续'})
        self.assertEqual([step['inputs'] for step in bridge.steps], [['4,L']])

    async def test_resume_on_same_frozen_game_still_repays_with_pre_pause_keys(self):
        session, runner, bridge, server, next_dir = await self.resumed_runner(123, same_game=True)
        self.assertEqual(runner.token_drift.deferred, 3)
        result = await server.call_tool('act', {'inputs': ['1,P', '20,L'], 'thought': '恢复后补推进'})
        self.assertEqual(bridge.steps[-1]['inputs'], ['3,R'])
        self.assertEqual(runner.token_drift.deferred, 0)
        self.assertNotIn('作废', '\n'.join(getattr(x, 'text', '') for x in result.content))

    async def test_token_drift_does_not_add_uncontrollable_stop_in_auto_story_mode(self):
        seen = []
        original = self.bridge.call
        def call(command, **args):
            if command == 'step':
                seen.append(list(args.get('stop_on') or []))
            return original(command, **args)
        self.bridge.call = call
        await self.act(['10,R'])                 # agent story mode forces it regardless of Token drift
        self.assertIn('uncontrollable', seen[-1])
        self.cfg.cutscene_mode = 'auto'
        await self.act(['10,R'])
        self.assertNotIn('uncontrollable', seen[-1])
        self.assertEqual(self.bridge.steps[-1]['inputs'], ['10,R'])

    async def test_limited_plan_keeps_actual_death_reason(self):
        from orchestrator.observation import STOP_TEXT
        self.bridge.interrupt = 'death'
        reply = await self.pause()
        self.assertIn(STOP_TEXT['death'], reply.splitlines()[0])
        self.assertIn('剩余输入没有执行', reply)
        self.assertNotIn('停止原因：Token 菜单恢复检查边界', reply)
