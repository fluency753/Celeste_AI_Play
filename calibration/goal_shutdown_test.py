"""Goal checks must keep the game frozen through recording and socket cleanup."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from calibration.http_fixture import bound_server
from calibration.formal_run_test import _observation

from calibration import recording_session_test
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.recording import Recorder, RecordingError
from orchestrator.run import _run_session, _shutdown_mode, _previous_kept_frozen, parse_args
from orchestrator.server import build_server
from mcp.server.mcpserver.exceptions import ToolError


class GoalShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, manual=False, failure=False, record=True, cleanup_failure=False,
                       report=False, goal_args=(), enter_from_menu=False, park=False):
        fixture = recording_session_test.RecordingSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.enterContext(patch('orchestrator.config.REPO_ROOT', fixture.root))
        root = fixture.first
        cfg = RunConfig(agent='test', save='test', save_slot=14, model='test', engine='codex',
                        runtime='host', sim=False, record=record, cutscene_mode='agent')
        metrics = Metrics(root/'metrics.json', 'test', 'test', 'high', 600)
        session = GameSession(cfg, root, metrics)
        level = _observation()
        level.update(scene='Level', frame=103, save={'name':'test','slot':14})
        level['level'].update(sid='Celeste/1-ForsakenCity', side='A', room='6zb')
        session.last_obs = level
        if enter_from_menu:
            session.last_obs = {'scene': 'Overworld', 'frame': 70, 'save': level['save'],
                                'menu': {'current': 'OuiChapterSelect'}}
            metrics.update(chapter=level['level']['sid'], chapter_side='A', room='6zb')
            cfg.thought_lead_s = 0
        bridge_obs = session.last_obs
        steps = []
        fixture.hello.update(active=True, frozen=True, frame=103)
        def call(command, **options):
            nonlocal bridge_obs
            if command == 'observe':
                return {'obs':copy.deepcopy(bridge_obs)}
            if command == 'step' and enter_from_menu:
                steps.append(options['inputs'])
                if len(steps) == 1:
                    self.assertEqual(options['inputs'], ['1,C'])
                    bridge_obs = {'scene': 'LevelLoader', 'frame': 71}
                elif len(steps) == 2:
                    bridge_obs = level
                else:
                    self.fail('主控在入图目标命中后继续推进了游戏')
                reply = {'obs': copy.deepcopy(bridge_obs), 'result': {'consumed': 1, 'planned': 1, 'stop': 'done',
                         'events': [], 'trajectory': []}}
                session._after_step(options, reply)
                if len(steps) == 2:
                    self.assertTrue(session.goals.stop_motion.is_set())
                return reply
            if command == 'deactivate':
                fixture.hello.update(active=False, frozen=False)
            if command == 'record' and options.get('action') == 'stop' and failure == 'request':
                raise RecordingError('stop request failed')
            reply = fixture.call(command, **options)
            if failure == 'timeout' and command == 'record' and options.get('action') == 'stop':
                fixture.hello['clock']['plugin']['state'] = 'finishing'
                reply = copy.deepcopy(fixture.hello)
            return reply
        client = SimpleNamespace(call=call, close=Mock())
        session.client = client
        session.connect = lambda: session.last_obs
        recorder = fixture.start() if record else None
        if recorder:
            fixture.advance(3)
            if failure == 'timeout':
                original_stop = recorder.stop
                recorder.stop = lambda **kwargs: original_stop(timeout=0, **kwargs)
        server = build_server(session)
        actor_calls = []
        async def actor():
            actor_calls.append(True)
            if park:
                runner.parked_frame = 103
                runner.parked_for_switch = True
                return '挂起以切换工作区'
            if enter_from_menu:
                self.assertTrue(session.goals.snapshot()['awaiting_entry'])
                self.assertFalse(session.goals.check(session.goal_values()))
                self.assertFalse(session.done)
                with self.assertRaisesRegex(ToolError, '尚未进入关卡'):
                    await server.call_tool('finish_goal', {'goal': '到达房间 6zb', 'evidence': '缓存房间为 6zb'})
                await server.call_tool('act', {'inputs': ['1,C'], 'thought': '进入章节'})
                self.assertTrue(session.goals.stop_motion.is_set())
                self.assertEqual(len(steps), 2)
                session.finish_user_stop()
                return session.done_reason
            if report:
                if goal_args:
                    self.assertEqual(session.goals.snapshot()['baseline']['frame'], 103)
                    with self.assertRaisesRegex(ToolError, '与用户登记的文字目标不同'):
                        await server.call_tool('finish_goal', {'goal': '旧目标', 'evidence': '已达成'})
                    self.assertFalse(session.done)
                reply = await server.call_tool('finish_goal',
                    {'goal': '到达房间 6zb', 'evidence': '最新观测的房间为 6zb'})
                self.assertIn('按智能体报告结束试玩', reply.content[0].text)
                with self.assertRaisesRegex(ToolError, '已结束'):
                    await server.call_tool('finish_goal', {'goal': '到达房间 6zb', 'evidence': '重复提交'})
                return session.done_reason
            baseline = session.goal_values()
            session.goals.set({'conditions':[{'field':'elapsed','op':'delta_gte','value':1}]}, baseline)
            with self.assertRaisesRegex(ToolError, '已有用户设置的自动停止条件'):
                await server.call_tool('finish_goal', {'goal': '到达房间 6zb', 'evidence': '已到达'})
            self.assertFalse(session.done)
            if manual:
                session.goals.stop_now()
            else:
                session.goals.check({**baseline, 'elapsed':baseline['elapsed']+1})
            session.finish_user_stop()
            return session.done_reason
        runner = SimpleNamespace(run=actor, interrupted_for_resume=False, turns=1, session_id='test-session')
        web = SimpleNamespace(started=True, should_exit=False)
        async def serve():
            while not web.should_exit:
                await asyncio.sleep(.001)
        web.serve = serve
        args = SimpleNamespace(prepared=None, fresh_session=False, no_agent=False, record=record, start_paused=False)
        if goal_args:
            with patch.object(sys, 'argv', ['run', '--agent', 'test', '--save', 'test', *goal_args]):
                _, parsed = parse_args()
            args.initial_goal = parsed.initial_goal
        def movie(*args, **kwargs):
            if not (manual or report or park):
                self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
                self.assertFalse(fixture.hello['record_audio_active'])
            if failure is True:
                raise RecordingError('encoder/ffmpeg fixture failure')
            return fixture.directory/'gameplay.mp4'
        with patch('orchestrator.run.GameSession',return_value=session), \
             patch('orchestrator.run.ensure_container',return_value='isolated-fixture-no-container'), \
             patch('orchestrator.run.build_server',return_value=server), \
             patch('orchestrator.run.add_gate_route'), \
             patch('orchestrator.http_service.uvicorn.Server',return_value=bound_server(web, cfg.mcp_port)), \
             patch('orchestrator.run.Playback',return_value=SimpleNamespace(collect=False,close=Mock(
                 side_effect=RuntimeError('playback cleanup failed') if cleanup_failure else None))), \
             patch('orchestrator.runtimes.runtime',return_value=SimpleNamespace(runner=lambda *a,**kw:runner)), \
             patch('orchestrator.recording.finished_movie',side_effect=movie), \
             patch('orchestrator.recording.make_pure',return_value={'state':'finished','frames':3,'seconds':.05,'removed':0,'file':'pure'}):
            if cleanup_failure:
                with self.assertRaisesRegex(RuntimeError, 'playback cleanup failed'):
                    await _run_session(cfg,args,root,None,None,None,metrics,recorder)
                summary = None
            else:
                summary = await _run_session(cfg,args,root,None,None,None,metrics,recorder)
        client.close.assert_called_once()
        if goal_args and not report and not enter_from_menu:
            self.assertFalse(actor_calls)
        return fixture, cfg, session, recorder, summary

    async def test_automatic_time_goal_stops_real_recorder_but_not_game(self):
        fixture,cfg,session,recorder,summary = await self.exercise()
        self.assertTrue(session.done and summary['goal']['reached'])
        self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
        self.assertEqual(fixture.hello['frame'],103)
        self.assertFalse(fixture.hello['clock']['lockstep'])
        self.assertFalse(fixture.hello['record_audio_active'])
        self.assertNotIn('deactivate',[cmd for cmd,_ in fixture.commands])
        self.assertEqual([options['action'] for cmd,options in fixture.commands if cmd=='record'], ['start','stop'])
        self.assertFalse(summary['game_released'])
        self.assertTrue(_previous_kept_frozen({'dir':fixture.first}))
        self.assertFalse(getattr(cfg,'_recording_retained',False))
        saved = Recorder.saved_session(fixture.first)
        self.assertEqual(saved['state'],'finished')
        self.assertEqual(Recorder.resume_mode(saved,fixture.hello),'recover')
        # Repeated cleanup does not send input or release the held game.
        recorder.stop()
        self.assertEqual([options['action'] for cmd,options in fixture.commands if cmd=='record'], ['start','stop'])

    async def test_manual_stop_keeps_explicit_return_of_control(self):
        fixture,_,_,_,summary = await self.exercise(manual=True)
        self.assertFalse(fixture.hello['active'])
        self.assertTrue(summary['game_released'])
        self.assertIn('deactivate',[cmd for cmd,_ in fixture.commands])

    async def test_parked_run_releases_recording_and_game_before_it_is_resumable(self):
        from orchestrator.parked_run import mark_resumed, parked_summary
        fixture, _, _, _, summary = await self.exercise(park=True)
        self.assertTrue(summary['game_released'])
        self.assertFalse(fixture.hello['active'] or fixture.hello['clock']['lockstep'])
        self.assertEqual(Recorder.saved_session(fixture.first)['state'], 'finished')
        self.assertEqual(parked_summary(fixture.first)['parked_slot'], 14)
        self.assertNotIn('step', [command for command, _ in fixture.commands])
        mark_resumed(fixture.first, fixture.root / 'successor')
        self.assertEqual(parked_summary(fixture.first)['resumed_by'], str(fixture.root / 'successor'))

    async def test_parked_run_is_not_resumable_before_capture_stop_is_confirmed(self):
        from orchestrator.parked_run import parked_summary
        fixture, _, _, _, summary = await self.exercise(park=True, failure='request')
        self.assertFalse(parked_summary(fixture.first))
        self.assertFalse(summary['game_released'])
        self.assertTrue(fixture.hello['active'])

    async def test_reported_oral_goal_stops_capture_and_releases_game_without_inputs(self):
        fixture, _, session, _, summary = await self.exercise(report=True)
        self.assertTrue(session.done)
        self.assertFalse(summary['completed'] or summary['user_stopped'] or summary['goal']['reached'])
        self.assertIsNone(summary['agent_stop'])
        report = summary['goal']['completion_report']
        self.assertEqual(report['goal'], '到达房间 6zb')
        self.assertEqual(report['observation']['frame'], 103)
        self.assertEqual(json.loads((fixture.first / 'goal.json').read_text(encoding='utf-8'))['completion_report'], report)
        self.assertTrue(summary['game_released'])
        self.assertEqual(Recorder.saved_session(fixture.first)['state'], 'finished')
        self.assertNotIn('step', [cmd for cmd, _ in fixture.commands])
        self.assertIn('deactivate', [cmd for cmd, _ in fixture.commands])

    async def test_launch_text_goal_uses_registered_text_and_fresh_baseline(self):
        _, _, _, _, summary = await self.exercise(report=True, goal_args=('--goal', '到达房间 6zb'))
        self.assertEqual(summary['goal']['completion_report']['goal'], '到达房间 6zb')
        self.assertEqual(summary['goal']['text'], '到达房间 6zb')

    async def test_satisfied_launch_condition_finishes_before_model_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'goal.json'
            path.write_text(json.dumps({'conditions':[{'field':'room','op':'eq','value':'6zb'}]}),
                            encoding='utf-8-sig')
            _, _, _, _, summary = await self.exercise(goal_args=('--goal-file', str(path)))
        self.assertTrue(summary['goal']['reached'])
        self.assertIsNone(summary['session_id'])
        self.assertFalse(summary['game_released'])

    async def test_launch_from_chapter_menu_cannot_match_cached_room_before_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'goal.json'
            path.write_text(json.dumps({'conditions':[{'field':'room','op':'eq','value':'6zb'}]}), encoding='utf-8')
            _, _, _, _, summary = await self.exercise(goal_args=('--goal-file', str(path)), enter_from_menu=True)
        self.assertTrue(summary['goal']['reached'])
        self.assertEqual(summary['goal']['baseline']['frame'], 103)
        self.assertFalse(summary['game_released'])

    def test_invalid_launch_goal_rejected_before_loading_previous_run(self):
        with patch.object(sys, 'argv', ['run', '--agent', 'test', '--resume', 'unused', '--goal', ' ']), \
             patch('orchestrator.run.load_previous') as load:
            with self.assertRaises(SystemExit):
                parse_args()
        load.assert_not_called()

    async def test_postprocessing_failure_does_not_release_goal_freeze(self):
        fixture,_,_,_,summary = await self.exercise(failure=True)
        self.assertFalse(summary['game_released'])
        self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
        self.assertNotIn('deactivate',[cmd for cmd,_ in fixture.commands])

    async def test_no_record_goal_still_retains_freeze(self):
        fixture,_,_,_,summary = await self.exercise(record=False)
        self.assertFalse(summary['game_released'])
        self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])

    async def test_failed_stop_request_keeps_freeze_and_does_not_claim_finished(self):
        fixture,_,_,recorder,summary = await self.exercise(failure='request')
        self.assertFalse(summary['game_released'])
        self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
        self.assertTrue(recorder.started)
        self.assertIsNone(summary['recording'])

    async def test_encoder_timeout_keeps_pending_state_and_goal_freeze(self):
        fixture,_,_,_,summary = await self.exercise(failure='timeout')
        self.assertFalse(summary['game_released'])
        self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
        self.assertTrue(summary['recording']['pending'])
        self.assertEqual(Recorder.saved_session(fixture.first)['state'],'finishing')

    async def test_cleanup_error_after_goal_does_not_undo_freeze(self):
        fixture,cfg,_,_,_ = await self.exercise(cleanup_failure=True)
        self.assertTrue(cfg._keep_frozen)
        self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
        self.assertNotIn('deactivate',[cmd for cmd,_ in fixture.commands])

    def test_shutdown_modes_do_not_confuse_game_ownership_with_live_encoder(self):
        for engine in ('codex','claude'):
            cfg=SimpleNamespace(engine=engine)
            args=SimpleNamespace(no_agent=False)
            session=SimpleNamespace(done=True,goals=SimpleNamespace(immediate=False,snapshot=lambda:{'reached':True}))
            self.assertEqual(_shutdown_mode(cfg,args,session),'freeze')
            session.goals.immediate=True
            self.assertEqual(_shutdown_mode(cfg,args,session),'release')
            session.done=False
            self.assertEqual(_shutdown_mode(cfg,args,session,interrupted=True),'retain')


if __name__ == '__main__':
    unittest.main()
