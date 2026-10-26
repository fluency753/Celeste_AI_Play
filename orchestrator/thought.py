"""思考语句（act 的 thought 与 narrate）的长度要求。

- 两种执行器都有总长度上限 thought_max_chars（默认 120 字，超过就退回）；
- Claude Code 执行器另限汉字数 thought_max_hanzi（默认 30 个；英文术语、数字与标点不计），写进提示词与工具说明，
  超过也退回，让智能体压缩后再提交（用户要求）。Codex 执行器不限汉字数，提示词与工具说明照旧。
- 开了"仅正式思考"（--formal-thought）时，narrate 写的是临时思考（只进控制台）：Claude Code 执行器的汉字数上限改用
  thought_temp_max_hanzi（默认 120 个），总长度上限放宽到它的 3 倍（不少于 thought_max_chars）；act 的 thought 是正式
  思考，规则不变。Codex 执行器照旧。
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


def temp_thoughts(cfg) -> bool:
    """开了"仅正式思考"：narrate 写的是临时思考。"""
    return bool(getattr(cfg, "formal_thought", False))


def hanzi_limit(cfg, temp: bool = False) -> int | None:
    """Claude Code 执行器的汉字数上限；Codex 执行器为 None（不限）。temp：开了"仅正式思考"时 narrate 的临时思考。"""
    if getattr(cfg, "engine", "claude") == "codex":
        return None
    if temp and temp_thoughts(cfg):
        return getattr(cfg, "thought_temp_max_hanzi", 120)
    return getattr(cfg, "thought_max_hanzi", 30)


def char_cap(cfg, temp: bool = False) -> int:
    """总长度上限：临时思考（Claude Code 执行器）放宽到汉字上限的 3 倍，免得写满汉字之前先撞上总长度。"""
    cap = getattr(cfg, "thought_max_chars", 120)
    limit = hanzi_limit(cfg, temp)
    if temp and temp_thoughts(cfg) and limit is not None:
        cap = max(cap, 3 * limit)
    return cap


def thought_problem(cfg, text: str, temp: bool = False) -> str | None:
    """思考语句超长时退回给智能体的说明（不带句号）；没问题时为 None。temp：narrate 的临时思考（开了"仅正式思考"时）。"""
    name = "临时思考" if temp and temp_thoughts(cfg) else "thought"
    cap = char_cap(cfg, temp)
    if len(text) > cap:
        return f"{name} 太长，请压缩（{cap} 字以内）" if name != "thought" else f"thought 太长，请压缩到一两句话（{cap} 字以内）"
    limit = hanzi_limit(cfg, temp)
    if limit is not None:
        n = count_hanzi(text)
        if n > limit:
            return f"{name} 太长：有 {n} 个汉字，请压缩到 {limit} 个以内（英文术语不计）"
    return None
