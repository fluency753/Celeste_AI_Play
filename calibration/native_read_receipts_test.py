import json
from pathlib import Path
import tempfile
import unittest

from orchestrator.native_read_receipts import NativeReadReceipts
from orchestrator.native_reading import parse_read_command


class _Study:
    def __init__(self, root):
        self.kb_dir = Path(root)
        self.files = ["a.md", "b.md"]
        self.extra = {}
        self.read = {rel: set() for rel in self.files}
        for rel in self.files:
            (self.kb_dir / rel).write_text("unique first\n共同第二行\nthird\n", encoding="utf-8")

    def container_path(self, rel):
        return "/kb/" + rel

    def note_lines(self, rel, lines):
        before = set(self.read[rel])
        self.read[rel].update(lines)
        return before != self.read[rel]


def _call(call_id="c", turn="t"):
    return {"type": "custom_tool_call", "name": "exec", "call_id": call_id,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn}}


def _output(text="unique first\n共同第二行\nthird\n", call_id="c", turn="t"):
    return {"type": "custom_tool_call_output", "call_id": call_id,
            "output": [{"type": "input_text", "text": text}],
            "internal_chat_message_metadata_passthrough": {"turn_id": turn}}


def _record(payload, kind="response_item"):
    return (json.dumps({"type": kind, "payload": payload}, ensure_ascii=False) + "\n").encode()


class NativeReadReceiptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.study = _Study(self.temp.name)
        self.tracker = NativeReadReceipts(self.study)
        self.tracker.begin_turn("t")
        self.intent = parse_read_command("cat /kb/a.md")

    def test_shared_study_empty_files_have_no_unread_content(self):
        from orchestrator.kb_study import KbStudy
        folder = self.study.kb_dir / 'empty'
        folder.mkdir()
        (folder / 'README.md').write_text('', encoding='utf-8')
        study = KbStudy(folder, 1)
        self.assertEqual(study.total['README.md'], 0)
        self.assertTrue(study.complete())
        self.assertTrue(study.ready())
        self.assertFalse(study.note_lines('README.md', [1]))

    def test_raw_final_text_is_receipt_and_replayed_rollout_deduplicates(self):
        self.tracker.response_item(_call())
        self.tracker.note_read(self.intent)
        self.assertTrue(self.tracker.response_item(_output()))
        self.tracker.begin_turn("t")
        self.tracker.ingest_rollout(_record(_call()) + _record(_output()))
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})
        self.assertFalse(self.tracker.response_item(_output()))

    def test_hidden_or_image_output_is_not_read(self):
        self.tracker.response_item(_call())
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output("HIDDEN_ONLY"))
        self.assertFalse(self.study.read["a.md"])
        self.tracker.response_item(_call("d"))
        self.tracker.note_read(self.intent)
        result = _output(call_id="d")
        result["output"] = [{"type": "input_image", "text": "unique first\n"}]
        self.tracker.response_item(result)
        self.assertFalse(self.study.read["a.md"])

    def test_resume_delayed_call_associates_one_pending_hook(self):
        self.tracker.note_read(self.intent)
        self.tracker.ingest_rollout(_record(_call()) + _record(_output()))
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})

    def test_two_reads_in_one_outer_call_get_no_credit(self):
        self.tracker.response_item(_call())
        self.tracker.note_read(self.intent)
        self.tracker.note_read(parse_read_command("cat /kb/b.md"))
        self.tracker.response_item(_output())
        self.assertFalse(any(self.study.read.values()))

    def test_parallel_calls_are_ambiguous_even_if_text_matches(self):
        self.tracker.response_item(_call())
        self.tracker.response_item(_call("d"))
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output())
        self.tracker.response_item(_output(call_id="d"))
        self.assertFalse(any(self.study.read.values()))

    def test_multiple_pending_hooks_do_not_guess_call_partition(self):
        self.tracker.note_read(self.intent)
        self.tracker.note_read(parse_read_command("cat /kb/b.md"))
        self.tracker.ingest_rollout(_record(_call()) + _record(_output())
                                    + _record(_call("d")) + _record(_output(call_id="d")))
        self.assertFalse(any(self.study.read.values()))
        # Once the ambiguous receipts have drained, one later explicit call is
        # independent. Retrying a clean read must not poison the entire turn.
        self.tracker.response_item(_call("clean"))
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output(call_id="clean"))
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})

    def test_metadata_query_does_not_make_single_content_read_ambiguous(self):
        self.tracker.response_item(_call())
        self.tracker.note_read(parse_read_command("ls /kb"))
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output())
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})

    def test_turn_boundaries_and_unknown_turn_do_not_credit_old_data(self):
        self.tracker.begin_turn("new")
        self.tracker.note_read(self.intent)
        self.tracker.ingest_rollout(_record(_call()) + _record(_output()))
        self.assertFalse(any(self.study.read.values()))
        self.tracker.begin_turn(None)
        self.tracker.response_item(_call())
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output())
        self.assertFalse(any(self.study.read.values()))

    def test_explicit_turn_cannot_override_conflicting_metadata(self):
        self.tracker.response_item(_call(turn="old"), turn_id="t")
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output(turn="old"), turn_id="t")
        self.assertFalse(any(self.study.read.values()))

    def test_no_metadata_requires_explicit_turn_or_rollout_turn_context(self):
        call, output = _call(), _output()
        call.pop("internal_chat_message_metadata_passthrough")
        output.pop("internal_chat_message_metadata_passthrough")
        self.tracker.response_item(call)
        self.tracker.note_read(self.intent)
        self.tracker.response_item(output)
        self.assertFalse(self.study.read["a.md"])
        context = _record({"type": "task_started", "turn_id": "t"}, "event_msg")
        self.tracker.ingest_rollout(context + _record(call) + _record(output))
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})

    def test_partial_utf8_json_line_is_retained_until_newline(self):
        self.tracker.note_read(self.intent)
        records = _record(_call()) + _record(_output())
        cut = records.index("共同".encode()) + 1
        self.assertEqual(self.tracker.ingest_rollout(records[:cut]), 1)
        self.assertFalse(self.study.read["a.md"])
        self.assertEqual(self.tracker.ingest_rollout(records[cut:-1]), 0)
        self.assertFalse(self.study.read["a.md"])
        self.assertEqual(self.tracker.ingest_rollout(b"\n"), 1)
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})

    def test_malformed_records_do_not_break_following_receipts(self):
        self.tracker.note_read(self.intent)
        self.tracker.ingest_rollout(b"bad\n[]\n" + _record(_call()) + _record(_output()))
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})

    def test_separate_text_blocks_do_not_join_partial_source_lines(self):
        self.tracker.response_item(_call())
        self.tracker.note_read(self.intent)
        output = _output()
        output["output"] = [{"type": "input_text", "text": "unique "},
                            {"type": "input_text", "text": "first\n共同第二行\nthi"}]
        self.tracker.response_item(output)
        self.assertEqual(self.study.read["a.md"], {2})

    def test_source_range_limits_visible_matching_lines(self):
        self.tracker.response_item(_call())
        self.tracker.note_read(parse_read_command("sed -n '2p' /kb/a.md"))
        self.tracker.response_item(_output())
        self.assertEqual(self.study.read["a.md"], {2})

    def test_function_output_and_explicit_current_turn_supported(self):
        call, output = _call(), _output()
        call["type"], output["type"] = "function_call", "function_call_output"
        call.pop("internal_chat_message_metadata_passthrough")
        output.pop("internal_chat_message_metadata_passthrough")
        output["output"] = "unique first\n"
        self.tracker.response_item(call, turn_id="t")
        self.tracker.note_read(self.intent)
        self.tracker.response_item(output, turn_id="t")
        self.assertEqual(self.study.read["a.md"], {1})

    def test_transport_header_is_not_source_content(self):
        header = "Script completed\nWall time 0.3 seconds\nOutput:\n"
        (self.study.kb_dir / "a.md").write_text(header, encoding="utf-8")
        self.tracker.response_item(_call())
        self.tracker.note_read(self.intent)
        self.tracker.response_item(_output(header))
        self.assertFalse(self.study.read["a.md"])

    def test_extra_source_and_unavailable_source(self):
        extra = self.study.kb_dir / "outside.txt"
        extra.write_text("outside\n", encoding="utf-8")
        rel = "/sim/README.md"
        self.study.files.append(rel)
        self.study.extra[rel] = extra
        self.study.read[rel] = set()
        self.study.container_path = lambda name: name if name in self.study.extra else "/kb/" + name
        self.tracker.response_item(_call())
        self.tracker.note_read(parse_read_command("cat /sim/README.md"))
        self.tracker.response_item(_output("outside\n"))
        self.assertEqual(self.study.read[rel], {1})
        extra.unlink()
        self.tracker.response_item(_call("d"))
        self.tracker.note_read(parse_read_command("cat /sim/README.md"))
        self.assertFalse(self.tracker.response_item(_output("outside\n", call_id="d")))

    def test_oversized_incomplete_record_does_not_grow_buffer_or_consume_next(self):
        self.tracker.max_record_bytes = 1024
        self.tracker.ingest_rollout(b"x" * 1025)
        self.assertEqual(self.tracker._tail, b"")
        self.tracker.ingest_rollout(b"x" * 1025)
        self.assertEqual(self.tracker._tail, b"")
        self.tracker.note_read(self.intent)
        self.tracker.ingest_rollout(b"tail\n" + _record(_call()) + _record(_output()))
        self.assertEqual(self.study.read["a.md"], {1, 2, 3})


if __name__ == "__main__":
    unittest.main()
