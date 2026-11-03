"""配置变了要重建智能体容器时，旧容器的 /tmp（智能体常把程序放在 /tmp/claude-<uid>）先打包到本机，
新容器建好后原样放回；保存失败就不重建，放回失败就报错并保留打包。"""
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import container
from orchestrator.config import RunConfig


def tar_bytes(files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.uid = info.gid = 1500
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeDocker:
    def __init__(self, tmp_files, found=None, save_fails=False, labels=None, mounts=None, image='sha:old'):
        self.archive = tar_bytes(tmp_files)
        self.found = len(tmp_files) if found is None else found
        self.save_fails = save_fails
        self.labels, self.mounts, self.image = labels or {}, mounts or [], image
        self.calls, self.restored = [], None

    def docker(self, *args, check=True, timeout=120):
        self.calls.append(args)
        out = ""
        if args[:2] == ("image", "inspect"):
            out = "sha:new"
        elif args[0] == "inspect":
            out = f'true|{self.image}|{json.dumps(self.labels)}|{json.dumps(self.mounts)}'
        elif args[0] == "info":
            out = "24"
        elif args[0] == "exec" and "find /tmp -type f | wc -l" in args:
            out = str(self.found)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def run(self, argv, stdout=None, stdin=None, stderr=None, timeout=None):
        self.calls.append(tuple(argv[1:]))
        if argv[1:3] == ["cp", "-a"]:
            self.restored = stdin.read()
            return SimpleNamespace(returncode=0, stderr=b"")
        if self.save_fails:
            return SimpleNamespace(returncode=1, stderr=b"no such container")
        stdout.write(self.archive)
        return SimpleNamespace(returncode=0, stderr=b"")


class CarryTmpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.cfg = RunConfig(agent="carry", save="s", model="m", knowledge_profile="explore")

    def ensure(self, fake):
        created = []
        with patch.object(container, "docker", fake.docker), patch.object(container.subprocess, "run", fake.run), \
             patch.object(container, "find_docker_exe", lambda: "docker"), \
             patch("orchestrator.config.REPO_ROOT", self.root), \
             patch("orchestrator.kb_access.KB_ROOT", self.root / 'kb'), \
             patch.object(container, "_create", lambda cfg, name: created.append(name) or fake.calls.append(("create",))):
            name = container.ensure_container(self.cfg, log=lambda *_: None)
        return name, created

    def order(self, fake):
        return [c[0] if c[0] != "cp" else ("save" if c[1] != "-a" else "restore") for c in fake.calls
                if c[0] in {"cp", "rm", "create"}]

    def test_rebuild_carries_tmp_over(self):
        fake = FakeDocker({"tmp/claude-1500/bs/bsearch.py": b"print(1)\n", "tmp/claude-1500/reg/csim.py": b"x = 1\n"})
        name, created = self.ensure(fake)
        self.assertEqual(created, [name])
        self.assertEqual(self.order(fake), ["save", "rm", "create", "restore"])   # 先存再删，建好再放回
        self.assertEqual(fake.restored, fake.archive)
        self.assertEqual(list((self.root / "local" / "container_tmp").iterdir()), [])  # 核对无误后删掉本机打包

    def test_save_failure_leaves_the_container_alone(self):
        fake = FakeDocker({"tmp/a.py": b"1"}, save_fails=True)
        with self.assertRaises(RuntimeError):
            self.ensure(fake)
        self.assertNotIn("rm", self.order(fake))

    def test_short_restore_keeps_the_archive(self):
        fake = FakeDocker({"tmp/a.py": b"1", "tmp/b.py": b"2"}, found=1)
        with self.assertRaisesRegex(RuntimeError, "打包保留在"):
            self.ensure(fake)
        kept = list((self.root / "local" / "container_tmp").iterdir())
        self.assertEqual(len(kept), 1)
        with tarfile.open(kept[0]) as tar:
            self.assertEqual(sorted(m.name for m in tar), ["tmp/a.py", "tmp/b.py"])

    def matching_labels(self):
        with patch('orchestrator.config.REPO_ROOT', self.root):
            return container._labels(self.cfg)

    def test_matching_labels_do_not_hide_exposed_simulator_mount(self):
        for destination in ('/sim', '/sim/private'):
            with self.subTest(destination=destination):
                fake = FakeDocker({'tmp/search.py': b'print(1)\n'}, image='sha:new',
                                  labels=self.matching_labels(),
                                  mounts=[{'Destination': destination, 'Source': '/private/simulator'}])
                self.assertEqual(fake.labels['celeste-ai-play.sim'], 'host-only-v1')
                name, created = self.ensure(fake)
                self.assertEqual(created, [name])
                self.assertEqual(self.order(fake), ['save', 'rm', 'create', 'restore'])
                self.assertEqual(fake.restored, fake.archive)

    def test_exposed_simulator_is_not_removed_when_tmp_backup_fails(self):
        fake = FakeDocker({'tmp/search.py': b'print(1)\n'}, save_fails=True, image='sha:new',
                          labels=self.matching_labels(), mounts=[{'Destination': '/sim'}])
        with self.assertRaises(RuntimeError):
            self.ensure(fake)
        self.assertEqual(self.order(fake), ['save'])

    def test_matching_container_without_simulator_mount_is_reused(self):
        fake = FakeDocker({}, image='sha:new', labels=self.matching_labels(),
                          mounts=[{'Destination': '/workspace'}, {'Destination': '/kb'}])
        _, created = self.ensure(fake)
        self.assertEqual(created, [])
        self.assertEqual(self.order(fake), [])

    def test_old_simulator_capability_label_requires_safe_rebuild(self):
        labels = self.matching_labels()
        labels['celeste-ai-play.sim'] = '1'
        fake = FakeDocker({'tmp/search.py': b'print(1)\n'}, image='sha:new', labels=labels)
        name, created = self.ensure(fake)
        self.assertEqual(created, [name])
        self.assertEqual(self.order(fake), ['save', 'rm', 'create', 'restore'])

    def git_mounts(self):
        with patch('orchestrator.config.REPO_ROOT', self.root):
            workspace = self.cfg.workspace_dir.resolve()
        return [{'Destination': destination, 'Source': str(source), 'Type': 'bind', 'RW': True}
                for destination, source in [('/workspace', workspace), ('/workspace/.git', workspace / '.git')]]

    def test_codex_git_mount_is_checked_even_when_labels_match(self):
        self.cfg.engine = 'codex'
        for damage in ('missing', 'source', 'readonly', 'volume', 'nested', 'duplicate'):
            with self.subTest(damage=damage):
                mounts = self.git_mounts()
                if damage == 'missing':
                    mounts.pop()
                elif damage == 'source':
                    mounts[-1]['Source'] = str(self.root / 'elsewhere')
                elif damage == 'readonly':
                    mounts[-1]['RW'] = False
                elif damage == 'volume':
                    mounts[-1]['Type'] = 'volume'
                elif damage == 'nested':
                    mounts.append({'Destination': '/workspace/.git/hooks'})
                else:
                    mounts.append(dict(mounts[-1]))
                fake = FakeDocker({'tmp/keep.py': b'keep'}, image='sha:new',
                                  labels=self.matching_labels(), mounts=mounts)
                name, created = self.ensure(fake)
                self.assertEqual(created, [name])
                self.assertEqual(self.order(fake), ['save', 'rm', 'create', 'restore'])
                self.assertEqual(fake.restored, fake.archive)

    def test_codex_matching_git_mount_is_reused(self):
        self.cfg.engine = 'codex'
        fake = FakeDocker({}, image='sha:new', labels=self.matching_labels(), mounts=self.git_mounts())
        self.assertEqual(self.ensure(fake)[1], [])
        self.assertEqual(self.order(fake), [])

    @unittest.skipUnless(os.name == 'nt', 'Docker Desktop drive mapping is Windows-only')
    def test_desktop_git_mount_spelling_reuses_only_the_same_directory(self):
        self.cfg.engine = 'codex'
        mounts = self.git_mounts()
        for row in mounts:
            path = Path(row['Source'])
            row['Source'] = '/run/desktop/mnt/host/' + path.drive[0].lower() + '/' + path.as_posix()[3:]
        fake = FakeDocker({}, image='sha:new', labels=self.matching_labels(), mounts=mounts)
        self.assertEqual(self.ensure(fake)[1], [])
        mounts[-1]['Source'] += '-different'
        fake = FakeDocker({'tmp/keep': b'yes'}, image='sha:new', labels=self.matching_labels(), mounts=mounts)
        self.assertTrue(self.ensure(fake)[1])
        self.assertEqual(self.order(fake), ['save', 'rm', 'create', 'restore'])

    def test_codex_gitdir_file_is_rejected_before_backup_or_removal(self):
        self.cfg.engine = 'codex'
        git = Path(self.git_mounts()[-1]['Source'])
        git.parent.mkdir(parents=True)
        git.write_text('gitdir: /outside\n', encoding='utf-8')
        fake = FakeDocker({'tmp/keep': b'yes'})
        with self.assertRaisesRegex(RuntimeError, '真实目录'):
            self.ensure(fake)
        self.assertEqual(self.order(fake), [])
        self.assertEqual(git.read_text(), 'gitdir: /outside\n')

    def test_codex_git_replacement_during_backup_preserves_container_and_archive(self):
        self.cfg.engine = 'codex'
        git = Path(self.git_mounts()[-1]['Source'])
        fake = FakeDocker({'tmp/keep': b'yes'})
        original_run = fake.run

        def backup_then_replace(*args, **kwargs):
            result = original_run(*args, **kwargs)
            git.rmdir()
            git.write_text('gitdir: /outside\n', encoding='utf-8')
            return result

        fake.run = backup_then_replace
        with self.assertRaisesRegex(RuntimeError, '真实目录'):
            self.ensure(fake)
        self.assertEqual(self.order(fake), ['save'])
        kept = list((self.root / 'local/container_tmp').iterdir())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), fake.archive)

    def test_codex_git_symlink_is_rejected_without_touching_its_target(self):
        self.cfg.engine = 'codex'
        git = Path(self.git_mounts()[-1]['Source'])
        git.parent.mkdir(parents=True)
        target = self.root / 'outside'
        target.mkdir()
        (target / 'keep').write_text('untouched', encoding='utf-8')
        try:
            git.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'Host cannot create symlink: {error}')
        fake = FakeDocker({'tmp/keep': b'yes'})
        with self.assertRaisesRegex(RuntimeError, '真实目录'):
            self.ensure(fake)
        self.assertEqual(self.order(fake), [])
        self.assertEqual((target / 'keep').read_text(), 'untouched')

    @unittest.skipUnless(os.name == 'nt', 'Windows directory junction regression')
    def test_codex_git_junction_during_backup_keeps_archive_and_target(self):
        self.cfg.engine = 'codex'
        git = Path(self.git_mounts()[-1]['Source'])
        target = self.root / 'junction-outside'
        target.mkdir()
        (target / 'keep').write_text('untouched', encoding='utf-8')
        fake = FakeDocker({'tmp/keep': b'yes'})
        original_run = fake.run
        run_host = subprocess.run

        def backup_then_link(*args, **kwargs):
            result = original_run(*args, **kwargs)
            git.rmdir()
            linked = run_host(['cmd', '/c', 'mklink', '/J', str(git), str(target)],
                              capture_output=True, text=True, timeout=10)
            self.assertEqual(linked.returncode, 0, linked.stdout + linked.stderr)
            return result

        fake.run = backup_then_link
        with self.assertRaisesRegex(RuntimeError, '真实目录'):
            self.ensure(fake)
        self.assertEqual(self.order(fake), ['save'])
        kept = list((self.root / 'local/container_tmp').iterdir())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_bytes(), fake.archive)
        self.assertEqual((target / 'keep').read_text(), 'untouched')


if __name__ == "__main__":
    unittest.main()
