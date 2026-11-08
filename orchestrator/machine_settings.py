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
# 运行记录（run_state.json）里有的本机设置：迁移时没有表单值就按本机最近一次运行
RUN_STATE_KEYS = ("runtime", "bridge_port", "recording_mode", "freeze_chapter_select", "freeze_file_select",
                  "freeze_main_menu")
USAGE_KEYS = ("max_usage", "quota_wait", "quota_wait_max_hours")
USAGE_DEFAULTS = {"max_usage": None, "quota_wait": True, "quota_wait_max_hours": 24}
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
    return {key: DEFAULTS[key] for key in MACHINE_KEYS}


def validate(values: dict) -> dict:
    """只收本机设置的字段，按开跑时同样的规则校验（launch_options.normalize），返回规范化后的值。"""
    from .launch_options import DEFAULTS, normalize
    if not isinstance(values, dict):
        raise ValueError("本机设置须为对象")
    extra = set(values) - set(MACHINE_KEYS)
    if extra:
        raise ValueError("本机设置的字段无效：" + "、".join(sorted(extra)))
    merged = {**defaults(), **values}
    checked = normalize({"agent": "machine", **{k: v for k, v in merged.items() if k in DEFAULTS}})
    return {key: checked[key] for key in MACHINE_KEYS}


def _latest_run_state() -> dict:
    root = _config.REPO_ROOT / "runs"
    states = sorted(root.glob("*/*/run_state.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
    return _read(states[0]) if states else {}


def _seed() -> dict:
    """第一次：当前选中配置的表单 > 本机最近一次运行 > 默认值，逐项取（取到的值不合规就跳过）。"""
    from . import profiles
    try:
        form = profiles.meta(profiles.selected()).get("form") or {}
    except (OSError, ValueError):
        form = {}
    state = _latest_run_state()
    seeded = {}
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
            try:
                return validate({k: v for k, v in stored.items() if k in MACHINE_KEYS})
            except ValueError:
                pass                       # 手改坏了：按默认值，下次保存时覆盖
            return defaults()
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
        if not entry["usage"]:
            entry.pop("usage")
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


def strip(form: dict) -> dict:
    """配置的表单里去掉本机设置与用量设置（它们不再随配置保存）。"""
    return {k: v for k, v in form.items() if k not in MACHINE_KEYS and k not in USAGE_KEYS}
