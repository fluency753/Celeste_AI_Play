"""HUD 会话适配的边界测试：口径、增量读取、隐私过滤与独立 agent 隔离。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from overlay.codex_session import SessionTelemetry, apply_session, _readers
from overlay.server import make_handler


def event(kind, payload, second=0):
    return {"timestamp": f"2026-10-24T00:00:{second:02d}Z", "type": kind, "payload": payload}


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "session.jsonl"
        self.path.touch()

    def tearDown(self):
        self.temp.cleanup()
        _readers.clear()

    def write(self, *records):
        with self.path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")

    def test_actual_metadata_last_context_and_no_private_reasoning(self):
        self.write(event("turn_context", {"model": "actual", "effort": "medium"}),
                   event("event_msg", {"type": "token_count", "info": {
                       "total_token_usage": {"total_tokens": 99999999},
                       "last_token_usage": {"input_tokens": 1200, "output_tokens": 30},
                       "model_context_window": 4000}}),
                   event("response_item", {"type": "reasoning", "text": "NEVER_EXPORT"}),
                   event("response_item", {"type": "message", "role": "assistant", "phase": "analysis",
                                           "content": [{"type": "output_text", "text": "NEVER_EXPORT"}]}),
                   event("response_item", {"type": "message", "role": "assistant", "phase": "commentary",
                                           "content": [{"type": "output_text", "text": "公开解说"}]}),
                   event("compacted", {"summary": "NEVER_EXPORT"}),
                   event("event_msg", {"type": "item_completed", "item": {
                       "type": "ContextCompaction", "id": "compact"}}))
        data = SessionTelemetry(self.path).snapshot()
        self.assertEqual((data["model"], data["effort"]), ("actual", "medium"))
        self.assertEqual(data["context_tokens"], 1230)
        self.assertEqual(data["compactions"], 1)
        self.assertEqual(data["public_commentary"], "公开解说")
        self.assertNotIn("NEVER_EXPORT", json.dumps(data))

    def test_overlapping_tools_are_not_double_counted(self):
        self.write(event("response_item", {"type": "function_call", "name": "first", "call_id": "1"}, 1),
                   event("response_item", {"type": "custom_tool_call", "name": "second", "call_id": "2"}, 2),
                   event("response_item", {"type": "function_call_output", "call_id": "1"}, 4),
                   event("response_item", {"type": "custom_tool_call_output", "call_id": "2"}, 6))
        data = SessionTelemetry(self.path).snapshot()
        self.assertEqual(data["tool_calls"], 2)
        self.assertEqual(data["tool_s"], 5)
        self.assertEqual(data["tool_call"], "")

    def test_partial_utf8_record_and_file_truncation(self):
        record = json.dumps(event("turn_context", {"model": "模型", "effort": "low"}), ensure_ascii=False).encode()
        self.path.write_bytes(record[:-2])
        reader = SessionTelemetry(self.path)
        self.assertIsNone(reader.snapshot(now=100)["model"])
        with self.path.open("ab") as stream:
            stream.write(record[-2:] + b"\n")
        self.assertEqual(reader.snapshot(now=101)["model"], "模型")
        self.path.write_bytes(b"\n")
        self.assertIsNone(reader.snapshot(now=102)["model"])

    def test_interrupted_tool_does_not_stay_active_next_turn(self):
        self.write(event("response_item", {"type": "function_call", "name": "old", "call_id": "1"}, 1),
                   event("event_msg", {"type": "task_started"}, 3))
        self.assertEqual(SessionTelemetry(self.path).snapshot()["tool_call"], "")

    def test_only_explicitly_attached_chat_changes_response(self):
        thread = "abcd-1234"
        root = Path(self.temp.name)
        sessions = root / "sessions"
        sessions.mkdir()
        self.path.rename(sessions / f"rollout-{thread}.jsonl")
        self.path = sessions / f"rollout-{thread}.jsonl"
        self.write(event("turn_context", {"model": "actual", "effort": "low"}))
        agent = {"agent": "native", "telemetry_source": "agent", "model": "native-model"}
        config = {"names": {"native": "Native"}}
        with patch.dict("os.environ", {"CODEX_HOME": str(root)}):
            self.assertEqual(apply_session(agent, config), (agent, config))
            attached = {**agent, "telemetry_source": "codex_session", "chat_thread_id": thread,
                        "deaths": 2, "chapter_time_ticks": 100}
            metrics, updated = apply_session(attached, config)
        self.assertEqual(metrics["model"], "actual")
        self.assertEqual(metrics["deaths"], 2)
        self.assertEqual(metrics["chapter_time_ticks"], 100)
        self.assertEqual(config["names"]["native"], "Native")
        self.assertEqual(updated["names"]["native"], "actual")

    def test_missing_log_does_not_keep_placeholder_model(self):
        with patch.dict("os.environ", {"CODEX_HOME": self.temp.name}):
            data, _ = apply_session({"agent": "direct", "telemetry_source": "codex_session",
                                     "chat_thread_id": "abcd", "model": "placeholder", "effort": "high"}, {})
        self.assertIsNone(data["model"])
        self.assertIsNone(data["effort"])
        self.assertFalse(data["chat"]["available"])

    def test_unattached_external_client_never_shows_config_as_telemetry(self):
        for source, thread in (("external", None), ("codex_session", "invalid/path")):
            data, _ = apply_session({"agent": "direct", "telemetry_source": source,
                                     "chat_thread_id": thread, "model": "placeholder", "effort": "high"}, {})
            self.assertIsNone(data["model"])
            self.assertIsNone(data["effort"])

    def test_http_response_uses_attached_thread_without_rewriting_game_stats(self):
        root = Path(self.temp.name)
        sessions = root / "sessions"
        sessions.mkdir()
        self.path.rename(sessions / "rollout-abcd.jsonl")
        self.path = sessions / "rollout-abcd.jsonl"
        self.write(event("turn_context", {"model": "wire-model", "effort": "low"}))
        run = root / "run"
        run.mkdir()
        original = {"agent": "direct", "deaths": 2, "model": "placeholder"}
        (run / "metrics.json").write_text(json.dumps(original), encoding="utf-8")
        with patch.dict("os.environ", {"CODEX_HOME": str(root)}):
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(run, "abcd"))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{server.server_port}/hud/state") as reply:
                    data = json.load(reply)
                self.assertEqual(data["metrics"]["model"], "wire-model")
                self.assertEqual(data["metrics"]["deaths"], 2)
                self.assertTrue(data["metrics"]["chat"]["available"])
                self.assertEqual(json.loads((run / "metrics.json").read_text()), original)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


if __name__ == "__main__":
    unittest.main()
