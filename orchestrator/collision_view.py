"""Render an authoritative, same-frame collision snapshot without route judgements."""
from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont
from .collision_snapshot import current_collision


BACKGROUND = (20, 24, 32)
TERRAIN = (101, 111, 127)
ENTITY_SOLID = (74, 143, 234)
ONE_WAY = (68, 218, 226)
HAZARD = (244, 91, 107)
SENSOR = (119, 211, 139)
DISABLED = (111, 112, 123)
UNKNOWN = (255, 199, 87)
PLAYER = (247, 249, 255)
MAX_EDGE = 1280
MAX_PRIMITIVES = 16000
MAX_DEPTH = 12
MARGIN, HEADER, FOOTER = 24, 68, 88


def _numbers(value, count):
    if not isinstance(value, (tuple, list)) or len(value) != count:
        return None
    try:
        valid = all(not isinstance(v, bool) and isinstance(v, (float, int)) and math.isfinite(v) for v in value)
    except OverflowError:
        valid = False
    if not valid:
        return None
    return value


def render_collision_view(obs: dict) -> bytes | None:
    """Return PNG bytes, or None when geometry is missing, stale or has invalid bounds.

    Limits bound raster size and drawing work. A drawing limit is visible in the
    result, never presented as a complete empty room. No data is retained between
    calls, so moving/removed/disabled entities cannot leave stale obstacles.
    """
    collision = current_collision(obs)
    if collision is None:
        return None
    bounds = _numbers(collision.get('bounds'), 4)
    objects = collision.get('objects')
    if (not bounds or bounds[2] <= 0 or bounds[3] <= 0 or not isinstance(objects, list)
            or not all(math.isfinite(bounds[i] + bounds[i + 2]) for i in (0, 1))):
        return None
    renderer = _Renderer(collision, bounds)
    return renderer.render(objects)


class _Renderer:
    def __init__(self, snapshot, bounds):
        self.snapshot, self.bounds = snapshot, bounds
        x, y, w, h = bounds
        self.scale = min(2.0, (MAX_EDGE - 2 * MARGIN) / w,
                         (MAX_EDGE - HEADER - FOOTER - 2 * MARGIN) / h)
        self.width = min(MAX_EDGE, max(640, math.ceil(w * self.scale) + 2 * MARGIN))
        self.height = min(MAX_EDGE, max(240, math.ceil(h * self.scale) + HEADER + FOOTER + 2 * MARGIN))
        self.left = (self.width - w * self.scale) / 2
        self.top = HEADER + MARGIN
        self.image = Image.new('RGB', (self.width, self.height), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.font = ImageFont.load_default(size=13)
        self.count, self.limited, self.unknown = 0, False, 0
        self.labels, self.label_cells = [], set()

    def point(self, x, y):
        return self.left + (x - self.bounds[0]) * self.scale, self.top + (y - self.bounds[1]) * self.scale

    def box(self, geometry):
        values = _numbers([geometry.get(k) for k in ('x', 'y', 'w', 'h')], 4)
        if not values or values[2] < 0 or values[3] < 0:
            return None
        x, y, w, h = values
        bx, by, bw, bh = self.bounds
        if x + w < bx or x > bx + bw or y + h < by or y > by + bh:
            return None
        # Rectangle bounds can be clipped exactly before conversion; huge world
        # coordinates must not be passed to the rasterizer or allocated as images.
        p = self.point(max(x, bx), max(y, by))
        q = self.point(min(x + w, bx + bw), min(y + h, by + bh))
        return p + q

    def dashed_line(self, a, b, color):
        length = math.dist(a, b)
        if not length:
            return
        for start in range(0, math.ceil(length), 9):
            stop = min(length, start + 5)
            p = tuple(a[i] + (b[i] - a[i]) * start / length for i in (0, 1))
            q = tuple(a[i] + (b[i] - a[i]) * stop / length for i in (0, 1))
            self.draw.line((p, q), fill=color, width=1)

    def outline(self, box, color):
        x, y, xx, yy = box
        points = [(x, y), (xx, y), (xx, yy), (x, yy), (x, y)]
        for a, b in zip(points, points[1:]):
            self.dashed_line(a, b, color)

    def geometry(self, shape, color, fill, dashed=False, depth=0):
        if not isinstance(shape, dict):
            self.unknown += 1
            return
        if self.count >= MAX_PRIMITIVES:
            self.limited = True
            return
        self.count += 1
        if depth > MAX_DEPTH:
            self.limited = True
            return
        kind = shape.get('shape')
        if kind in {'list', 'grid'} and isinstance(shape.get('parts'), list):
            for part in shape['parts']:
                self.geometry(part, color, fill, dashed, depth + 1)
                if self.count >= MAX_PRIMITIVES:
                    break
            return
        box = self.box(shape)
        if box is None:
            return
        if kind == 'rect':
            if dashed:
                self.outline(box, color)
            else:
                self.draw.rectangle(box, fill=color if fill else None, outline=color, width=1)
        elif kind == 'circle':
            numbers = _numbers([shape.get(k) for k in ('x', 'y', 'w', 'h', 'r')], 5)
            if not numbers or numbers[4] <= 0:
                self.unknown += 1
                self.outline(box, UNKNOWN)
                return
            x, y, w, h, radius = numbers
            if abs(w - 2 * radius) > .01 or abs(h - 2 * radius) > .01:
                self.unknown += 1
                self.outline(box, UNKNOWN)
                return
            # Draw only bounded visible scanlines, preserving circles that cross
            # the room edge instead of distorting their clipped box into an oval.
            cx, cy = self.point(x + radius, y + radius)
            radius *= self.scale
            if not all(math.isfinite(v) for v in (cx, cy, radius, cy - radius, cy + radius)):
                self.unknown += 1
                self.outline(box, UNKNOWN)
                return
            top = max(math.floor(cy - radius), math.floor(self.top))
            bottom = min(math.ceil(cy + radius), math.ceil(self.top + self.bounds[3] * self.scale))
            for py in range(top, bottom + 1):
                dy = py - cy
                if abs(dy) > radius:
                    continue
                dx = math.sqrt(max(0, (radius - dy) * (radius + dy)))
                a, b = max(self.left, cx - dx), min(self.left + self.bounds[2] * self.scale, cx + dx)
                if a > b:
                    continue
                if fill and not dashed:
                    self.draw.line((a, py, b, py), fill=color)
                else:
                    for px in (cx - dx, cx + dx):
                        if self.left <= px <= self.left + self.bounds[2] * self.scale and (not dashed or (py // 4) % 2 == 0):
                            self.draw.point((px, py), fill=color)
        else:
            self.unknown += 1
            self.outline(box, UNKNOWN)

    def label(self, obj):
        shape = obj.get('geometry') or {}
        box = self.box(shape)
        if not box or obj.get('source') != 'entity' or len(self.labels) >= 80:
            return
        x, y, _, bottom = box
        y = y - 15 if y - self.top >= 15 else bottom + 2
        cell = (int(x // 55), int(y // 18))
        if cell not in self.label_cells:
            self.label_cells.add(cell)
            self.labels.append((x, y, str(obj.get('id', '?'))[:14]))

    def render(self, objects):
        s = self.snapshot
        bx, by, bw, bh = self.bounds
        self.draw.rectangle((self.left, self.top, self.left + bw * self.scale, self.top + bh * self.scale),
                            outline=(61, 70, 84))
        # Grid work stays bounded even when world-space bounds are enormous.
        step = 8 * max(1, math.ceil(24 / (8 * self.scale)))
        for index in range(math.ceil(bw / step) + 1):
            x, _ = self.point(bx + index * step, by)
            self.draw.line((x, self.top, x, self.top + bh * self.scale), fill=(31, 37, 47))
        for index in range(math.ceil(bh / step) + 1):
            _, y = self.point(bx, by + index * step)
            self.draw.line((self.left, y, self.left + bw * self.scale, y), fill=(31, 37, 47))
        ranks = {'solid': 0, 'one_way': 1, 'hazard': 2, 'sensor': 3, 'unknown': 4}
        # Bound ordering work separately from primitive drawing. A truncated view
        # is flagged prominently rather than silently advertised as complete.
        if len(objects) > MAX_PRIMITIVES:
            self.limited = True
        selected = [obj for obj in objects[:MAX_PRIMITIVES] if isinstance(obj, dict)]
        for obj in sorted(selected, key=lambda o: (ranks.get(o.get('kind'), 4), o.get('source') != 'terrain')):
            kind, enabled = obj.get('kind'), obj.get('collidable')
            color = {'solid': TERRAIN if obj.get('source') == 'terrain' else ENTITY_SOLID,
                     'one_way': ONE_WAY, 'hazard': HAZARD, 'sensor': SENSOR}.get(kind, UNKNOWN)
            if enabled is False:
                color = DISABLED
            self.geometry(obj.get('geometry'), color, fill=kind in {'solid', 'hazard'} and enabled is True,
                          dashed=enabled is not True or kind not in ranks or kind == 'unknown')
            self.label(obj)
            if self.limited and self.count >= MAX_PRIMITIVES:
                break
        for x, y, label in self.labels:
            self.draw.text((x + 2, y + 1), label, fill=PLAYER, stroke_width=1, stroke_fill=BACKGROUND, font=self.font)
        hitbox = _numbers((s.get('player') or {}).get('hitbox'), 4)
        if hitbox and hitbox[2] > 0 and hitbox[3] > 0:
            box = self.box(dict(zip(('x', 'y', 'w', 'h'), hitbox)))
            if box:
                self.draw.rectangle(box, fill=PLAYER, outline=(255, 237, 132), width=2)
        y = self.height - FOOTER + 5
        for i, (label, color) in enumerate([('terrain', TERRAIN), ('entity solid', ENTITY_SOLID),
                ('one-way', ONE_WAY), ('hazard', HAZARD), ('sensor', SENSOR), ('disabled', DISABLED)]):
            x = 16 + i % 3 * 200
            yy = y + i // 3 * 19
            self.draw.rectangle((x, yy + 2, x + 10, yy + 12), fill=color)
            self.draw.text((x + 16, yy), label, fill=PLAYER, font=self.font)
        self.draw.text((16, y + 42), f"world x=[{bx:g},{bx+bw:g}] y=[{by:g},{by+bh:g}]  "
                       f"objects={len(objects)}  ?shapes={self.unknown}", font=self.font, fill=PLAYER)
        note = 'DRAW LIMIT REACHED: some geometry is NOT shown' if self.limited else 'Entity IDs are spaced for readability; use collision.objects for all IDs.'
        self.draw.text((16, y + 60), note, font=self.font, fill=UNKNOWN if self.limited else PLAYER)
        # Header is written after drawing so local unsupported/limited shapes do
        # not inherit an upstream "exact" claim that this raster did not satisfy.
        text = (f"COLLISION SNAPSHOT  frame={s['frame']}  room={str(s.get('room', '?'))[:32]}  "
                f"scope={str(s.get('scope', '?'))[:12]}")
        self.draw.text((16, 9), text, font=self.font, fill=PLAYER)
        coverage = s.get('coverage') or {}
        exact = s.get('geometryComplete') is True and not self.unknown and not self.limited
        self.draw.text((16, 30), f"coverage={'complete' if s.get('complete') is True and not self.limited else 'PARTIAL / unknown'}  "
                       f"geometry={'exact' if exact else 'PARTIAL / unknown'}  "
                       f"terrain={coverage.get('terrain', '?')} entities={coverage.get('entities', '?')}",
                       font=self.font, fill=PLAYER)
        output = io.BytesIO()
        self.image.save(output, format='PNG')
        return output.getvalue()
