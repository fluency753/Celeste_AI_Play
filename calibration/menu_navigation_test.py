"""Exercise complete menu macros while substituting only the game IPC boundary."""
import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest

from tools.cutscene import leave_level_by_pause_menu, skip_by_pause_menu
from tools.enter_save import enter_save, load_save_and_freeze, open_chapter_select, to_file_select
from tools.menu_navigation import MenuActionBlocked


def level(**updates):
    obs = {'scene': 'Level', 'controllable': True, 'save': {'name': 'test', 'slot': 10},
           'level': {'sid': 'Celeste/3-CelestialResort', 'room': '00-c', 'canPause': True,
                     'inCutscene': False, 'paused': False}}
    obs.update(updates)
    return obs


def file_select():
    return {'scene': 'Overworld', 'menu': {'current': 'OuiFileSelect', 'slotSelected': True,
            'slotIndex': 0, 'buttonIndex': 0, 'slots': [{'index': 0, 'fileSlot': 10,
                                                     'exists': True, 'name': 'test'}]}}


def pause_menu(ident='menu_pause_return'):
    return {'focused': True, 'selection': 0,
            'items': [{'index': 0, 'id': ident, 'selectable': True, 'disabled': False}]}


class GameIpc:
    def __init__(self, initial, steps=(), capability=True):
        self.obs = copy.deepcopy(initial)
        self.responses = list(steps)
        self.calls = []
        self.capability = capability

    def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == 'hello':
            return {'chapter_loaded_stop': self.capability}
        if command in {'activate', 'observe'}:
            return {'obs': self.obs}
        if command == 'step':
            if not self.responses:
                raise AssertionError('Macro sent unexpected input after its terminal result')
            reply = copy.deepcopy(self.responses.pop(0))
            self.obs = reply['obs']
            return reply
        raise AssertionError(command)

    @property
    def steps(self):
        return [kwargs for command, kwargs in self.calls if command == 'step']


def reply(obs, **result):
    return {'obs': obs, 'result': {'stop': 'done', 'consumed': 1, **result}}


def blocked(obs):
    return reply(obs, stop='chapter_reset_blocked', consumed=1,
                 block={'rule_id': 'permission.return_to_map', 'action': 'return_to_map',
                        'reason': 'Controller policy forbids return_to_map'})


class MenuNavigationTests(unittest.TestCase):
    def test_resume_loaded_level_returns_without_input_or_departure(self):
        for controllable in (True, False):
            obs = level(controllable=controllable)
            ipc = GameIpc(obs)
            self.assertEqual(load_save_and_freeze(ipc, 'test', save_slot=10), obs)
            self.assertEqual(ipc.steps, [])

    def test_native_continue_stops_at_loaded_level_without_settling(self):
        loaded = level(controllable=False)
        ipc = GameIpc(file_select(), [reply(loaded, stop='chapter_loaded', consumed=0)])
        self.assertEqual(load_save_and_freeze(ipc, 'test', save_slot=10), loaded)
        self.assertEqual(len(ipc.steps), 1)
        self.assertEqual(ipc.steps[0]['inputs'][0], '1,C')
        self.assertEqual(ipc.steps[0]['stop_on'], ['chapter_loaded'])

    def test_loaded_story_is_handed_back_without_skip_or_confirmation(self):
        for scene in ('IntroVignette', 'LevelEnter'):
            loaded = level(scene=scene, controllable=False)
            ipc = GameIpc(file_select(), [reply(loaded, stop='chapter_loaded')])
            self.assertEqual(load_save_and_freeze(ipc, 'test', save_slot=10), loaded)
            self.assertEqual(len(ipc.steps), 1)

    def test_no_active_session_returns_native_map_without_selecting_chapter(self):
        selected = {'scene': 'Overworld', 'save': {'name': 'test', 'slot': 10},
                    'menu': {'current': 'OuiChapterSelect', 'area': 3}}
        ipc = GameIpc(file_select(), [reply(selected)])
        self.assertEqual(load_save_and_freeze(ipc, 'test', save_slot=10), selected)
        self.assertEqual(len(ipc.steps), 1)

    def test_old_bridge_and_ambiguous_boolean_fail_before_inputs(self):
        ipc = GameIpc(level(), capability=False)
        with self.assertRaisesRegex(RuntimeError, 'chapter_loaded_stop'):
            load_save_and_freeze(ipc, 'test', save_slot=10)
        self.assertEqual(ipc.steps, [])
        with self.assertRaisesRegex(ValueError, 'load_save_and_freeze'):
            enter_save(ipc, 'test', stop_at_chapter_select=True)
        self.assertEqual([cmd for cmd, _ in ipc.calls], ['hello'])

    def test_wrong_loaded_save_does_not_leave_or_reenter(self):
        ipc = GameIpc(level(save={'name': 'someone else', 'slot': 11}))
        with self.assertRaisesRegex(RuntimeError, '其他存档'):
            load_save_and_freeze(ipc, 'test', save_slot=10)
        self.assertEqual(ipc.steps, [])

    def test_all_menu_macros_propagate_denial_without_followup_input(self):
        paused = level(pauseMenu=pause_menu())
        cutscene = level(pauseMenu=pause_menu('menu_pause_skip_cutscene'))
        cutscene['level']['inCutscene'] = True
        for initial, macro in (
                (paused, lambda c: leave_level_by_pause_menu(c)),
                (paused, lambda c: to_file_select(c)),
                (paused, lambda c: open_chapter_select(c, 'test', save_slot=10)),
                (cutscene, lambda c: skip_by_pause_menu(c)),
                (file_select(), lambda c: enter_save(c, 'test', save_slot=10)),
                (file_select(), lambda c: load_save_and_freeze(c, 'test', save_slot=10))):
            ipc = GameIpc(initial, [blocked(initial)])
            with self.subTest(macro=macro), self.assertRaises(MenuActionBlocked) as raised:
                macro(ipc)
            self.assertIn('返回地图', str(raised.exception))
            self.assertIn('permission.return_to_map', str(raised.exception))
            self.assertEqual(raised.exception.obs, initial)
            self.assertEqual(raised.exception.result['consumed'], 1)
            self.assertEqual(len(ipc.steps), 1)

    def test_legacy_stop_without_block_also_terminates_navigation(self):
        initial = file_select()
        ipc = GameIpc(initial, [reply(initial, stop='entry_blocked')])
        with self.assertRaises(MenuActionBlocked):
            enter_save(ipc, 'test', save_slot=10)
        self.assertEqual(len(ipc.steps), 1)

    def test_skip_denial_preserves_consumed_frame_accounting_callback(self):
        cutscene = level(pauseMenu=pause_menu('menu_pause_skip_cutscene'))
        cutscene['level']['inCutscene'] = True
        ipc = GameIpc(cutscene, [blocked(cutscene)])
        accounted = []
        with self.assertRaises(MenuActionBlocked):
            skip_by_pause_menu(ipc, on_step=lambda inputs, result: accounted.append(result))
        self.assertEqual([r['consumed'] for r in accounted], [1])

    def test_exit_wait_timeout_is_failure_instead_of_false_success(self):
        paused = level(pauseMenu=pause_menu())
        ipc = GameIpc(paused, [reply(paused)] * 61)
        with self.assertRaisesRegex(RuntimeError, '等待退出关卡超时'):
            leave_level_by_pause_menu(ipc)
        self.assertEqual(len(ipc.steps), 61)


class MenuCliTests(unittest.TestCase):
    """Invoke the shipped CLI over a private TCP peer, never the in-use game port."""
    def invoke(self, denied):
        commands = []
        failures = []
        loaded = level()
        loaded['level'].update(side='Normal', deaths=0)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(10)
            port = listener.getsockname()[1]

            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn, conn.makefile('rwb') as stream:
                        for line in stream:
                            request = json.loads(line)
                            commands.append(request)
                            command = request['cmd']
                            data = {'id': request['id'], 'ok': True}
                            if command == 'hello':
                                data['chapter_loaded_stop'] = True
                            elif command == 'activate':
                                data['obs'] = file_select()
                            elif command == 'step':
                                self.assertEqual(request['stop_on'], ['chapter_loaded'])
                                self.assertEqual(request['action_policy']['entry_target']['slot'], 10)
                                data.update(blocked(file_select()) if denied else reply(loaded, stop='chapter_loaded'))
                            else:
                                raise AssertionError('Unexpected command after macro terminal result: ' + command)
                            stream.write((json.dumps(data) + '\n').encode())
                            stream.flush()
                except Exception as exc:
                    failures.append(exc)

            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            result = subprocess.run([sys.executable, '-X', 'utf8', 'tools/enter_save.py', 'test',
                                     '--save-slot', '10', '--port', str(port)],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                    encoding='utf-8', timeout=15)
            worker.join(3)
            self.assertFalse(worker.is_alive())
            if failures:
                raise failures[0]
        return result, commands

    def test_default_cli_load_keeps_control_without_exit_or_extra_inputs(self):
        result, commands = self.invoke(False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('scene=Level', result.stdout)
        self.assertIn('test (slot 10)', result.stdout)
        self.assertEqual([c['cmd'] for c in commands], ['hello', 'activate', 'step'])

    def test_cli_receiver_sees_action_reason_and_consumed_frames_on_denial(self):
        result, commands = self.invoke(True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('主控拦截「返回地图」', result.stderr)
        self.assertIn('permission.return_to_map', result.stderr)
        self.assertIn('本次不允许返回地图', result.stderr)
        self.assertIn('已推进输入 1/', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual([c['cmd'] for c in commands], ['hello', 'activate', 'step'])


if __name__ == '__main__':
    unittest.main()
