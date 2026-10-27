"""5A 镜子后的 void 房间操控 PlayerSeeker（Madeline 睡在 Dummy、inControl=false、不在剧情中）：主控当作可操作，
不自动推进等 Madeline 恢复控制（原来每次 act 先推 18000 帧，章节计时白走 5 分钟再拒绝执行）；Madeline 被 PlayerSeeker
碰到的 death 不计死亡。其他"不可操作又不在剧情里"的状态最多等 IDLE_GIVE_UP_FRAMES 就交回，之后的按键照常执行。

只模拟桥接；GameSession.act 的真实流程（校验、执行、自动推进、日志）都走一遍。"""
import copy
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import RunConfig
from orchestrator.game_session import (IDLE_GIVE_UP_FRAMES, SEEKER_DEATH_NOTE, GameSession, PlanSpec, idle_uncontrolled,
                                       seeker_control)
from orchestrator.metrics import Metrics
from orchestrator.modes import act_frame_min


def level_obs(sid="Celeste/5-MirrorTemple", room="void", state="Dummy", controllable=False, dead=False, **level):
    obs = {"scene": "Level", "frame": 100, "controllable": controllable and not dead, "storyInput": None,
           "level": {"sid": sid, "side": "A", "room": room, "inCutscene": False, "skippingCutscene": False,
                     "paused": False, "transitioning": False, "frozen": False, "completed": False,
                     "dialogOpen": False, "dialogWaiting": False, "deaths": 0, "chapterTimeTicks": 0,
                     "deathsInRoom": 0, "bounds": [0, 0, 320, 180], "camera": [0, 0]},
           "player": {"pos": [10, 20], "speed": [0, 0], "state": state, "dashes": 1, "maxDashes": 1,
                      "stamina": 110, "onGround": True, "dead": dead, "inControl": controllable}}
    obs["level"].update(level)
    return obs


def seeker_obs(**level):
    return level_obs(**level)


def c00_obs():
    return level_obs(room="c-00", state="Normal", controllable=True)


class StepClient:
    """按顺序回放预先写好的 step 结果；用完后每次 step 都返回 make() 的观测。observe 返回当前观测。"""

    def __init__(self, results, make=seeker_obs):
        self.make, self.results, self.steps, self.observes = make, list(results), [], 0
        self.obs = make()

    def call(self, cmd, **args):
        if cmd == "observe":
            self.observes += 1
            return {"obs": copy.deepcopy(self.obs)}
        if cmd == "step":
            self.steps.append(list(args["inputs"]))
            planned = sum(int(str(s).split(",")[0]) for s in args["inputs"])
            stop, consumed, obs, events = self.results.pop(0) if self.results else ("done", planned, self.make(), [])
            self.obs = obs
            return {"result": {"stop": stop, "consumed": planned if consumed is None else consumed, "planned": planned,
                               "events": events, "trajectory": []}, "obs": copy.deepcopy(obs)}
        return {}

    def close(self):
        pass


class DetectionTests(unittest.TestCase):
    def test_only_the_void_room_with_madeline_asleep(self):
        self.assertTrue(seeker_control(seeker_obs()))
        for obs in (seeker_obs(room="c-00"), level_obs(sid="Celeste/4-GoldenRidge"), seeker_obs(paused=True),
                    seeker_obs(inCutscene=True), seeker_obs(transitioning=True), level_obs(dead=True),
                    level_obs(state="Normal", controllable=True), level_obs(state="IntroWakeUp")):
            self.assertFalse(seeker_control(obs), obs["level"]["room"])
        b_side = seeker_obs()
        b_side["level"]["side"] = "B"
        self.assertFalse(seeker_control(b_side))
        self.assertFalse(seeker_control({"scene": "Overworld"}))

    def test_idle_uncontrolled_excludes_cutscenes_and_other_known_states(self):
        self.assertTrue(idle_uncontrolled(level_obs(sid="Celeste/4-GoldenRidge", room="a-01")))
        for change in ({"inCutscene": True}, {"dialogOpen": True}, {"dialogWaiting": True}, {"paused": True},
                       {"transitioning": True}, {"frozen": True}, {"completed": True}):
            self.assertFalse(idle_uncontrolled(level_obs(**change)), change)
        self.assertFalse(idle_uncontrolled(level_obs(dead=True)))
        self.assertFalse(idle_uncontrolled(level_obs(state="Normal", controllable=True)))
        no_player = level_obs()
        no_player["player"] = None
        self.assertFalse(idle_uncontrolled(no_player))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.cfg = RunConfig(agent="test", save="test", model="test", runtime="host", mode="explore",
                             knowledge_profile="explore", chapter=5)
        self.s = GameSession(self.cfg, root, Metrics(root / "metrics.json", "test", "test", "high", 600))
        self.s.cfg.thought_lead_s = 0

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def test_auto_advance_hands_the_seeker_over_without_stepping(self):
        self.s.client = StepClient([])
        obs, _, total, notes = self.s._auto_advance(seeker_obs(), None)
        self.assertEqual(self.s.client.steps, [])
        self.assertEqual(total, 0)
        self.assertTrue(any("PlayerSeeker" in str(n) for n in notes))

    def test_mirror_cutscene_then_void_stops_at_the_seeker(self):
        cutscene = level_obs(room="b-16", state="Dummy", inCutscene=True)
        self.s.client = StepClient([("controllable", 30, cutscene, []), ("room", 12, seeker_obs(), [])],
                                   make=lambda: cutscene)
        self.s.cfg.cutscene_mode = "agent"          # 不走暂停菜单跳过，直接按 30 帧推进
        obs, _, total, notes = self.s._auto_advance(cutscene, None, stop_for_skip=False)
        self.assertEqual(obs["level"]["room"], "void")
        self.assertEqual(total, 42)
        self.assertTrue(any("PlayerSeeker" in str(n) for n in notes))

    def test_act_executes_the_inputs_without_auto_advancing(self):
        self.s.client = StepClient([])
        self.s.last_obs = seeker_obs()
        least = act_frame_min(self.cfg)
        self.assertEqual(self.s._least_frames(PlanSpec(inputs=["1"]), 1), least)   # 每次至少帧数照常
        outcome = self.s.act([f"{least},R"], "往右飘过去", None)
        self.assertEqual(self.s.client.steps, [[f"{least},R"]])       # 原来先推 600 × 30 帧再拒绝
        self.assertEqual(self.s.client.observes, 0)
        self.assertEqual(outcome.auto_frames, 0)
        self.assertEqual(self.s.metrics.state.deaths, 0)

    def test_madeline_caught_by_the_seeker_is_not_a_death(self):
        least = act_frame_min(self.cfg)
        caught = seeker_obs()
        caught["player"]["dead"] = True
        self.s.client = StepClient([("death", 20, caught, [{"type": "death", "frame": 120}]),
                                    ("controllable", 30, c00_obs(), [])])
        self.s.last_obs = seeker_obs()
        outcome = self.s.act([f"{least},R,X"], "冲向睡着的人", None)
        self.assertEqual(self.s.metrics.state.deaths, 0)
        self.assertIn(SEEKER_DEATH_NOTE, outcome.auto_notes)
        self.assertEqual(outcome.obs["level"]["room"], "c-00")         # 死亡动画后进 c-00 由主控照常推进
        self.assertGreater(outcome.auto_frames, 0)
        # 离开 void 之后的死亡照常计数
        self.s.client = StepClient([("death", 10, level_obs(room="c-00", state="Normal", dead=True),
                                     [{"type": "death", "frame": 300}]), ("controllable", 30, c00_obs(), [])],
                                   make=c00_obs)
        self.s.act([f"{least},L"], "往左跳", None)
        self.assertEqual(self.s.metrics.state.deaths, 1)

    def test_other_idle_states_give_up_and_then_accept_inputs(self):
        stuck = lambda: level_obs(sid="Celeste/4-GoldenRidge", room="a-01")
        self.s.client = StepClient([], make=stuck)
        obs, _, total, notes = self.s._auto_advance(stuck(), None)
        self.assertGreaterEqual(total, IDLE_GIVE_UP_FRAMES)
        self.assertLess(total, IDLE_GIVE_UP_FRAMES + 60)               # 原来推满 600 × 30 = 18000 帧
        self.assertTrue(any("停止自动推进" in str(n) for n in notes))
        self.assertTrue(self.s._idle_handover)
        # 下一次 act 不再先自动推进，按键照常执行
        self.s.last_obs = obs
        steps_before = len(self.s.client.steps)
        least = act_frame_min(self.cfg)
        outcome = self.s.act([f"{least},C"], "按确认试试", None)
        self.assertEqual(self.s.client.steps[steps_before:], [[f"{least},C"]])
        self.assertEqual(outcome.auto_frames, 0)
        # 恢复可操作后清掉，之后的不可操作状态照常自动推进
        self.s.client = StepClient([], make=lambda: level_obs(sid="Celeste/4-GoldenRidge", room="a-01",
                                                              state="Normal", controllable=True))
        self.s.act([f"{least},R"], "往右走", None)
        self.assertFalse(self.s._idle_handover)

    def test_short_dummy_stretches_still_advance_to_control(self):
        respawn = level_obs(sid="Celeste/4-GoldenRidge", room="a-01", state="IntroRespawn")
        ready = level_obs(sid="Celeste/4-GoldenRidge", room="a-01", state="Normal", controllable=True)
        self.s.client = StepClient([("done", 30, respawn, []), ("controllable", 12, ready, [])], make=lambda: ready)
        obs, _, total, notes = self.s._auto_advance(respawn, None)
        self.assertTrue(obs["controllable"])
        self.assertEqual(total, 42)
        self.assertFalse(any("停止自动推进" in str(n) for n in notes))
        self.assertFalse(self.s._idle_handover)

    def test_resume_handling_leaves_the_seeker_alone(self):
        self.s.client = StepClient([])
        self.s.last_obs = seeker_obs()
        self.assertEqual(self.s._resume_auto_handling(), [])
        self.assertEqual(self.s.client.steps, [])


if __name__ == "__main__":
    unittest.main()
