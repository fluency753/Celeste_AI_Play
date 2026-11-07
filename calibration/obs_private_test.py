"""私有便携版 OBS（tools/obs_setup.py private）：只复制 OBS 官方组件、按进程路径区分私有与个人 OBS、
只启动和关闭私有 OBS、移走个人 OBS 的锁步插件、个人场景只认标题 Celeste 的游戏窗口。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import obs_setup


def touch(path: Path, data: bytes = b'x') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fake_obs(root: Path) -> Path:
    """一份最小的 OBS 安装：官方组件带 .pdb，另有第三方插件、旧的锁步插件和便携配置。"""
    for name in ('bin/64bit/obs64.exe', 'bin/64bit/obs.dll', 'bin/64bit/obs64.pdb', 'data/libobs/default.effect',
                 'obs-plugins/64bit/obs-x264.dll', 'obs-plugins/64bit/obs-x264.pdb', 'obs-plugins/64bit/libcef.dll',
                 'obs-plugins/64bit/obs-browser-page.exe', 'obs-plugins/64bit/locales/en-US.pak',
                 'obs-plugins/64bit/bililive_obs_plugin.dll', 'obs-plugins/64bit/celeste-lockstep.dll',
                 'data/obs-plugins/obs-x264/locale/en-US.ini', 'data/obs-plugins/bililive_obs_plugin/a.ini',
                 'config/obs-studio/user.ini', 'portable_mode.txt', 'disable_updater'):
        touch(root / name)
    return root


def signed(paths):
    return {os.path.normcase(str(p)) for p in paths
            if p.name in ('obs-x264.dll', 'libcef.dll', 'obs-browser-page.exe')}


class PrivateObsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.private = self.root / 'repo/local/obs'
        self.personal = fake_obs(self.root / 'Steam/OBS Studio')
        self.appdata = self.root / 'AppData'
        personal_conf = self.appdata / 'obs-studio'
        touch(personal_conf / 'user.ini', '[General]\nFirstRun=true\n[Basic]\nProfile=Mine\n'.encode())
        touch(personal_conf / 'global.ini', '[General]\nLastVersion=537001986\n'.encode())
        for patcher in (patch.object(obs_setup, 'PRIVATE_ROOT', self.private),
                        patch.object(obs_setup, 'PERSONAL_BACKUPS', self.root / 'repo/local/obs-personal-backup'),
                        patch.object(obs_setup, 'STATE', self.root / 'repo/build/obs_previous.json'),
                        patch.object(obs_setup, 'personal_obs_dir', return_value=self.personal),
                        patch.object(obs_setup, '_signed_by_obs', side_effect=signed),
                        patch.dict(os.environ, {'APPDATA': str(self.appdata)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.pids = {}      # 进程号 → 可执行文件路径
        for patcher in (patch.object(obs_setup, '_obs_pids', side_effect=lambda: list(self.pids)),
                        patch('orchestrator.gamewindow.process_path', side_effect=lambda pid: self.pids[pid])):
            patcher.start()
            self.addCleanup(patcher.stop)

    def create(self, **kwargs):
        with patch('tools.build_obs_plugin.obs_version', return_value='32.2.2'):
            return obs_setup.private_create(log=lambda _: None, **kwargs)

    def test_copies_only_official_components_and_switches_to_private_config(self):
        self.assertIsNone(obs_setup.private_root())
        self.assertEqual(obs_setup.obs_config_dir(), self.appdata / 'obs-studio')
        self.create()
        files = sorted(p.relative_to(self.private).as_posix() for p in self.private.rglob('*') if p.is_file())
        program = [f for f in files if not f.startswith('config/')]
        self.assertEqual(program, ['bin/64bit/obs.dll', 'bin/64bit/obs64.exe', 'celeste-ai-obs.json',
                                   'data/libobs/default.effect', 'data/obs-plugins/obs-x264/locale/en-US.ini',
                                   'disable_updater', 'obs-plugins/64bit/celeste-lockstep.dll',
                                   'obs-plugins/64bit/libcef.dll', 'obs-plugins/64bit/locales/en-US.pak',
                                   'obs-plugins/64bit/obs-browser-page.exe', 'obs-plugins/64bit/obs-x264.dll',
                                   'portable_mode.txt'])   # 不带 .pdb、第三方插件和来源的便携配置
        self.assertEqual(obs_setup.private_root(), self.private)
        conf = self.private / 'config/obs-studio'
        self.assertEqual(obs_setup.obs_config_dir(), conf)
        self.assertEqual(obs_setup.find_obs_dir(None), self.private)
        self.assertEqual(obs_setup.find_obs_dir(str(self.personal)), self.personal)
        user = (conf / 'user.ini').read_text(encoding='utf-8')
        for line in ('FirstRun=true', 'LastVersion=537001986', 'Profile=CelesteAI', 'SceneCollection=CelesteAI'):
            self.assertIn(line, user)
        self.assertTrue((conf / 'basic/profiles/CelesteAI/basic.ini').is_file())
        self.assertTrue((conf / 'basic/scenes/CelesteAI.json').is_file())
        # 你自己的 OBS：配置与安装原样（没加 --move-personal-plugin 时插件只是复制过去）
        self.assertEqual((self.appdata / 'obs-studio/user.ini').read_text(encoding='utf-8'),
                         '[General]\nFirstRun=true\n[Basic]\nProfile=Mine\n')
        self.assertFalse((self.appdata / 'obs-studio/basic').exists())
        self.assertTrue((self.personal / 'obs-plugins/64bit/celeste-lockstep.dll').is_file())
        marker = json.loads((self.private / 'celeste-ai-obs.json').read_text(encoding='utf-8'))
        self.assertEqual((marker['format'], marker['version']), (1, '32.2.2'))

    def test_move_personal_plugin_keeps_a_backup_and_update_keeps_private_state(self):
        touch(self.root / 'repo/build/obs-plugin/celeste-lockstep.dll', b'built')
        with patch.object(obs_setup, 'REPO_ROOT', self.root / 'repo'):
            self.pids = {7: str(self.personal / 'bin/64bit/obs64.exe')}
            with self.assertRaisesRegex(SystemExit, '正在运行'):
                self.create(move_personal_plugin=True)
            self.pids = {}
            self.create(move_personal_plugin=True)
            self.assertFalse((self.personal / 'obs-plugins/64bit/celeste-lockstep.dll').exists())
            backups = list((self.root / 'repo/local/obs-personal-backup').rglob('celeste-lockstep.dll'))
            self.assertEqual([p.read_bytes() for p in backups], [b'x'])
            plugin = self.private / 'obs-plugins/64bit/celeste-lockstep.dll'
            self.assertEqual(plugin.read_bytes(), b'x')
            plugin.write_bytes(b'rebuilt')
            touch(self.private / 'config/obs-studio/basic/scenes/CelesteAI.json', b'{"name": "CelesteAI", "custom": 1}')
            touch(self.personal / 'bin/64bit/obs.dll', b'new version')
            self.create()                                     # 更新：刷新程序，插件与配置保留
            self.assertEqual((self.private / 'bin/64bit/obs.dll').read_bytes(), b'new version')
            self.assertEqual(plugin.read_bytes(), b'rebuilt')
            self.assertIn('custom', (self.private / 'config/obs-studio/basic/scenes/CelesteAI.json').read_text())
            marker = json.loads((self.private / 'celeste-ai-obs.json').read_text(encoding='utf-8'))
            self.assertIn('created', marker)
            self.assertIn('updated', marker)

    def test_processes_are_told_apart_by_path(self):
        self.create()
        self.pids = {1: str(self.private / 'bin/64bit/obs64.exe'), 2: str(self.personal / 'bin/64bit/obs64.exe'),
                     3: ''}                                    # 3：以管理员身份运行，读不到路径
        self.assertEqual(obs_setup.obs_processes(private=True), [1])
        self.assertEqual(obs_setup.obs_processes(private=False), [2, 3])
        self.assertTrue(obs_setup.obs_running())
        self.pids = {2: str(self.personal / 'bin/64bit/obs64.exe')}
        self.assertFalse(obs_setup.obs_running())            # 只开着你自己的 OBS：私有 OBS 算没在运行
        with patch.object(obs_setup, 'PRIVATE_ROOT', self.root / 'none'):
            self.assertTrue(obs_setup.obs_running())          # 没有私有 OBS 时照旧：任何 OBS 都算

    def test_launch_and_stop_touch_only_the_private_obs(self):
        self.create()
        mine = str(self.personal / 'bin/64bit/obs64.exe')
        self.pids = {2: mine}
        started = []
        with patch.object(obs_setup.subprocess, 'Popen', side_effect=lambda args, **_: started.append(args)), \
                patch.object(obs_setup, 'ensure_hud_service'), patch.object(obs_setup, 'print_status'), \
                patch.object(obs_setup, 'plugin_log_status', return_value={'connected': True}), \
                patch.object(obs_setup.time, 'sleep'):
            obs_setup.launch(obs_setup.load_config(), self.personal, minimized=True)
        self.assertEqual(Path(started[0][0]), self.private / 'bin/64bit/obs64.exe')
        self.assertIn('--portable', started[0])
        self.assertIn('--multi', started[0])
        self.assertFalse(obs_setup.STATE.exists())           # 不记下、不还原你的 OBS 的选择
        self.pids = {1: str(self.private / 'bin/64bit/obs64.exe'), 2: mine}
        calls = []

        def run(args, **_):
            calls.append(args)
            if args[0] == 'taskkill':
                self.pids.pop(1, None)

        with patch.object(obs_setup.subprocess, 'run', side_effect=run), patch.object(obs_setup.time, 'sleep'):
            obs_setup.stop(wait=1)
        self.assertEqual(calls, [['taskkill', '/PID', '1']])
        self.assertEqual(self.pids, {2: mine})               # 你自己的 OBS 还开着

    def test_personal_capture_matches_only_the_personal_window_title(self):
        scenes = self.appdata / 'obs-studio/basic/scenes'

        def collection(name, window, priority):
            return {'name': name, 'sources': [
                {'id': 'game_capture', 'name': '游戏', 'settings': {'capture_mode': 'window', 'window': window, 'priority': priority}},
                {'id': 'wasapi_process_output_capture', 'name': '游戏声音', 'settings': {'window': window, 'priority': priority}},
                {'id': 'game_capture', 'name': '别的游戏', 'settings': {'window': 'X:Cls:other.exe', 'priority': 2}},
                {'id': 'dshow_input', 'name': '摄像头', 'settings': {}}]}

        touch(scenes / '未命名.json', json.dumps(collection('未命名', 'Celeste:SDL_app:Celeste.exe', 2), ensure_ascii=False).encode())
        touch(scenes / 'CelesteAI.json', json.dumps(collection('CelesteAI', 'Celeste AI Play:SDL_app:Celeste.exe', 1)).encode())
        before = {p.name: p.read_bytes() for p in scenes.iterdir()}
        self.assertEqual(len(obs_setup.personal_capture(log=lambda _: None)), 2)
        self.assertEqual({p.name: p.read_bytes() for p in scenes.iterdir()}, before)   # 不加 --apply 只列出
        self.pids = {2: str(self.personal / 'bin/64bit/obs64.exe')}
        with self.assertRaisesRegex(SystemExit, '正在运行'):
            obs_setup.personal_capture(apply=True, log=lambda _: None)
        self.pids = {}
        obs_setup.personal_capture(apply=True, log=lambda _: None)
        data = json.loads((scenes / '未命名.json').read_text(encoding='utf-8'))
        windows = [(s['name'], s['settings'].get('window'), s['settings'].get('priority')) for s in data['sources']]
        self.assertEqual(windows, [('游戏', 'Celeste:SDL_app:Celeste.exe', 1), ('游戏声音', 'Celeste:SDL_app:Celeste.exe', 1),
                                   ('别的游戏', 'X:Cls:other.exe', 2), ('摄像头', None, None)])
        self.assertEqual((scenes / 'CelesteAI.json').read_bytes(), before['CelesteAI.json'])   # 本工具的场景集合不动
        backups = list((self.root / 'repo/local/obs-personal-backup').rglob('未命名.json'))
        self.assertEqual([p.read_bytes() for p in backups], [before['未命名.json']])
        self.assertEqual(obs_setup.personal_capture(apply=True, log=lambda _: None), [])     # 再跑一次没有要改的


if __name__ == '__main__':
    unittest.main()
