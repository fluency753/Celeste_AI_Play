"""Codex capacity recovery keeps real turn, tool, controls and timing consumers.

Only the external RPC/model and game bridge are fixtures. These checks never
connect to the user's controller, account, game, OBS or saved configuration.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from calibration.http_fixture import bound_server

from calibration.collision_contract_test import FakeBridge
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


CAPACITY = 'Selected model is at capacity. Please try a different model.'


class WaitClock:
    """模型等待（codex_recovery._wait_for_model）读到的时钟与睡眠。

    等待每睡一次就把时钟往前拨过要睡的那一段，不占真实时间；hold 为真时等待原地不动（照常检查
    停止、断线等条件）。这样“等待期间用户做了某事”与“等待自己到期”谁先谁后由测试决定，不看机器快慢：
    原先用几十到几百毫秒的真实等待，在慢的机器上（持续集成的 Windows 运行器）等待常常先到期。
    事件循环、metrics 与控制通道仍用真实时钟。"""

    def __init__(self, hold):
        self.skew, self.hold = 0.0, hold

    def monotonic(self):
        return time.monotonic() + self.skew

    async def sleep(self, seconds):
        if self.hold:
            await asyncio.sleep(.002)
        else:
            self.skew += seconds
            await asyncio.sleep(0)


class Proxy:
    """一个模块，只换掉其中几个名字。"""

    def __init__(self, module, **overrides):
        self._module = module
        self.__dict__.update(overrides)

    def __getattr__(self, name):
        return getattr(self._module, name)


class CountingBridge(FakeBridge):
    def __init__(self):
        super().__init__()
        self.steps = []

    def call(self, command, **args):
        if command == 'step':
            self.steps.append(deepcopy(args['inputs']))
        return super().call(command, **args)


class ModelAvailabilityClassificationTests(unittest.TestCase):
    def test_auth_refusal_requires_explicit_credentials_evidence(self):
        from orchestrator.codex_recovery import requires_login
        for error in ({'status': 401}, {'statusCode': 401}, {'httpStatusCode': 401},
                      'workspace routing discovery unauthorized (401)', 'token_expired'):
            self.assertTrue(requires_login(error), error)
        for error in ({'code': -32602, 'message': 'HTTP 401 is not a valid model name'},
                      'HTTP 403 permission denied', 'responseStreamDisconnected'):
            self.assertFalse(requires_login(error), error)

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
                      {'message': 'token_expired', 'codexErrorInfo': 'responseStreamDisconnected'},
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
        async with asyncio.timeout(15):     # 上限而已：慢的机器上 3 秒不够
            while not condition():
                await asyncio.sleep(.002)

    async def run_case(self, *, failure='rpc', failures=1, action=False,
                       waiting=None, configure=None, initial_user=None, preparing=False, naming=False,
                       report_goal=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = RunConfig(agent='capacity-test', save='test', model='test', engine='codex',
                            mode='explore', runtime='container', sim=False, sim_assist=False,
                            collision_image='off', record=False)
            cfg.min_frames_per_act = 1
            cfg.net_wait_cap_s = 60        # 秒数都是 WaitClock 上的：一次等待不占真实时间
            cfg.net_probe_s = .005
            cfg.net_wait_max_h = 1
            if configure:
                configure(cfg)
            # 有 waiting 步骤时，等待先原地不动，等这些步骤做完（或它自己放行）才走到期。
            clock = self.clock = WaitClock(hold=waiting is not None)
            clock_time = Proxy(time, monotonic=clock.monotonic)
            metrics = Metrics(root/'metrics.json', 'capacity-test', 'test', 'high', 60, study=preparing)
            with patch('orchestrator.config.REPO_ROOT', root):
                session = GameSession(cfg, root, metrics)
            bridge = CountingBridge()
            session.client = bridge
            if report_goal:
                session._update_metrics_from_obs(bridge.obs)
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
                if report_goal:
                    session.agent_stop = {'reason': '已解决的历史阻塞'}
                    response = await runner._request('item/tool/call', {
                        'callId': 'finish-goal', 'tool': 'celeste_finish_goal',
                        'arguments': {'goal': '进入 s3 房间', 'evidence': '关卡观测的房间为 s3'}})
                    actions.append(response)
                    await asyncio.sleep(.25)
                    runner.rpc.close.assert_not_awaited()
                if failed and action:
                    response = await runner._request('item/tool/call', {
                        'callId': 'action-before-capacity', 'tool': 'celeste_act',
                        'arguments': {'inputs': ['1,R'], 'thought': '核验容量失败前的动作'}})
                    actions.append(response)
                terminal_errors = {
                    'auth_after_action': {'message': 'workspace routing discovery unauthorized (401)'},
                    'parameter_after_turn': {'code': -32602, 'message': 'HTTP 401 is not a valid model name'},
                }
                runner._event('turn/completed', {'turn': {
                    'id': f'turn-{number}', 'status': 'failed' if failed else 'completed',
                    **({'error': terminal_errors.get(failure, {'message': CAPACITY})} if failed else {})}})
                if not failed and not report_goal:
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
                    try:
                        await self.until(lambda: getattr(runner, 'model_waiting', False))
                        await waiting(runner, session, metrics)
                    finally:
                        clock.hold = False
                watcher = asyncio.create_task(watch())
            try:
                with patch('orchestrator.config.REPO_ROOT', root), \
                        patch('orchestrator.personal_prompts.STORE', root/'personal.json'), \
                        patch('orchestrator.prompting.OVERRIDES', root/'prompts'), \
                        patch('orchestrator.model_catalog.load_models', return_value={'models': []}), \
                        patch('orchestrator.codex_recovery.time', clock_time), \
                        patch('orchestrator.codex_agent.time', clock_time), \
                        patch('orchestrator.session_controls.time', clock_time), \
                        patch('orchestrator.codex_recovery.asyncio', Proxy(asyncio, sleep=clock.sleep)):
                    try:
                        state['reason'] = await asyncio.wait_for(runner.run(), 60)   # 只是上限：卡住时不至于一直等
                    except (RuntimeError, OSError, TimeoutError) as exc:
                        state['error'] = exc
                if watcher:
                    await watcher
                if children:
                    await asyncio.gather(*children)
                from orchestrator.run import _shutdown_mode
                state.update(runner=runner, session=session, metrics=metrics, sent=sent, actions=actions,
                             steps=deepcopy(bridge.steps),
                             public=(root/'public_events.jsonl').read_text(encoding='utf-8')
                             if (root/'public_events.jsonl').exists() else '',
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
                runner.close_events()      # 运行被上限打断时也要关上，否则 Windows 上临时目录删不掉
                session.close(False)

    async def test_goal_acceptance_reaches_turn_with_historical_blocker(self):
        state = await self.run_case(failures=0, report_goal=True)
        self.assertIsNone(state['error'])
        self.assertEqual(state['reason'], state['session'].done_reason)
        self.assertEqual(state['shutdown'], 'release')
        self.assertEqual(state['steps'], [])
        self.assertTrue(state['actions'][0]['success'])
        self.assertIn('按智能体报告结束试玩', state['actions'][0]['contentItems'][0]['text'])

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
        # 总时限 120 秒（WaitClock 上的）：第一次失败后等 30 秒重试，再等 60 秒重试，第三次失败后等到总时限为止。
        result = await self.run_case(failures=None, configure=lambda cfg: setattr(cfg, 'net_wait_max_h', 120 / 3600))
        self.assertIsNotNone(result['error'])
        self.assertIn('等待已达上限', str(result['error']))
        self.assertEqual(len(result['sent']), 3)
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
        for failure in ('parameter', 'parameter_after_turn'):
            with self.subTest(failure=failure):
                result = await self.run_case(failure=failure)
                self.assertIsNotNone(result['error'])
                self.assertEqual(len(result['sent']), 1)
                self.assertEqual(result['metrics'].state.net_waits, 0)
                self.assertEqual(result['shutdown'], 'release')

    async def test_final_auth_refusal_preserves_game_and_recording_without_replaying_action(self):
        for failure, action in [('auth', False), ('auth_after_action', True)]:
            with self.subTest(failure=failure):
                result = await self.run_case(failure=failure, action=action)
                self.assertIsNone(result['error'])
                self.assertIn('--container unused login', result['reason'])
                self.assertIn('原运行目录续跑', result['public'])
                self.assertEqual(result['shutdown'], 'retain')
                self.assertEqual(result['metrics'].state.net_waits, 0)
                self.assertEqual(len(result['sent']), 1)
                self.assertEqual(result['steps'], [['1,R']] if action else [])

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
        result = await self.run_case(waiting=interrupt)
        self.assertIsNone(result['error'])
        self.assertTrue(result['runner'].interrupted_for_resume)
        self.assertEqual(result['shutdown'], 'retain')
        self.assertEqual(len(result['sent']), 1)

    async def test_user_stop_during_wait_keeps_normal_release_semantics(self):
        async def stop(runner, session, metrics):
            runner.control.enqueue('stop')
            await self.until(lambda: session.done)
        result = await self.run_case(waiting=stop)
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
        result = await self.run_case(waiting=reach)
        self.assertIsNone(result['error'])
        self.assertEqual(result['shutdown'], 'freeze')
        self.assertTrue(result['session'].goals.snapshot()['reached'])
        self.assertEqual(len(result['sent']), 1)

    async def test_wait_does_not_consume_run_or_naming_budget(self):
        # 预算 20 秒，等待 30 秒（WaitClock 上的）：等待若计入现实用时，预算就用完了。
        # 真实的处理时间远小于 20 秒，不会自己把预算用完（原先预算只有 0.3 秒，慢的机器上会）。
        for naming in (False, True):
            with self.subTest(naming=naming):
                result = await self.run_case(configure=lambda cfg: setattr(cfg, 'max_minutes', 1 / 3), naming=naming)
                self.assertIsNone(result['error'])
                self.assertEqual(len(result['sent']), 2)
                self.assertGreaterEqual(result['metrics'].state.net_wait_s, 30)
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
            self.clock.hold = False            # 用户暂停之后，让等待自己到期
            await self.until(lambda: not runner.model_waiting)
            observations['paused_after_wait'] = runner.paused
            elapsed = metrics.elapsed()
            await asyncio.sleep(.04)
            observations['elapsed_during_user_pause'] = metrics.elapsed() - elapsed
            runner.control.enqueue('resume')
        result = await self.run_case(waiting=pause_then_resume)
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
        result = await self.run_case(waiting=controls)
        self.assertIsNone(result['error'])
        self.assertTrue(observations['still_waiting'])
        self.assertLess(observations['premature_elapsed'], .02)
        self.assertEqual(len(result['sent']), 2)

    async def test_rpc_disconnect_during_wait_retains_control_without_replay(self):
        async def disconnect(runner, session, metrics):
            runner.rpc.reader.set_result(None)
        result = await self.run_case(waiting=disconnect)
        self.assertIsNotNone(result['error'])
        self.assertEqual(len(result['sent']), 1)
        self.assertEqual(result['shutdown'], 'retain')


class CapacityRecordingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_capacity_error_retains_real_recorder_in_connected_session_finally(self):
        await self.exercise(CAPACITY)

    async def test_auth_error_retains_real_recorder_and_writes_resume_reason(self):
        await self.exercise('workspace routing discovery unauthorized (401)', auth=True)

    async def test_crash_after_the_game_process_exited_names_the_exit_as_the_reason(self):
        await self.exercise(CAPACITY, game_gone=True)

    async def exercise(self, failure, *, auth=False, game_gone=False):
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
            exit_record = {'pid': 40, 'exit_code': 0xC0000005, 'exit_code_hex': '0xC0000005'}
            if game_gone:      # What game_watch holds once the watched process has exited.
                session.game_watch = SimpleNamespace(gone=exit_record, records=[exit_record], close=Mock())
            args = SimpleNamespace(prepared=None, fresh_session=False, no_agent=False, record=True,
                                   start_paused=False)
            if auth:
                args.codex_preflight = ('unused-container', cfg.mcp_port)
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
                        raise RuntimeError(failure)
                    return {}
                return SimpleNamespace(start=AsyncMock(), close=AsyncMock(),
                                       request=AsyncMock(side_effect=request),
                                       reader=asyncio.get_running_loop().create_future())
            try:
                with patch('orchestrator.config.REPO_ROOT', fixture.root), \
                        patch('orchestrator.personal_prompts.STORE', fixture.root/'personal.json'), \
                        patch('orchestrator.prompting.OVERRIDES', fixture.root/'prompts'), \
                        patch('orchestrator.run.ensure_container', return_value='unused-container') as ensure, \
                        patch('orchestrator.http_service.uvicorn.Server', return_value=bound_server(web, cfg.mcp_port)), \
                        patch('orchestrator.run.Playback', return_value=SimpleNamespace(collect=False, close=Mock())), \
                        patch('orchestrator.model_catalog.load_models', return_value={'models': []}), \
                        patch('orchestrator.codex_agent.CodexRPC', side_effect=create_rpc):
                    if auth:
                        await _run_connected_session(cfg, args, root, None, None, None,
                                                     metrics, recorder, None, session)
                    else:
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
                if auth:
                    ensure.assert_not_called()
                    self.assertIn('--container unused-container login', summary['stop_reason'])
                    self.assertEqual(summary['session_id'], 'retained-thread')
                    self.assertNotIn('error', summary)
                elif game_gone:
                    # The cause comes first; the controller's own failure stays in summary['error'].
                    self.assertEqual((summary['stop_reason'], summary['stop_cause']),
                                     ('游戏进程已退出（退出码 0xC0000005，异常退出）', 'game_exit'))
                    self.assertEqual(summary['game_exits'], [exit_record])
                    self.assertEqual(summary['error']['type'], 'RuntimeError')
                    session.game_watch.close.assert_called()
                else:
                    # A crashed run says why in its own summary, not just "中断".
                    self.assertTrue(summary['stop_reason'].startswith('主控异常：RuntimeError: '))
                    self.assertNotIn('stop_cause', summary)
                    self.assertEqual(summary['error']['type'], 'RuntimeError')
                    self.assertIn('自动等待已关闭', summary['error']['message'])
            finally:
                session.close(False)
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
