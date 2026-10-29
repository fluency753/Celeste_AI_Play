"""Container Codex state is separate and imports preserve explicit user ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def test_rollout_reader_tails_bytes_and_rejects_other_paths(self):
        sessions = self.home / 'sessions'
        sessions.mkdir()
        path = sessions / 'fixture.jsonl'
        path.write_bytes(b'old\n')
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME': str(self.home)}):
            cursor, data = codex.read_rollout('fixture', str(path))
            self.assertEqual((cursor, data), (4, b''))
            with path.open('ab') as stream:
                stream.write('new中'.encode())
            end, data = codex.read_rollout('fixture', str(path), cursor)
            self.assertEqual(data, 'new中'.encode())
            self.assertEqual(codex.read_rollout('fixture', str(path), end), (end, b''))
            secret = self.home / 'auth.json'
            secret.write_text('sensitive')
            with self.assertRaisesRegex(RuntimeError, '无法核验'):
                codex.read_rollout('fixture', str(secret), 0)

    def test_auth_is_explicit_stdin_atomic_and_failure_does_not_expose_secrets(self):
        source = self.root/'selected-auth.json'
        secret = {'tokens': {'access_token':'sensitive-fixture'}}
        source.write_text(json.dumps(secret), encoding='utf-8')
        with patch.object(codex, 'exec_command', side_effect=self.worker_command), \
             patch.dict(os.environ, {'CODEX_HOME':str(self.home)}):
            codex.import_auth('fixture', source)
            self.assertEqual(json.loads((self.home/'auth.json').read_text()), secret)
            self.assertEqual(json.loads(source.read_text()), secret)
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
            self.assertEqual((self.home/'auth.json').read_text(), 'original')

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
            self.assertEqual(path.read_text(), original)
            self.assertEqual(source.read_text(), original)
            self.assertEqual([p.name for p in path.parent.iterdir()], ['rollout.jsonl'])
            with self.assertRaises(RuntimeError):
                codex.read_metadata('fixture', str(source))

    def test_runtime_missing_login_has_actionable_message(self):
        outputs = [subprocess.CompletedProcess([],0,f'codex-cli {codex.CODEX_VERSION}\n',''),
                   subprocess.CompletedProcess([],1,'','Not logged in')]
        with patch.object(codex, '_run', side_effect=outputs), patch.object(codex, '_copy_account_auth', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'login.*import-auth'):
                codex.ensure_runtime('fixture')
        with patch.object(codex, '_run', return_value=subprocess.CompletedProcess([],0,'codex-cli wrong','')):
            with self.assertRaisesRegex(RuntimeError, '重新构建'):
                codex.ensure_runtime('fixture', require_login=False)

    def test_explicit_account_initializes_only_missing_workspace_auth(self):
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
            if command == ('codex', 'login', 'status'):
                present = (home/'auth.json').is_file()
                return subprocess.CompletedProcess(command,0 if present else 1,'Logged in using ChatGPT' if present else '','')
            self.assertEqual(command[0], 'python3')
            return subprocess.run([sys.executable,*command[1:]], input=input, capture_output=True,
                                  text=True, encoding='utf-8', env={**os.environ,'CODEX_HOME':str(home)}, timeout=timeout)

        with patch.object(codex, '_run', side_effect=external_cli), \
             patch.object(container, 'docker', return_value=subprocess.CompletedProcess([],0,'image-fixture\n','')), \
             patch.object(container, 'ensure_container', return_value='account-fixture') as factory:
            codex.ensure_runtime('workspace-fixture')
            config = factory.call_args.args[0]
            self.assertEqual(config.engine, 'codex')
            self.assertEqual(config.image, 'image-fixture')
            self.assertIn('chatgpt.com', config.provider_domains)
            self.assertEqual(json.loads((workspace/'auth.json').read_text()), original)
            self.assertEqual(json.loads((account/'auth.json').read_text()), original)
            (workspace/'auth.json').write_text(json.dumps({'tokens': {'access_token':'workspace-refreshed'}}),encoding='utf-8')
            calls.clear()
            codex.ensure_runtime('workspace-fixture')
            self.assertFalse(any(name == 'account-fixture' for name, _ in calls))
            self.assertEqual(json.loads((workspace/'auth.json').read_text())['tokens']['access_token'],'workspace-refreshed')


if __name__ == '__main__':
    unittest.main()
