"""Collision pictures preserve actual shapes, current identity and bounded output."""
import copy
import io
import unittest
from unittest.mock import patch

from PIL import Image

from orchestrator.collision_view import (
    BACKGROUND, DISABLED, ENTITY_SOLID, HAZARD, TERRAIN, MAX_EDGE,
    render_collision_view,
)


def rectangle(x, y, w, h):
    return {'shape': 'rect', 'x': x, 'y': y, 'w': w, 'h': h}


def snapshot(objects, frame=12, bounds=None):
    return {'frame': frame, 'level': {'sid': 'Celeste/3-CelestialResort', 'room': 's3'},
            'collision': {'version': 1, 'frame': frame, 'sid': 'Celeste/3-CelestialResort', 'room': 's3',
                          'scope': 'room', 'bounds': bounds or [0, 0, 320, 180],
                          'coverage': {'terrain': True, 'entities': True},
                          'complete': True, 'geometryComplete': True, 'objects': objects,
                          'player': {'hitbox': [280, 140, 8, 11]}}}


def object_at(geometry, *, kind='solid', source='entity', collidable=True, ident=171):
    return {'id': ident, 'source': source, 'type': 'Solid', 'kind': kind,
            'collidable': collidable, 'geometry': geometry}


class CollisionViewTests(unittest.TestCase):
    def render(self, obs):
        data = render_collision_view(obs)
        self.assertIsInstance(data, bytes)
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.format, 'PNG')
        image.load()
        return image

    def at(self, image, x, y):
        # For a 320x180 room, the view uses 2x world pixels with 24/92 padding.
        return image.getpixel((round(24 + x * 2), round(92 + y * 2)))

    def test_terrain_and_entity_brick_are_both_visible(self):
        obs = snapshot([object_at(rectangle(16, 16, 24, 24), source='terrain'),
                        object_at(rectangle(80, 16, 24, 24))])
        image = self.render(obs)
        self.assertEqual(self.at(image, 28, 30), TERRAIN)
        self.assertEqual(self.at(image, 92, 30), ENTITY_SOLID)

    def test_circle_and_composite_shapes_do_not_fill_their_bounds(self):
        circle = {'shape': 'circle', 'x': 64, 'y': 16, 'w': 16, 'h': 16, 'r': 8}
        parts = {'shape': 'list', 'x': 108, 'y': 16, 'w': 40, 'h': 24,
                 'parts': [rectangle(108, 16, 8, 24), rectangle(140, 16, 8, 24)]}
        obs = snapshot([object_at(circle, kind='hazard'), object_at(parts, kind='hazard')])
        image = self.render(obs)
        self.assertEqual(self.at(image, 72, 24), HAZARD)
        self.assertNotEqual(self.at(image, 65, 17), HAZARD)
        self.assertEqual(self.at(image, 112, 32), HAZARD)
        self.assertNotEqual(self.at(image, 128, 32), HAZARD)

    def test_grid_uses_occupied_parts_and_unknown_never_becomes_a_solid_box(self):
        grid = {'shape': 'grid', 'x': 16, 'y': 80, 'w': 24, 'h': 16,
                'parts': [rectangle(16, 80, 8, 16), rectangle(32, 80, 8, 16)]}
        unknown = {'shape': 'unknown', 'x': 80, 'y': 80, 'w': 32, 'h': 24}
        image = self.render(snapshot([object_at(grid), object_at(unknown)]))
        self.assertEqual(self.at(image, 20, 90), ENTITY_SOLID)
        self.assertNotEqual(self.at(image, 28, 90), ENTITY_SOLID)
        self.assertNotEqual(self.at(image, 96, 92), ENTITY_SOLID)

    def test_disabled_solid_sensor_and_one_way_are_not_filled_as_walls(self):
        obs = snapshot([object_at(rectangle(16, 16, 24, 24), collidable=False),
                        object_at(rectangle(80, 16, 24, 24), kind='sensor'),
                        object_at(rectangle(140, 16, 24, 24), kind='one_way')])
        image = self.render(obs)
        self.assertNotIn(self.at(image, 28, 30), {ENTITY_SOLID, DISABLED})
        self.assertNotEqual(self.at(image, 92, 30), ENTITY_SOLID)
        self.assertNotEqual(self.at(image, 152, 30), ENTITY_SOLID)

    def test_moving_and_removed_entities_do_not_leave_stale_geometry(self):
        original = snapshot([object_at(rectangle(16, 16, 24, 24))])
        moved = snapshot([object_at(rectangle(80, 16, 24, 24))], frame=13)
        first, second = self.render(original), self.render(moved)
        self.assertEqual(self.at(first, 28, 30), ENTITY_SOLID)
        self.assertNotEqual(self.at(second, 28, 30), ENTITY_SOLID)
        self.assertEqual(self.at(second, 92, 30), ENTITY_SOLID)
        third = self.render(snapshot([], frame=14))
        self.assertNotEqual(self.at(third, 92, 30), ENTITY_SOLID)

    def test_missing_stale_or_other_room_snapshot_is_not_drawn(self):
        self.assertIsNone(render_collision_view({}))
        for change in ({'frame': 11}, {'sid': 'other'}, {'room': 'other'}):
            with self.subTest(change=change):
                obs = snapshot([])
                obs['collision'].update(change)
                self.assertIsNone(render_collision_view(obs))

    def test_partial_view_remains_renderable_without_claiming_full_room(self):
        obs = snapshot([object_at(rectangle(16, 16, 24, 24))])
        obs['collision'].update(scope='view', complete=False, geometryComplete=False,
                                coverage={'terrain': False, 'entities': True})
        partial = self.render(obs)
        full = copy.deepcopy(obs)
        full['collision'].update(scope='room', complete=True, geometryComplete=True,
                                 coverage={'terrain': True, 'entities': True})
        self.assertNotEqual(partial.tobytes(), self.render(full).tobytes())

    def test_huge_room_and_recursive_geometry_have_bounded_output(self):
        shape = rectangle(0, 0, 8, 8)
        for _ in range(30):
            shape = {'shape': 'list', 'x': 0, 'y': 0, 'w': 8, 'h': 8, 'parts': [shape]}
        image = self.render(snapshot([object_at(shape)], bounds=[-100000000, -100000000, 200000000, 200000000]))
        self.assertLessEqual(max(image.size), MAX_EDGE)
        # Floating-point rounding of scale can otherwise ceil 1232px to 1233px.
        image = self.render(snapshot([], bounds=[0, 0, 8905, 180]))
        self.assertLessEqual(max(image.size), MAX_EDGE)
        with patch('orchestrator.collision_view.MAX_PRIMITIVES', 2):
            image = self.render(snapshot([object_at(rectangle(i * 8, 0, 8, 8)) for i in range(100)]))
            self.assertLessEqual(max(image.size), MAX_EDGE)


if __name__ == '__main__':
    unittest.main()
