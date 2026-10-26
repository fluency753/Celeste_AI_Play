"""Lossless shared shapes for detail pages; explicit distribution-only hazard overviews."""
import copy
import hashlib
import json
import math

from .collision_snapshot import current_collision, collision_problem

SMALL_HAZARD_SET = 12
GROUP_CELL = 64


def snapshot_id(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]


def _bounds(shape):
    try:
        values = [shape[k] for k in ('x', 'y', 'w', 'h')]
        if (all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in values)
                and values[2] >= 0 and values[3] >= 0):
            return values
    except (KeyError, TypeError, OverflowError):
        pass
    return None


def _known(shape):
    if not isinstance(shape, dict) or _bounds(shape) is None:
        return False
    kind = shape.get('shape')
    circle = kind == 'circle' and type(shape.get('r')) in (int, float) and math.isfinite(shape['r']) and shape['r'] >= 0
    return kind == 'rect' or circle or (kind in {'list', 'grid'} and
        isinstance(shape.get('parts'), list) and all(_known(p) for p in shape['parts']))


def _relative(shape, x, y):
    if not isinstance(shape, dict): return {'shape': 'unknown', 'raw': copy.deepcopy(shape)}
    result = copy.deepcopy(shape)
    if isinstance(result.get('x'), (float, int)): result['x'] -= x
    if isinstance(result.get('y'), (float, int)): result['y'] -= y
    if isinstance(result.get('parts'), list): result['parts'] = [_relative(p, x, y) for p in result['parts']]
    return result


class _Encoder:
    def __init__(self, obs):
        self.shapes = {}
        self.indices = {}
        self.entities = {e.get('id'): e for e in obs.get('entities') or []}

    def shape(self, obj):
        geometry = obj.get('geometry') or {}
        bounds = _bounds(geometry)
        anchor = bounds[:2] if bounds else [0, 0]
        relative = _relative(geometry, *anchor)
        key = json.dumps(relative, sort_keys=True, separators=(',', ':'))
        if key not in self.indices:
            ident = 's' + str(len(self.indices))
            self.indices[key] = ident
            self.shapes[ident] = relative
        return self.indices[key], anchor

    def pack(self, obj):
        result = {k: obj[k] for k in ('id', 'source', 'type', 'category', 'kind', 'collidable', 'mutable') if k in obj}
        result['shape'], result['at'] = self.shape(obj)
        entity = self.entities.get(obj.get('id'), {})
        attributes = {k: v for k, v in entity.items()
                      if k not in {'id', 'type', 'cat', 'x', 'y', 'w', 'h', 'collider', 'parts', 'fields', 'moved'}}
        if attributes: result['attributes'] = attributes
        if entity.get('fields'): result['state'] = entity['fields']
        if entity.get('moved'): result['moved'] = entity['moved']
        return result


def _header(data):
    return {**{k: data.get(k) for k in ('version', 'frame', 'sid', 'room', 'scope', 'bounds', 'coverage',
                                      'complete', 'geometryComplete', 'player')},
            'snapshot_id': snapshot_id(data), 'encoding': 'shared-shapes-v1',
            'shapeNote': 'geometry = shapes[object.shape] translated by object.at; all x/y including nested parts use this same anchor. No rounding.'}


def geometry_overview(obs):
    data = current_collision(obs)
    if data is None:
        return None
    encoder = _Encoder(obs)
    all_hazards = [o for o in data['objects'] if o.get('kind') == 'hazard']
    hazards = [o for o in all_hazards if _known(o.get('geometry'))]
    cohorts = {}
    common_data = {}
    for obj in hazards:
        common = {k: v for k, v in encoder.pack(obj).items() if k not in {'id', 'at', 'state', 'moved'}}
        key = json.dumps(common, sort_keys=True, separators=(',', ':'))
        common_data[key] = common
        cohorts.setdefault(key, []).append(obj)
    repeated = [o for members in cohorts.values() if len(members) > 1 for o in members]
    hidden = len(hazards) > SMALL_HAZARD_SET and bool(repeated)
    hazard_ids = {id(o) for o in repeated} if hidden else set()
    objects = [encoder.pack(o) for o in data['objects'] if id(o) not in hazard_ids]
    buckets = {}
    for key, members in cohorts.items():
        for obj in members if hidden and len(members) > 1 else []:
            _, at = encoder.shape(obj)
            cell = (math.floor(at[0] / GROUP_CELL), math.floor(at[1] / GROUP_CELL))
            buckets.setdefault(key, {}).setdefault(cell, []).append(obj)
    groups = []
    for key, cells in buckets.items():
        pending = set(cells)
        while pending:
            seed = min(pending); pending.remove(seed)
            queue = [seed]; members = []
            while queue:
                cell = queue.pop(); members.extend(cells[cell])
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        neighbour = (cell[0]+dx, cell[1]+dy)
                        if neighbour in pending: pending.remove(neighbour); queue.append(neighbour)
            rectangles = [_bounds(o['geometry']) for o in members]
            x, y = min(r[0] for r in rectangles), min(r[1] for r in rectangles)
            group = {**common_data[key],
                           'count': len(members), 'bounds': [x, y, max(r[0]+r[2] for r in rectangles)-x,
                                                          max(r[1]+r[3] for r in rectangles)-y]}
            states = [{k: v for k, v in encoder.pack(o).items() if k in {'state', 'moved'}} for o in members]
            if all(state == states[0] for state in states):
                group.update(states[0])
            else:
                group['instanceStates'] = [{'id': obj.get('id'), **state} for obj, state in zip(members, states)]
            groups.append(group)
    return {**_header(data), 'shapes': encoder.shapes, 'objects': objects,
            'detailsComplete': not hidden, 'hazards': {'total': len(all_hazards), 'detailsReturned': len(all_hazards)-len(hazard_ids),
                'detailsOmitted': len(hazard_ids), 'groups': groups,
                'grouping': 'repeated identical type/shape/attributes, adjacent 64px anchor cells; differing state/moved retained in instanceStates. Distribution index only, NOT filled obstacles or safe gaps'},
            'detailNote': 'Dense hazards are overview only. Use observe(detail="geometry", region=[x,y,w,h]) for exact candidates; complete means source coverage, NOT all instances shown. Raw game/latest_obs.json and game/collision.json remain complete.'}


def _intersects(shape, region):
    bounds = _bounds(shape)
    if bounds is None: return True  # malformed/unknown cannot establish absence
    x, y, w, h = bounds; rx, ry, rw, rh = region
    if x > rx+rw or x+w < rx or y > ry+rh or y+h < ry: return False
    kind = shape.get('shape')
    if kind == 'circle' and _known(shape):
        r = shape['r']; cx, cy = x+r, y+r
        return (cx-max(rx,min(cx,rx+rw)))**2 + (cy-max(ry,min(cy,ry+rh)))**2 <= r*r
    if kind in {'list', 'grid'} and isinstance(shape.get('parts'), list):
        return any(_intersects(part, region) for part in shape['parts'])
    return True


def geometry_page(obs, *, region=None, offset=0, limit=32, expected_snapshot=None):
    data = current_collision(obs)
    if data is None: raise ValueError(collision_problem(obs) + '；先 observe 获取支持统一碰撞的观测。')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError('offset 必须非负整数，limit 为 1–64')
    ident = snapshot_id(data)
    if expected_snapshot is not None and expected_snapshot != ident:
        raise ValueError('快照已改变，不能跨帧或跨房间混合分页；请从 offset=0 重新查询。')
    if offset and expected_snapshot is None:
        raise ValueError('分页必须带上次返回的 snapshot_id，避免跨帧混页。')
    if region is not None:
        try:
            valid = (isinstance(region, list) and len(region) == 4 and all(type(v) in (float, int) and math.isfinite(v) for v in region)
                     and region[2] > 0 and region[3] > 0 and math.isfinite(region[0]+region[2]) and math.isfinite(region[1]+region[3]))
        except OverflowError: valid = False
        if not valid: raise ValueError('region 必须为有限世界坐标 [x,y,正宽,正高]')
    matches = [o for o in data['objects'] if region is None or _intersects(o.get('geometry') or {}, region)]
    encoder = _Encoder(obs)
    objects = [encoder.pack(o) for o in matches[offset:offset+limit]]
    bx, by, bw, bh = data['bounds']
    covered = region is None or (region[0] >= bx and region[1] >= by and region[0]+region[2] <= bx+bw and region[1]+region[3] <= by+bh)
    return {**_header(data), 'source': 'last_observation_cache; no game advancement', 'region': region,
            'regionWithinSourceBounds': covered, 'selection': 'known shapes intersect region; unknown shapes use conservative bounds. Returned shapes are whole, not cropped.',
            'shapes': encoder.shapes, 'objects': objects, 'matched_total': len(matches), 'offset': offset,
            'returned': len(objects), 'next_offset': offset+len(objects) if offset+len(objects) < len(matches) else None}
