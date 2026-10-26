"""OBS 持有录制，Python 主控交接时不重开文件或音频；不连接真实 OBS/游戏。"""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from orchestrator.recording import Recorder, RecordOptions, RecordingError


class RecordingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.first = self.root/'first'; self.first.mkdir()
        self.second = self.root/'second'; self.second.mkdir()
        self.directory = self.first/'recording'
        self.commands = []
        self.hello = {'record_audio_nrt': True, 'record_audio_tracks': 2, 'record_audio_active': False,
                      'record_audio_frames': 0, 'clock': {'connected': True, 'lockstep': False,
                      'plugin': {'state': 'idle', 'frames': 0, 'audio_nrt': True, 'audio_tracks': 2}}}

    def tearDown(self):
        self.temp.cleanup()

    def call(self, command, **args):
        self.commands.append((command, args))
        if command == 'record':
            plugin = self.hello['clock']['plugin']
            if args['action'] == 'start':
                plugin.update(state='recording', dir=args['dir'], frames=0)
                self.hello['clock']['lockstep'] = True
                self.hello['record_audio_active'] = True
                (self.directory/'video.mkv').write_bytes(b'encoder-open\n')
                (self.directory/'frames.csv').write_text('index,game_frame,chapter_ticks,obs_ts_ns\n', encoding='utf-8')
            else:
                plugin.update(state='finished', recording=False)
                self.hello['clock']['lockstep'] = False
                self.hello['record_audio_active'] = False
        return copy.deepcopy(self.hello)

    def advance(self, count):
        plugin = self.hello['clock']['plugin']
        with (self.directory/'video.mkv').open('ab') as video, (self.directory/'frames.csv').open('a', encoding='utf-8') as csv:
            for i in range(plugin['frames'], plugin['frames']+count):
                video.write(f'frame-{i}\n'.encode())
                csv.write(f'{i},{100+i},{i*166667},{i*16666667}\n')
        plugin['frames'] += count
        self.hello['record_audio_frames'] += count

    def start(self):
        recorder = Recorder(self.call, RecordOptions(self.directory, 'ffmpeg', '', audio_source='game_nrt'), log=lambda _: None)
        recorder.bind_run(self.first)
        with patch('orchestrator.recording.gamewindow.ensure_visible'):
            recorder.start()
        return recorder

    def test_encoder_audio_and_frame_index_survive_multiple_controller_handoffs(self):
        recorder = self.start()
        self.advance(5)
        before = (self.directory/'video.mkv').read_bytes()
        held = recorder.retain()
        self.assertFalse(recorder.started)
        self.assertTrue(held['pending'])
        self.assertTrue(self.hello['record_audio_active'])
        self.assertTrue(self.hello['clock']['lockstep'])
        self.assertEqual(self.hello['record_audio_frames'], 5)
        self.assertEqual((self.directory/'video.mkv').read_bytes(), before)
        saved = Recorder.saved_session(self.first)
        next_recorder = Recorder.adopt(self.call, saved, self.second, log=lambda _: None)
        self.assertEqual(next_recorder.opts.dir.resolve(), self.directory.resolve())
        self.advance(7)
        next_recorder.retain()
        self.assertEqual(Recorder.saved_session(self.second)['id'], saved['id'])
        final = Recorder.adopt(self.call, Recorder.saved_session(self.second), self.second, log=lambda _: None)
        result = final.stop()
        actions = [a['action'] for command, a in self.commands if command == 'record']
        self.assertEqual(actions, ['start', 'stop'])
        self.assertEqual(result['frames'], 12)
        self.assertEqual(result['check']['missing_frames'], 0)
        self.assertEqual(result['check']['repeats'], 0)
        self.assertEqual(self.hello['record_audio_frames'], 12)
        self.assertTrue((self.directory/'video.mkv').read_bytes().startswith(before))
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'finished')
        self.assertEqual(Recorder.saved_session(self.second)['state'], 'finished')
        self.assertFalse((self.second/'recording').exists())

    def test_lost_or_mismatched_recorder_is_rejected_without_touching_encoder(self):
        self.start().retain()
        saved = Recorder.saved_session(self.first)
        before = len(self.commands)
        for mutation in ['different-directory', 'disconnected', 'stopped', 'no-audio']:
            hello = copy.deepcopy(self.hello)
            if mutation == 'different-directory': hello['clock']['plugin']['dir'] = str(self.second)
            if mutation == 'disconnected': hello['clock']['connected'] = False
            if mutation == 'stopped': hello['clock']['plugin']['state'] = 'finished'
            if mutation == 'no-audio': hello['record_audio_active'] = False
            with self.subTest(mutation=mutation), self.assertRaises(RecordingError):
                Recorder.validate_resume(saved, hello)
        with self.assertRaises(RecordingError):
            Recorder.validate_resume(None, self.hello)
        self.assertEqual(len(self.commands), before)

    def test_new_start_cannot_overwrite_existing_recording(self):
        self.start()
        recorder = Recorder(self.call, RecordOptions(self.second/'recording', 'ffmpeg', ''), log=lambda _: None)
        before = [c for c in self.commands if c[0] == 'record']
        with self.assertRaisesRegex(RecordingError, '其他会话'):
            recorder.start()
        self.assertEqual([c for c in self.commands if c[0] == 'record'], before)

    def test_explicit_release_finalizes_retained_video(self):
        self.start().retain()
        self.advance(2)
        result = Recorder.finish_saved(self.call, Recorder.saved_session(self.first), self.first, log=lambda _: None)
        self.assertEqual(result['state'], 'finished')
        self.assertEqual(result['frames'], 2)
        self.assertEqual([a['action'] for c,a in self.commands if c=='record'], ['start','stop'])

    def test_startup_link_preserves_original_recording_identity(self):
        self.start().retain()
        saved = Recorder.saved_session(self.first)
        Recorder.link_saved(saved, self.second)
        self.assertEqual(Recorder.saved_session(self.second)['id'], saved['id'])
        self.hello['clock']['plugin']['state'] = 'finished'
        with self.assertRaises(RecordingError):
            Recorder.adopt(self.call, saved, self.second, log=lambda _: None)

    def test_corrupted_identity_and_foreign_release_are_rejected(self):
        self.start().retain()
        saved = Recorder.saved_session(self.first)
        self.hello['clock']['plugin']['dir'] = str(self.second)
        with self.assertRaises(RecordingError):
            Recorder.finish_saved(self.call, saved, self.first, log=lambda _: None)
        self.assertEqual([a['action'] for c, a in self.commands if c == 'record'], ['start'])
        pointer = self.first/'recording_session.json'
        data = json.loads(pointer.read_text(encoding='utf-8'))
        data['id'] = 'wrong-session'
        pointer.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaises(RecordingError):
            Recorder.saved_session(self.first)

    def test_console_release_finalizes_before_deactivating(self):
        from overlay import console_api
        self.start().retain()
        client = SimpleNamespace(call=self.call, close=Mock())
        with patch.object(console_api, 'orchestrator_status', return_value={'running': False}), \
             patch.object(console_api, 'latest_run', return_value=self.first), \
             patch('tools.bridge_client.BridgeClient', return_value=client):
            self.assertTrue(console_api.release()['released'])
        commands = [c for c, a in self.commands]
        self.assertLess(max(i for i, c in enumerate(commands) if c == 'record'), commands.index('deactivate'))
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'finished')
        client.close.assert_called_once()

    def test_recording_is_not_retained_while_menus_run(self):
        """游戏停在菜单或地图上（不冻结）时不保留录制：否则中断期间的菜单会被一直录进同一份录像。"""
        recorder = self.start()
        self.hello['frozen'] = False
        with self.assertRaisesRegex(RecordingError, '不在冻结'):
            recorder.retain()
        self.assertEqual([a['action'] for c, a in self.commands if c == 'record'], ['start'])
        self.hello['frozen'] = True
        self.assertTrue(recorder.retain()['pending'])

    def release_with(self, client):
        from overlay import console_api
        with patch.object(console_api, 'orchestrator_status', return_value={'running': False}), \
             patch.object(console_api, 'latest_run', return_value=self.first), \
             patch('tools.bridge_client.BridgeClient', return_value=client):
            return console_api.release()

    def test_console_release_hands_back_game_when_retained_recording_is_lost(self):
        self.start().retain()
        self.hello['clock'].update(lockstep=False)
        self.hello['clock']['plugin'] = {'state': 'idle'}          # OBS 关了或崩溃
        self.hello['record_audio_active'] = False
        result = self.release_with(SimpleNamespace(call=self.call, close=Mock()))
        self.assertTrue(result['released'])
        self.assertIn('已放弃', result['note'])
        self.assertIn('deactivate', [c for c, a in self.commands])
        self.assertEqual(json.loads((self.directory/'session.json').read_text(encoding='utf-8'))['state'], 'abandoned')

    def test_console_release_hands_back_game_when_session_pointer_is_broken(self):
        self.start().retain()
        self.hello['clock'].update(lockstep=False)
        self.hello['clock']['plugin'] = {'state': 'idle'}
        (self.first/'recording_session.json').write_text('{broken', encoding='utf-8')
        result = self.release_with(SimpleNamespace(call=self.call, close=Mock()))
        self.assertTrue(result['released'])
        self.assertIn('读不出来', result['note'])
        self.assertIn('deactivate', [c for c, a in self.commands])

    def test_console_release_releases_game_as_soon_as_capture_stops(self):
        self.start().retain()
        order = []
        with patch('orchestrator.recording.make_pure', side_effect=lambda *a, **k: order.append('pure') or {}):
            def call(command, **args):
                order.append(command)
                return self.call(command, **args)
            (self.directory/'gameplay.mp4').write_bytes(b'x')
            self.release_with(SimpleNamespace(call=call, close=Mock()))
        self.assertLess(order.index('deactivate'), order.index('pure'))   # 不等纯享版生成就交还游戏

    def test_new_recording_resume_abandons_lost_recording_only(self):
        from orchestrator.run import abandon_recording
        self.start().retain()
        saved = Recorder.saved_session(self.first)
        client = lambda: SimpleNamespace(call=self.call, close=Mock())
        with patch('tools.bridge_client.BridgeClient', side_effect=lambda **k: client()):
            # 独立音频停了、OBS 还开着同一目录：先收尾原录像，再另起
            self.hello['record_audio_active'] = False
            abandon_recording(47774, saved, self.first, RecordingError('原录像的独立音频已停止'))
            self.assertEqual(Recorder.saved_session(self.first)['state'], 'finished')
            # OBS 关了：标成 abandoned
            self.hello['clock']['plugin'] = {'state': 'idle'}
            abandon_recording(47774, {**saved, 'state': 'recording'}, self.first, RecordingError('原录像已断开'))
            self.assertEqual(json.loads((self.directory/'session.json').read_text(encoding='utf-8'))['state'], 'abandoned')
            # OBS 在录别的会话：不另开录像
            self.hello['clock']['plugin'] = {'state': 'recording', 'dir': str(self.second)}
            with self.assertRaises(RecordingError):
                abandon_recording(47774, None, self.first, RecordingError('原录像已断开'))

    def test_resume_menu_is_recorded_at_normal_speed_without_restarting(self):
        from orchestrator.config import RunConfig
        from orchestrator.run import enter_save, check_resume_position
        self.start().retain()
        cfg = RunConfig(agent='test', save='test', model='test')
        cfg._recording_resume_manifest = Recorder.saved_session(self.first)
        cfg._keep_frozen = True
        client = SimpleNamespace(call=Mock(), close=Mock())
        obs = {'scene': 'Level', 'save': {'name': 'test', 'slot': 7}, 'level': {}}
        with patch('tools.bridge_client.BridgeClient', return_value=client), \
             patch('tools.enter_save.enter_save', return_value=obs) as macro:
            enter_save(cfg)
            self.assertEqual(macro.call_args.kwargs['menu_speed'], 1)
            macro.side_effect = RuntimeError('menu failed')
            with self.assertRaises(RuntimeError):
                enter_save(cfg)
            client.call.assert_not_called()
            client.call.side_effect = RuntimeError('observe failed')
            with self.assertRaises(RuntimeError):
                check_resume_position(cfg)
        self.assertEqual([a['action'] for c,a in self.commands if c == 'record'], ['start'])
        self.assertFalse(any(call.args[0] == 'deactivate' for call in client.call.call_args_list))
        self.assertTrue(self.hello['record_audio_active'])

    def test_failed_startup_preserves_old_recording_and_latest_run(self):
        from orchestrator import run
        from orchestrator.config import RunConfig
        self.start().retain()
        cfg = RunConfig(agent='test', save='test', model='test')
        cfg.hud_mod = False
        args = SimpleNamespace(prepared=None, previous={'dir': self.first}, record=True, no_agent=True)
        runs = self.root/'runs'
        runs.mkdir()
        latest = runs/'latest.txt'
        latest.write_text(str(self.second), encoding='utf-8')
        with patch.object(run, 'REPO_ROOT', self.root), \
             patch.object(run, 'parse_args', return_value=(cfg, args)), \
             patch.object(run, 'port_free', return_value=True), \
             patch('orchestrator.bridge_requirements.preflight_bridge', return_value=self.hello), \
             patch.object(run, 'make_run_dir', return_value=self.second), \
             patch.object(run, 'start_mod_hud', side_effect=RuntimeError('startup failed')), \
             patch.object(run, 'close_obs') as close_obs, \
             patch('tools.bridge_client.BridgeClient') as bridge:
            with self.assertRaises(RuntimeError):
                run.main()
        self.assertEqual(latest.read_text(encoding='utf-8'), str(self.first))
        self.assertTrue(cfg._keep_frozen)
        close_obs.assert_not_called()
        bridge.assert_not_called()
        self.assertEqual([a['action'] for c,a in self.commands if c == 'record'], ['start'])


class RunRecordingLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_retains_on_interrupt_but_finalizes_on_user_stop(self):
        from orchestrator.config import RunConfig
        from orchestrator.game_session import GameSession
        from orchestrator.metrics import Metrics
        from orchestrator.run import _run_connected_session
        for interrupt, resume in [(True, False), (False, False), (True, True), (False, True)]:
            with self.subTest(interrupt=interrupt, resume=resume), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                cfg = RunConfig(agent='test', save='test', model='test', engine='codex', runtime='host', sim=False)
                if resume:
                    cfg._recording_resume_manifest = {'state': 'recording'}
                    cfg._keep_frozen = True
                metrics = Metrics(directory/'metrics.json', 'test', 'test', 'high', 600)
                session = GameSession(cfg, directory, metrics)
                session.connect = Mock(return_value={'scene': 'Level'})
                session.bridge_call = Mock()
                args = SimpleNamespace(prepared=None, fresh_session=False, no_agent=False, record=True,
                                       ffmpeg=None, record_video_args=None, audio_offset_ms=None,
                                       record_marker=False, start_paused=False)
                recorder = Mock(started=True)
                recorder.retain.return_value = {'state':'retained','pending':True}
                recorder.stop.return_value = {'state':'finished'}
                async def run():
                    if not interrupt:
                        session.done = True
                        session.done_reason = '用户停止'
                    return '用户中断' if interrupt else '用户停止'
                runner = SimpleNamespace(run=run, interrupted_for_resume=interrupt, turns=1, session_id='session')
                web = SimpleNamespace(started=True, should_exit=False)
                async def serve():
                    while not web.should_exit: await asyncio.sleep(.001)
                web.serve = serve
                server = SimpleNamespace(streamable_http_app=lambda **kw: None)
                playback = SimpleNamespace(collect=False,close=Mock())
                try:
                    with patch('orchestrator.run.Recorder',return_value=recorder) as recorder_class, \
                         patch('orchestrator.run.build_server',return_value=server), \
                         patch('orchestrator.run.add_gate_route'), \
                         patch('orchestrator.run.uvicorn.Server',return_value=web), \
                         patch('orchestrator.run.Playback',return_value=playback), \
                         patch('orchestrator.runtimes.runtime',return_value=SimpleNamespace(runner=lambda *a,**kw:runner)):
                        recorder_class.adopt.return_value = recorder
                        await _run_connected_session(cfg,args,directory,None,None,None,metrics,None,None,session)
                    self.assertEqual(recorder_class.adopt.called, resume)
                    if interrupt:
                        recorder.retain.assert_called_once()
                        recorder.stop.assert_not_called()
                        self.assertTrue(cfg._recording_retained)
                    else:
                        recorder.stop.assert_called_once()
                        recorder.retain.assert_not_called()
                finally:
                    session._frames_log.close();session._decisions_log.close()


if __name__ == '__main__': unittest.main()
