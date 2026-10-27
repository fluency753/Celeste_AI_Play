"""配置变了要重建智能体容器时，旧容器的 /tmp（智能体常把程序放在 /tmp/claude-<uid>）先打包到本机，
新容器建好后原样放回；保存失败就不重建，放回失败就报错并保留打包。"""
import io
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
    def __init__(self, tmp_files, found=None, save_fails=False):
        self.archive = tar_bytes(tmp_files)
        self.found = len(tmp_files) if found is None else found
        self.save_fails = save_fails
        self.calls, self.restored = [], None

    def docker(self, *args, check=True, timeout=120):
        self.calls.append(args)
        out = ""
        if args[:2] == ("image", "inspect"):
            out = "sha:new"
        elif args[0] == "inspect":
            out = 'true|sha:old|{}'
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


if __name__ == "__main__":
    unittest.main()
