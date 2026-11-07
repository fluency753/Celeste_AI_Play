"""关于页的检查更新、一键更新与换版本：用临时的本地 git 仓库模拟 GitHub，不联网、不动本仓库。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from overlay import console_api

GIT = shutil.which("git")
FIXTURE_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def write_versions(root: Path, console: str, bridge: str, updatable: bool = True):
    (root / "overlay").mkdir(exist_ok=True)
    (root / "game-bridge" / "Source").mkdir(parents=True, exist_ok=True)
    feature = "\n\ndef update_apply():\n    pass\n" if updatable else "\n"
    (root / "overlay" / "console_api.py").write_text(f'CONSOLE_VERSION = "{console}"{feature}', encoding="utf-8")
    (root / "game-bridge" / "Source" / "Bridge.cs").write_text(f'public const string Version = "{bridge}";\n',
                                                                encoding="utf-8")


class GitFixture(unittest.TestCase):
    """模拟的 GitHub（裸仓库）、作者那边的克隆与用户本机的克隆。"""

    def setUp(self):
        # Fixture setup and the real console subprocesses must use the same
        # Git configuration when interpreting the checkout's line endings.
        self.enterContext(patch.dict(os.environ, FIXTURE_GIT_ENV))
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

    def publish(self, console="1.1.0", bridge="2.0.0", extra=None, tag=None, updatable=True, message="second"):
        write_versions(self.upstream, console, bridge, updatable)
        for name, text in (extra or {}).items():
            (self.upstream / name).write_text(text, encoding="utf-8")
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-m", message)
        git(self.upstream, "push", "origin", "HEAD:main")
        if tag:
            git(self.upstream, "tag", "-a", tag, "-m", tag)
            git(self.upstream, "push", "origin", tag)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.upstream, capture_output=True, text=True,
                              check=True).stdout.strip()


@unittest.skipIf(GIT is None, "没有 git")
class ConsoleUpdateTests(GitFixture):
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
        with self.assertRaisesRegex(ValueError, "先撤销"):
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



@unittest.skipIf(GIT is None, "没有 git")
class ConsoleRollbackTests(GitFixture):
    """换版本：列出正式版、公测版（标签）与最近的开发版；换过去之后一键更新能回到最新，本机数据不动。"""

    def history(self):
        old = self.publish(console="1.0.5", updatable=False, message="太旧")
        stable = self.publish(console="1.1.0", tag="v1.1.0", message="正式")
        beta = self.publish(console="1.2.0", bridge="2.1.0", tag="v1.2.0-beta.1", message="公测")
        git(self.upstream, "tag", "not-a-version")
        git(self.upstream, "push", "origin", "not-a-version")
        latest = self.publish(console="1.2.0", bridge="2.1.0", extra={"requirements.txt": "y\n"}, message="最新")
        self.assertTrue(console_api.update_apply()["updated"])
        return old, stable, beta, latest

    def test_versions_are_grouped_and_filtered(self):
        old, stable, beta, latest = self.history()
        result = console_api.versions()
        self.assertTrue(result["can_rollback"], result["reason"])
        rows = {row["ref"]: row for row in result["versions"]}
        self.assertNotIn(old, rows)                          # 没有一键更新的旧版本：换过去就回不来，不列
        self.assertEqual((rows[stable]["kind"], rows[stable]["tag"], rows[stable]["console"]), ("release", "v1.1.0", "1.1.0"))
        self.assertEqual((rows[beta]["kind"], rows[beta]["tag"], rows[beta]["bridge"]), ("prerelease", "v1.2.0-beta.1", "2.1.0"))
        self.assertEqual((rows[latest]["kind"], rows[latest]["current"], rows[latest]["subject"]), ("commit", True, "最新"))
        self.assertEqual(sum(1 for row in result["versions"] if row["ref"] in (stable, beta)), 2)   # 打了标签的不重复列
        self.assertFalse(any(row["tag"] == "not-a-version" for row in result["versions"]))

    def test_rollback_keeps_local_data_and_update_comes_back(self):
        _, stable, _, latest = self.history()
        (self.local / "local").mkdir()
        (self.local / "local" / "mine.json").write_text("{}\n", encoding="utf-8")
        done = console_api.rollback({"ref": stable})
        self.assertTrue(done["restart"])
        self.assertEqual(done["after"]["console"], "1.1.0")
        self.assertTrue(any("桥接 Mod" in note for note in done["notes"]))
        self.assertTrue(any("requirements.txt" in note for note in done["notes"]))
        self.assertTrue((self.local / "local" / "mine.json").is_file())
        branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=self.local, capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(branch, "main")                     # 仍在分支上：一键更新与 git pull --ff-only 照常可用
        check = console_api.update_check()
        self.assertEqual((check["behind"], check["ahead"], check["can_update"]), (2, 0, True))
        self.assertEqual(console_api.update_apply()["after"]["console"], "1.2.0")
        self.assertTrue(next(r for r in console_api.versions()["versions"] if r["ref"] == latest)["current"])

    def test_blockers(self):
        _, stable, _, latest = self.history()
        for bad in (None, {}, {"ref": "v1.1.0"}, {"ref": stable, "x": 1}, {"ref": "0" * 40}):
            with self.assertRaises(ValueError):
                console_api.rollback(bad)
        with self.assertRaisesRegex(ValueError, "已经是这个版本"):
            console_api.rollback({"ref": latest})
        with patch.object(console_api, "orchestrator_status", return_value={"running": True}):
            self.assertIn("有主控在运行", console_api.versions(fetch=False)["reason"])
            with self.assertRaisesRegex(ValueError, "有主控在运行"):
                console_api.rollback({"ref": stable})
        (self.local / "overlay" / "console_api.py").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "先撤销"):
            console_api.rollback({"ref": stable})
        git(self.local, "checkout", "--", ".")
        git(self.local, "checkout", "--detach", "HEAD")
        with self.assertRaisesRegex(ValueError, "不在分支上"):
            console_api.rollback({"ref": stable})
        git(self.local, "checkout", "main")
        (self.local / "mine.txt").write_text("x\n", encoding="utf-8")
        git(self.local, "add", "mine.txt")
        git(self.local, "commit", "-m", "mine")
        self.assertFalse(console_api.versions(fetch=False)["can_rollback"])
        with self.assertRaisesRegex(ValueError, "GitHub 上没有的提交"):
            console_api.rollback({"ref": stable})
        self.assertIn("1.2.0", (self.local / "overlay" / "console_api.py").read_text(encoding="utf-8"))


@unittest.skipIf(GIT is None, "没有 git")
class ConsoleUpdateChannelTests(GitFixture):
    """检查更新与一键更新按通道：正式版、公测版（含正式版，谁新用谁）、开发版。"""

    history = ConsoleRollbackTests.history

    def test_channels_pick_their_latest_version_and_fast_forward(self):
        _, stable, beta, latest = self.history()
        console_api.rollback({"ref": stable})
        release = console_api.update_check(channel="release")
        self.assertEqual((release["latest"]["tag"], release["behind"], release["can_update"]), ("v1.1.0", 0, False))
        self.assertIn("已经是最新版本（正式版）", release["reason"])
        pre = console_api.update_check(channel="prerelease")
        self.assertEqual((pre["latest"]["tag"], pre["target"], pre["can_update"]), ("v1.2.0-beta.1", beta, True))
        done = console_api.update_apply("prerelease")
        self.assertEqual((done["after"]["console"], done["after"]["bridge"]), ("1.2.0", "2.1.0"))
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.local, capture_output=True, text=True).stdout.strip(), beta)
        newer = console_api.update_check(channel="release")
        self.assertFalse(newer["can_update"])
        self.assertIn("还新", newer["reason"])
        with self.assertRaisesRegex(ValueError, "还新"):
            console_api.update_apply("release")
        dev = console_api.update_check(channel="dev")
        self.assertEqual((dev["target"], dev["behind"], dev["can_update"]), (dev["upstream"], 1, True))
        self.assertEqual(console_api.update_apply("dev")["after"]["commit"], latest[:7])

    def test_no_release_yet_and_bad_channel(self):
        self.publish()
        result = console_api.update_check(channel="release")
        self.assertIsNone(result["latest"])
        self.assertIn("还没有发布正式版", result["reason"])
        with self.assertRaisesRegex(ValueError, "还没有发布公测版"):
            console_api.update_apply("prerelease")
        for bad in ("stable", None, ""):
            with self.assertRaises(ValueError):
                console_api.update_check(channel=bad)


if __name__ == "__main__":
    unittest.main()
