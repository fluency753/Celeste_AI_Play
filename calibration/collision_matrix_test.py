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
    def test_terrain_entity_seam_blocks_body(self):
        matrix = cm.Matrix(observation([obj(1, 15, 0, 4, 20, source="terrain"),
                                        obj(2, 15, 20, 4, 20)]))
        result = matrix.transition([10, 20], [23, 20], body=[-1, -1, 2, 2])
        self.assertFalse(result["clear"])
        self.assertEqual(set(result["blockers"]), {"terrain:1", "entity:2"})

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

    def test_body_width_blocks_narrow_gap(self):
        matrix = cm.Matrix(observation([obj(1, 0, 15, 18, 4), obj(2, 22, 15, 18, 4)]))
        self.assertTrue(matrix.transition([20, 10], [20, 25], body=[-1, -1, 2, 2])["clear"])
        self.assertFalse(matrix.transition([20, 10], [20, 25], body=[-3, -1, 6, 2])["clear"])

    def test_long_axis_move_sweeps_intermediate_wall(self):
        matrix = cm.Matrix(observation([obj(1, 20, 0, 1, 40)]))
        self.assertFalse(matrix.transition([5, 5], [30, 5], body=[0, 0, 1, 1])["clear"])
        with self.assertRaises(ValueError):
            matrix.transition([5, 5], [30, 30], body=[0, 0, 1, 1])

    def test_one_way_blocks_only_downward_crossing(self):
        matrix = cm.Matrix(observation([obj(1, 0, 20, 40, 4, kind="one_way")]))
        self.assertFalse(matrix.transition([10, 15], [10, 25], body=[0, 0, 2, 2])["clear"])
        self.assertTrue(matrix.transition([10, 25], [10, 15], body=[0, 0, 2, 2])["clear"])
        self.assertTrue(matrix.transition([10, 18], [20, 18], body=[0, 0, 2, 2])["clear"])

    def test_unknown_is_explicit_and_policy_can_override(self):
        matrix = cm.Matrix(observation([obj(1, 20, 0, 1, 40, kind="unknown")]))
        result = matrix.route([5, 5], [30, 5], body=[0, 0, 1, 1])
        self.assertIn("entity:1", result["unknown_sources"])
        self.assertEqual(result["status"], "no_sampled_path")
        self.assertEqual(matrix.route([5, 5], [30, 5], body=[0, 0, 1, 1],
                                     policies={"entity:1": "pass"}, step=1)["status"], "spatial_path")

    def test_inactive_hazards_not_assumed_permanently_safe(self):
        matrix = cm.Matrix(observation([obj(1, 20, 0, 1, 40, kind="hazard", collidable=False)]))
        self.assertFalse(matrix.transition([5, 5], [30, 5], body=[0, 0, 1, 1])["clear"])

    def test_partial_coverage_and_search_budget_are_reported(self):
        obs = observation()
        obs["collision"].update(scope="view", complete=False, geometryComplete=False,
                                 coverage={"terrain": True, "entities": False})
        result = cm.Matrix(obs).route([3, 3], [30, 30], max_nodes=1)
        self.assertEqual(result["status"], "budget_exhausted")
        self.assertTrue(any("partial coverage" in item for item in result["warnings"]))

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
        self.assertEqual(loaded.transition([4, 4], [30, 4]),
                         matrix.transition([4, 4], [30, 4]))

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

    def test_unknown_geometry_uses_bounds_conservatively(self):
        shape = {"shape": "custom", "x": 15, "y": 0, "w": 4, "h": 40}
        obs = observation([obj(1, 0, 0, 0, 0, shape=shape)])
        obs["collision"]["geometryComplete"] = False
        matrix = cm.Matrix(obs)
        self.assertFalse(matrix.transition([5, 5], [30, 5], body=[0, 0, 1, 1])["clear"])
        result = matrix.route([5, 5], [30, 5], body=[0, 0, 1, 1])
        self.assertTrue(any("unknown geometry" in item for item in result["warnings"]))

    def test_nonupward_oneway_requires_explicit_policy(self):
        matrix = cm.Matrix(observation([obj(1, 15, 0, 4, 40, kind="one_way", direction="left")]))
        self.assertFalse(matrix.transition([5, 5], [30, 5], body=[0, 0, 1, 1])["clear"])
        self.assertTrue(matrix.transition([5, 5], [30, 5], body=[0, 0, 1, 1],
                                          policies={"entity:1": "pass"})["clear"])

    def test_exact_goal_tail_does_not_cut_diagonally_through_wall(self):
        matrix = cm.Matrix(observation([obj(1, 4, 0, 1, 5), obj(2, 0, 4, 5, 1)]))
        result = matrix.route([2, 2], [5, 5], body=[0, 0, 1, 1], step=4, max_nodes=100)
        self.assertEqual(result["status"], "no_sampled_path")
        self.assertEqual(result["path"], [])

    def test_invalid_search_bounds_rejected(self):
        matrix = cm.Matrix(observation())
        for kwargs in ({"step": 0}, {"step": float("nan")}, {"max_nodes": True},
                       {"max_nodes": 1.5}, {"max_nodes": 0}, {"tolerance": -1},
                       {"tolerance": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                matrix.route([3, 3], [20, 20], **kwargs)

    def test_mismatched_frame_rejected(self):
        obs = observation()
        obs["collision"]["frame"] = 2
        with self.assertRaises(ValueError):
            cm.Matrix(obs)

    def test_missing_snapshot_identity_rejected(self):
        with self.assertRaises(ValueError):
            cm.Matrix({"collision": {"version": 1, "scope": "room", "bounds": [0, 0, 4, 4], "objects": []}})

    def test_goal_between_grid_points_can_be_connected_exactly(self):
        matrix = cm.Matrix(observation())
        result = matrix.route([3.25, 3], [6.25, 3], body=[0, 0, 1, 1], max_nodes=100)
        self.assertEqual(result["status"], "spatial_path")
        self.assertEqual(result["path"][-1], [6.25, 3])


if __name__ == "__main__":
    unittest.main()
