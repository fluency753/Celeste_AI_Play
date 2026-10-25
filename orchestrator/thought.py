"""思考语句（act 的 thought 与 narrate）的长度要求。

- 两种执行器都有总长度上限 thought_max_chars（默认 120 字，超过就退回）；
- Claude Code 执行器另限汉字数 thought_max_hanzi（默认 30 个；英文术语、数字与标点不计），写进提示词与工具说明，
  超过也退回，让智能体压缩后再提交（用户要求）。Codex 执行器不限汉字数，提示词与工具说明照旧。
"""
from __future__ import annotations


def count_hanzi(text: str) -> int:
    """汉字个数（CJK 统一表意文字及扩展区、兼容表意文字）；英文、数字与标点不计。"""
    n = 0
    for ch in text or "":
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF or 0xF900 <= o <= 0xFAFF or 0x20000 <= o <= 0x3134F:
            n += 1
    return n


def hanzi_limit(cfg) -> int | None:
    """Claude Code 执行器的汉字数上限；Codex 执行器为 None（不限）。"""
    if getattr(cfg, "engine", "claude") == "codex":
        return None
    return getattr(cfg, "thought_max_hanzi", 30)


def thought_problem(cfg, text: str) -> str | None:
    """思考语句超长时退回给智能体的说明（不带句号）；没问题时为 None。"""
    cap = getattr(cfg, "thought_max_chars", 120)
    if len(text) > cap:
        return f"thought 太长，请压缩到一两句话（{cap} 字以内）"
    limit = hanzi_limit(cfg)
    if limit is not None:
        n = count_hanzi(text)
        if n > limit:
            return f"thought 太长：有 {n} 个汉字，请压缩到 {limit} 个以内（英文术语不计）"
    return None
