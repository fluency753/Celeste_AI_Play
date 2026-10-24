"""OBS 的录制配置：建专用的配置文件与场景集合，启动、关闭 OBS。不改动你自己的配置文件与场景。

    python tools/obs_setup.py setup     # 写入配置文件与场景集合 CelesteAI（场景集合已存在时不动，--reset 重建）
    python tools/obs_setup.py launch    # 写入后用它们启动 OBS（最小化到托盘）；记下你原来选中的配置，关闭时还原
    python tools/obs_setup.py stop      # 关闭 OBS，并把 OBS 下次打开时选中的配置还原成你原来的（--force：关不掉时强制结束）
    python tools/obs_setup.py status    # OBS 是否在运行、插件是否加载并连上桥接（看 OBS 最新日志）

配置文件（Video/Audio）：画布与输出 1920×1080、60 帧、NV12、Rec.709 有限范围；48 kHz 立体声。
场景（Celeste AI）：游戏画面（游戏采集，铺满画布）、游戏声音（应用程序音频采集，只录 Celeste.exe）、
HUD（浏览器源，右上角）。没有桌面音频和麦克风。

OBS 安装目录：--obs-dir，或 configs/local.recording.json 的 obs_dir，或几个默认位置。
插件构建与安装见 tools/build_obs_plugin.py。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.recording import load_config  # noqa: E402

STATE = REPO_ROOT / "build" / "obs_previous.json"   # 启动前选中的配置文件与场景集合
SELECTION_KEYS = ("Profile", "ProfileDir", "SceneCollection", "SceneCollectionFile")


def obs_config_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "obs-studio"


def find_obs_dir(explicit: str | None) -> Path:
    candidates = [explicit, load_config().get("obs_dir"),
                  os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "obs-studio"),
                  os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Steam", "steamapps", "common", "OBS Studio")]
    for c in candidates:
        if c and (Path(c) / "bin" / "64bit" / "obs64.exe").exists():
            return Path(c)
    raise SystemExit("找不到 OBS：用 --obs-dir 指定安装目录，或写进 configs/local.recording.json 的 obs_dir")


# ---------------------------------------------------------------- 配置文件与场景集合

def profile_settings(cfg: dict) -> dict[str, dict[str, str]]:
    width, height = cfg["canvas"]
    recordings = str(REPO_ROOT / "runs" / "obs").replace(os.sep, "/")
    return {
        "General": {"Name": cfg["profile"]},
        "Video": {"BaseCX": str(width), "BaseCY": str(height), "OutputCX": str(width), "OutputCY": str(height),
                  "FPSType": "0", "FPSCommon": str(cfg["fps"]), "ScaleType": "bicubic", "ColorFormat": "NV12",
                  "ColorSpace": "709", "ColorRange": "Partial"},
        "Audio": {"SampleRate": str(cfg["sample_rate"]), "ChannelSetup": "Stereo"},
        "SimpleOutput": {"FilePath": recordings},
    }


def merge_ini(path: Path, settings: dict[str, dict[str, str]]) -> bool:
    """把这些键写进 INI（保留文件里其他内容）；返回是否有改动。"""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    out, section, written = [], None, set()

    def flush(sec):
        blanks = 0
        while out and out[-1].strip() == "":   # 补的键放在这一节的空行之前
            out.pop()
            blanks += 1
        for key, value in settings.get(sec, {}).items():
            if (sec, key) not in written:
                out.append(f"{key}={value}")
                written.add((sec, key))
        out.extend([""] * blanks)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section is not None:
                flush(section)
            section = stripped[1:-1]
            out.append(line)
            continue
        if section in settings and "=" in line:
            key = line.split("=", 1)[0]
            if key in settings[section]:
                out.append(f"{key}={settings[section][key]}")
                written.add((section, key))
                continue
        out.append(line)
    if section is not None:
        flush(section)
    for sec in settings:
        if any((sec, key) not in written for key in settings[sec]):
            if out and out[-1] != "":
                out.append("")
            out.append(f"[{sec}]")
            flush(sec)
    text = "\n".join(out) + "\n"
    if path.exists() and path.read_text(encoding="utf-8-sig") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _source(name: str, kind: str, settings: dict, mixers: int = 0) -> dict:
    return {
        "name": name, "uuid": str(uuid.uuid4()), "id": kind, "versioned_id": kind, "settings": settings,
        "mixers": mixers, "sync": 0, "flags": 0, "volume": 1.0, "balance": 0.5, "enabled": True, "muted": False,
        "push-to-mute": False, "push-to-mute-delay": 0, "push-to-talk": False, "push-to-talk-delay": 0,
        "hotkeys": {}, "deinterlace_mode": 0, "deinterlace_field_order": 0, "monitoring_type": 0,
        "private_settings": {},
    }


def _item(source: dict, item_id: int, x: float, y: float, bounds: tuple[float, float] | None, locked: bool = True) -> dict:
    return {
        "name": source["name"], "source_uuid": source["uuid"], "visible": True, "locked": locked, "rot": 0.0,
        "pos": {"x": x, "y": y}, "scale": {"x": 1.0, "y": 1.0}, "align": 5,
        "bounds_type": 2 if bounds else 0, "bounds_align": 0,
        "bounds": {"x": bounds[0], "y": bounds[1]} if bounds else {"x": 0.0, "y": 0.0},
        "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "id": item_id,
        "group_item_backup": False, "scale_filter": "point", "blend_method": "default", "blend_type": "normal",
        "show_transition": {"duration": 0}, "hide_transition": {"duration": 0}, "private_settings": {},
    }


def scene_collection(cfg: dict) -> dict:
    width, height = cfg["canvas"]
    hud = cfg["hud"]
    game = _source("Celeste 游戏画面", "game_capture", {
        "capture_mode": "window", "window": cfg["game_window"], "priority": 2,  # 2：按可执行文件匹配窗口
        "capture_cursor": False, "allow_transparency": False, "limit_framerate": False,
        "capture_overlays": False, "anti_cheat_hook": True, "hook_rate": 1, "capture_audio": False,
    })
    sound = _source("Celeste 游戏声音", "wasapi_process_output_capture",
                    {"window": cfg["game_window"], "priority": 2}, mixers=0xFF)
    # 用本地文件打开 HUD 页面（页面再去连 HUD 服务取数据）：OBS 比主控先启动、HUD 服务还没起来时，
    # 网址模式的页面加载失败后不会重试，本地文件模式不受影响
    page = REPO_ROOT / "overlay" / "hud.html"
    overlay = _source("HUD", "browser_source", {
        "is_local_file": bool(hud.get("local_file", True)), "local_file": str(page).replace(os.sep, "/"),
        "url": hud["url"], "width": hud["width"], "height": hud["height"], "css": "",
        "reroute_audio": False, "restart_when_active": False, "shutdown": False, "fps_custom": False,
    })
    scene = _source("Celeste AI", "scene", {
        "id_counter": 3, "custom_size": False,
        "items": [_item(game, 1, 0.0, 0.0, (float(width), float(height))),
                  _item(sound, 2, 0.0, 0.0, None),
                  _item(overlay, 3, float(hud["x"]), float(hud["y"]), None)],
    })
    return {
        "current_scene": "Celeste AI", "current_program_scene": "Celeste AI",
        "scene_order": [{"name": "Celeste AI"}], "name": cfg["collection"],
        "sources": [scene, game, sound, overlay], "groups": [], "quick_transitions": [], "transitions": [],
        "saved_projectors": [], "current_transition": "Fade", "transition_duration": 300, "preview_locked": False,
        "scaling_enabled": False, "scaling_level": 0, "scaling_off_x": 0.0, "scaling_off_y": 0.0,
        "modules": {},
    }


def sync_hud_size(collection: Path, cfg: dict) -> bool:
    """场景集合已存在时，只把 HUD 浏览器源的宽高与它在场景里的位置改成配置里的值（HUD 改尺寸后，旧场景会切掉
    卡片或让它离开右上角），其他调整原样保留。OBS 退出时会写回场景集合，所以只在 OBS 没在运行时改。"""
    if obs_running():
        return False
    try:
        data = json.loads(collection.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hud = cfg["hud"]
    changed = False
    for source in data.get("sources", []):
        settings = source.get("settings") or {}
        if source.get("id") == "browser_source" and source.get("name") == "HUD":
            for key in ("width", "height"):
                if settings.get(key) != hud[key]:
                    settings[key] = hud[key]
                    changed = True
        if source.get("id") == "scene":
            for item in settings.get("items") or []:
                pos = {"x": float(hud["x"]), "y": float(hud["y"])}
                if item.get("name") == "HUD" and item.get("pos") != pos:
                    item["pos"] = pos
                    changed = True
    if changed:
        collection.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    return changed


def setup(cfg: dict, reset: bool = False) -> None:
    """配置文件：每次都把画布、帧率、颜色、声音这几项写成录制需要的值（其他设置保留）。
    场景集合：不存在时才创建；--reset 时先备份再重建（你在 OBS 里对这个场景的调整会被覆盖）。"""
    base = obs_config_dir() / "basic"
    profile = base / "profiles" / cfg["profile"] / "basic.ini"
    collection = base / "scenes" / f"{cfg['collection']}.json"
    changed = merge_ini(profile, profile_settings(cfg))
    print(f"配置文件：{profile}{'（已更新）' if changed else ''}")
    if collection.exists() and not reset:
        resized = sync_hud_size(collection, cfg)
        print(f"场景集合：{collection}（已存在，" + ("只把 HUD 浏览器源的尺寸与位置更新为配置值" if resized else "未改动")
              + "；要重建用 setup --reset）")
        return
    if collection.exists():
        backup = collection.with_name(collection.name + "." + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
        collection.replace(backup)
        print(f"已备份原场景集合 → {backup.name}")
    collection.parent.mkdir(parents=True, exist_ok=True)
    collection.write_text(json.dumps(scene_collection(cfg), ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"场景集合：{collection}（已创建）")


# ---------------------------------------------------------------- 选中的配置（user.ini）

def _user_ini() -> Path:
    path = obs_config_dir() / "user.ini"
    return path if path.exists() else obs_config_dir() / "global.ini"


def read_selection() -> dict:
    values, section = {}, None
    try:
        lines = _user_ini().read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        if line.startswith("["):
            section = line.strip()[1:-1]
        elif section == "Basic" and "=" in line:
            key, value = line.split("=", 1)
            if key in SELECTION_KEYS:
                values[key] = value
    return values


def write_selection(values: dict) -> None:
    path = _user_ini()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    out, section, done = [], None, set()
    for line in lines:
        if line.startswith("["):
            if section == "Basic":
                out += [f"{k}={v}" for k, v in values.items() if k not in done]
                done |= set(values)
            section = line.strip()[1:-1]
        elif section == "Basic" and "=" in line and line.split("=", 1)[0] in values:
            key = line.split("=", 1)[0]
            out.append(f"{key}={values[key]}")
            done.add(key)
            continue
        out.append(line)
    if section == "Basic":
        out += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 进程

def obs_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/NH"], capture_output=True, text=True,
                         errors="replace").stdout
    return "obs64.exe" in out


def launch(cfg: dict, obs_dir: Path, minimized: bool, reset: bool = False) -> None:
    if obs_running():
        raise SystemExit("OBS 已经在运行：请先关闭（或确认它用的是 CelesteAI 配置文件与场景集合，然后直接开始）")
    setup(cfg, reset)
    selection = read_selection()
    if selection.get("Profile") != cfg["profile"]:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    clear_stale_sentinel()
    exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    args = [str(exe), "--profile", cfg["profile"], "--collection", cfg["collection"], "--scene", "Celeste AI",
            "--disable-updater", "--disable-missing-files-check"]
    if minimized:
        args.append("--minimize-to-tray")
    # 独立于启动它的进程：启动脚本所在的终端或作业被关掉时，OBS 不跟着被结束
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(args, cwd=exe.parent, creationflags=flags | 0x01000000)   # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(args, cwd=exe.parent, creationflags=flags)
    print(f"已启动 OBS（{exe}）")
    started = time.monotonic()
    while time.monotonic() - started < 30:
        time.sleep(0.5)
        info = plugin_log_status()
        if info.get("connected"):
            break
    print_status()


def clear_stale_sentinel() -> None:
    """OBS 没有正常退出（崩溃或被强制结束）时会留下 .sentinel/run_* 文件，下次启动先弹窗问要不要进安全模式，
    插件在安全模式下不加载。OBS 没在运行时这些文件都是过时的：说明一下并删掉，按正常模式启动。"""
    sentinel = obs_config_dir() / ".sentinel"
    stale = list(sentinel.glob("run_*")) if sentinel.is_dir() else []
    if stale:
        print("上次 OBS 没有正常退出（崩溃或被强制结束）；按正常模式启动，不进安全模式")
        for path in stale:
            try:
                path.unlink()
            except OSError:
                pass


def latest_log() -> Path | None:
    logs = sorted((obs_config_dir() / "logs").glob("*.txt"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def plugin_log_status() -> dict:
    log = latest_log()
    if not log:
        return {}
    text = log.read_text(encoding="utf-8", errors="replace")
    connected = text.rfind("[celeste-lockstep] connected")
    disconnected = text.rfind("[celeste-lockstep] disconnected")
    return {"log": str(log), "loaded": "[celeste-lockstep] loaded" in text, "connected": connected > disconnected}


def print_status() -> None:
    info = plugin_log_status()
    print(f"OBS 运行中：{'是' if obs_running() else '否'}；最新日志：{info.get('log')}")
    if info:
        print(f"锁步插件已加载：{'是' if info.get('loaded') else '否（检查 obs-plugins/64bit/celeste-lockstep.dll）'}；"
              f"已连上桥接时钟端口：{'是' if info.get('connected') else '否（游戏与桥接是否在运行？插件每秒重连一次）'}")


def stop(wait: float = 20.0, force: bool = False) -> None:
    """关闭 OBS 并还原配置选择。force：正常关闭在 wait 秒内没有退出时强制结束（录制要已经收尾；
    最小化到托盘的 OBS 有时不响应正常关闭）。强制结束留下的崩溃标记在下次 launch 时清掉。"""
    if obs_running():
        subprocess.run(["taskkill", "/IM", "obs64.exe"], capture_output=True)   # 正常关闭（不加 /F）
        deadline = time.monotonic() + wait
        while obs_running() and time.monotonic() < deadline:
            time.sleep(0.5)
        if obs_running() and force:
            print(f"OBS 在 {wait:g} 秒内没有正常退出，强制结束")
            subprocess.run(["taskkill", "/F", "/IM", "obs64.exe"], capture_output=True)
            deadline = time.monotonic() + 10
            while obs_running() and time.monotonic() < deadline:
                time.sleep(0.5)
        if obs_running():
            raise SystemExit("OBS 没有在限定时间内退出：请手动关闭（或运行 python tools/obs_setup.py stop --force）"
                             "后再运行 python tools/obs_setup.py restore")
        print("已关闭 OBS")
    restore()


def restore() -> None:
    if not STATE.exists():
        return
    if obs_running():
        raise SystemExit("OBS 还在运行：关闭后再还原")
    selection = json.loads(STATE.read_text(encoding="utf-8"))
    if selection:
        write_selection(selection)
        print(f"已还原 OBS 下次打开时的配置文件与场景集合：{selection.get('Profile')} / {selection.get('SceneCollection')}")
    STATE.unlink()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "launch", "stop", "status", "restore"])
    ap.add_argument("--obs-dir")
    ap.add_argument("--show", action="store_true", help="launch 时显示 OBS 窗口（默认最小化到托盘）")
    ap.add_argument("--reset", action="store_true", help="setup / launch 时重建场景集合（原来的先备份）")
    ap.add_argument("--force", action="store_true", help="stop 时正常关闭 20 秒内没有退出就强制结束（先确认没有在录制）")
    args = ap.parse_args()
    cfg = load_config()
    if args.command == "setup":
        setup(cfg, args.reset)
    elif args.command == "launch":
        launch(cfg, find_obs_dir(args.obs_dir), minimized=not args.show, reset=args.reset)
    elif args.command == "stop":
        stop(force=args.force)
    elif args.command == "status":
        print_status()
    elif args.command == "restore":
        restore()
    return 0


if __name__ == "__main__":
    sys.exit(main())
