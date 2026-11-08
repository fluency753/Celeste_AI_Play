"""模型档案：与具体模型相关的设置（docs/开发/设置分层.md 第一步）。

- Claude 执行器：configs/providers.json（本机覆盖 configs/local.providers.json）各提供方 models[] 里
  与所选模型同 id 的那一行。
- Codex：configs/codex.json（本机覆盖 configs/local.codex.json）。模型列表与推理强度以 Codex 的模型目录为准，
  这里的 models[] 按模型 ID 叠加在目录上。

模型行里可选的字段：
- label：控制台下拉框与 HUD 的显示名；
- autocompact：这个模型的自动压缩窗口（Claude 执行器）：auto，或 100000～1000000 的 token 数；
- context_limit：HUD 上下文警示与笔记提醒的上限（token），与执行器报告的窗口、实际压缩窗口一起取最小的；
- thought：思考语句的汉字数上限 {"max_hanzi": …, "temp_max_hanzi": …}（见 thought.hanzi_limit）；
- images：观测里附不附截图（true／false），优先于提供方的 images。

优先顺序：本次运行填写 > 模型档案 > 提供方 > 开关带来的默认值 > 内置默认值（见 config.autocompact_choice）。
"""
from __future__ import annotations

import copy
import re

from . import config as _config

# configs/codex.json 缺失或读不出时用的值（与原来写在 runtimes.py 里的相同）
CODEX_DEFAULTS = {"label": "Codex", "images": True, "default_effort": "medium",
                  "allowed_domains": ["chatgpt.com", "auth.openai.com", "api.openai.com"], "models": []}
DOMAIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+")


def codex_paths():
    root = _config.REPO_ROOT / "configs"
    return root / "codex.json", root / "local.codex.json"


def codex_settings() -> dict:
    """Codex 的提供方信息：入库的 configs/codex.json，被本机 configs/local.codex.json 按顶层字段覆盖。
    读不出的文件跳过；类型不对的字段用内置值，免得手改坏一处就开不了跑。"""
    from .session_store import load_json
    merged: dict = {}
    for path in codex_paths():
        try:
            data = load_json(path, encoding="utf-8-sig")
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            merged.update({k: v for k, v in data.items() if not k.startswith("_")})
    out = copy.deepcopy(CODEX_DEFAULTS)
    if isinstance(merged.get("label"), str) and merged["label"].strip():
        out["label"] = merged["label"].strip()[:60]
    if isinstance(merged.get("images"), bool):
        out["images"] = merged["images"]
    if isinstance(merged.get("default_effort"), str) and merged["default_effort"].strip():
        out["default_effort"] = merged["default_effort"].strip()
    domains = merged.get("allowed_domains")
    if isinstance(domains, list) and domains and all(isinstance(d, str) and DOMAIN.fullmatch(d) for d in domains):
        out["allowed_domains"] = list(dict.fromkeys(domains))
    if isinstance(merged.get("models"), list):
        out["models"] = [row for row in merged["models"] if isinstance(row, dict) and isinstance(row.get("id"), str)]
    return out


def model_row(cfg, model: str | None = None) -> dict:
    """所选模型（或指定的 model）在模型档案里的那一行；没有时为空。"""
    resolved = getattr(cfg, '_resolved_profile_model', None)
    ident = model if model is not None else resolved or getattr(cfg, "model", None)
    rows = (getattr(cfg, "provider_info", None) or {}).get("models") or []
    row = next((row for row in rows if isinstance(row, dict) and row.get("id") == ident), None)
    if row is None and (model is None or model == resolved):
        row = next((row for row in rows if isinstance(row, dict) and row.get('id') == getattr(cfg, 'model', None)), None)
    return row or {}


def valid_autocompact(value) -> str | None:
    """auto，或 100000～1000000 的 token 数；别的写法（包括空）返回 None。"""
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == "auto" or (text.isdigit() and 100000 <= int(text) <= 1000000):
        return text
    return None


def positive_tokens(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return None
    return int(value) if 1000 <= value <= 100_000_000 else None


def model_autocompact(cfg) -> str | None:
    return valid_autocompact(model_row(cfg).get("autocompact"))


def model_label(cfg, model: str | None = None, fallback: str | None = None) -> str | None:
    label = model_row(cfg, model).get("label")
    return label.strip()[:60] if isinstance(label, str) and label.strip() else fallback


def model_context_limit(cfg, model: str | None = None) -> int | None:
    if entry := saved_choice(cfg, 'context_limit', model):
        return entry['value']
    return positive_tokens(model_row(cfg, model).get("context_limit"))


THOUGHT_KEYS = {"max_hanzi": "thought_max_hanzi", "temp_max_hanzi": "thought_temp_max_hanzi"}


def row_thought(row: dict) -> dict:
    """一个模型行的思考字数上限：{"max_hanzi": 汉字数或 None, "temp_max_hanzi": 汉字数或 None}。
    范围与同名可调参数相同（5～400、10～400），只认整数，别的写法不用。"""
    from .tuning import coerce
    thought = row.get("thought") if isinstance(row, dict) else None
    thought = thought if isinstance(thought, dict) else {}
    out = {}
    for name, key in THOUGHT_KEYS.items():
        value = thought.get(name)
        try:
            out[name] = coerce(key, value) if isinstance(value, int) and not isinstance(value, bool) else None
        except ValueError:
            out[name] = None
    return out


def model_thought(cfg) -> dict:
    """所选模型的思考字数上限（见 row_thought）。"""
    return row_thought(model_row(cfg))


def model_images(cfg) -> bool | None:
    if entry := saved_choice(cfg, 'images'):
        return entry['value']
    value = model_row(cfg).get("images")
    return value if isinstance(value, bool) else None


def saved_choice(cfg, key: str, model: str | None = None) -> dict | None:
    """同一模型恢复时使用已生效的非敏感设置；不冻结账户或连接配置。"""
    snapshot = getattr(cfg, '_saved_model_profile', None) or {}
    if not snapshot:
        return None
    identity = snapshot.get('identity') or {}
    aliases = {identity.get('requested_model'), identity.get('resolved_model')}
    if (identity.get('engine') != cfg.engine or identity.get('provider') != cfg.provider
            or cfg.model not in aliases or (model is not None and model not in aliases)):
        return None
    resolved = getattr(cfg, '_resolved_profile_model', None)
    if resolved and identity.get('resolved_model') and resolved != identity['resolved_model']:
        return None
    return (snapshot.get('values') or {}).get(key)


def restore_profile(cfg, state: dict, *, explicit_tunes=(), explicit_autocompact=False, changed_save_context=False):
    """旧记录缺少的字段继续解析当前默认；已有有效快照不随配置文件漂移。"""
    fields = state.get('model_profile')
    if not isinstance(fields, dict):
        return
    identity = state.get('model_profile_identity')
    if not isinstance(identity, dict):
        identity = {
            'engine': state.get('engine', 'claude'), 'provider': state.get('provider', 'anthropic'),
            'requested_model': state.get('requested_model') or state.get('model'), 'resolved_model': state.get('model')}
    values = {}
    from .tuning import coerce
    for key in ('images', 'context_limit', 'autocompact', *THOUGHT_KEYS.values()):
        entry = fields.get(key)
        if not isinstance(entry, dict) or 'value' not in entry:
            continue
        if (key in explicit_tunes or (key == 'autocompact' and
                (explicit_autocompact or (changed_save_context and entry.get('source') in {'default', 'save_context'})))):
            continue
        value = entry['value']
        valid = False
        if key == 'images':
            valid = isinstance(value, bool)
        elif key == 'context_limit':
            valid = value is None or positive_tokens(value) is not None
        elif key == 'autocompact':
            valid = value is None or valid_autocompact(value) is not None
        elif value is None:
            valid = cfg.engine == 'codex'
        else:
            try:
                valid = coerce(key, value) == value
            except ValueError:
                pass
        if valid:
            source = entry.get('source')
            values[key] = {'value': value, 'source': source if source in {'run', 'model', 'provider', 'save_context', 'default'} else 'default'}
    cfg._saved_model_profile = {'identity': dict(identity), 'values': values}


def profile_state(cfg, *, resolved_model: str | None = None) -> dict:
    """只保存有效值、来源和模型身份；不包含凭据、地址或账户额度。"""
    if resolved_model:
        cfg._resolved_profile_model = resolved_model
    identity = {'engine': cfg.engine, 'provider': cfg.provider, 'requested_model': cfg.model,
                'resolved_model': getattr(cfg, '_resolved_profile_model', None) or cfg.model}
    return {'model_profile': profile_sources(cfg), 'model_profile_identity': identity}


def profile_sources(cfg) -> dict:
    """与模型相关、按层合并后的值和来源，记进 run_state.json 的 model_profile（复盘时看出值是这次填的、
    跟模型来的还是默认的）。来源：run 本次运行（表单、命令行或可调参数）、model 模型档案、provider 提供方、
    save_context 节省上下文带来的默认值、default 内置默认值；Codex 不适用的项不记。"""
    from .config import autocompact_choice
    from .thought import hanzi_limit, temp_thoughts
    codex = getattr(cfg, "engine", "claude") == "codex"
    out = {}
    if not codex:
        value, source = autocompact_choice(cfg)
        out["autocompact"] = {"value": value, "source": source or "default"}
    thought = model_thought(cfg)
    tuned = getattr(cfg, "tuning", None) or {}
    keys = [("thought_max_hanzi", "max_hanzi", False)]
    if temp_thoughts(cfg):
        keys.append(("thought_temp_max_hanzi", "temp_max_hanzi", True))
    for key, row_key, temp in keys:
        if codex:
            source = "model" if thought[row_key] is not None else "default"
        else:
            source = "run" if key in tuned else "model" if thought[row_key] is not None else "default"
        out[key] = {"value": hanzi_limit(cfg, temp=temp), "source": source}
    out["images"] = {"value": bool(cfg.images), "source": "model" if model_images(cfg) is not None else "provider"}
    out["context_limit"] = {"value": model_context_limit(cfg), "source": "model" if model_context_limit(cfg) else "default"}
    for key in out:
        if key in tuned or (key == 'autocompact' and cfg.autocompact):
            continue
        if entry := saved_choice(cfg, key):
            out[key] = dict(entry)
    return out


def hud_fields(cfg, model: str | None = None, catalog_label: str | None = None) -> dict:
    """写进统计（metrics.json）给 HUD 用的模型档案：显示名、上下文上限、实际传给 Claude Code 的压缩窗口。"""
    from .config import compact_budget
    return {"model_label": model_label(cfg, model, catalog_label),
            "context_limit": model_context_limit(cfg, model),
            "compact_window": compact_budget(cfg) if getattr(cfg, "engine", "claude") == "claude" else None}
