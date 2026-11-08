"""Package identity, extraction, caching and installation with isolated real files."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from tools import component_package as packages


def fixture(root):
    for name, text in {
        "overlay/console_api.py": 'CONSOLE_VERSION = "1.22.0"\n',
        "game-runtime/everest.yaml": '- Name: CelesteAIRuntime\n  Version: 1.0.6\n',
        "game-bridge/everest.yaml": '- Name: CelesteAIBridge\n  Version: 1.11.1\n',
        "requirements.txt": "", "setup.ps1": "# setup\n", "tools/input.py": "# source\n",
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def bundle(root, dest):
    for name in packages.FILES:
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("everest.yaml"):
            folder = "game-runtime" if "CelesteAIRuntime" in name else "game-bridge"
            path.write_bytes((root / folder / "everest.yaml").read_bytes())
        else:
            path.write_bytes(b"test component " + name.encode())
    data = {"schema": 1, "platform": packages.PLATFORM, "project_version": "1.22.0", "tag": "v1.22.0",
            "source_digest": packages.source_digest(root), "component_versions": packages.component_versions(root),
            "obs_versions": ["33.0.0"],
            "files": {name: {"sha256": packages.sha256(dest / name), "size": (dest / name).stat().st_size}
                      for name in packages.FILES}}
    (dest / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def archive(source, target, extra=None):
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for path in source.rglob("*"):
            if path.is_file():
                z.write(path, "components/" + path.relative_to(source).as_posix())
        if extra:
            z.writestr(*extra)


def release_metadata(zipped, **changes):
    asset = {"name": packages.asset_name("v1.22.0"), "size": zipped.stat().st_size,
             "digest": "sha256:" + packages.sha256(zipped)}
    asset.update(changes)
    return {"tag_name": "v1.22.0", "assets": [asset]}


class ComponentPackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "source"
        fixture(self.root)
        self.bundle = self.base / "bundle"
        self.data = bundle(self.root, self.bundle)

    def test_source_archive_without_git_and_normalized_line_endings(self):
        self.assertFalse((self.root / ".git").exists())
        packages.validate(self.bundle, self.root)
        path = self.root / "tools/input.py"
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        packages.validate(self.bundle, self.root)
        local = self.root / "configs/local.private.json"
        local.parent.mkdir(exist_ok=True)
        local.write_text('"not a build input"')
        packages.validate(self.bundle, self.root)
        path.write_text("# changed production input\n")
        with self.assertRaises(packages.Unavailable):
            packages.validate(self.bundle, self.root)

    def test_wrong_version_platform_and_component_versions(self):
        for key, value, error in [("project_version", "1.21.0", packages.PackageError),
                                  ("platform", "linux-x64", packages.PackageError),
                                  ("component_versions", {}, packages.PackageError)]:
            with self.subTest(key=key):
                data = {**self.data, key: value}
                (self.bundle / "manifest.json").write_text(json.dumps(data))
                with self.assertRaises(error):
                    packages.validate(self.bundle, self.root)

    def test_corrupt_missing_extra_and_duplicate_manifest(self):
        target = self.bundle / "obs/celeste-lockstep.dll"
        old = target.read_bytes()
        target.write_bytes(b"corrupt")
        with self.assertRaisesRegex(packages.PackageError, "校验失败"):
            packages.validate(self.bundle, self.root)
        target.write_bytes(old)
        extra = self.bundle / "credentials.json"
        extra.write_text("not shipped")
        with self.assertRaisesRegex(packages.PackageError, "多余"):
            packages.validate(self.bundle, self.root)
        extra.unlink()
        (self.bundle / "manifest.json").write_text('{"schema":1,"schema":1}')
        with self.assertRaisesRegex(packages.PackageError, "重复"):
            packages.validate(self.bundle, self.root)

    def test_zip_extraction_rejects_unknown_traversal_and_duplicate_paths(self):
        for i, name in enumerate(("../escape", "components/../escape", "components\\manifest.json",
                                  "components/OBS/celeste-lockstep.dll", "components/manifest.json")):
            with self.subTest(name=name):
                path = self.base / f"bad-{i}.zip"
                archive(self.bundle, path, (name, "extra"))
                with self.assertRaises(packages.PackageError):
                    packages.unpack(path, self.base / f"extract-{i}", self.root)
        self.assertFalse((self.base / "escape").exists())

    def test_real_zip_download_is_validated_before_cache_publication(self):
        zipped = self.base / "package.zip"
        archive(self.bundle, zipped)
        responses = [io.BytesIO(json.dumps(release_metadata(zipped)).encode()), io.BytesIO(zipped.read_bytes())]
        with patch.object(packages.urllib.request, "urlopen", side_effect=responses) as network:
            path, _ = packages.find_bundle(self.root, network=True)
            packages.validate(path, self.root)
            self.assertEqual(path, self.root / "local/components/v1.22.0/components")
            packages.find_bundle(self.root, network=True)
        self.assertEqual(network.call_count, 2)
        self.assertTrue(network.call_args_list[0].args[0].full_url.endswith("/releases/tags/v1.22.0"))
        self.assertTrue(network.call_args_list[1].args[0].full_url.endswith(
            "/releases/download/v1.22.0/" + packages.asset_name("v1.22.0")))

    def test_check_mode_never_creates_cache_or_downloads(self):
        with patch.object(packages.urllib.request, "urlopen", side_effect=AssertionError("network in check mode")):
            self.assertIsNone(packages.find_bundle(self.root)[0])
        self.assertFalse((self.root / "local").exists())

    def test_validation_cli_uses_utf8_even_on_an_english_windows_pipe(self):
        tool = Path(packages.__file__)
        result = subprocess.run([sys.executable, str(tool), "--root", str(self.root), "--validate", str(self.bundle)],
                                env=dict(os.environ, PYTHONIOENCODING="cp1252"), capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertIn("校验通过", result.stdout.decode("utf-8"))

    def test_build_log_forwarding_uses_utf8_under_cp1252(self):
        child = "import sys;sys.stdout.buffer.write('编译组件成功'.encode('utf-8'))"
        code = "from tools.component_package import configure_output;from tools.package_release import run;import sys;configure_output();run(sys.executable,'-c'," + repr(child) + ")"
        result = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, PYTHONIOENCODING="cp1252"),
                                cwd=Path(packages.__file__).resolve().parents[1], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(result.stdout.decode("utf-8"), "编译组件成功")

    def test_explicit_release_tag_must_match_bundled_and_downloaded_manifest(self):
        path = self.root / "components"
        data = bundle(self.root, path)
        data["tag"] = "v1.22.0-beta.1"
        (path / "manifest.json").write_text(json.dumps(data))
        self.assertEqual(packages.find_bundle(self.root)[0], path)  # Explicitly extracted prerelease.
        self.assertIsNone(packages.find_bundle(self.root, tag="v1.22.0")[0])
        zipped = self.base / "wrong-tag.zip"
        archive(path, zipped)
        with patch.object(packages.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(release_metadata(zipped)).encode()), io.BytesIO(zipped.read_bytes())]):
            self.assertIsNone(packages.find_bundle(self.root, network=True, tag="v1.22.0")[0])
        self.assertFalse((self.root / "local/components/v1.22.0").exists())

    def test_release_missing_and_offline_are_not_corruption(self):
        with patch.object(packages.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("x", 404, "missing", {}, None)):
            self.assertIsNone(packages.find_bundle(self.root, network=True)[0])
        with patch.object(packages.urllib.request, "urlopen", side_effect=TimeoutError("offline")):
            with self.assertRaises(packages.Unavailable):
                packages.find_bundle(self.root, network=True)
        with patch.object(packages.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("x", 403, "rate limit exceeded", {}, None)):
            with self.assertRaisesRegex(packages.Unavailable, "HTTP 403"):
                packages.find_bundle(self.root, network=True)

    def test_corrupt_cache_is_not_silently_replaced(self):
        cache = self.root / "local/components/v1.22.0/components"
        bundle(self.root, cache)
        target = cache / "obs/celeste-lockstep.dll"
        target.write_bytes(b"bad cached bytes")
        with patch.object(packages.urllib.request, "urlopen", side_effect=AssertionError("must not download around corruption")):
            with self.assertRaisesRegex(packages.PackageError, "校验失败"):
                packages.find_bundle(self.root, network=True)
        self.assertEqual(target.read_bytes(), b"bad cached bytes")

    def test_invalid_download_does_not_discard_previous_cache(self):
        cache = self.root / "local/components/v1.22.0/components"
        data = bundle(self.root, cache)
        data["source_digest"] = "0" * 64
        (cache / "manifest.json").write_text(json.dumps(data))
        old = (cache / "obs/celeste-lockstep.dll").read_bytes()
        zipped = self.base / "not-zip"
        zipped.write_bytes(b"not zip")
        with patch.object(packages.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(release_metadata(zipped)).encode()), io.BytesIO(zipped.read_bytes())]):
            with self.assertRaises(packages.PackageError):
                packages.find_bundle(self.root, network=True)
        self.assertEqual((cache / "obs/celeste-lockstep.dll").read_bytes(), old)

    def test_github_digest_and_size_mismatch_keep_previous_cache(self):
        cache = self.root / "local/components/v1.22.0/components"
        data = bundle(self.root, cache)
        data["source_digest"] = "0" * 64
        manifest = cache / "manifest.json"
        manifest.write_text(json.dumps(data))
        old = manifest.read_bytes()
        zipped = self.base / "package.zip"
        archive(self.bundle, zipped)
        for change in ({"digest": "sha256:" + "0" * 64}, {"size": zipped.stat().st_size + 1}):
            with self.subTest(change=change), patch.object(packages.urllib.request, "urlopen", side_effect=[
                    io.BytesIO(json.dumps(release_metadata(zipped, **change)).encode()), io.BytesIO(zipped.read_bytes())]):
                with self.assertRaisesRegex(packages.PackageError, "GitHub Release.*不一致"):
                    packages.find_bundle(self.root, network=True)
            self.assertEqual(manifest.read_bytes(), old)
            self.assertFalse(list(cache.parent.parent.glob("*.previous-*")))

    def test_release_metadata_requires_valid_archive_identity(self):
        zipped = self.base / "package.zip"
        archive(self.bundle, zipped)
        for changes in ({"digest": None}, {"digest": "sha1:" + "a" * 40}, {"digest": "sha256:" + "x" * 64},
                        {"size": 0}, {"size": True}, {"size": packages.MAX_ARCHIVE + 1}):
            with self.subTest(changes=changes), patch.object(packages.urllib.request, "urlopen", return_value=
                    io.BytesIO(json.dumps(release_metadata(zipped, **changes)).encode())) as network:
                with self.assertRaisesRegex(packages.PackageError, "缺少有效"):
                    packages.find_bundle(self.root, network=True)
                self.assertEqual(network.call_count, 1)
        self.assertFalse((self.root / "local/components/v1.22.0").exists())

    def test_missing_attachment_can_fall_back_and_legacy_checksum_is_ignored(self):
        release = {"tag_name": "v1.22.0", "assets": [{"name": "SHA256SUMS"}]}
        with patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(release).encode())) as network:
            self.assertIsNone(packages.find_bundle(self.root, network=True)[0])
            self.assertEqual(network.call_count, 1)
        zipped = self.base / "package.zip"
        archive(self.bundle, zipped)
        release["assets"].extend(release_metadata(zipped)["assets"])
        with patch.object(packages.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(release).encode()), io.BytesIO(zipped.read_bytes())]):
            packages.validate(packages.find_bundle(self.root, network=True)[0], self.root)

    def test_wrong_tag_duplicate_assets_and_invalid_metadata_are_rejected(self):
        zipped = self.base / "package.zip"
        archive(self.bundle, zipped)
        release = release_metadata(zipped)
        payloads = [b"not JSON", b" " * (1024 * 1024 + 1),
                    json.dumps({**release, "tag_name": "v1.22.0-beta.1"}).encode(),
                    json.dumps({**release, "assets": release["assets"] * 2}).encode(),
                    json.dumps({**release, "assets": [None]}).encode()]
        for raw in payloads:
            with self.subTest(size=len(raw)), patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(raw)):
                with self.assertRaises(packages.PackageError):
                    packages.find_bundle(self.root, network=True)

    def test_stale_and_corrupt_cache_is_still_a_hard_error(self):
        cache = self.root / "local/components/v1.22.0/components"
        data = bundle(self.root, cache)
        data["source_digest"] = "0" * 64
        (cache / "manifest.json").write_text(json.dumps(data))
        (cache / "obs/celeste-lockstep.dll").write_bytes(b"corrupt")
        with patch.object(packages.urllib.request, "urlopen", side_effect=AssertionError("must not bypass corrupt cache")):
            with self.assertRaisesRegex(packages.PackageError, "校验失败"):
                packages.find_bundle(self.root, network=True)

    def test_obs_install_preserves_settings_and_backs_up_old_dll(self):
        obs = self.base / "obs"
        target = obs / "obs-plugins/64bit/celeste-lockstep.dll"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old plugin")
        settings = obs / "settings.ini"
        settings.write_bytes(b"personal settings")
        with patch("tools.build_obs_plugin.obs_running", return_value=False):
            backup = packages.install_obs(self.bundle / "obs/celeste-lockstep.dll", obs)
        self.assertEqual(backup.read_bytes(), b"old plugin")
        self.assertEqual(target.read_bytes(), (self.bundle / "obs/celeste-lockstep.dll").read_bytes())
        self.assertEqual(settings.read_bytes(), b"personal settings")

    def test_obs_running_or_replace_failure_keeps_original_dll(self):
        obs = self.base / "obs"
        target = obs / "obs-plugins/64bit/celeste-lockstep.dll"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"old")
        with patch("tools.build_obs_plugin.obs_running", return_value=True):
            with self.assertRaises(packages.PackageError):
                packages.install_obs(self.bundle / "obs/celeste-lockstep.dll", obs)
        with patch("tools.build_obs_plugin.obs_running", return_value=False), \
             patch.object(packages.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                packages.install_obs(self.bundle / "obs/celeste-lockstep.dll", obs)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertTrue(any(p.read_bytes() == b"old" for p in target.parent.glob("*.backup-*")))
        self.assertFalse(list(target.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
