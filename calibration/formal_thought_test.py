"""仅正式思考（--formal-thought）、单次 act 的最短帧数（min_frames_per_act）与游戏内 HUD 的全透明样式。"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.game_session import ActError, GameSession
from orchestrator.launch_options import build, config_for
from orchestrator.metrics import Metrics
from orchestrator.prompting import current_rules_note, fill, play_prompt
from orchestrator.server import narrate_reminder
from orchestrator.thought import thought_problem
from overlay.history import PublicHistory
from overlay.modhud import render, view_model

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "configs" / "hud.json").read_text(encoding="utf-8"))


def cfg(**kw):
    c = RunConfig(agent="a", save="s", model="opus")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def explore(**kw):
    return cfg(mode="explore", knowledge_profile="explore", chapter=4, **kw)


class ThoughtLengthTests(unittest.TestCase):
    def test_off_keeps_the_old_limits(self):
        c = explore()
        self.assertIsNone(thought_problem(c, "字" * 30))
        self.assertIn("31 个汉字", thought_problem(c, "字" * 31))
        self.assertIn("31 个汉字", thought_problem(c, "字" * 31, temp=True))   # 没开时 narrate 与 act 规则相同

    def test_on_temp_thoughts_get_120_hanzi_and_act_stays_30(self):
        c = explore(formal_thought=True)
        self.assertIsNone(thought_problem(c, "字" * 120 + " wallbounce hyper", temp=True))
        self.assertIn("临时思考", thought_problem(c, "字" * 121, temp=True))
        self.assertIn("31 个汉字", thought_problem(c, "字" * 31))
        c.thought_temp_max_hanzi = 60
        self.assertIn("61 个汉字", thought_problem(c, "字" * 61, temp=True))

    def test_codex_executor_is_unchanged(self):
        c = explore(formal_thought=True, engine="codex")
        self.assertIsNone(thought_problem(c, "字" * 100, temp=True))
        self.assertIn("120 字以内", thought_problem(c, "字" * 121, temp=True))


class PromptTests(unittest.TestCase):
    def test_off_prompt_has_no_formal_rule(self):
        for c in (explore(), cfg(mode="speedrun", knowledge_profile="full", sim=True)):
            text = play_prompt(c)
            self.assertNotIn("仅正式思考", text)
            self.assertNotIn("{formal_thought", text)
            self.assertNotIn("{act_min_frames}", text)

    def test_on_prompt_explains_both_kinds(self):
        text = play_prompt(explore(formal_thought=True))
        self.assertIn("本次开启了“仅正式思考”", text)
        self.assertIn("每条汉字不超过 120 个", text)
        self.assertIn("act 的 thought（正式思考）尽量简短，汉字不超过 30 个", text)
        self.assertNotIn("思考语句尽量简短，汉字不超过 30 个", text)
        speed = play_prompt(cfg(mode="speedrun", knowledge_profile="full", sim=True, formal_thought=True))
        self.assertIn("本次开启了“仅正式思考”", speed)

    def test_tool_texts_and_reminder(self):
        from orchestrator.server import ACT_DESCRIPTION, NARRATE_DESCRIPTION, NARRATE_TEMP_DESCRIPTION
        on, off = explore(formal_thought=True), explore()
        self.assertIn("这里的 thought 是正式思考", fill(ACT_DESCRIPTION, on))
        self.assertNotIn("正式思考", fill(ACT_DESCRIPTION, off))
        self.assertIn("汉字不超过 120 个", fill(NARRATE_TEMP_DESCRIPTION, on))
        self.assertNotIn("{", fill(NARRATE_TEMP_DESCRIPTION, on))
        self.assertIn("HUD 上的思考内容", narrate_reminder(off, 31))
        self.assertIn("临时思考", narrate_reminder(on, 31))
        self.assertIn("HUD 上的思考内容", narrate_reminder(None, 31))
        self.assertIn("汉字不超过 30 个", fill(NARRATE_DESCRIPTION, off))

    def test_on_prompt_replaces_the_sentences_that_send_narrate_to_viewers(self):
        explore_on = play_prompt(explore(formal_thought=True))
        self.assertIn('用 narrate 写下临时思考', explore_on)
        self.assertNotIn('用 narrate 更新 HUD', explore_on)
        self.assertNotIn("告诉观众；", explore_on)
        speed_on = play_prompt(cfg(mode="speedrun", knowledge_profile="full", sim=True, formal_thought=True))
        self.assertIn("HUD 显示你最近一次 act 的 thought（正式思考）", speed_on)
        self.assertNotIn("只能看到 `narrate` 写下的内容", speed_on)
        self.assertLess(speed_on.index("本次开启了“仅正式思考”"), speed_on.index("这些是临时思考"))
        host = play_prompt(cfg(mode="speedrun", knowledge_profile="full", runtime="host", formal_thought=True))
        self.assertIn("本次开启了“仅正式思考”", host)          # 没有 hud_zh 时规则也在
        web = play_prompt(explore(formal_thought=True, hud_mod=False))
        self.assertIn("显示在控制台和网页 HUD 的历史里", web)

    def test_explore_prompt_encourages_hidden_rooms(self):
        self.assertIn("隐藏房间", play_prompt(explore()))

    def test_frame_range_in_prompt_and_resume_note(self):
        c = explore()
        self.assertIn("至少 40 帧、最多 150 帧", play_prompt(c))
        self.assertIn('"act_frames":[40,150]', current_rules_note(c))
        c.explore_act_frames = 30                     # 下限不超过上限
        self.assertIn("至少 30 帧、最多 30 帧", play_prompt(c))


class SwitchTests(unittest.TestCase):
    def test_launch_options_pass_the_switch(self):
        o = {"agent": "x", "mode": "explore", "formal_thought": True}
        self.assertIn("--formal-thought", build(o))
        self.assertTrue(config_for(o).formal_thought)
        self.assertNotIn("--formal-thought", build({"agent": "x"}))
        self.assertFalse(config_for({"agent": "x"}).formal_thought)

    def parse(self, root, *extra):
        from orchestrator.run import parse_args
        argv = ["run", "--engine", "codex", "--agent", "test", "--save", "selected", "--chapter", "0", *extra]
        with patch("orchestrator.config.REPO_ROOT", root), patch.object(sys, "argv", argv):
            return parse_args()[0]

    def test_command_line_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(self.parse(Path(folder), "--formal-thought").formal_thought)
            self.assertFalse(self.parse(Path(folder)).formal_thought)


class MetricsAndHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.m = Metrics(self.root / "metrics.json", "a", "opus", "high", 600)

    def test_formal_and_temp_thoughts(self):
        self.m.set_thought("先观察出口", formal=True)
        self.m.set_temp_thought("右边那块平台能借 liftboost，但风会把我吹回去")
        st = self.m.state
        self.assertEqual((st.thought, st.formal_thought), ("先观察出口", "先观察出口"))
        self.assertEqual(st.temp_thought, "右边那块平台能借 liftboost，但风会把我吹回去")
        self.m.set_thought("narrate 旧规则")
        self.assertEqual(st.formal_thought, "先观察出口")

    def test_console_history_labels_formal_thoughts(self):
        (self.root / "decisions.jsonl").write_text(json.dumps({"i": 1, "attempt": 0, "thought": "顺风 hyper", "t": 1})
                                                   + "\n", encoding="utf-8")
        items = PublicHistory(self.root).snapshot({"formal_only": True, "started_wall": 100})
        self.assertEqual([i["kind"] for i in items], ["正式思考"])
        items = PublicHistory(self.root).snapshot({"started_wall": 100})
        self.assertEqual([i["kind"] for i in items], ["尝试"])
        # 动作开始时已按"正式思考"记下：之后某次读到的统计是空的（文件正被替换），也不改成"尝试"
        from orchestrator.telemetry import publish
        other = self.root / "other"; other.mkdir()
        publish(other, "act:1:0", "顺风 hyper", kind="正式思考")
        (other / "decisions.jsonl").write_text((self.root / "decisions.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual([i["kind"] for i in PublicHistory(other).snapshot({})], ["正式思考"])


class ActMinimumTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cfg = RunConfig(agent="t", save="test", model="m", runtime="host", mode="explore",
                             knowledge_profile="explore", chapter=4, thought_lead_s=0)
        self.s = GameSession(self.cfg, root, Metrics(root / "metrics.json", "t", "m", "high", 600))
        self.addCleanup(self.s.close, False)

    def act(self, inputs, controllable=True):
        self.s.last_obs = {"scene": "Level", "controllable": controllable, "level": {"room": "a"}}
        done = ({"stop": "done", "consumed": 1, "planned": 1}, self.s.last_obs, None, 0, 0, [])
        with patch.object(self.s, "_execute", return_value=done) as execute:
            self.s.act(inputs, "试一下", None)
        return execute

    def test_short_act_is_rejected_while_controllable(self):
        with self.assertRaises(ActError) as caught:
            self.act(["30,R", "9"])
        self.assertIn("至少推进 40 帧（这次只有 39 帧）", str(caught.exception))
        self.act(["30,R", "10"]).assert_called_once()

    def test_menus_and_story_are_not_limited(self):
        self.act(["1,R"], controllable=False).assert_called_once()

    def test_last_tas_window_and_retry_padding(self):
        from orchestrator.game_session import PlanSpec
        self.s.last_obs = {"scene": "Level", "controllable": True, "level": {"room": "a"}}
        tail = PlanSpec(inputs=["10,R"], input_source={"offset": 90, "total": 100, "locations": [0] * 10})
        self.assertEqual(self.s._least_frames(tail, 10), 1)
        middle = PlanSpec(inputs=["10,R"], input_source={"offset": 10, "total": 100, "locations": [0] * 10})
        self.assertEqual(self.s._least_frames(middle, 10), 40)
        padded = self.s._pad_retry_plan(PlanSpec(inputs=["12,R", "1,R,J"]))
        self.assertEqual(padded.inputs, ["12,R", "1,R,J", "27"])
        self.assertEqual(len(self.s._check_plan(padded)), 40)
        long = PlanSpec(inputs=["45,R"])
        self.assertIs(self.s._pad_retry_plan(long), long)

    def test_tunable_minimum(self):
        self.cfg.min_frames_per_act = 10
        self.act(["10,R"]).assert_called_once()


class HudStyleTests(unittest.TestCase):
    def test_formal_only_shows_one_large_thought_and_keeps_status(self):
        now = time.time()
        m = {"agent": "opus5.5", "model": "opus", "status": "thinking", "chapter": "Celeste/4-GoldenRidge", "room": "a-02",
             "effort": "high", "thought": "临时的想法不上 HUD", "formal_only": True,
             "formal_thought": "顺风 hyper 越过尖刺", "formal_thought_wall": now - 3}
        history = [{"id": "x", "text": "临时思考", "kind": "临时思考", "wall": now - 9}]
        vm = view_model(m, CONFIG, history, now)
        self.assertEqual(vm.status, "思考中")
        self.assertEqual([(e.text, e.role) for e in vm.history], [("顺风 hyper 越过尖刺", "formal")])
        self.assertEqual(view_model({**m, "formal_thought": ""}, CONFIG, history, now).history, ())
        off = view_model({**m, "formal_only": False}, CONFIG, history, now)
        self.assertEqual([e.text for e in off.history], ["临时思考", "临时的想法不上 HUD"])

    def test_card_top_sits_at_ten_percent_of_the_view(self):
        import struct
        from overlay.modhud import MAPPING_SIZE, OFF_IMG_H, OFF_MARGIN_TOP, OFF_VIEW_W, ModHud, SharedHud
        now = time.time()
        m = {"agent": "a", "status": "thinking", "chapter": "Celeste/4-GoldenRidge", "formal_only": True,
             "formal_thought": "顺风 hyper 越过尖刺", "formal_thought_wall": now}
        with tempfile.TemporaryDirectory() as folder:
            shared = SharedHud(buffer=bytearray(MAPPING_SIZE))
            struct.pack_into("<ii", shared.buf, OFF_VIEW_W, 1920, 1080)
            hud = ModHud(Path(folder), shared=shared, metrics=m)
            with patch("overlay.history.read_history", return_value=[]):
                self.assertTrue(hud.tick())
                self.assertEqual(struct.unpack_from("<i", shared.buf, OFF_MARGIN_TOP)[0], 108)
                self.assertLessEqual(108 + struct.unpack_from("<i", shared.buf, OFF_IMG_H)[0], 1080 - 16)
                hud._config = {**hud._config, "mod_top": None}      # 不设时照旧贴着上边
                self.assertTrue(hud.tick())
                self.assertEqual(struct.unpack_from("<i", shared.buf, OFF_MARGIN_TOP)[0], 16)
            hud.close()

    def test_card_is_fully_transparent_by_default(self):
        now = time.time()
        vm = view_model({"agent": "a", "status": "thinking", "thought": "先观察。"}, CONFIG, [], now)
        image = render(vm, 350, CONFIG.get("colors")).image
        w, h = image.size
        self.assertEqual(image.getpixel((w - 6, h // 2))[3], 0)       # 卡片右缘：没有底色与边框
        self.assertGreater(image.getchannel("A").getextrema()[1], 200)  # 文字照常画出


if __name__ == "__main__":
    unittest.main()
