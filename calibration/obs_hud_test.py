"""Preserve custom OBS layouts while migrating legacy full-canvas HUD sources."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.obs_setup import load_config, scene_collection, sync_hud_size


class ObsHudTests(unittest.TestCase):
    def test_new_scene_hud_is_unlocked_and_uses_card_dimensions(self):
        cfg = load_config()
        data = scene_collection(cfg)
        hud = next(s for s in data["sources"] if s["name"] == "HUD")
        scene = next(s for s in data["sources"] if s["id"] == "scene")
        item = next(i for i in scene["settings"]["items"] if i["name"] == "HUD")
        self.assertLess(hud["settings"]["width"], cfg["canvas"][0])
        self.assertFalse(item["locked"])

    def test_migration_changes_only_hud_and_retains_future_custom_layout(self):
        cfg = load_config()
        data = scene_collection(cfg)
        hud = next(s for s in data["sources"] if s["name"] == "HUD")
        scene = next(s for s in data["sources"] if s["id"] == "scene")
        item = next(i for i in scene["settings"]["items"] if i["name"] == "HUD")
        hud["settings"].update(width=1920, height=1080)
        item.update(pos={"x": 0.0, "y": 0.0}, locked=True, pos_rel={"x": -1.77, "y": -1})
        original_other = copy.deepcopy([s for s in data["sources"] if s["id"] != "scene" and s["name"] != "HUD"])
        with tempfile.TemporaryDirectory() as folder, patch("tools.obs_setup.obs_running", return_value=False):
            path = Path(folder) / "scene.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertTrue(sync_hud_size(path, cfg))
            migrated = json.loads(path.read_text(encoding="utf-8"))
            other = [s for s in migrated["sources"] if s["id"] != "scene" and s["name"] != "HUD"]
            self.assertEqual(other, original_other)
            scene = next(s for s in migrated["sources"] if s["id"] == "scene")
            item = next(i for i in scene["settings"]["items"] if i["name"] == "HUD")
            self.assertFalse(item["locked"])
            self.assertNotIn("pos_rel", item)
            item.update(pos={"x": 80, "y": 200}, scale={"x": .8, "y": .8})
            hud = next(s for s in migrated["sources"] if s["name"] == "HUD")
            hud["settings"].update(width=500, height=820)
            path.write_text(json.dumps(migrated), encoding="utf-8")
            self.assertFalse(sync_hud_size(path, cfg))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), migrated)


if __name__ == "__main__":
    unittest.main()
