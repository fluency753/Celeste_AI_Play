"""节省上下文（--save-context）：开关的默认值与续跑沿用；同一房间里的 act 结果省略样板、进新房间照常给全；
关掉时返回与原来一样；act 说明与探索提示词按开关写。"""
import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibration.collision_contract_test import FakeBridge, observation
from orchestrator.config import RunConfig
from orchestrator.execution import WorkspaceCapabilities
from orchestrator.game_session import GameSession
from orchestrator.launch_options import build, config_for
from orchestrator.metrics import Metrics
from orchestrator.modes import describe
from orchestrator.prompting import SAVE_CONTEXT_ACT, SAVE_CONTEXT_PRINT, fill, play_prompt
from orchestrator.run import save_context_choice
from orchestrator.server import ACT_DESCRIPTION, FILE_JSON_HEADER, build_server


class SwitchTests(unittest.TestCase):
    def test_new_runs_default_on_and_resumes_keep_the_recorded_setting(self):
        self.assertTrue(save_context_choice(None, None))
        self.assertFalse(save_context_choice(None, False))
        self.assertFalse(save_context_choice({"state": {}}, None))            # 开关出现之前的旧运行：按关
        self.assertTrue(save_context_choice({"state": {"save_context": True}}, None))
        self.assertTrue(save_context_choice({"state": {}}, True))             # 追加下一章时显式打开
        self.assertFalse(save_context_choice({"state": {"save_context": True}}, False))

    def test_console_switch(self):
        self.assertIn("--save-context", build({"agent": "x"}))
        self.assertTrue(config_for({"agent": "x"}).save_context)
        self.assertIn("--no-save-context", build({"agent": "x", "save_context": False}))
        self.assertFalse(config_for({"agent": "x", "save_context": False}).save_context)
        cfg = RunConfig(agent="a", save="s", model="m", mode="explore", save_context=True)
        self.assertIn("节省上下文", describe(cfg))

    def test_texts_follow_the_switch(self):
        on = RunConfig(agent="a", save="s", model="m", mode="explore", knowledge_profile="explore", chapter=4,
                       save_context=True)
        off = RunConfig(agent="a", save="s", model="m", mode="explore", knowledge_profile="explore", chapter=4)
        self.assertIn(SAVE_CONTEXT_ACT, fill(ACT_DESCRIPTION, on))
        self.assertNotIn("节省上下文", fill(ACT_DESCRIPTION, off))
        self.assertIn(SAVE_CONTEXT_PRINT, play_prompt(on))
        self.assertNotIn(SAVE_CONTEXT_PRINT, play_prompt(off))
        for cfg in (on, off):
            self.assertEqual(re.findall(r"\{[a-z_]+\}", fill(ACT_DESCRIPTION, cfg)), [])

    def test_no_stiffness_note_only_when_off(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for flag, expect in ((True, {}), (False, None)):
                cfg = RunConfig(agent="t", save="test", model="m", runtime="host", mode="explore", save_context=flag)
                s = GameSession(cfg, root, Metrics(root / "metrics.json", "t", "m", "high", 600))
                try:
                    out = s._stiffen({"frames": 0, "think_s": 30, "tool_s": 0, "seconds": 0})
                finally:
                    s.close(False)
                if expect is None:
                    self.assertIn("这次不僵直", out["note"])
                else:
                    self.assertEqual(out, expect)


# 有程序能力（program_location 不为空）才用文件模式
@patch("orchestrator.server.workspace_capabilities", lambda cfg: WorkspaceCapabilities("host", True, True, "host"))
class ActResultTests(unittest.TestCase):
    def texts(self, save_context):
        with tempfile.TemporaryDirectory() as folder, patch("orchestrator.config.REPO_ROOT", Path(folder)):
            root = Path(folder)
            cfg = RunConfig(agent="ctx", save="test", model="stub", engine="claude", runtime="host", sim=False,
                            mode="explore", knowledge_profile="explore", chapter=3, save_context=save_context)
            cfg.min_frames_per_act = 1
            session = GameSession(cfg, root, Metrics(root / "metrics.json", "ctx", "stub", "high", 60))
            session.client = FakeBridge()
            session.workspace = root / "workspace"          # 文件模式：几何写进 latest_obs.json，正文给文件位置
            (session.workspace / "game").mkdir(parents=True)
            session.effort_levels = lambda: ["low", "high"]
            server = build_server(session)
            try:
                asyncio.run(server.call_tool("observe", {}))
                same = asyncio.run(server.call_tool("act", {"inputs": ["1,R"], "thought": "同房间"}))
                original = session.client.call

                def to_new_room(command, **args):
                    reply = original(command, **args)
                    if command == "step":
                        for o in (reply["obs"], session.client.obs):
                            o["level"]["room"] = o["collision"]["room"] = "s4"
                    return reply
                session.client.call = to_new_room
                entered = asyncio.run(server.call_tool("act", {"inputs": ["1,R"], "thought": "进新房间"}))
                pick = lambda r: "\n".join(b.text for b in r.content if getattr(b, "type", "") == "text")
                return pick(same), pick(entered)
            finally:
                session.close(False)

    def test_same_room_drops_boilerplate_new_room_keeps_everything(self):
        same, entered = self.texts(True)
        self.assertNotIn(FILE_JSON_HEADER, same)
        self.assertIn("观测文件：/workspace/game/latest_obs.json（帧 ", same)
        self.assertNotIn("可选推理强度", same)
        self.assertNotIn("本房间范围", same)
        self.assertIn("画面左上角世界坐标", same)
        self.assertIn("Madeline：位置", same)                   # 角色状态照常保留
        self.assertIn(FILE_JSON_HEADER, entered)
        self.assertIn("本房间范围", entered)
        self.assertIn("可选推理强度", entered)

    def test_switch_off_keeps_the_old_format(self):
        same, _ = self.texts(False)
        self.assertIn(FILE_JSON_HEADER, same)
        self.assertIn("可选推理强度", same)
        self.assertIn("本房间范围", same)


if __name__ == "__main__":
    unittest.main()
