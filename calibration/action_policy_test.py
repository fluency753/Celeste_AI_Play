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
        return RunConfig(agent='test', save='test', model='test', runtime='host')

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
        options = {'agent':'test', 'action_permissions':MIXED}
        cfg = config_for(options)
        self.assertEqual(permissions_for(cfg).snapshot(), MIXED)
        argv = build(options)
        for flag in ('--no-allow-restart-chapter','--no-allow-return-to-map','--allow-save-quit'):
            self.assertIn(flag, argv)
        with patch('sys.argv', ['run', '--agent','test','--save','test','--no-agent','--no-record',
                                '--allow-chapter-reset','--no-allow-restart-chapter','--no-allow-return-to-map']):
            cfg, _ = parse_args()
        self.assertEqual(permissions_for(cfg).snapshot(), MIXED)

    def test_preferences_migrate_and_partial_updates_do_not_collapse(self):
        with tempfile.TemporaryDirectory() as folder:
            control = RunControl(Path(folder))
            with control._db() as db:
                db.execute("INSERT INTO preferences VALUES('allow_chapter_reset','true')")
            self.assertTrue(all(control.action_permissions_preference().values()))
            control.set_action_permissions(MIXED)
            self.assertEqual(RunControl(Path(folder)).action_permissions_preference(), MIXED)
            self.assertFalse(control.chapter_reset_preference())
            with self.assertRaises(ValueError):
                control.enqueue('action_permissions', '{"save_quit":1}')
            control.set_chapter_reset(False)
            self.assertEqual(control.action_permissions_preference(), ActionPermissions().snapshot())

    def test_runtime_snapshot_successor_and_chat_keep_latest_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'source'; root.mkdir()
            cfg = self.cfg(); configure(cfg, ActionPermissions.read(MIXED))
            write_run_state(root, cfg, 'old', None)
            state = json.loads((root/'run_state.json').read_text())
            self.assertEqual(state['action_permissions'], MIXED)
            restored = config_from_state(state)
            self.assertEqual(permissions_for(restored).snapshot(), MIXED)
            changed = {**MIXED, 'return_to_map':True}
            RunControl(root).set_action_permissions(changed)
            target = successor_run(root)
            inherited = json.loads((target/'run_state.json').read_text())
            self.assertEqual(inherited['action_permissions'], changed)
            self.assertEqual(permissions_for(config_from_state(inherited)).snapshot(), changed)

    def test_wire_policy_retains_mandatory_target_and_limited_scope(self):
        cfg = self.cfg(); configure(cfg, ActionPermissions.read(MIXED))
        policy = step_options(cfg, sid='target', slot=10, departure=True)['action_policy']
        self.assertEqual(policy, {'version':1,'permissions':MIXED,'entry_target':{'sid':'target','slot':10},'departure':'current_level'})


class PolicyUiTests(unittest.TestCase):
    def test_both_surfaces_display_mixed_permissions_without_total_switch(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node needed for real UI state code')
        root = Path(__file__).resolve().parents[1]
        for filename, prefix, first, last in (
            ('console.html','live', "  for (const [key,id]", '  $("fastToggle").hidden'),
            ('hud.html','permission', '    const pendingPermissions=', '    const manageable ='),
        ):
            source = (root/'overlay'/filename).read_text(encoding='utf-8')
            offset = source.index('function renderControls') if filename == 'console.html' else source.index('const pendingStory=')
            start = source.index(first, offset); end = source.index(last, start)
            script = '''const inputs={}; const $=id=>inputs[id]??={};
const c={connected:true,chapter_reset_control:true,action_policy_supported:true,allow_chapter_reset:true,action_permissions:%s};
const connected=true,busy=false,pendingStory=null;
%s
process.stdout.write(JSON.stringify(inputs));''' % (json.dumps(MIXED), source[start:end])
            result = subprocess.run([node,'-e',script],capture_output=True,text=True,check=True)
            inputs = json.loads(result.stdout)
            self.assertEqual([inputs[prefix+ident]['value'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')], ['deny','deny','allow'])
            self.assertTrue(all(not inputs[prefix+ident]['disabled'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')))
            old_script = script.replace('action_policy_supported:true,', '')
            old = json.loads(subprocess.run([node,'-e',old_script],capture_output=True,text=True,check=True).stdout)
            self.assertTrue(all(old[prefix+ident]['disabled'] for ident in ('RestartChapter','ReturnToMap','SaveQuit')))
