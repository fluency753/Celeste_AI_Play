"""中断后保持冻结的运行可以唤起聊天 / 整理工作区（之后再 --resume），会话管理仍要求先停止试玩。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.chat import can_wake, can_wake_frozen, chat


def make_run(folder, **summary):
    run = Path(folder)
    (run / "run_state.json").write_text(json.dumps({"engine": "claude", "session_id": "abc"}), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run


class ChatFrozenTests(unittest.TestCase):
    def test_interrupted_frozen_run_can_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, user_stopped=False, game_released=False)
            self.assertTrue(can_wake_frozen(run))
            self.assertFalse(can_wake(run))       # 会话管理仍不允许

    def test_released_or_completed_runs_are_not_frozen(self):
        for summary in ({"completed": False, "user_stopped": False, "game_released": True},
                        {"completed": False, "user_stopped": False},
                        {"completed": True, "game_released": False}):
            with tempfile.TemporaryDirectory() as folder:
                self.assertFalse(can_wake_frozen(make_run(folder, **summary)), summary)

    def test_no_session_or_unknown_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, game_released=False)
            (run / "run_state.json").write_text(json.dumps({"engine": "claude"}), encoding="utf-8")
            self.assertFalse(can_wake_frozen(run))
            (run / "run_state.json").write_text(json.dumps({"engine": "nope", "session_id": "x"}), encoding="utf-8")
            self.assertFalse(can_wake_frozen(run))
        self.assertFalse(can_wake_frozen(None))

    def test_naming_is_not_allowed_on_frozen_run(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, game_released=False)
            with self.assertRaises(ValueError):
                asyncio.run(chat(run, name_request="r1"))

    def test_neither_state_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, user_stopped=False, game_released=True)
            with self.assertRaises(ValueError) as caught:
                asyncio.run(chat(run, allow_programs=True))
            self.assertIn("中断后保持冻结", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
