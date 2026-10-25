"""Independent audio selection must fail explicitly instead of recording live BGM."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestrator.recording import Recorder, RecordOptions, RecordingError, options_from


class RecordingAudioTests(unittest.TestCase):
    def recorder(self, folder, call):
        return Recorder(call, RecordOptions(Path(folder), "ffmpeg", "", audio_source="game_nrt"), log=lambda _: None)

    def test_old_bridge_is_rejected_before_starting_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            call = Mock(return_value={"clock": {"connected": True}})
            with self.assertRaisesRegex(RecordingError, "更新桥接"):
                self.recorder(folder, call).start()
            call.assert_called_once_with("hello")

    @patch("orchestrator.recording.gamewindow.ensure_visible")
    def test_new_mode_is_forwarded_and_keeps_live_music_enabled(self, _visible):
        with tempfile.TemporaryDirectory() as folder:
            hello = {"record_audio_nrt": True, "record_audio_tracks": 2, "clock": {
                "connected": True, "lockstep": True, "plugin": {"state": "idle", "audio_nrt": True, "audio_tracks": 2}}}
            def respond(command, **kwargs):
                if command == 'record' and kwargs.get('action') == 'start':
                    hello['clock']['plugin']['state'] = 'recording'
                return hello
            call = Mock(side_effect=respond)
            recorder = self.recorder(folder, call)
            recorder.opts.continuous_music = True
            recorder.start()
            start = next(c for c in call.call_args_list if c.args == ("record",))
            self.assertEqual(start.kwargs["audio_source"], "game_nrt")
            self.assertTrue(start.kwargs["continuous_music"])
            self.assertTrue(recorder.started)

    @patch("orchestrator.recording.gamewindow.ensure_visible")
    def test_plugin_failure_releases_secondary_mixer(self, _visible):
        with tempfile.TemporaryDirectory() as folder:
            call = Mock(return_value={"record_audio_nrt": True, "record_audio_tracks": 2, "clock": {
                "connected": True, "plugin": {"state": "failed", "error": "mapping unavailable", "audio_nrt": True, "audio_tracks": 2}}})
            with self.assertRaisesRegex(RecordingError, "mapping unavailable"):
                self.recorder(folder, call).start()
            call.assert_called_with("record", action="stop")

    def test_mode_loaded_from_config(self):
        with patch("orchestrator.recording.load_config", return_value={"audio_source": "game_nrt"}), \
             patch("orchestrator.recording.find_ffmpeg", return_value="ffmpeg"):
            self.assertEqual(options_from(Path("run")).audio_source, "game_nrt")

    def test_mixer_diagnostics_survive_recording_finalization(self):
        import json
        with tempfile.TemporaryDirectory() as folder:
            diagnostics = {'full': {'created': 2000, 'tracked': 1, 'peak_tracked': 22}}
            call = Mock(return_value={'record_audio_diagnostics': diagnostics, 'clock': {
                'lockstep': False, 'plugin': {'state': 'finished', 'frames': 0}}})
            recorder = self.recorder(folder, call)
            recorder.started = True
            result = recorder.stop()
            self.assertEqual(result['audio_diagnostics'], diagnostics)
            saved = json.loads((Path(folder)/'recording.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['audio_diagnostics'], diagnostics)

    def test_game_release_precedes_encoding_and_waits_until_capture_has_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder=self.recorder(folder,Mock());recorder.started=True
            (Path(folder)/'gameplay.mkv').touch()
            states=[({'lockstep':True},{'state':'finishing','recording':False}),
                    ({'lockstep':False},{'state':'finishing','recording':True}),
                    ({'lockstep':False},{'state':'finishing','recording':False}),
                    ({'lockstep':False},{'state':'finished','recording':False,'frames':1})]
            events=[]
            def state():
                item=states.pop(0);events.append('capture:'+str(item[0]['lockstep'])+':'+str(item[1]['recording']));return item
            def pure(*args):
                self.assertEqual(events.count('release'),1)
                events.append('encoding');return {}
            with patch.object(recorder,'_plugin',side_effect=state),patch('orchestrator.recording.time.sleep'),patch('orchestrator.recording.make_pure',side_effect=pure):
                recorder.stop(on_capture_stopped=lambda:events.append('release'))
            self.assertEqual(events,['capture:True:False','capture:False:True','capture:False:False','release','capture:False:False','encoding'])

    def test_old_plugin_is_rejected_before_starting_secondary_mixer(self):
        with tempfile.TemporaryDirectory() as folder:
            call = Mock(return_value={"record_audio_nrt": True, "record_audio_tracks": 2, "clock": {"connected": True, "plugin": {}}})
            with self.assertRaisesRegex(RecordingError, "更新插件"):
                self.recorder(folder, call).start()
            call.assert_called_once_with("hello")

    def test_single_track_bridge_or_plugin_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            old_bridge = Mock(return_value={"record_audio_nrt": True, "clock": {
                "connected": True, "plugin": {"audio_nrt": True, "audio_tracks": 2}}})
            with self.assertRaisesRegex(RecordingError, "更新桥接"):
                self.recorder(folder, old_bridge).start()
            old_plugin = Mock(return_value={"record_audio_nrt": True, "record_audio_tracks": 2, "clock": {
                "connected": True, "plugin": {"audio_nrt": True}}})
            with self.assertRaisesRegex(RecordingError, "更新插件"):
                self.recorder(folder, old_plugin).start()
            old_plugin.assert_called_once_with("hello")


class PureCutAudioTests(unittest.TestCase):
    """纯享版声音：有纯享版音轨 audio_play.wav 时直接用（音乐连贯），没有时从 audio.wav 裁剪。"""

    def _write(self, folder, ticks, play_frames):
        import numpy as np
        from orchestrator.purecut import _wav_header
        folder = Path(folder)
        (folder / "frames.csv").write_text("index,game_frame,chapter_ticks,obs_ts_ns\n" + "".join(
            f"{i},{i},{t},{i}\n" for i, t in enumerate(ticks)), encoding="utf-8")
        full = np.arange(len(ticks) * 800 * 2, dtype="<f4")
        (folder / "audio.wav").write_bytes(_wav_header(full.nbytes) + full.tobytes())
        if play_frames is not None:
            play = -np.arange(play_frames * 800 * 2, dtype="<f4")
            (folder / "audio_play.wav").write_bytes(_wav_header(play.nbytes) + play.tobytes())
        (folder / "video.mkv").touch()

    def _make(self, folder):
        from orchestrator import purecut
        used = []
        def run(cmd, log_path):
            if "-map" in cmd:
                used.append(Path(cmd[cmd.index("-i", cmd.index("-i") + 1) + 1]).name)
        with patch.object(purecut, "_run", side_effect=run):
            return purecut.make_pure(Path(folder), "ffmpeg", "", log=lambda _: None), used

    def test_play_track_is_used_when_it_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, [0, 1, -1, -1, 2, 3], play_frames=4)
            result, used = self._make(folder)
            self.assertEqual(result["audio"], "audio_play.wav")
            self.assertEqual(used, ["audio_play.wav"])
            self.assertFalse((Path(folder) / "audio_pure_cut.wav").exists())

    def test_falls_back_to_cutting_the_full_track(self):
        with tempfile.TemporaryDirectory() as folder:
            self._write(folder, [0, 1, -1, -1, 2, 3], play_frames=None)
            result, used = self._make(folder)
            self.assertEqual(result["audio"], "audio.wav 裁剪")
            self.assertEqual(used, ["audio_pure_cut.wav"])
        with tempfile.TemporaryDirectory() as folder:   # 长度对不上也不用它
            self._write(folder, [0, 1, -1, -1, 2, 3], play_frames=5)
            result, used = self._make(folder)
            self.assertEqual(used, ["audio_pure_cut.wav"])


if __name__ == "__main__":
    unittest.main()
