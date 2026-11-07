"""OBS 的录制配置：建专用的配置文件与场景集合，启动、关闭 OBS。不改动你自己的配置文件与场景。

    python tools/obs_setup.py setup     # 写入配置文件与场景集合 CelesteAI（场景集合已存在时不动，--reset 重建）
    python tools/obs_setup.py launch    # 写入后用它们启动 OBS（最小化到托盘）；记下你原来选中的配置，关闭时还原
    python tools/obs_setup.py stop      # 关闭 OBS，并把 OBS 下次打开时选中的配置还原成你原来的（--force：关不掉时强制结束）
    python tools/obs_setup.py status    # OBS 是否在运行、插件是否加载并连上桥接（看 OBS 最新日志）
    python tools/obs_setup.py private [--move-personal-plugin]   # 建（或更新）私有便携版 OBS，见下
    python tools/obs_setup.py personal-capture [--apply]         # 你自己的 OBS 只采集标题为 Celeste 的游戏窗口

私有 OBS（可选）：从你的 OBS 安装复制一份便携版到 local/obs（只复制 OBS 官方签名的组件与插件，不带调试符号、
第三方插件和你的配置），配置在 local/obs/config，用 --portable --multi 启动，与你自己的 OBS 同时运行。建好后
主控只启动、关闭这一份（按进程路径区分，不碰你自己的 OBS），锁步插件也只装在这里：--move-personal-plugin
把你的 OBS 里的插件移到 local/obs-personal-backup（两个 OBS 都加载它时会互相顶掉桥接的时钟连接）。
配合 AI 专用游戏副本使用：personal-capture --apply 把你自己的场景里采集 Celeste.exe 的来源改成只认标题
“Celeste”的窗口（先备份），个人直播不会采到标题为“Celeste AI Play”的副本。

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
import urllib.request
import csv
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from orchestrator.recording import load_config  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 从没有控制台的进程（pythonw）里调用时不弹黑色窗口
STATE = REPO_ROOT / "build" / "obs_previous.json"   # 启动前选中的配置文件与场景集合
SELECTION_KEYS = ("Profile", "ProfileDir", "SceneCollection", "SceneCollectionFile")
PRIVATE_ROOT = REPO_ROOT / "local" / "obs"          # 私有便携版 OBS
PRIVATE_MARKER = "celeste-ai-obs.json"
PERSONAL_BACKUPS = REPO_ROOT / "local" / "obs-personal-backup"
PLUGIN_DLL = "celeste-lockstep.dll"
PERSONAL_WINDOW = "Celeste:SDL_app:Celeste.exe"     # 你自己的游戏窗口（按标题匹配时只认标题 Celeste）
PORTABLE_FLAGS = ("portable_mode.txt", "obs_portable_mode.txt", "portable_mode", "obs_portable_mode")


def _inside(path, root) -> bool:
    from orchestrator.game_instance import inside
    return inside(path, root)


def private_root() -> Path | None:
    """私有便携版 OBS（private 命令建的 local/obs）存在时返回它的目录。"""
    if (PRIVATE_ROOT / PRIVATE_MARKER).is_file() and (PRIVATE_ROOT / "bin" / "64bit" / "obs64.exe").is_file():
        return PRIVATE_ROOT
    return None


def personal_config_dir() -> Path:
    """你自己的 OBS 的配置目录。"""
    return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "obs-studio"


def obs_config_dir() -> Path:
    """主控用的 OBS 的配置目录：有私有 OBS 时是它的便携配置（local/obs/config/obs-studio），否则是你自己的 OBS 的。"""
    root = private_root()
    return root / "config" / "obs-studio" if root else personal_config_dir()


def label() -> str:
    return "私有 OBS" if private_root() else "OBS"


def personal_obs_dir(explicit: str | None = None) -> Path:
    """你自己的 OBS 的安装目录（不是私有 OBS）。"""
    from orchestrator.gamepaths import obs_candidates
    for path in obs_candidates(explicit, load_config().get("obs_dir")):
        if not _inside(path, PRIVATE_ROOT):
            return path
    raise SystemExit("找不到 OBS：用 --obs-dir 指定安装目录，或写进 configs/local.recording.json 的 obs_dir")


def find_obs_dir(explicit: str | None) -> Path:
    """主控用的 OBS 的安装目录：有私有 OBS 时就是它（--obs-dir 明确指定时除外）。"""
    if not explicit and (root := private_root()):
        return root
    return personal_obs_dir(explicit)


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


def capture_target(cfg: dict) -> tuple[str, int]:
    """游戏画面与声音采集匹配哪个窗口：(OBS 的窗口串 "标题:类名:程序", 匹配方式)。
    AI 专用副本模式下按窗口标题匹配（1）：标题不符的窗口直接排除，个人的《蔚蓝》同时开着也不会被录进去；
    否则按可执行文件匹配（2，原来的做法）。"""
    try:
        from orchestrator.game_instance import current
        marker = current()
    except Exception:  # noqa: BLE001（读不到副本标记时按原来的方式）
        marker = None
    if marker:
        title = marker["window_title"].replace(":", "#3A")
        return f"{title}:SDL_app:Celeste.exe", 1
    return cfg["game_window"], 2


GAME_SOURCES = ("game_capture", "wasapi_process_output_capture")


def sync_game_capture(collection: Path, cfg: dict) -> bool:
    """已有场景集合里由本工具建的游戏画面／声音来源（窗口串以 :SDL_app:Celeste.exe 结尾）改成当前的采集目标；
    OBS 运行时不改（它退出时会写回自己的设置）。返回是否改了。"""
    if obs_running():
        return False
    try:
        data = json.loads(collection.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    window, priority = capture_target(cfg)
    changed = False
    for source in data.get("sources", []):
        settings = source.get("settings") or {}
        if source.get("id") in GAME_SOURCES and str(settings.get("window", "")).endswith(":SDL_app:Celeste.exe"):
            if settings.get("window") != window or settings.get("priority") != priority:
                settings.update(window=window, priority=priority)
                source["settings"] = settings
                changed = True
    if changed:
        collection.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    return changed


def capture_mismatch(cfg: dict) -> str | None:
    """AI 副本模式下 OBS 正在运行、而它的场景集合仍不按副本窗口标题采集时，返回说明；其他情况返回 None。"""
    window, priority = capture_target(cfg)
    if priority != 1:
        return None
    collection = obs_config_dir() / "basic" / "scenes" / f"{cfg.get('collection_file', cfg['collection'])}.json"
    try:
        data = json.loads(collection.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for source in data.get("sources", []):
        settings = source.get("settings") or {}
        if source.get("id") == "game_capture" and str(settings.get("window", "")).endswith(":SDL_app:Celeste.exe"):
            if settings.get("window") != window or settings.get("priority") != priority:
                return ("OBS 正在运行，它的游戏画面来源仍按可执行文件匹配窗口，个人的《蔚蓝》开着时可能录错游戏。"
                        "请关闭 OBS，主控会改成按 AI 副本的窗口标题采集后重新启动它")
    return None


def scene_collection(cfg: dict) -> dict:
    width, height = cfg["canvas"]
    hud = cfg["hud"]
    window, priority = capture_target(cfg)   # 2：按可执行文件匹配窗口；1：按窗口标题（AI 副本）
    game = _source("Celeste 游戏画面", "game_capture", {
        "capture_mode": "window", "window": window, "priority": priority,
        "capture_cursor": False, "allow_transparency": False, "limit_framerate": False,
        "capture_overlays": False, "anti_cheat_hook": True, "hook_rate": 1, "capture_audio": False,
    })
    sound = _source("Celeste 游戏声音", "wasapi_process_output_capture",
                    {"window": window, "priority": priority}, mixers=0xFF)
    # OBS 33 的本地文件源不能跨源访问回环服务；默认用同源 HTTP，启动 OBS 前先启动 HUD 服务。
    page = REPO_ROOT / "overlay" / "hud.html"
    overlay = _source("HUD", "browser_source", {
        "is_local_file": bool(hud.get("local_file", False)), "local_file": str(page).replace(os.sep, "/"),
        "url": hud["url"], "width": hud["width"], "height": hud["height"], "css": "",
        "reroute_audio": False, "restart_when_active": False, "shutdown": False, "fps_custom": False,
    })
    scene_name = cfg.get("scene", "Celeste AI")
    scene = _source(scene_name, "scene", {
        "id_counter": 3, "custom_size": False,
        "items": [_item(game, 1, 0.0, 0.0, (float(width), float(height))),
                  _item(sound, 2, 0.0, 0.0, None),
                  _item(overlay, 3, float(hud["x"]), float(hud["y"]), None, locked=False)],
    })
    return {
        "current_scene": scene_name, "current_program_scene": scene_name,
        "scene_order": [{"name": scene_name}], "name": cfg["collection"],
        "sources": [scene, game, sound, overlay], "groups": [], "quick_transitions": [], "transitions": [],
        "saved_projectors": [], "current_transition": "Fade", "transition_duration": 300, "preview_locked": False,
        "scaling_enabled": False, "scaling_level": 0, "scaling_off_x": 0.0, "scaling_off_y": 0.0,
        "modules": {},
    }


def sync_hud_size(collection: Path, cfg: dict) -> bool:
    """迁移旧整画布 HUD；迁移后保留用户在 OBS 中调整的来源尺寸和所有场景变换。"""
    if obs_running():
        return False
    try:
        data = json.loads(collection.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hud = cfg["hud"]
    changed = False
    migrated = set()
    for source in data.get("sources", []):
        settings = source.get("settings") or {}
        if source.get("id") == "browser_source" and source.get("name") == "HUD":
            desired = {"url": hud["url"], "is_local_file": bool(hud.get("local_file", False))}
            if [settings.get("width"), settings.get("height")] == cfg["canvas"]:
                desired.update(width=hud["width"], height=hud["height"])
                migrated.add(source.get("uuid"))
            for key, value in desired.items():
                if settings.get(key) != value:
                    settings[key] = value
                    changed = True
    for source in data.get("sources", []):
        if source.get("id") == "scene":
            for item in (source.get("settings") or {}).get("items") or []:
                if item.get("name") == "HUD" and item.get("source_uuid") in migrated:
                    if item.get("pos") == {"x": 0.0, "y": 0.0}:
                        item["pos"] = {"x": float(hud["x"]), "y": float(hud["y"])}
                        item.pop("pos_rel", None)  # OBS 33 重新计算相对位置
                    item["locked"] = False
                    changed = True
    if changed:
        collection.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    return changed


def setup(cfg: dict, reset: bool = False) -> None:
    """配置文件：每次都把画布、帧率、颜色、声音这几项写成录制需要的值（其他设置保留）。
    场景集合：不存在时才创建；--reset 时先备份再重建（你在 OBS 里对这个场景的调整会被覆盖）。"""
    if private_root():
        _private_base_config(cfg)
    base = obs_config_dir() / "basic"
    profile = base / "profiles" / cfg["profile"] / "basic.ini"
    collection = base / "scenes" / f"{cfg.get('collection_file', cfg['collection'])}.json"
    # OBS 按标准录像目录查询剩余空间；目录不存在时 Windows 查询失败，会显示 0 MB。
    Path(profile_settings(cfg)["SimpleOutput"]["FilePath"]).mkdir(parents=True, exist_ok=True)
    changed = merge_ini(profile, profile_settings(cfg))
    print(f"配置文件：{profile}{'（已更新）' if changed else ''}")
    if collection.exists() and not reset:
        resized = sync_hud_size(collection, cfg)
        captured = sync_game_capture(collection, cfg)
        notes = [text for flag, text in ((resized, "已更新 HUD 来源"), (captured, "游戏采集已改为当前目标")) if flag]
        print(f"场景集合：{collection}（已存在，" + ("、".join(notes) + "；保留 OBS 自定义布局" if notes else "未改动")
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

def _user_ini(config_dir: Path | None = None) -> Path:
    config_dir = config_dir or obs_config_dir()
    path = config_dir / "user.ini"
    return path if path.exists() else config_dir / "global.ini"


def read_selection(config_dir: Path | None = None) -> dict:
    values, section = {}, None
    try:
        lines = _user_ini(config_dir).read_text(encoding="utf-8-sig").splitlines()
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


def write_selection(values: dict, config_dir: Path | None = None) -> None:
    path = _user_ini(config_dir)
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

def _obs_pids() -> list[int]:
    """所有 obs64.exe 的进程号（tasklist 也看得到以管理员身份运行的）。"""
    if sys.platform != "win32":
        return []
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/FO", "CSV", "/NH"], capture_output=True,
                         text=True, errors="replace", creationflags=NO_WINDOW).stdout
    return [int(row[1]) for row in csv.reader(out.splitlines())
            if len(row) > 1 and row[0].lower() == "obs64.exe" and row[1].isdigit()]


def obs_processes(private: bool) -> list[int]:
    """正在运行的 OBS：private=True 只要私有 OBS（可执行文件在 local/obs 里），False 只要其他的（你自己的 OBS；
    读不到路径的，例如以管理员身份运行的，也算你自己的）。"""
    from orchestrator.gamewindow import process_path
    found = []
    for pid in _obs_pids():
        path = process_path(pid)
        if (bool(path) and _inside(path, PRIVATE_ROOT)) == private:
            found.append(pid)
    return found


def _post_close(pids: list[int]) -> int:
    """给这些 OBS 进程的主窗口（标题以“OBS ”开头）发 WM_CLOSE，让它正常退出。最小化到托盘时主窗口是隐藏的，
    taskkill 不加 /F 时它收不到关闭通知。返回发了几个窗口。"""
    if sys.platform != "win32" or not pids:
        return 0
    import ctypes
    from ctypes import wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wt.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    targets, sent = set(pids), 0

    def visit(hwnd, _lparam):
        nonlocal sent
        owner = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in targets:
            title = ctypes.create_unicode_buffer(300)
            user32.GetWindowTextW(hwnd, title, 300)
            if title.value.startswith("OBS "):
                user32.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE
                sent += 1
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return sent


def obs_running() -> bool:
    """主控用的 OBS 是否在运行：有私有 OBS 时只看它（你自己的 OBS 开着不算），否则看任何 OBS。"""
    if private_root():
        return bool(obs_processes(private=True))
    return bool(_obs_pids())


def launch(cfg: dict, obs_dir: Path, minimized: bool, reset: bool = False) -> None:
    root = private_root()
    if obs_running():
        raise SystemExit(f"{label()} 已经在运行；主控可以沿用当前场景。需要重新启动时请先关闭它。")
    setup(cfg, reset)
    ensure_hud_service(cfg)
    if root:
        obs_dir = root      # 私有 OBS 总是选中专用配置（_private_base_config），不用记下、还原你的选择
    else:
        selection = read_selection()
        if selection.get("Profile") != cfg["profile"]:
            STATE.parent.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    clear_stale_sentinel()
    exe = obs_dir / "bin" / "64bit" / "obs64.exe"
    args = [str(exe), "--profile", cfg["profile"], "--collection", cfg["collection"], "--scene", cfg.get("scene", "Celeste AI"),
            "--disable-updater", "--disable-missing-files-check"]
    if root:
        args += ["--portable", "--multi"]   # 配置在 local/obs/config；与你自己的 OBS 同时运行
    if minimized:
        args.append("--minimize-to-tray")
    # 独立于启动它的进程：启动脚本所在的终端或作业被关掉时，OBS 不跟着被结束
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(args, cwd=exe.parent, creationflags=flags | 0x01000000)   # CREATE_BREAKAWAY_FROM_JOB
    except OSError:
        subprocess.Popen(args, cwd=exe.parent, creationflags=flags)
    print(f"已启动 {label()}（{exe}）")
    started = time.monotonic()
    while time.monotonic() - started < 30:
        time.sleep(0.5)
        info = plugin_log_status()
        if info.get("connected"):
            break
    print_status()


def ensure_hud_service(cfg: dict) -> None:
    """URL 模式要在 OBS 打开页面之前就绪。独立启动的服务可跨多次主控运行复用。"""
    if cfg["hud"].get("local_file", False):
        return
    url = cfg["hud"]["url"]
    from urllib.parse import urlsplit
    address = urlsplit(url)
    if address.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("自动启动 HUD 只支持本机地址")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def ready() -> bool:
        try:
            with opener.open(url, timeout=1) as response:
                return response.status == 200
        except OSError:
            return False

    if ready():
        return
    log = REPO_ROOT / "build" / "hud-service.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as stream:
        subprocess.Popen([sys.executable, "-X", "utf8", "-m", "overlay.server", "--port", str(address.port or 80)],
                         cwd=REPO_ROOT, stdout=stream, stderr=stream, stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.1)
    raise SystemExit(f"HUD 服务未启动，见 {log}")


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
    root = private_root()
    if root:
        mine = obs_processes(private=False)
        print(f"私有 OBS：{root}；你自己的 OBS {'开着' if mine else '没开'}")
        if (personal := _personal_plugin()) is not None:
            print(f"注意：你自己的 OBS 里还有锁步插件（{personal}），两个 OBS 同时开时会互相顶掉桥接的时钟连接；"
                  "用 private --move-personal-plugin 移走")
    print(f"{label()} 运行中：{'是' if obs_running() else '否'}；最新日志：{info.get('log')}")
    if info:
        print(f"锁步插件已加载：{'是' if info.get('loaded') else '否（检查 obs-plugins/64bit/celeste-lockstep.dll）'}；"
              f"已连上桥接时钟端口：{'是' if info.get('connected') else '否（游戏与桥接是否在运行？插件每秒重连一次）'}")


def stop(wait: float = 20.0, force: bool = False) -> None:
    """关闭 OBS 并还原配置选择。force：正常关闭在 wait 秒内没有退出时强制结束（录制要已经收尾；
    最小化到托盘的 OBS 有时不响应正常关闭）。强制结束留下的崩溃标记在下次 launch 时清掉。"""
    root = private_root()

    def taskkill(*flags: str) -> None:
        if root:    # 只关私有 OBS 的进程，不碰你自己的 OBS
            targets = [arg for pid in obs_processes(private=True) for arg in ("/PID", str(pid))]
            if targets:
                subprocess.run(["taskkill", *flags, *targets], capture_output=True, creationflags=NO_WINDOW)
        else:
            subprocess.run(["taskkill", *flags, "/IM", "obs64.exe"], capture_output=True, creationflags=NO_WINDOW)

    if obs_running():
        _post_close(obs_processes(private=True) if root else _obs_pids())   # 托盘里的 OBS 也正常退出
        taskkill()   # 正常关闭（不加 /F）
        deadline = time.monotonic() + wait
        while obs_running() and time.monotonic() < deadline:
            time.sleep(0.5)
        if obs_running() and force:
            print(f"{label()} 在 {wait:g} 秒内没有正常退出，强制结束")
            taskkill("/F")
            deadline = time.monotonic() + 10
            while obs_running() and time.monotonic() < deadline:
                time.sleep(0.5)
        if obs_running():
            raise SystemExit(f"{label()} 没有在限定时间内退出：请手动关闭（或运行 python tools/obs_setup.py stop --force）"
                             + ("" if root else "后再运行 python tools/obs_setup.py restore"))
        print(f"已关闭 {label()}")
    if not root:
        restore()


def restore() -> None:
    if not STATE.exists():
        return
    if obs_processes(private=False):
        raise SystemExit("OBS 还在运行：关闭后再还原")
    selection = json.loads(STATE.read_text(encoding="utf-8"))
    if selection:
        write_selection(selection, personal_config_dir())
        print(f"已还原 OBS 下次打开时的配置文件与场景集合：{selection.get('Profile')} / {selection.get('SceneCollection')}")
    STATE.unlink()


# ---------------------------------------------------------------- 私有 OBS

def _ini_value(path: Path, section: str, key: str) -> str | None:
    current = None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1]
        elif current == section and "=" in line and line.split("=", 1)[0] == key:
            return line.split("=", 1)[1].strip()
    return None


def _private_base_config(cfg: dict) -> None:
    """私有 OBS 的全局设置：不弹首次运行的自动配置向导、不检查更新，默认选中专用的配置文件与场景集合。"""
    conf = obs_config_dir()
    general = {"FirstRun": "true", "EnableAutoUpdates": "false"}
    for name in ("user.ini", "global.ini"):   # 与你的 OBS 是同一版本：沿用它记下的版本号，不弹“新功能”介绍
        if last := _ini_value(personal_config_dir() / name, "General", "LastVersion"):
            general["LastVersion"] = last
            break
    basic = {"Profile": cfg["profile"], "ProfileDir": cfg["profile"], "SceneCollection": cfg["collection"],
             "SceneCollectionFile": cfg.get("collection_file", cfg["collection"])}
    merge_ini(conf / "global.ini", {"General": general})
    merge_ini(conf / "user.ini", {"General": general, "Basic": basic})


def _personal_plugin() -> Path | None:
    """你自己的 OBS 里装着的锁步插件（没有或找不到你的 OBS 时返回 None）。"""
    try:
        path = personal_obs_dir() / "obs-plugins" / "64bit" / PLUGIN_DLL
    except SystemExit:
        return None
    return path if path.is_file() else None


def _signed_by_obs(paths: list[Path]) -> set[str]:
    """这些文件里由 OBS Project 签名的（OBS 自带的组件与插件），返回 normcase 后的路径。"""
    if not paths:
        return set()
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as listing:
        listing.write("\n".join(str(p) for p in paths))
    try:
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  f"Get-Content -LiteralPath '{listing.name.replace(chr(39), chr(39) * 2)}' -Encoding UTF8 | ForEach-Object {{ "
                  "$s = Get-AuthenticodeSignature -LiteralPath $_; "
                  "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'OBS Project') { $_ } }")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=600).stdout
    finally:
        os.unlink(listing.name)
    return {os.path.normcase(line.strip()) for line in out.splitlines() if line.strip()}


def _copy_obs(source: Path, root: Path, log=print) -> list[str]:
    """把 OBS 程序复制到 root：跳过调试符号（.pdb）、源里的便携配置，以及不是 OBS 官方签名的插件
    （第三方插件与旧的锁步插件）和它们的 data 目录。返回跳过的插件文件名。"""
    plugins = source / "obs-plugins" / "64bit"
    modules = [p for p in plugins.iterdir() if p.is_file() and p.suffix.lower() in (".dll", ".exe")] if plugins.is_dir() else []
    official = _signed_by_obs(modules)
    skipped = sorted(p.name for p in modules if os.path.normcase(str(p)) not in official)
    stems = {Path(name).stem.lower() for name in skipped}
    count = 0

    def same(a, b) -> bool:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def ignore(directory, names):
        out = []
        for name in names:
            lower = name.lower()
            if lower.endswith(".pdb"):
                out.append(name)
            elif same(directory, source) and (lower == "config" or lower in PORTABLE_FLAGS or name == PRIVATE_MARKER):
                out.append(name)
            elif same(directory, plugins) and name in skipped:
                out.append(name)
            elif same(directory, source / "data" / "obs-plugins") and lower in stems:
                out.append(name)
        return out

    def copy(src, dst):
        nonlocal count
        count += 1
        return shutil.copy2(src, dst)

    shutil.copytree(source, root, ignore=ignore, copy_function=copy, dirs_exist_ok=True)
    log(f"已复制 OBS 程序：{count} 个文件" + (f"；不复制非官方插件：{'、'.join(skipped)}" if skipped else ""))
    return skipped


def private_create(source: str | None = None, move_personal_plugin: bool = False, log=print) -> Path:
    """建（或更新）私有便携版 OBS：复制程序到 local/obs，写便携模式标记，装锁步插件，写专用配置。
    已有时只刷新程序文件（OBS 升级后），配置与插件保留。不改你自己的 OBS 的配置；
    move_personal_plugin 时把你的 OBS 里的锁步插件移到 local/obs-personal-backup。"""
    personal = Path(source) if source else personal_obs_dir()
    if not (personal / "bin" / "64bit" / "obs64.exe").is_file():
        raise SystemExit(f"{personal} 里没有 bin\\64bit\\obs64.exe，不是 OBS 的安装目录")
    if _inside(personal, PRIVATE_ROOT) or _inside(PRIVATE_ROOT, personal):
        raise SystemExit("私有 OBS 不能与来源 OBS 互相包含")
    if obs_processes(private=True):
        raise SystemExit("私有 OBS 正在运行：先关闭它（python tools/obs_setup.py stop）")
    personal_plugin = personal / "obs-plugins" / "64bit" / PLUGIN_DLL
    if move_personal_plugin and personal_plugin.is_file() and obs_processes(private=False):
        raise SystemExit("你自己的 OBS 正在运行（插件文件被占用）：关闭后再移走锁步插件")
    updating = private_root() is not None
    log(f"{'更新' if updating else '建立'}私有 OBS：从 {personal} 复制到 {PRIVATE_ROOT}（只复制 OBS 官方组件，不带你的配置）")
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
    _copy_obs(personal, PRIVATE_ROOT, log)
    (PRIVATE_ROOT / "portable_mode.txt").write_text("", encoding="utf-8")
    plugin = PRIVATE_ROOT / "obs-plugins" / "64bit" / PLUGIN_DLL
    built = REPO_ROOT / "build" / "obs-plugin" / PLUGIN_DLL
    if move_personal_plugin and personal_plugin.is_file():
        backup = PERSONAL_BACKUPS / datetime.now().strftime("%Y%m%d-%H%M%S") / "obs-plugins" / "64bit"
        backup.mkdir(parents=True, exist_ok=True)
        moved = Path(shutil.move(str(personal_plugin), str(backup / PLUGIN_DLL)))
        log(f"已把你的 OBS 里的锁步插件移到 {moved}")
        if not plugin.is_file():
            shutil.copy2(moved, plugin)
    if not plugin.is_file():
        for candidate in (built, personal_plugin):
            if candidate.is_file():
                shutil.copy2(candidate, plugin)
                break
    if plugin.is_file():
        log(f"锁步插件：{plugin}")
    else:
        log("私有 OBS 里还没有锁步插件：运行 python tools/build_obs_plugin.py 构建并安装")
    if personal_plugin.is_file():
        log("注意：你自己的 OBS 里还有锁步插件，两个 OBS 同时开时会互相顶掉桥接的时钟连接；"
            "关闭你的 OBS 后用 private --move-personal-plugin 移走")
    version = ""
    try:
        from tools.build_obs_plugin import obs_version
        version = obs_version(PRIVATE_ROOT)
    except (SystemExit, OSError):
        pass
    from orchestrator.session_store import write_json
    marker = {"format": 1, "source": str(personal), "version": version,
              ("updated" if updating else "created"): datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if updating:
        try:
            marker = {**json.loads((PRIVATE_ROOT / PRIVATE_MARKER).read_text(encoding="utf-8")), **marker}
        except (OSError, ValueError):
            pass
    write_json(PRIVATE_ROOT / PRIVATE_MARKER, marker, indent=2)
    setup(load_config())
    if STATE.exists() and not obs_processes(private=False):
        restore()     # 以前主控用你自己的 OBS 时记下的选择，现在还回去
    log(f"私有 OBS 已就绪：{PRIVATE_ROOT}（主控以后只启动、关闭这一份）")
    return PRIVATE_ROOT


def personal_capture(apply: bool = False, log=print) -> list[str]:
    """你自己的 OBS 的场景集合里，采集 Celeste.exe 窗口的游戏画面／声音来源改成只认标题为 Celeste 的窗口
    （匹配方式“标题必须匹配”）：AI 副本的窗口标题是 Celeste AI Play，个人直播不会采到它。
    apply=False 只列出要改的；apply=True 先把原文件备份到 local/obs-personal-backup 再改。你的 OBS 开着时不改。"""
    scenes = personal_config_dir() / "basic" / "scenes"
    if apply and obs_processes(private=False):
        raise SystemExit("你自己的 OBS 正在运行：关闭后再改（它退出时会写回自己的设置）")
    project = load_config()["collection"]
    backup = PERSONAL_BACKUPS / datetime.now().strftime("%Y%m%d-%H%M%S") / "scenes"
    changes = []
    for path in sorted(scenes.glob("*.json")) if scenes.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("name") == project:   # 本工具的专用场景集合不算你的
            continue
        found = []
        for source in data.get("sources", []):
            settings = source.get("settings") or {}
            window = str(settings.get("window", ""))
            if source.get("id") in GAME_SOURCES and window.endswith(":SDL_app:Celeste.exe") \
                    and (window != PERSONAL_WINDOW or settings.get("priority") != 1):
                found.append(f"{data.get('name', path.stem)} / {source.get('name')}（{window}，匹配方式 {settings.get('priority')}）")
                settings.update(window=PERSONAL_WINDOW, priority=1)
                source["settings"] = settings
        if found and apply:
            backup.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup / path.name)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
        changes += found
    if not changes:
        log("你自己的 OBS 里没有需要改的游戏采集来源")
    for line in changes:
        log(("已改：" if apply else "要改：") + line + " → 只认标题 Celeste")
    if changes and apply:
        log(f"原文件备份在 {backup}")
    return changes


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["setup", "launch", "stop", "status", "restore", "private", "personal-capture"])
    ap.add_argument("--obs-dir", help="private：从这个 OBS 安装复制（默认你自己的 OBS）")
    ap.add_argument("--move-personal-plugin", action="store_true",
                    help="private：把你自己的 OBS 里的锁步插件移到 local/obs-personal-backup（要先关闭你的 OBS）")
    ap.add_argument("--apply", action="store_true", help="personal-capture：真的改（不加只列出要改的）")
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
    elif args.command == "private":
        private_create(args.obs_dir, args.move_personal_plugin)
    elif args.command == "personal-capture":
        personal_capture(args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
