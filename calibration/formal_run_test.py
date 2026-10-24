"""Claude 执行器的速通正式运行保持原有行为：提示词、观测、推理档位与停下时冻结游戏。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.config import REPO_ROOT, RunConfig
from orchestrator.modes import KNOWLEDGE_OF_MODE, game_tools, remind_seconds
from orchestrator.observation import describe_obs
from orchestrator.prompting import play_prompt
from orchestrator import run as run_module
from orchestrator.run import _keeps_game_frozen, _previous_kept_frozen


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


def _cfg(mode="speedrun", sim=True, live=False, sim_assist=False, cutscene_mode="auto"):
    cfg = RunConfig(agent="t", save="test", model="opus")
    cfg.mode, cfg.knowledge_profile = mode, KNOWLEDGE_OF_MODE[mode]
    cfg.sim, cfg.live, cfg.sim_assist, cfg.cutscene_mode = sim, live, sim_assist, cutscene_mode
    cfg.live_remind_s = remind_seconds(live)
    cfg.chapter, cfg.chapters = 1, None
    return cfg


class FormalRunTest(unittest.TestCase):
    def test_prompt_versions_by_mode(self):
        speedrun = play_prompt(_cfg())
        self.assertIn("# 思考与工具状态（HUD）", speedrun)
        self.assertIn("半分钟", speedrun)
        self.assertNotIn("# 直播", speedrun)
        self.assertNotIn("exit_level", speedrun)
        self.assertNotIn("当前剧情模式", speedrun)
        self.assertIn("预置模拟器", speedrun)
        nosim = play_prompt(_cfg(sim=False))
        self.assertIn("本次运行不提供预置模拟器", nosim)
        self.assertNotIn("# 预置模拟器", nosim)
        speedrun_live = play_prompt(_cfg(live=True))
        self.assertIn("# 直播", speedrun_live)
        self.assertIn("10 秒", speedrun_live)
        self.assertIn("exit_level", speedrun_live)
        self.assertNotIn("节奏要求", speedrun_live)          # 速通 + 直播不加探索的节奏要求
        self.assertNotIn("找到一条安全", speedrun_live)
        explore = play_prompt(_cfg(mode="explore", sim=False, sim_assist=True))
        self.assertIn("# 新手探索 Celeste", explore)
        self.assertIn("observe/act/narrate/set_effort/cutscene/simulate", explore)
        self.assertNotIn("exit_level", explore)
        self.assertNotIn("# 直播", explore)
        self.assertNotIn("10–20 秒", explore)
        self.assertIn("本次指定章节：1 被遗弃之城", explore)
        explore_live = play_prompt(_cfg(mode="explore", sim=False, sim_assist=True, live=True))
        self.assertIn("# 直播（探索）", explore_live)
        self.assertIn("10–20 秒", explore_live)
        self.assertIn("observe/act/narrate/set_effort/exit_level/cutscene/simulate", explore_live)
        for text in (speedrun, nosim, speedrun_live, explore, explore_live):
            self.assertNotIn("{remind}", text)
            self.assertNotIn("{game_tools}", text)
            self.assertNotIn("# 新手探索", text) if text in (speedrun, nosim, speedrun_live) else None

    def test_tools_by_mode(self):
        self.assertEqual(game_tools(_cfg()), ["observe", "act", "narrate", "set_effort"])
        self.assertEqual(game_tools(_cfg(live=True)), ["observe", "act", "narrate", "set_effort", "exit_level", "cutscene"])
        self.assertEqual(game_tools(_cfg(mode="explore", sim=False, sim_assist=True)),
                         ["observe", "act", "narrate", "set_effort", "cutscene", "simulate"])
        self.assertIn("cutscene", game_tools(_cfg(cutscene_mode="agent")))

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

    def test_interrupted_run_records_frozen_game_in_summary(self):
        """Ctrl+C：summary.json 在会话自己的 finally 里写出、没有返回值，仍要记下游戏保持冻结，续跑才知道。"""
        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            cfg = SimpleNamespace(_keep_frozen=False)

            class FakeSession:
                def __init__(self, *args):
                    self.client = object()

                def close(self, release):
                    self.released = release

            async def interrupted(*args, **kwargs):
                (run_dir / "summary.json").write_text('{"stop_reason": "中断"}', encoding="utf-8")
                cfg._keep_frozen = True
                raise RuntimeError("interrupted")

            with patch.object(run_module, "GameSession", FakeSession), \
                 patch.object(run_module, "_run_connected_session", interrupted):
                with self.assertRaises(RuntimeError):
                    asyncio.run(run_module._run_session(cfg, SimpleNamespace(), run_dir, None, None, None, None))
            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertIs(summary["game_released"], False)
            self.assertEqual(summary["stop_reason"], "中断")
            self.assertTrue(_previous_kept_frozen({"dir": run_dir}))


if __name__ == "__main__":
    unittest.main()
