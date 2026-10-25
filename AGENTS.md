# Repository Guidelines

## Project Structure & Module Organization

- `orchestrator/`: Python controller, runtime adapters, game tools, recording, and prompts.
- `game-bridge/Source/`: C# Everest Mod providing frame control, observations, audio, and HUD rendering.
- `overlay/`: web HUD, desktop console, history, and configuration APIs.
- `obs-plugin/`: native C lockstep recording plugin; `tools/`: deployment, builds, and diagnostics.
- `calibration/`: regression tests and native checks; `kb/`: agent knowledge; `docs/`: guides and images.
- `configs/`: shared defaults. `local/`, `runs/`, `workspaces/`, and `sim/` are ignored local data.

## Build, Test, and Development Commands

Run from the repository root in PowerShell. Activate `.venv` before Python commands.

- `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Check`: check deployment readiness.
- `python -m overlay.server --port 47790`: serve `/hud` and `/console` locally.
- `python -m unittest discover -s calibration -p '*_test.py'`: run Python regressions.
- `dotnet build game-bridge -c Release "-p:CelestePrefix=<game-directory>"`: build the bridge against installed game assemblies.
- `dotnet run --project calibration/story_text "-p:CelestePrefix=<game-directory>"`: run native story checks.
- `python tools/build_obs_plugin.py --no-install`: build the OBS plugin without installing it.

## Coding Style & Naming Conventions

Use four-space Python/C# indentation; follow adjacent code. Use `snake_case` for Python functions/modules and `PascalCase` for C# types/methods. Preserve file encoding and `.gitattributes` line endings; avoid unrelated formatting. No repository-wide formatter or linter is configured.

## Testing & Integration Review

Python tests use `unittest`, named `calibration/*_test.py` with `test_*` methods. Add behavioral regressions for fixes; mock external processes and isolate tests from real controller state.

Before changing shared behavior:

- Trace every entry: `act`, `observe`, `cutscene`, retry, resume, and chapter transitions. Centralize shared rules.
- Check automatic/agent story modes, tool availability, pause/resume, death/respawn, retries, and multi-chapter boundaries where affected.
- Validate capabilities at startup and runtime switches. Do not infer input readiness solely from loss of player control.
- Review HUD/recording timing, tool schemas, prompts, knowledge, versions, and documentation together.

Verify deterministic behavior directly; use an independent agent to assess instructions and tool selection. Report verified scenarios and gaps, not test counts.

## Commit & Pull Request Guidelines

Use concise Chinese, action-oriented subjects. Explain behavior and validation; retain accurate coauthor trailers. PRs describe the problem, scope, checks, relevant issues, and UI screenshots. Follow the requested push/merge policy.

## Local Safety

Never commit credentials, saves, recordings, or decompiled simulator assets. Back up installed files before deployment. Preserve user OBS/CeleMod settings, use explicit test saves, and avoid opening a second bridge connection while a controller owns it.
