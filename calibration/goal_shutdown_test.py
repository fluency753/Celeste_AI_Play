"""Goal checks must keep the game frozen through recording and socket cleanup."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from calibration import recording_session_test
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.recording import Recorder, RecordingError
from orchestrator.run import _run_session, _shutdown_mode, _previous_kept_frozen


class GoalShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, manual=False, failure=False, record=True, cleanup_failure=False):
        fixture = recording_session_test.RecordingSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.enterContext(patch('orchestrator.config.REPO_ROOT', fixture.root))
        root = fixture.first
        cfg = RunConfig(agent='test', save='test', save_slot=14, model='test', engine='codex',
                        runtime='host', sim=False, record=record, cutscene_mode='agent')
        metrics = Metrics(root/'metrics.json', 'test', 'test', 'high', 600)
        session = GameSession(cfg, root, metrics)
        session.last_obs = {'scene': 'Level', 'frame': 103, 'save': {'name':'test','slot':14},
                            'level': {'sid':'Celeste/1-ForsakenCity','side':'A','room':'6zb'}}
        fixture.hello.update(active=True, frozen=True, frame=103)
        def call(command, **options):
            if command == 'observe':
                return {'obs':copy.deepcopy(session.last_obs)}
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
        async def actor():
            baseline = session.goal_values()
            session.goals.set({'conditions':[{'field':'elapsed','op':'delta_gte','value':1}]}, baseline)
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
        server = SimpleNamespace(streamable_http_app=lambda **kw:None)
        def movie(*args, **kwargs):
            if not manual:
                self.assertTrue(fixture.hello['active'] and fixture.hello['frozen'])
                self.assertFalse(fixture.hello['record_audio_active'])
            if failure is True:
                raise RecordingError('encoder/ffmpeg fixture failure')
            return fixture.directory/'gameplay.mp4'
        with patch('orchestrator.run.GameSession',return_value=session), \
             patch('orchestrator.run.ensure_container',return_value='isolated-fixture-no-container'), \
             patch('orchestrator.run.build_server',return_value=server), \
             patch('orchestrator.run.add_gate_route'), \
             patch('orchestrator.run.uvicorn.Server',return_value=web), \
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
