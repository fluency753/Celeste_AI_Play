"""Wizard uses real bundle selection/validation; only OS/network boundaries are mocked."""
import contextlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from calibration.component_package_test import bundle, fixture
from tools import component_package as packages, setup_wizard as wizard


class SetupPrebuiltTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        fixture(self.root)
        source = self.root / "game-bridge/Source/Bridge.cs"
        source.parent.mkdir()
        source.write_text('public const string Version = "1.11.1";')
        self.game = self.root / "game"
        self.game.mkdir()
        (self.game / "Celeste.dll").write_bytes(b"installed game")
        self.answers = wizard.Answers(celeste_dir=str(self.game))
        self.enterContext(patch.object(wizard, "REPO", self.root))
        self.enterContext(patch.object(wizard, "CHECK_ONLY", False))
        self.enterContext(patch.object(wizard, "RELEASE_TAG", None))
        self.enterContext(patch.object(wizard, "ask_yes", return_value=True))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_bridge_prefers_prebuilt_and_never_requires_sdk_or_game_exit(self):
        bundle(self.root, self.root / "components")
        with patch.object(wizard, "find_dotnet", side_effect=AssertionError("SDK should not be required")), \
             patch.object(wizard, "process_running", side_effect=AssertionError("do not stop the game")), \
             patch.object(wizard, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            wizard.step_bridge(self.answers)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["-ComponentRoot", str(self.root / "components")])
        self.assertEqual(argv[argv.index("-CelesteDir") + 1], str(self.game))
        self.assertEqual(argv[argv.index("-HudPort") + 1], "47790")

    def test_bridge_deployment_uses_the_configured_controller_hud_port(self):
        bundle(self.root, self.root / "components")
        path = self.root / "configs/local.console.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text('{"machine":{"hud_port":48790}}')
        with patch.object(wizard, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            wizard.step_bridge(self.answers)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("-HudPort") + 1], "48790")

    def test_absent_release_falls_back_to_source_with_sdk(self):
        with patch.object(packages.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("x", 404, "missing", {}, None)), \
             patch.object(wizard, "find_dotnet", return_value="dotnet.exe"), \
             patch.object(wizard, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            wizard.step_bridge(self.answers)
        self.assertNotIn("-ComponentRoot", run.call_args.args[0])

    def test_corrupt_bundle_does_not_fall_back_or_deploy(self):
        bundle(self.root, self.root / "components")
        (self.root / "components/obs/celeste-lockstep.dll").write_bytes(b"corrupt")
        with patch.object(wizard, "run", side_effect=AssertionError("must not deploy")):
            with self.assertRaises(packages.PackageError):
                wizard.step_bridge(self.answers)

    def test_check_is_read_only_and_dotnet_is_optional(self):
        with patch.object(wizard, "CHECK_ONLY", True), \
             patch.object(packages, "download", side_effect=AssertionError("no downloads")), \
             patch.object(wizard, "find_dotnet", return_value=None), \
             patch.object(wizard, "winget_install", side_effect=AssertionError("no installation")), \
             patch.object(wizard, "run", side_effect=AssertionError("no deployment")):
            wizard.step_dotnet(self.answers)
            wizard.step_bridge(self.answers)
        self.assertFalse((self.root / "local").exists())

    def test_source_fallback_accepts_a_newer_compatible_sdk(self):
        exe = self.root / "dotnet.exe"
        exe.write_bytes(b"fixture executable")
        with patch.object(wizard.shutil, "which", return_value=str(exe)), \
             patch.object(wizard, "run", return_value=subprocess.CompletedProcess([], 0, "10.0.400 [SDK]\n")):
            self.assertEqual(wizard.find_dotnet(self.answers), str(exe))

    def recording(self, version="33.0.0", running=False):
        obs = self.root / "obs"
        (obs / "obs-plugins/64bit").mkdir(parents=True, exist_ok=True)
        (obs / "obs-plugins/64bit/celeste-lockstep.dll").write_bytes(b"old")
        self.enterContext(patch.object(wizard, "find_obs_dir", return_value=obs))
        self.enterContext(patch("orchestrator.recording.find_ffmpeg", return_value="ffmpeg.exe"))
        self.enterContext(patch.object(wizard, "LOCAL_RECORDING", self.root / "configs/local.recording.json"))
        self.enterContext(patch("tools.build_obs_plugin.obs_version", return_value=version))
        self.enterContext(patch("tools.build_obs_plugin.obs_running", return_value=running))
        return obs

    def test_recording_installs_compatible_prebuilt_with_backup(self):
        bundle(self.root, self.root / "components")
        obs = self.recording()
        with patch.object(wizard, "run", side_effect=AssertionError("do not build or stop OBS")):
            wizard.step_recording(self.answers)
        target = obs / "obs-plugins/64bit/celeste-lockstep.dll"
        self.assertEqual(target.read_bytes(), (self.root / "components/obs/celeste-lockstep.dll").read_bytes())
        self.assertTrue(list(target.parent.glob("*.backup-*")))

    def test_recording_mismatch_falls_back_but_running_obs_is_never_stopped(self):
        bundle(self.root, self.root / "components")
        self.recording(version="32.2.2")
        with patch.object(wizard, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            wizard.step_recording(self.answers)
        self.assertIn("tools/build_obs_plugin.py", run.call_args.args[0])
        with patch("tools.build_obs_plugin.obs_running", return_value=True), \
             patch.object(wizard, "run", side_effect=AssertionError("never stop the user's OBS")):
            wizard.step_recording(self.answers)


if __name__ == "__main__":
    unittest.main()
