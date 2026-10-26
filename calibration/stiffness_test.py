"""思考僵直（探索模式 --stiffness）：冻结期间的思考与工具时间按比例变成下一次 act 前的僵直。

只模拟桥接；GameSession.act 的真实流程（校验、僵直、执行、自动推进、日志）都走一遍。"""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession, PlanSpec, RetrySpec, held_keys
from orchestrator.launch_options import build, config_for
from orchestrator.metrics import Metrics
from orchestrator.prompting import play_prompt
from tools.bridge_client import BridgeClient


def level_obs(room="a", controllable=True, dead=False):
    return {"scene": "Level", "frame": 100, "controllable": controllable and not dead, "storyInput": None,
            "level": {"sid": "Celeste/4-GoldenRidge", "side": "A", "room": room, "inCutscene": False, "deaths": 0,
                      "chapterTimeTicks": 0, "deathsInRoom": 0, "bounds": [0, 0, 320, 180], "camera": [0, 0]},
            "player": {"pos": [10, 20], "speed": [0, 0], "state": "Normal", "dashes": 1, "maxDashes": 1,
                       "stamina": 110, "onGround": True, "dead": dead}}


class StepClient:
    """按顺序回放预先写好的 step 结果；observe 返回当前观测。"""
    def __init__(self, results, make=None, delay=0.0):
        self.make, self.delay = make or level_obs, delay
        self.results, self.steps, self.obs = list(results), [], self.make()

    def call(self, cmd, **args):
        if cmd == "observe":
            return {"obs": copy.deepcopy(self.obs)}
        if cmd == "step":
            self.steps.append({k: args[k] for k in ("inputs", "stop_on") if k in args})
            if self.delay and len(self.steps) == 1:
                time.sleep(self.delay)             # 第一次推进（僵直）在现实里花的时间
            planned = sum(int(str(s).split(",")[0]) for s in args["inputs"])
            stop, consumed, obs, events = self.results.pop(0) if self.results else ("done", planned, self.make(), [])
            self.obs = obs
            return {"result": {"stop": stop, "consumed": planned if consumed is None else consumed, "planned": planned,
                               "events": events, "trajectory": []}, "obs": copy.deepcopy(obs)}
        return {}

    def close(self):
        pass


class HeldKeysTests(unittest.TestCase):
    def test_last_executed_frame(self):
        plan = ["12,R", "1,R,J", "15, R ,G", "8"]
        self.assertEqual(held_keys(plan, 12), "R")
        self.assertEqual(held_keys(plan, 13), "R,J")
        self.assertEqual(held_keys(plan, 20), "R,G")
        self.assertEqual(held_keys(plan, 36), "")          # 最后一段没按键
        self.assertEqual(held_keys([{"n": 3, "keys": "L,X"}], 2), "L,X")
        self.assertIsNone(held_keys(plan, 99))
        self.assertIsNone(held_keys(["x,R"], 1))

    def test_bridge_client_reports_every_step_to_the_session(self):
        client = BridgeClient.__new__(BridgeClient)
        client.next_id, client.cancel_check, client.step_options = 1, None, None
        seen = []
        client.after_step = lambda args, reply: seen.append((args["inputs"], reply["result"]["consumed"]))
        client.file = Mock()
        client.file.readline.side_effect = [b'{"id":1,"ok":true,"result":{"consumed":4}}\n', b'{"id":2,"ok":true}\n']
        client.call("step", inputs=["6,R"])
        client.call("observe")
        self.assertEqual(seen, [(["6,R"], 4)])


class StiffnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = RunConfig(agent="test", save="test", model="test", runtime="host", mode="explore",
                             knowledge_profile="explore", chapter=4, stiffness=True,
                             stiff_think_s=0.3, stiff_tool_s=0.6)   # 用例按这两个值写，与默认值无关
        self.s = GameSession(self.cfg, root, Metrics(root / "metrics.json", "test", "test", "high", 600))
        self.s.cfg.thought_lead_s = 0
        self.counters = [0.0, 0.0, 0.0]
        self.s.metrics.freeze_counters = lambda: tuple(self.counters)
        self.s.last_obs = level_obs()
        self.s._start_freeze()

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def freeze(self, think, tool, compact=0.0):
        self.counters = [think, tool, compact]

    def test_each_part_needs_a_full_unit_then_scales_proportionally(self):
        for (think, tool, compact), seconds in (((149, 149, 0), 0), ((150, 0, 0), 0.3), ((300, 120, 0), 0.6),
                                                ((120, 300, 0), 1.2), ((375, 375, 0), 2.25),
                                                ((200, 0, 60), 0)):          # 压缩时间不算思考
            with self.subTest(think=think, tool=tool, compact=compact):
                self.freeze(think, tool, compact)
                stats = self.s.freeze_stats()
                self.assertAlmostEqual(stats["seconds"], seconds, places=3)
                self.assertEqual(stats["frames"], round(seconds * 60))
        self.freeze(150, 0)
        self.assertIn("预计僵直 0.3 秒（18 帧）", self.s.freeze_line())
        self.freeze(10, 10)
        self.assertIn("目前不会僵直", self.s.freeze_line())

    def test_tunables_change_the_rule(self):
        self.s.cfg.stiff_unit_s, self.s.cfg.stiff_think_s, self.s.cfg.stiff_tool_s = 60, 1, 2
        self.freeze(90, 60)
        self.assertAlmostEqual(self.s.freeze_stats()["seconds"], 1.5 + 2, places=3)

    def test_holds_last_frame_before_the_agent_inputs_and_outside_the_frame_cap(self):
        self.s._remember_held_input({"inputs": ["10,R", "5,R,G"]}, {"result": {"consumed": 15}})
        self.freeze(300, 150)                      # 0.6 + 0.6 = 1.2 秒 = 72 帧
        self.s.client = StepClient([])
        outcome = self.s.act(["150,L"], "往左走到平台边", None)   # 150 帧正好是 act 上限，僵直不占它
        self.assertEqual(self.s.client.steps[0], {"inputs": ["72,R,G"], "stop_on": ["death", "room", "complete", "uncontrollable"]})
        self.assertEqual(self.s.client.steps[1]["inputs"], ["150,L"])
        self.assertEqual((outcome.result["consumed"], outcome.result["stiffness"]["frames"]), (150, 72))
        self.assertTrue(any("按住上一帧的按键（R,G）僵直了 72 帧" in n for n in outcome.auto_notes))
        self.assertEqual((self.s.metrics.state.stiff_frames, self.s.metrics.state.frames_played), (72, 150))
        self.assertEqual(self.s.freeze_stats()["frames"], 0)          # 动作结束：下一次冻结从头算
        frames = [json.loads(line) for line in (Path(self.temp.name) / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(frames)                   # 假桥接没有轨迹；只确认不报错

    def test_neutral_hold_when_last_frame_had_no_keys(self):
        self.s._remember_held_input({"inputs": ["10,R", "6"]}, {"result": {"consumed": 16}})
        self.freeze(150, 0)
        self.s.client = StepClient([])
        self.s.act(["5,R"], "往右走", None)
        self.assertEqual(self.s.client.steps[0]["inputs"], ["18"])

    def test_death_during_stiffness_releases_keys_and_skips_the_agent_inputs(self):
        self.s._remember_held_input({"inputs": ["3,R"]}, {"result": {"consumed": 3}})
        self.freeze(0, 750)                       # 3 秒 = 180 帧
        died = ("death", 40, level_obs(dead=True), [{"type": "death", "frame": 140}])
        self.s.client = StepClient([died, ("controllable", None, level_obs(), [])])
        outcome = self.s.act(["20,J"], "跳上去", None)
        self.assertEqual(self.s.client.steps[0]["inputs"], ["180,R"])
        self.assertNotIn(["20,J"], [step["inputs"] for step in self.s.client.steps])   # 智能体的按键没有执行
        self.assertTrue(all(",R" not in "".join(step["inputs"]) for step in self.s.client.steps[1:]))  # 之后不再按键
        self.assertEqual((outcome.result["stop"], outcome.result["consumed"]), ("death", 0))
        self.assertEqual(self.s.metrics.state.deaths, 1)
        self.assertTrue(any("死亡，已立刻松开所有按键" in n and "没有执行" in n for n in outcome.auto_notes))

    def test_room_change_during_stiffness_stops_without_running_the_plan(self):
        self.s._remember_held_input({"inputs": ["3,R"]}, {"result": {"consumed": 3}})
        self.freeze(300, 0)
        self.s.client = StepClient([("room", 10, level_obs(room="b"), [{"type": "room", "frame": 110}])])
        outcome = self.s.act(["20,J"], "跳上去", None)
        self.assertEqual(len(self.s.client.steps), 1)
        self.assertEqual(outcome.obs["level"]["room"], "b")
        self.assertTrue(any("进入了别的房间" in n for n in outcome.auto_notes))

    def test_no_stiffness_when_disabled_uncontrollable_or_below_the_unit(self):
        self.freeze(300, 300)
        self.s.last_obs = level_obs(controllable=False)
        self.s.client = StepClient([("done", None, level_obs(), [])])
        outcome = self.s.act(["5,R"], "往右走", None)
        self.assertEqual([step["inputs"] for step in self.s.client.steps][0], ["5,R"])
        self.assertTrue(any("角色当前不可操作，这次不僵直" in n for n in outcome.auto_notes))
        for enabled, think in ((False, 600), (True, 100)):
            with self.subTest(enabled=enabled, think=think):
                self.s.cfg.stiffness = enabled
                self.s.last_obs = level_obs()
                self.s._start_freeze() if enabled else None
                self.freeze(self.counters[0] + think, self.counters[1])
                self.s.client = StepClient([])
                self.s.act(["5,R"], "往右走", None)
                self.assertEqual([step["inputs"] for step in self.s.client.steps], [["5,R"]])
        self.s.cfg.mode = "speedrun"
        self.s.cfg.stiffness = True
        self.assertIsNone(self.s.freeze_stats())

    def test_first_freeze_starts_at_the_first_game_tool(self):
        self.s._freeze_base = None
        self.freeze(5000, 5000)                   # 准备阶段的时间不算
        self.s.client = StepClient([])
        self.s.observe()
        self.freeze(5150, 5000)
        self.assertEqual(self.s.freeze_stats()["frames"], 18)


class StiffnessReviewTests(unittest.TestCase):
    """独立审查发现的问题的回归。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent="test", save="test", model="test", runtime="host", mode="explore",
                             knowledge_profile="explore", chapter=4, stiffness=True, thought_lead_s=0,
                        stiff_think_s=0.3, stiff_tool_s=0.6)
        self.s = GameSession(self.cfg, self.root, Metrics(self.root / "metrics.json", "test", "test", "high", 600))
        self.s.last_obs = level_obs()

    def tearDown(self):
        self.s.close()
        self.doCleanups()
        self.temp.cleanup()

    def test_stiffness_game_time_is_not_counted_as_tool_time(self):
        m = self.s.metrics
        self.s.cfg.stiff_unit_s, self.s.cfg.stiff_think_s = 0.01, 0.2
        m.turn_begin()
        self.s._start_freeze()
        time.sleep(0.05)                           # 思考 0.05 秒 → 1 秒僵直
        m.tool_begin("act", game_tool=True)
        before = m.freeze_counters()[1]
        self.s.client = StepClient([], delay=0.3)
        self.s.act(["5,R"], "往右走", None)
        m.tool_end("act")
        self.assertGreater(m.state.auto_game_s, 0.25)
        self.assertLess(m.freeze_counters()[1] - before, 0.15)    # 僵直的 0.3 秒是游戏推进，不算工具时间

    def test_retry_never_runs_the_plan_after_an_interrupted_stiffness(self):
        self.s.container, self.s.workspace = "container", self.root
        plan = PlanSpec(inputs=["20,J"], retry=RetrySpec("python3 planner.py", 3, 10.0, ("death",)), text="{}")
        self.s.resolve_plan_file = lambda path: plan
        self.s._plan_path = lambda path: (self.root / "plan.json").resolve()
        self.s._freeze_base = (0.0, 0.0, 0.0)
        self.s.metrics.freeze_counters = lambda: (0.0, 750.0, 0.0)
        self.s._held_input = ("R", "celeste_ai")
        died = ("death", 40, level_obs(dead=True), [{"type": "death", "frame": 140}])
        self.s.client = StepClient([died, ("controllable", None, level_obs(), [])])
        with patch("orchestrator.game_session.run_program") as program:
            outcome = self.s.act(None, "跳上去", None, plan_file="plan.json")
        program.assert_not_called()
        self.assertNotIn(["20,J"], [step["inputs"] for step in self.s.client.steps])
        self.assertEqual(len(outcome.attempts), 1)

    def test_held_input_ignores_menu_keys_and_other_scenes(self):
        def step(inputs, consumed, scene="Level", fmt=None):
            args = {"inputs": inputs, **({"input_format": fmt} if fmt else {})}
            self.s._remember_held_input(args, {"result": {"consumed": consumed}, "obs": {"scene": scene, "frame": 7}})
            return self.s._held_input
        self.assertEqual(step(["6,R,G"], 6), ("R,G", "celeste_ai"))
        self.assertEqual(step(["10,R+G"], 10), ("R,G", "celeste_ai"))      # 与 expand_inputs 一样的写法
        self.assertEqual(step(["10,r,g"], 10), ("R,G", "celeste_ai"))
        self.assertEqual(step(["10,a:45,j"], 10), ("A:45,J", "celeste_ai"))
        self.assertEqual(step(["1,C"], 1), ("", "celeste_ai"))             # 确认键不算按住
        self.assertEqual(step(["10,R"], 10, scene="Overworld"), ("", "celeste_ai"))   # 关卡以外按空帧算
        self.assertEqual(step(["4,R,S,Q,O,C"], 4, fmt="celeste_tas"), ("R,C", "celeste_tas"))  # TAS 的 C 是第二冲刺
        saved = json.loads((self.root / "held_input.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"keys": "R,C", "input_format": "celeste_tas", "frame": 7})

    def resumed_session(self, released, frame):
        self.s._remember_held_input({"inputs": ["6,R,G"]}, {"result": {"consumed": 6}, "obs": {"scene": "Level", "frame": 500}})
        (self.root / "summary.json").write_text(json.dumps({"game_released": released}), encoding="utf-8")
        later = Path(tempfile.mkdtemp(dir=self.root))
        metrics = Metrics(later / "metrics.json", "test", "test", "high", 600)
        metrics.state.resumed_from = str(self.root)
        resumed = GameSession(self.cfg, later, metrics)
        self.addCleanup(resumed.close)
        resumed._restore_held_input(frame)
        return resumed, later

    def test_resume_restores_held_keys_only_when_still_frozen_on_the_same_frame(self):
        resumed, later = self.resumed_session(False, 500)
        self.assertEqual(resumed._held_input, ("R,G", "celeste_ai"))
        self.assertEqual(json.loads((later / "held_input.json").read_text(encoding="utf-8"))["keys"], "R,G")  # 立即接着存
        for released, frame in ((True, 500), (False, 530)):       # 交还过控制；续跑前菜单宏推进过
            with self.subTest(released=released, frame=frame):
                resumed, _ = self.resumed_session(released, frame)
                self.assertEqual(resumed._held_input, ("", "celeste_ai"))

    def test_bridge_refusal_does_not_block_every_later_act(self):
        self.s._freeze_base = (0.0, 0.0, 0.0)
        self.s.metrics.freeze_counters = lambda: (300.0, 0.0, 0.0)
        client = StepClient([])
        client.call = Mock(side_effect=RuntimeError("step failed: input_format change requires an explicit neutral frame"))
        self.s.client = client
        with self.assertRaises(RuntimeError):
            self.s.act(["5,R"], "往右走", None)
        self.assertEqual(self.s._freeze_base, (300.0, 0.0, 0.0))     # 已重置：下一次不会再为同一段冻结僵直
        self.s.client = StepClient([])

    def test_agent_mode_stops_at_story_and_ignores_the_registered_completion(self):
        self.s.cfg.cutscene_mode = "agent"
        self.s._pending_complete = True
        self.s._freeze_base = (0.0, 0.0, 0.0)
        self.s.metrics.freeze_counters = lambda: (300.0, 0.0, 0.0)
        self.s.client = StepClient([])
        self.s.act(["5,R"], "往右走", None)
        self.assertEqual(self.s.client.steps[0]["stop_on"], ["death", "room", "uncontrollable", "story_input"])

    def test_stop_request_during_stiffness_skips_the_plan(self):
        self.s._freeze_base = (0.0, 0.0, 0.0)
        self.s.metrics.freeze_counters = lambda: (300.0, 0.0, 0.0)
        self.s.client = StepClient([])
        original = self.s.goals.check
        def check(values):
            original(values)
            self.s.goals.stop_motion.set()
        self.s.goals.check = check
        outcome = self.s.act(["5,R"], "往右走", None)
        self.assertEqual(len(self.s.client.steps), 1)
        self.assertTrue(any("主控已请求停止" in n for n in outcome.auto_notes))

    def test_codex_compaction_time_is_recorded(self):
        from orchestrator.codex_agent import CodexRunner
        runner = CodexRunner.__new__(CodexRunner)
        runner._events, runner.control, runner.compacted, runner.paused, runner.chat_only = None, None, set(), False, False
        runner.metrics = self.s.metrics
        runner._public_compaction = lambda *a, **k: None
        runner._event("item/started", {"item": {"type": "contextCompaction", "id": "c"}})
        time.sleep(0.05)
        runner._event("item/completed", {"item": {"type": "contextCompaction", "id": "c"}})
        self.assertGreater(self.s.metrics.state.compact_s, 0.04)
        self.assertEqual(self.s.metrics.state.compactions, 1)

    def test_decisions_record_the_stiffness_for_replay(self):
        self.s._freeze_base = (0.0, 0.0, 0.0)
        self.s.metrics.freeze_counters = lambda: (300.0, 0.0, 0.0)
        self.s._held_input = ("L", "celeste_ai")
        self.s.client = StepClient([])
        self.s.act(["5,R"], "往右走", None)
        self.s._decisions_log.flush()
        record = json.loads((self.root / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual((record["stiffness"]["frames"], record["stiffness"]["keys"]), (36, "L"))
        # 重放：不开僵直的会话按记录僵直
        self.s.cfg.stiffness = False
        self.s.replay_stiffness = record["stiffness"]
        self.s.client = StepClient([])
        self.s.act(["5,R"], "往右走", None)
        self.assertEqual(self.s.client.steps[0]["inputs"], ["36,L"])


class StiffnessSettingsTests(unittest.TestCase):
    def test_prompt_mentions_the_rule_only_when_enabled(self):
        cfg = RunConfig(agent="t", save="t", model="m", mode="explore", knowledge_profile="explore", chapter=4)
        self.assertNotIn("思考僵直", play_prompt(cfg))
        self.assertNotIn("{stiffness_rule}", play_prompt(cfg))
        cfg.stiffness = True
        text = play_prompt(cfg)
        self.assertIn("思考时冻结。本次开启了思考僵直", text)
        self.assertIn("思考时间满 2.5 分钟后，每 2.5 分钟僵直 0.2 秒", text)   # 默认值
        self.assertIn("每 2.5 分钟另外僵直 0.4 秒", text)
        self.assertIn("不计入每次 act 的 150 帧上限", text)
        self.assertIn("僵直期间死亡会立刻松开所有按键", text)
        cfg.engine = "codex"                      # 两种执行器都适用
        self.assertIn("本次开启了思考僵直", play_prompt(cfg))

    def test_console_switch_is_explore_only_and_off_by_default(self):
        base = {"agent": "opus5.5", "mode": "explore", "save": {"name": "opus5.5", "slot": 8, "create": False},
                "chapters": [4], "sim_assist": False}
        self.assertNotIn("--stiffness", build(base))
        self.assertIn("--stiffness", build({**base, "stiffness": True}))
        self.assertTrue(config_for({**base, "stiffness": True}).stiffness)
        self.assertNotIn("--stiffness", build({**base, "mode": "speedrun", "stiffness": True}))
        self.assertFalse(config_for({**base, "mode": "speedrun", "stiffness": True}).stiffness)


if __name__ == "__main__":
    unittest.main()


class StiffnessToolTextTests(unittest.TestCase):
    def test_observe_and_act_show_the_freeze_to_the_agent(self):
        import asyncio
        from orchestrator.server import build_server
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        cfg = RunConfig(agent="test", save="test", model="test", runtime="host", mode="explore",
                        knowledge_profile="explore", chapter=4, stiffness=True, thought_lead_s=0,
                        stiff_think_s=0.3, stiff_tool_s=0.6)
        session = GameSession(cfg, root, Metrics(root / "metrics.json", "test", "test", "high", 600))
        self.addCleanup(session.close)
        counters = [0.0, 0.0, 0.0]
        session.metrics.freeze_counters = lambda: tuple(counters)
        def no_player():
            obs = level_obs()
            del obs["player"]                      # 文字描述里不需要角色细节
            return obs
        session.client = StepClient([], make=no_player)
        session.last_obs = level_obs()
        server = build_server(session, study=Mock(ready=Mock(return_value=True)))
        text = asyncio.run(server.call_tool("observe", {})).content[-1].text
        self.assertIn("思考僵直：这次冻结已思考 0.0 分钟、运行工具 0.0 分钟，目前不会僵直。", text)
        counters[:] = [150.0, 300.0, 0.0]          # 0.3 + 1.2 = 1.5 秒 = 90 帧
        text = asyncio.run(server.call_tool("observe", {})).content[-1].text
        self.assertIn("思考 2.5 分钟、运行工具 5.0 分钟，下一次 act 执行前预计僵直 1.5 秒（90 帧）", text)
        text = asyncio.run(server.call_tool("act", {"inputs": ["5,R"], "thought": "往右走"})).content[-1].text
        self.assertIn("这次冻结思考 2.5 分钟、运行工具 5.0 分钟，执行你的按键之前先（上一帧没有按键）僵直了 90 帧（1.5 秒）", text)
        self.assertNotIn("目前不会僵直", text)     # act 的结果不再附上刚重置的冻结
        counters[:] = [160.0, 310.0, 0.0]
        text = asyncio.run(server.call_tool("act", {"inputs": ["5,R"], "thought": "往右走"})).content[-1].text
        self.assertIn("按规则这次不僵直（思考、工具时间各自满 2.5 分钟才计）", text)   # 没到门槛也告诉智能体这次冻结的时间
        self.assertEqual(session.client.steps[0]["inputs"], ["90"])
