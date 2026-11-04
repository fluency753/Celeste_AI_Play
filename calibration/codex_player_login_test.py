"""Offline App Server and sandbox check using only disposable Docker volumes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4

from orchestrator.codex_container import CodexLoginBusy, exec_command, login_args, login_status
from orchestrator.codex_rpc import CodexRPC
from orchestrator.container import PLAYER_LOGIN_MOUNT, docker


@unittest.skipUnless(os.getenv('CELESTE_CODEX_PLAYER_LOGIN_TEST_IMAGE'), 'requires an explicit disposable test image')
class PlayerLoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspaces_share_only_the_serial_login_and_tools_cannot_read_it(self):
        image = os.environ['CELESTE_CODEX_PLAYER_LOGIN_TEST_IMAGE']
        prefix = 'celeste-login-test-' + uuid4().hex[:12]
        a_name, b_name = prefix + '-a', prefix + '-b'
        volume = prefix + '-auth'
        self.addCleanup(docker, 'volume', 'rm', volume, check=False)
        for name in (a_name, b_name):
            self.addCleanup(docker, 'rm', '-f', name, check=False)
            docker('run', '-d', '--init', '--name', name, '--network', 'none', '--pull=never',
                   '--cap-add', 'NET_ADMIN', '--security-opt', 'seccomp=unconfined',
                   '-v', f'{volume}:{PLAYER_LOGIN_MOUNT}', '-e', 'CELESTE_CODEX_PLAYER_LOGIN=1', image)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in (a_name, b_name):
                result = docker('exec', name, 'grep', '-F', f'"{PLAYER_LOGIN_MOUNT}"', '/etc/codex/requirements.toml',
                                check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.install_fixture(a_name, 'fixture-a')
            self.assertTrue(login_status(b_name)['authenticated'])
            a = CodexRPC(root / 'a', container=a_name)
            b = CodexRPC(root / 'b', container=b_name)
            contender = CodexRPC(root / 'contender', container=b_name, require_login=False)
            try:
                await a.start()
                with self.assertRaises(CodexLoginBusy):
                    await b.start()
                with self.assertRaises(CodexLoginBusy):
                    await contender.start()
                self.assertEqual(self.install_fixture(b_name, 'fixture-b', check=False), 73)
            finally:
                await a.close()
                await b.close()
                await contender.close()
            b = CodexRPC(root / 'b', container=b_name)
            try:
                await b.start()
            finally:
                await b.close()
            self.install_fixture(b_name, 'fixture-b')
            docker('exec', a_name, 'python3', '-c',
                   "import json; from pathlib import Path; "
                   "assert json.loads(Path('/home/agent/.codex/auth.json').read_text())['OPENAI_API_KEY'] == 'fixture-b'")
            a = CodexRPC(root / 'a-again', container=a_name)
            try:
                await a.start()
            finally:
                await a.close()
            for name in (a_name, b_name):
                command = (
                    f'! grep -q fixture- {PLAYER_LOGIN_MOUNT}/auth.json && '
                    '! grep -q fixture- /home/agent/.codex/auth.json && echo sandbox-ok'
                )
                result = subprocess.run(exec_command(name, 'codex', 'sandbox', '--', 'bash', '-c', command),
                                        capture_output=True, text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('sandbox-ok', result.stdout)

    @staticmethod
    def install_fixture(container, value, *, check=True):
        return subprocess.run(exec_command(container, *login_args('--with-api-key')), input=value,
                              text=True, capture_output=True, check=check, timeout=30).returncode
