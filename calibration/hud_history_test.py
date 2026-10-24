"""公开解说历史：完整文本、增量读取、隐私过滤和保留范围。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from overlay.history import PublicHistory


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = self.root / "codex_events.jsonl"
        self.reader = PublicHistory(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def emit(self, item, stamp=10000):
        with self.events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"method": "item/completed", "params": {
                "completedAtMs": stamp, "item": item}}, ensure_ascii=False) + "\n")

    def test_full_public_message_without_reasoning_or_stream_fragments(self):
        self.emit({"type": "reasoning", "id": "secret", "text": "PRIVATE"})
        self.emit({"type": "agentMessage", "id": "public", "text": "完整解说" * 100})
        data = self.reader.snapshot({})
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["text"], "完整解说" * 100)
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertEqual(self.reader.snapshot({}), data)

    def test_partial_line_waits_until_complete_and_decisions_are_ordered(self):
        raw = json.dumps({"method": "item/completed", "params": {"completedAtMs": 12000,
                         "item": {"type": "agentMessage", "id": "p", "text": "完整一句"}}}).encode()
        self.events.write_bytes(raw[:20])
        self.assertEqual(self.reader.snapshot({}), [])
        with self.events.open("ab") as stream:
            stream.write(raw[20:] + b"\n")
        (self.root / "decisions.jsonl").write_text(json.dumps({"i": 1, "t": 1, "thought": "先跳上台阶"}) + "\n", encoding="utf-8")
        result = self.reader.snapshot({"started_wall": 10})
        self.assertEqual([item["kind"] for item in result], ["尝试", "解说"])

    def test_only_recent_twelve_messages_are_retained(self):
        for n in range(80):
            self.emit({"type": "agentMessage", "id": str(n), "text": f"消息 {n}"}, stamp=n*1000)
        data = self.reader.snapshot({})
        self.assertEqual(len(data), 12)
        self.assertEqual(data[0]["text"], "消息 68")
        self.assertEqual(data[-1]["text"], "消息 79")

    def test_compaction_system_message_updates_without_private_summary(self):
        started = {"method": "item/started", "params": {"startedAtMs": 123000,
                   "item": {"type": "contextCompaction", "id": "compact", "summary": "PRIVATE"}}}
        self.events.write_text(json.dumps(started) + "\n", encoding="utf-8")
        first = self.reader.snapshot({})
        self.assertEqual(first[0]["role"], "system")
        self.assertIn("正在压缩", first[0]["text"])
        self.emit({"type": "contextCompaction", "id": "compact", "summary": "PRIVATE"})
        final = self.reader.snapshot({})
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["text"], "上下文压缩完成。")
        self.assertEqual(final[0]["wall"], 123)
        self.assertNotIn("PRIVATE", json.dumps(final))


if __name__ == "__main__":
    unittest.main()
