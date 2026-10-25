"""当前碰撞箱沿坐标轴平移的首个障碍；不推断跳跃或完整路线。"""
from .connectivity import observed_solids


def axis_clearance(obs):
    hitbox = (obs.get('player') or {}).get('hitbox')
    if not hitbox:
        return None
    x, y, w, h = hitbox
    blockers = [(list(rect), '地形', False) for rect in (observed_solids(obs) or ())]
    for entity in obs.get('entities') or []:
        category = entity.get('cat')
        if category not in {'solid', 'dynamic_solid', 'dream_block', 'jumpthru'}:
            continue
        if entity.get('collidable') is False:
            continue
        if not all(key in entity for key in ('x', 'y', 'w', 'h')):
            continue
        label = f"{entity.get('type', category)}#{entity.get('id', '?')}"
        if category == 'dream_block':
            label += '（普通移动会碰撞，冲刺能否穿过需另核验激活状态）'
        elif category == 'dynamic_solid':
            label += '（当前形状，状态变化后需重查）'
        elif category == 'jumpthru':
            label += '（只挡从上方下落，不表示可按下穿过）'
        if entity.get('shape'):
            label += '（外接框候选，非矩形形状需另核验）'
        blockers.append(([entity[key] for key in ('x', 'y', 'w', 'h')], label, category == 'jumpthru'))
    result = {}
    for rect, label, one_way in blockers:
        bx, by, bw, bh = rect
        if bw <= 0 or bh <= 0:
            continue
        distances = {}
        if y < by + bh and by < y + h and not one_way:
            distances.update(left=x - bx - bw, right=bx - x - w)
        if x < bx + bw and bx < x + w:
            distances['down'] = by - y - h
            if not one_way:
                distances['up'] = y - by - bh
        for direction, distance in distances.items():
            if distance < 0:
                continue
            if direction not in result or distance < result[direction]['distance']:
                result[direction] = {'distance': distance, 'rect': rect, 'obstacle': label}
    return result


def clearance_text(obs):
    result = axis_clearance(obs)
    if not result:
        return []
    lines = ['局部阻挡检查（保持当前碰撞箱和另一轴位置不变；不是跳跃预测，也不是路线结论）：']
    for direction, label in (('left', '左'), ('right', '右'), ('up', '上'), ('down', '下')):
        if direction in result:
            item = result[direction]
            lines.append(f"- 向{label}直移 {item['distance']:g} 像素后接触 {item['obstacle']}，范围 {item['rect']}。")
    lines.append('静态空区连通只表示可能绕行，不代表当前高度直走畅通；以上未判断危险、重力、移动后的障碍或房间外情况。')
    return lines
