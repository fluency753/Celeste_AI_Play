"""本机设置与提供方账户（docs/开发/设置分层.md 第二步）。

- 本机设置：这台机器、这套组件的环境（运行方式、端口、录像与 OBS、HUD 服务与窗口），所有配置共用，
  存在 configs/local.console.json 的 "machine"（不入库），切换配置时不变，配置与预置配置都不再带它们。
  第一次读取时从当前选中配置的表单、本机最近一次运行的记录依次取值（没有的用默认值），所以升级后行为不变。
- 提供方账户：用量上限与额度用完后是否等待，存在 configs/local.providers.json 对应提供方的 "usage"
  （Anthropic 订阅也写在这里）；只对 Claude 执行器生效（Codex 没有额度等待）。
命令行仍可覆盖任何一项。
"""
from __future__ import annotations

import threading

from . import config as _config

MACHINE_KEYS = ("runtime", "bridge_port", "mcp_port", "hud_port", "hud", "hud_window", "ffmpeg", "record_video_args",
                "audio_offset_ms", "recording_mode", "freeze_chapter_select", "freeze_file_select", "freeze_main_menu",
                "keep_obs_open", "continuous_music")
# 可调参数里属于本机的：自写程序最多用几个 CPU 核（默认本机核数）。存在本机设置里，开跑时由控制台写成 --tune
MACHINE_TUNES = ("agent_cpus",)
# 运行记录（run_state.json）里有的本机设置：迁移时没有表单值就按本机最近一次运行
RUN_STATE_KEYS = ("runtime", "bridge_port", "recording_mode", "freeze_chapter_select", "freeze_file_select",
                  "freeze_main_menu")
USAGE_KEYS = ("max_usage", "quota_wait", "quota_wait_max_hours")
# 本次运行的设置（docs/开发/设置分层.md 第三步）：按组固定在配置里；没固定的组记住上次填写的（本机，所有配置共用）
RUN_GROUPS = {"model": ("engine", "provider", "model", "effort"), "agent": ("agent",),
              "chapters": ("map", "chapters", "segments", "sides", "seg_off"), "save": ("save",),
              "max_minutes": ("max_minutes",), "start_paused": ("start_paused",), "autocompact": ("autocompact",)}
RUN_KEYS = tuple(key for keys in RUN_GROUPS.values() for key in keys)
INDEPENDENT_KEYS = ("live", "record")       # 直播与 OBS 录屏：各自记住上次的选择，不进配置
DEBUG_KEYS = ("record_marker", "skip_enter", "no_agent", "play_speed", "auto_speed")   # 调试项：每次回到默认，不记住
LAST_MAX_BYTES = 50_000
USAGE_DEFAULTS = {"max_usage": None, "quota_wait": True, "quota_wait_max_hours": 24}
RESUME_MACHINE_KEYS = ('hud_port', 'hud', 'hud_window', 'keep_obs_open', 'ffmpeg')
_lock = threading.Lock()


def prefs_path():
    return _config.REPO_ROOT / "configs" / "local.console.json"


def _read(path) -> dict:
    from .session_store import load_json
    try:
        data = load_json(path, encoding="utf-8-sig")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def defaults() -> dict:
    from .launch_options import DEFAULTS
    return {**{key: DEFAULTS[key] for key in MACHINE_KEYS}, **{key: None for key in MACHINE_TUNES}}


def _tune(key, value):
    """本机的可调参数：None（默认）或范围内的数，范围与可调参数表相同。"""
    if value is None:
        return None
    from .tuning import coerce
    return coerce(key, value)


def validate(values: dict) -> dict:
    """只收本机设置的字段，按开跑时同样的规则校验（launch_options.normalize），返回规范化后的值。"""
    from .launch_options import DEFAULTS, normalize
    if not isinstance(values, dict):
        raise ValueError("本机设置须为对象")
    extra = set(values) - set(MACHINE_KEYS) - set(MACHINE_TUNES)
    if extra:
        raise ValueError("本机设置的字段无效：" + "、".join(sorted(extra)))
    merged = {**defaults(), **values}
    checked = normalize({"agent": "machine", **{k: v for k, v in merged.items() if k in DEFAULTS}})
    return {**{key: checked[key] for key in MACHINE_KEYS}, **{key: _tune(key, merged[key]) for key in MACHINE_TUNES}}


def _latest_run_state() -> dict:
    root = _config.REPO_ROOT / "runs"
    states = sorted(root.glob("*/*/run_state.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
    return _read(states[0]) if states else {}


def _selected_form() -> dict:
    from . import profiles
    try:
        form = profiles.meta(profiles.selected()).get("form") or {}
    except (OSError, ValueError):
        form = {}
    return form if isinstance(form, dict) else {}


def _seed_tunes(form: dict, keys=MACHINE_TUNES) -> dict:
    """本机的可调参数按选中配置的表单取值（1.19.0 之前它们在可调参数里，随配置保存）；不合规的按默认。"""
    tune = form.get("tune") if isinstance(form.get("tune"), dict) else {}
    seeded = {}
    for key in keys:
        try:
            seeded[key] = _tune(key, tune.get(key))
        except ValueError:
            seeded[key] = None
    return seeded


def _seed() -> dict:
    """第一次：当前选中配置的表单 > 本机最近一次运行 > 默认值，逐项取（取到的值不合规就跳过）。"""
    form = _selected_form()
    state = _latest_run_state()
    seeded = _seed_tunes(form)
    for key in MACHINE_KEYS:
        for source in (form, state if key in RUN_STATE_KEYS else {}):
            if key in source and source[key] is not None:
                try:
                    seeded[key] = validate({**seeded, key: source[key]})[key]
                    break
                except ValueError:
                    continue
    return validate(seeded)


def load() -> dict:
    """本机设置（缺的项用默认值）；还没有时按 _seed 迁移并写下来。"""
    path = prefs_path()
    with _lock:
        data = _read(path)
        stored = data.get("machine")
        if isinstance(stored, dict):
            missing = [key for key in MACHINE_TUNES if key not in stored]
            if missing:                    # 1.17.0～1.18.0 存的本机设置还没有这些：按选中配置补上一次
                stored = {**stored, **_seed_tunes(_selected_form(), missing)}
                _write(path, {**data, "machine": stored})
            tunes = {}
            for key in MACHINE_TUNES:      # 换了机器等原因超出范围的：按默认，不连累别的本机设置
                try:
                    tunes[key] = _tune(key, stored.get(key))
                except ValueError:
                    tunes[key] = None
            try:
                return {**validate({k: v for k, v in stored.items() if k in MACHINE_KEYS}), **tunes}
            except ValueError:
                pass                       # 手改坏了：按默认值，下次保存时覆盖
            return {**defaults(), **tunes}
        settings = _seed()
        _write(path, {**data, "machine": settings})
        return settings


def save(update: dict) -> dict:
    """保存本机设置（只改给出的字段），返回保存后的全部本机设置。"""
    if not isinstance(update, dict) or not update:
        raise ValueError("本机设置须为非空对象")
    current = load()
    settings = validate({**current, **update})
    path = prefs_path()
    with _lock:
        data = _read(path)
        _write(path, {**data, "machine": settings})
    return settings


def apply_resume_machine(args, state: dict, argv: list[str]) -> dict:
    """控制台续跑采用安全的本机偏好；连接、容器与录像身份不随偏好切换。"""
    settings = load()
    def explicit(flag):
        return any(arg == flag or arg.startswith(flag + '=') for arg in argv)
    applied = {}
    for key in RESUME_MACHINE_KEYS:
        flag = '--' + key.replace('_', '-')
        negative = '--no-' + key.replace('_', '-')
        if explicit(flag) or explicit(negative):
            continue
        value = settings[key]
        if key in ('hud', 'hud_window'):
            setattr(args, 'no_' + key, not value)
        else:
            setattr(args, key, (value or None) if key == 'ffmpeg' else value)
        applied[key] = value
    for key in ('bridge_port', 'mcp_port'):
        if not explicit('--' + key.replace('_', '-')) and key in state:
            setattr(args, key, state[key])
    ports = [args.bridge_port, args.mcp_port, args.hud_port]
    if any(type(port) is not int or not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != 3:
        raise ValueError('合并后的 HUD／桥接／MCP 端口无效或冲突；请调整本机 HUD 端口或显式 --hud-port，不改动原会话连接')
    retained = {
        'connection': '沿用原会话的桥接与 MCP 端口，不重定向到其他游戏',
        'runtime': '沿用原执行环境，不移动已有模型会话',
        'agent_cpus': '沿用原运行的 CPU 配置，由容器核验实际可用量，不采用本机新偏好',
        'recording': '沿用原录像组织、冻结策略及录制清单参数，不隐式重开录像'}
    return {'current_machine': applied, 'retained': retained}


def _write(path, data: dict) -> None:
    from .session_store import write_json
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data, indent=2)


# ---------------------------------------------------------------- 提供方账户

def normalize_usage(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - set(USAGE_KEYS):
        raise ValueError("用量设置的字段无效")
    out = dict(USAGE_DEFAULTS)
    if values.get("max_usage") not in (None, ""):
        value = values["max_usage"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.01 <= value <= 1:
            raise ValueError("提前暂停的用量比例须在 0.01～1 之间，或不设")
        out["max_usage"] = float(value)
    if "quota_wait" in values:
        if not isinstance(values["quota_wait"], bool):
            raise ValueError("额度用完时等待须为开或关")
        out["quota_wait"] = values["quota_wait"]
    if values.get("quota_wait_max_hours") not in (None, ""):
        value = values["quota_wait_max_hours"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.1 <= value <= 240:
            raise ValueError("额度等待上限须在 0.1～240 小时之间")
        out["quota_wait_max_hours"] = value
    return out


def provider_usage(info: dict | None) -> dict:
    """提供方设置里的 usage（读出来的值不合规时用默认值）。"""
    try:
        return normalize_usage({k: v for k, v in ((info or {}).get("usage") or {}).items() if k in USAGE_KEYS})
    except (ValueError, AttributeError):
        return dict(USAGE_DEFAULTS)


def usage_for(provider: str) -> dict:
    try:
        info = _config.load_provider(provider)
    except SystemExit:
        return dict(USAGE_DEFAULTS)
    return provider_usage(info)


def save_usage(provider: str, values: dict) -> dict:
    """把用量设置写进 configs/local.providers.json 的这个提供方（只写 usage，别的字段原样保留）。"""
    from . import provider_settings
    if provider == "codex":
        raise ValueError("Codex 执行器没有额度等待，用量设置不适用")
    usage = normalize_usage(values)
    with provider_settings._lock:
        _, local = provider_settings._read_local()
        try:
            _config.load_provider(provider)
        except SystemExit as exc:
            raise ValueError(str(exc)) from None
        entry = dict(local.get(provider) or {}) if isinstance(local.get(provider), dict) else {}
        entry["usage"] = {k: v for k, v in usage.items() if v != USAGE_DEFAULTS[k]}
        # An explicit empty object means the user chose the defaults. Keep it
        # so a legacy profile cannot migrate its old limits again.
        if entry:
            local[provider] = entry
        else:
            local.pop(provider, None)
        provider_settings._write_local(local)
    return usage_for(provider)


def seed_usage(form: dict | None) -> None:
    """迁移：选中配置的表单里有不是默认值的用量设置、而它的提供方还没设过时，写进提供方。"""
    form = form or {}
    if form.get("engine", "claude") != "claude":
        return
    provider = form.get("provider") or "anthropic"
    values = {k: form[k] for k in USAGE_KEYS if k in form}
    try:
        usage = normalize_usage(values)
        info = _config.load_provider(provider)
    except (ValueError, SystemExit):
        return
    if "usage" in info or usage == USAGE_DEFAULTS:
        return
    save_usage(provider, usage)


def pins_of(form: dict) -> list[str]:
    """配置固定了哪些组：新版写在 pins；旧版配置没有 pins，存过的本次运行设置全部算固定（迁移时行为不变）。"""
    pins = form.get("pins")
    if isinstance(pins, list):
        return [group for group in RUN_GROUPS if group in pins]
    return [group for group, keys in RUN_GROUPS.items() if any(key in form for key in keys)]


def strip(form: dict) -> dict:
    """配置的表单里去掉不随配置保存的项：本机设置、用量设置、直播与录屏、调试项、没固定的本次运行设置；
    写下 pins（固定了哪些组）。"""
    pins = pins_of(form)
    drop = set(MACHINE_KEYS) | set(USAGE_KEYS) | set(INDEPENDENT_KEYS) | set(DEBUG_KEYS)
    drop |= {key for group, keys in RUN_GROUPS.items() if group not in pins for key in keys}
    kept = {k: v for k, v in form.items() if k not in drop and k != "pins"}
    if isinstance(kept.get("tune"), dict):
        kept["tune"] = {k: v for k, v in kept["tune"].items() if k not in MACHINE_TUNES}
    return {**kept, "pins": pins}


# ---------------------------------------------------------------- 记住的上次填写

def _last_from(source: dict) -> dict:
    return {key: source[key] for key in (*RUN_KEYS, *INDEPENDENT_KEYS) if key in source}


def load_last() -> dict:
    """没固定的本次运行设置与直播、录屏上次填写的值。还没有时按当前选中配置的表单给出（不写文件，第一次保存时才写）。"""
    import json
    stored = _read(prefs_path()).get("last")
    if isinstance(stored, dict):
        return _last_from(stored)
    from . import profiles
    try:
        form = profiles.meta(profiles.selected()).get("form") or {}
    except (OSError, ValueError):
        form = {}
    seeded = _last_from(form)
    return seeded if len(json.dumps(seeded, ensure_ascii=False)) <= LAST_MAX_BYTES else {}


def save_last(values: dict) -> dict:
    """保存上次填写的值（只收本次运行的设置与直播、录屏；开跑时还会按运行设置逐项校验）。与原来记住的合并：
    当前配置固定了的组不在 values 里，原来记住的值留着，换到不固定它们的配置时照样用得上。"""
    import json
    if not isinstance(values, dict):
        raise ValueError("上次填写的值须为对象")
    extra = set(values) - set(RUN_KEYS) - set(INDEPENDENT_KEYS)
    if extra:
        raise ValueError("上次填写的值里有不认识的项：" + "、".join(sorted(extra)))
    if len(json.dumps(values, ensure_ascii=False).encode("utf-8")) > LAST_MAX_BYTES:
        raise ValueError("上次填写的值太大")
    path = prefs_path()
    with _lock:
        data = _read(path)
        stored = data.get("last") if isinstance(data.get("last"), dict) else {}
        merged = {**_last_from(stored), **values}
        if len(json.dumps(merged, ensure_ascii=False).encode("utf-8")) > LAST_MAX_BYTES:
            merged = dict(values)
        _write(path, {**data, "last": merged})
    return merged
