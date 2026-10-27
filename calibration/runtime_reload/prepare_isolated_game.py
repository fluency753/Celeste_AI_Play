"""Build a private Celeste installation for reload acceptance; never edit the live game.

Only immutable Content/native-library directories are junctioned. Saves, Mods,
Everest caches, settings and logs belong to the private installation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def junction(source: Path, target: Path) -> None:
    env = dict(os.environ, CELESTE_CHECK_LINK=str(target), CELESTE_CHECK_SOURCE=str(source))
    subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
        "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:CELESTE_CHECK_LINK "
        "-Target $env:CELESTE_CHECK_SOURCE | Out-Null",
    ], env=env, check=True, capture_output=True)


def prepare(game: Path, output: Path, ipc_port: int, clock_port: int, slot: int) -> dict:
    game, output = game.resolve(), output.resolve()
    allowed = (ROOT / "local").resolve()
    if output == allowed or allowed not in output.parents:
        raise ValueError("Isolation directory must be below this repository's ignored local/ directory")
    if output.exists():
        raise ValueError("Use a new isolation directory; existing game/test data is never overwritten")
    if not (game / "Celeste.dll").is_file():
        raise ValueError("Installed Celeste.dll not found")
    if ipc_port in {47774, 47776, 47780, 47790, 47792} or clock_port in {47774, 47776, 47780, 47790, 47792}:
        raise ValueError("Do not use a live controller/HUD/clock port for acceptance")
    if not (0 < ipc_port < 65536 and 0 < clock_port < 65536 and ipc_port != clock_port):
        raise ValueError("Two distinct TCP ports are required")
    sources = [game / "Saves" / "settings.celeste", *sorted((game / "Saves").glob(f"{slot}.*")),
               *sorted((game / "Saves").glob(f"{slot}-*"))]
    sources = [p for p in sources if p.is_file()]
    if not (game / "Saves" / f"{slot}.celeste").is_file():
        raise ValueError("Explicit source save slot does not exist")
    original_hashes = {str(p): digest(p) for p in sources}
    output.mkdir(parents=True)
    for path in game.iterdir():
        if path.is_file() and path.suffix.lower() in {".exe", ".dll", ".json", ".yaml", ".yml", ".config", ".pdb"}:
            shutil.copy2(path, output / path.name)
    for name in ("BuildIsFNA.txt", "FileProxyStreamDummy.txt", "gamecontrollerdb.txt", "steam_appid.txt"):
        if (game / name).is_file():
            shutil.copy2(game / name, output / name)
    # BOOT.RestartViaLauncher otherwise asks Steam to start the registered live
    # installation and exits before Everest writes a private log.
    (output / "steam_appid.txt").write_text("504230\n", encoding="ascii")
    # A copied marker could redirect settings to the real user's XDG folder.
    if (output / "EverestXDGFlag").exists():
        raise ValueError("Unexpected XDG marker in private game")
    for name in ("Content", "everest-lib", "lib64-win-x64"):
        if (game / name).is_dir():
            junction(game / name, output / name)
    for name in ("piton-apphosts", "piton-runtime"):
        if (game / name).is_dir():
            shutil.copytree(game / name, output / name)
    (output / "orig").mkdir()
    for path in (game / "orig").iterdir():
        if path.is_file():
            shutil.copy2(path, output / "orig" / path.name)
    saves = output / "Saves"
    saves.mkdir()
    for path in sources:
        shutil.copy2(path, saves / path.name)
    # Do not import user Mod settings or the user's enabled-Mod profile.
    (saves / "modsettings-Everest.celeste").write_text("CodeReload_WIP: true\n", encoding="utf-8")
    for module in ("CelesteAIRuntime", "CelesteAIBridge"):
        (saves / f"modsettings-{module}.celeste").write_text(
            f"Port: {ipc_port}\nClockPort: {clock_port}\n", encoding="utf-8")
    mods = output / "Mods"
    mods.mkdir()
    for folder, module in (("game-runtime", "CelesteAIRuntime"), ("game-bridge", "CelesteAIBridge")):
        source = ROOT / folder
        target = mods / module
        (target / "bin").mkdir(parents=True)
        shutil.copy2(source / "everest.yaml", target / "everest.yaml")
        for extension in ("dll", "pdb"):
            artifact = source / "bin" / f"{module}.{extension}"
            if artifact.exists():
                shutil.copy2(artifact, target / "bin" / artifact.name)
        if not (target / "bin" / f"{module}.dll").is_file():
            raise ValueError(f"Build {module} before preparing acceptance")
    manifest = {"source": str(game), "output": str(output), "slot": slot,
                "ipc_port": ipc_port, "clock_port": clock_port, "source_hashes": original_hashes}
    (output / "isolation.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if any(digest(Path(path)) != value for path, value in original_hashes.items()):
        raise RuntimeError("Original save/settings changed during preparation")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ipc-port", type=int, default=48974)
    parser.add_argument("--clock-port", type=int, default=48976)
    parser.add_argument("--slot", type=int, required=True)
    args = parser.parse_args()
    manifest = prepare(args.game, args.output, args.ipc_port, args.clock_port, args.slot)
    print(json.dumps({key: value for key, value in manifest.items() if key != "source_hashes"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
