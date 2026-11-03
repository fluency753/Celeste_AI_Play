"""Opt-in offline native sandbox regression; no model, real auth, or shared container.

CELESTE_CODEX_GIT_IMAGE=<test image> python -m unittest calibration.codex_git_permissions_test
The image must contain the production managed policy and codex-shell.py.
"""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import textwrap
from types import SimpleNamespace
import unittest
import uuid

from orchestrator import container
from orchestrator.config import find_docker_exe


@unittest.skipUnless(os.environ.get('CELESTE_CODEX_GIT_IMAGE'), 'Set CELESTE_CODEX_GIT_IMAGE for isolated Docker regression')
class CodexGitPermissionsTests(unittest.TestCase):
    def docker(self, *args, check=True, timeout=60):
        result = subprocess.run([self.executable, *args], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=timeout)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def driver(self, source, *, user='root'):
        return self.docker('exec', '-u', user, self.name, 'python3', '-c', textwrap.dedent(source))

    def native(self, command, *, profile='celeste-write', check=True):
        encoded = base64.b64encode(command.encode()).decode()
        return self.docker('exec', '-u', 'agent', '-w', '/workspace',
                           '-e', 'CODEX_HOME=/home/agent/.codex', self.name,
                           'codex', 'sandbox', '--include-managed-config', '-P', profile,
                           '-C', '/workspace', '--', 'python3',
                           '/usr/local/lib/celeste/codex-shell.py', '--seconds', '20',
                           '--command-base64', encoded, check=check)

    def native_python(self, source, **kwargs):
        return self.native('python3 -c ' + shlex.quote(textwrap.dedent(source)), **kwargs)

    def test_git_commits_and_native_boundaries(self):
        self.executable = find_docker_exe()
        self.name = 'celeste-git-permissions-' + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix='celeste-codex-git-') as folder:
            workspace = Path(folder) / 'workspace'
            git = container._workspace_git(SimpleNamespace(workspace_dir=workspace))
            workspace.chmod(0o777)
            git.chmod(0o777)
            try:
                self.docker('run', '-d', '--rm', '--init', '--name', self.name, '--network', 'none',
                            '--security-opt', 'seccomp=unconfined',
                            '--mount', f'type=bind,source={workspace},target=/workspace',
                            '--mount', f'type=bind,source={git},target=/workspace/.git',
                            '--entrypoint', '/bin/sh', os.environ['CELESTE_CODEX_GIT_IMAGE'],
                            '-c', 'exec sleep 600')
                self.check_boundaries()
            finally:
                removed = self.docker('rm', '-f', self.name, check=False)
                if removed.returncode:
                    self.assertIn('No such container', removed.stderr, removed.stdout + removed.stderr)

    def check_boundaries(self):
        self.driver('''
            import json, os, pathlib, subprocess
            outside = pathlib.Path('/opt/git-boundary')
            outside.mkdir()
            (outside / 'sentinel').write_text('outside-original')
            subprocess.run(['git', 'init', '--bare', str(outside / 'external.git')], check=True, capture_output=True)
            auth = pathlib.Path('/home/agent/.codex/auth.json')
            auth.parent.mkdir(parents=True, exist_ok=True)
            auth.write_text(json.dumps({'OPENAI_API_KEY':'sk-fake-offline-fixture'}))
            for root in (outside, auth.parent):
                for path in [root, *root.rglob('*')]:
                    os.chown(path, 1500, 1500)
        ''')
        # A loopback connection succeeds without the native sandbox despite --network none.
        listener = """
import pathlib, socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen()
pathlib.Path('/opt/git-boundary/port').write_text(str(s.getsockname()[1]))
while True:
    conn, _ = s.accept()
    with conn:
        conn.sendall(b'LOCAL_LISTENER_OK')
"""
        self.docker('exec', '-d', self.name, 'python3', '-c', listener)
        baseline = self.driver('''
            import pathlib, socket, time, subprocess
            portfile = pathlib.Path('/opt/git-boundary/port')
            for _ in range(100):
                if portfile.exists(): break
                time.sleep(.02)
            assert portfile.exists(), 'listener did not start'
            port = int(portfile.read_text())
            with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
                assert connection.recv(100) == b'LOCAL_LISTENER_OK'
            sentinel = pathlib.Path('/opt/git-boundary/sentinel')
            sentinel.write_text('outside-original')
            assert 'sk-fake-offline-fixture' in pathlib.Path('/home/agent/.codex/auth.json').read_text()
            subprocess.run(['git', '--git-dir=/opt/git-boundary/external.git', 'config', 'fixture.baseline', 'yes'], check=True)
            print(port)
        ''', user='agent')
        port = int(baseline.stdout.strip())
        self.native("set -eu; git init; git config user.name Fixture; git config user.email fixture@example.invalid; "
                    "printf 'hook-marker\\nhook-results.json\\nlinked-worktree/\\n' > .gitignore; "
                    "printf first > note; git add note .gitignore; git commit -m first")
        # Install an actual executable Git hook. Its child inherits both production sandboxes.
        hook = textwrap.dedent(f'''\
            #!/usr/bin/env python3
            import json, pathlib, socket
            results = {{}}
            pathlib.Path('/workspace/hook-marker').write_text('hook-executed')
            for name, operation in [
                ('outside', lambda: pathlib.Path('/opt/git-boundary/sentinel').write_text('escaped')),
                ('credential', lambda: pathlib.Path('/home/agent/.codex/auth.json').read_text()),
                ('network', lambda: socket.create_connection(('127.0.0.1', {port}), timeout=1)),
            ]:
                try:
                    value = operation()
                    if name == 'network': value.close()
                    results[name] = True
                except OSError:
                    results[name] = False
            pathlib.Path('/workspace/hook-results.json').write_text(json.dumps(results))
            assert not any(results.values()), results
        ''')
        self.driver(f"import pathlib; p=pathlib.Path('/workspace/.git/hooks/pre-commit'); p.write_text({hook!r}); p.chmod(0o755)")
        self.native("set -eu; printf second > note; git add note; git commit -m second; "
                    "test $(git rev-list --count HEAD) = 2; test -z \"$(git status --porcelain)\"; "
                    "test $(cat hook-marker) = hook-executed")
        self.driver('''
            import json, pathlib
            assert json.loads(pathlib.Path('/workspace/hook-results.json').read_text()) == {
                'outside':False, 'credential':False, 'network':False}
            assert pathlib.Path('/opt/git-boundary/sentinel').read_text() == 'outside-original'
        ''')
        read = self.native_python('''
            import json, pathlib
            results = []
            for name in ('/workspace/read-profile-write', '/workspace/.git/read-profile-write'):
                try: pathlib.Path(name).write_text('forbidden')
                except OSError: results.append(False)
                else: results.append(True)
            print(json.dumps(results))
            assert results == [False, False], results
        ''', profile='celeste-read', check=False)
        self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
        self.assertIn('[false, false]', read.stdout)
        # Root of the nested bind cannot be replaced, including a replacement symlink.
        self.native_python('''
            import os, pathlib
            root = pathlib.Path('/workspace/.git')
            replacement = pathlib.Path('/workspace/git-replacement')
            replacement.symlink_to('/opt/git-boundary/external.git', target_is_directory=True)
            for operation in (lambda: root.rename('/workspace/git-moved'),
                              lambda: root.rmdir(), lambda: root.unlink(),
                              lambda: os.replace(replacement, root)):
                try: operation()
                except OSError: pass
                else: raise AssertionError('Git mount root was replaced')
            replacement.unlink()
            assert root.is_dir() and not root.is_symlink()
        ''')
        # Both an internal symlink and Git's gitdir indirection must keep outside data read-only.
        self.native_python('''
            import pathlib, subprocess
            link = pathlib.Path('/workspace/.git/escape')
            link.symlink_to('/opt/git-boundary', target_is_directory=True)
            try:
                try: (link / 'sentinel').write_text('escaped')
                except OSError: pass
                else: raise AssertionError('Internal symlink escaped')
            finally: link.unlink()
            worktree = pathlib.Path('/workspace/linked-worktree')
            worktree.mkdir()
            (worktree / '.git').write_text('gitdir: /opt/git-boundary/external.git\\n')
            result = subprocess.run(['git', '-c', 'safe.directory=/workspace/linked-worktree',
                                     '-C', str(worktree), 'config', 'fixture.escape', 'yes'],
                                    capture_output=True, text=True)
            assert result.returncode != 0, 'gitdir escaped'
            reason = result.stderr.lower()
            assert 'dubious ownership' not in reason, reason
            assert 'read-only file system' in reason or 'permission denied' in reason, reason
        ''')
        self.driver('''
            import pathlib, subprocess
            assert pathlib.Path('/opt/git-boundary/sentinel').read_text() == 'outside-original'
            result = subprocess.run(['git', '--git-dir=/opt/git-boundary/external.git', 'config', '--get', 'fixture.escape'], capture_output=True)
            assert result.returncode == 1, result.stdout
        ''', user='agent')
        self.native('set -eu; test -z "$(git status --porcelain)"; test $(git rev-list --count HEAD) = 2')


if __name__ == '__main__':
    unittest.main()
