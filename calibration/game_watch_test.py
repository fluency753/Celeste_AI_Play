"""游戏与 Steam 客户端的开跑前核对，以及运行中游戏进程退出记录。Win32 调用由替身代替。"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from calibration.isolation import unlink_links
from orchestrator import game_watch
from orchestrator.game_session import GameSession

GAME_STARTED = 1_700_000_000.0


class FakeSystem:
    def __init__(self, root: Path):
        self.root = root
        self.game_dir = root / 'Celeste'
        self.steam_dir = root / 'Steam'
        (self.game_dir / 'Mods/CelesteAIRuntime/bin').mkdir(parents=True)
        (self.steam_dir / 'dumps').mkdir(parents=True)
        self.processes = {
            40: {'exe': str(self.game_dir / 'Celeste.exe'), 'started': GAME_STARTED, 'exited': None, 'code': None,
                 'modules': {'celeste.exe', 'steam_api64.dll', 'steamclient64.dll'}},
            7: {'exe': str(self.steam_dir / 'steam.exe'), 'started': GAME_STARTED - 600, 'exited': None,
                'code': None, 'modules': set()},
        }
        self.steam = [7]
        self.open_handles = 0
        self.lock = threading.Lock()

    @property
    def hello(self):
        return {'process_id': 40, 'runtime_path': str(self.game_dir / 'Mods/CelesteAIRuntime/bin/CelesteAIRuntime.dll')}

    def open(self, pid, *, wait=False):
        if pid not in self.processes:
            return None
        with self.lock:
            self.open_handles += 1
        return {'pid': pid}

    def close(self, handle):
        if handle:
            with self.lock:
                self.open_handles -= 1

    def wait(self, handle, timeout_s):
        process = self.processes[handle['pid']]
        if process['exited'] is None:
            time.sleep(min(timeout_s, 0.01))
        return process['exited'] is not None

    def exit_code(self, handle):
        return self.processes[handle['pid']]['code']

    def times(self, handle):
        process = self.processes[handle['pid']]
        return process['started'], process['exited']

    def executable(self, handle):
        return self.processes[handle['pid']]['exe']

    def modules(self, pid):
        return self.processes[pid]['modules']

    def pids(self, name):
        self.last_name = name
        return list(self.steam)

    def restart_steam(self, started):
        self.processes[8] = {'exe': str(self.steam_dir / 'steam.exe'), 'started': started, 'exited': None,
                             'code': None, 'modules': set()}
        self.steam = [8]

    def end_game(self, code):
        process = self.processes[40]
        process['code'], process['exited'] = code, time.time()


class GameWatchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.system = FakeSystem(self.root)
        self.run = self.root / 'run'
        self.run.mkdir()
        self.logs = []

    def test_game_dir_comes_from_runtime_path(self):
        self.assertEqual(game_watch.game_dir_from(self.system.hello), self.system.game_dir.resolve())
        self.assertIsNone(game_watch.game_dir_from({'process_id': 40}))

    def test_game_directory_match_tolerates_other_spellings_of_the_same_folder(self):
        # The resolved game folder can be spelled differently from the process path
        # (8.3 short names such as C:\Users\ADMINI~1 on Windows, links elsewhere).
        real = self.system.game_dir
        self.assertTrue(game_watch._inside(str(real / 'Celeste.exe'), real.resolve()))
        self.assertFalse(game_watch._inside(str(self.root / 'Other/Celeste.exe'), real))
        try:
            (self.root / 'alias').symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest('cannot create a directory link here')
        self.addCleanup(unlink_links, self.root)     # Before the temporary folder's own cleanup.
        self.assertTrue(game_watch._inside(str(self.root / 'alias/Celeste.exe'), real.resolve()))

    def test_steam_started_before_the_game_is_fine(self):
        facts = game_watch.require_live_steam(self.system.hello, self.logs.append, self.system)
        self.assertEqual((facts['relation'], facts['steam_client_loaded'], facts['stale']),
                         ('steam_before_game', True, False))
        self.assertEqual(self.system.last_name, 'steam.exe')
        self.assertEqual(self.logs, [])
        self.assertEqual(self.system.open_handles, 0)

    def test_steam_restarted_or_gone_after_the_game_refuses_to_start(self):
        self.system.restart_steam(GAME_STARTED + 3600)
        with self.assertRaisesRegex(RuntimeError, '重启过.*未开始录制或进档'):
            game_watch.require_live_steam(self.system.hello, self.logs.append, self.system)
        self.system.steam = []
        with self.assertRaisesRegex(RuntimeError, '已退出.*未开始录制或进档'):
            game_watch.require_live_steam(self.system.hello, self.logs.append, self.system)
        self.assertEqual(self.system.open_handles, 0)

    def test_game_without_steam_client_is_not_blocked(self):
        self.system.restart_steam(GAME_STARTED + 3600)
        self.system.processes[40]['modules'] = {'celeste.exe'}
        facts = game_watch.require_live_steam(self.system.hello, self.logs.append, self.system)
        self.assertFalse(facts['stale'])

    def test_unknown_facts_and_failed_checks_never_block(self):
        self.system.restart_steam(GAME_STARTED + 3600)
        self.system.processes[40]['modules'] = None           # Modules could not be read.
        facts = game_watch.require_live_steam(self.system.hello, self.logs.append, self.system)
        self.assertFalse(facts['stale'])
        self.assertTrue(any('无法确认' in line for line in self.logs))
        self.logs.clear()
        broken = Mock(wraps=self.system)
        broken.pids.side_effect = OSError('snapshot denied')
        self.assertIsNone(game_watch.require_live_steam(self.system.hello, self.logs.append, broken))
        self.assertTrue(any('没有核对完' in line and 'snapshot denied' in line for line in self.logs))
        # A process outside the reported game directory (another game, a dotnet host) is not judged.
        self.system.processes[40]['exe'] = str(self.root / 'elsewhere/Celeste.exe')
        facts = game_watch.require_live_steam(self.system.hello, self.logs.append, self.system)
        self.assertFalse(facts['checked'])
        self.assertFalse(game_watch.require_live_steam({'process_id': 40}, self.logs.append, self.system)['checked'])

    def test_exit_is_recorded_with_code_time_steam_state_logs_and_dumps(self):
        (self.system.game_dir / 'log.txt').write_text('loaded prologue\n', encoding='utf-8')
        old_dump = self.system.steam_dir / 'dumps/assert_celeste.exe_old.dmp'
        old_dump.write_bytes(b'old')
        os.utime(old_dump, (GAME_STARTED - 86400, GAME_STARTED - 86400))
        watch = game_watch.GameExitWatch(self.run, self.logs.append, self.system, poll_s=0.01)
        self.addCleanup(watch.close)
        self.assertTrue(watch.attach(self.system.hello))
        self.assertTrue(watch.attach(self.system.hello))     # Same process: still one watcher.
        self.system.restart_steam(GAME_STARTED + 3600)
        (self.system.steam_dir / 'dumps/assert_celeste.exe_new.dmp').write_bytes(b'new')
        self.system.end_game(0xC0000005)
        self.wait_for(lambda: watch.records)
        record = watch.records[0]
        self.assertEqual((record['pid'], record['exit_code'], record['exit_code_hex']), (40, 0xC0000005, '0xC0000005'))
        self.assertEqual((record['steam_at_start'], record['steam_at_exit']), ('steam_before_game', 'steam_after_game'))
        self.assertTrue(record['steam_client_loaded'])
        self.assertTrue(record['game_log']['modified_since_start'])
        self.assertIsNone(record['error_log'])
        self.assertEqual([Path(row['path']).name for row in record['steam_dumps']], ['assert_celeste.exe_new.dmp'])
        lines = (self.run / game_watch.EXIT_LOG).read_text(encoding='utf-8').splitlines()
        self.assertEqual([json.loads(line) for line in lines], [record])
        self.assertTrue(any('已退出' in line and '0xC0000005' in line and '重启过' in line for line in self.logs), self.logs)
        watch.close()
        self.assertEqual(len(watch.records), 1)
        self.wait_for(lambda: self.system.open_handles == 0)

    def test_exit_is_remembered_until_another_game_answers_and_listeners_are_told(self):
        told = []
        watch = game_watch.GameExitWatch(self.run, self.logs.append, self.system, poll_s=0.01, on_exit=told.append)
        self.addCleanup(watch.close)
        self.assertTrue(watch.attach(self.system.hello))
        self.assertIsNone(watch.gone)
        self.assertIsNone(game_watch.exit_stop_reason(watch, finished=False))
        self.system.end_game(0)
        self.wait_for(lambda: told)
        self.assertEqual(told, watch.records)
        self.assertIs(watch.gone, watch.records[0])
        self.assertEqual(game_watch.exit_text(watch.gone), '游戏进程已退出（退出码 0x00000000，正常退出）')
        self.assertEqual(game_watch.exit_stop_reason(watch, finished=False), '游戏进程已退出（退出码 0x00000000，正常退出）')
        self.assertIsNone(game_watch.exit_stop_reason(watch, finished=True))     # 已经通关或由用户结束：原因不改
        watch.close()
        self.assertIsNotNone(watch.gone)                                        # 结束监视不等于游戏回来了
        # 重新启动的游戏回了 hello：不再算“已退出”。
        self.system.processes[41] = dict(self.system.processes[40], exited=None, code=None, started=GAME_STARTED + 900)
        self.assertTrue(watch.attach(dict(self.system.hello, process_id=41)))
        self.assertIsNone(watch.gone)
        self.assertEqual(len(watch.records), 1)

    def test_exit_text_tells_normal_from_abnormal_and_a_failing_listener_is_logged(self):
        self.assertEqual(game_watch.exit_text({'exit_code': 0xC0000005, 'exit_code_hex': '0xC0000005'}),
                         '游戏进程已退出（退出码 0xC0000005，异常退出）')
        self.assertEqual(game_watch.exit_text({'exit_code': None, 'exit_code_hex': None}), '游戏进程已退出（退出码未知）')
        self.assertIsNone(game_watch.exit_stop_reason(None, finished=False))

        def broken(record):
            raise OSError('disk full')
        watch = game_watch.GameExitWatch(self.run, self.logs.append, self.system, poll_s=0.01, on_exit=broken)
        self.addCleanup(watch.close)
        self.assertTrue(watch.attach(self.system.hello))
        self.system.end_game(1)
        self.wait_for(lambda: any('退出通知未送出' in line for line in self.logs))
        self.assertEqual(len(watch.records), 1)                                 # 记录本身已经写下

    def test_session_announces_the_exit_in_public_events(self):
        session = GameSession.__new__(GameSession)
        session.run_dir, session.client = self.run, None
        session._frames_log, session._decisions_log = Mock(), Mock()
        with patch.object(game_watch, '_system', return_value=self.system):
            session._watch_game_exit(self.system.hello)
            self.assertIsNone(session.game_exit_notice())
            self.system.end_game(0xC0000005)
            self.wait_for(lambda: session.game_exit_notice())
            events = self.run / 'public_events.jsonl'     # Written by the waiting thread: wait for the whole line.
            self.wait_for(lambda: events.exists() and events.read_text(encoding='utf-8').endswith('\n'))
            event = json.loads(events.read_text(encoding='utf-8').splitlines()[0])
            self.assertEqual((event['role'], event['kind']), ('system', '系统'))
            self.assertIn('游戏进程已退出（退出码 0xC0000005，异常退出）。主控已无法操作游戏', event['text'])
            self.assertEqual(session.game_exit_notice(), '游戏进程已退出（退出码 0xC0000005，异常退出）')
            session.close()

    def test_close_without_exit_leaves_no_record_and_releases_the_handle(self):
        watch = game_watch.GameExitWatch(self.run, self.logs.append, self.system, poll_s=0.01)
        self.assertTrue(watch.attach(self.system.hello))
        watch.close()
        self.assertEqual(watch.records, [])
        self.assertFalse((self.run / game_watch.EXIT_LOG).exists())
        self.assertEqual(self.system.open_handles, 0)

    def test_reused_pid_or_unverifiable_process_is_not_watched(self):
        watch = game_watch.GameExitWatch(self.run, self.logs.append, self.system, poll_s=0.01)
        self.addCleanup(watch.close)
        original = self.system.open

        def reused(pid, *, wait=False):
            if wait:                     # The checked process ended and the PID now names another one.
                self.system.processes[40]['started'] = GAME_STARTED + 5
            return original(pid, wait=wait)
        with patch.object(self.system, 'open', side_effect=reused):
            self.assertFalse(watch.attach(self.system.hello))
        self.assertEqual(self.system.open_handles, 0)
        self.assertFalse(watch.attach({'process_id': 40}))
        self.assertFalse(watch.attach({'process_id': 'x', 'runtime_path': self.system.hello['runtime_path']}))

    def test_session_watches_after_connect_and_stops_on_close(self):
        session = GameSession.__new__(GameSession)
        session.run_dir, session.client = self.run, None
        session._frames_log, session._decisions_log = Mock(), Mock()
        with patch.object(game_watch, '_system', return_value=self.system):
            session._watch_game_exit(self.system.hello)
            self.assertEqual(session.game_watch.pid, 40)
            session.close()
        self.assertIsNone(session.game_watch.pid)
        self.wait_for(lambda: self.system.open_handles == 0)

    def test_preflight_runs_the_steam_check_and_closes_its_connection(self):
        from orchestrator import bridge_requirements
        client = Mock()
        client.call.return_value = self.system.hello
        with patch('tools.bridge_client.BridgeClient', return_value=client), \
             patch.object(bridge_requirements, 'validate_bridge'), \
             patch('orchestrator.action_policy.require_capability'), \
             patch.object(game_watch, 'require_live_steam', side_effect=RuntimeError('Steam 重启过')) as check:
            with self.assertRaisesRegex(RuntimeError, 'Steam 重启过'):
                bridge_requirements.preflight_bridge(47774)
        check.assert_called_once()
        self.assertEqual(check.call_args.args[0], self.system.hello)
        client.close.assert_called_once()

    def wait_for(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail('condition not reached')
            time.sleep(0.01)


if __name__ == '__main__':
    unittest.main()
