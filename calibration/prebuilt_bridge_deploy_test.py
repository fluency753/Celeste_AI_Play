"""Execute the real PowerShell deployment on a disposable fake installation."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from calibration.component_package_test import bundle, fixture

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell required")
class PrebuiltBridgeDeployTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "repo"
        fixture(self.root)
        for name in ("deploy_bridge.ps1", "component_package.py"):
            shutil.copy2(ROOT / "tools" / name, self.root / "tools" / name)
        self.component = self.base / "components"
        bundle(self.root, self.component)
        self.game = self.base / "game"
        (self.game / "Saves").mkdir(parents=True)
        (self.game / "Celeste.dll").write_bytes(b"isolated target")
        for name in ("CelesteAIBridge", "CelesteAIRuntime"):
            path = self.game / "Mods" / name
            (path / "bin").mkdir(parents=True)
            (path / "everest.yaml").write_text("old metadata")
            (path / "bin" / (name + ".dll")).write_bytes(b"old " + name.encode())
        self.settings = self.game / "Saves/modsettings-CelesteAIRuntime.celeste"
        self.settings.write_bytes(b"Port: 49999\r\nClockPort: 49998\r\n")

    def deploy(self, running=False):
        # Only the OS process-list boundary is replaced. Never connect to a game.
        process = "[pscustomobject]@{Name='Celeste.exe';ExecutablePath=(Join-Path $env:FIXTURE_GAME 'Celeste.exe')}" if running else "@()"
        script = f"function Get-CimInstance {{ {process} }}; & $env:FIXTURE_DEPLOY -CelesteDir $env:FIXTURE_GAME -ComponentRoot $env:FIXTURE_COMPONENTS"
        env = dict(os.environ, FIXTURE_GAME=str(self.game), FIXTURE_COMPONENTS=str(self.component),
                   FIXTURE_DEPLOY=str(self.root / "tools/deploy_bridge.ps1"), FIXTURE_PYTHON=sys.executable)
        # If the precompiled branch accidentally calls a build, fail loudly.
        script = "function dotnet { throw 'SDK must not be called' }; function python { & $env:FIXTURE_PYTHON @args }; " + script
        return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                              env=env, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace")

    def test_offline_deploy_installs_both_mods_without_sdk_and_preserves_settings(self):
        before = self.settings.read_bytes()
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("CelesteAIBridge", "CelesteAIRuntime"):
            dest = self.game / "Mods" / name / "bin" / (name + ".dll")
            self.assertEqual(dest.read_bytes(), (self.component / "mods" / name / "bin" / (name + ".dll")).read_bytes())
            backups = list((self.root / "local/backups").glob(f"*/{name}/bin/{name}.dll"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"old " + name.encode())
        self.assertEqual(self.settings.read_bytes(), before)

    def test_running_game_with_different_runtime_is_refused_before_any_write(self):
        result = self.deploy(running=True)
        self.assertNotEqual(result.returncode, 0)
        for name in ("CelesteAIBridge", "CelesteAIRuntime"):
            self.assertEqual((self.game / "Mods" / name / "bin" / (name + ".dll")).read_bytes(), b"old " + name.encode())
        self.assertFalse((self.root / "local/backups").exists())

    def test_setup_check_without_virtualenv_does_not_create_anything(self):
        directory = self.base / "check-only"
        directory.mkdir()
        script = directory / "setup.ps1"
        shutil.copy2(ROOT / "setup.ps1", script)
        result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Check"],
                                capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([p.name for p in directory.iterdir()], ["setup.ps1"])


if __name__ == "__main__":
    unittest.main()
