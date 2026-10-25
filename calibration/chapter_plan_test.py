"""运行中在控制台改之后要通关的章节：校验、队列、run_state 与控制指令。"""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from orchestrator.control import RunControl
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics


def session(folder: Path, chapter="Celeste/1-ForsakenCity", queue=(2,), splits=None, all_chapters=(0, 1, 2)):
    s = GameSession.__new__(GameSession)          # 不连游戏：只用到章节队列相关的属性
    s.lock = threading.RLock()
    s.done, s.chapter_finished = False, False
    s.chapter_queue = list(queue)
    s.run_dir = folder
    s.start_sid = chapter
    s.metrics = Metrics(folder / "metrics.json", "a", "opus", "high", 600)
    s.metrics.update(chapter=chapter, splits=list(splits or [{"chapter": "Celeste/0-Intro", "ticks": 1}]))
    (folder / "run_state.json").write_text(json.dumps({"all_chapters": list(all_chapters)}), encoding="utf-8")
    return s


class ChapterPlanTests(unittest.TestCase):
    def test_plan_and_edit(self):
        with tempfile.TemporaryDirectory() as d:
            s = session(Path(d))
            self.assertEqual(s.chapter_plan(), {"done": [0], "current": 1, "queue": [2], "switching": False,
                                                "editable": True})
            note = s.set_chapter_queue([4, 3])
            self.assertEqual(s.chapter_queue, [3, 4])                       # 按官方顺序
            self.assertIn("依次是", note)
            state = json.loads((Path(d) / "run_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["all_chapters"], [0, 1, 3, 4])           # 续跑按已通关章数从这里接着走
            self.assertIn("最后一章", s.set_chapter_queue([]))
            self.assertEqual(s.chapter_queue, [])
            self.assertEqual(json.loads((Path(d) / "run_state.json").read_text(encoding="utf-8"))["all_chapters"], [0, 1])

    def test_rejects_bad_edits(self):
        with tempfile.TemporaryDirectory() as d:
            s = session(Path(d))
            for bad in ([1], [0], [2, 2], ["2"], [99]):
                with self.assertRaises(ValueError, msg=bad):
                    s.set_chapter_queue(bad)
            s.chapter_finished = True
            with self.assertRaisesRegex(ValueError, "换到下一章"):
                s.set_chapter_queue([2])
            s.chapter_finished, s.done = False, True
            with self.assertRaisesRegex(ValueError, "已结束"):
                s.set_chapter_queue([2])

    def test_control_validates_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            control = RunControl(Path(d))
            self.assertTrue(control.enqueue("chapters", json.dumps({"queue": [2, 3]})))
            for bad in ("[]", json.dumps({"queue": "2"}), json.dumps({"queue": [-1]})):
                with self.assertRaises(ValueError):
                    control.enqueue("chapters", bad)


if __name__ == "__main__":
    unittest.main()
