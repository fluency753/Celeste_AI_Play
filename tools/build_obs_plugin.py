"""构建并安装 OBS 锁步录制插件（obs-plugin/celeste-lockstep.c）。

不需要 Visual Studio：用 pip 安装的 zig（自带 clang 与链接器）交叉编译出 Windows 64 位 DLL；
libobs 的头文件从 obs-studio 仓库按与本机 OBS 相同的版本取（只取 libobs 目录）。

用法（仓库根目录；安装前先关闭 OBS）：
    python tools/build_obs_plugin.py                                                 # 自动找 OBS，构建并安装
    python tools/build_obs_plugin.py --obs-dir "<OBS 安装目录>"                        # 指定 OBS 目录
    python tools/build_obs_plugin.py --no-install                                    # 只构建
    python tools/build_obs_plugin.py --obs-version 32.2.2 --no-install               # 不读本机 OBS，指定版本

输出：build/obs-plugin/celeste-lockstep.dll；安装到 <OBS>/obs-plugins/64bit/。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "obs-plugin"
BUILD = REPO_ROOT / "build" / "obs-plugin"


def obs_version(obs_dir: Path) -> str:
    exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    if not exe.exists():
        raise SystemExit(f"找不到 {exe}")
    if sys.platform == "win32":
        out = subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-Item '{exe}').VersionInfo.ProductVersion"],
                             capture_output=True, text=True).stdout.strip()
        if out:
            return out
    raise SystemExit("读不到 OBS 版本，请用 --obs-version 指定")


def fetch_headers(version: str) -> Path:
    dest = BUILD / f"obs-studio-{version}"
    if (dest / "libobs" / "obs-module.h").exists():
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", *args], cwd=dest, check=True)
    git("init", "-q")
    git("remote", "add", "origin", "https://github.com/obsproject/obs-studio.git")
    git("config", "core.sparseCheckout", "true")
    (dest / ".git" / "info" / "sparse-checkout").write_text("libobs/\n", encoding="utf-8")
    git("fetch", "-q", "--depth", "1", "origin", "tag", version)
    git("checkout", "-q", f"tags/{version}")
    return dest


def zig(*args: str) -> None:
    try:
        import ziglang  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "ziglang"], check=True)
    subprocess.run([sys.executable, "-m", "ziglang", *args], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--obs-dir", help="OBS 安装目录（含 bin\\64bit\\obs64.exe）；不写时依次看 configs/local.recording.json 的 "
                    "obs_dir、Program Files 与各个 Steam 库")
    ap.add_argument("--obs-version", help="不读本机 OBS，直接指定 libobs 版本（如 32.2.2）")
    ap.add_argument("--no-install", action="store_true")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    obs_dir = None
    if not (args.obs_version and args.no_install):
        sys.path.insert(0, str(REPO_ROOT))
        from tools.obs_setup import find_obs_dir
        obs_dir = find_obs_dir(args.obs_dir)
    version = args.obs_version or obs_version(obs_dir)
    print(f"OBS 版本 {version}", flush=True)
    headers = fetch_headers(version)
    BUILD.mkdir(parents=True, exist_ok=True)
    lib = BUILD / "libobs.a"
    dll = BUILD / "celeste-lockstep.dll"
    zig("dlltool", "-d", str(SRC / "obs.def"), "-l", str(lib), "-m", "i386:x86-64")
    zig("cc", "-target", "x86_64-windows-gnu", "-O2", "-shared", "-Wall", "-Wno-unused-parameter",
        "-I", str(headers / "libobs"), "-I", str(SRC / "config"), str(SRC / "celeste-lockstep.c"),
        "-L", str(BUILD), "-lobs", "-lws2_32", "-o", str(dll))
    print(f"已构建 {dll}", flush=True)
    if not args.no_install:
        target = obs_dir / "obs-plugins" / "64bit"
        try:
            shutil.copy2(dll, target / dll.name)
        except PermissionError as exc:
            raise SystemExit(f"复制失败（OBS 是否还开着？）：{exc}") from exc
        print(f"已安装到 {target}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
