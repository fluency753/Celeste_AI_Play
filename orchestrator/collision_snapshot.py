"""Use only collision data belonging to this observation; never merge frames."""
import json
import math


def collision_problem(obs: dict) -> str | None:
    data = obs.get('collision')
    if not isinstance(data, dict):
        return obs.get('collisionUnavailableReason') or '当前观测未提供统一碰撞快照'
    level = obs.get('level') or {}
    frame = obs.get('frame')
    if not isinstance(frame, int) or isinstance(frame, bool) or data.get('frame') != frame:
        return '碰撞快照帧号与当前观测不一致'
    if not level.get('sid') or level.get('room') is None or data.get('sid') != level['sid'] or data.get('room') != level['room']:
        return '碰撞快照与当前地图或房间不一致'
    if data.get('version') != 1 or data.get('scope') not in {'room', 'view'}:
        return '碰撞快照版本或覆盖范围无效'
    bounds = data.get('bounds')
    try:
        valid_bounds = (isinstance(bounds, list) and len(bounds) == 4
                        and all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in bounds)
                        and bounds[2] > 0 and bounds[3] > 0
                        and math.isfinite(bounds[0] + bounds[2]) and math.isfinite(bounds[1] + bounds[3]))
    except OverflowError:
        valid_bounds = False
    if not valid_bounds:
        return '碰撞快照边界无效'
    coverage = data.get('coverage')
    if (not isinstance(data.get('objects'), list) or not isinstance(coverage, dict)
            or any(type(coverage.get(key)) is not bool for key in ('terrain', 'entities'))
            or type(data.get('complete')) is not bool or type(data.get('geometryComplete')) is not bool
            or data['complete'] != (coverage['terrain'] and coverage['entities'])):
        return '碰撞快照覆盖信息无效'
    return None


def current_collision(obs: dict) -> dict | None:
    return obs['collision'] if collision_problem(obs) is None else None


def collision_summary(obs: dict) -> str:
    data = current_collision(obs)
    if data is None:
        if not obs.get('level'):
            return '当前不在关卡中，没有碰撞快照。'
        return collision_problem(obs) + '；旧 solids 仅表示地形，实体碰撞仍须检查 entities。'
    scope = '整个房间（含边距）' if data['scope'] == 'room' else '当前局部范围'
    coverage = '地形与实体均已覆盖' if data['complete'] else '部分数据，未提供的类别不能视为空'
    shapes = '形状完整' if data['geometryComplete'] else '含未知或未完整表达的形状'
    return (f"统一碰撞快照：帧 {data['frame']}，{scope}，{len(data['objects'])} 个碰撞体；{coverage}，{shapes}。"
            '实体位置和碰撞状态只对当前帧成立；停着不代表不会移动，局部未见不代表已消失。')


def collision_json(obs: dict) -> str:
    return json.dumps({'collision': current_collision(obs), 'collisionNote': collision_summary(obs)},
                      ensure_ascii=False, separators=(',', ':'))


def terrain_rects(data: dict) -> list[list[float]]:
    """Legacy terrain-only view, derived from authoritative terrain shapes when available."""
    result = []
    def visit(shape):
        if shape.get('shape') == 'rect':
            result.append([shape[k] for k in ('x', 'y', 'w', 'h')])
        elif shape.get('shape') in {'grid', 'list'}:
            for part in shape.get('parts', []):
                visit(part)
    for obj in data['objects']:
        if obj.get('source') == 'terrain' and obj.get('collidable') is True:
            visit(obj['geometry'])
    return result
