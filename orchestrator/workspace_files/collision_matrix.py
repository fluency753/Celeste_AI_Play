"""File-only collision raster and body-space geometric queries (standard library).

No game connection, frame advancement, or physics solver. See kb/collision_matrix.md.
"""
from __future__ import annotations

from array import array
import argparse
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import time

MAX_CELLS = 300_000
MAX_REFERENCES = 3_000_000
MAX_PARTS = 100_000
MAX_RASTER_VISITS = 6_000_000


def _rect(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
            or value[2] < 0 or value[3] < 0):
        raise ValueError('invalid rectangle')
    return list(value)


def _overlap(a, b):
    return a[0] < b[0]+b[2] and b[0] < a[0]+a[2] and a[1] < b[1]+b[3] and b[1] < a[1]+a[3]


def _parts(shape, budget, depth=0):
    budget[0] += 1
    if budget[0] > MAX_PARTS or depth > 24:
        raise ValueError('collision shape budget exceeded')
    bounds = _rect([shape[k] for k in ('x', 'y', 'w', 'h')])
    kind = shape.get('shape')
    if kind in ('list', 'grid') and isinstance(shape.get('parts'), list):
        return [part for child in shape['parts'] for part in _parts(child, budget, depth+1)]
    if kind == 'circle':
        radius = shape.get('r')
        if type(radius) not in (int, float) or not math.isfinite(radius) or radius < 0:
            raise ValueError('invalid circle radius')
        return [('circle', [bounds[0], bounds[1], radius*2, radius*2], radius)]
    return [('rect' if kind == 'rect' else 'unknown', bounds, None)]


def _hits(part, rect):
    kind, bounds, radius = part
    if not _overlap(bounds, rect):
        return False
    if kind != 'circle':
        return True
    cx, cy = bounds[0]+radius, bounds[1]+radius
    dx = cx - max(rect[0], min(cx, rect[0]+rect[2]))
    dy = cy - max(rect[1], min(cy, rect[1]+rect[3]))
    return dx*dx+dy*dy < radius*radius


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        temporary.write_bytes(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Matrix:
    """Each cell indexes every intersecting source; exact geometry refines queries."""

    def __init__(self, obs, cell=2):
        started = time.perf_counter()
        if cell not in (1, 2, 4, 8):
            raise ValueError('cell must be 1, 2, 4, or 8 pixels')
        data = obs.get('collision')
        level = obs.get('level') or {}
        if (not isinstance(data, dict) or data.get('version') != 1
                or type(obs.get('frame')) is not int or not level.get('sid') or level.get('room') is None
                or data.get('frame') != obs.get('frame') or data.get('sid') != level.get('sid')
                or data.get('room') != level.get('room') or data.get('scope') not in ('room', 'view')):
            raise ValueError('missing or mismatched authoritative collision snapshot')
        coverage = data.get('coverage')
        if (not isinstance(data.get('objects'), list) or not isinstance(coverage, dict)
                or any(type(coverage.get(k)) is not bool for k in ('terrain', 'entities'))
                or type(data.get('complete')) is not bool or type(data.get('geometryComplete')) is not bool
                or data['complete'] != (coverage['terrain'] and coverage['entities'])):
            raise ValueError('invalid collision coverage')
        self.bounds = _rect(data['bounds'])
        if not self.bounds[2] or not self.bounds[3]:
            raise ValueError('empty coverage')
        self.cell = cell
        self.width = math.ceil(self.bounds[2]/cell)
        self.height = math.ceil(self.bounds[3]/cell)
        count = self.width*self.height
        if count > MAX_CELLS:
            raise ValueError(f'raster needs {count} cells; limit {MAX_CELLS}, use a larger cell')
        self.identity = {k: data.get(k) for k in ('version', 'frame', 'sid', 'room', 'scope',
                                                'coverage', 'complete', 'geometryComplete')}
        self.identity['snapshot_id'] = hashlib.sha256(json.dumps(
            {'collision': data, 'entities': obs.get('entities'), 'player': obs.get('player')},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
        self.player = obs.get('player') or {}
        self.exits = obs.get('exits') or []
        entities = {e.get('id'): e for e in obs.get('entities') or []}
        self.sources = []
        self.parts = []
        cells = {}
        source_keys = set()
        references = 0
        raster_visits = 0
        budget = [0]
        for obj in data['objects']:
            source = dict(obj)
            source['key'] = f"{obj['source']}:{obj['id']}"
            if source['key'] in source_keys:
                raise ValueError('duplicate collision source identity')
            source_keys.add(source['key'])
            if obj.get('source') == 'entity' and obj.get('id') in entities:
                source['entity'] = entities[obj['id']]
            parts = _parts(obj['geometry'], budget)
            index = len(self.sources)
            self.sources.append(source)
            self.parts.append(parts)
            occupied = set()
            for part in parts:
                x, y, w, h = part[1]
                # Bound actual work, including repeated overlapping shape parts.
                clipped_w = max(0, min(x+w, self.bounds[0]+self.bounds[2])-max(x, self.bounds[0]))
                clipped_h = max(0, min(y+h, self.bounds[1]+self.bounds[3])-max(y, self.bounds[1]))
                raster_visits += (math.ceil(clipped_w/cell)+1)*(math.ceil(clipped_h/cell)+1)
                if raster_visits > MAX_RASTER_VISITS:
                    raise ValueError('raster work budget exceeded')
                for row, col in self._range(part[1]):
                    if _hits(part, self._cell_rect(row, col)):
                        occupied.add(row*self.width+col)
                        if len(occupied) + references > MAX_REFERENCES:
                            raise ValueError('source/cell reference budget exceeded')
            references += len(occupied)
            for position in occupied:
                cells.setdefault(position, []).append(index)
        self.memberships = [[]]
        signatures = {(): 0}
        self.cells = array('I', [0])*count
        for position, members in cells.items():
            key = tuple(members)
            if key not in signatures:
                signatures[key] = len(self.memberships)
                self.memberships.append(members)
            self.cells[position] = signatures[key]
        self.build_ms = (time.perf_counter()-started)*1000

    @classmethod
    def from_file(cls, path, cell=2):
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return cls(value.get('obs', value), cell)

    @classmethod
    def load(cls, path):
        """Read an exported matrix, checking its RLE and source indices before use."""
        path = Path(path)
        if path.stat().st_size > 32*1024*1024:
            raise ValueError('matrix file exceeds 32 MiB')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('format') != 'collision-matrix-v1':
            raise ValueError('not a collision-matrix-v1 file')
        # Rebuild from authoritative sources: bounded, validates source shapes too, and
        # rejects corrupted cell memberships instead of accepting false empty space.
        objects, entities = [], []
        for source in data['sources']:
            objects.append({k: v for k, v in source.items() if k not in ('key', 'entity')})
            if 'entity' in source:
                entities.append(source['entity'])
        collision = {k: data.get(k) for k in ('version', 'frame', 'sid', 'room', 'scope',
                                             'coverage', 'complete', 'geometryComplete', 'bounds')}
        collision['objects'] = objects
        matrix = cls({'frame': data['frame'], 'level': {'sid': data['sid'], 'room': data['room']},
                      'collision': collision, 'entities': entities, 'player': data.get('player'),
                      'exits': data.get('exits')}, data['cell'])
        if data.get('size') != [matrix.width, matrix.height] or len(data['rows']) != matrix.height:
            raise ValueError('matrix dimensions disagree with sources')
        memberships = data['memberships']
        if not isinstance(memberships, list) or len(memberships) > MAX_CELLS+1:
            raise ValueError('invalid membership table')
        reference_count = 0
        for members in memberships:
            if (not isinstance(members, list) or any(type(i) is not int or not 0 <= i < len(objects) for i in members)
                    or members != sorted(set(members))):
                raise ValueError('invalid membership source indices')
            reference_count += len(members)
            if reference_count > MAX_REFERENCES:
                raise ValueError('membership reference budget exceeded')
        for row, runs in enumerate(data['rows']):
            expected = matrix.cells[row*matrix.width:(row+1)*matrix.width]
            cursor = 0
            for count, membership in runs:
                if (type(count) is not int or count <= 0 or cursor+count > matrix.width
                        or type(membership) is not int or not 0 <= membership < len(memberships)
                        or any(matrix.memberships[value] != memberships[membership]
                               for value in expected[cursor:cursor+count])):
                    raise ValueError('invalid matrix row')
                cursor += count
            if cursor != matrix.width:
                raise ValueError('incomplete matrix row')
        matrix.identity['snapshot_id'] = data['snapshot_id']
        return matrix

    def _range(self, rect):
        x, y, w, h = rect
        ox, oy = self.bounds[:2]
        c0, r0 = max(0, math.floor((x-ox)/self.cell)), max(0, math.floor((y-oy)/self.cell))
        c1 = min(self.width, math.ceil((x+w-ox)/self.cell))
        r1 = min(self.height, math.ceil((y+h-oy)/self.cell))
        for row in range(r0, r1):
            for col in range(c0, c1):
                yield row, col

    def _cell_rect(self, row, col):
        return [self.bounds[0]+col*self.cell, self.bounds[1]+row*self.cell,
                min(self.cell, self.bounds[2]-col*self.cell),
                min(self.cell, self.bounds[3]-row*self.cell)]

    def _candidates(self, rect):
        found = set()
        for row, col in self._range(rect):
            found.update(self.memberships[self.cells[row*self.width+col]])
        return found

    def contacts(self, rect):
        """Exact shape intersections, with unknown geometry conservatively bounded."""
        rect = _rect(rect)
        return [self.sources[i] for i in sorted(self._candidates(rect))
                if any(_hits(part, rect) for part in self.parts[i])]

    def save(self, path):
        rows = []
        for row in range(self.height):
            runs = []
            for value in self.cells[row*self.width:(row+1)*self.width]:
                if runs and runs[-1][1] == value:
                    runs[-1][0] += 1
                else:
                    runs.append([1, value])
            rows.append(runs)
        _write(path, {'format': 'collision-matrix-v1', **self.identity, 'bounds': self.bounds,
                      'cell': self.cell, 'size': [self.width, self.height],
                      'row_encoding': '[run_length,membership_index]', 'memberships': self.memberships,
                      'sources': self.sources, 'rows': rows, 'player': self.player, 'exits': self.exits})

    def policy(self, source, policies):
        mode = policies.get(source['key'], policies.get(source.get('type'),
                            policies.get(source.get('kind'))))
        if mode is not None:
            if mode not in ('block', 'pass', 'one_way', 'unknown'):
                raise ValueError(f'invalid policy {mode}')
            return mode
        kind = source.get('kind')
        if kind == 'hazard':
            return 'block'  # Far-away hazards may enable when approached.
        if source.get('collidable') is False or kind == 'sensor':
            return 'pass'
        return {'solid': 'block', 'one_way': 'one_way'}.get(kind, 'unknown')

    def body(self):
        pos, box = self.player.get('pos'), self.player.get('hitbox')
        if not pos or not box:
            raise ValueError('provide body=[left_offset,top_offset,width,height]')
        return [box[0]-pos[0], box[1]-pos[1], box[2], box[3]]

    def transition(self, start, end, body=None, policies=None):
        """Axis-aligned swept-body test. Unknown sources conservatively block."""
        body = _rect(body if body is not None else self.body())
        policies = policies or {}
        if start[0] != end[0] and start[1] != end[1]:
            raise ValueError('axis-aligned transitions only')
        a = [start[0]+body[0], start[1]+body[1], body[2], body[3]]
        b = [end[0]+body[0], end[1]+body[1], body[2], body[3]]
        swept = [min(a[0], b[0]), min(a[1], b[1]), body[2]+abs(end[0]-start[0]),
                 body[3]+abs(end[1]-start[1])]
        bx, by, bw, bh = self.bounds
        outside = (swept[0] < bx or swept[1] < by or swept[0]+swept[2] > bx+bw
                   or swept[1]+swept[3] > by+bh)
        blockers = []
        for i in sorted(self._candidates(swept)):
            source = self.sources[i]
            mode = self.policy(source, policies)
            if mode == 'pass':
                continue
            for part in self.parts[i]:
                if mode == 'one_way':
                    # Unknown/non-upward platform semantics require an explicit policy.
                    if part[0] != 'rect' or source.get('direction', source.get('dir', 'up')) not in ('up', 'Up', 'U'):
                        hit = _hits(part, swept)
                    else:
                        x, top, w, _ = part[1]
                        hit = (b[1]+b[3] > a[1]+a[3] and a[1]+a[3] <= top
                               and b[1]+b[3] > top and b[0] < x+w and b[0]+b[2] > x)
                else:
                    hit = _hits(part, swept)
                if hit:
                    blockers.append(source['key'])
                    break
        return {'clear': not outside and not blockers, 'outside_coverage': outside,
                'blockers': blockers}

    def route(self, start, goal, *, body=None, policies=None, step=None, tolerance=0,
              max_nodes=100_000):
        """A* spatial corridor only: no gravity, stamina, jump or hazard-time proof."""
        started = time.perf_counter()
        body = _rect(body if body is not None else self.body())
        step = self.cell if step is None else step
        if type(step) not in (int, float) or not math.isfinite(step) or step <= 0:
            raise ValueError('step must be positive')
        if (len(start) != 2 or len(goal) != 2 or any(type(v) not in (int, float)
                or not math.isfinite(v) for v in (*start, *goal)) or tolerance < 0
                or not math.isfinite(tolerance) or type(max_nodes) is not int or not 0 < max_nodes <= MAX_CELLS):
            raise ValueError('invalid route bounds')
        policies = policies or {}
        start, goal = list(start), list(goal)
        def position(node):
            return [start[0]+node[0]*step, start[1]+node[1]*step]
        def distance(point):
            return abs(point[0]-goal[0])+abs(point[1]-goal[1])
        frontier = [(distance(start), 0, (0, 0))]
        parents, costs = {(0, 0): None}, {(0, 0): 0}
        blocked = set()
        visited = 0
        target = None
        tail = []
        initial = self.transition(start, start, body, policies)
        if not initial['clear']:
            frontier = []
            blocked.update(initial['blockers'])
        while frontier and visited < max_nodes:
            _, cost, node = heapq.heappop(frontier)
            if cost != costs[node]:
                continue
            visited += 1
            point = position(node)
            if distance(point) <= tolerance:
                target = node
                break
            # Goals need not share the start's (often fractional) grid alignment.
            # Exact swept axis segments connect the final local gap without snapping.
            if distance(point) <= step*2:
                for corner in ([goal[0], point[1]], [point[0], goal[1]]):
                    if (self.transition(point, corner, body, policies)['clear']
                            and self.transition(corner, goal, body, policies)['clear']):
                        target, tail = node, [corner, goal]
                        break
                if target is not None:
                    break
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                neighbor = (node[0]+dx, node[1]+dy)
                next_cost = cost+step
                if next_cost >= costs.get(neighbor, math.inf):
                    continue
                dest = position(neighbor)
                test = self.transition(point, dest, body, policies)
                if not test['clear']:
                    blocked.update(test['blockers'])
                    continue
                costs[neighbor], parents[neighbor] = next_cost, node
                heapq.heappush(frontier, (next_cost+distance(dest), next_cost, neighbor))
        path = []
        node = target
        while node is not None:
            path.append(position(node))
            node = parents[node]
        path.reverse()
        for point in tail:
            if path[-1] != point:
                path.append(point)
        blockers = [s for s in self.sources if s['key'] in blocked]
        warnings = ['spatial corridor only; validate movement and changing entities with actions/simulation',
                    'no sampled path does not prove the room is unreachable',
                    'blocking frontier includes explored obstacles, not a minimal separating cut']
        if self.identity.get('scope') != 'room' or not self.identity.get('complete') or not self.identity.get('geometryComplete'):
            warnings.append('partial coverage or unknown geometry; result is conditional')
        return {'snapshot': self.identity, 'status': 'spatial_path' if path else (
                    'invalid_start' if not initial['clear'] else 'budget_exhausted' if frontier else 'no_sampled_path'),
                'path': path, 'step': step, 'body': body, 'policies': policies, 'visited': visited,
                'physical_reachability': 'unverified',
                'elapsed_ms': (time.perf_counter()-started)*1000,
                'blocking_sources': [s['key'] for s in blockers],
                'unknown_sources': [s['key'] for s in blockers if self.policy(s, policies) == 'unknown'],
                'entity_frontier': [s['key'] for s in blockers if s.get('source') == 'entity'],
                'warnings': warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--obs', default=str(Path(__file__).with_name('latest_obs.json')))
    parser.add_argument('--out', default=None)
    parser.add_argument('--cell', type=int, default=2)
    args = parser.parse_args()
    matrix = Matrix.from_file(args.obs, args.cell)
    output = args.out or str(Path(args.obs).with_name('collision_matrix.json'))
    matrix.save(output)
    print(json.dumps({'file': output, **matrix.identity, 'size': [matrix.width, matrix.height],
                      'sources': len(matrix.sources), 'build_ms': matrix.build_ms}, ensure_ascii=False))


if __name__ == '__main__':
    main()
