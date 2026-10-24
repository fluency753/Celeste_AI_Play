import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.metrics import Metrics


class DecisionTimingTests(unittest.TestCase):
    def test_fast_snapshot_wait_excludes_tools_pause_and_idle_between_turns(self):
        with tempfile.TemporaryDirectory() as tmp, patch("time.monotonic") as clock:
            clock.return_value = 100
            metrics = Metrics(Path(tmp) / "metrics.json", "test", "model", "high", 600)
            metrics.turn_begin(fast=True, service_tier="priority")
            clock.return_value = 102
            metrics.turn_acknowledged("turn-a")
            clock.return_value = 110
            metrics.tool_begin("read", label="Read")
            clock.return_value = 115
            metrics.tool_end("read")
            clock.return_value = 120
            metrics.tool_begin("act", True, "act")
            metrics.decision_begin()
            self.assertEqual(metrics.state.decision_wait_s, 15)  # includes thinking before Read
            clock.return_value = 122
            metrics.decision_end({"planned": 30, "consumed": 20, "stop": "room"})
            metrics.tool_end("act")
            metrics.turn_end()
            clock.return_value = 200
            metrics.turn_begin(fast=False, service_tier="default")
            clock.return_value = 203
            metrics.pause()
            clock.return_value = 503
            metrics.resume(quota=False)
            # Controllers resume through a fresh model turn, never count 300s pause as latency.
            metrics.turn_end()
            metrics.turn_begin(fast=False, service_tier="default")
            clock.return_value = 507
            metrics.tool_begin("act2", True, "act")
            metrics.decision_begin()
            self.assertEqual(metrics.state.decision_wait_s, 4)
            metrics.tool_end("act2")
            metrics.turn_end()
            metrics.turn_end()  # cleanup isn't another turn
            rows = [json.loads(s) for s in (Path(tmp) / "decision_timing.jsonl").read_text().splitlines()]
            requests = [r for r in rows if r["event"] == "turn_requested"]
            self.assertEqual([r["fast_requested"] for r in requests], [True, False, False])
            ack = next(r for r in rows if r["event"] == "turn_acknowledged")
            self.assertEqual(ack["request_ack_s"], 2)
            self.assertEqual([r["active_s"] for r in rows if r["event"] == "turn_ended"], [22, 3, 4])
            self.assertNotIn("service_tier_actual", ack)
            decision = next(r for r in rows if r["event"] == "decision_completed")
            self.assertEqual(decision["result"]["consumed"], 20)

    def test_start_play_does_not_reset_timing_clock(self):
        with tempfile.TemporaryDirectory() as tmp, patch("time.monotonic") as clock:
            clock.return_value = 100
            metrics = Metrics(Path(tmp) / "metrics.json", "claude", "model", "high", 600, study=True)
            metrics.turn_begin()
            clock.return_value = 160
            metrics.start_play()
            clock.return_value = 165
            metrics.turn_acknowledged("a")
            row = json.loads((Path(tmp) / "decision_timing.jsonl").read_text().splitlines()[-1])
            self.assertEqual(row["request_ack_s"], 65)
            self.assertIsNone(row["fast_requested"])
