import unittest

from orchestrator.exploration import action_hint


def obs(x=0, y=0, room="a", controllable=True):
    return {"level": {"sid": "map", "room": room}, "player": {"pos": [x, y]},
            "controllable": controllable}


class ExplorationTests(unittest.TestCase):
    def test_first_blocked_action_triggers_without_retries(self):
        hint = action_hint(obs(), obs(1), {"consumed": 30, "stop": "done"}, ["30,R"])
        self.assertIn("可能受阻", hint)
        self.assertNotIn("simulate", hint)                 # 没开限额预测时不提 simulate
        self.assertIn("simulate", action_hint(obs(), obs(1), {"consumed": 30, "stop": "done"}, ["30,R"], simulate=True))

    def test_first_death_triggers_even_after_respawn(self):
        self.assertIn("死亡", action_hint(obs(), obs(), {"stop": "death"}, ["30,R"]))

    def test_no_false_block_for_climbing_taps_idle_or_new_room(self):
        for after, inputs, frames in [(obs(0, -20), ["30,R,G"], 30),
                                       (obs(), ["1,R"], 1), (obs(), ["30"], 30),
                                       (obs(room="b"), ["30,R"], 30)]:
            self.assertEqual(action_hint(obs(), after, {"consumed": frames}, inputs), "")

    def test_long_wait_triggers_without_any_collision(self):
        self.assertIn("等待了 35 秒", action_hint(obs(), obs(50), {"consumed": 30}, ["30,R"], 35))
        self.assertEqual(action_hint(obs(), obs(controllable=False), {"stop": "death"}, [], 35), "")
