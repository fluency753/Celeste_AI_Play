"""Configuration ownership and restoration through real launch/state consumers.

Every file and provider lives in the fixture's temporary repository. No game,
controller, container, account or user configuration is contacted or changed.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

from calibration.machine_settings_test import Fixture
from orchestrator import machine_settings as ms
from orchestrator.config import RunConfig, autocompact_choice
from orchestrator.launch_options import build, config_for, resume_args
from orchestrator.model_profile import profile_sources, profile_state
from orchestrator.run import parse_args, write_run_state
from orchestrator.runtimes import config_from_state
from orchestrator.session_store import load_json, write_json
from orchestrator.thought import hanzi_limit


class LayerSafetyTests(Fixture):
    def provider(self, *, images=False, hanzi=10, limit=150000, compact='200000'):
        write_json(self.root / 'configs/providers.json', {'anthropic': {
            'images': images, 'models': [{'id': 'opus', 'images': images, 'thought': {'max_hanzi': hanzi},
                                       'context_limit': limit, 'autocompact': compact,
                                       'efforts': ['high'], 'default_effort': 'high'}]}})

    def make_run(self, **extra):
        directory = self.root / 'runs/a/old'
        directory.mkdir(parents=True)
        state = {'agent': 'a', 'save': 's', 'save_slot': 14, 'engine': 'claude', 'provider': 'anthropic',
                 'model': 'opus', 'requested_model': 'opus', 'effort': 'high', 'runtime': 'host',
                 'mode': 'explore', 'knowledge_profile': 'explore', 'sim': False, 'record': False,
                 'max_minutes': 600, 'all_chapters': [1], 'plan_pieces': [[1, 0, None]], 'tuning': {},
                 'bridge_port': 47000, 'mcp_port': 47001, 'session_id': 'fixture', 'recording_mode': 'chapter',
                 **extra}
        write_json(directory / 'run_state.json', state)
        write_json(directory / 'metrics.json', {'splits': [], 'effort': 'high'})
        return directory

    def parse(self, *args):
        with patch.object(sys, 'argv', ['run', '--agent', 'a', '--save', 's', '--no-agent', *args]):
            return parse_args()

    def old_profile(self):
        cfg = RunConfig(agent='a', save='s', model='opus', engine='claude', provider='anthropic')
        cfg.provider_info = {'models': [{'id': 'opus', 'images': True, 'thought': {'max_hanzi': 20},
                                        'context_limit': 300000, 'autocompact': '400000'}]}
        return profile_state(cfg)

    def test_reset_usage_remains_default_after_repeated_options_and_provider_reset(self):
        from overlay import console_api
        from orchestrator import provider_settings as ps, profiles
        pid = profiles.create('legacy', base=profiles.DEFAULT)
        path = self.root / 'local/profiles' / pid / 'profile.json'
        data = load_json(path)
        data['form'] = {'engine': 'claude', 'provider': 'deepseek', 'max_usage': .8, 'quota_wait': False}
        write_json(path, data)
        profiles.select(pid)
        self.assertEqual(console_api.machine_payload()['provider_usage']['deepseek']['max_usage'], .8)
        console_api.save_provider_usage({'provider': 'deepseek', 'usage': {}})
        for _ in range(2):
            self.assertEqual(console_api.machine_payload()['provider_usage']['deepseek'], ms.USAGE_DEFAULTS)
        ps.remove({'id': 'deepseek', 'version': ps.listing()['version']})
        self.assertEqual(console_api.machine_payload()['provider_usage']['deepseek'], ms.USAGE_DEFAULTS)
        self.assertEqual(load_json(self.root / 'configs/local.providers.json')['deepseek']['usage'], {})

    def test_explicit_default_survives_preview_launch_state_and_resume(self):
        self.provider()
        options = {'agent': 'a', 'save': {'name': 's', 'slot': 14, 'create': False},
                   'engine': 'claude', 'provider': 'anthropic', 'model': 'opus', 'effort': 'high',
                   'mode': 'explore', 'runtime': 'host', 'chapters': [1], 'sim': False,
                   'tune': {'thought_max_hanzi': 30}, 'record': False}
        self.assertEqual(hanzi_limit(config_for(options)), 30)
        with patch.object(sys, 'argv', ['run', *build(options), '--no-agent']):
            cfg, _ = parse_args()
        self.assertEqual(hanzi_limit(cfg), 30)
        self.assertEqual(profile_sources(cfg)['thought_max_hanzi'], {'value': 30, 'source': 'run'})
        directory = self.root / 'runs/a/new'
        directory.mkdir(parents=True)
        write_run_state(directory, cfg, 'fixture', None)
        write_json(directory / 'metrics.json', {'splits': [], 'effort': 'high'})
        self.provider(hanzi=5)
        resumed, _ = self.parse('--resume', str(directory))
        self.assertEqual(hanzi_limit(resumed), 30)
        self.assertEqual(resumed.tuning['thought_max_hanzi'], 30)

    def test_same_model_resume_and_chat_keep_effective_profile_after_file_changes(self):
        self.provider()
        directory = self.make_run(**self.old_profile())
        cfg, args = self.parse('--resume', str(directory))
        self.assertTrue(cfg.images)
        self.assertEqual(hanzi_limit(cfg), 20)
        self.assertEqual(autocompact_choice(cfg), ('400000', 'model'))
        self.assertEqual(profile_sources(cfg)['context_limit']['value'], 300000)
        self.assertFalse(cfg.provider_info['images'])  # live account config was not frozen
        chat = config_from_state(load_json(directory / 'run_state.json'))
        self.assertTrue(chat.images)
        self.assertEqual(hanzi_limit(chat), 20)
        out = self.root / 'runs/a/next'
        out.mkdir()
        write_run_state(out, cfg, 'fixture', args.previous)
        self.assertEqual(load_json(out / 'run_state.json')['model_profile'], self.old_profile()['model_profile'])

    def test_resume_explicit_overrides_and_new_model_do_not_reuse_old_defaults(self):
        self.provider()
        directory = self.make_run(**self.old_profile())
        cfg, _ = self.parse('--resume', str(directory), '--tune', 'thought_max_hanzi=30', '--autocompact', 'auto')
        self.assertEqual(hanzi_limit(cfg), 30)
        self.assertEqual(autocompact_choice(cfg), ('auto', 'run'))
        cfg, _ = self.parse('--resume', str(directory), '--model', 'another')
        self.assertFalse(cfg.images)
        self.assertIsNone(profile_sources(cfg)['context_limit']['value'])

    def test_codex_resolved_alias_snapshot_is_used_by_resume_and_note_window(self):
        from calibration.note_reminders_test import runner_fixture
        cfg = RunConfig(agent='a', save='s', model='alias', engine='codex', provider='codex')
        cfg.provider_info = {'models': [{'id': 'gpt-6.1-sol', 'images': True, 'thought': {'max_hanzi': 20},
                                        'context_limit': 100000}]}
        snapshot = profile_state(cfg, resolved_model='gpt-6.1-sol')
        directory = self.make_run(engine='codex', provider='codex', runtime='container',
                                  model='gpt-6.1-sol', requested_model='alias', **snapshot)
        resumed, _ = self.parse('--resume', str(directory))
        self.assertEqual(hanzi_limit(resumed), 20)
        with runner_fixture('codex') as (runner, _, _):
            runner.cfg = resumed
            runner.metrics.state.context_window = 1000000
            self.assertEqual(runner._notes_window(), 100000)

    def test_console_resume_uses_safe_machine_values_and_retains_session_resources(self):
        directory = self.make_run(tuning={'agent_cpus': 2})
        ms.save({'hud_port': 48001, 'hud': False, 'hud_window': False, 'runtime': 'container',
                 'agent_cpus': 1, 'bridge_port': 48002, 'mcp_port': 48003, 'recording_mode': 'continuous'})
        with patch('orchestrator.launch_options.REPO_ROOT', self.root):
            argv = resume_args(directory, current_machine=True)
        with patch.object(sys, 'argv', ['run', *argv, '--no-agent']):
            cfg, args = parse_args()
        self.assertEqual((args.hud_port, args.no_hud, args.no_hud_window), (48001, True, True))
        self.assertEqual((cfg.runtime, cfg.agent_cpus, cfg.bridge_port, cfg.mcp_port), ('host', 2, 47000, 47001))
        self.assertEqual(cfg.recording_mode, 'chapter')
        out = self.root / 'runs/a/next'
        out.mkdir()
        write_run_state(out, cfg, 'fixture', args.previous)
        origin = load_json(out / 'run_state.json')['machine_settings_origin']
        self.assertEqual(origin['current_machine']['hud_port'], 48001)
        self.assertIn('agent_cpus', origin['retained'])
        cfg, args = self.parse('--resume', str(directory), '--use-machine-settings', '--hud-port', '48009')
        self.assertEqual(args.hud_port, 48009)

    def test_legacy_run_without_profile_snapshot_uses_current_defaults(self):
        self.provider()
        cfg, _ = self.parse('--resume', str(self.make_run()))
        self.assertFalse(cfg.images)
        self.assertEqual(hanzi_limit(cfg), 10)

    def test_resume_checks_ports_after_machine_and_session_layers_are_merged(self):
        directory = self.make_run()
        ms.save({'bridge_port': 48002, 'mcp_port': 48003, 'hud_port': 47000})
        with self.assertRaises(SystemExit):
            self.parse('--resume', str(directory), '--use-machine-settings')
        cfg, args = self.parse('--resume', str(directory), '--use-machine-settings', '--hud-port', '48009')
        self.assertEqual((cfg.bridge_port, cfg.mcp_port, args.hud_port), (47000, 47001, 48009))
