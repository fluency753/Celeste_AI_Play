"""Credit native reads only from the final, model-visible outer tool result.

Raw Responses items and trusted rollout tails feed the same receiver. The caller
owns the rollout byte offset and must provide new bytes only, from this session.
No command stdout, tool-reported counts, or model-written log is evidence here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

from .native_reading import ReadIntent, matched_read_lines


@dataclass
class _Call:
    reads: list[ReadIntent] = field(default_factory=list)
    ambiguous: bool = False


class NativeReadReceipts:
    def __init__(self, study, *, max_record_bytes: int = 8 * 1024 * 1024):
        self.study = study
        self.max_record_bytes = max_record_bytes
        self.turn_id: str | None = None
        self._tail = b""
        self._discard_line = False
        self._rollout_turn: str | None = None
        self.begin_turn(None)

    def begin_turn(self, turn_id: str | None) -> None:
        """Start at turn/started, before native Pre hooks. None disables credit.

        Repeated notifications for the same known turn preserve deduplication.
        The caller should seed its external rollout offset at EOF before a turn.
        """
        if turn_id is not None and turn_id == self.turn_id:
            return
        self.turn_id = turn_id
        self._calls: dict[str, _Call] = {}
        self._seen_calls: set[str] = set()
        self._outputs: set[str] = set()
        self._pending: list[ReadIntent] = []
        self._orphaned = False
        self._tail = b""
        self._discard_line = False
        self._rollout_turn = None

    def note_read(self, intent: ReadIntent) -> None:
        """Record a successful command with verified execution cwd, not its stdout.

        One outer call must contain exactly one content read. Multiple outstanding
        calls cannot be associated with hook IDs and therefore receive no credit.
        """
        if (self.turn_id is None or not isinstance(intent, ReadIntent)
                or not intent.content):
            return
        if self._calls:
            for call in self._calls.values():
                call.reads.append(intent)
                if len(self._calls) != 1:
                    call.ambiguous = True
        else:
            self._pending.append(intent)
            # Without the call events, two hooks cannot safely be partitioned
            # between one compound call and two parallel calls in this turn.
            if len(self._pending) > 1:
                self._orphaned = True

    def response_item(self, item: dict, *, turn_id: str | None = None) -> bool:
        """Consume a raw/rollout Response item. Return study.note_lines changes.

        Only explicit current-turn attribution qualifies. The raw notification's
        turnId should be passed by the caller; rollout items may carry metadata.
        """
        if not isinstance(item, dict) or self.turn_id is None:
            return False
        metadata = item.get("internal_chat_message_metadata_passthrough") or {}
        embedded_turn = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if turn_id is not None and embedded_turn is not None and turn_id != embedded_turn:
            return False
        if (turn_id or embedded_turn) != self.turn_id:
            return False
        kind, call_id = item.get("type"), item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return False
        if kind in {"custom_tool_call", "function_call"}:
            if call_id in self._seen_calls or call_id in self._outputs:
                return False
            self._seen_calls.add(call_id)
            call = _Call(self._pending, self._orphaned)
            self._pending = []
            if self._calls:
                call.ambiguous = True
                for active in self._calls.values():
                    active.ambiguous = True
            self._calls[call_id] = call
            return False
        if kind not in {"custom_tool_call_output", "function_call_output"}:
            return False
        if call_id in self._outputs:
            return False
        self._outputs.add(call_id)
        call = self._calls.pop(call_id, None)
        ambiguous = self._orphaned
        if not self._calls and not self._pending:
            self._orphaned = False
        if call is None or call.ambiguous or ambiguous or len(call.reads) != 1:
            return False
        intent = call.reads[0]
        source = self._source(intent)
        if source is None:
            return False
        rel, text = source
        lines: set[int] = set()
        for block in _text_blocks(item.get("output")):
            lines.update(matched_read_lines(intent, text, block))
        return bool(lines and self.study.note_lines(rel, lines))

    def _source(self, intent: ReadIntent) -> tuple[str, str] | None:
        if not intent.content or intent.path is None:
            return None
        for rel in self.study.files:
            if self.study.container_path(rel) == intent.path:
                path = self.study.extra.get(rel) or (self.study.kb_dir / rel)
                try:
                    return rel, path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    # A missing/unreadable trusted source cannot prove receipt.
                    return None
        return None

    def ingest_rollout(self, data: bytes) -> int:
        """Consume new trusted JSONL bytes, retaining an incomplete final line.

        Return the number of complete JSONL records consumed (including ignored
        records). Malformed complete records are skipped; partial UTF-8 is retained
        until its newline arrives. The caller owns bounded transport/tail reads.
        """
        if not isinstance(data, bytes):
            raise TypeError("rollout data must be bytes")
        if self._discard_line:
            if b"\n" not in data:
                return 0
            data = data.split(b"\n", 1)[1]
            self._discard_line = False
        self._tail += data
        records = self._tail.split(b"\n")
        self._tail = records.pop()
        if len(self._tail) > self.max_record_bytes:
            # Large image/reasoning records are not read receipts. Bound the
            # partial-line buffer and skip through that record's final newline.
            self._tail = b""
            self._discard_line = True
        for line in records:
            if len(line) > self.max_record_bytes:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = event.get("type")
            if kind == "event_msg" and payload.get("type") == "task_started":
                self._rollout_turn = payload.get("turn_id")
            elif kind == "turn_context":
                self._rollout_turn = payload.get("turn_id")
            elif kind == "response_item":
                metadata = payload.get("internal_chat_message_metadata_passthrough")
                embedded = metadata.get("turn_id") if isinstance(metadata, dict) else None
                self.response_item(payload, turn_id=embedded or self._rollout_turn)
        return len(records)


def _text_blocks(output) -> list[str]:
    if isinstance(output, str):
        blocks = [output]
    elif isinstance(output, list):
        blocks = [part["text"] for part in output if isinstance(part, dict)
                  and part.get("type") in {"input_text", "text"}
                  and isinstance(part.get("text"), str)]
    else:
        return []
    # This transport-only block is not printed by the command. Do not match its
    # status lines against coincidentally identical KB lines.
    return [block for block in blocks if not re.fullmatch(
        r"Script (?:completed|running with cell ID [^\n]+)\r?\n"
        r"Wall time [^\n]+\r?\nOutput:\r?\n", block)]
