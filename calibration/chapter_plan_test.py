"""按段通关与运行中调整要通关的范围：分段、截的终点、换到下一截、run_state 与控制指令。"""
import json
from contextlib import ExitStack
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.segments import Piece, parse_segments, piece_label, pieces_of, table_from_areas, validate_segments
from tools.enter_save import enter_save

AREAS = [
    {"id": 0, "checkpoints": [{"index": 0, "level": "0", "name": "开始"}]},
    {"id": 1, "checkpoints": [{"index": 0, "level": "1", "name": "开始"}, {"index": 1, "level": "6", "name": "渡口"},
                              {"index": 2, "level": "9b", "name": "深渊"}]},
    {"id": 2, "checkpoints": [{"index": 0, "level": "start", "name": "开始"}, {"index": 1, "level": "3", "name": "干预"},
                              {"index": 2, "level": "end_3", "name": "清醒"}]},
]
TABLE = table_from_areas(AREAS)


def session(folder: Path, plan, splits=(), segment=None):
    cfg = RunConfig(agent="a", save="s", model="opus", runtime="host")
    cfg.plan, cfg.checkpoints = list(plan), TABLE
    cfg.resume_segment = segment
    metrics = Metrics(folder / "metrics.json", "a", "opus", "high", 600)
    metrics.update(chapter="Celeste/1-ForsakenCity", splits=list(splits))
    s = GameSession(cfg, folder, metrics)
    (folder / "run_state.json").write_text(json.dumps({"all_chapters": [1]}), encoding="utf-8")
    return s


def state(folder: Path) -> dict:
    return json.loads((folder / "run_state.json").read_text(encoding="utf-8"))


class SegmentTests(unittest.TestCase):
    def test_parse_and_pieces(self):
        segs = parse_segments("1.1-2,2", TABLE)
        self.assertEqual(segs, [(1, 1), (1, 2), (2, 0), (2, 1), (2, 2)])
        self.assertEqual(pieces_of(segs, TABLE), [Piece(1, 1, None), Piece(2, 0, None)])
        self.assertEqual(pieces_of(parse_segments("1.0,1.2", TABLE), TABLE), [Piece(1, 0, 1), Piece(1, 2, None)])
        self.assertEqual(pieces_of(parse_segments("0,1", TABLE), TABLE), [Piece(0), Piece(1)])   # 整章
        for bad in ("1.3", "99", "x", "1.2-1", ""):
            with self.assertRaises(ValueError, msg=bad):
                parse_segments(bad, TABLE)
        self.assertEqual(validate_segments([[2, 0], [1, 2]], TABLE), [(1, 2), (2, 0)])
        for bad in ([], [[1, 3]], [["1", 0]], [[1]], "1"):
            with self.assertRaises(ValueError, msg=bad):
                validate_segments(bad, TABLE)
        self.assertIn("渡口 → 深渊", piece_label(Piece(1, 1, 2), TABLE))
        self.assertEqual(piece_label(Piece(1), TABLE), "1 被遗弃之城")


class PlanTests(unittest.TestCase):
    def test_piece_ends_on_entering_end_checkpoint(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as cleanup:
            s = session(Path(d), [Piece(1, 0, 1), Piece(1, 2, None), Piece(2)])
            cleanup.callback(s._frames_log.close)
            cleanup.callback(s._decisions_log.close)
            self.assertEqual(s._stop_rooms(), {"stop_rooms": ["6"]})
            self.assertFalse(s._note_room("Celeste/1-ForsakenCity", "5", 100))
            self.assertTrue(s._note_room("Celeste/1-ForsakenCity", "6", 1234))
            split = s.metrics.state.splits[-1]
            self.assertEqual((split["ticks"], split["from"], split["to"]), (1234, 0, 1))
            self.assertTrue(s.chapter_finished and s.metrics.state.piece_done)
            self.assertEqual(s._stop_rooms(), {})
            saved = state(Path(d))
            self.assertEqual(saved["plan_pieces"], [[1, 0, 1], [1, 2, None], [2, 0, None]])
            self.assertTrue(saved["piece_pending"])
            self.assertEqual(s.chapter_plan()["segments"]["done"], [[1, 0]])

    def test_last_piece_finishes_the_run(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as cleanup:
            s = session(Path(d), [Piece(1, 1, 2)])
            cleanup.callback(s._frames_log.close)
            cleanup.callback(s._decisions_log.close)
            self.assertTrue(s._note_room("Celeste/1-ForsakenCity", "9b", 50))
            self.assertTrue(s.done)
            self.assertEqual(s.done_reason, "分段完成")

    def test_segment_tracking_and_runtime_edit(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as cleanup:
            s = session(Path(d), [Piece(1)])                  # 整章 1A
            cleanup.callback(s._frames_log.close)
            cleanup.callback(s._decisions_log.close)
            self.assertEqual(s._stop_rooms(), {})
            s._note_room("Celeste/1-ForsakenCity", "6")
            self.assertEqual(s.segment_index, 1)
            self.assertEqual(state(Path(d))["segment_index"], 1)
            plan = s.chapter_plan()["segments"]
            self.assertEqual((plan["current"], plan["queue"]), ([1, 1], [[1, 2]]))
            note = s.set_plan([[2, 0]])                         # 不玩 1A 的最后一段：到"深渊"为止，再去 2A 开头一段
            self.assertEqual(s.piece, Piece(1, 0, 2))
            self.assertEqual(s.chapter_queue, [Piece(2, 0, 1)])
            self.assertIn("进入房间 9b", note)
            self.assertEqual(state(Path(d))["plan_pieces"], [[1, 0, 2], [2, 0, 1]])
            s.set_plan([[1, 2]])                                # 又加回来：并进当前这一截，之后不再有别的
            self.assertEqual((s.piece, s.chapter_queue), (Piece(1, 0, None), []))
            self.assertIn("最后一截", s.set_plan([]))           # 空 = 当前这一段完成后结束
            self.assertEqual(s.piece, Piece(1, 0, 2))
            for bad in ([[1, 1]], [[0, 0]], [[1, 5]]):
                with self.assertRaises(ValueError, msg=bad):
                    s.set_plan(bad)
            s.chapter_finished = True
            with self.assertRaisesRegex(ValueError, "换到下一截"):
                s.set_plan([[2, 0]])
            s.chapter_finished, s.done = False, True
            with self.assertRaisesRegex(ValueError, "已结束"):
                s.set_plan([[2, 0]])

    def test_old_chapter_queue_control_still_works(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as cleanup:
            s = session(Path(d), [Piece(1)])
            cleanup.callback(s._frames_log.close)
            cleanup.callback(s._decisions_log.close)
            s.set_chapter_queue([2])
            self.assertEqual((s.piece, s.chapter_queue), (Piece(1), [Piece(2)]))
            with self.assertRaises(ValueError):
                s.set_chapter_queue([1])

    def test_next_piece_teleports_to_its_start(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as cleanup:
            s = session(Path(d), [Piece(1, 0, 1), Piece(1, 2, None)])
            cleanup.callback(s._frames_log.close)
            cleanup.callback(s._decisions_log.close)
            s._note_room("Celeste/1-ForsakenCity", "6", 10)
            arrived = {"scene": "Level", "controllable": True,
                       "level": {"sid": "Celeste/1-ForsakenCity", "room": "9b", "chapterTimeTicks": 0}}

            class Client:
                def call(self, cmd, **args):
                    return {"obs": arrived}
            s.client = Client()
            with patch("enter_save.enter_save") as macro:
                obs, _, notes = s._enter_next_chapter()
            self.assertEqual(macro.call_args.kwargs["checkpoint"], 2)
            self.assertEqual(macro.call_args.kwargs["chapter"], 1)
            self.assertEqual((s.piece, s.chapter_finished, s.metrics.state.piece_done), (Piece(1, 2, None), False, False))
            self.assertIn("送到", notes[0])
            self.assertIn("章节通关为止", notes[0])
            self.assertFalse(state(Path(d))["piece_pending"])


class MacroAndControlTests(unittest.TestCase):
    def test_enter_save_teleports_to_checkpoint(self):
        level = {"scene": "Level", "controllable": True, "save": {"name": "s", "slot": 1},
                 "level": {"sid": "Celeste/1-ForsakenCity", "room": "6", "inCutscene": False, "skippingCutscene": False}}
        calls = []

        class Client:
            def call(self, cmd, **args):
                calls.append((cmd, args))
                if cmd == "hello":
                    return {"checkpoints": True}
                if cmd == "enter_checkpoint":
                    level["level"]["room"] = "9b"
                    return {"room": "9b"}
                return {"obs": json.loads(json.dumps(level))}
        obs = enter_save(Client(), "s", chapter=1, checkpoint=2, save_slot=1)
        self.assertEqual(obs["level"]["room"], "9b")
        self.assertIn(("enter_checkpoint", {"chapter": 1, "checkpoint": 2}), calls)

    def test_control_validates_chapters(self):
        with tempfile.TemporaryDirectory() as d:
            control = RunControl(Path(d))
            self.assertTrue(control.enqueue("chapters", json.dumps({"queue": [2, 3]})))
            self.assertTrue(control.enqueue("chapters", json.dumps({"segments": [[1, 2], [2, 0]]})))
            self.assertTrue(control.enqueue("chapters", json.dumps({"segments": []})))
            for bad in ("[]", json.dumps({"queue": "2"}), json.dumps({"queue": [-1]}),
                        json.dumps({"segments": [[1]]}), json.dumps({"segments": "x"})):
                with self.assertRaises(ValueError):
                    control.enqueue("chapters", bad)



class ResumeExtensionTests(unittest.TestCase):
    """原计划全部完成的运行：续跑时用 --chapters / --segments 追加接下来要玩的部分，同一会话与统计接着算。"""

    def make_run(self, root: Path, pieces, splits, extra=None) -> Path:
        run = root / "runs" / "a" / "20261025-042029"
        run.mkdir(parents=True)
        state = {"agent": "a", "save": "s", "engine": "claude", "model": "opus", "effort": "high", "max_minutes": 1440,
                 "runtime": "host", "sim": False, "record": False, "mode": "explore", "knowledge_profile": "explore",
                 "all_chapters": sorted({p[0] for p in pieces}), "plan_pieces": pieces, "piece_pending": False,
                 "segment_index": 2, "session_id": "sess", "tuning": {"explore_program_s": None}, **(extra or {})}
        (run / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
        (run / "metrics.json").write_text(json.dumps({"splits": splits, "effort": "high"}), encoding="utf-8")
        return run

    def parse(self, root: Path, *argv):
        import sys
        from orchestrator.run import parse_args
        with patch("orchestrator.config.REPO_ROOT", root), patch("orchestrator.segments.load_table", return_value=TABLE), \
             patch.object(sys, "argv", ["run", "--agent", "a", "--save", "s", *argv]):
            return parse_args()

    def test_completed_plan_can_continue_with_next_chapter(self):
        from orchestrator.run import write_run_state
        done = [{"chapter": "Celeste/0-Intro"}, {"chapter": "Celeste/1-ForsakenCity"}, {"chapter": "Celeste/2-OldSite"}]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = self.make_run(root, [[0, 0, None], [1, 0, None], [2, 0, None]], done)
            with self.assertRaisesRegex(SystemExit, "--chapters"):
                self.parse(root, "--resume", str(run))
            cfg, args = self.parse(root, "--resume", str(run), "--chapters", "3")
            self.assertEqual((cfg.plan, cfg.chapters, cfg.chapter), ([Piece(3)], [3], 3))
            self.assertIsNone(cfg.resume_segment)                 # 新的一章从开头玩，不沿用上一章的段号
            self.assertFalse(cfg.resume_pending)
            self.assertEqual((cfg.max_minutes, cfg.tuning), (1440, {"explore_program_s": None}))   # 其余设置沿用
            out = root / "next"
            out.mkdir()
            write_run_state(out, cfg, "sess", args.previous)
            state = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["plan_pieces"], [[0, 0, None], [1, 0, None], [2, 0, None], [3, 0, None]])
            self.assertEqual(state["all_chapters"], [0, 1, 2, 3])
            cfg, _ = self.parse(root, "--resume", str(run), "--segments", "2.1")   # 也可以只追加某几段
            self.assertEqual(cfg.plan, [Piece(2, 1, 2)])

    def test_unfinished_plan_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = self.make_run(root, [[1, 0, None], [2, 0, None]], [{"chapter": "Celeste/1-ForsakenCity"}])
            cfg, _ = self.parse(root, "--resume", str(run))
            self.assertEqual(cfg.plan, [Piece(2)])
            with self.assertRaisesRegex(SystemExit, "还没有完成"):
                self.parse(root, "--resume", str(run), "--chapters", "3")

if __name__ == "__main__":
    unittest.main()
