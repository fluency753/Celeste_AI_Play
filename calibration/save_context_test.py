"""节省上下文（--save-context）：开关的默认值与续跑沿用；act 结果更简短（轨迹不写列名、实体只列起止位置不同的），
同一房间里另外省略样板（观测文件一行、Madeline 只写非默认项等）、进新房间照常给全；关掉时返回与原来一样；
act 说明与程序一节的建议按开关写；Claude 的自动压缩窗口没另外设置时默认 500000。"""
import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibration.collision_contract_test import FakeBridge, observation
import orchestrator.kb_access  # noqa: F401  先按真实仓库算好 KB_ROOT：下面的用例会临时替换 REPO_ROOT
from orchestrator.config import SAVE_CONTEXT_AUTOCOMPACT, RunConfig, autocompact_window
from orchestrator.geometry_context import geometry_page, observation_id
from orchestrator.observation import brief_player_lines, describe_moved_entities, describe_step
from orchestrator.observation_delivery import SHORT_SNAPSHOT_CHARS
from orchestrator.execution import WorkspaceCapabilities
from orchestrator.game_session import GameSession
from orchestrator.launch_options import build, config_for
from orchestrator.metrics import Metrics
from orchestrator.modes import describe
from orchestrator.prompting import SAVE_CONTEXT_PRINT, fill, play_prompt, save_context_act
from orchestrator.run import save_context_choice
from orchestrator.server import ACT_DESCRIPTION, FILE_JSON_HEADER, build_server, observation_tool_description


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
        rule = save_context_act(on)
        self.assertIn(rule, fill(ACT_DESCRIPTION, on))
        self.assertIn("帧、x、y、vx、vy、状态、冲刺、着地", rule)          # 轨迹列名从结果挪到工具说明
        self.assertIn("/workspace/game/latest_obs.json", rule)            # 容器运行方式有程序能力：写明观测文件位置
        self.assertNotIn("节省上下文", fill(ACT_DESCRIPTION, off))
        self.assertIn(SAVE_CONTEXT_PRINT, play_prompt(on))
        self.assertNotIn(SAVE_CONTEXT_PRINT, play_prompt(off))
        for cfg in (on, off):
            self.assertEqual(re.findall(r"\{[a-z_]+\}", fill(ACT_DESCRIPTION, cfg)), [])
        no_programs = RunConfig(agent="a", save="s", model="m", runtime="host", mode="explore",
                                knowledge_profile="explore", chapter=4, save_context=True)
        with patch("orchestrator.prompting.workspace_capabilities", lambda cfg: WorkspaceCapabilities("host", False, False, None)):
            self.assertNotIn("latest_obs.json", save_context_act(no_programs))   # 没有程序能力时正文给布局，不提观测文件

    def test_program_advice_reaches_both_modes_and_executors(self):
        """程序一节的建议放在共享接口说明里：探索与速通、Claude 与 Codex 都有，关掉时都没有。"""
        for extra in ({"mode": "explore"}, {"mode": "speedrun", "sim": False},
                      {"mode": "explore", "engine": "codex", "provider": "codex", "model": "gpt-5.5"},
                      {"mode": "speedrun", "sim": False, "engine": "codex", "provider": "codex", "model": "gpt-5.5"}):
            for flag in (True, False):
                with self.subTest(extra=extra, save_context=flag):
                    cfg = config_for({"agent": "x", "save_context": flag, **extra})
                    self.assertEqual(SAVE_CONTEXT_PRINT in play_prompt(cfg), flag)

    def test_autocompact_defaults_to_500k_only_when_nothing_else_is_set(self):
        cfg = RunConfig(agent="a", save="s", model="m", save_context=True)
        self.assertEqual(autocompact_window(cfg), SAVE_CONTEXT_AUTOCOMPACT)
        self.assertEqual(SAVE_CONTEXT_AUTOCOMPACT, "500000")
        cfg.provider_info = {"autocompact": "786432"}                     # 提供方设置的窗口优先
        self.assertEqual(autocompact_window(cfg), "786432")
        cfg.autocompact = "auto"                                          # 本次运行设置的最优先
        self.assertEqual(autocompact_window(cfg), "auto")
        off = RunConfig(agent="a", save="s", model="m", save_context=False)
        self.assertIsNone(autocompact_window(off))
        off.autocompact = "300000"
        self.assertEqual(autocompact_window(off), "300000")

    def test_claude_runner_passes_the_default_window(self):
        from calibration.note_reminders_test import runner_fixture
        with runner_fixture('claude') as (runner, session, path):
            runner.cfg.save_context = True
            args = runner._common_args()
            self.assertEqual(args[args.index('--autocompact') + 1], '500000')
            self.assertEqual(runner._notes_window(), 500000)      # 笔记提醒按实际传给 Claude Code 的窗口算
            runner.cfg.save_context = False
            self.assertNotIn('--autocompact', runner._common_args())

    def test_registered_act_description_follows_the_switch(self):
        """实际注册的 act 说明：有 help 资料时用的是精简版，精简版也要按开关写节省上下文那句。"""
        caps = WorkspaceCapabilities("host", True, True, "host")
        with patch("orchestrator.prompt_environment.workspace_capabilities", lambda cfg: caps), \
                patch("orchestrator.server.workspace_capabilities", lambda cfg: caps):
            for engine in ("claude", "codex"):
                on = RunConfig(agent="a", save="s", model="m", engine=engine, runtime="host", mode="explore",
                               knowledge_profile="explore", chapter=4, save_context=True)
                off = RunConfig(agent="a", save="s", model="m", engine=engine, runtime="host", mode="explore",
                                knowledge_profile="explore", chapter=4, save_context=False)
                text_on = fill(observation_tool_description(on, "act"), on)
                text_off = fill(observation_tool_description(off, "act"), off)
                self.assertIn('/kb/shared/tools/act.md', text_on)     # 确实走的是精简版
                self.assertIn("本次开启了节省上下文，", text_on)
                self.assertIn("结果更简短：轨迹不写列名", text_on)    # Codex 会把工具名换成 celeste_act
                self.assertNotIn("节省上下文", text_off)
                for text in (text_on, text_off):
                    self.assertEqual(re.findall(r"\{[a-z_]+\}", text), [])

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
                bridge = session.client.call

                def with_motion(command, **args):          # 给结果加上轨迹与实体移动，看列名与实体行怎么写
                    reply = bridge(command, **args)
                    if command == "step":
                        reply["result"]["trajectory"] = [[reply["obs"]["frame"], 4.0, 11.0, 0.0, 0.0, "Normal", 1, True]]
                        reply["result"]["entityTrajectory"] = [[1, 171, 0, -8], [2, 171, 8, -8], [2, 5, 3, 3]]
                    return reply
                session.client.call = with_motion
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
        self.assertRegex(same, r"观测文件已更新（帧 \d+，快照 [0-9a-f]{%d}）" % SHORT_SNAPSHOT_CHARS)
        self.assertNotIn("latest_obs.json", same)               # 文件位置固定，写在工具说明里
        self.assertNotIn("可选推理强度", same)
        self.assertNotIn("本房间范围", same)
        self.assertIn("画面左上角世界坐标", same)
        self.assertIn("Madeline：位置", same)                   # 角色状态照常保留，只省默认项
        for default in ("体力", "贴墙", "下蹲", "可操作", "剩余帧数"):
            self.assertNotIn(default, same)
        self.assertIn("移动过的实体 1 个：#171 (0,-8)→(8,-8)；另有 1 个起止位置相同", same)
        for text in (same, entered):
            self.assertIn("轨迹（每 1 帧取样，另含事件帧与最后一帧）：", text)   # 列名在工具说明里
            self.assertNotIn("列：帧", text)
        self.assertIn(FILE_JSON_HEADER, entered)
        self.assertIn("本房间范围", entered)
        self.assertIn("可选推理强度", entered)
        self.assertIn("体力 110", entered)

    def test_switch_off_keeps_the_old_format(self):
        same, _ = self.texts(False)
        self.assertIn(FILE_JSON_HEADER, same)
        self.assertIn("可选推理强度", same)
        self.assertIn("本房间范围", same)
        self.assertIn("体力 110", same)
        self.assertIn("剩余帧数：coyote time 0", same)
        self.assertIn("列：帧, x, y, vx, vy, 状态, 冲刺, 着地", same)
        self.assertIn("这段动作中画面附近有 2 个实体移动过", same)


class BriefFormatTests(unittest.TestCase):
    PLAYER = {"pos": [4.5, 11.25], "speed": [90.0, -40.0], "state": "Normal", "dashes": 0, "maxDashes": 1,
              "stamina": 110, "onGround": False, "facing": "L", "wallLeft": False, "wallRight": False,
              "ducking": False, "hitbox": [0, 0, 8, 11], "timers": {"jumpGrace": 0, "varJump": 0, "dashCooldown": 0}}

    def test_player_line_keeps_only_values_that_differ_from_the_default(self):
        line, box = brief_player_lines(self.PLAYER, True)
        self.assertEqual(line, "Madeline：位置 (4.50, 11.25)  速度 (90.0, -40.0)  状态 Normal  着地 否  冲刺 0/1  朝向 L")
        self.assertEqual(box, "碰撞箱 x 0~8，y 0~11")
        busy = {**self.PLAYER, "stamina": 42.5, "wallRight": True, "ducking": True,
                "timers": {"jumpGrace": 0, "varJump": 7, "dashCooldown": 2}}
        line, box = brief_player_lines(busy, False)
        for part in ("体力 42.5", "贴墙 右", "下蹲", "不可操作"):
            self.assertIn(part, line)
        self.assertEqual(box, "碰撞箱 x 0~8，y 0~11；剩余帧数：variable jump 7，dash cooldown 2")

    def test_entities_are_sorted_by_displacement_and_unmoved_ones_only_counted(self):
        moving = {1: [1, 0, 0, 5, 3, 0], 2: [1, 10, 10, 9, 10, 40], 3: [2, 7, 7, 9, 7, 7], 4: [3, 1, 1, 3, 1.2, 1]}
        self.assertEqual(describe_moved_entities(moving),
                         "移动过的实体 2 个：#2 (10,10)→(10,40)；#1 (0,0)→(3,0)；另有 2 个起止位置相同")
        many = {i: [1, 0, 0, 2, i, 0] for i in range(1, 14)}
        text = describe_moved_entities(many)
        self.assertTrue(text.startswith("移动过的实体 13 个：#13 (0,0)→(13,0)；#12"))
        self.assertIn("另有 3 个移动较小的未列", text)
        self.assertEqual(describe_moved_entities({3: [2, 7, 7, 9, 7, 7]}), "画面附近 1 个实体位置变过，起止位置都相同。")
        step = describe_step({"consumed": 3, "planned": 3, "stop": "done",
                              "entityTrajectory": [[1, 9, 0, 0], [3, 9, 4, 0]]}, brief=True)
        self.assertIn("移动过的实体 1 个：#9 (0,0)→(4,0)", step)

    def test_geometry_detail_accepts_the_short_snapshot_prefix(self):
        from calibration.collision_contract_test import observation
        obs = observation()
        ident = observation_id(obs)
        for given in (ident, ident[:SHORT_SNAPSHOT_CHARS]):
            geometry_page(obs, offset=1, limit=1, expected_snapshot=given)
        for wrong in (ident[:SHORT_SNAPSHOT_CHARS - 1], "0" * SHORT_SNAPSHOT_CHARS if not ident.startswith("0" * 8) else "f" * 8):
            with self.assertRaises(ValueError):
                geometry_page(obs, offset=1, limit=1, expected_snapshot=wrong)


if __name__ == "__main__":
    unittest.main()
