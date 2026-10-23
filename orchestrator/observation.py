"""把桥接返回的 JSON 观测整理成给智能体看的文字与图片。"""
from __future__ import annotations

import base64
import io
import math

from PIL import Image as PILImage

from .config import CHAPTER_NAMES

STOP_TEXT = {
    "done": "按键全部执行完",
    "death": "Madeline 死亡",
    "room": "进入新房间",
    "uncontrollable": "失去控制（转场、剧情或死亡动画）",
    "controllable": "恢复可操作",
    "land": "落地",
    "refill": "冲刺次数恢复",
    "complete": "章节完成",
    "cancelled": "被取消",
}


def upscale_png(b64: str, factor: int = 2) -> bytes:
    """最近邻放大，像素边界保持锐利。"""
    img = PILImage.open(io.BytesIO(base64.b64decode(b64)))
    img = img.resize((img.width * factor, img.height * factor), PILImage.NEAREST)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def fmt_ticks(ticks: int) -> str:
    total_ms = ticks // 10_000
    s, ms = divmod(total_ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def chapter_label(level: dict) -> str:
    name = CHAPTER_NAMES.get(level.get("sid", ""), level.get("sid", "?"))
    side = level.get("side", "A")
    return name if side == "A" else f"{name} {side} 面"


def _yn(v: bool) -> str:
    return "是" if v else "否"


def describe_obs(obs: dict, elapsed_s: float, budget_s: float) -> str:
    lines: list[str] = []
    level = obs.get("level")
    player = obs.get("player")

    head = f"[帧 {obs.get('frame')}] "
    if level:
        head += (f"{chapter_label(level)} · 房间 {level['room']} · 章节计时 {fmt_ticks(level['chapterTimeTicks'])}"
                 f" · 死亡 {level['deaths']}（本房间 {level['deathsInRoom']}）")
    else:
        head += f"场景 {obs.get('scene')}"
    head += f" · 现实用时 {fmt_clock(elapsed_s)} / 预算 {fmt_clock(budget_s)}"
    lines.append(head)

    if player:
        x, y = player["pos"]
        vx, vy = player["speed"]
        hb = player["hitbox"]
        t = player["timers"]
        lines.append(
            f"Madeline：位置 ({x:.2f}, {y:.2f})  速度 ({vx:.1f}, {vy:.1f})  状态 {player['state']}  "
            f"着地 {_yn(player['onGround'])}  冲刺 {player['dashes']}/{player['maxDashes']}  "
            f"体力 {player['stamina']}  朝向 {player['facing']}  贴墙 左{_yn(player['wallLeft'])}/右{_yn(player['wallRight'])}  "
            f"下蹲 {_yn(player['ducking'])}  可操作 {_yn(obs.get('controllable', False))}")
        lines.append(
            f"碰撞箱 x {hb[0]}~{hb[0] + hb[2]}，y {hb[1]}~{hb[1] + hb[3]}；"
            f"剩余帧数：土狼时间 {t['jumpGrace']}，可变跳 {t['varJump']}，冲刺冷却 {t['dashCooldown']}")
    if level:
        cam = level["camera"]
        b = level["bounds"]
        lines.append(f"画面左上角世界坐标 ({cam[0]}, {cam[1]})，画面 320×180；"
                     f"本房间范围 x {b[0]}~{b[0] + b[2]}，y {b[1]}~{b[1] + b[3]}（离开这个范围就会进入相邻房间）")
        flags = [name for name, key in (("剧情中", "inCutscene"), ("转场中", "transitioning"), ("暂停", "paused")) if level.get(key)]
        if flags:
            lines.append("当前：" + "、".join(flags))

    entities = obs.get("entities") or []
    if entities:
        px, py = player["pos"] if player else (0.0, 0.0)
        entities = sorted(entities, key=lambda e: math.hypot(e["x"] + e["w"] / 2 - px, e["y"] + e["h"] / 2 - py))
        lines.append(f"视野内实体（按距离排序，共 {len(entities)} 个，最多列 25 个）：")
        for e in entities[:25]:
            extra = []
            for key in ("dir", "orient", "twoDashes", "red", "r", "shape", "visible"):
                if key in e:
                    extra.append(f"{key}={e[key]}")
            extra_text = (" " + " ".join(extra)) if extra else ""
            lines.append(f"- {e['type']} [{e['cat']}] 左上 ({e['x']}, {e['y']}) 宽高 {e['w']}×{e['h']}{extra_text}")

    tiles = obs.get("tiles")
    if tiles:
        ox, oy = tiles["origin"]
        rows = tiles["rows"]
        cols = len(rows[0]) if rows else 0
        lines.append(f"字符地图（每格 8×8 像素；第 0 行第 0 列格子的左上角是世界坐标 ({ox}, {oy})，"
                     f"第 r 行第 c 列的左上角是 ({ox} + 8c, {oy} + 8r)）：")
        lines.append("    " + "".join(str(c // 10) for c in range(cols)))
        lines.append("    " + "".join(str(c % 10) for c in range(cols)))
        for r, row in enumerate(rows):
            lines.append(f"{r:02d}  {row}")
    return "\n".join(lines)


def describe_step(result: dict, max_rows: int = 30) -> str:
    stop = result.get("stop", "?")
    lines = [f"执行了 {result['consumed']}/{result['planned']} 帧，停止原因：{STOP_TEXT.get(stop, stop)}"]
    events = result.get("events") or []
    if events:
        lines.append("事件：" + "；".join(
            f"帧 {e['frame']} {STOP_TEXT.get(e['type'], e['type'])}{('(' + e['detail'] + ')') if e.get('detail') else ''}"
            for e in events))
    traj = result.get("trajectory") or []
    if traj:
        step = max(1, math.ceil(len(traj) / max_rows))
        event_frames = {e["frame"] for e in events}
        picked = [row for i, row in enumerate(traj)
                  if i % step == 0 or i == len(traj) - 1 or row[0] in event_frames]
        lines.append(f"轨迹（每 {step} 帧取样，另含事件帧与最后一帧；列：帧, x, y, vx, vy, 状态, 冲刺, 着地）：")
        for row in picked:
            if len(row) < 8 or row[1] is None:
                lines.append(f"  {row[0]}: (Madeline 不在场)")
            else:
                f, x, y, vx, vy, st, dashes, ground = row[:8]
                lines.append(f"  {f}: {x:.2f}, {y:.2f}, {vx:.1f}, {vy:.1f}, {st}, {dashes}, {ground}")
    return "\n".join(lines)
