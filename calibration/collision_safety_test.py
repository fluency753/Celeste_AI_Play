"""碰撞图出错不影响工作区其余数据；同一帧的碰撞图只画一次。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics


class CollisionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        cfg = RunConfig(agent="t", save="test", model="m", runtime="host", mode="explore",
                        knowledge_profile="explore", chapter=4)
        self.s = GameSession(cfg, root, Metrics(root / "metrics.json", "t", "m", "high", 600))
        self.addCleanup(self.s.close, False)
        self.s.workspace = root / "workspace"
        (self.s.workspace / "game").mkdir(parents=True)
        self.obs = {"scene": "Level", "frame": 7, "controllable": True,
                    "level": {"sid": "Celeste/4-GoldenRidge", "room": "a-00"}}

    def test_collision_failure_does_not_skip_last_act(self):
        for error in (PermissionError("locked"), ValueError("bad shape")):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.s, "_write_collision_view", side_effect=error):
                    self.s._write_workspace(self.obs, {"decision": 3})
                last = json.loads((self.s.workspace / "game" / "last_act.json").read_text(encoding="utf-8"))
                self.assertEqual(last, {"decision": 3})
                self.assertTrue((self.s.workspace / "game" / "latest_obs.json").exists())

    def test_same_frame_is_rendered_once(self):
        with patch("orchestrator.game_session.render_collision_view", return_value=b"png") as render:
            self.assertEqual(self.s.collision_png(self.obs), b"png")
            self.assertEqual(self.s.collision_png(self.obs), b"png")
            self.assertEqual(render.call_count, 1)
            self.s.collision_png(dict(self.obs, frame=8))
            self.assertEqual(render.call_count, 2)


class FileModeTextTests(unittest.TestCase):
    def test_file_mode_still_reports_collectibles(self):
        from calibration.collision_contract_test import observation
        from orchestrator.observation import describe_obs, enrich_obs
        obs = enrich_obs(observation())
        obs["entities"].append({"id": 900, "type": "Strawberry", "cat": "collectible", "x": 40, "y": 0, "w": 14, "h": 14,
                                "collected": False})
        text = describe_obs(obs, full_room=False, explore=True, file_context=True)
        self.assertIn("Strawberry#900", text)
        self.assertIn("尚未确认收集", text)


if __name__ == "__main__":
    unittest.main()
