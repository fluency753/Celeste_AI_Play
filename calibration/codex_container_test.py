"""Container Codex state is separate and imports preserve explicit user ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from orchestrator import codex_container as codex
from orchestrator import container
from orchestrator.config import RunConfig


class ContainerCodexTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.home = self.root/'home'
        self.home.mkdir()

    def worker_command(self, name, *command, **kwargs):
        self.assertEqual(name, 'fixture')
        self.assertEqual(command[0], 'python3')
        return [sys.executable, *command[1:]]

    def test_codex_create_mounts_own_volume_and_openai_domains_only(self):
        for engine in ('codex', 'claude'):
            with self.subTest(engine=engine):
                cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine=engine)
                cfg.provider_info = {'allowed_domains':['chatgpt.com','auth.openai.com','api.openai.com']} if engine == 'codex' else {}
                with patch('orchestrator.config.REPO_ROOT', self.root), \
                     patch.object(container, 'REPO_ROOT', self.root), patch.object(container, 'docker') as docker:
                    container._create(cfg, 'fixture')
                args = docker.call_args.args
                codex_volumes = [a for a in args if a.endswith(':/home/agent/.codex')]
                if engine == 'codex':
                    self.assertEqual(codex_volumes, ['celeste-codex-fixture:/home/agent/.codex'])
                    self.assertIn('ALLOW_ANTHROPIC=0', args)
                    self.assertIn('PROVIDER_DOMAINS=' + ' '.join(cfg.provider_domains), args)
                    self.assertEqual(container._labels(cfg)['celeste-ai-play.codex_home'], '1')
                else:
                    self.assertEqual(codex_volumes, [])
                    self.assertNotIn('celeste-ai-play.codex_home', container._labels(cfg))
                self.assertIn('celeste-claude-fixture:/home/agent/.claude', args)

    def test_exec_uses_native_container_client_without_host_binary(self):
        with patch.object(codex, 'find_docker_exe', return_value='docker'):
            command = codex.exec_command('fixture', 'codex', 'app-server', '--listen', 'stdio://')
            self.assertEqual(command, ['docker','exec','-i','-u','agent','-w','/workspace',
                                      '-e','CODEX_HOME=/home/agent/.codex','fixture',
                                      'codex','app-server','--listen','stdio://'])
            with self.assertRaises(ValueError):
                codex.exec_command('-bad', 'codex')

    def test_context_snapshot_copies_only_portable_settings(self):
        path = self.root / 'config.toml'
        with patch.dict(os.environ, {'CODEX_HOME': str(self.root)}):
            self.assertEqual(codex.context_config_snapshot(), '')
            path.write_text('model_context_window = 500000\nmodel_auto_compact_token_limit = 450000\n'
                            'sandbox_mode = "danger-full-access"\n[mcp_servers.fixture]\nsecret = "fixture"\n',
                            encoding='utf-8-sig')
            self.assertEqual(json.loads(codex.context_config_snapshot()),
                             {'model_context_window': 500000, 'model_auto_compact_token_limit': 450000})
            # A bad value or an unreadable file is reported and skipped, never blocking creation.
            logs = []
            path.write_text('model_context_window = true\nmodel_auto_compact_token_limit = 450000\n', encoding='utf-8')
            self.assertEqual(json.loads(codex.context_config_snapshot(logs.append)),
                             {'model_auto_compact_token_limit': 450000})
            self.assertTrue(any('model_context_window' in line for line in logs))
            logs.clear()
            path.write_text('model_context_window = true', encoding='utf-8')
            self.assertEqual(codex.context_config_snapshot(logs.append), '')
            path.write_text('model_context_window = [unclosed', encoding='utf-8')
            self.assertEqual(codex.context_config_snapshot(logs.append), '')
            self.assertTrue(any('TOMLDecodeError' in line and '默认值' in line for line in logs), logs)

    def test_context_init_is_atomic_one_time_and_preserves_existing_config(self):
        snapshot = '{"model_context_window":500000}'
        config = self.home / 'config.toml'
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME': str(self.home)}):
            codex.initialize_context_config('fixture', snapshot)
            self.assertEqual(tomllib.loads(config.read_text(encoding='utf-8')), {'model_context_window': 500000})
            config.unlink()
            codex.initialize_context_config('fixture', snapshot)
            self.assertFalse(config.exists())
            (self.home / '.celeste-context-config-initialized').unlink()
            config.write_text('model_context_window = 600000\n', encoding='utf-8')
            codex.initialize_context_config('fixture', snapshot)
            self.assertEqual(tomllib.loads(config.read_text(encoding='utf-8')), {'model_context_window': 600000})
            self.assertFalse(list(self.home.glob('.context-config-*')))

    def test_initialization_retry_uses_the_creation_snapshot(self):
        cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine='codex')
        path = self.root / 'config.toml'
        path.write_text('model_context_window = 500000\n', encoding='utf-8')
        labels, mounts = {}, []
        volume_snapshot = None
        runs = 0

        def docker(*args, **kwargs):
            nonlocal volume_snapshot, runs
            if args[:2] == ('image', 'inspect'):
                output = 'image-id'
            elif args[0] == 'inspect':
                if not labels:
                    return subprocess.CompletedProcess(args, 1, '', '')
                output = f'true|image-id|{json.dumps(labels)}|{json.dumps(mounts)}'
            elif args[:2] == ('volume', 'ls'):
                output = container._codex_volume(cfg) if volume_snapshot else ''
            elif args[:2] == ('volume', 'create'):
                volume_snapshot = args[3].split('=', 1)[1]
                output = container._codex_volume(cfg)
            elif args[:2] == ('volume', 'inspect'):
                output = volume_snapshot
            elif args[0] == 'run':
                runs += 1
                if runs == 1:
                    raise RuntimeError('run failed before creating a container')
                labels.update(dict(args[i + 1].split('=', 1) for i, arg in enumerate(args) if arg == '--label'))
                mounts.extend({'Destination': destination, 'Type': 'bind', 'RW': True, 'Source': str(source.resolve())}
                              for destination, source in (('/workspace', cfg.workspace_dir),
                                                          ('/workspace/.git', cfg.workspace_dir / '.git')))
                output = 'container-id'
            else:
                output = ''
            return subprocess.CompletedProcess(args, 0, output, '')

        with patch('orchestrator.config.REPO_ROOT', self.root), patch.object(container, 'REPO_ROOT', self.root), \
             patch('orchestrator.kb_access.prepare_view'), patch.object(container, 'docker', side_effect=docker), \
             patch.object(container, '_save_tmp', side_effect=AssertionError('reuse must not rebuild')), \
             patch.dict(os.environ, {'CODEX_HOME': str(self.root)}), \
             patch.object(codex, 'initialize_context_config', side_effect=[RuntimeError('lost reply'), None]) as init:
            with self.assertRaisesRegex(RuntimeError, 'run failed'):
                container.ensure_container(cfg, log=lambda _: None)
            path.write_text('model_context_window = 600000\n', encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'lost reply'):
                container.ensure_container(cfg, log=lambda _: None)
            container.ensure_container(cfg, log=lambda _: None)
        self.assertEqual([json.loads(call.args[1]) for call in init.call_args_list],
                         [{'model_context_window': 500000}, {'model_context_window': 500000}])

    def test_existing_volume_skips_desktop_snapshot(self):
        cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine='codex')
        with patch.object(container, 'docker', return_value=subprocess.CompletedProcess([], 0,
                          container._codex_volume(cfg) + '\n', '')) as docker, \
             patch.object(codex, 'context_config_snapshot') as snapshot:
            container._prepare_context_volume(cfg)
        docker.assert_called_once()
        snapshot.assert_not_called()

    def test_only_claude_agent_creation_mounts_the_preset_simulator(self):
        for engine in ('codex', 'claude'):
            for enabled in (False, True):
                with self.subTest(engine=engine, sim=enabled):
                    # Codex 只有限额（主控预测）；Claude 不限额时直接调用 /sim
                    cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine=engine,
                                    sim=enabled, pause_limit=enabled and engine == 'codex')
                    with patch('orchestrator.config.REPO_ROOT', self.root), \
                         patch.object(container, 'REPO_ROOT', self.root), patch.object(container, 'docker') as docker:
                        container._create(cfg, 'fixture')
                        labels = container._labels(cfg)
                        git_mount = f'{cfg.workspace_dir / ".git"}:/workspace/.git'
                        self.assertTrue((cfg.workspace_dir / '.git').is_dir())
                    args = docker.call_args.args
                    mounts = [args[index + 1] for index, arg in enumerate(args[:-1]) if arg == '-v']
                    self.assertTrue(any(mount.endswith(':/workspace') for mount in mounts))
                    self.assertTrue(any(mount.endswith(':/kb:ro') for mount in mounts))
                    if engine == 'codex':
                        # Codex 的预置模拟器只由主控私有 worker 使用
                        self.assertFalse(any(':/sim' in mount for mount in mounts), mounts)
                        self.assertEqual(labels['celeste-ai-play.sim'], 'host-only-v1')
                        self.assertIn('celeste-ai-play.sim=host-only-v1', args)
                    else:
                        # Claude 不限额：sim/dist 只读挂到 /sim，智能体直接调用（限额时同 Codex，见 container_tmp_test）
                        self.assertEqual(any(m == f'{self.root / "sim" / "dist"}:/sim:ro' for m in mounts), enabled, mounts)
                        self.assertEqual(sum(':/sim' in mount for mount in mounts), int(enabled), mounts)
                        self.assertEqual(labels['celeste-ai-play.sim'], '1' if enabled else '0')
                    # 两个执行器都把 /workspace/.git 固定为真实目录并单独挂载
                    self.assertIn(git_mount, mounts)
                    self.assertEqual(labels['celeste-ai-play.git_mount'], 'workspace-dir-v1')

    def test_auth_is_explicit_stdin_atomic_and_failure_does_not_expose_secrets(self):
        source = self.root/'selected-auth.json'
        secret = {'tokens': {'access_token':'sensitive-fixture'}}
        source.write_text(json.dumps(secret), encoding='utf-8')
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME':str(self.home)}):
            codex.import_auth('fixture', source)
            self.assertEqual(json.loads((self.home/'auth.json').read_text(encoding='utf-8')), secret)
            self.assertEqual(json.loads(source.read_text(encoding='utf-8')), secret)
            self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['auth.json'])
        with patch.object(codex, '_run', return_value=subprocess.CompletedProcess([],1,'','sensitive-fixture')):
            with self.assertRaisesRegex(RuntimeError, '导入') as error:
                codex.import_auth('fixture', source)
            self.assertNotIn('sensitive-fixture', str(error.exception))

    def test_bad_auth_rejected_before_child_and_retains_existing_cache(self):
        (self.home/'auth.json').write_text('original', encoding='utf-8')
        source = self.root/'selected-auth.json'
        for text in ('invalid', '{}', '[]'):
            source.write_text(text, encoding='utf-8')
            with patch.object(codex, '_run') as run, self.assertRaises(ValueError):
                codex.import_auth('fixture', source)
            run.assert_not_called()
            self.assertEqual((self.home/'auth.json').read_text(encoding='utf-8'), 'original')

    def test_session_import_keeps_original_and_never_overwrites(self):
        source = self.root/'host'/'sessions'/'2026'/'10'/'30'/'rollout.jsonl'
        source.parent.mkdir(parents=True)
        payload = {'id':'fixture','cwd':'host-path','dynamic_tools':[{'name':'celeste_act'}]}
        original = json.dumps({'type':'session_meta','payload':payload}) + '\n' + json.dumps({'type':'history'})
        source.write_text(original, encoding='utf-8')
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME':str(self.home)}):
            destination = codex.import_session('fixture', source)
            self.assertEqual(destination, codex.CODEX_HOME+'/sessions/2026/10/30/rollout.jsonl')
            path = self.home/'sessions/2026/10/30/rollout.jsonl'
            self.assertEqual(codex.read_metadata('fixture', str(path)), payload)
            with self.assertRaisesRegex(RuntimeError, '已存在'):
                codex.import_session('fixture', source)
            self.assertEqual(path.read_text(encoding='utf-8'), original)
            self.assertEqual(source.read_text(encoding='utf-8'), original)
            self.assertEqual([p.name for p in path.parent.iterdir()], ['rollout.jsonl'])
            with self.assertRaises(RuntimeError):
                codex.read_metadata('fixture', str(source))

    def test_runtime_missing_login_has_actionable_message(self):
        outputs = [subprocess.CompletedProcess([],0,f'codex-cli {codex.CODEX_VERSION}\n',''),
                   subprocess.CompletedProcess([],1,'','Not logged in')]
        with patch.object(codex, '_run', side_effect=outputs):
            with self.assertRaisesRegex(codex.CodexLoginRequired, '--container fixture login'):
                codex.ensure_runtime('fixture')
        with patch.object(codex, '_run', return_value=subprocess.CompletedProcess([],0,'codex-cli wrong','')):
            with self.assertRaisesRegex(RuntimeError, '重新构建'):
                codex.ensure_runtime('fixture', require_login=False)

    def test_metadata_reader_blocks_pending_or_malformed_migration_before_resume(self):
        path = self.home / 'sessions/2026/11/rollout-fixture.jsonl'
        path.parent.mkdir(parents=True)
        payload = {'id': 'fixture', 'cwd': '/workspace', 'dynamic_tools': []}
        original = json.dumps({'type': 'session_meta', 'payload': payload}) + '\n'
        path.write_text(original, encoding='utf-8')
        journal = self.home / 'simulation-schema-migrations/fixture/journal.json'
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME': str(self.home)}):
            self.assertEqual(codex.read_metadata('fixture', str(path)), payload)
            journal.parent.mkdir(parents=True)
            for raw in ('{"state":"prepared"}', '{"state":"unknown"}', '{}',
                        'invalid json', 'null', '[]', '"not an object"'):
                with self.subTest(journal=raw):
                    journal.write_text(raw, encoding='utf-8')
                    with self.assertRaisesRegex(RuntimeError, '迁移尚未完成.*拒绝开始模型轮次') as error:
                        codex.read_metadata('fixture', str(path))
                    self.assertIn('新建准备会话', str(error.exception))
                    self.assertEqual(path.read_text(encoding='utf-8'), original)
            journal.unlink()
            journal.mkdir()
            with self.assertRaisesRegex(RuntimeError, '迁移尚未完成'):
                codex.read_metadata('fixture', str(path))
            journal.rmdir()
            journal.write_text('{"state":"complete"}', encoding='utf-8')
            self.assertEqual(codex.read_metadata('fixture', str(path)), payload)
            self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_metadata_reader_rejects_symlinked_migration_journal_even_if_complete(self):
        path = self.home / 'sessions/2026/11/rollout-fixture.jsonl'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'fixture'}}) + '\n', encoding='utf-8')
        journal = self.home / 'simulation-schema-migrations/fixture/journal.json'
        journal.parent.mkdir(parents=True)
        target = self.root / 'other-journal.json'
        target.write_text('{"state":"complete"}', encoding='utf-8')
        try:
            journal.symlink_to(target)
        except OSError as error:
            self.skipTest(f'Host cannot create symlink: {error}')
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME': str(self.home)}), \
             self.assertRaisesRegex(RuntimeError, '迁移尚未完成'):
            codex.read_metadata('fixture', str(path))
        self.assertEqual(target.read_text(encoding='utf-8'), '{"state":"complete"}')

    def test_metadata_reader_ignores_workspace_json_and_re_shadow_modules(self):
        workspace = self.root / 'untrusted-workspace'
        workspace.mkdir()
        marker = self.root / 'shadow-imported'
        malicious = f'open({str(marker)!r}, "w").write("shadowed")\nraise RuntimeError("untrusted module imported")\n'
        for name in ('json.py', 're.py'):
            (workspace / name).write_text(malicious, encoding='utf-8')
        path = self.home / 'sessions/rollout-fixture.jsonl'
        path.parent.mkdir()
        payload = {'id': 'fixture', 'cwd': '/workspace', 'dynamic_tools': []}
        path.write_text(json.dumps({'type': 'session_meta', 'payload': payload}) + '\n', encoding='utf-8')
        spawn = subprocess.run

        def external_spawn(argv, **kwargs):
            self.assertEqual(argv[1:3], ['-I', '-c'])
            return spawn(argv, cwd=workspace, **kwargs)

        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.object(codex.subprocess, 'run', side_effect=external_spawn), \
             patch.dict(os.environ, {'CODEX_HOME': str(self.home), 'PYTHONPATH': str(workspace)}):
            self.assertEqual(codex.read_metadata('fixture', str(path)), payload)
        self.assertFalse(marker.exists())

    def test_workspace_login_never_seeds_from_the_catalog_account(self):
        account = self.root/'account'
        workspace = self.root/'workspace'
        account.mkdir()
        workspace.mkdir()
        original = {'tokens': {'access_token':'isolated-account-fixture'}}
        (account/'auth.json').write_text(json.dumps(original), encoding='utf-8')
        calls = []

        def external_cli(name, *command, input=None, timeout=30):
            calls.append((name, command))
            home = account if name == 'account-fixture' else workspace
            if command == ('codex', '--version'):
                return subprocess.CompletedProcess(command,0,f'codex-cli {codex.CODEX_VERSION}\n','')
            if command == tuple(codex.login_args('status')):
                present = (home/'auth.json').is_file()
                return subprocess.CompletedProcess(command,0 if present else 1,'Logged in using ChatGPT' if present else '','')
            self.assertEqual(command[0], 'python3')
            return subprocess.run([sys.executable,*command[1:]], input=input, capture_output=True,
                                  text=True, encoding='utf-8', env={**os.environ,'CODEX_HOME':str(home)}, timeout=timeout)

        with patch.object(codex, '_run', side_effect=external_cli), \
             patch.object(codex, 'ensure_account_container') as factory:
            with self.assertRaisesRegex(codex.CodexLoginRequired, '--container workspace-fixture login'):
                codex.ensure_runtime('workspace-fixture')
            self.assertFalse((workspace/'auth.json').exists())
            factory.assert_not_called()
            self.assertEqual(json.loads((account/'auth.json').read_text(encoding='utf-8')), original)
            (workspace/'auth.json').write_text(json.dumps({'tokens': {'access_token':'workspace-refreshed'}}),encoding='utf-8')
            calls.clear()
            codex.ensure_runtime('workspace-fixture')
            self.assertFalse(any(name == 'account-fixture' for name, _ in calls))
            self.assertEqual(json.loads((workspace/'auth.json').read_text(encoding='utf-8'))['tokens']['access_token'],'workspace-refreshed')


if __name__ == '__main__':
    unittest.main()
