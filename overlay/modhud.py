"""HUD 的 MOD 形式：主控把旧版 HUD 卡片（加上思考语句历史）画成图片，桥接 Mod 把它画在游戏画面右上角。

开关：`--hud-mod`（不开直播时默认开）/ `--no-hud-mod`（直播模式默认，用可交互的网页 HUD）。开着时不打开置顶 HUD 窗口，
OBS 浏览器源里的网页 HUD 也不显示（录像里只有游戏画面里的这一份）。

分工：
- 这里（主控进程里的一个线程）每 0.1 秒读一次统计（metrics）、公开解说历史与 HUD 配置，显示的内容变了就用 Pillow
  画一张卡片（版式按 2026-10-24 之前的旧版 HUD：大号章节计时、推理强度、统计两列、调用工具；思考语句一栏改成新版那样
  保留最近几条历史，最新一条在最下面高亮），连同章节计时用的数字字形表写进共享内存 `Local\\CelesteAIHud-<桥接端口>`。
- 桥接（game-bridge/Source/ModHud.cs）在每帧画完游戏之后把卡片贴在游戏显示区的右上角，章节计时按当帧的游戏时间
  用字形表拼出来，逐帧准确；智能体的观测截图取的是游戏自己的画面缓冲，不含 HUD。
- 现实时间只取本机时钟（time.time()），显示在解说历史的时间标签里，不进入智能体的观测。

共享内存布局（小端）见 HEADER_* 常量；图片与字形表是预乘 alpha 的 RGBA。卡片图片用 img_seq 做顺序锁（写之前置奇数、
写完置偶数），计时字段单独写（每帧读，偶尔读到一半没有关系）。主控每 0.1 秒写一次心跳，桥接 5 秒收不到心跳就隐藏 HUD。
"""
from __future__ import annotations

import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

MAGIC = 0x48494143          # "CAIH"
LAYOUT_VERSION = 1
HEADER_SIZE = 4096
IMAGE_MAX = 6 * 1024 * 1024
ATLAS_MAX = 1024 * 1024
MAPPING_SIZE = HEADER_SIZE + IMAGE_MAX + ATLAS_MAX
MAX_GLYPHS = 32
# 头部字段的偏移
OFF_MAGIC, OFF_VERSION, OFF_SEQ, OFF_ENABLED = 0, 4, 8, 16
OFF_IMG_W, OFF_IMG_H, OFF_ATLAS_W, OFF_ATLAS_H = 20, 24, 28, 32
OFF_TIMER_X, OFF_TIMER_Y, OFF_TIMER_LIVE = 36, 40, 44
OFF_TIMER_BASE, OFF_TIMER_EXTRA = 48, 56
OFF_GLYPHS_N, OFF_MARGIN_RIGHT, OFF_MARGIN_TOP = 64, 68, 72
OFF_GLYPHS = 80
OFF_HEARTBEAT, OFF_VIEW_W, OFF_VIEW_H = 2048, 2056, 2060
# 桥接写的状态（控制台读，不用连桥接）：心跳、接管、锁步、帧号、OBS 插件、录像音频、版本、场景
OFF_BRIDGE_BEAT, OFF_BRIDGE_ACTIVE, OFF_BRIDGE_LOCKSTEP, OFF_BRIDGE_FRAME = 2064, 2072, 2076, 2080
OFF_BRIDGE_CLOCK, OFF_BRIDGE_RECORDING, OFF_BRIDGE_VERSION, OFF_BRIDGE_SCENE = 2088, 2092, 2096, 2128

DESIGN_WIDTH = 350            # 旧版设计稿：1080p 下卡片页面宽 350 像素（游戏宽 1920 的约 18%）
DESIGN_VIEW = 1920
HISTORY_MAX = 260             # 思考语句历史一栏最高（设计像素）；放不下时最早的几条从上边裁掉
TEXT_SCALE = 1.15             # 文字相对旧版设计稿放大的倍数（章节计时那一行除外）；卡片宽度与间距不变
TIMER_CHARS = "0123456789:."

STATUS = {
    "starting": ("等待", "idle"), "studying": ("读知识库", "studying"), "thinking": ("思考中", "thinking"),
    "tool": ("运行工具", "tool"), "compacting": ("压缩上下文", "thinking"), "executing": ("执行中", "executing"),
    "auto": ("自动推进", "idle"), "retrying": ("自动重试", "retrying"), "replaying": ("规划回放", "replay"),
    "quota_wait": ("等待额度重置", "idle"), "done": ("已完成", "idle"), "stopped": ("已结束", "idle"),
}
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_COLORS = {
    "card": "rgba(16, 19, 34, 0.80)", "border": "rgba(255, 255, 255, 0.10)", "text": "#F3F5FA",
    "label": "#8F9AB3", "warn": "#F08A3C", "dash0": "#5BC0EB", "dash1": "#E0475B", "dash2": "#F28AC4",
    "effort": "#7FA6F5", "effort_high": "#F5B841", "thinking": "#F5B841", "idle": "#8A93A6",
}


# ---------------------------------------------------------------- 颜色与字体

def parse_color(text: str, fallback=(255, 255, 255, 255)) -> tuple[int, int, int, int]:
    text = (text or "").strip()
    try:
        if text.startswith("#"):
            h = text[1:]
            if len(h) in (3, 4):
                h = "".join(c * 2 for c in h)
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            a = int(h[6:8], 16) if len(h) == 8 else 255
            return r, g, b, a
        if text.startswith("rgb"):
            parts = [p.strip() for p in text[text.index("(") + 1:text.rindex(")")].split(",")]
            r, g, b = (int(float(p)) for p in parts[:3])
            a = int(round(float(parts[3]) * 255)) if len(parts) > 3 else 255
            return r, g, b, a
    except (ValueError, IndexError):
        pass
    return fallback


def _font_dirs() -> list[Path]:
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
    if sys.platform != "win32":
        dirs += [Path("/usr/share/fonts/opentype/noto"), Path("/usr/share/fonts/truetype/dejavu"),
                 Path("/usr/share/fonts/truetype/noto"), Path("/usr/share/fonts/truetype/liberation")]
    return dirs


# 角色 → 候选（文件名, ttc 索引, 可变字体实例名）
FONT_CANDIDATES = {
    "cjk": [("msyh.ttc", 1, None), ("NotoSansCJK-Regular.ttc", 2, None), ("NotoSansCJK-Medium.ttc", 2, None),
            ("DejaVuSans.ttf", 0, None)],
    "cjk_bold": [("msyhbd.ttc", 1, None), ("NotoSansCJK-Bold.ttc", 2, None), ("DejaVuSans-Bold.ttf", 0, None)],
    "num": [("bahnschrift.ttf", 0, b"SemiBold SemiCondensed"), ("DejaVuSansCondensed-Bold.ttf", 0, None),
            ("LiberationSansNarrow-Bold.ttf", 0, None), ("DejaVuSans-Bold.ttf", 0, None)],
}
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def font(role: str, size: float) -> ImageFont.FreeTypeFont:
    size = max(6, int(round(size)))
    key = (role, size)
    if key in _font_cache:
        return _font_cache[key]
    loaded = None
    for name, index, variation in FONT_CANDIDATES[role]:
        for folder in _font_dirs():
            path = folder / name
            if not path.exists():
                continue
            try:
                loaded = ImageFont.truetype(str(path), size, index=index)
                if variation:
                    try:
                        loaded.set_variation_by_name(variation)
                    except (OSError, ValueError):
                        pass
                break
            except OSError:
                loaded = None
        if loaded:
            break
    if loaded is None:
        loaded = ImageFont.load_default(size)
    _font_cache[key] = loaded
    return loaded


def _is_ascii(ch: str) -> bool:
    return ord(ch) < 0x2000


def runs(text: str, num: bool) -> list[tuple[str, bool]]:
    """把文字切成（片段, 用数字字体）：数字字体只用于 ASCII，中文用中文字体。"""
    if not num:
        return [(text, False)] if text else []
    out: list[tuple[str, bool]] = []
    for ch in text:
        flag = _is_ascii(ch)
        if out and out[-1][1] == flag:
            out[-1] = (out[-1][0] + ch, flag)
        else:
            out.append((ch, flag))
    return out


@dataclass
class Style:
    size: float
    color: tuple
    bold: bool = False
    num: bool = False

    def fonts(self):
        return font("cjk_bold" if self.bold else "cjk", self.size), font("num", self.size)


def text_width(text: str, style: Style) -> float:
    cjk, num = style.fonts()
    return sum((num if flag else cjk).getlength(part) for part, flag in runs(text, style.num))


def draw_text(draw: ImageDraw.ImageDraw, x: float, baseline: float, text: str, style: Style) -> float:
    cjk, num = style.fonts()
    for part, flag in runs(text, style.num):
        f = num if flag else cjk
        draw.text((x, baseline), part, font=f, fill=style.color, anchor="ls")
        x += f.getlength(part)
    return x


def wrap(text: str, style: Style, width: float) -> list[str]:
    """按宽度折行：中文逐字可断，英文单词尽量不拆，原文的换行保留。"""
    lines: list[str] = []
    for para in str(text).split("\n"):
        tokens: list[str] = []
        word = ""
        for ch in para:
            if ch.isascii() and not ch.isspace():
                word += ch
                continue
            if word:
                tokens.append(word)
                word = ""
            tokens.append(ch)
        if word:
            tokens.append(word)
        line = ""
        for token in tokens:
            if text_width(line + token, style) <= width or not line:
                if text_width(token, style) > width and not line:   # 单个超长的词：逐字拆
                    for ch in token:
                        if text_width(line + ch, style) > width and line:
                            lines.append(line)
                            line = ""
                        line += ch
                    continue
                line += token
            else:
                lines.append(line.rstrip())
                line = token.lstrip()
        lines.append(line.rstrip())
    return lines or [""]


def clamp_lines(lines: list[str], n: int, style: Style, width: float) -> list[str]:
    if len(lines) <= n:
        return lines
    kept = lines[:n]
    last = kept[-1]
    while last and text_width(last + "…", style) > width:
        last = last[:-1]
    kept[-1] = last + "…"
    return kept


# ---------------------------------------------------------------- 显示内容（与旧版 hud.html 的 render() 一致）

def pad(n: float, w: int = 2) -> str:
    return str(int(math.floor(n))).zfill(w)


def clock(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 3600)}:{pad(sec // 60 % 60)}:{pad(sec % 60)}"


def tokens_text(n: float) -> str:
    if not n:
        return "0"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e4:
        return f"{round(n / 1000)}k"
    return f"{n / 1000:.1f}k"


def model_key(model: str) -> str:
    """与网页 HUD 的 modelKey() 相同：去掉 [1m] 之类的后缀、claude- 前缀与版本号。"""
    import re
    m = re.sub(r"\[[^\]]*\]$", "", str(model or "")).strip()
    m = re.sub(r"^claude-", "", m)
    m = re.sub(r"-\d+(?:-\d+)*$", "", m)
    return m


def display_name(m: dict, config: dict) -> str:
    names = config.get("names") or {}
    for key in (m.get("agent"), m.get("model"), model_key(m.get("model", ""))):
        if key and key in names:
            return names[key]
    return m.get("agent") or "—"


@dataclass(frozen=True)
class HistoryEntry:
    tag: str
    text: str
    latest: bool
    role: str = ""


@dataclass(frozen=True)
class ViewModel:
    name: str
    status: str
    dot: str
    room: str
    room_id: str
    theme: str
    effort: str | None
    effort_level: int
    effort_pending: str
    effort_color: str
    stats: tuple
    tool_call: str
    history: tuple
    meta: str
    timer_base: int = field(compare=False, default=0)
    timer_extra: int = field(compare=False, default=0)
    timer_live: bool = field(compare=False, default=False)


def composed_history(items: list[dict], m: dict) -> list[dict]:
    """与网页 HUD 的 composedThoughts() 相同：最新一句思考还没进历史时补在末尾。"""
    items = list(items or [])
    replaying = bool(m.get("replaying")) or m.get("status") == "replaying"
    text = (m.get("replay_label") if replaying and not m.get("live") else m.get("thought")) or ""
    text = text.strip()
    if text and not any(str(i.get("text", "")).strip() == text for i in items[-6:]):
        items.append({"id": "latest", "text": text, "kind": "解说", "wall": m.get("thought_updated_wall")})
    return items[-12:]


def view_model(m: dict, config: dict, history: list[dict], now: float) -> ViewModel:
    colors = {**DEFAULT_COLORS, **(config.get("colors") or {})}
    replaying = bool(m.get("replaying")) or m.get("status") == "replaying"
    text, kind = STATUS["replaying"] if replaying else STATUS.get(m.get("status"), (m.get("status") or "", "idle"))
    playing = (m.get("started_wall") or 0) > 0
    counting = playing or (m.get("kb_progress") is not None and m.get("kb_progress") >= 1)
    ended = m.get("status") in ("done", "stopped")
    if m.get("status") == "studying":
        text = f"{text} {round((m.get('kb_progress') or 0) * 100)}%"
    if m.get("status") == "quota_wait" and m.get("quota_wait_until"):
        t = time.localtime(m["quota_wait_until"])
        text = f"{text}（{t.tm_hour:02d}:{t.tm_min:02d} 恢复）"

    chapters = config.get("chapters") or {}
    chapter = m.get("chapter") or ""
    room = chapters.get(chapter) or chapter.replace("Celeste/", "")

    theme = colors.get("dash1", "#E0475B")
    if (m.get("max_dashes") or 0) > 0 and m.get("dashes") == 0:
        theme = colors.get("dash0", "#5BC0EB")
    elif (m.get("dashes") or 0) >= 2:
        theme = colors.get("dash2", "#F28AC4")

    chapter_done = any(s.get("chapter") == chapter for s in (m.get("splits") or []))

    effort = m.get("effort")
    level = EFFORT_LEVELS.index(effort) if effort in EFFORT_LEVELS else -1
    effort_names = config.get("effort_names") or {}
    pending = m.get("effort_pending")
    pending_text = f"→ {effort_names.get(pending, pending)}" if pending and pending != effort else ""
    effort_color = colors.get("effort_high", "#F5B841") if level >= 3 else colors.get("effort", "#7FA6F5")

    thinking = m.get("thinking_s") or 0
    if counting and not ended and m.get("status") in ("thinking", "compacting") and (m.get("thinking_since_wall") or 0) > 0:
        thinking += max(0.0, now - m["thinking_since_wall"])
    tool = m.get("tool_s") or 0
    if counting and not ended and m.get("status") == "tool" and (m.get("thinking_since_wall") or 0) > 0:
        tool += max(0.0, now - m["thinking_since_wall"])
    if m.get("status") == "retrying" and (m.get("phase_since_wall") or 0) > 0:
        tool += max(0.0, now - m["phase_since_wall"])
    study = m.get("study_s") or 0
    if m.get("status") == "studying" and (m.get("written_wall") or 0) > 0:
        study += max(0.0, now - m["written_wall"])
    studied = study > 0 or m.get("status") == "studying"

    ctx = m.get("context_tokens") or 0
    limit = config.get("compact_threshold") or 967000
    runs_n = m.get("sim_round_rollouts") or 0
    runs_text = (f"{runs_n / 10000:.{0 if runs_n >= 1e6 else 1}f}万" if runs_n >= 10000 else f"{runs_n:,}") + " 条"
    best = m.get("sim_best_frames") or 0
    # （键, 值, 值的颜色种类：""/"warn"/"dim"）
    stats = [
        ("思考", clock(thinking), ""), ("上下文", tokens_text(ctx), "warn" if ctx / limit >= 0.9 else ""),
        ("工具", clock(tool), ""), ("压缩", str(m.get("compactions") or 0), ""),
        ("知识库读取", clock(study) if studied else "—", "" if studied else "dim"), ("死亡", f"{m.get('deaths') or 0} 次", ""),
    ]
    if m.get("sim_stats", True):             # 探索模式关掉限额预测时智能体不能推演，这几栏不显示
        stats += [("模拟器调用", f"{m.get('sim_calls') or 0} 次", ""), ("推演路径", runs_text, ""),
                  ("推演最速", f"{best / 60:.3f}s" if best > 0 else "—", "" if best > 0 else "dim")]
    energy = m.get("sim_energy")
    if energy is not None and m.get("sim_stats", True):   # 探索模式的限额短路线预测：只显示余额 / 上限（规则说明不上 HUD）
        value = f"{energy.get('balance')} / {energy.get('capacity')}" if isinstance(energy, dict) else str(energy)
        stats.append(("推演额度", value, ""))

    call = ""
    if not ended and m.get("tool_call"):
        call = f"{m['tool_call']} — {m['tool_status']}" if m.get("tool_status") else m["tool_call"]

    meta = ""
    if replaying:
        info = (f"第 {m['replay_round']} 轮规划 · 推演 {(m.get('replay_runs') or 0):,} 条路线"
                if (m.get("replay_round") or 0) > 0 else "即将执行的路线")
        meta = f"{info} · {m['replay_label']}" if m.get("live") and m.get("replay_label") else info

    entries = []
    items = composed_history(history, m)
    for i, item in enumerate(items):
        stamp = ""
        if item.get("wall"):
            t = time.localtime(item["wall"])
            stamp = f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d} · "
        entries.append(HistoryEntry(tag=stamp + (item.get("kind") or "解说"), text=str(item.get("text", "")).strip(),
                                    latest=i == len(items) - 1, role=str(item.get("role") or "")))

    return ViewModel(
        name=display_name(m, config), status=text, dot=colors.get(kind, colors.get("idle", "#8F9AB3")),
        room=room, room_id=str(m.get("room") or ""), theme=theme,
        effort=effort_names.get(effort, effort) if level >= 0 else None, effort_level=level,
        effort_pending=pending_text, effort_color=effort_color, stats=tuple(stats), tool_call=call,
        history=tuple(entries), meta=meta,
        timer_base=int(m.get("completed_ticks") or 0), timer_extra=int(m.get("chapter_time_ticks") or 0),
        timer_live=not chapter_done)


# ---------------------------------------------------------------- 画卡片

@dataclass
class Rendered:
    image: Image.Image           # 直通 alpha 的 RGBA
    timer_x: int
    timer_y: int                 # 章节计时的基线


def _rounded(size, radius, fill, outline=None, width=1) -> Image.Image:
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=fill,
                                           outline=outline, width=width)
    return layer


def _with_shadow(layer: Image.Image, px: float, alpha: float = 0.5, dy: float = 1, blur: float = 1) -> Image.Image:
    """CSS 的 text-shadow: 0 1px 2px rgba(0,0,0,.5)：把这一层的 alpha 做成模糊的黑影垫在下面。"""
    a = layer.getchannel("A").point(lambda v: int(v * alpha))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    shadow.putalpha(a)
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(0.5, blur * px)))
    out = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, int(round(dy * px))))
    out.alpha_composite(layer)
    return out


def render(vm: ViewModel, width: int, colors: dict | None = None) -> Rendered:
    colors = {**DEFAULT_COLORS, **(colors or {})}
    px = width / DESIGN_WIDTH
    fs = px * TEXT_SCALE                                     # 文字用的比例
    text_c, label_c = parse_color(colors["text"]), parse_color(colors["label"])
    warn_c = parse_color(colors["warn"])
    rule_c = parse_color(colors["border"])
    margin, pad_l, pad_r, pad_t, pad_b = 4 * px, 21 * px, 16 * px, 13 * px, 14 * px
    left, right = margin + pad_l, width - margin - pad_r
    inner = right - left
    tall = int(1600 * px)
    layer = Image.new("RGBA", (width, tall), (0, 0, 0, 0))     # 文字层（之后加阴影）
    flat = Image.new("RGBA", (width, tall), (0, 0, 0, 0))      # 不加阴影的东西：分隔线、历史气泡
    d, fd = ImageDraw.Draw(layer), ImageDraw.Draw(flat)
    y = margin + pad_t

    # 第一行：名字 · 状态（圆点）· 章节与房间
    lh = 15 * 1.35 * fs
    base = y + lh * 0.5 + 15 * fs * 0.36
    x = draw_text(d, left, base, vm.name, Style(15 * fs, text_c, bold=True)) + 8 * px
    dot = 7 * px
    dot_c = parse_color(vm.dot)
    d.ellipse((x, base - 15 * fs * 0.36 - dot / 2 + 0.5 * px, x + dot, base - 15 * fs * 0.36 + dot / 2 + 0.5 * px), fill=dot_c)
    x += dot + 5 * px
    room_style, rm_style = Style(15 * fs, text_c, bold=True), Style(15 * fs, label_c)
    room_w = text_width(vm.room, room_style) + ((4 * px + text_width(vm.room_id, rm_style)) if vm.room_id else 0)
    status_style = Style(13 * fs, label_c)
    status_room = right - room_w - 8 * px - x
    status = vm.status
    while status and text_width(status, status_style) > status_room:
        status = status[:-1]
    if status != vm.status and status:
        status = status[:-1] + "…"
    draw_text(d, x, base, status, status_style)
    rx = draw_text(d, right - room_w, base, vm.room, room_style)
    if vm.room_id:
        draw_text(d, rx + 4 * px, base, vm.room_id, rm_style)
    y += lh

    # 第二行：章节计时（桥接按当帧的游戏时间画）与推理强度
    y += 4 * px
    timer_h = 36 * 1.05 * px          # 章节计时与推理强度这一行保持原来的大小（放大后两者会挤在一起）
    timer_base = y + timer_h * 0.82
    timer_x, timer_y = left, timer_base
    if vm.effort is not None:
        val_style = Style(18 * px, parse_color(vm.effort_color), num=True)
        pend_style = Style(18 * px, parse_color(colors["thinking"]), num=True)
        val_w = text_width(vm.effort, val_style) + (text_width(vm.effort_pending, pend_style) if vm.effort_pending else 0)
        meter_w = 5 * 3 * px + 4 * 2 * px
        row_base = timer_base - 1 * px
        vx = right - val_w
        ex = draw_text(d, vx, row_base, vm.effort, val_style)
        if vm.effort_pending:
            draw_text(d, ex, row_base, vm.effort_pending, pend_style)
        mx = vx - 6 * px - meter_w
        on_c, off_c = parse_color(vm.effort_color), (255, 255, 255, 41)
        for i, frac in enumerate((0.36, 0.52, 0.68, 0.84, 1.0)):
            h = 12 * px * frac
            bx = mx + i * 5 * px
            d.rounded_rectangle((bx, row_base - h, bx + 3 * px, row_base), radius=max(0, px), fill=on_c if i <= vm.effort_level else off_c)
        lbl = Style(12 * px, label_c)
        draw_text(d, right - text_width("推理强度", lbl), row_base - 18 * 1.2 * px, "推理强度", lbl)
    y += timer_h + 11 * px

    # 分隔线
    def rule(at):
        fd.rectangle((left - 0, at, right, at + max(1, px) - 1), fill=rule_c)
    rule(y)
    y += max(1, px)

    # 统计：两列，最后一行"调用工具"占满一行（最多三行）
    y += 9 * px
    col_gap = 10 * px
    col_w = (inner - col_gap) / 2
    row_h = 16 * 1.3 * fs
    stat_k, stat_v = Style(12.5 * fs, label_c), Style(16 * fs, text_c, num=True)   # 两列放得下最长的键与值
    for i, (k, v, tone) in enumerate(vm.stats):
        cx = left + (i % 2) * (col_w + col_gap)
        ry = y + (i // 2) * (row_h + 3 * px)
        b = ry + row_h * 0.5 + 16 * fs * 0.36
        draw_text(d, cx, b, k, stat_k)
        color = warn_c if tone == "warn" else label_c if tone == "dim" else text_c
        vs = Style(16 * fs, color, num=True)
        draw_text(d, cx + col_w - text_width(v, vs), b, v, vs)
    y += math.ceil(len(vm.stats) / 2) * (row_h + 3 * px)
    call_style = Style(14 * fs, text_c if vm.tool_call else label_c)
    call_text = vm.tool_call or "—"
    k_w = text_width("调用工具", stat_k) + 8 * px
    call_lines = clamp_lines(wrap(call_text, call_style, inner - k_w), 3, call_style, inner - k_w)
    call_lh = 14 * 1.35 * fs
    b = y + max(row_h, call_lh) * 0.5 + 14 * fs * 0.36
    draw_text(d, left, y + row_h * 0.5 + 16 * fs * 0.36, "调用工具", stat_k)
    for j, line in enumerate(call_lines):
        lx = left + k_w if len(call_lines) > 1 else right - text_width(line, call_style)
        draw_text(d, lx, b + j * call_lh, line, call_style)
    y += max(row_h, call_lh * len(call_lines)) + 9 * px
    rule(y)
    y += max(1, px)

    # 思考语句历史：新版那样一条一个气泡，最新的在最下面高亮；放不下时最早的从上边裁掉
    if vm.history:
        y += 10 * px - 7 * px
        hist = _history(vm.history, inner, px, text_c, label_c, rule_c, fs)
        limit = int(HISTORY_MAX * px)
        if hist.height > limit:
            hist = hist.crop((0, hist.height - limit, hist.width, hist.height))
            fade = int(24 * px)                            # 顶边渐隐，看得出上面还有
            mask = hist.getchannel("A")
            ramp = Image.linear_gradient("L").resize((hist.width, fade))
            top = mask.crop((0, 0, hist.width, fade))
            mask.paste(Image.composite(top, Image.new("L", top.size, 0), ramp), (0, 0))
            hist.putalpha(mask)
        flat.alpha_composite(hist, (int(left), int(y)))
        y += hist.height
    if vm.meta:
        meta_style = Style(13 * fs, label_c)
        lines = clamp_lines(wrap(vm.meta, meta_style, inner), 2, meta_style, inner)
        y += 4 * px
        for line in lines:
            draw_text(d, left, y + 13 * 1.4 * fs * 0.5 + 13 * fs * 0.36, line, meta_style)
            y += 13 * 1.4 * fs
    height = int(math.ceil(y + pad_b + margin))

    # 卡片：圆角底、描边、投影，左边的色条跟着 Madeline 的发色
    card_box = (int(margin), int(margin), width - int(margin), height - int(margin))
    cw, ch = card_box[2] - card_box[0], card_box[3] - card_box[1]
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = _rounded((cw, ch), int(12 * px), (0, 0, 0, 82))
    blurred = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    blurred.alpha_composite(shadow, (card_box[0], card_box[1] + int(3 * px)))
    out.alpha_composite(blurred.filter(ImageFilter.GaussianBlur(max(1, 6 * px))))
    out.alpha_composite(_rounded((cw, ch), int(12 * px), parse_color(colors["card"]), rule_c, max(1, int(px))),
                        (card_box[0], card_box[1]))
    ImageDraw.Draw(out).rounded_rectangle(
        (margin + 8 * px, margin + 14 * px, margin + 11 * px, height - margin - 14 * px), radius=2 * px, fill=parse_color(vm.theme))
    out.alpha_composite(flat.crop((0, 0, width, height)))
    out.alpha_composite(_with_shadow(layer.crop((0, 0, width, height)), px))
    return Rendered(out, int(round(timer_x)), int(round(timer_y)))


def _history(entries, width: float, px: float, text_c, label_c, rule_c, fs: float | None = None) -> Image.Image:
    fs = fs or px
    text_style, tag_style = Style(15 * fs, text_c), Style(11 * fs, label_c)
    blocks = []
    pad_x, pad_y, gap = 10 * px, 9 * px, 7 * px
    box_w = width - 3 * px
    for e in entries:
        lines = wrap(e.text, text_style, box_w - 2 * pad_x)
        tag_h, line_h = 11 * 1.5 * fs, 15 * 1.5 * fs
        h = int(pad_y * 2 + tag_h + line_h * len(lines))
        if e.role == "user":
            fill = (91, 145, 230, 61)
        elif e.role == "system":
            fill = (190, 153, 82, 26)
        else:
            fill = (127, 166, 245, 46) if e.latest else (150, 173, 225, 26)
        box = _rounded((int(box_w), h), int(9 * px), fill, rule_c, 1)
        bd = ImageDraw.Draw(box)
        draw_text(bd, pad_x, pad_y + tag_h * 0.5 + 11 * fs * 0.36, e.tag, tag_style)
        for j, line in enumerate(lines):
            draw_text(bd, pad_x, pad_y + tag_h + line_h * j + line_h * 0.5 + 15 * fs * 0.36, line, text_style)
        blocks.append(box)
    total = int(sum(b.height for b in blocks) + gap * (len(blocks) + 1))
    column = Image.new("RGBA", (int(width), max(1, total)), (0, 0, 0, 0))
    y = gap
    for b in blocks:
        column.alpha_composite(b, (0, int(y)))
        y += b.height + gap
    return column


# ---------------------------------------------------------------- 计时数字的字形表

@dataclass
class Atlas:
    image: Image.Image
    glyphs: list[tuple[int, int, int, int, int, int, int, int, int]]   # (字符, 大小档 0/1, ax, ay, aw, ah, dx, dy, advance)


def make_atlas(px: float, colors: dict | None = None) -> Atlas:
    """章节计时 H:MM:SS（36 设计像素）与 .mmm（小一号、颜色淡一点），数字等宽，阴影烘进字形里。"""
    colors = {**DEFAULT_COLORS, **(colors or {})}
    specs = [(0, 36 * px, parse_color(colors["text"])), (1, 36 * 0.62 * px, (201, 208, 222, 255))]
    cells = []
    for cls, size, color in specs:
        f = font("num", size)
        digit_adv = max(f.getlength(c) for c in "0123456789")
        for ch in TIMER_CHARS:
            l, t, r, b = f.getbbox(ch, anchor="ls")
            p = int(math.ceil(3 * px)) + 1
            w, h = int(r - l) + 2 * p, int(b - t) + 2 * p
            adv = digit_adv if ch.isdigit() else f.getlength(ch)
            shift = (digit_adv - f.getlength(ch)) / 2 if ch.isdigit() else 0
            glyph = Image.new("RGBA", (max(1, w), max(1, h)), (0, 0, 0, 0))
            ImageDraw.Draw(glyph).text((p - l, p - t), ch, font=f, fill=color, anchor="ls")
            glyph = _with_shadow(glyph, px)
            cells.append((ord(ch), cls, glyph, int(round(l - p + shift)), int(t - p), int(round(adv))))
    width = sum(c[2].width for c in cells) + len(cells)
    height = max(c[2].height for c in cells)
    atlas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glyphs, x = [], 0
    for code, cls, glyph, dx, dy, adv in cells:
        atlas.alpha_composite(glyph, (x, 0))
        glyphs.append((code, cls, x, 0, glyph.width, glyph.height, dx, dy, adv))
        x += glyph.width + 1
    return Atlas(atlas, glyphs)


def premultiplied(image: Image.Image) -> bytes:
    import numpy as np
    a = np.asarray(image.convert("RGBA"), dtype=np.uint16)
    rgb = (a[..., :3] * a[..., 3:4] + 127) // 255
    return np.concatenate([rgb, a[..., 3:4]], axis=-1).astype(np.uint8).tobytes()


# ---------------------------------------------------------------- 共享内存

class SharedHud:
    """写桥接读的共享内存。Windows 用命名映射；别的系统（测试）用一块内存代替。"""

    def __init__(self, name: str | None = None, buffer=None):
        if buffer is not None:
            self.buf = buffer
        else:
            import mmap
            if sys.platform != "win32":
                self.buf = bytearray(MAPPING_SIZE)
            else:
                self.buf = mmap.mmap(-1, MAPPING_SIZE, tagname=name)
        self.seq = struct.unpack_from("<q", self.buf, OFF_SEQ)[0] & ~1
        struct.pack_into("<II", self.buf, OFF_MAGIC, MAGIC, LAYOUT_VERSION)

    def view_size(self) -> tuple[int, int] | None:
        """桥接报告的游戏显示区大小；还没报告（游戏没在画）时 None。"""
        w, h = struct.unpack_from("<ii", self.buf, OFF_VIEW_W)
        return (w, h) if w > 0 and h > 0 else None

    def publish(self, card: bytes, size: tuple[int, int], atlas: bytes, atlas_size: tuple[int, int],
                glyphs: list, timer_xy: tuple[int, int], margins: tuple[int, int]) -> None:
        if len(card) > IMAGE_MAX or len(atlas) > ATLAS_MAX:
            raise ValueError("HUD 图片太大")
        self.seq += 1
        struct.pack_into("<q", self.buf, OFF_SEQ, self.seq)          # 奇数：正在写
        struct.pack_into("<iiii", self.buf, OFF_IMG_W, size[0], size[1], atlas_size[0], atlas_size[1])
        struct.pack_into("<ii", self.buf, OFF_TIMER_X, *timer_xy)
        n = min(len(glyphs), MAX_GLYPHS)
        struct.pack_into("<iii", self.buf, OFF_GLYPHS_N, n, *margins)
        for i, g in enumerate(glyphs[:n]):
            struct.pack_into("<9i", self.buf, OFF_GLYPHS + i * 36, *g)
        self.buf[HEADER_SIZE:HEADER_SIZE + len(card)] = card
        self.buf[HEADER_SIZE + IMAGE_MAX:HEADER_SIZE + IMAGE_MAX + len(atlas)] = atlas
        self.seq += 1
        struct.pack_into("<q", self.buf, OFF_SEQ, self.seq)          # 偶数：写完
        struct.pack_into("<i", self.buf, OFF_ENABLED, 1)

    def timer(self, base: int, extra: int, live: bool) -> None:
        struct.pack_into("<qq", self.buf, OFF_TIMER_BASE, base, extra)
        struct.pack_into("<i", self.buf, OFF_TIMER_LIVE, 1 if live else 0)

    def heartbeat(self) -> None:
        struct.pack_into("<q", self.buf, OFF_HEARTBEAT, int(time.time() * 1000))

    def disable(self) -> None:
        struct.pack_into("<i", self.buf, OFF_ENABLED, 0)
        struct.pack_into("<q", self.buf, OFF_HEARTBEAT, 0)


def mapping_name(bridge_port: int) -> str:
    return f"Local\\CelesteAIHud-{bridge_port}"


_status_maps: dict[int, object] = {}


def bridge_status(bridge_port: int = 47774, buffer=None) -> dict:
    """桥接写在共享内存里的状态。桥接（0.13.1 起）每 0.25 秒写一次；3 秒没更新就当游戏没在运行。
    只读不写：控制台用它看游戏状态，不连桥接（桥接只接受一个连接，连上会顶掉主控）。"""
    if buffer is None:
        if sys.platform != "win32":
            return {"running": False, "reason": "只在 Windows 上读取"}
        if bridge_port not in _status_maps:
            import mmap
            _status_maps[bridge_port] = mmap.mmap(-1, MAPPING_SIZE, tagname=mapping_name(bridge_port))
        buffer = _status_maps[bridge_port]
    beat = struct.unpack_from("<q", buffer, OFF_BRIDGE_BEAT)[0]
    if beat <= 0:
        return {"running": False, "reason": "没有收到桥接的状态（游戏没开，或桥接早于 0.13.1）"}
    age = time.time() - beat / 1000
    text = lambda off, n: bytes(buffer[off:off + n]).split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return {"running": age < 3, "age_s": round(age, 1),
            "active": struct.unpack_from("<i", buffer, OFF_BRIDGE_ACTIVE)[0] == 1,
            "lockstep": struct.unpack_from("<i", buffer, OFF_BRIDGE_LOCKSTEP)[0] == 1,
            "frame": struct.unpack_from("<q", buffer, OFF_BRIDGE_FRAME)[0],
            "obs_plugin": struct.unpack_from("<i", buffer, OFF_BRIDGE_CLOCK)[0] == 1,
            "recording_audio": struct.unpack_from("<i", buffer, OFF_BRIDGE_RECORDING)[0] == 1,
            "version": text(OFF_BRIDGE_VERSION, 32), "scene": text(OFF_BRIDGE_SCENE, 64)}


# ---------------------------------------------------------------- 主控里的线程

class ModHud:
    """每 0.1 秒看一次要显示的内容，变了就重画卡片发给桥接；章节计时字段与心跳每次都写。"""

    def __init__(self, run_dir: Path, bridge_port: int = 47774, interval: float = 0.1,
                 shared: SharedHud | None = None, log=print, metrics=None):
        """metrics 不给时读运行目录里的 metrics.json（与网页 HUD 相同；开局录制时那里是占位内容）。"""
        self.run_dir, self.metrics, self.interval, self.log = Path(run_dir), metrics, interval, log
        self._state: dict = {}
        self.shared = shared or SharedHud(mapping_name(bridge_port))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = None
        self._atlas_key = None
        self._atlas: Atlas | None = None
        self._config: dict = {}
        self._config_at = 0.0
        self._history: list = []
        self._history_at = 0.0
        self.renders = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="mod-hud", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self.shared.disable()
        except (ValueError, OSError):
            pass

    def _inputs(self, now: float) -> tuple[dict, dict, list]:
        if now - self._config_at > 2:
            from overlay.server import load_config
            self._config, self._config_at = load_config(), now
        if self.metrics is None:
            from overlay.server import read_metrics
            state = read_metrics(self.run_dir)
            if state is not None:
                self._state = state
            state = self._state           # 文件正被替换读不到时用上一次的
        elif hasattr(self.metrics, "snapshot"):
            state = self.metrics.snapshot()
        else:
            state = dict(self.metrics)
        if now - self._history_at > 0.5:
            from overlay.history import read_history
            try:
                self._history = read_history(self.run_dir, state)
            except Exception:   # noqa: BLE001（历史读不到时保留上一次）
                pass
            self._history_at = now
        return state, self._config, self._history

    def tick(self) -> bool:
        """看一次；重画了返回 True。"""
        now = time.time()
        state, config, history = self._inputs(now)
        self.shared.heartbeat()
        if not state:                     # 这次运行还没有统计：先不显示
            struct.pack_into("<i", self.shared.buf, OFF_ENABLED, 0)
            self._last = None
            return False
        vm = view_model(state, config, history, now)
        view = self.shared.view_size() or (DESIGN_VIEW, 1080)
        width = max(160, int(round(DESIGN_WIDTH * view[0] / DESIGN_VIEW)))
        colors = config.get("colors") or {}
        self.shared.timer(vm.timer_base, vm.timer_extra, vm.timer_live)
        key = (vm, width, tuple(sorted(colors.items())))
        if key == self._last:
            return False
        px = width / DESIGN_WIDTH
        if self._atlas_key != (width, tuple(sorted(colors.items()))):
            self._atlas = make_atlas(px, colors)
            self._atlas_key = (width, tuple(sorted(colors.items())))
        card = render(vm, width, colors)
        atlas = self._atlas
        margin = int(round(16 * px))
        self.shared.publish(premultiplied(card.image), card.image.size, premultiplied(atlas.image), atlas.image.size,
                            atlas.glyphs, (card.timer_x, card.timer_y), (margin, margin))
        self._last = key
        self.renders += 1
        return True

    def _loop(self) -> None:
        failures = 0
        while not self._stop.wait(self.interval):
            try:
                self.tick()
                failures = 0
            except Exception as exc:  # noqa: BLE001（HUD 出错不影响运行）
                failures += 1
                if failures in (1, 50):
                    self.log(f"[hud] 游戏内 HUD 画图失败：{exc}")
