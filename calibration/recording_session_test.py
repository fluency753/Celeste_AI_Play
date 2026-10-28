"""OBS 持有录制，Python 主控交接时不重开文件或音频；不连接真实 OBS/游戏。"""
import asyncio
import copy
import time
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

    def test_maintenance_stop_defers_media_work_and_repeat_stop_does_not_resend(self):
        recorder=self.start();self.advance(3)
        with patch('orchestrator.recording.make_pure') as pure,patch('orchestrator.recording_recovery.finalize') as publish:
            result=recorder.stop(defer_postprocessing=True)
            repeated=recorder.stop(defer_postprocessing=True)
        self.assertEqual(result,repeated)
        self.assertTrue(result['encoder_finished'] and result['postprocess_pending'])
        self.assertEqual(result['frames'],3)
        self.assertEqual(self.commands.count(('record',{'action':'stop','marker':False})),1)
        pure.assert_not_called();publish.assert_not_called()
        saved=Recorder.saved_session(self.first)
        self.assertEqual(saved['state'],'finishing')
        self.assertTrue(saved['postprocess_pending'])
        self.assertEqual(Recorder.resume_mode(saved,self.hello),'recover')

    def test_deferred_postprocessing_can_finish_after_transport_closed(self):
        recorder=self.start();self.advance(3)
        def close():recorder.call=Mock(side_effect=RuntimeError('transport closed'))
        recorder.stop(defer_postprocessing=True,on_encoder_finished=close)
        with patch('orchestrator.recording.finished_movie',return_value=self.directory/'gameplay.mp4'),patch('orchestrator.recording.make_pure',return_value={'state':'finished','frames':3,'seconds':.05,'removed':0,'file':'pure'}) as pure:
            result=recorder.stop()
        self.assertEqual(result['state'],'finished')
        pure.assert_called_once()
        recorder.call.assert_not_called()
        self.assertEqual(Recorder.saved_session(self.first)['state'],'finished')

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

    def test_stop_command_failure_keeps_ownership_for_cleanup_retry(self):
        recorder = self.start()
        original = recorder.call
        def fail(command, **args):
            if command == 'record':
                raise OSError('bridge send failed')
            return original(command, **args)
        recorder.call = fail
        with self.assertRaisesRegex(OSError, 'bridge send failed'):
            recorder.stop()
        self.assertTrue(recorder.started)
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'recording')
        recorder.call = original
        self.assertEqual(recorder.stop()['state'], 'finished')
        self.assertFalse(recorder.started)

    def test_asynchronous_start_ignores_previous_terminal_status_until_own_capture_appears(self):
        recorder = Recorder(self.call, RecordOptions(self.directory, 'ffmpeg', '', audio_source='game_nrt'), log=lambda _: None)
        recorder.bind_run(self.first)
        original = recorder.call
        requested = False
        stale_once = True
        def queued(command, **args):
            nonlocal requested, stale_once
            if command == 'hello' and not requested:
                reply = copy.deepcopy(self.hello)
                reply['clock']['plugin'].update(state='finished', dir=str(self.second))
                return reply
            if command == 'record' and args.get('action') == 'start':
                requested = True
                return original(command, **args)
            if command == 'hello' and stale_once:
                stale_once = False
                reply = copy.deepcopy(self.hello)
                reply['clock'].update(lockstep=False)
                reply['clock']['plugin'].update(state='finished', dir=str(self.second), recording=False)
                return reply
            return original(command, **args)
        recorder.call = queued
        with patch('orchestrator.recording.gamewindow.ensure_visible'), patch('orchestrator.recording.time.sleep'):
            recorder.start()
        self.assertTrue(recorder.started)
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'recording')
        self.assertEqual([a['action'] for c, a in self.commands if c == 'record'], ['start'])

    def test_encoder_timeout_is_pending_and_retry_does_not_stop_or_release_twice(self):
        recorder = self.start()
        original = recorder.call
        def finishing(command, **args):
            reply = original(command, **args)
            if command == 'record' and args.get('action') == 'stop':
                self.hello['clock']['plugin']['state'] = 'finishing'
            return reply
        recorder.call = finishing
        release = Mock()
        result = recorder.stop(timeout=0, on_capture_stopped=release)
        self.assertEqual(result['state'], 'finishing')
        self.assertTrue(result['pending'])
        self.assertFalse(recorder.started)
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'finishing')
        with self.assertRaisesRegex(RecordingError, '收尾'):
            Recorder.validate_resume(Recorder.saved_session(self.first), self.hello)
        self.hello['clock']['plugin']['state'] = 'finished'
        result = recorder.stop(timeout=0, on_capture_stopped=release)
        self.assertEqual(result['state'], 'finished')
        self.assertFalse(result['pending'])
        self.assertIs(recorder.stop(on_capture_stopped=release), result)
        release.assert_called_once()
        self.assertEqual([a['action'] for c, a in self.commands if c == 'record'], ['start', 'stop'])

    def test_obs_disconnect_during_stop_returns_pending_without_waiting(self):
        recorder = self.start()
        original = recorder.call
        def disconnected(command, **args):
            reply = original(command, **args)
            if command == 'record' and args.get('action') == 'stop':
                # OBS 崩溃：插件状态停在断开前的"录制中"，时钟端口已断开
                self.hello['clock'].update(connected=False, lockstep=True)
                self.hello['clock']['plugin'].update(state='recording', recording=True)
            return copy.deepcopy(self.hello)
        recorder.call = disconnected
        started = time.monotonic()
        result = recorder.stop(timeout=30)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(result['pending'])
        self.assertEqual(result['state'], 'recording')

    def test_timeout_cannot_claim_finished_while_capture_still_runs(self):
        recorder = self.start()
        original = recorder.call
        def inconsistent(command, **args):
            if command == 'record' and args.get('action') == 'stop':
                self.hello['clock']['plugin']['state'] = 'finished'
                self.hello['clock']['plugin']['recording'] = True
                return copy.deepcopy(self.hello)
            return original(command, **args)
        recorder.call = inconsistent
        release = Mock()
        result = recorder.stop(timeout=0, on_capture_stopped=release)
        self.assertEqual(result['state'], 'recording')
        self.assertTrue(recorder.started)
        release.assert_not_called()
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'recording')
        self.hello['clock']['lockstep'] = False
        self.hello['clock']['plugin']['recording'] = False
        self.assertEqual(recorder.stop(timeout=0, on_capture_stopped=release)['state'], 'finished')
        release.assert_called_once()

    def test_failed_manifest_publication_is_retried_before_caching_completion(self):
        recorder = self.start()
        save = recorder._save_session
        attempts = []
        def interrupted(state):
            attempts.append(state)
            if len(attempts) == 1:
                raise PermissionError('manifest locked')
            return save(state)
        with patch.object(recorder, '_save_session', side_effect=interrupted):
            with self.assertRaisesRegex(PermissionError, 'manifest locked'):
                recorder.stop()
            self.assertIsNone(recorder.result)
            self.assertEqual(Recorder.saved_session(self.first)['state'], 'recording')
            self.assertEqual(recorder.stop()['state'], 'finished')
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'finished')
        self.assertEqual([a['action'] for c, a in self.commands if c == 'record'], ['start', 'stop'])

    def test_completed_recorder_cannot_be_reused_for_another_capture(self):
        recorder = self.start()
        recorder.stop()
        before = list(self.commands)
        with self.assertRaisesRegex(RecordingError, '新的 Recorder'):
            recorder.start()
        self.assertEqual(self.commands, before)

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

    def test_console_release_rejects_foreign_capture_without_abandoning_own_session(self):
        self.start().retain()
        self.hello['clock']['plugin']['dir'] = str(self.second)
        for state in ('recording', 'finishing'):
            with self.subTest(state=state):
                self.hello['clock']['plugin']['state'] = state
                with self.assertRaisesRegex(ValueError, '另一段录像'):
                    self.release_with(SimpleNamespace(call=self.call, close=Mock()))
                self.assertEqual(Recorder.saved_session(self.first)['state'], 'recording')
                self.assertNotIn('deactivate', [c for c, a in self.commands])

    def test_cleanup_persistence_failure_cannot_block_release_of_lost_capture(self):
        self.start().retain()
        self.hello['clock'].update(lockstep=False, plugin={'state': 'idle'})
        self.hello['record_audio_active'] = False
        with patch.object(Recorder, 'abandon_saved', side_effect=PermissionError('manifest locked')):
            result = self.release_with(SimpleNamespace(call=self.call, close=Mock()))
        self.assertTrue(result['released'])
        self.assertIn('未能保存', result['note'])
        self.assertIn('deactivate', [c for c, a in self.commands])

    def test_stop_rechecks_owner_before_sending_and_during_encoder_wait(self):
        from orchestrator.recording import RecordingOwnershipError
        recorder = self.start()
        self.hello['clock']['plugin']['dir'] = str(self.second)
        with self.assertRaises(RecordingOwnershipError):
            recorder.stop(timeout=0)
        self.assertEqual([a['action'] for c, a in self.commands if c == 'record'], ['start'])
        self.hello['clock']['plugin']['dir'] = str(self.directory)
        original = recorder.call
        def takeover(command, **args):
            reply = original(command, **args)
            if command == 'record' and args.get('action') == 'stop':
                self.hello['clock']['plugin'].update(dir=str(self.second), state='finished', frames=999)
            return reply
        recorder.call = takeover
        with self.assertRaises(RecordingOwnershipError):
            recorder.stop(timeout=0)
        self.assertIsNone(recorder.result)
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'recording')

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

    def test_new_recording_resume_finishes_an_encoded_recording_instead_of_abandoning_it(self):
        from orchestrator.run import abandon_recording
        recorder = self.start()
        original = recorder.call
        def finishing(command, **args):
            reply = original(command, **args)
            if command == 'record' and args.get('action') == 'stop':
                self.hello['clock']['plugin']['state'] = 'finishing'
            return reply
        recorder.call = finishing
        recorder.stop(timeout=0)                                   # 上次收尾超时：会话停在 finishing
        saved = Recorder.saved_session(self.first)
        self.assertEqual(saved['state'], 'finishing')
        self.hello['clock']['plugin']['state'] = 'finished'       # 之后 OBS 编码完了同一目录
        client = lambda: SimpleNamespace(call=self.call, close=Mock())
        with patch('tools.bridge_client.BridgeClient', side_effect=lambda **k: client()):
            abandon_recording(47774, saved, self.first, RecordingError('原录像正在收尾'))
        self.assertEqual(Recorder.saved_session(self.first)['state'], 'finished')

    def test_new_recording_resume_abandons_recording_lost_with_obs(self):
        from orchestrator.run import abandon_recording
        self.start().retain()
        saved = Recorder.saved_session(self.first)
        # OBS 崩溃：桥接仍报告断开前"录制中"的状态，时钟端口已断开；不能发停止命令，只能放弃
        self.hello['clock']['connected'] = False
        client = lambda: SimpleNamespace(call=self.call, close=Mock())
        with patch('tools.bridge_client.BridgeClient', side_effect=lambda **k: client()):
            with patch('orchestrator.recording.Recorder.validate_resume'):
                abandon_recording(47774, saved, self.first, RecordingError('原录像已断开'))
        self.assertEqual(json.loads((self.directory/'session.json').read_text(encoding='utf-8'))['state'], 'abandoned')
        self.assertNotIn('stop', [a.get('action') for c, a in self.commands if c == 'record'])

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
        for interrupt, resume, menu in [(True, False,False), (False, False,False), (True, True,False), (False, True,False), (True,False,True)]:
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
                if menu:
                    recorder.retain.side_effect=RuntimeError('菜单正常运行')
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
                         patch('orchestrator.run.ensure_container',return_value='test-container'), \
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
                        if menu:
                            recorder.stop.assert_called_once_with(defer_postprocessing=True)
                        else:
                            recorder.stop.assert_not_called()
                            self.assertTrue(cfg._recording_retained)
                    else:
                        recorder.stop.assert_called_once()
                        recorder.retain.assert_not_called()
                finally:
                    session._frames_log.close();session._decisions_log.close()


if __name__ == '__main__': unittest.main()
