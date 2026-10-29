"""Conservative native read intents and evidence from model-visible plain text.

This module neither executes commands nor trusts tool-reported line counts. Unknown
shell expressions must keep their normal Bash policy rather than being guessed at.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import posixpath
import re
import shlex


@dataclass(frozen=True)
class ReadIntent:
    path: str | None
    start: int = 1
    end: int | None = None
    tail: int | None = None
    content: bool = True


def _path(value: str, cwd: str, *, dashed: bool = False) -> str | None:
    if not value or value == "-" or (value.startswith("-") and not dashed):
        return None
    if any(char in value for char in "*?[]{}~"):
        return None
    return posixpath.normpath(posixpath.join(cwd, value))


def parse_read_command(command: str, cwd: str = "/workspace") -> ReadIntent | None:
    """Recognize one plain read command, preserving its explicit source range.

Only stock command names and their /bin or /usr/bin paths qualify. This is
deliberately narrower than a shell parser: substitution, globs, compound commands,
transformed cat output and arbitrary sed programs stay normal Bash operations.
"""
    if not isinstance(command, str) or not isinstance(cwd, str):
        return None
    if not cwd.startswith("/") or any(c in cwd for c in "\x00\r\n$`"):
        return None
    if any(c in command for c in "\x00\r\n`"):
        return None
    # Only sed's single-quoted, literal EOF address may contain a dollar sign.
    if "$" in command and not re.fullmatch(
            r"\s*(?:sed|/(?:usr/)?bin/sed)\s+-n\s+'[1-9][0-9]*,\$p'\s+[^$]+", command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(re.fullmatch(r"[;&|<>()]+", t) for t in tokens):
        return None
    executable, *args = tokens
    name = posixpath.basename(executable)
    if executable not in {name, f"/bin/{name}", f"/usr/bin/{name}"}:
        return None
    if name == "pwd":
        return ReadIntent(None, content=False) if args in ([], ["-L"], ["-P"]) else None
    if name == "ls":
        while args and re.fullmatch(r"-[alh1d]+", args[0]):
            args.pop(0)
        dashed = bool(args and args[0] == "--")
        if dashed:
            args.pop(0)
        if len(args) > 1:
            return None
        path = _path(args[0] if args else ".", cwd, dashed=dashed)
        return ReadIntent(path, content=False) if path is not None else None
    start, end, tail = 1, None, None
    if name == "cat":
        pass
    elif name == "sed":
        if len(args) < 3 or args[0] != "-n":
            return None
        match = re.fullmatch(r"([1-9][0-9]{0,17})(?:,([1-9][0-9]{0,17}|\$))?p", args[1])
        if not match:
            return None
        start = int(match[1])
        end = None if match[2] == "$" else int(match[2] or match[1])
        if end is not None and end < start:
            return None
        args = args[2:]
    elif name in {"head", "tail"}:
        number = "10"
        if args and args[0] == "-n":
            if len(args) < 2:
                return None
            number, args = args[1], args[2:]
        elif args and args[0].startswith("-n"):
            number, args = args[0][2:], args[1:]
        elif args and args[0].startswith("--lines="):
            number, args = args[0][8:], args[1:]
        if not re.fullmatch(r"\+?[0-9]{1,18}", number):
            return None
        if name == "head":
            if number.startswith("+"):
                return None
            end = int(number)
        elif number.startswith("+"):
            start = int(number)
            if start == 0:
                return None
        else:
            tail = int(number)
    else:
        return None
    dashed = bool(args and args[0] == "--")
    if dashed:
        args = args[1:]
    if len(args) != 1:
        return None
    path = _path(args[0], cwd, dashed=dashed)
    return ReadIntent(path, start, end, tail) if path is not None else None


def matched_read_lines(intent: ReadIntent, source_text: str, visible_text: str,
                       *, successful: bool = True) -> set[int]:
    """Return 1-based source rows evidenced by a single visible content block.

The caller supplies the trusted source associated with ``intent.path``, excludes
transport-only headers, and calls separately for separate text blocks. No prefix
stripping, JSON metadata, reported totals, or inferred omitted rows are accepted.
"""
    if not successful or not intent.content or intent.path is None:
        return set()
    source_parts = source_text.splitlines(keepends=True)
    source = [line.removesuffix("\n").removesuffix("\r") for line in source_parts]
    first = max(1, intent.start)
    last = min(len(source), intent.end if intent.end is not None else len(source))
    if intent.tail is not None:
        first = max(first, len(source) - intent.tail + 1)
    if first > last:
        return set()
    observed = visible_text.splitlines(keepends=True)
    complete = [line.removesuffix("\n").removesuffix("\r") for line in observed
                if line.endswith("\n")]
    # A truncated middle row may equal the real EOF row (including duplicates).
    # Without its delimiter the final fragment is evidence only when the entire
    # requested range is present verbatim. Paging the EOF row alone also works.
    if (observed and not observed[-1].endswith("\n") and last == len(source)
            and source_parts and not source_parts[-1].endswith("\n")
            and visible_text.replace('\r\n', '\n') ==
                ''.join(source_parts[first - 1:last]).replace('\r\n', '\n')):
        complete.append(observed[-1])
    matched: set[int] = set()
    candidates = source[first - 1:last]
    for block in SequenceMatcher(None, candidates, complete, autojunk=False).get_matching_blocks():
        if block.size and any(line.strip() for line in candidates[block.a:block.a + block.size]):
            matched.update(range(first + block.a, first + block.a + block.size))
    return matched
