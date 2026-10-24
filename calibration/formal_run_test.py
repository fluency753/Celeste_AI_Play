"""Claude 执行器的速通正式运行保持原有行为：提示词、观测、推理档位与停下时冻结游戏。"""
import json
import unittest
from types import SimpleNamespace

from orchestrator.config import REPO_ROOT
from orchestrator.observation import describe_obs
from orchestrator.prompting import compose_prompt
from orchestrator.run import _keeps_game_frozen


def _observation():
    return {"scene": "Level", "frame": 1,
            "level": {"sid": "Celeste/1-ForsakenCity", "room": "1", "chapterTimeTicks": 0, "deaths": 0,
                      "deathsInRoom": 0, "bounds": [0, 0, 40, 24], "camera": [0, 0], "side": "A"},
            "tiles": {"scope": "room", "origin": [0, 0], "cell": 8, "rows": ["..#..", "..#..", "..#.."]},
            "player": {"pos": [4, 11], "speed": [0, 0], "state": "Normal", "dashes": 1, "maxDashes": 1,
                       "stamina": 110, "onGround": True, "facing": "R", "wallLeft": False, "wallRight": False, "ducking": False,
                       "hitbox": [0, 0, 8, 11], "timers": {"jumpGrace": 0, "varJump": 0, "dashCooldown": 0}},
            "entities": [{"id": 1, "type": "Spring", "cat": "spring", "x": 32, "y": 8, "w": 4, "h": 4},
                         {"id": 2, "type": "Strawberry", "cat": "collectible", "x": 8, "y": 8, "w": 4, "h": 4}]}


class FormalRunTest(unittest.TestCase):
    def test_live_pacing_only_for_native_gpt(self):
        claude = compose_prompt("container", sim=True, live=True)
        self.assertNotIn("直播节奏", claude)
        self.assertNotIn("找到一条安全", claude)
        self.assertIn("半分钟", claude)
        codex = compose_prompt("container", sim=True, live=True, engine="codex", remind_s=10)
        self.assertIn("直播节奏", codex)
        self.assertIn("10 秒", codex)
        self.assertNotIn("{remind}", claude + codex)

    def test_route_hints_only_in_explore(self):
        speedrun = describe_obs(_observation(), full_room=True)
        self.assertNotIn("静态地形检查", speedrun)
        self.assertNotIn("当前观测中的收集品", speedrun)
        explore = describe_obs(_observation(), full_room=True, explore=True)
        self.assertIn("静态地形检查", explore)
        self.assertIn("当前观测中的收集品", explore)

    def test_formal_models_offer_all_effort_levels(self):
        providers = json.loads((REPO_ROOT / "configs" / "providers.json").read_text(encoding="utf-8"))
        efforts = {m["id"]: m["efforts"] for p in ("anthropic", "deepseek") for m in providers[p]["models"]}
        for model in ("opus", "fable", "deepseek-flash"):
            self.assertEqual(efforts[model], ["low", "medium", "high", "xhigh", "max"], model)

    def test_claude_run_stopped_early_keeps_game_frozen(self):
        args = SimpleNamespace(no_agent=False)
        claude = SimpleNamespace(engine="claude")
        self.assertTrue(_keeps_game_frozen(claude, args, SimpleNamespace(done=False)))
        self.assertFalse(_keeps_game_frozen(claude, args, SimpleNamespace(done=True)))
        self.assertFalse(_keeps_game_frozen(SimpleNamespace(engine="codex"), args, SimpleNamespace(done=False)))
        self.assertFalse(_keeps_game_frozen(claude, SimpleNamespace(no_agent=True), SimpleNamespace(done=False)))


if __name__ == "__main__":
    unittest.main()
