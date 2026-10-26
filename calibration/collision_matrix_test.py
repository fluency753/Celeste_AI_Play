"""Collision matrix behavior checks without a game connection."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.workspace_files import collision_matrix as cm

def obj(ident, x, y, w, h, *, source="entity", kind="solid", shape=None, **extra):
    return {"id": ident, "source": source, "type": "Test", "kind": kind,
            "collidable": True, "mutable": source == "entity",
            "geometry": shape or {"shape": "rect", "x": x, "y": y, "w": w, "h": h}, **extra}

def observation(objects=(), bounds=(0, 0, 40, 40)):
    return {"frame": 1, "level": {"sid": "test", "room": "a"},
            "player": {"pos": [3, 3], "hitbox": [2, 1, 2, 2]}, "entities": [],
            "collision": {"version": 1, "frame": 1, "sid": "test", "room": "a",
                          "scope": "room", "bounds": list(bounds),
                          "coverage": {"terrain": True, "entities": True},
                          "complete": True, "geometryComplete": True, "objects": list(objects)}}

class CollisionMatrixTest(unittest.TestCase):

    def test_cells_keep_state_and_kind_without_traversal_decisions(self):
        objects = [obj(1, 10, 10, 8, 8, kind="hazard", collidable=False),
                   obj(2, 10, 10, 8, 8, kind="sensor"),
                   obj(3, 10, 10, 8, 8, kind="one_way", direction="up"),
                   obj(4, 10, 10, 8, 8, kind="unknown")]
        matrix = cm.Matrix(observation(objects))
        sources = matrix.cell_sources(6, 6)
        self.assertEqual([source['id'] for source in sources], [1, 2, 3, 4])
        self.assertFalse(sources[0]['collidable'])
        self.assertEqual(sources[2]['direction'], 'up')
        self.assertEqual(matrix.contacts([12, 12, 1, 1]), sources)
        for name in ('policy', 'transition', 'route', 'body'):
            self.assertFalse(hasattr(matrix, name), name)

    def test_cell_indices_reject_negative_fractional_and_outside(self):
        matrix = cm.Matrix(observation())
        for row, col in [(-1, 0), (0, -1), (True, 0), (0, 1.5), (20, 0), (0, 20)]:
            with self.subTest(row=row, col=col), self.assertRaises(ValueError):
                matrix.cell_sources(row, col)

    def test_unknown_shape_is_preserved_as_range_candidate(self):
        shape = {'shape': 'custom', 'x': 10, 'y': 10, 'w': 4, 'h': 4}
        obs = observation([obj(1, 0, 0, 0, 0, shape=shape)])
        obs['collision'].update(scope='view', geometryComplete=False, complete=False,
                                coverage={'terrain': False, 'entities': True})
        matrix = cm.Matrix(obs)
        self.assertEqual(matrix.contacts([11, 11, 1, 1])[0]['geometry'], shape)
        self.assertFalse(matrix.identity['geometryComplete'])
        self.assertEqual(matrix.identity['scope'], 'view')
        self.assertFalse(matrix.identity['coverage']['terrain'])

    def test_input_mutation_does_not_change_owned_snapshot(self):
        obs = observation([obj(1, 10, 10, 8, 8)])
        obs['level']['camera'] = [0, 0]
        obs['entities'] = [{'id': 1, 'fields': {'enabled': True}}]
        matrix = cm.Matrix(obs)
        obs['collision']['objects'][0]['geometry']['x'] = 30
        obs['player']['pos'][0] = 30
        obs['level']['camera'][0] = 30
        obs['entities'][0]['fields']['enabled'] = False
        self.assertEqual(matrix.contacts([11, 11, 1, 1])[0]['geometry']['x'], 10)
        self.assertEqual(matrix.player['pos'][0], 3)
        self.assertEqual(matrix.level['camera'], [0, 0])
        self.assertTrue(matrix.sources[0]['entity']['fields']['enabled'])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'matrix.json'
            matrix.save(path)
            loaded = cm.Matrix.load(path)
        self.assertEqual(loaded.level, matrix.level)
        self.assertEqual(loaded.sources, matrix.sources)

    def test_terrain_entity_join_reports_both_geometries_without_classifying_passage(self):
        matrix = cm.Matrix(observation([obj(1, 15, 0, 4, 20, source="terrain"),
                                        obj(2, 15, 20, 4, 20)]))
        sources = matrix.contacts([15, 19, 4, 2])
        self.assertEqual([source["key"] for source in sources], ["terrain:1", "entity:2"])
        self.assertEqual(sources[0]["geometry"]["y"] + sources[0]["geometry"]["h"],
                         sources[1]["geometry"]["y"])

    def test_identity_includes_camera_and_exit_data(self):
        obs = observation()
        first = cm.Matrix(obs).identity['snapshot_id']
        obs['level']['camera'] = [10, 20]
        second = cm.Matrix(obs).identity['snapshot_id']
        obs['exits'] = [{'side': 'left', 'room': 'b'}]
        third = cm.Matrix(obs).identity['snapshot_id']
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)

    def test_overlap_keeps_every_source(self):
        matrix = cm.Matrix(observation([obj(1, 10, 10, 8, 8), obj(2, 12, 12, 8, 8)]))
        self.assertEqual([s["id"] for s in matrix.contacts([13, 13, 1, 1])], [1, 2])

    def test_grid_holes_are_not_filled_by_bounds(self):
        geometry = {"shape": "grid", "x": 8, "y": 8, "w": 16, "h": 8,
                    "parts": [{"shape": "rect", "x": 8, "y": 8, "w": 4, "h": 8},
                              {"shape": "rect", "x": 20, "y": 8, "w": 4, "h": 8}]}
        matrix = cm.Matrix(observation([obj(1, 0, 0, 0, 0, shape=geometry)]), cell=8)
        self.assertEqual(matrix.contacts([14, 10, 2, 2]), [])
        self.assertEqual(len(matrix.contacts([9, 10, 1, 1])), 1)

    def test_circle_bounding_box_corner_is_empty(self):
        shape = {"shape": "circle", "x": 10, "y": 10, "w": 10, "h": 10, "r": 5}
        matrix = cm.Matrix(observation([obj(1, 0, 0, 0, 0, shape=shape)]), cell=8)
        self.assertEqual(matrix.contacts([10, 10, 1, 1]), [])
        self.assertEqual(len(matrix.contacts([14, 14, 1, 1])), 1)

    def test_file_contains_same_snapshot_sources_and_rows(self):
        matrix = cm.Matrix(observation([obj(1, 10, 10, 8, 8)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            matrix.save(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["snapshot_id"], matrix.identity["snapshot_id"])
            self.assertEqual(saved["sources"], matrix.sources)
            for row in saved["rows"]:
                self.assertEqual(sum(run[0] for run in row), matrix.width)
                self.assertTrue(all(run[1] < len(saved["memberships"]) for run in row))
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_limits_fail_before_allocating_unbounded_matrix(self):
        with patch.object(cm, "MAX_CELLS", 5), self.assertRaises(ValueError):
            cm.Matrix(observation())
        with patch.object(cm, "MAX_REFERENCES", 2), self.assertRaises(ValueError):
            cm.Matrix(observation([obj(1, 0, 0, 40, 40)]))

    def test_export_load_roundtrip_preserves_queries_and_identity(self):
        matrix = cm.Matrix(observation([obj(1, 15, 0, 4, 40)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            matrix.save(path)
            loaded = cm.Matrix.load(path)
        self.assertEqual(loaded.identity, matrix.identity)
        self.assertEqual(loaded.sources, matrix.sources)
        self.assertEqual(loaded.cells, matrix.cells)
        self.assertEqual(loaded.contacts([15, 4, 2, 2]),
                         matrix.contacts([15, 4, 2, 2]))

    def test_export_membership_numbering_can_change_without_changing_geometry(self):
        matrix = cm.Matrix(observation([obj(1, 10, 10, 8, 8), obj(2, 12, 12, 8, 8)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            matrix.save(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            count = len(value["memberships"])
            value["memberships"] = value["memberships"][::-1]
            for row in value["rows"]:
                for run in row:
                    run[1] = count - 1 - run[1]
            path.write_bytes(json.dumps(value).encode("utf-8"))
            loaded = cm.Matrix.load(path)
        self.assertEqual([s["key"] for s in loaded.contacts([13, 13, 1, 1])],
                         [s["key"] for s in matrix.contacts([13, 13, 1, 1])])

    def test_duplicate_source_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            cm.Matrix(observation([obj(1, 10, 10, 8, 8), obj(1, 20, 20, 8, 8)]))

    def test_corrupt_export_cannot_hide_occupied_cells(self):
        matrix = cm.Matrix(observation([obj(1, 10, 10, 8, 8)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            matrix.save(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["rows"] = [[[matrix.width, 0]] for _ in range(matrix.height)]
            path.write_bytes(json.dumps(value).encode("utf-8"))
            with self.assertRaises(ValueError):
                cm.Matrix.load(path)

    def test_corrupt_export_cannot_hide_source_membership(self):
        matrix = cm.Matrix(observation([obj(1, 10, 10, 8, 8)]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.json"
            matrix.save(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["memberships"] = [[]]
            path.write_bytes(json.dumps(value).encode("utf-8"))
            with self.assertRaises(ValueError):
                cm.Matrix.load(path)

    def test_repeated_parts_have_work_budget_even_when_memberships_deduplicate(self):
        rect = {"shape": "rect", "x": 0, "y": 0, "w": 40, "h": 40}
        shape = {"shape": "list", "x": 0, "y": 0, "w": 40, "h": 40,
                 "parts": [rect, rect, rect]}
        with patch.object(cm, "MAX_RASTER_VISITS", 500), self.assertRaisesRegex(ValueError, "work budget"):
            cm.Matrix(observation([obj(1, 0, 0, 0, 0, shape=shape)]))

    def test_invalid_coverage_is_rejected(self):
        for field, value in (("coverage", {"terrain": True, "entities": 1}),
                             ("complete", False), ("geometryComplete", None)):
            with self.subTest(field=field):
                obs = observation()
                obs["collision"][field] = value
                with self.assertRaises(ValueError):
                    cm.Matrix(obs)

    def test_mismatched_frame_rejected(self):
        obs = observation()
        obs["collision"]["frame"] = 2
        with self.assertRaises(ValueError):
            cm.Matrix(obs)

    def test_missing_snapshot_identity_rejected(self):
        with self.assertRaises(ValueError):
            cm.Matrix({"collision": {"version": 1, "scope": "room", "bounds": [0, 0, 4, 4], "objects": []}})

if __name__ == "__main__":
    unittest.main()
