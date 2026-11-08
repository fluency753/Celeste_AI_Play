"""Validate and upload component assets to an EXISTING Release; never move tags."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import component_package as packages


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def verify(root: Path, directory: Path, tag: str) -> list[Path]:
    packages.check_tag(tag, packages.project_version(root))
    files = [directory / packages.asset_name(tag), directory / "components.json", directory / "SHA256SUMS"]
    with tempfile.TemporaryDirectory() as work:
        bundle = packages.unpack(files[0], Path(work) / "expanded", root)
        data = packages.validate(bundle, root)
    if data != json.loads(files[1].read_text(encoding="utf-8")):
        raise packages.PackageError("外部清单与包内清单不一致")
    expected = f"{packages.sha256(files[0])}  {files[0].name}\n"
    if files[2].read_text(encoding="ascii") != expected:
        raise packages.PackageError("发布包校验清单不一致")
    settings = json.loads((root / "configs/component_build.json").read_text(encoding="utf-8"))
    if data["everest"]["reference_source"] != "official-pinned" or data["everest"]["asset_sha256"] != settings["everest_asset_sha256"]:
        raise packages.PackageError("发布要求使用固定校验值的官方标准 Everest 引用包")
    if data["toolchain"] != {"dotnet_sdk": settings["dotnet_sdk"], "ziglang": settings["ziglang"]}:
        raise packages.PackageError("发布工具链与固定配置不一致")
    if data.get("source_dirty") is not False:
        raise packages.PackageError("不能发布含未提交源码的候选构建")
    if data["tag"] != tag or data["source_commit"] != git(root, "rev-parse", tag + "^{commit}"):
        raise packages.PackageError("构建提交与发布标签不一致")
    if git(root, "rev-parse", "HEAD") != data["source_commit"] or git(root, "status", "--porcelain", "--untracked-files=no"):
        raise packages.PackageError("必须在发布标签的干净检出上上传")
    try:
        git(root, "merge-base", "--is-ancestor", data["source_commit"], "origin/main")
    except subprocess.CalledProcessError as exc:
        raise packages.PackageError("发布标签必须属于 origin/main；请先通过 SSH 同步主线") from exc
    return files


def api(url: str, token: str, body: bytes | None = None, content_type: str = "application/json"):
    request = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + token, "User-Agent": "Celeste-AI-Play-release",
        "Accept": "application/vnd.github+json", "Content-Type": content_type,
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def upload(files: list[Path], tag: str, repository: str, token: str, revision: str) -> None:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise packages.PackageError("GitHub 仓库名无效")
    base = f"https://api.github.com/repos/{repository}"
    release = api(base + "/releases/tags/" + urllib.parse.quote(tag, safe=""), token)
    actual = api(base + "/commits/" + urllib.parse.quote(tag, safe=""), token)
    if release["tag_name"] != tag or actual["sha"] != revision:
        raise packages.PackageError("GitHub Release 的标签或提交不一致")
    if bool(release["prerelease"]) != ("-" in tag):
        raise packages.PackageError("正式版／公测版标记与标签不一致")
    # Re-running after partial upload is safe; existing different bytes are immutable.
    existing = {asset["name"]: asset for asset in release.get("assets", [])}
    for path in files:
        digest = "sha256:" + packages.sha256(path)
        if path.name in existing:
            asset = existing[path.name]
            if asset.get("digest") != digest or asset.get("size") != path.stat().st_size:
                raise packages.PackageError(f"Release 已有不同内容的 {path.name}，拒绝覆盖；请发布修复版本")
            print(f"已有相同文件，跳过：{path.name}")
            continue
        url = f"https://uploads.github.com/repos/{repository}/releases/{release['id']}/assets?name=" + urllib.parse.quote(path.name)
        content_type = "application/zip" if path.suffix == ".zip" else "application/octet-stream"
        result = api(url, token, path.read_bytes(), content_type)
        if result.get("size") != path.stat().st_size or result.get("digest") != digest:
            raise packages.PackageError(f"上传回执校验失败：{path.name}；保留已有产物，不自动覆盖")
        print(f"已上传：{path.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--directory", type=Path, default=ROOT / "build/release")
    parser.add_argument("--repository", default=packages.REPOSITORY)
    parser.add_argument("--check", action="store_true", help="只核对产物、源码、工具链和标签，不联网或上传")
    args = parser.parse_args()
    files = verify(ROOT, args.directory, args.tag)
    if args.check:
        print("发布产物、源码、工具链与标签一致")
        return
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise packages.PackageError("上传需要 GH_TOKEN 或 GitHub Actions 的 GITHUB_TOKEN；不会读取其他登录文件")
    upload(files, args.tag, args.repository, token, git(ROOT, "rev-parse", "HEAD"))


if __name__ == "__main__":
    main()
