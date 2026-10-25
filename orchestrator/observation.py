"""把桥接返回的 JSON 观测整理成给智能体看的文字与图片。"""
from __future__ import annotations

import base64
import io
import json
import math
import re

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
    "deviate": "偏离预期轨迹",
}


def upscale_png(b64: str, factor: int = 2) -> bytes:
    """最近邻放大，像素边界保持锐利。"""
    img = PILImage.open(io.BytesIO(base64.b64decode(b64)))
    img = img.resize((img.width * factor, img.height * factor), PILImage.NEAREST)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


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


VIEW_W, VIEW_H = 320, 180


def solid_rects(tiles: dict | None) -> list[list[int]]:
    """把字符地图里的地形格子（#）合并成像素坐标的矩形碰撞箱 [x, y, 宽, 高]（先按行取连续段，再纵向合并相同的段）。"""
    if not tiles or not tiles.get("rows"):
        return []
    ox, oy = tiles["origin"]
    cell = tiles.get("cell", 8)
    rects: list[list[int]] = []
    open_runs: dict[tuple[int, int], list[int]] = {}
    for r, row in enumerate(list(tiles["rows"]) + [""]):
        runs, c = set(), 0
        while c < len(row):
            if row[c] == "#":
                start = c
                while c < len(row) and row[c] == "#":
                    c += 1
                runs.add((start, c))
            else:
                c += 1
        for key in list(open_runs):
            if key not in runs:
                rects.append(open_runs.pop(key))
        for c0, c1 in runs:
            if (c0, c1) in open_runs:
                open_runs[(c0, c1)][3] += cell
            else:
                open_runs[(c0, c1)] = [ox + c0 * cell, oy + r * cell, (c1 - c0) * cell, cell]
    return sorted(rects, key=lambda q: (q[1], q[0]))


# 相邻房间与本房间的边界之间可能隔着几个像素（1A 开头几个房间上下相隔 4 像素）。游戏换房时在 Madeline 中心外侧
# 8 像素（左右）或 12 像素（上下）处找房间（Level.EnforceBounds）：左右隔得开约 4 像素，向上最多约 19 像素
# （Madeline 最多出界 24 像素），向下约 6 像素以上（一直往下掉）。按这个容差把隔开的房间也算作出口。
SIDE_GAP, UP_GAP, DOWN_GAP = 4, 19, 16


def room_exits(level: dict, neighbors: list[dict] | None) -> list[dict]:
    """与相邻房间正对着的边界段，就是可能的出口（是否被地形挡住要看地形）。"""
    if not neighbors or not level.get("bounds"):
        return []
    bx, by, bw, bh = level["bounds"]
    out = []
    for n in neighbors:
        nx, ny, nw, nh = n["bounds"]
        y0, y1 = max(by, ny), min(by + bh, ny + nh)
        x0, x1 = max(bx, nx), min(bx + bw, nx + nw)
        if 0 <= bx - (nx + nw) <= SIDE_GAP and y1 > y0:
            out.append({"side": "left", "x": bx, "y": [y0, y1], "room": n["room"]})
        elif 0 <= nx - (bx + bw) <= SIDE_GAP and y1 > y0:
            out.append({"side": "right", "x": bx + bw, "y": [y0, y1], "room": n["room"]})
        elif 0 <= by - (ny + nh) <= UP_GAP and x1 > x0:
            out.append({"side": "top", "y": by, "x": [x0, x1], "room": n["room"]})
        elif 0 <= ny - (by + bh) <= DOWN_GAP and x1 > x0:
            out.append({"side": "bottom", "y": by + bh, "x": [x0, x1], "room": n["room"]})
    return out


KEY_FIELD = re.compile(r"(?i)(speed|vel|state|timer|target|percent|node|dir|angle|radius|delay|wait|anger|chase|"
                       r"moving|active|triggered|falling|lerp|ease|progress|amount)")


def key_fields(entity: dict, limit: int = 10) -> dict:
    """从反射读出的状态字段里挑出与运动有关的几项（完整字段在工作区文件里）。"""
    fields = entity.get("fields") or {}
    picked = {}
    for name, value in fields.items():
        if KEY_FIELD.search(name) and not isinstance(value, list | dict) or (
                isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value)
                and KEY_FIELD.search(name)):
            picked[name] = round(value, 3) if isinstance(value, float) else value
            if len(picked) >= limit:
                break
    if "stateMachine" in fields:
        picked["stateMachine"] = fields["stateMachine"]
    return picked


def is_dynamic(entity: dict) -> bool:
    if entity.get("moved"):
        return True
    fields = entity.get("fields") or {}
    for name, value in fields.items():
        if re.search(r"(?i)speed|vel", name):
            if isinstance(value, (int, float)) and value:
                return True
            if isinstance(value, list) and len(value) == 2 and any(isinstance(v, (int, float)) and v for v in value):
                return True
    return False


def enrich_obs(obs: dict) -> dict:
    """整房间观测里补上合并后的地形碰撞箱与出口，原地修改并返回。"""
    tiles = obs.get("tiles")
    if tiles and tiles.get("scope") == "room":
        obs["solids"] = solid_rects(tiles)
        obs["exits"] = room_exits(obs.get("level") or {}, obs.get("neighbors"))
    return obs


def compact_entity(entity: dict) -> dict:
    """给智能体看的实体：去掉完整字段，只给会动的实体保留与运动有关的字段。"""
    out = {k: v for k, v in entity.items() if k != "fields"}
    if entity.get("cat") == "collectible":
        for key in ("collected", "isGhostBerry", "flyingAway", "Winged", "Golden", "Moon", "WaitingOnSeeds"):
            if key in (entity.get("fields") or {}):
                out.setdefault(key, entity["fields"][key])
    if is_dynamic(entity):
        picked = key_fields(entity)
        if picked:
            out["state"] = picked
    return out


def room_json(obs: dict) -> str:
    """进入新房间时附在返回结果里的完整数据：所有数值都是像素坐标，与游戏一致。"""
    level = obs.get("level") or {}
    player = obs.get("player") or {}
    data = {
        "frame": obs.get("frame"), "sid": level.get("sid"),
        "room": level.get("room"), "bounds": level.get("bounds"), "exits": obs.get("exits"),
        "exitsNote": "仅相邻房间的边界投影，不保证有开口，也不保证从当前位置可达；先核对地形。",
        "spawns": obs.get("spawns", []), "respawnPoint": obs.get("respawnPoint"),
        "solids": obs.get("solids"),
        "entities": [compact_entity(e) for e in obs.get("entities", [])],
        "triggers": [t for t in obs.get("triggers", []) if not t["type"].startswith(COSMETIC_TRIGGERS)],
        "player": {k: player.get(k) for k in ("pos", "speed", "state", "dashes", "maxDashes", "stamina", "onGround",
                                              "facing", "ducking", "hitbox", "internal") if k in player},
    }
    if level.get('dialogOpen') or level.get('dialogChoices') or level.get('inCutscene'):
        data['dialog']={key:level.get(key) for key in ('dialogOpen','dialogWaiting','dialogChoices','dialogChoiceIndex','dialogChoiceReady')}
    if obs.get('story'):
        data['story']=obs['story']
    if 'storyInput' in obs:
        data['storyInput']=obs['storyInput']
    if obs.get('storyPlayback'):
        data['storyPlayback']=obs['storyPlayback']
    lines = ["{"]
    keys = list(data)
    for i, key in enumerate(keys):
        value = data[key]
        tail = "," if i < len(keys) - 1 else ""
        if isinstance(value, list) and value and key in ("solids", "entities", "triggers", "exits"):
            lines.append(f'  "{key}": [')
            for j, item in enumerate(value):
                lines.append("    " + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                             + ("," if j < len(value) - 1 else ""))
            lines.append("  ]" + tail)
        else:
            lines.append(f'  "{key}": ' + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + tail)
    lines.append("}")
    return "\n".join(lines)


# 与操作无关的触发器（镜头、音乐、光照等），在给智能体看的数据里省略（工作区文件里保留全部）。
# 风的触发器（WindTrigger、WindAttackTrigger）会改变风向、影响移动，不省略
COSMETIC_TRIGGERS = ("Camera", "Music", "Ambience", "Light", "Bloom", "Rumble", "Sound", "Altmusic", "Minitext",
                     "Lookout", "Spawn")


def crop_to_view(obs: dict, margin: int = 16) -> tuple[list[dict], dict | None]:
    """把整房间的实体与字符地图裁到当前画面（外扩 margin），用于每次动作后的简要文字。"""
    level = obs.get("level") or {}
    cam = level.get("camera")
    entities = obs.get("entities") or []
    tiles = obs.get("tiles")
    if not cam:
        return entities, tiles
    x0, y0 = cam[0] - margin, cam[1] - margin
    x1, y1 = cam[0] + VIEW_W + margin, cam[1] + VIEW_H + margin
    shown = [e for e in entities if e["x"] < x1 and e["x"] + e["w"] > x0 and e["y"] < y1 and e["y"] + e["h"] > y0]
    if tiles and tiles.get("rows"):
        ox, oy = tiles["origin"]
        rows = tiles["rows"]
        c0 = max(0, int(math.floor((x0 - ox) / 8)))
        c1 = min(len(rows[0]), int(math.ceil((x1 - ox) / 8)))
        r0 = max(0, int(math.floor((y0 - oy) / 8)))
        r1 = min(len(rows), int(math.ceil((y1 - oy) / 8)))
        if c1 > c0 and r1 > r0:
            tiles = {**tiles, "origin": [ox + 8 * c0, oy + 8 * r0], "rows": [row[c0:c1] for row in rows[r0:r1]],
                     "scope": "view"}
    return shown, tiles


def describe_obs(obs: dict, full_room: bool = True, effort: str | None = None, explore: bool = False) -> str:
    """full_room=True：整个房间的实体与地图（进入新房间、主动观察时）；False：只描述当前画面附近。
    effort：附在头部的推理强度说明（例如"推理强度：high"）。
    头部不写现实用时与时间预算：预算只是主控的安全上限，写给智能体看会让它赶时间、执行没优化完的路线。"""
    lines: list[str] = []
    level = obs.get("level")
    player = obs.get("player")

    head = f"[帧 {obs.get('frame')}] "
    if level:
        head += (f"{chapter_label(level)} · 房间 {level['room']} · 章节计时 {fmt_ticks(level['chapterTimeTicks'])}"
                 f" · 死亡 {level['deaths']}（本房间 {level['deathsInRoom']}）")
    else:
        head += f"场景 {obs.get('scene')}"
    if effort:
        head += f" · {effort}"
    lines.append(head)
    for previous in obs.get('storyPlayback') or []:
        lines.append('刚才由主控自动播放的文字：'+previous['text'])
    node = obs.get('storyInput')
    if node:
        lines.append('当前剧情输入节点：'+node+'；主控已停止自动播放，教学请用 act，完整对白用 advance，选项用 choose。')
    elif obs.get('controllable') and (level or {}).get('inCutscene'):
        lines.append('虽然有剧情标记，角色当前仍可操作，请继续用 act；不需要等待或确认。')
    for story in obs.get('story') or []:
        portrait=story.get('speakerPortrait') or {}
        speaker=portrait.get('sprite')
        if story.get('text'):
            label = '当前纪念碑文字' if story.get('kind') == 'memorial' else ('当前对白（头像标识 '+speaker+'）' if speaker else '当前剧情文字')
            lines.append(label+'：'+story['text'])
        if story.get('controls'):
            lines.append('当前教学按键：'+json.dumps(story['controls'],ensure_ascii=False))
    if (level or {}).get('dialogChoices'):
        lines.append('当前需要选择对话选项；advance不会代选，请用cutscene(action="choose", choice=index)：')
        for option in level['dialogChoices']:
            lines.append(f"  {option['index']}: {option['text']}" + ('（当前选中）' if option.get('selected') else ''))

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
            f"剩余帧数：coyote time {t['jumpGrace']}，variable jump {t['varJump']}，dash cooldown {t['dashCooldown']}")
    if level:
        cam = level["camera"]
        b = level["bounds"]
        lines.append(f"画面左上角世界坐标 ({cam[0]}, {cam[1]})，画面 320×180；"
                     f"本房间范围 x {b[0]}~{b[0] + b[2]}，y {b[1]}~{b[1] + b[3]}（边界未必有出口；相邻房间不等于可直接到达）")
        flags = [name for name, key in (("剧情中", "inCutscene"), ("转场中", "transitioning"), ("暂停", "paused")) if level.get(key)]
        if flags:
            lines.append("当前：" + "、".join(flags))

    if full_room:
        entities, tiles = obs.get("entities") or [], obs.get("tiles")
    else:
        entities, tiles = crop_to_view(obs)
    # 收集品摘要只给探索模式；完整房间数据在所有模式保留全部实体。
    collectibles = [compact_entity(e) for e in entities if e.get("cat") == "collectible"] if explore else []
    if collectibles:
        lines.append("当前观测中的收集品（是否能接近需结合地形确认）：")
        for e in collectibles:
            status = "已收集" if e.get("collected") else "此前存档已取得的幽灵收集品" if e.get("isGhostBerry") else "尚未确认收集"
            lines.append(f"- {e['type']}#{e.get('id', '?')} 碰撞范围 ({e['x']}, {e['y']}, {e['w']}, {e['h']})；{status}")
    if entities:
        px, py = player["pos"] if player else (0.0, 0.0)
        entities = sorted(entities, key=lambda e: math.hypot(e["x"] + e["w"] / 2 - px, e["y"] + e["h"] / 2 - py))
        limit = 60 if full_room else 25
        where = "整个房间的实体" if full_room else "画面附近的实体"
        lines.append(f"{where}（按距离排序，共 {len(entities)} 个，最多列 {limit} 个）：")
        for e in entities[:limit]:
            extra = []
            for key in ("dir", "orient", "twoDashes", "red", "r", "shape", "visible", "collected", "respawnTimer",
                        "start", "target", "percent", "triggered", "falling", "canDash", "active", "node",
                        "activated", "fallen"):
                if key in e:
                    extra.append(f"{key}={e[key]}")
            if e.get("moved"):
                extra.append(f"本次移动 Δ=({e['moved'][0]}, {e['moved'][1]})")
            if is_dynamic(e):
                picked = key_fields(e, limit=6)
                if picked:
                    extra.append("状态 " + json.dumps(picked, ensure_ascii=False, separators=(",", ":")))
            extra_text = (" " + " ".join(extra)) if extra else ""
            ident = f"#{e['id']} " if "id" in e else ""
            lines.append(f"- {ident}{e['type']} [{e['cat']}] 左上 ({e['x']}, {e['y']}) 宽高 {e['w']}×{e['h']}{extra_text}")

    if tiles:
        ox, oy = tiles["origin"]
        rows = tiles["rows"]
        cols = len(rows[0]) if rows else 0
        scope = "整个房间" if tiles.get("scope") == "room" else "当前画面附近"
        if tiles.get("legend"):
            lines.append("图例：" + "；".join(f"{symbol}={meaning}" for symbol, meaning in tiles["legend"].items()))
        lines.append(f"字符地图（{scope}；每格 8×8 像素；第 0 行第 0 列格子的左上角是世界坐标 ({ox}, {oy})，"
                     f"第 r 行第 c 列的左上角是 ({ox} + 8c, {oy} + 8r)）：")
        lines.append("    " + "".join(str(c // 10) for c in range(cols)))
        lines.append("    " + "".join(str(c % 10) for c in range(cols)))
        for r, row in enumerate(rows):
            lines.append(f"{r:02d}  {row}")
    return "\n".join(lines)


def describe_deviation(dev: dict, tolerance: float) -> str:
    exp, act = dev.get("expected") or [], dev.get("actual") or []
    exp_text = f"({exp[0]:.2f}, {exp[1]:.2f}{', ' + exp[2] if len(exp) > 2 else ''})" if len(exp) >= 2 else "?"
    if len(act) >= 2 and act[0] is not None:
        act_text = f"({act[0]:.2f}, {act[1]:.2f}, {act[2] if len(act) > 2 else '?'})"
        diff = f"，差 ({act[0] - exp[0]:+.2f}, {act[1] - exp[1]:+.2f})"
    else:
        act_text, diff = "(Madeline 不在场)", ""
    return (f"计划第 {dev.get('index')} 帧（全局帧 {dev.get('frame')}）偏离预期：预期 {exp_text}，实际 {act_text}{diff}，"
            f"容差 {tolerance:g} 像素")


def describe_attempts(attempts: list[dict]) -> str:
    """自动重试的经过：每次执行与每次重算各一行。"""
    lines = ["自动重试经过："]
    for a in attempts:
        if a["kind"] == "execute":
            text = f"  第 {a['attempt'] + 1} 次执行：{a['consumed']}/{a['planned']} 帧，{STOP_TEXT.get(a['stop'], a['stop'])}"
            if a.get("deviation"):
                text += f"（计划第 {a['deviation'].get('index')} 帧）"
        else:
            text = (f"  重算 {a['attempt']}（原因：{STOP_TEXT.get(a['reason'], a['reason'])}）："
                    f"{'超时' if a.get('timed_out') else '退出码 ' + str(a['returncode'])}，{a['seconds']:.1f} 秒")
            if a.get("error"):
                text += f"；{a['error'].strip()[-300:]}"
        lines.append(text)
    return "\n".join(lines)


def describe_step(result: dict, max_rows: int = 30) -> str:
    stop = result.get("stop", "?")
    lines = [f"执行了 {result['consumed']}/{result['planned']} 帧，停止原因：{STOP_TEXT.get(stop, stop)}"]
    if result.get("expectFrames"):
        lines.append(f"预期轨迹：对照了前 {result['expectFrames']} 帧，容差 {result.get('tolerance', 1):g} 像素")
    if result.get("deviation"):
        lines.append(describe_deviation(result["deviation"], result.get("tolerance", 1)))
    events = result.get("events") or []
    if events:
        lines.append("事件：" + "；".join(
            f"帧 {e['frame']} {STOP_TEXT.get(e['type'], e['type'])}{('(' + e['detail'] + ')') if e.get('detail') else ''}"
            for e in events))
    moving = {}
    for row in result.get("entityTrajectory") or []:
        frame, ident, x, y = row
        first = moving.setdefault(ident, [frame, x, y, frame, x, y])
        first[3:] = [frame, x, y]
    if moving:
        parts = [f"#{i} ({v[1]:.0f},{v[2]:.0f})→({v[4]:.0f},{v[5]:.0f})" for i, v in list(moving.items())[:10]]
        lines.append(f"这段动作中画面附近有 {len(moving)} 个实体移动过（编号 起点→终点）：" + "；".join(parts))
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
