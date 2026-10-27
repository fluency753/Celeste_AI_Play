"""--note-file 的局间留言除了附在开场消息里，也要出现在控制台的对话栏（公开事件，角色是用户）。"""
import tempfile
import unittest
from pathlib import Path

from orchestrator.run import publish_user_note
from overlay.history import PublicHistory


class NoteVisibleTests(unittest.TestCase):
    def test_note_shows_up_as_a_user_message(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            publish_user_note(run, "5A 留言：先整理笔记")
            items = PublicHistory(run).snapshot({}, 12)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["role"], "user")
            self.assertEqual(items[0]["text"], "5A 留言：先整理笔记")
            self.assertIn("局间留言", items[0]["kind"])

    def test_no_note_no_event(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            publish_user_note(run, None)
            self.assertFalse((run / "public_events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
