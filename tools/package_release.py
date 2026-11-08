"""Build a versioned component-only Release asset. Never install or publish it."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import component_package as packages


def run(*command: str, root: Path = ROOT) -> str:
    result = subprocess.run(command, cwd=root, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr, flush=True)
        result.check_returncode()
    return result.stdout.strip()


def references(root: Path, settings: dict) -> Path:
    base = root / "build" / "release-references"
    base.mkdir(parents=True, exist_ok=True)
    archive = base / (settings["everest_tag"] + ".zip")
    if not archive.exists():
        with tempfile.TemporaryDirectory(dir=base) as work:
            temporary = Path(work) / "refs.zip"
            packages.download("https://github.com/EverestAPI/Everest/releases/download/" +
                              settings["everest_tag"] + "/lib-stripped.zip", temporary)
            if packages.sha256(temporary) != settings["everest_asset_sha256"]:
                raise packages.PackageError("官方 Everest 引用包校验失败")
            temporary.replace(archive)
    if packages.sha256(archive) != settings["everest_asset_sha256"]:
        raise packages.PackageError("缓存 Everest 引用包校验失败")
    dest = base / settings["everest_tag"]
    # The checksum pins this official archive. It is build input, never shipped.
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            name = entry.filename
            if "\\" in name or ":" in name or ".." in Path(name).parts or name.startswith("/"):
                raise packages.PackageError("Everest 引用路径无效")
        z.extractall(dest)
    matches = list(dest.rglob("Celeste.dll"))
    if len(matches) != 1 or not (matches[0].parent / "MMHOOK_Celeste.dll").is_file():
        raise packages.PackageError("官方引用包缺少唯一的 Celeste.dll／MMHOOK_Celeste.dll")
    return matches[0].parent


def verify_headers(headers: Path, commit: str, root: Path = ROOT) -> None:
    actual = run("git", "-C", str(headers), "rev-parse", "HEAD", root=root).splitlines()[-1]
    dirty = run("git", "-C", str(headers), "status", "--porcelain", "--untracked-files=all", "--ignored", "--", "libobs", root=root)
    if actual != commit or dirty:
        raise packages.PackageError("OBS 头文件提交或工作树与固定构建配置不一致")


def build(root: Path, output: Path, tag: str, reference_dir: Path | None = None) -> Path:
    version = packages.project_version(root)
    packages.check_tag(tag, version)
    settings = json.loads((root / "configs/component_build.json").read_text(encoding="utf-8"))
    if any((root / folder / "local.props").exists() for folder in ("game-bridge", "game-runtime")):
        raise packages.PackageError("发布构建不读取本机 local.props，请在干净检出中构建")
    revision = run("git", "rev-parse", "HEAD", root=root).splitlines()[-1]
    before = packages.source_digest(root)
    refs = reference_dir.resolve() if reference_dir else references(root, settings)
    provenance = "explicit-local" if reference_dir else "official-pinned"
    run("dotnet", "build", str(root / "game-bridge"), "-t:Rebuild", "-c", "Release",
        f"-p:CelestePrefix={refs}", root=root)
    from tools.build_obs_plugin import fetch_headers
    headers = fetch_headers(settings["obs_version"])
    verify_headers(headers, settings["obs_commit"], root)
    run(sys.executable, str(root / "tools/build_obs_plugin.py"), "--obs-version", settings["obs_version"], "--no-install", root=root)
    # Build to private paths; packaging never replaces an in-use launcher.
    with tempfile.TemporaryDirectory(prefix="celeste-components-") as work:
        stage = Path(work) / "components"
        stage.mkdir()
        for target, name in (("console", "CelesteAIConsole.exe"), ("hud", "CelesteAIHud.exe")):
            path = stage / "launchers" / name
            path.parent.mkdir(exist_ok=True)
            run(sys.executable, str(root / "tools/build_launcher.py"), "--target", target, "--out", str(path), root=root)
        for module, folder in (("CelesteAIRuntime", "game-runtime"), ("CelesteAIBridge", "game-bridge")):
            dest = stage / "mods" / module
            (dest / "bin").mkdir(parents=True)
            shutil.copy2(root / folder / "everest.yaml", dest / "everest.yaml")
            for suffix in ("dll", "pdb"):
                shutil.copy2(root / folder / "bin" / f"{module}.{suffix}", dest / "bin" / f"{module}.{suffix}")
        (stage / "obs").mkdir()
        shutil.copy2(root / "build/obs-plugin/celeste-lockstep.dll", stage / "obs/celeste-lockstep.dll")
        sdk = run("dotnet", "--version", root=root).splitlines()[-1]
        manifest = {
            "schema": 1, "platform": packages.PLATFORM, "project_version": version, "tag": tag,
            "source_commit": revision, "source_digest": before,
            "source_dirty": bool(run("git", "status", "--porcelain", "--untracked-files=no", root=root)),
            "component_versions": packages.component_versions(root),
            "everest": {"tag": settings["everest_tag"], "reference_source": provenance,
                        "asset_sha256": settings["everest_asset_sha256"] if not reference_dir else None,
                        "celeste_sha256": packages.sha256(refs / "Celeste.dll")},
            "obs_versions": [settings["obs_version"]],
            "toolchain": {"dotnet_sdk": sdk, "ziglang": importlib.metadata.version("ziglang")},
            "files": {name: {"sha256": packages.sha256(stage / name), "size": (stage / name).stat().st_size}
                      for name in sorted(packages.FILES)},
        }
        if packages.source_digest(root) != before:
            raise packages.PackageError("构建期间源码改变，未生成发布包")
        verify_headers(headers, settings["obs_commit"], root)
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        packages.validate(stage, root)
        output.mkdir(parents=True, exist_ok=True)
        archive = output / packages.asset_name(tag)
        if archive.exists():
            raise packages.PackageError("输出包已存在；请使用新目录，不覆盖已生成的发布产物")
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
            for name in sorted(packages.FILES | {"manifest.json"}):
                z.write(stage / name, "components/" + name)
        (output / "SHA256SUMS").write_text(f"{packages.sha256(archive)}  {archive.name}\n", encoding="ascii")
        (output / "components.json").write_text((stage / "manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="默认 v<项目版本>；公测版显式指定 vX.Y.Z-beta.N")
    parser.add_argument("--output", type=Path, default=ROOT / "build/release")
    parser.add_argument("--reference-dir", type=Path, help="仅本机验证：明确指定标准 Everest 引用目录；此类产物不能自动发布")
    args = parser.parse_args()
    print(build(ROOT, args.output, args.tag or "v" + packages.project_version(ROOT), args.reference_dir))


if __name__ == "__main__":
    main()
