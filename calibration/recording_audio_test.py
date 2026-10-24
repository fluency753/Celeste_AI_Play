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
            call = Mock(return_value={"record_audio_nrt": True, "clock": {
                "connected": True, "lockstep": True, "plugin": {"state": "recording", "audio_nrt": True}}})
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
            call = Mock(return_value={"record_audio_nrt": True, "clock": {
                "connected": True, "plugin": {"state": "failed", "error": "mapping unavailable", "audio_nrt": True}}})
            with self.assertRaisesRegex(RecordingError, "mapping unavailable"):
                self.recorder(folder, call).start()
            call.assert_called_with("record", action="stop")

    def test_mode_loaded_from_config(self):
        with patch("orchestrator.recording.load_config", return_value={"audio_source": "game_nrt"}), \
             patch("orchestrator.recording.find_ffmpeg", return_value="ffmpeg"):
            self.assertEqual(options_from(Path("run")).audio_source, "game_nrt")

    def test_old_plugin_is_rejected_before_starting_secondary_mixer(self):
        with tempfile.TemporaryDirectory() as folder:
            call = Mock(return_value={"record_audio_nrt": True, "clock": {"connected": True, "plugin": {}}})
            with self.assertRaisesRegex(RecordingError, "更新插件"):
                self.recorder(folder, call).start()
            call.assert_called_once_with("hello")


if __name__ == "__main__":
    unittest.main()
