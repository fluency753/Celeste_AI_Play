"""模型档案：与具体模型相关的设置（docs/开发/设置分层.md 第一步）。

- Claude 执行器：configs/providers.json（本机覆盖 configs/local.providers.json）各提供方 models[] 里
  与所选模型同 id 的那一行。
- Codex：configs/codex.json（本机覆盖 configs/local.codex.json）。模型列表与推理强度以 Codex 的模型目录为准，
  这里的 models[] 按模型 ID 叠加在目录上。

模型行里可选的字段：
- label：控制台下拉框与 HUD 的显示名；
- autocompact：这个模型的自动压缩窗口（Claude 执行器）：auto，或 100000～1000000 的 token 数；
- context_limit：HUD 上下文警示与笔记提醒的上限（token），与执行器报告的窗口、实际压缩窗口一起取最小的。

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
    ident = model if model is not None else getattr(cfg, "model", None)
    rows = (getattr(cfg, "provider_info", None) or {}).get("models") or []
    return next((row for row in rows if isinstance(row, dict) and row.get("id") == ident), {})


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
    return positive_tokens(model_row(cfg, model).get("context_limit"))


def hud_fields(cfg, model: str | None = None, catalog_label: str | None = None) -> dict:
    """写进统计（metrics.json）给 HUD 用的模型档案：显示名、上下文上限、实际传给 Claude Code 的压缩窗口。"""
    from .config import compact_budget
    return {"model_label": model_label(cfg, model, catalog_label),
            "context_limit": model_context_limit(cfg, model),
            "compact_window": compact_budget(cfg) if getattr(cfg, "engine", "claude") == "claude" else None}
