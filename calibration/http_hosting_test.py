"""Exercise the three real HTTP consumers without model, game or OBS processes."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from starlette.applications import Starlette

from calibration.http_fixture import bound_server
from calibration import recording_session_test
from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.http_service import http_service
from orchestrator.lifecycle import host_runner
from orchestrator.live import LiveStatus
from orchestrator.metrics import Metrics
from orchestrator.playback import Playback
from orchestrator.recording import Recorder, RecordOptions
from orchestrator.run import _run_session


async def gate_request(port, data):
    content = json.dumps(data).encode()
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    try:
        writer.write(f'POST /gate HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n'.encode() + content)
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 2)
        return json.loads(reply.partition(b'\r\n\r\n')[2])
    finally:
        writer.close()
        await writer.wait_closed()


class HostingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch('orchestrator.config.REPO_ROOT', self.root))
        self.enterContext(patch('orchestrator.personal_prompts.STORE', self.root/'personal.json'))
        self.enterContext(patch('orchestrator.prompting.OVERRIDES', self.root/'prompts'))

    def configuration(self, engine):
        return RunConfig(agent='http-test', save='test', model='test', engine=engine,
                         runtime='host', mcp_port=0, sim=False, study=False)

    def metrics(self, directory=None):
        return Metrics((directory or self.root)/'metrics.json', 'test', 'test', 'high', 600)

    async def test_game_free_host_uses_bound_port_and_closes_its_real_http_listener(self):
        cfg = self.configuration('claude')
        with patch('orchestrator.runtimes.Runtime.token', return_value='fixture'):
            async with host_runner(cfg, self.root, self.metrics(), None, chat_only=True) as runner:
                port = cfg.mcp_port
                self.assertGreater(port, 0)
                result = await gate_request(port, {'tool_name': 'Read', 'session_id': runner.session_id})
                self.assertEqual(result['decision'], 'deny')  # No active model turn owns this request.
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', port)

    async def test_game_free_startup_failure_closes_events_and_preserves_original_error(self):
        expected = ValueError('fixture lifespan startup')
        @asynccontextmanager
        async def lifespan(application):
            raise expected
            yield
        server = SimpleNamespace(streamable_http_app=lambda **kw: Starlette(lifespan=lifespan))
        closed = []
        original_close = ClaudeRunner.close_events
        def close(runner):
            original_close(runner)
            closed.append(runner)
        with patch('orchestrator.server.build_server', return_value=server), \
                patch('orchestrator.runtimes.Runtime.token', return_value='fixture'), \
                patch('orchestrator.agent.ClaudeRunner.close_events', close):
            with self.assertRaises(ValueError) as caught:
                async with host_runner(self.configuration('claude'), self.root, self.metrics(), None):
                    self.fail('Failed host must not be yielded')
        self.assertIs(caught.exception, expected)
        self.assertEqual(len(closed), 1)

    async def test_native_gate_reuses_only_a_live_owned_service_and_releases_port(self):
        cfg = self.configuration('codex')
        runner = CodexRunner(cfg, self.root, None, self.metrics(), 'fixture', chat_only=True)
        try:
            with patch('orchestrator.http_service.http_service', wraps=http_service) as start:
                await asyncio.wait_for(asyncio.gather(runner._start_native_gate(),
                                                     runner._start_native_gate()), 3)
                self.assertEqual(start.call_count, 1)
            task, context, port = runner._native_web_task, runner._native_service, cfg.mcp_port
            self.assertEqual(runner.rpc.environment['MCP_PORT'], str(port))
            await runner._start_native_gate()
            self.assertIs(runner._native_web_task, task)
            self.assertIs(runner._native_service, context)
            denied = await gate_request(port, {'engine': 'codex', 'token': 'bad'})
            self.assertEqual(denied['decision'], 'deny')
            await runner._close_native_gate()
            self.assertTrue(task.done())
            with self.assertRaises(OSError):
                await asyncio.open_connection('127.0.0.1', port)
            # An exited service is not silently treated as ready on reconnect.
            await runner._start_native_gate()
            runner._native_web_task.cancel()
            await asyncio.gather(runner._native_web_task, return_exceptions=True)
            with self.assertRaisesRegex(RuntimeError, '已经退出'):
                await asyncio.wait_for(runner._start_native_gate(), 3)
            self.assertIsNone(runner._native_service)
        finally:
            await runner._close_native_gate()

    async def test_native_rpc_start_failure_still_closes_http_and_events(self):
        cfg = self.configuration('codex')
        runner = CodexRunner(cfg, self.root, None, self.metrics(), 'fixture', chat_only=True)
        expected = ValueError('RPC startup failed after HTTP ready')
        async def start():
            await runner._start_native_gate()
            raise expected
        runner.rpc.start = AsyncMock(side_effect=start)
        runner.rpc.close = AsyncMock(side_effect=OSError('RPC close failed'))
        with self.assertRaises(ValueError) as caught:
            await runner.run()
        self.assertIs(caught.exception, expected)
        self.assertTrue(runner._native_web_task.done())
        self.assertIsNone(runner._native_service)
        self.assertTrue(runner._events.closed)
        self.assertTrue(any('RPC close failed' in note for note in caught.exception.__notes__))

    async def exercise_playing_host(self, ownership, *, cancel=False, stop_failure=False,
                                    body_failure=False, adopt_failure=False, other_close_failure=False,
                                    finish_cancel=False, summary_failure=False):
        fixture = recording_session_test.RecordingSessionTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        cfg = RunConfig(agent='http-owner', save='test', model='test', engine='codex', runtime='host',
                        mcp_port=0, sim=False, playback=False, recording_mode='continuous')
        metrics = self.metrics(fixture.first)
        persisted = {'mcp_port': 0, 'engine': 'codex', 'rules_delivered': {'preserved': True}}
        (fixture.first/'run_state.json').write_text(json.dumps(persisted), encoding='utf-8')
        if summary_failure:
            metrics.snapshot = Mock(side_effect=ValueError('summary snapshot failed'))
        with patch('orchestrator.config.REPO_ROOT', fixture.root):
            session = GameSession(cfg, fixture.first, metrics)
        session.client = SimpleNamespace(call=fixture.call, close=Mock())
        session.connect = Mock(return_value={'scene': 'Level', 'frame': 10})
        recorder = None
        if ownership in ('incoming', 'saved'):
            recorder = fixture.start()
            fixture.advance(2)
            if ownership == 'saved':
                recorder.retain()
                cfg._recording_resume_manifest = Recorder.saved_session(fixture.first)
                cfg._keep_frozen = True
                recorder = None
        expected = ValueError('original HTTP startup failure')
        stop_error = RuntimeError('HTTP stop failure')
        body_error = ValueError('original runner failure')
        settlement_cancel = asyncio.CancelledError('settlement cancelled')
        if finish_cancel:
            session.finish_at_results = Mock(side_effect=settlement_cancel)
        ready = asyncio.Event()
        @asynccontextmanager
        async def lifespan(application):
            ready.set()
            if cancel:
                await asyncio.Event().wait()
            raise expected
            yield
        broken_app = Starlette(lifespan=lifespan)
        http = SimpleNamespace(streamable_http_app=lambda **kw: broken_app)
        def build_server(*args):
            state_path = fixture.first/'run_state.json'
            state = json.loads(state_path.read_text(encoding='utf-8'))
            state['rules_delivered'] = persisted['rules_delivered']
            state_path.write_text(json.dumps(state), encoding='utf-8')
            return http
        resources = {}
        def playback(*args, **kwargs):
            obj = Playback(*args, **kwargs)
            resources['playback'], resources['playback_thread'] = obj, obj._worker
            if other_close_failure:
                close = obj.close
                def failing():
                    close()
                    raise RuntimeError('playback close failure')
                obj.close = failing
            return obj
        def live(*args, **kwargs):
            obj = LiveStatus(*args, **kwargs)
            resources['live'] = obj
            if other_close_failure:
                close = obj.close
                def failing():
                    close()
                    raise RuntimeError('live close failure')
                obj.close = failing
            return obj
        async def actor():
            if body_failure:
                raise body_error
            session.done, session.done_reason = True, '章节完成' if finish_cancel else '用户停止'
            return '用户停止'
        runner = SimpleNamespace(run=actor, interrupted_for_resume=False, turns=1, session_id='fixture')
        def short_http(app, **options):
            return http_service(app, **options, start_timeout=.5, stop_timeout=.02)
        args = SimpleNamespace(prepared=None, fresh_session=False, no_agent=False, record=True,
                               ffmpeg='ffmpeg', record_video_args=None, audio_offset_ms=None,
                               record_marker=False, start_paused=False)
        from orchestrator.runtimes import runtime
        adapter = runtime('codex')
        with patch('orchestrator.run.GameSession', return_value=session), \
                patch('orchestrator.run.build_server', side_effect=build_server), \
                patch('orchestrator.run.add_gate_route'), \
                patch('orchestrator.run.http_service', side_effect=short_http), \
                patch('orchestrator.run.Playback', side_effect=playback), \
                patch('orchestrator.run.LiveStatus', side_effect=live), \
                patch('orchestrator.run.ensure_container', return_value='fixture-container') as container, \
                patch('orchestrator.run.options_from', return_value=RecordOptions(fixture.directory, 'ffmpeg', '', audio_source='game_nrt')), \
                patch('orchestrator.recording.gamewindow.ensure_visible'), \
                patch('orchestrator.recording.finished_movie', return_value=fixture.directory/'gameplay.mp4'), \
                patch('orchestrator.recording.make_pure', return_value={'state':'finished','frames':2,'seconds':.03,'removed':0,'file':'pure'}), \
                patch('orchestrator.runtimes.runtime', return_value=SimpleNamespace(runner=lambda *a, **kw: runner, configure=adapter.configure)), \
                patch.object(Recorder, 'adopt', side_effect=expected if adopt_failure else None) as adopt:
            fake = None
            if stop_failure or body_failure or adopt_failure or finish_cancel:
                fake = bound_server(SimpleNamespace(started=True, should_exit=False))
                async def serve():
                    while not fake.should_exit:
                        await asyncio.sleep(.001)
                    if stop_failure:
                        raise stop_error
                fake.serve = serve
            factory = patch('orchestrator.http_service.uvicorn.Server', return_value=fake) if fake else patch.dict({}, {})
            with factory:
                task = asyncio.create_task(_run_session(cfg, args, fixture.first, None, None, None, metrics, recorder))
                if cancel:
                    await asyncio.wait_for(ready.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    chosen = body_error if body_failure else settlement_cancel if finish_cancel else stop_error if stop_failure else expected
                    with self.assertRaises(type(chosen)) as caught:
                        await task
                    self.assertIs(caught.exception, chosen)
                    self.assertFalse(any('generator didn' in note
                                         for note in getattr(chosen, '__notes__', ())))
                    if body_failure and stop_failure:
                        self.assertTrue(any('HTTP stop failure' in note for note in chosen.__notes__))
                    if summary_failure:
                        self.assertTrue(any('summary snapshot failed' in note for note in chosen.__notes__))
                    if other_close_failure:
                        for text in ('playback close failure', 'live close failure'):
                            self.assertTrue(any(text in note for note in chosen.__notes__))
            if fake:
                from orchestrator.runtimes import config_from_state
                state = json.loads((fixture.first/'run_state.json').read_text(encoding='utf-8'))
                self.assertGreater(state['mcp_port'], 0)
                self.assertEqual(state['mcp_port'], cfg.mcp_port)
                self.assertEqual(state['rules_delivered'], persisted['rules_delivered'])
                self.assertEqual(config_from_state(state).mcp_port, cfg.mcp_port)
            if not (stop_failure or body_failure or finish_cancel):
                container.assert_not_called()
            if ownership == 'saved':
                self.assertTrue(cfg._keep_frozen)
                if not adopt_failure:
                    adopt.assert_not_called()
                self.assertTrue(fixture.hello['record_audio_active'])
                self.assertEqual([a['action'] for c, a in fixture.commands if c == 'record'], ['start'])
                self.assertNotIn('deactivate', [c for c, _ in fixture.commands])
            else:
                self.assertFalse(fixture.hello['record_audio_active'])
                self.assertEqual([a['action'] for c, a in fixture.commands if c == 'record'], ['start', 'stop'])
            session.client = None  # The outer lifecycle has already closed this exact fixture connection.
        self.assertTrue(resources['playback']._closed)
        self.assertFalse(resources['playback_thread'].is_alive())
        self.assertFalse(resources['live']._thread.is_alive())

    async def test_playing_startup_failure_cleans_current_capture_but_never_adopts_old_capture(self):
        for ownership in ('incoming', 'fresh', 'saved'):
            with self.subTest(ownership=ownership):
                await self.exercise_playing_host(ownership)

    async def test_playing_startup_cancellation_has_same_capture_ownership(self):
        for ownership in ('incoming', 'fresh', 'saved'):
            with self.subTest(ownership=ownership):
                await self.exercise_playing_host(ownership, cancel=True)

    async def test_http_stop_failure_does_not_skip_real_capture_stop(self):
        await self.exercise_playing_host('incoming', stop_failure=True)

    async def test_primary_runner_error_survives_all_other_cleanup_failures(self):
        await self.exercise_playing_host('incoming', stop_failure=True, body_failure=True, other_close_failure=True)

    async def test_settlement_cancellation_closes_http_threads_and_real_capture(self):
        await self.exercise_playing_host('incoming', finish_cancel=True)

    async def test_summary_failure_preserves_original_runner_error(self):
        await self.exercise_playing_host('incoming', body_failure=True, summary_failure=True)

    async def test_adopt_failure_after_ready_preserves_old_capture(self):
        await self.exercise_playing_host('saved', adopt_failure=True)


if __name__ == '__main__':
    unittest.main()
