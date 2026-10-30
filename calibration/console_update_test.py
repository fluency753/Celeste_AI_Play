"""关于页的检查更新与一键更新：用临时的本地 git 仓库模拟 GitHub，不联网、不动本仓库。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from overlay import console_api

GIT = shutil.which("git")
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


def write_versions(root: Path, console: str, bridge: str):
    (root / "overlay").mkdir(exist_ok=True)
    (root / "game-bridge" / "Source").mkdir(parents=True, exist_ok=True)
    (root / "overlay" / "console_api.py").write_text(f'CONSOLE_VERSION = "{console}"\n', encoding="utf-8")
    (root / "game-bridge" / "Source" / "Bridge.cs").write_text(f'public const string Version = "{bridge}";\n',
                                                                encoding="utf-8")


@unittest.skipIf(GIT is None, "没有 git")
class ConsoleUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.origin, self.upstream, self.local = base / "origin.git", base / "upstream", base / "local"
        git(base, "init", "--bare", "-b", "main", str(self.origin))
        git(base, "clone", str(self.origin), str(self.upstream))
        write_versions(self.upstream, "1.0.0", "2.0.0")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-m", "first")
        git(self.upstream, "push", "origin", "HEAD:main")
        git(base, "clone", str(self.origin), str(self.local))
        for item in (patch.object(console_api, "REPO_ROOT", self.local),
                     patch.object(console_api, "orchestrator_status", return_value={"running": False})):
            item.start()
            self.addCleanup(item.stop)

    def publish(self, console="1.1.0", bridge="2.0.0", extra=None):
        write_versions(self.upstream, console, bridge)
        for name, text in (extra or {}).items():
            (self.upstream / name).write_text(text, encoding="utf-8")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-m", "second")
        git(self.upstream, "push", "origin", "HEAD:main")

    def test_up_to_date(self):
        result = console_api.update_check()
        self.assertEqual(result["behind"], 0)
        self.assertFalse(result["can_update"])
        self.assertEqual(result["latest"]["console"], "1.0.0")
        with self.assertRaisesRegex(ValueError, "已经是最新版本"):
            console_api.update_apply()

    def test_newer_version_is_shown_and_fast_forwarded(self):
        self.publish(console="1.1.0", bridge="2.1.0", extra={"requirements.txt": "x\n"})
        result = console_api.update_check()
        self.assertEqual((result["behind"], result["ahead"]), (1, 0))
        self.assertTrue(result["can_update"])
        self.assertEqual(result["current"]["console"], "1.0.0")
        self.assertEqual((result["latest"]["console"], result["latest"]["bridge"]), ("1.1.0", "2.1.0"))
        done = console_api.update_apply()
        self.assertTrue(done["restart"])
        self.assertEqual(done["after"]["console"], "1.1.0")
        self.assertTrue(any("桥接 Mod" in note for note in done["notes"]))
        self.assertTrue(any("requirements.txt" in note for note in done["notes"]))
        self.assertIn("1.1.0", (self.local / "overlay" / "console_api.py").read_text(encoding="utf-8"))

    def test_local_changes_or_running_controller_block_the_update(self):
        self.publish()
        (self.local / "overlay" / "console_api.py").write_text("changed\n", encoding="utf-8")
        result = console_api.update_check()
        self.assertFalse(result["can_update"])
        self.assertIn("overlay/console_api.py", result["reason"])
        with self.assertRaisesRegex(ValueError, "先提交或撤销"):
            console_api.update_apply()
        git(self.local, "checkout", "--", ".")
        with patch.object(console_api, "orchestrator_status", return_value={"running": True}):
            with self.assertRaisesRegex(ValueError, "有主控在运行"):
                console_api.update_apply()
        self.assertTrue(console_api.update_check()["can_update"])

    def test_local_commits_are_not_overwritten(self):
        self.publish()
        (self.local / "mine.txt").write_text("x\n", encoding="utf-8")
        git(self.local, "add", "mine.txt")
        git(self.local, "commit", "-m", "mine")
        result = console_api.update_check()
        self.assertEqual(result["ahead"], 1)
        self.assertFalse(result["can_update"])
        self.assertIn("不能直接快进", result["reason"])


if __name__ == "__main__":
    unittest.main()
