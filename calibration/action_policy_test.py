"""Policy configuration/consumer regressions; only temporary files and fake IO."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.action_policy import ActionPermissions, configure, permissions_for, permissions_from_state, step_options
from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.launch_options import build, config_for
from orchestrator.run import parse_args, write_run_state
from orchestrator.runtimes import config_from_state
from orchestrator.chat import successor_run


MIXED = {'restart_chapter': False, 'return_to_map': False, 'save_quit': True}


class PolicyConfigurationTests(unittest.TestCase):
    def cfg(self):
        # 三项独立选择只在探索模式里有意义（速通模式固定全部允许，见下面的速通用例）
        return RunConfig(agent='test', save='test', model='test', runtime='host', mode='explore', knowledge_profile='explore')

    def test_legacy_import_and_canonical_precedence(self):
        cfg = self.cfg()
        cfg.allow_chapter_reset = True
        self.assertTrue(all(permissions_for(cfg).snapshot().values()))
        cfg.action_permissions = MIXED.copy()
        self.assertEqual(permissions_for(cfg).snapshot(), MIXED)
        self.assertEqual(permissions_from_state({'action_permissions':MIXED, 'allow_chapter_reset':True}).snapshot(), MIXED)
        self.assertEqual(permissions_from_state({'allow_chapter_reset':True}).snapshot(), ActionPermissions.legacy(True).snapshot())

    def test_permissions_validate_keys_completeness_and_types(self):
        for data in ({}, {**MIXED,'teleport':True}, {**MIXED,'save_quit':1}, {**MIXED,'save_quit':'false'}, []):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ActionPermissions.read(data)
        for data in ({}, {'teleport':True}, {'save_quit':0}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ActionPermissions().update(data)

    def test_console_and_cli_preserve_three_independent_choices(self):
        options = {'agent':'test', 'mode':'explore', 'action_permissions':MIXED}
        cfg = config_for(options)
        self.assertEqual(permissions_for(cfg).snapshot(), MIXED)
        argv = build(options)
        for flag in ('--no-allow-restart-chapter','--no-allow-return-to-map','--allow-save-quit'):
            self.assertIn(flag, argv)
        with patch('sys.argv', ['run', '--agent','test','--save','test','--no-agent','--no-record','--mode','explore',
                                '--allow-chapter-reset','--no-allow-restart-chapter','--no-allow-return-to-map']):
            cfg, _ = parse_args()
        self.assertEqual(permissions_for(cfg).snapshot(), MIXED)
        self.assertFalse(cfg.full_control)

    def test_speedrun_opens_every_game_action_and_rejects_partial_denials(self):
        everything = {'restart_chapter': True, 'return_to_map': True, 'save_quit': True}
        options = {'agent':'test', 'mode':'speedrun', 'action_permissions':MIXED}
        cfg = config_for(options)
        self.assertTrue(cfg.full_control)
        self.assertEqual(permissions_for(cfg).snapshot(), everything)
        self.assertFalse(any(flag.startswith('--no-allow') for flag in build(options)))
        with patch('sys.argv', ['run', '--agent','test','--save','test','--no-agent','--no-record','--no-sim']):
            cfg, _ = parse_args()
        self.assertTrue(cfg.full_control)
        self.assertEqual(permissions_for(cfg).snapshot(), everything)
        # 之后任何来源（运行中偏好、续跑快照）都不改变速通的三项权限
        configure(cfg, ActionPermissions.read(MIXED))
        self.assertEqual(permissions_for(cfg).snapshot(), everything)
        with tempfile.TemporaryDirectory() as folder:
            speedrun = RunConfig(agent='test', save='test', model='test', runtime='host')
            configure(speedrun, ActionPermissions.read(MIXED))     # 早于 1.41.0 的速通运行存的是部分允许
            write_run_state(Path(folder), speedrun, 'old', None)
            state = json.loads((Path(folder) / 'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['action_permissions'], MIXED)
        self.assertEqual(permissions_for(config_from_state(state)).snapshot(), everything)
        for flag in ('--no-allow-save-quit', '--no-allow-chapter-reset'):
            with self.subTest(flag=flag), patch('sys.argv', ['run','--agent','test','--save','test','--no-agent','--no-record',
                                                              '--no-sim', flag]), self.assertRaisesRegex(SystemExit, '速通模式固定开放'):
                parse_args()

    def test_preferences_migrate_and_partial_updates_do_not_collapse(self):
        with tempfile.TemporaryDirectory() as folder:
            control = RunControl(Path(folder))
            with control._db() as db:
                db.execute("INSERT INTO preferences VALUES('allow_chapter_reset','true')")
            self.assertTrue(all(control.action_permissions_preference().values()))
            control.set_action_permissions(MIXED)
            self.assertEqual(RunControl(Path(folder)).action_permissions_preference(), MIXED)
            self.assertFalse(all(control.action_permissions_preference().values()))
            with self.assertRaises(ValueError):
                control.enqueue('action_permissions', '{"save_quit":1}')
            control.set_action_permissions(ActionPermissions.legacy(False).snapshot())
            self.assertEqual(control.action_permissions_preference(), ActionPermissions().snapshot())

    def test_runtime_snapshot_successor_and_chat_keep_latest_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'; root.mkdir()
            cfg = self.cfg(); configure(cfg, ActionPermissions.read(MIXED))
            write_run_state(root, cfg, 'old', None)
            state = json.loads((root/'run_state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['action_permissions'], MIXED)
            restored = config_from_state(state)
            self.assertEqual(permissions_for(restored).snapshot(), MIXED)
            changed = {**MIXED, 'return_to_map':True}
            RunControl(root).set_action_permissions(changed)
            target = successor_run(root)
            inherited = json.loads((target/'run_state.json').read_text(encoding='utf-8'))
            self.assertEqual(inherited['action_permissions'], changed)
            self.assertEqual(permissions_for(config_from_state(inherited)).snapshot(), changed)

    def test_wire_policy_retains_mandatory_target_and_limited_scope(self):
        cfg = self.cfg(); configure(cfg, ActionPermissions.read(MIXED))
        policy = step_options(cfg, sid='target', slot=10, departure=True)['action_policy']
        self.assertEqual(policy, {'version':1,'permissions':MIXED,'entry_target':{'sid':'target','slot':10},'departure':'current_level',
                                  'settings_menus': False})


class SettingsMenuGuardTests(unittest.TestCase):
    def test_all_permissions_require_bridge_settings_guard(self):
        from orchestrator.action_policy import require_settings_guard
        cfg = RunConfig(agent='test', save='test', model='test', runtime='host', mode='explore', knowledge_profile='explore')
        configure(cfg, ActionPermissions.read(MIXED))
        require_settings_guard(cfg, {})                   # 权限受限时选项本来就按未知项拦下，不需要新桥接
        configure(cfg, ActionPermissions(True, True, True))
        with self.assertRaisesRegex(ValueError, '1.18.0'):
            require_settings_guard(cfg, {'settings_menu_guard': False})
        require_settings_guard(cfg, {'settings_menu_guard': True})

    def test_speedrun_sends_every_bridge_key_and_explains_settings_block(self):
        from types import SimpleNamespace
        from orchestrator.action_policy import block_note
        from orchestrator.game_session import GameSession, LETTERS
        self.assertEqual(GameSession._allowed_letters(SimpleNamespace(cfg=SimpleNamespace(full_control=True))), LETTERS)
        note = block_note({'action': 'settings_menu', 'rule_id': 'menu.settings', 'actual': '选项'})
        self.assertIn('「选项」', note)
        self.assertIn('B 返回', note)


class PolicyUiTests(unittest.TestCase):
    def test_both_surfaces_display_mixed_permissions_without_total_switch(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node needed for real UI state code')
        root = Path(__file__).resolve().parents[1]
        for filename, prefix, first, last in (
            ('console.html','live', "  for (const [key,id]", '  $("fastToggle").hidden'),
            ('hud.html','permission', '    const pendingPermissions=', "    $('cutsceneMode').value"),
        ):
            source = (root/'overlay'/filename).read_text(encoding='utf-8')
            offset = source.index('function renderControls') if filename == 'console.html' else source.index('const pendingStory=')
            start = source.index(first, offset); end = source.index(last, start)
            script = '''const inputs={}; const $=id=>inputs[id]??={}; const document={activeElement:null};
const c={connected:true,chapter_reset_control:true,action_policy_supported:true,allow_chapter_reset:true,action_permissions:%s};
const connected=true,busy=false,pendingStory=null;
%s
process.stdout.write(JSON.stringify(inputs));''' % (json.dumps(MIXED), source[start:end])
            result = subprocess.run([node,'-e',script],capture_output=True,text=True,check=True)
            inputs = json.loads(result.stdout)
            self.assertEqual([inputs[prefix+ident]['value'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')], ['deny','deny','allow'])
            self.assertTrue(all(not inputs[prefix+ident]['disabled'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')))
            # The user is choosing in one dropdown: polling must not snap it back.
            focused = script.replace('const document={activeElement:null};',
                                     "const document={activeElement:$('%sRestartChapter')}; document.activeElement.value='allow';" % prefix)
            chosen = json.loads(subprocess.run([node,'-e',focused],capture_output=True,text=True,check=True).stdout)
            self.assertEqual([chosen[prefix+ident]['value'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')], ['allow','deny','allow'])
            old_script = script.replace('action_policy_supported:true,', '')
            old = json.loads(subprocess.run([node,'-e',old_script],capture_output=True,text=True,check=True).stdout)
            self.assertTrue(all(old[prefix+ident]['disabled'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')))
            fixed_script = script.replace('action_policy_supported:true,', 'action_policy_supported:true,action_permissions_fixed:true,')
            fixed = json.loads(subprocess.run([node,'-e',fixed_script],capture_output=True,text=True,check=True).stdout)
            self.assertTrue(all(fixed[prefix+ident]['disabled'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')))
