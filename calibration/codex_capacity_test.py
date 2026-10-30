"""Codex capacity recovery keeps real turn, tool, controls and timing consumers.

Only the external RPC/model and game bridge are fixtures. These checks never
connect to the user's controller, account, game, OBS or saved configuration.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from calibration.collision_contract_test import FakeBridge
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


CAPACITY = 'Selected model is at capacity. Please try a different model.'


class CountingBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.steps = []

    def call(self, command, **args):
        if command == 'step':
            self.steps.append(deepcopy(args['inputs']))
        return super().call(command, **args)


class ModelAvailabilityClassificationTests(unittest.TestCase):
    def test_explicit_service_failures_and_structured_status_are_recoverable(self):
        from orchestrator.codex_recovery import is_model_unavailable
        for error in (CAPACITY, RuntimeError(CAPACITY), {'message': CAPACITY},
                      {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 503}}},
                      {'codexErrorInfo': 'serverOverloaded'},
                      {'message': 'Service temporarily unavailable', 'status': 529},
                      {'message': CAPACITY, 'httpStatusCode': 429},
                      {'message': 'Request timed out', 'httpStatusCode': 408}):
            with self.subTest(error=error):
                self.assertTrue(is_model_unavailable(error))

    def test_authentication_quota_and_tool_policy_are_not_capacity_retries(self):
        from orchestrator.codex_recovery import is_model_unavailable
        for error in ('HTTP 401 authentication failed', 'HTTP 403 permission denied',
                      'usage limit reached', 'quota exceeded', 'Invalid params: model not found',
                      'tool permission denied', 'HTTP 400 invalid argument',
                      {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 429}}},
                      {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 410}}},
                      {'message': 'HTTP 501 Not Implemented', 'codexErrorInfo': 'httpConnectionFailed'},
                      {'code': -32602, 'message': 'Selected model is at capacity'}):
            with self.subTest(error=error):
                self.assertFalse(is_model_unavailable(error))


class CodexCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(.002)

    async def run_case(self, *, failure='rpc', failures=1, action=False,
                       waiting=None, configure=None, initial_user=None, preparing=False, naming=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = RunConfig(agent='capacity-test', save='test', model='test', engine='codex',
                            mode='explore', runtime='container', sim=False, sim_assist=False,
                            collision_image='off', record=False)
            cfg.min_frames_per_act = 1
            cfg.net_wait_cap_s = .025
            cfg.net_probe_s = .005
            cfg.net_wait_max_h = .01
            if configure:
                configure(cfg)
            metrics = Metrics(root/'metrics.json', 'capacity-test', 'test', 'high', 60, study=preparing)
            with patch('orchestrator.config.REPO_ROOT', root):
                session = GameSession(cfg, root, metrics)
            bridge = CountingBridge()
            session.client = bridge
            study = None
            if preparing:
                kb = root/'kb'
                kb.mkdir()
                (kb/'README.md').write_text('unread required line\n', encoding='utf-8')
                study = KbStudy(kb, 10)
            study_started = study.started if study else None
            if not preparing:
                metrics.start_play()
            runner = CodexRunner(cfg, root, session, metrics, 'unused', game_server=build_server(session),
                                 resume_note='原始任务，请执行原始动作标记。', study=study, naming_only=naming)
            if initial_user:
                runner.pending_messages.append({'id': -1, 'text': initial_user})
            sent, actions, children = [], [], []
            state = {'reason': None, 'error': None}

            async def finish(number, failed):
                if failed and action:
                    response = await runner._request('item/tool/call', {
                        'callId': 'action-before-capacity', 'tool': 'celeste_act',
                        'arguments': {'inputs': ['1,R'], 'thought': '核验容量失败前的动作'}})
                    actions.append(response)
                runner._event('turn/completed', {'turn': {
                    'id': f'turn-{number}', 'status': 'failed' if failed else 'completed',
                    **({'error': {'message': CAPACITY}} if failed else {})}})
                if not failed:
                    if naming:
                        runner.naming_reply = 'Fixture'
                    session.done = True
                    session.done_reason = '隔离验证完成'

            async def request(method, params):
                if method == 'thread/start':
                    return {'thread': {'id': 'same-thread'}, 'model': 'test',
                            'activePermissionProfile': {'id': params['permissions']}}
                if method == 'turn/start':
                    sent.append(deepcopy(params))
                    number = len(sent)
                    failed = failures is None or number <= failures
                    if failed and failure == 'rpc':
                        raise RuntimeError(CAPACITY)
                    if failed and failure == 'auth':
                        raise RuntimeError('HTTP 401 Unauthorized: invalid authentication')
                    if failed and failure == 'parameter':
                        raise RuntimeError('Invalid params: unknown model')
                    if failed and failure == 'timeout':
                        raise TimeoutError('turn/start acknowledgment timed out')
                    if failed and failure == 'connection':
                        raise ConnectionError('Codex app-server disconnected')
                    child = asyncio.create_task(finish(number, failed))
                    children.append(child)
                    return {'turn': {'id': f'turn-{number}'}}
                return {}

            runner.rpc.start, runner.rpc.close = AsyncMock(), AsyncMock()
            runner.rpc.request = AsyncMock(side_effect=request)
            runner.rpc.reader = asyncio.get_running_loop().create_future()
            watcher = None
            if waiting:
                async def watch():
                    await self.until(lambda: getattr(runner, 'model_waiting', False))
                    await waiting(runner, session, metrics)
                watcher = asyncio.create_task(watch())
            try:
                with patch('orchestrator.config.REPO_ROOT', root), \
                        patch('orchestrator.personal_prompts.STORE', root/'personal.json'), \
                        patch('orchestrator.prompting.OVERRIDES', root/'prompts'), \
                        patch('orchestrator.model_catalog.load_models', return_value={'models': []}):
                    try:
                        state['reason'] = await asyncio.wait_for(runner.run(), 5)
                    except (RuntimeError, OSError, TimeoutError) as exc:
                        state['error'] = exc
                if watcher:
                    await watcher
                if children:
                    await asyncio.gather(*children)
                from orchestrator.run import _shutdown_mode
                state.update(runner=runner, session=session, metrics=metrics, sent=sent, actions=actions,
                             steps=deepcopy(bridge.steps),
                             study_shift=study.started-study_started if study else None,
                             shutdown=_shutdown_mode(cfg, SimpleNamespace(no_agent=False), session,
                                                     interrupted=runner.interrupted_for_resume,
                                                     model_failed=(getattr(runner, 'retain_on_model_failure', False)
                                                                   and not runner.stop_requested)))
                return state
            finally:
                if watcher and not watcher.done():
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                session.close(False)

    async def test_explicit_start_rejection_retries_same_thread_and_keeps_input(self):
        result = await self.run_case(failures=2, initial_user='保留这条尚未送达的用户消息。')
        self.assertIsNone(result['error'])
        self.assertEqual(result['reason'], '隔离验证完成')
        self.assertEqual(len(result['sent']), 3)
        self.assertEqual({p['threadId'] for p in result['sent']}, {'same-thread'})
        for params in result['sent']:
            text = params['input'][0]['text']
            self.assertEqual(text.count('保留这条尚未送达的用户消息。'), 1)
            self.assertEqual(text.count('原始动作标记'), 1)
        self.assertEqual(result['steps'], [])
        self.assertGreaterEqual(result['metrics'].state.net_waits, 1)
        self.assertGreater(result['metrics'].state.net_wait_s, 0)
        self.assertEqual(result['metrics'].state.quota_waits, 0)

    async def test_failed_accepted_turn_does_not_replay_executed_action_or_old_user_command(self):
        result = await self.run_case(failure='turn', action=True)
        self.assertIsNone(result['error'])
        self.assertEqual(len(result['sent']), 2)
        self.assertEqual(len(result['steps']), 1)
        self.assertTrue(result['actions'][0]['success'])
        resumed = result['sent'][1]['input'][0]['text']
        self.assertIn('observe', resumed)
        self.assertNotIn('原始动作标记', resumed)
        self.assertEqual(result['runner'].session_id, 'same-thread')

    async def test_continuous_capacity_failure_has_a_total_deadline_and_retains_control(self):
        result = await self.run_case(failures=None, configure=lambda cfg: setattr(cfg, 'net_wait_max_h', .00002))
        self.assertIsNotNone(result['error'])
        self.assertGreaterEqual(len(result['sent']), 2)
        self.assertLess(len(result['sent']), 12)
        self.assertEqual(result['shutdown'], 'retain')
        self.assertTrue(result['runner'].retain_on_model_failure)
        self.assertFalse(result['runner'].interrupted_for_resume)
        self.assertEqual(result['steps'], [])

    async def test_disabling_automatic_wait_still_retains_a_resumable_run(self):
        result = await self.run_case(configure=lambda cfg: setattr(cfg, 'net_wait', False))
        self.assertIsNotNone(result['error'])
        self.assertEqual(len(result['sent']), 1)
        self.assertEqual(result['shutdown'], 'retain')
        self.assertEqual(result['metrics'].state.net_waits, 0)

    async def test_non_transient_errors_do_not_enter_capacity_wait(self):
        for failure in ('auth', 'parameter'):
            with self.subTest(failure=failure):
                result = await self.run_case(failure=failure)
                self.assertIsNotNone(result['error'])
                self.assertEqual(len(result['sent']), 1)
                self.assertEqual(result['metrics'].state.net_waits, 0)

    async def test_unknown_start_ack_does_not_replay_and_retains_control(self):
        for failure in ('timeout', 'connection'):
            with self.subTest(failure=failure):
                result = await self.run_case(failure=failure)
                self.assertIsNotNone(result['error'])
                self.assertEqual(len(result['sent']), 1)
                self.assertEqual(result['shutdown'], 'retain')

    async def test_waiting_allows_explicit_interrupt_without_another_model_request(self):
        async def interrupt(runner, session, metrics):
            runner.control.enqueue('interrupt')
            await self.until(lambda: runner.stop_requested)
        result = await self.run_case(waiting=interrupt, configure=lambda cfg: setattr(cfg, 'net_wait_cap_s', .25))
        self.assertIsNone(result['error'])
        self.assertTrue(result['runner'].interrupted_for_resume)
        self.assertEqual(result['shutdown'], 'retain')
        self.assertEqual(len(result['sent']), 1)

    async def test_user_stop_during_wait_keeps_normal_release_semantics(self):
        async def stop(runner, session, metrics):
            runner.control.enqueue('stop')
            await self.until(lambda: session.done)
        result = await self.run_case(waiting=stop, configure=lambda cfg: setattr(cfg, 'net_wait_cap_s', .25))
        self.assertIsNone(result['error'])
        self.assertEqual(result['shutdown'], 'release')
        self.assertTrue(result['session'].goals.immediate)
        self.assertFalse(result['runner'].interrupted_for_resume)
        self.assertEqual(len(result['sent']), 1)

    async def test_reached_goal_during_wait_keeps_freeze_and_finishes_normally(self):
        async def reach(runner, session, metrics):
            session.goals.set({'conditions': [{'field': 'elapsed', 'op': 'gte', 'value': 0}]},
                              session.goal_values())
            await self.until(lambda: session.done)
        result = await self.run_case(waiting=reach, configure=lambda cfg: setattr(cfg, 'net_wait_cap_s', .25))
        self.assertIsNone(result['error'])
        self.assertEqual(result['shutdown'], 'freeze')
        self.assertTrue(result['session'].goals.snapshot()['reached'])
        self.assertEqual(len(result['sent']), 1)

    async def test_wait_does_not_consume_run_or_naming_budget(self):
        def config(cfg):
            cfg.max_minutes = .005
            cfg.net_wait_cap_s = .4
        for naming in (False, True):
            with self.subTest(naming=naming):
                result = await self.run_case(configure=config, naming=naming)
                self.assertIsNone(result['error'])
                self.assertEqual(len(result['sent']), 2)
                self.assertGreater(result['metrics'].state.net_wait_s, .1)
                self.assertIn(result['reason'], ('隔离验证完成', '存档命名完成'))

    async def test_preparation_limit_excludes_capacity_wait(self):
        result = await self.run_case(preparing=True)
        self.assertIsNone(result['error'])
        self.assertGreater(result['study_shift'], .01)
        self.assertGreaterEqual(result['study_shift'], result['metrics'].state.net_wait_s-.005)
        self.assertEqual(result['steps'], [])

    async def test_user_pause_during_wait_is_preserved_until_user_resumes(self):
        observations = {}
        async def pause_then_resume(runner, session, metrics):
            runner.control.enqueue('pause')
            await self.until(lambda: runner.paused and not runner.pausing)
            await self.until(lambda: not runner.model_waiting)
            observations['paused_after_wait'] = runner.paused
            elapsed = metrics.elapsed()
            await asyncio.sleep(.04)
            observations['elapsed_during_user_pause'] = metrics.elapsed() - elapsed
            runner.control.enqueue('resume')
        result = await self.run_case(waiting=pause_then_resume, configure=lambda cfg: setattr(cfg, 'net_wait_cap_s', .2))
        self.assertIsNone(result['error'])
        self.assertTrue(observations['paused_after_wait'])
        self.assertLess(observations['elapsed_during_user_pause'], .02)
        self.assertEqual(len(result['sent']), 2)

    async def test_pause_then_resume_during_wait_does_not_restart_timer_early(self):
        observations = {}
        async def controls(runner, session, metrics):
            runner.control.enqueue('pause')
            await self.until(lambda: runner.paused and not runner.pausing)
            runner.control.enqueue('resume')
            await self.until(lambda: not runner.paused)
            observations['still_waiting'] = runner.model_waiting
            elapsed = metrics.elapsed()
            await asyncio.sleep(.04)
            observations['premature_elapsed'] = metrics.elapsed() - elapsed
        result = await self.run_case(waiting=controls, configure=lambda cfg: setattr(cfg, 'net_wait_cap_s', .4))
        self.assertIsNone(result['error'])
        self.assertTrue(observations['still_waiting'])
        self.assertLess(observations['premature_elapsed'], .02)
        self.assertEqual(len(result['sent']), 2)

    async def test_rpc_disconnect_during_wait_retains_control_without_replay(self):
        async def disconnect(runner, session, metrics):
            runner.rpc.reader.set_result(None)
        result = await self.run_case(waiting=disconnect, configure=lambda cfg: setattr(cfg, 'net_wait_cap_s', .2))
        self.assertIsNotNone(result['error'])
        self.assertEqual(len(result['sent']), 1)
        self.assertEqual(result['shutdown'], 'retain')


class CapacityRecordingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_error_retains_real_recorder_in_connected_session_finally(self):
        from calibration.recording_session_test import RecordingSessionTests
        from orchestrator.recording import Recorder
        from orchestrator.run import _run_connected_session
        fixture = RecordingSessionTests()
        fixture.setUp()
        try:
            root = fixture.first
            cfg = RunConfig(agent='capacity-lifecycle', save='test', model='test', engine='codex',
                            runtime='container', mode='explore', sim=False, sim_assist=False,
                            collision_image='off', recording_mode='continuous')
            cfg.net_wait = False
            recorder = fixture.start()
            fixture.advance(3)
            original_video = (fixture.directory/'video.mkv').read_bytes()
            metrics = Metrics(root/'metrics.json', cfg.agent, cfg.model, 'high', 60)
            with patch('orchestrator.config.REPO_ROOT', fixture.root):
                session = GameSession(cfg, root, metrics)
            session.client = SimpleNamespace(call=fixture.call, close=Mock())
            session.connect = Mock(return_value={'scene': 'Level'})
            args = SimpleNamespace(prepared=None, fresh_session=False, no_agent=False, record=True,
                                   start_paused=False)
            web = SimpleNamespace(started=True, should_exit=False)
            async def serve():
                while not web.should_exit:
                    await asyncio.sleep(.001)
            web.serve = serve
            runners = []
            def create_rpc(directory, on_event, on_request, **options):
                runners.append(on_event.__self__)
                async def request(method, params):
                    if method == 'thread/start':
                        return {'thread': {'id': 'retained-thread'}, 'model': 'test',
                                'activePermissionProfile': {'id': params['permissions']}}
                    if method == 'turn/start':
                        raise RuntimeError(CAPACITY)
                    return {}
                return SimpleNamespace(start=AsyncMock(), close=AsyncMock(),
                                       request=AsyncMock(side_effect=request),
                                       reader=asyncio.get_running_loop().create_future())
            try:
                with patch('orchestrator.config.REPO_ROOT', fixture.root), \
                        patch('orchestrator.personal_prompts.STORE', fixture.root/'personal.json'), \
                        patch('orchestrator.prompting.OVERRIDES', fixture.root/'prompts'), \
                        patch('orchestrator.run.ensure_container', return_value='unused-container'), \
                        patch('orchestrator.run.uvicorn.Server', return_value=web), \
                        patch('orchestrator.run.Playback', return_value=SimpleNamespace(collect=False, close=Mock())), \
                        patch('orchestrator.model_catalog.load_models', return_value={'models': []}), \
                        patch('orchestrator.codex_agent.CodexRPC', side_effect=create_rpc):
                    with self.assertRaisesRegex(RuntimeError, '自动等待已关闭'):
                        await _run_connected_session(cfg, args, root, None, None, None,
                                                     metrics, recorder, None, session)
                self.assertTrue(cfg._keep_frozen)
                self.assertTrue(cfg._recording_retained)
                self.assertTrue(runners[0].retain_on_model_failure)
                self.assertFalse(runners[0].interrupted_for_resume)
                self.assertFalse(recorder.started)
                self.assertTrue(fixture.hello['record_audio_active'])
                self.assertTrue(fixture.hello['clock']['lockstep'])
                self.assertEqual(Recorder.saved_session(root)['state'], 'recording')
                self.assertEqual((fixture.directory/'video.mkv').read_bytes(), original_video)
                self.assertEqual([a['action'] for c, a in fixture.commands if c == 'record'], ['start'])
                self.assertNotIn('deactivate', [c for c, _ in fixture.commands])
                summary = json.loads((root/'summary.json').read_text(encoding='utf-8'))
                self.assertEqual(summary['recording']['state'], 'retained')
            finally:
                session.close(False)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
