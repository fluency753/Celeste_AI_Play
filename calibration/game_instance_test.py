"""AI 专用游戏副本：创建与更新只动副本、身份核验、OBS 按副本窗口标题采集（orchestrator/game_instance.py）。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import game_instance, gamepaths
from orchestrator.bridge_requirements import validate_bridge


def fake_game(root: Path) -> Path:
    """一个最小的个人安装：游戏本体、个人 Mod 与存档、日志和备份。"""
    root.mkdir(parents=True)
    (root / 'Celeste.exe').write_bytes(b'exe')
    (root / 'Celeste.dll').write_bytes(b'dll')
    (root / 'Content/Maps').mkdir(parents=True)
    (root / 'Content/Maps/1.bin').write_bytes(b'map')
    (root / 'log.txt').write_text('personal log', encoding='utf-8')
    (root / 'Celeste.tas').write_text('tas', encoding='utf-8')
    for module in ('CelesteAIRuntime', 'CelesteAIBridge', 'SpringCollab2020.zip'):
        target = root / 'Mods' / module
        if module.endswith('.zip'):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'zip')
        else:
            (target / 'bin').mkdir(parents=True)
            (target / 'everest.yaml').write_text(f'- Name: {module}\n', encoding='utf-8')
    (root / 'Mods/blacklist.txt').write_text('x', encoding='utf-8')
    (root / 'Saves').mkdir()
    for name in ('0.celeste', '8.celeste', 'settings.celeste', 'modsettings-Everest.celeste', 'modsettings-Foo.celeste'):
        (root / 'Saves' / name).write_text(name, encoding='utf-8')
    (root / 'Backups').mkdir()
    (root / 'Backups/old.celeste').write_text('b', encoding='utf-8')
    return root


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = fake_game(self.root / 'Steam/Celeste')

    def test_copy_keeps_only_game_ai_mods_and_initial_settings(self):
        marker = game_instance.create('main', self.source, self.root / 'instances/main', log=lambda _: None)
        target = Path(marker['dir'])
        self.assertTrue((target / 'Celeste.exe').is_file())
        self.assertEqual((target / 'Content/Maps/1.bin').read_bytes(), b'map')
        self.assertEqual(sorted(p.name for p in (target / 'Mods').iterdir()), ['CelesteAIBridge', 'CelesteAIRuntime'])
        self.assertEqual(sorted(p.name for p in (target / 'Saves').iterdir()),
                         ['modsettings-Everest.celeste', 'settings.celeste'])   # 不复制任何存档
        for absent in ('log.txt', 'Celeste.tas', 'Backups'):
            self.assertFalse((target / absent).exists(), absent)
        self.assertEqual((target / 'steam_appid.txt').read_text(encoding='ascii'), '480')
        self.assertEqual(game_instance.info(target)['window_title'], 'Celeste AI Play')
        self.assertEqual(marker['missing_mods'], [])
        # 源安装原样
        self.assertTrue((self.source / 'Saves/8.celeste').is_file())
        self.assertFalse((self.source / game_instance.MARKER).exists())

    def test_refuses_unsafe_targets_and_sources(self):
        target = self.root / 'instances/main'
        target.mkdir(parents=True)
        (target / 'keep.txt').write_text('x', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已存在且不为空'):
            game_instance.create('main', self.source, target, log=lambda _: None)
        with self.assertRaisesRegex(ValueError, '源安装里面'):
            game_instance.create('main', self.source, self.source / 'ai', log=lambda _: None)
        with self.assertRaisesRegex(ValueError, '名字'):
            game_instance.create('../x', self.source, self.root / 'y', log=lambda _: None)
        with self.assertRaisesRegex(ValueError, '冒号'):
            game_instance.create('x', self.source, self.root / 'y', title='a:b', log=lambda _: None)
        made = game_instance.create('copy', self.source, self.root / 'instances/copy', log=lambda _: None)
        with self.assertRaisesRegex(ValueError, '本身就是 AI 副本'):
            game_instance.create('again', made['dir'], self.root / 'instances/again', log=lambda _: None)

    def test_missing_ai_mods_are_reported(self):
        for module in ('CelesteAIRuntime', 'CelesteAIBridge'):
            (self.source / 'Mods' / module / 'everest.yaml').unlink()
        marker = game_instance.create('main', self.source, self.root / 'i', log=lambda _: None)
        self.assertEqual(marker['missing_mods'], ['CelesteAIRuntime', 'CelesteAIBridge'])

    def test_update_refreshes_the_game_but_keeps_instance_state(self):
        marker = game_instance.create('main', self.source, self.root / 'i', log=lambda _: None)
        target = Path(marker['dir'])
        (target / 'Saves/0.celeste').write_text('ai save', encoding='utf-8')
        (self.source / 'Content/Maps/1.bin').write_bytes(b'new map')
        updated = game_instance.update(target, log=lambda _: None)
        self.assertEqual((target / 'Content/Maps/1.bin').read_bytes(), b'new map')
        self.assertEqual((target / 'Saves/0.celeste').read_text(encoding='utf-8'), 'ai save')
        self.assertEqual(sorted(p.name for p in (target / 'Mods').iterdir()), ['CelesteAIBridge', 'CelesteAIRuntime'])
        self.assertEqual((target / 'steam_appid.txt').read_text(encoding='ascii'), '480')
        self.assertIn('updated', updated)
        self.assertEqual(game_instance.info(target)['name'], 'main')

    def test_copy_saves_copies_whole_slots_and_never_overwrites(self):
        saves = self.source / 'Saves'
        (saves / '8.celeste').write_text('<SaveData><Name>opus5.5</Name></SaveData>', encoding='utf-8')
        for name in ('8-modsave-Everest.celeste', '8-modsession-Everest.celeste', '8-modsavedata.celeste',
                     '80.celeste', '80-modsave-Everest.celeste', '1-modsave-Everest.celeste', '10.celeste'):
            (saves / name).write_text(name, encoding='utf-8')
        marker = game_instance.create('main', self.source, self.root / 'i', log=lambda _: None)
        target = Path(marker['dir'])
        before = {p.name: p.read_bytes() for p in saves.iterdir()}
        copied = game_instance.copy_saves([8, 0], target, log=lambda _: None)
        self.assertEqual(copied, [{'slot': 8, 'name': 'opus5.5', 'files': 4}, {'slot': 0, 'name': '', 'files': 1}])
        self.assertEqual(sorted(p.name for p in (target / 'Saves').iterdir()),
                         ['0.celeste', '8-modsave-Everest.celeste', '8-modsavedata.celeste',
                          '8-modsession-Everest.celeste', '8.celeste', 'modsettings-Everest.celeste',
                          'settings.celeste'])           # 80、10、1- 开头的都不是槽 8 / 0
        self.assertEqual({p.name: p.read_bytes() for p in saves.iterdir()}, before)   # 源安装原样
        (target / 'Saves/8.celeste').write_text('ai progress', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '已经有存档槽 8'):
            game_instance.copy_saves([10, 8], target, log=lambda _: None)
        self.assertEqual((target / 'Saves/8.celeste').read_text(encoding='utf-8'), 'ai progress')
        self.assertFalse((target / 'Saves/10.celeste').exists())      # 先全部核对，有一个不行就都不复制
        with self.assertRaisesRegex(ValueError, '没有存档槽 3'):
            game_instance.copy_saves([3], target, log=lambda _: None)
        with self.assertRaisesRegex(ValueError, '整数'):
            game_instance.copy_saves([-1], target, log=lambda _: None)
        with self.assertRaisesRegex(ValueError, '不是 AI 副本'):
            game_instance.copy_saves([8], self.source, log=lambda _: None)
        with patch.object(game_instance, 'game_processes', side_effect=lambda d: [1] if Path(d) == self.source.resolve() else []):
            with self.assertRaisesRegex(RuntimeError, '正在运行'):
                game_instance.copy_saves([10], target, log=lambda _: None)

    def test_invalid_markers_are_not_instances(self):
        folder = self.root / 'x'
        folder.mkdir()
        for content in ('not json', '{"format": 2, "window_title": "t"}', '{"format": 1, "window_title": ""}', '[]'):
            (folder / game_instance.MARKER).write_text(content, encoding='utf-8')
            self.assertIsNone(game_instance.info(folder), content)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.marker = game_instance.create('main', fake_game(Path(self.temp.name) / 'Steam/Celeste'),
                                           Path(self.temp.name) / 'i', log=lambda _: None)

    def test_running_steam_counts_even_when_the_registry_shows_no_user(self):
        # 实测：Steam 重新打开、登录后注册表 ActiveProcess 仍是 0，游戏照样连得上
        with patch.object(game_instance, '_steam_user', return_value=0), \
                patch.object(game_instance, 'steam_running', return_value=True):
            self.assertTrue(game_instance.steam_ready())
        with patch.object(game_instance, '_steam_user', return_value=0), \
                patch.object(game_instance, 'steam_running', return_value=False):
            self.assertFalse(game_instance.steam_ready())

    def test_a_freshly_started_steam_is_awaited_until_it_logs_in(self):
        users = iter([0, 0, 1234])
        with patch.object(game_instance, 'steam_ready', return_value=False), \
                patch.object(game_instance, '_steam_registry', return_value=('steam.exe', 0)), \
                patch.object(game_instance, '_steam_user', side_effect=lambda: next(users)), \
                patch.object(game_instance, '_detached') as start, patch.object(game_instance.time, 'sleep'):
            game_instance.ensure_steam(log=lambda _: None)
        start.assert_called_once_with(['steam.exe', '-silent'])

    def launch(self, poll):
        class Process:
            pid = 99
            returncode = 1

            def poll(self):
                return poll

        with patch.object(game_instance.sys, 'platform', 'win32'), \
                patch.object(game_instance, 'game_processes', return_value=[]), \
                patch.object(game_instance, 'ensure_steam'), \
                patch.object(game_instance, '_detached', return_value=Process()), \
                patch.object(game_instance, 'apply_title', return_value=True), \
                patch.object(game_instance.time, 'sleep'):
            return game_instance.launch(self.marker['dir'], log=lambda _: None)

    def test_launch_reports_a_game_that_exits_right_away(self):
        self.assertEqual(self.launch(None)['pid'], 99)
        with self.assertRaisesRegex(RuntimeError, '启动后退出了.*Steam'):
            self.launch(1)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recording_config = patch('orchestrator.recording.LOCAL_CONFIG', self.root / 'local.recording.json')
        self.recording_config.start()
        self.addCleanup(self.recording_config.stop)
        self.source = fake_game(self.root / 'Steam/Celeste')
        self.marker = game_instance.create('main', self.source, self.root / 'instances/main', log=lambda _: None)
        os.environ.pop('CELESTE_DIR', None)
        self.prefs = patch.object(gamepaths, '_console_prefs', return_value={'celeste_dir': self.marker['dir']})
        self.prefs.start()
        self.addCleanup(self.prefs.stop)
        self.deploy = patch.object(gamepaths, '_deploy_answers', return_value={})
        self.deploy.start()
        self.addCleanup(self.deploy.stop)

    def hello(self, runtime):
        return {'bridge': '1.10.1', 'create_save': True, 'dialog_choices': True, 'story_text': True,
                'entry_guard': True, 'runtime_path': runtime, 'process_id': 7}

    def test_current_follows_the_configured_game_location_only(self):
        self.assertEqual(game_instance.current()['dir'], self.marker['dir'])
        self.assertEqual(gamepaths.celeste_dir_info()['instance']['name'], 'main')
        with patch.object(gamepaths, '_console_prefs', return_value={'celeste_dir': str(self.source)}):
            self.assertIsNone(game_instance.current())
            self.assertIsNone(gamepaths.celeste_dir_info()['instance'])
        with patch.object(gamepaths, '_console_prefs', return_value={}):
            self.assertIsNone(game_instance.current())   # 自动查找到的游戏不算副本模式

    def test_identity_accepts_only_the_instance_bridge(self):
        inside = str(Path(self.marker['dir']) / 'Mods/CelesteAIRuntime/bin/CelesteAIRuntime.dll')
        outside = str(self.source / 'Mods/CelesteAIRuntime/bin/CelesteAIRuntime.dll')
        with patch('orchestrator.action_policy.require_pico8_guard'):
            self.assertEqual(game_instance.check_identity(self.hello(inside))['name'], 'main')
            with self.assertRaisesRegex(RuntimeError, '不是 AI 副本'):
                validate_bridge(self.hello(outside))
            with self.assertRaisesRegex(RuntimeError, '没有报告模组路径'):
                validate_bridge(self.hello(None))
            with patch.object(gamepaths, '_console_prefs', return_value={'celeste_dir': str(self.source)}):
                self.assertIsNone(game_instance.check_identity(self.hello(outside)))   # 不是副本模式：照旧

    def test_obs_captures_the_instance_window_by_title(self):
        from tools import obs_setup
        cfg = obs_setup.load_config()
        self.assertEqual(obs_setup.capture_target(cfg), ('Celeste AI Play:SDL_app:Celeste.exe', 1))
        sources = {s['id']: s['settings'] for s in obs_setup.scene_collection(cfg)['sources']}
        self.assertEqual((sources['game_capture']['window'], sources['game_capture']['priority']),
                         ('Celeste AI Play:SDL_app:Celeste.exe', 1))
        self.assertEqual(sources['wasapi_process_output_capture']['priority'], 1)
        with patch.object(gamepaths, '_console_prefs', return_value={'celeste_dir': str(self.source)}):
            self.assertEqual(obs_setup.capture_target(cfg), (cfg['game_window'], 2))

    def test_existing_obs_collection_is_retargeted_only_while_obs_is_closed(self):
        from tools import obs_setup
        for local in ({}, {'collection': '我的测试场景 2', 'collection_file': 'my_test_scene_2'}):
            with self.subTest(local=local):
                (self.root / 'local.recording.json').write_text(json.dumps(local), encoding='utf-8')
                cfg = obs_setup.load_config()
                with patch.object(gamepaths, '_console_prefs', return_value={'celeste_dir': str(self.source)}):
                    collection = obs_setup.scene_collection(cfg)
                custom = {'id': 'game_capture', 'name': 'other', 'settings': {'window': 'Other:Cls:other.exe', 'priority': 2}}
                collection['sources'].append(custom)
                path = self.root / 'obs/basic/scenes' / f"{cfg.get('collection_file', cfg['collection'])}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(collection), encoding='utf-8')
                original = path.read_bytes()
                with patch.object(obs_setup, 'obs_config_dir', return_value=self.root / 'obs'), \
                     patch.object(obs_setup, 'obs_running', return_value=True):
                    self.assertFalse(obs_setup.sync_game_capture(path, cfg))
                    self.assertEqual(path.read_bytes(), original)
                    self.assertIn('关闭 OBS', obs_setup.capture_mismatch(cfg))
                with patch.object(obs_setup, 'obs_config_dir', return_value=self.root / 'obs'), \
                     patch.object(obs_setup, 'obs_running', return_value=False):
                    self.assertTrue(obs_setup.sync_game_capture(path, cfg))
                    self.assertIsNone(obs_setup.capture_mismatch(cfg))
                data = json.loads(path.read_text(encoding='utf-8'))
                windows = {s['name']: (s['settings']['window'], s['settings']['priority'])
                           for s in data['sources'] if s['id'] in obs_setup.GAME_SOURCES}
                self.assertEqual(windows['Celeste 游戏画面'], ('Celeste AI Play:SDL_app:Celeste.exe', 1))
                self.assertEqual(windows['Celeste 游戏声音'], ('Celeste AI Play:SDL_app:Celeste.exe', 1))
                self.assertEqual(windows['other'], ('Other:Cls:other.exe', 2))


if __name__ == '__main__':
    unittest.main()
