"""Release upload retries and provenance checks; no real GitHub writes."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from calibration.component_package_test import archive, bundle, fixture
from tools import component_package as packages, publish_release as publish, package_release


class ReleaseUploadTests(unittest.TestCase):
    def files(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        files = [root / name for name in (packages.asset_name("v1.22.0"), "components.json")]
        for file in files:
            file.write_bytes(file.name.encode())
        return files

    def test_retry_skips_identical_files_and_uploads_only_missing(self):
        files = self.files()
        def asset(path):
            return {"name": path.name, "size": path.stat().st_size, "digest": "sha256:" + packages.sha256(path)}
        release = {"id": 42, "tag_name": "v1.22.0", "prerelease": False, "assets": [asset(files[0])]}
        replies = [release, {"sha": "a" * 40}, asset(files[1])]
        with patch.object(publish, "api", side_effect=replies) as api:
            publish.upload(files, "v1.22.0", "test/repo", "token", "a" * 40)
        writes = [call for call in api.call_args_list if len(call.args) > 2]
        self.assertEqual(len(writes), 1)
        self.assertTrue(all(call.args[0].startswith("https://uploads.github.com/repos/test/repo/releases/42/assets?name=") for call in writes))

    def test_different_published_bytes_are_never_overwritten(self):
        files = self.files()
        release = {"id": 42, "tag_name": "v1.22.0", "prerelease": False,
                   "assets": [{"name": files[0].name, "size": 3, "digest": "sha256:" + "0" * 64}]}
        with patch.object(publish, "api", side_effect=[release, {"sha": "a" * 40}]) as api:
            with self.assertRaisesRegex(packages.PackageError, "拒绝覆盖"):
                publish.upload(files, "v1.22.0", "test/repo", "token", "a" * 40)
        self.assertEqual(api.call_count, 2)

    def test_remote_tag_mismatch_and_bad_receipt_stop_publication(self):
        files = self.files()
        release = {"id": 42, "tag_name": "v1.22.0", "prerelease": False, "assets": []}
        with patch.object(publish, "api", side_effect=[release, {"sha": "b" * 40}]):
            with self.assertRaisesRegex(packages.PackageError, "提交不一致"):
                publish.upload(files, "v1.22.0", "test/repo", "token", "a" * 40)
        with patch.object(publish, "api", side_effect=[release, {"sha": "a" * 40}, {"size": 0}]) as api:
            with self.assertRaisesRegex(packages.PackageError, "回执校验失败"):
                publish.upload(files, "v1.22.0", "test/repo", "token", "a" * 40)
        self.assertEqual(api.call_count, 3)


@unittest.skipUnless(shutil.which("git"), "git unavailable")
class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        fixture(self.root)
        self.settings = {"everest_asset_sha256": "a" * 64, "dotnet_sdk": "10.0.400", "ziglang": "0.16.0"}
        (self.root / "configs").mkdir()
        (self.root / "configs/component_build.json").write_text(json.dumps(self.settings))
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()
        self.git = git
        git("init", "-q", "-b", "main")
        git("config", "user.name", "fixture")
        git("config", "user.email", "fixture@example.com")
        git("add", ".")
        git("-c", "core.hooksPath=", "commit", "-qm", "fixture")
        git("tag", "v1.22.0")
        git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
        self.dest = self.root / "outputs"
        self.dest.mkdir()
        b = self.dest / "bundle"
        data = bundle(self.root, b)
        data.update(source_commit=git("rev-parse", "HEAD"), source_dirty=False, toolchain={"dotnet_sdk": "10.0.400", "ziglang": "0.16.0"},
                    everest={"reference_source": "official-pinned", "asset_sha256": "a" * 64})
        self.data = data
        self.bundle = b
        self.write()

    def write(self):
        (self.bundle / "manifest.json").write_text(json.dumps(self.data))
        archive(self.bundle, self.dest / packages.asset_name("v1.22.0"))
        (self.dest / "components.json").write_text(json.dumps(self.data))

    def test_real_tag_and_source_identity_without_network(self):
        self.assertEqual([p.name for p in publish.verify(self.root, self.dest, "v1.22.0")],
                         [packages.asset_name("v1.22.0"), "components.json"])

    def test_build_generates_only_archive_and_manifest(self):
        (self.root / "Celeste.dll").write_bytes(b"standard reference fixture")
        settings = {**self.settings, "everest_tag": "standard-fixture", "obs_version": "33.0.0", "obs_commit": "c" * 40}
        (self.root / "configs/component_build.json").write_text(json.dumps(settings))
        headers = self.root / "build/headers"
        headers.mkdir(parents=True)
        for folder, module in (("game-bridge", "CelesteAIBridge"), ("game-runtime", "CelesteAIRuntime")):
            target = self.root / folder / "bin"
            target.mkdir()
            for suffix in ("dll", "pdb"):
                (target / f"{module}.{suffix}").write_bytes(b"native build output")
        plugin = self.root / "build/obs-plugin/celeste-lockstep.dll"
        plugin.parent.mkdir()
        plugin.write_bytes(b"OBS build output")
        def run(*command, root):
            if command[:2] == ("dotnet", "--version"):
                return settings["dotnet_sdk"]
            if command[0] == "git":
                if "-C" in command:
                    return settings["obs_commit"] if "rev-parse" in command else ""
                return self.git(*command[1:])
            if "--out" in command:
                Path(command[command.index("--out") + 1]).write_bytes(b"launcher build output")
            return ""
        output = self.root / "build/release"
        with patch.object(package_release, "run", side_effect=run), \
             patch("tools.build_obs_plugin.fetch_headers", return_value=headers):
            zipped = package_release.build(self.root, output, "v1.22.0", self.root)
        self.assertEqual({p.name for p in output.iterdir()}, {packages.asset_name("v1.22.0"), "components.json"})
        packages.unpack(zipped, self.root / "unpacked", self.root)

    def test_unpinned_references_and_mismatched_source_commit_refuse_upload(self):
        self.data["everest"]["reference_source"] = "explicit-local"
        self.write()
        with self.assertRaisesRegex(packages.PackageError, "官方标准"):
            publish.verify(self.root, self.dest, "v1.22.0")
        self.data["everest"]["reference_source"] = "official-pinned"
        self.data["source_commit"] = "b" * 40
        self.write()
        with self.assertRaisesRegex(packages.PackageError, "构建提交"):
            publish.verify(self.root, self.dest, "v1.22.0")

    def test_side_branch_tag_cannot_be_published(self):
        self.git("checkout", "--orphan", "side")
        self.git("-c", "core.hooksPath=", "commit", "-qm", "independent side branch")
        self.git("tag", "-d", "v1.22.0")
        self.git("tag", "v1.22.0")
        self.data["source_commit"] = self.git("rev-parse", "HEAD")
        self.write()
        with self.assertRaisesRegex(packages.PackageError, "origin/main"):
            publish.verify(self.root, self.dest, "v1.22.0")

    def test_pinned_header_commit_also_requires_a_clean_header_tree(self):
        headers = self.root / "headers"
        (headers / "libobs").mkdir(parents=True)
        path = headers / "libobs/obs.h"
        path.write_bytes(b"official header")
        (headers / ".gitignore").write_text("libobs/generated.h\n")
        def git(*args):
            return subprocess.run(["git", *args], cwd=headers, check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-q")
        git("config", "user.name", "fixture")
        git("config", "user.email", "fixture@example.com")
        git("add", ".")
        git("-c", "core.hooksPath=", "commit", "-qm", "headers")
        commit = git("rev-parse", "HEAD")
        package_release.verify_headers(headers, commit, self.root)
        path.write_bytes(b"modified header")
        with self.assertRaises(packages.PackageError):
            package_release.verify_headers(headers, commit, self.root)
        path.write_bytes(b"official header")
        (headers / "libobs/generated.h").write_text("ignored injected header")
        with self.assertRaises(packages.PackageError):
            package_release.verify_headers(headers, commit, self.root)


if __name__ == "__main__":
    unittest.main()
