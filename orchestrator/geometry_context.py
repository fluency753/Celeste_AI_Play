"""Lossless shared shapes for detail pages; explicit distribution-only hazard overviews."""
import copy
import hashlib
import json
import math

from .collision_snapshot import current_collision, collision_problem
from .entity_context import selected_state, COSMETIC_TRIGGERS


def snapshot_id(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]


def _observation_id(obs):
    return snapshot_id({**{key: obs.get(key) for key in
                          ('collision', 'entities', 'player', 'exits', 'spawns', 'respawnPoint', 'triggers')},
                        'side': (obs.get('level') or {}).get('side')})


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
        attributes = {k: v for k, v in obj.items() if k not in result and k != 'geometry'}
        attributes.update({k: v for k, v in entity.items()
                      if k not in {'id', 'type', 'cat', 'x', 'y', 'w', 'h', 'collider', 'parts', 'fields', 'moved'}}
        )
        if attributes: result['attributes'] = attributes
        if entity.get('fields'): result['state'] = entity['fields']
        if entity.get('moved'): result['moved'] = entity['moved']
        return result


def _header(data, obs):
    return {**{k: data.get(k) for k in ('version', 'frame', 'sid', 'room', 'scope', 'bounds', 'coverage',
                                      'complete', 'geometryComplete', 'player')},
            'snapshot_id': _observation_id(obs), 'encoding': 'shared-shapes-v1',
            'shapeNote': 'geometry = shapes[object.shape] translated by object.at; all x/y including nested parts use this same anchor. No rounding.'}


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def room_metadata(obs):
    """The same outer baseline fields that roomChanges replaces, including clears."""
    data = current_collision(obs)
    covered = {obj.get('id') for obj in data['objects']} if data else set()
    result = {key: copy.deepcopy(obs.get(key)) for key in ('exits', 'spawns', 'respawnPoint')}
    result['triggers'] = [trigger for trigger in obs.get('triggers') or []
                          if not trigger.get('type', '').startswith(COSMETIC_TRIGGERS)]
    result['entities'] = []
    for entity in obs.get('entities') or []:
        if entity.get('id') not in covered:
            item = {key: value for key, value in entity.items() if key not in {'fields', 'moved'}}
            state = selected_state(entity)
            if state:
                item['state'] = state
            result['entities'].append(item)
    return result


def _context_snapshot(obs, shape_ids=None):
    data = current_collision(obs)
    if data is None:
        return None
    encoder = _Encoder(obs)
    shapes, records = {}, {}
    shape_ids = dict(shape_ids or {})
    for obj in data['objects']:
        packed = encoder.pack(obj)
        shape = encoder.shapes[packed['shape']]
        shape_key = json.dumps(shape, sort_keys=True, separators=(',', ':'))
        if shape_key not in shape_ids:
            shape_ids[shape_key] = 's' + str(len(shape_ids))
        ident = shape_ids[shape_key]
        shapes[ident] = shape
        packed['shape'] = ident
        packed.pop('moved', None)  # Geometry positions already express movement; not a second clock.
        state = selected_state(encoder.entities.get(obj.get('id'), {}))
        packed.pop('state', None)
        if state:
            packed['state'] = state
        attributes = packed.get('attributes', {})
        attributes = {key: value for key, value in attributes.items()
                      if key not in packed and key not in {'shape', 'r'}}
        if attributes:
            packed['attributes'] = attributes
        else:
            packed.pop('attributes', None)
        records[(obj.get('source'), obj.get('id'))] = packed
    identity = {key: data.get(key) for key in
                ('version', 'sid', 'room', 'scope', 'bounds', 'coverage', 'complete', 'geometryComplete')}
    identity['side'] = (obs.get('level') or {}).get('side')
    # Source IDs must identify distinct instances for replacement/removal to be meaningful.
    if len(records) != len(data['objects']):
        return None
    room_data = room_metadata(obs)
    return {'data': data, 'identity': identity, 'records': records, 'shapes': shapes, 'room_data': room_data,
            'snapshot_id': _observation_id(obs), 'shape_ids': shape_ids}


def _groups(records):
    cohorts = {}
    for obj in records:
        common = {key: value for key, value in obj.items() if key not in {'id', 'at', 'state', 'shape'}}
        key = json.dumps(common, sort_keys=True, separators=(',', ':'))
        if key not in cohorts:
            cohorts[key] = (common, [])
        cohorts[key][1].append(obj)
    result = []
    for common, objects in cohorts.values():
        group = dict(common)
        columns = ['id', 'x', 'y']
        same_shape = all(obj['shape'] == objects[0]['shape'] for obj in objects)
        if same_shape:
            group['shape'] = objects[0]['shape']
        else:
            columns.append('shape')
        states = [obj.get('state', {}) for obj in objects]
        same_state = all(state == states[0] for state in states)
        if same_state:
            if states[0]:
                group['state'] = states[0]
        else:
            columns.append('state')
        if len(columns) != 3:
            group['instanceColumns'] = columns
        group['instances'] = []
        for obj, state in zip(objects, states):
            row = [obj['id'], *obj['at']]
            if not same_shape:
                row.append(obj['shape'])
            if not same_state:
                row.append(state)
            group['instances'].append(row)
        result.append(group)
    return result


def _base(current):
    data = current['data']
    return {**current['identity'], 'frame': data['frame'], 'snapshot_id': current['snapshot_id'],
            'encoding': 'room-context-v2', 'instanceColumns': ['id', 'x', 'y'],
            'stateDetail': 'selected gameplay fields; geometry query/raw workspace retains full reflection'}


def _shape_subset(current, records):
    return {obj['shape']: current['shapes'][obj['shape']] for obj in records}


def _baseline(current):
    records = list(current['records'].values())
    return {**_base(current), 'mode': 'baseline', 'detailsComplete': True,
            'object_count': len(records), 'shapes': current['shapes'], 'groups': _groups(records)}


def geometry_overview(obs):
    """Exact layout: group metadata once, preserve every instance position; no hazard threshold."""
    current = _context_snapshot(obs)
    return _baseline(current) if current is not None else None


class GeometryContext:
    """Per-server last-emitted projection. Preparing a response never advances the baseline."""
    def __init__(self):
        self.last_emitted = None

    def prepare(self, obs, *, force=False):
        previous = self.last_emitted
        current = _context_snapshot(obs, previous['shape_ids'] if previous else None)
        if current is None:
            return {'mode': 'unavailable', 'frame': obs.get('frame'),
                    'reason': collision_problem(obs) or 'duplicate collision IDs', 'baselineInvalidated': True}, None
        if (force or previous is None or current['identity'] != previous['identity']
                or current['data']['frame'] < previous['data']['frame']):
            current = _context_snapshot(obs)  # A baseline replaces the whole shape namespace.
            return _baseline(current), current
        added, changed = [], []
        for key, obj in current['records'].items():
            if key not in previous['records']:
                added.append(obj)
            elif obj != previous['records'][key]:
                changed.append(obj)
        removed = [[source, ident] for source, ident in previous['records'] if (source, ident) not in current['records']]
        used = added + changed
        delta = {'mode': 'delta', 'encoding': 'room-context-v2', 'frame': current['data']['frame'],
                 'sid': current['data']['sid'], 'room': current['data']['room'],
                 'base_snapshot_id': previous['snapshot_id'], 'snapshot_id': current['snapshot_id'],
                 'object_count': len(current['records']), 'added': _groups(added),
                 'changed': _groups(changed), 'removed': removed, 'shapes': _shape_subset(current, used)}
        room_changes = {key: value for key, value in current['room_data'].items()
                        if value != previous['room_data'].get(key)}
        if room_changes:
            delta['roomChanges'] = room_changes
        baseline = _baseline(current)
        # A large rebuild should not cost more than resending its complete compact layout.
        return (baseline if _size(baseline) < _size(delta) else delta), current

    def commit(self, candidate):
        self.last_emitted = copy.deepcopy(candidate)


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
    ident = _observation_id(obs)
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
    return {**_header(data, obs), 'source': 'last_observation_cache; no game advancement', 'region': region,
            'regionWithinSourceBounds': covered, 'selection': 'known shapes intersect region; unknown shapes use conservative bounds. Returned shapes are whole, not cropped.',
            'shapes': encoder.shapes, 'objects': objects, 'matched_total': len(matches), 'offset': offset,
            'returned': len(objects), 'next_offset': offset+len(objects) if offset+len(objects) < len(matches) else None}
