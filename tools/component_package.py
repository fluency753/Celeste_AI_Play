"""Versioned, source-matched Windows component bundles; no game or user data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

REPOSITORY = "fluency753/Celeste_AI_Play"
PLATFORM = "windows-x64"
MAX_ARCHIVE = 128 * 1024 * 1024
FILES = frozenset({
    *(f"mods/{mod}/{name}" for mod in ("CelesteAIRuntime", "CelesteAIBridge")
      for name in ("everest.yaml", f"bin/{mod}.dll", f"bin/{mod}.pdb")),
    "obs/celeste-lockstep.dll", "launchers/CelesteAIConsole.exe", "launchers/CelesteAIHud.exe",
})


class PackageError(ValueError):
    """An existing package failed validation; never silently install around it."""


class Unavailable(PackageError):
    """No package for these sources; source compilation remains available."""


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_version(root: Path) -> str:
    text = (root / "overlay/console_api.py").read_text(encoding="utf-8-sig")
    match = re.search(r'^CONSOLE_VERSION\s*=\s*"(\d+\.\d+\.\d+)"', text, re.M)
    if not match:
        raise PackageError("读不到项目版本")
    return match[1]


def component_versions(root: Path) -> dict[str, str]:
    values = {}
    for folder, name in (("game-runtime", "runtime"), ("game-bridge", "bridge")):
        match = re.search(r"^\s+Version:\s*(\d+\.\d+\.\d+)\s*$",
                          (root / folder / "everest.yaml").read_text(encoding="utf-8-sig"), re.M)
        if not match:
            raise PackageError(f"读不到 {name} 版本")
        values[name] = match[1]
    return values


def source_digest(root: Path) -> str:
    # Include all production/build inputs, including Runtime's linked Bridge files.
    # Never read local settings, build artifacts, notes, credentials or game files.
    paths = [root / "requirements.txt", root / "setup.ps1"]
    suffixes = {".py", ".cs", ".csproj", ".c", ".h", ".def", ".rc", ".manifest", ".yaml", ".json", ".md", ".html", ".js", ".css"}
    for directory in ("orchestrator", "overlay", "tools", "game-bridge", "game-runtime", "obs-plugin", "configs"):
        for path in (root / directory).rglob("*"):
            relative = path.relative_to(root)
            if any(p in {"bin", "obj", "__pycache__"} for p in relative.parts):
                continue
            if path.name == "local.props" or directory == "configs" and relative.parts[1].startswith("local"):
                continue
            if path.is_file() and path.suffix in suffixes:
                paths.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_symlink():
            raise PackageError(f"构建输入不能是链接：{path}")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        # Git archives use LF; Windows checkouts may use CRLF.
        digest.update(hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def check_tag(tag: str, version: str) -> str:
    if not isinstance(tag, str) or not re.fullmatch(r"v" + re.escape(version) + r"(?:-(?:beta|rc)\.\d+)?", tag):
        raise PackageError(f"Release 标签必须与项目版本 {version} 一致")
    return tag


def asset_name(tag: str) -> str:
    return f"Celeste_AI_Play-{tag}-components-windows-x64.zip"


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PackageError("组件清单含重复字段")
        result[key] = value
    return result


def validate(bundle: Path, root: Path, *, expected_tag: str | None = None) -> dict:
    bundle = Path(bundle)
    if bundle.is_symlink() or bundle.exists() and getattr(bundle.lstat(), "st_file_attributes", 0) & 0x400:
        raise PackageError("组件目录不能是链接或重解析点")
    manifest = bundle / "manifest.json"
    if not manifest.is_file():
        raise PackageError("组件包缺少 manifest.json")
    if manifest.stat().st_size > 128 * 1024:
        raise PackageError("组件清单过大")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PackageError("组件清单无效") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("platform") != PLATFORM:
        raise PackageError("不支持的组件包格式或平台")
    if not isinstance(data.get("project_version"), str) or not re.fullmatch(r"\d+\.\d+\.\d+", data["project_version"]):
        raise PackageError("项目版本字段无效")
    versions = data.get("component_versions")
    if not isinstance(versions, dict) or set(versions) != {"runtime", "bridge"} or any(
            not isinstance(v, str) or not re.fullmatch(r"\d+\.\d+\.\d+", v) for v in versions.values()):
        raise PackageError("组件版本字段无效")
    if not isinstance(data.get("source_digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", data["source_digest"]):
        raise PackageError("源码指纹字段无效")
    check_tag(data.get("tag"), data["project_version"])
    records = data.get("files")
    if not isinstance(records, dict) or set(records) != FILES:
        raise PackageError("组件清单文件范围无效")
    observed = set()
    for path in bundle.rglob("*"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise PackageError("组件包不能包含链接或重解析点")
        if path.is_file():
            observed.add(path.relative_to(bundle).as_posix())
    if observed != FILES | {"manifest.json"}:
        raise PackageError("组件包包含缺失或多余文件")
    for name, record in records.items():
        path = bundle / name
        if not isinstance(record, dict) or type(record.get("size")) is not int or not 0 < record["size"] <= 32 * 1024 * 1024:
            raise PackageError(f"组件文件大小无效：{name}")
        if path.stat().st_size != record["size"] or sha256(path) != record.get("sha256"):
            raise PackageError(f"组件校验失败：{name}")
    obs = data.get("obs_versions")
    if not isinstance(obs, list) or not obs or any(not isinstance(v, str) or not re.fullmatch(r"\d+\.\d+\.\d+", v) for v in obs):
        raise PackageError("OBS 兼容版本清单无效")
    # YAML metadata must agree with the versions whose binary hashes were checked.
    for module, key in (("CelesteAIRuntime", "runtime"), ("CelesteAIBridge", "bridge")):
        text = (bundle / "mods" / module / "everest.yaml").read_text(encoding="utf-8-sig")
        if not re.search(r"^  Version:\s*" + re.escape(data["component_versions"][key]) + r"\s*$", text, re.M):
            raise PackageError(f"Mod 元数据版本不一致：{module}")
    # Corrupt packages are errors even when they are stale. Only a fully valid
    # but incompatible package may lead to download/source fallback.
    if data["project_version"] != project_version(root) or data["component_versions"] != component_versions(root):
        raise Unavailable("组件包版本与当前源码不一致")
    if data["source_digest"] != source_digest(root):
        raise Unavailable("组件包构建输入与当前源码不一致")
    if expected_tag is not None and data["tag"] != expected_tag:
        raise Unavailable("组件包标签与指定 Release 不一致")
    return data


def unpack(archive: Path, destination: Path, root: Path) -> Path:
    """Extract only the fixed component file set into a NEW private directory."""
    if destination.exists():
        raise PackageError("解压目标必须是新目录")
    allowed = {"components/" + name for name in FILES | {"manifest.json"}}
    try:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist()
            names = [i.filename for i in entries]
            if set(names) != allowed or len(names) != len(allowed):
                raise PackageError("ZIP 文件范围、重复条目或路径无效")
            if sum(i.file_size for i in entries) > MAX_ARCHIVE:
                raise PackageError("ZIP 解压大小超限")
            for entry in entries:
                if "\\" in entry.filename or ":" in entry.filename or ".." in PurePosixPath(entry.filename).parts:
                    raise PackageError("ZIP 路径无效")
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 1 or entry.file_size > 32 * 1024 * 1024:
                    raise PackageError("ZIP 链接、加密或文件大小无效")
            destination.mkdir(parents=True)
            for entry in entries:
                path = destination / entry.filename
                path.parent.mkdir(parents=True, exist_ok=True)
                with z.open(entry) as source, path.open("xb") as target:
                    shutil.copyfileobj(source, target)
        validate(destination / "components", root)
        return destination / "components"
    except zipfile.BadZipFile as exc:
        raise PackageError("组件 ZIP 损坏") from exc


def download(url: str, target: Path, limit: int = MAX_ARCHIVE) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Celeste-AI-Play-components"})
    with urllib.request.urlopen(request, timeout=30) as response, target.open("xb") as stream:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise PackageError("组件下载大小超限")
            stream.write(chunk)


def find_bundle(root: Path, *, network: bool = False, tag: str | None = None) -> tuple[Path | None, str]:
    """Check mode uses network=False: no downloads, directory creation or writes."""
    requested_tag = tag
    version = project_version(root)
    tag = check_tag(tag or f"v{version}", version)
    cache = root / "local" / "components" / tag
    reasons = []
    for path in (root / "components", cache / "components"):
        if path.exists():
            try:
                validate(path, root, expected_tag=requested_tag if path == root / "components" else tag)
                return path, "已校验对应版本组件包"
            except Unavailable as exc:
                reasons.append(str(exc))
    if not network:
        return None, "; ".join(reasons) or "本机没有对应版本组件包"
    parent = root / "local" / "components"
    parent.mkdir(parents=True, exist_ok=True)
    # Keep old caches intact, including when a download or validation fails.
    with tempfile.TemporaryDirectory(prefix=".download-", dir=parent) as work:
        stage = Path(work)
        archive = stage / "package.zip"
        try:
            download(f"https://github.com/{REPOSITORY}/releases/download/{tag}/{asset_name(tag)}", archive)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                return None, f"{tag} 未发布预编译包，可使用源码编译"
            raise Unavailable(f"组件下载失败（HTTP {exc.code}），可重试或使用源码编译") from exc
        except (OSError, TimeoutError) as exc:
            raise Unavailable("暂时无法下载组件，可重试或使用源码编译") from exc
        try:
            bundle = unpack(archive, stage / "expanded", root)
            validate(bundle, root, expected_tag=tag)
        except Unavailable as exc:
            return None, str(exc)
        # A cache with this exact tag but other sources is retained under a unique backup.
        aside = None
        if cache.exists():
            aside = parent / (tag + ".previous-" + os.urandom(6).hex())
            cache.rename(aside)
        try:
            os.replace(bundle.parent, cache)
        except OSError:
            if aside is not None and not cache.exists():
                aside.rename(cache)
            raise
        return cache / "components", "已下载并校验对应版本组件包"


def install_obs(source: Path, obs: Path) -> Path | None:
    """Do not stop OBS or change its settings; preserve the old DLL on any failure."""
    from tools.build_obs_plugin import obs_running
    if obs_running():
        raise PackageError("OBS 正在运行，未安装插件；请自行关闭 OBS 后重试")
    target = obs / "obs-plugins" / "64bit" / "celeste-lockstep.dll"
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if target.exists():
        backup = target.with_name(target.name + ".backup-" + os.urandom(6).hex())
        shutil.copy2(target, backup)
    with tempfile.NamedTemporaryFile(prefix=".celeste-lockstep-", suffix=".tmp", dir=target.parent, delete=False) as f:
        temporary = Path(f.name)
    try:
        shutil.copyfile(source, temporary)
        if obs_running():
            raise PackageError("OBS 在安装前启动，未替换插件")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--validate", type=Path, required=True)
    args = parser.parse_args()
    validate(args.validate, args.root)
    print("组件包版本、源码指纹与全部文件校验通过")


if __name__ == "__main__":
    configure_output()
    main()
