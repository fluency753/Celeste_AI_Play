"""OBS launch must select the same clock for video timing and game audio."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import obs_setup


class ObsClockTests(unittest.TestCase):
    def test_default_and_custom_clock(self):
        self.assertEqual(obs_setup.clock_port({}), 47776)
        self.assertEqual(obs_setup.clock_port({'clock_port': 47777}), 47777)

    def test_original_game_can_use_title_matching(self):
        with patch('orchestrator.game_instance.current', return_value=None):
            self.assertEqual(obs_setup.capture_target({'game_window': 'Original:SDL_app:Celeste.exe',
                                                       'game_window_priority': 1}),
                             ('Original:SDL_app:Celeste.exe', 1))

    def test_invalid_clock_fails_before_launch_side_effects(self):
        for value in (None, True, '47777', 0, 1023, 65536, 47777.0):
            with self.subTest(value=value), patch.object(obs_setup, 'setup') as setup:
                with self.assertRaisesRegex(ValueError, 'clock_port'):
                    obs_setup.launch({'clock_port': value}, Path('unused'), True)
                setup.assert_not_called()

    def test_launch_passes_configured_clock_to_both_process_paths(self):
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as tmp:
                cfg = {'clock_port': 47777, 'profile': 'test', 'collection': 'test'}
                with patch.object(obs_setup, 'private_root', return_value=Path(tmp)), \
                     patch.object(obs_setup, 'obs_running', return_value=False), \
                     patch.object(obs_setup, 'setup'), \
                     patch.object(obs_setup, 'ensure_hud_service'), \
                     patch.object(obs_setup, 'clear_stale_sentinel'), \
                     patch.object(obs_setup, 'print_status'), \
                     patch.object(obs_setup.time, 'sleep'), \
                     patch.object(obs_setup, 'plugin_log_status', return_value={'connected': True, 'clock_port': 47777}), \
                     patch.dict(os.environ, {'CELESTE_AI_CLOCK_PORT': '49999'}), \
                     patch.object(obs_setup.subprocess, 'Popen', side_effect=[OSError('job'), object()] if fallback else None) as start:
                    obs_setup.launch(cfg, Path(tmp), True)
                self.assertEqual(start.call_count, 2 if fallback else 1)
                for call in start.call_args_list:
                    self.assertEqual(call.kwargs['env']['CELESTE_AI_CLOCK_PORT'], '47777')

    def test_log_reports_latest_connection_and_disconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'obs.log'
            log.write_text('[celeste-lockstep] loaded\n'
                           '[celeste-lockstep] connected to bridge clock port 47776\n'
                           '[celeste-lockstep] disconnected\n'
                           '[celeste-lockstep] connected to bridge clock port 47777\n', encoding='utf-8')
            with patch.object(obs_setup, 'latest_log', return_value=log):
                info = obs_setup.plugin_log_status()
                self.assertTrue(info['connected'])
                self.assertEqual(info['clock_port'], 47777)
                with patch.object(obs_setup, 'capture_target', return_value=('Celeste:SDL_app:Celeste.exe', 2)):
                    self.assertIn('47777', obs_setup.capture_mismatch({'clock_port': 47776}))
                    self.assertIsNone(obs_setup.capture_mismatch({'clock_port': 47777}))
                with log.open('a', encoding='utf-8') as stream:
                    stream.write('[celeste-lockstep] disconnected\n')
                self.assertFalse(obs_setup.plugin_log_status()['connected'])

    def test_wrong_clock_is_not_a_successful_launch(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(obs_setup, 'private_root', return_value=Path(tmp)), \
             patch.object(obs_setup, 'obs_running', return_value=False), \
             patch.object(obs_setup, 'setup'), \
             patch.object(obs_setup, 'ensure_hud_service'), \
             patch.object(obs_setup, 'clear_stale_sentinel'), \
             patch.object(obs_setup.subprocess, 'Popen'), \
             patch.object(obs_setup.time, 'sleep'), \
             patch.object(obs_setup.time, 'monotonic', side_effect=[0, 0, 31]), \
             patch.object(obs_setup, 'plugin_log_status', return_value={'connected': True, 'clock_port': 47776}):
            with self.assertRaisesRegex(SystemExit, '47777.*47776'):
                obs_setup.launch({'clock_port': 47777, 'profile': 'test', 'collection': 'test'}, Path(tmp), True)

    def test_startup_delay_can_expire_before_first_poll(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(obs_setup, 'private_root', return_value=Path(tmp)), \
             patch.object(obs_setup, 'obs_running', return_value=False), \
             patch.object(obs_setup, 'setup'), \
             patch.object(obs_setup, 'ensure_hud_service'), \
             patch.object(obs_setup, 'clear_stale_sentinel'), \
             patch.object(obs_setup.subprocess, 'Popen'), \
             patch.object(obs_setup.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(SystemExit, '47777'):
                obs_setup.launch({'clock_port': 47777, 'profile': 'test', 'collection': 'test'}, Path(tmp), True)


if __name__ == '__main__':
    unittest.main()
