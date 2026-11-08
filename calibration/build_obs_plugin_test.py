"""OBS 插件源码编译取 libobs 头文件：先 HTTPS（不需要密钥），不通再试 SSH，都不通时讲清原因；
git 不弹出凭据或主机指纹的询问。只替换 git 命令本身，其余（目录、稀疏检出设置）是真的。"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import build_obs_plugin as plugin


class HeaderFetchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.enterContext(patch.object(plugin, "BUILD", Path(folder.name)))
        self.calls = []

    def fake_git(self, failing):
        def run(argv, cwd=None, check=False, env=None):
            self.calls.append((argv[1:], env))
            if argv[1] == "init":
                (Path(cwd) / ".git" / "info").mkdir(parents=True)
            if argv[1] == "fetch":
                remote = next(a for a, _ in reversed(self.calls) if a[:2] == ["remote", "set-url"])[-1]
                if remote in failing:
                    raise subprocess.CalledProcessError(128, argv)
            if argv[1] == "checkout":
                (Path(cwd) / "libobs").mkdir()
                (Path(cwd) / "libobs" / "obs-module.h").write_text("")
            return subprocess.CompletedProcess(argv, 0)
        return run

    def fetched_from(self):
        remotes = [a[-1] for a, _ in self.calls if a[:2] == ["remote", "set-url"]]
        return remotes

    def test_https_first_without_any_key(self):
        with patch.object(plugin.subprocess, "run", side_effect=self.fake_git(failing=())):
            dest = plugin.fetch_headers("32.2.2")
        self.assertTrue((dest / "libobs" / "obs-module.h").exists())
        self.assertEqual(self.fetched_from(), ["https://github.com/obsproject/obs-studio.git"])
        env = self.calls[0][1]
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])

    def test_ssh_is_the_fallback(self):
        with patch.object(plugin.subprocess, "run",
                          side_effect=self.fake_git(failing=("https://github.com/obsproject/obs-studio.git",))):
            plugin.fetch_headers("32.2.2")
        self.assertEqual(self.fetched_from(), list(plugin.HEADER_REMOTES))

    def test_both_failing_explains_instead_of_a_traceback(self):
        with patch.object(plugin.subprocess, "run", side_effect=self.fake_git(failing=plugin.HEADER_REMOTES)):
            with self.assertRaises(SystemExit) as caught:
                plugin.fetch_headers("99.0.0")
        self.assertIn("取不到 OBS 99.0.0 的 libobs 头文件", str(caught.exception))
        self.assertIn("git@github.com:obsproject/obs-studio.git", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
