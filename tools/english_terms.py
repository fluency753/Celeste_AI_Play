"""把知识库与提示词里的技巧 / 机制专有名词换成英文（社区通用名），其余中文不动。

用法（仓库根目录）：python tools/english_terms.py kb/*.md orchestrator/prompts/*.md [--check]
只改 kb/ 下项目自己写的文档与提示词；kb/reference/ 里的社区原文保持原样，不要用它处理。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 中文名 → 英文名。按长度从长到短替换，避免短词先吃掉长词的一部分。
GLOSSARY: dict[str, str] = {
    # 墙
    "墙弹引申": "wallbounce variants", "带刺墙弹": "spiked wallbounce", "墙弹取消": "wallbounce cancel",
    "墙弹": "wallbounce", "蹭墙跳": "wallbounce", "弹墙跳": "wallbounce",
    "超级蹬墙跳": "super walljump", "超级蹬墙": "super walljump", "蹬墙跳": "walljump", "蹬墙": "walljump", "蹲姿爬墙跳": "crouch climbjump", "爬墙跳": "climbjump", "抓墙跳": "climbjump",
    "攀爬跳": "climbjump", "翻墙": "climb hop", "贴墙下滑": "wallslide",
    "中性跳": "neutral jump", "无方向蹬墙": "neutral jump",
    "偷体力抓跳": "wallboost", "超频": "overclock", "无体力攀爬": "no-stamina climb",
    "半体力攀爬": "half-stamina climb", "体力管理": "stamina management",
    "踩刺边跳": "corner jump", "五跳": "5-jump", "六跳": "6-jump", "悬垂越过": "overhangs",
    # 冲刺
    "超冲": "hyper", "超级跳": "super", "大跳": "super", "空中 super": "midair super", "延长 super": "extended super",
    "凌波微步": "wavedash", "反凌波": "reverse wavedash", "凌波": "wavedash",
    "究极冲刺": "ultra", "连续 Ultra": "chained ultra", "连 u": "chained ultra", "平地 Ultra": "grounded ultra",
    "平地 ultra": "grounded ultra", "平地 u": "grounded ultra", "延迟 Ultra": "delayed ultra",
    "下蹲冲刺": "crouch dash", "蹲冲": "crouch dash", "反向技巧": "reverse tech",
    "冲刺取消": "dash cancel", "冲刺中断": "dash cancel", "冲刺攻击": "dash attack", "冲刺状态保留": "dash attack",
    "冲刺冷却": "dash cooldown", "冲刺间隔": "dash spacing",
    "失败 hyper": "failed hyper", "失败 wavedash": "failed wavedash",
    # 跳
    "连续兔子跳": "chained bunnyhop", "兔子跳": "bunnyhop", "跳跃加速": "jump boost",
    "跳跃高度控制": "jump control", "浮空跳": "float jump", "蹲跳": "crouch jump",
    "土狼时间": "coyote time", "土狼跳": "coyote jump", "狼跳": "coyote time", "土狼": "coyote",
    "可变跳跃高度": "variable jump height", "可变跳跃": "variable jump", "可变跳": "variable jump",
    "快速下落": "fastfall", "速降": "fastfall",
    "预输入": "input buffering", "输入缓冲": "input buffer", "按键缓冲": "input buffer", "跳跃缓冲": "jump buffer",
    # 角、碰撞、平台
    "墙角修正": "corner correction", "抓角加速": "cornerboost", "咖啡跳": "coffee jump", "墙角踢": "cornerkick",
    "贴刺跳": "spike jump", "磕顶跳": "ceiling pop", "碰撞距离容错": "collision leniency", "碰撞容错": "collision leniency",
    "多重机关加速": "multiboost", "延迟机关加速": "delayed liftboost", "机关加速": "liftboost", "平台速度": "liftboost",
    "撞墙速度保留": "wall speed retention", "速度保留": "speed retention",
    "羽毛加速": "feather boost", "换房技巧": "screen transition tech", "方块更新技巧": "reform tech", "梦境块技巧": "dream block tech",
    # 规则
    "冻结帧": "freeze frames", "亚像素": "subpixel",
}
SPECIAL = [(re.compile(r"(?<!摩)擦地"), "cornerslip")]   # "低摩擦地面"里的"擦地"不是技巧名
NICKNAMES = ["\"牛抽奖\"", "“牛抽奖”", "牛抽奖"]          # 只在别名列表里出现的戏称，直接去掉

MARK = "\x00"
CJK = re.compile(r"[一-鿿]")
WORDISH = re.compile(r"[A-Za-z0-9]")


def _space(text: str) -> str:
    """英文名与相邻的汉字、字母之间留一个空格；挨着标点、括号、空白时不加。"""
    out = []
    parts = text.split(MARK)
    for i, part in enumerate(parts):
        if i % 2 == 1:  # 英文名
            prev = out[-1][-1:] if out and out[-1] else ""
            nxt = parts[i + 1][:1] if i + 1 < len(parts) else ""
            lead = " " if prev and (CJK.match(prev) or WORDISH.match(prev)) else ""
            tail = " " if nxt and (CJK.match(nxt) or WORDISH.match(nxt)) else ""
            out.append(lead + part + tail)
        else:
            out.append(part)
    return "".join(out)


def _dedupe(text: str) -> str:
    """替换后会出现"wallbounce（Wallbounce，WB，wallbounce）"这样的重复：括号里去掉与前面同名的别名；
    括号整个去掉后，英文名与后面的汉字之间补一个空格。"""
    def fix(m: re.Match) -> str:
        head, inner, nxt = m.group(1), m.group(2), m.group(3)
        base = head.strip().lower()
        seen, kept = set(), []
        for item in re.split(r"[，,]", inner):
            key = item.strip().lower()
            if not key or key in seen or base.endswith(key):
                continue
            seen.add(key)
            kept.append(item.strip())
        if kept:
            return head + "（" + "，".join(kept) + "）" + nxt
        return head + (" " if CJK.match(nxt) else "") + nxt
    return re.sub(r"([A-Za-z0-9\- ]+)（([^（）\n]*)）(.?)", fix, text)


MULTI_SPACE = re.compile(r"(?<=\S)  +(?=\S)")
HEADING = re.compile(r"^(#{1,6} (?:[\dA-Z]+(?:\.\d+)*\.? )?)([a-z])", re.M)


def _capitalize_headings(text: str) -> str:
    """标题里打头的英文技巧名首字母大写（正文保持小写）；别的英文开头（如 celeste.ink）不动。"""
    names = sorted(set(GLOSSARY.values()) | {"cornerslip"}, key=len, reverse=True)

    def fix(m: re.Match) -> str:
        rest = text[m.end(2) - 1:m.end(2) + 40]
        if any(rest.startswith(name) for name in names):
            return m.group(1) + m.group(2).upper()
        return m.group(0)
    return HEADING.sub(fix, text)


def _drop_echo(text: str) -> str:
    """表格里原来是"中文名 English Name"，替换后成了"cornerboost Corner Boost"：去掉紧跟着的同名英文。"""
    norm = lambda s: s.lower().replace(" ", "").replace("-", "")
    for en in sorted(set(GLOSSARY.values()), key=len, reverse=True):
        pattern = re.compile(re.escape(en) + r" ([A-Z][A-Za-z\-]*(?: [A-Z][A-Za-z\-]*)*)")
        text = pattern.sub(lambda m: en if norm(m.group(1)).rstrip("s") == norm(en).rstrip("s") else m.group(0), text)
    return text


def convert(text: str) -> str:
    original = text
    for nick in NICKNAMES:
        text = text.replace("，" + nick, "").replace(nick, "")
    for zh in sorted(GLOSSARY, key=len, reverse=True):
        text = text.replace(zh, MARK + GLOSSARY[zh] + MARK)
    for pattern, en in SPECIAL:
        text = pattern.sub(MARK + en + MARK, text)
    text = _space(text)
    # 替换在词边上补空格时可能多出连续空格：只在原来没有连续空格的行里合并（代码块里对齐用的空格不动）
    old_lines, new_lines = original.splitlines(keepends=True), text.splitlines(keepends=True)
    if len(old_lines) == len(new_lines):
        text = "".join(n if MULTI_SPACE.search(o) else MULTI_SPACE.sub(" ", n) for o, n in zip(old_lines, new_lines))
    return _capitalize_headings(_drop_echo(_dedupe(text)))


def main() -> int:
    check = "--check" in sys.argv
    names = []
    for arg in [a for a in sys.argv[1:] if not a.startswith("--")]:
        # PowerShell 不展开通配符：自己展开
        names += sorted(str(p) for p in Path().glob(arg.replace("\\", "/"))) if any(c in arg for c in "*?[") else [arg]
    for name in names:
        path = Path(name)
        if "reference" in path.parts:
            print(f"跳过社区原文：{path}")
            continue
        old = path.read_text(encoding="utf-8")
        new = convert(old)
        left = [zh for zh in GLOSSARY if zh in new]
        print(f"{path}: {'有改动' if new != old else '无改动'}" + (f"；仍有 {left}" if left else ""))
        if not check and new != old:
            path.write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
