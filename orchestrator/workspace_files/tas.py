"""CelesteTAS digital inputs: explicit parsing, source cursors and simulator conversion.

No game/config commands are executed. Run this module with --help to prepare a
bounded JSON plan from a selected region of a .tas file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

FORMATS = ('celeste_ai', 'celeste_tas')
TAS_KEYS = frozenset('LRUDJKXCZVGHSQNO')
SIM_KEYS = {'L':'L', 'R':'R', 'U':'U', 'D':'D', 'J':'J', 'K':'K',
            'X':'X,T', 'C':'Y', 'Z':'Z', 'V':'V', 'G':'G'}
MAX_SOURCE_FRAMES = 1_000_000
_METADATA = re.compile(r'^(ChapterTime|FileTime):\s*\d[\d:.]*(?:\(\d+\))?\s*$', re.I)


def check_format(value):
    if value not in FORMATS:
        raise ValueError(f'input_format must be one of {FORMATS}, got {value!r}')
    return value


def expand(inputs, max_frames=MAX_SOURCE_FRAMES):
    """Return per-frame TAS keys. Unsupported commands/analog modes fail explicitly."""
    if not isinstance(inputs, list) or not inputs:
        raise ValueError('TAS inputs must be a nonempty list of frame-count segments')
    frames = []
    for segment in inputs:
        if not isinstance(segment, str):raise ValueError('TAS segment must be text')
        head, _, keys = segment.partition(',')
        if not re.fullmatch(r'\s*\d+\s*', head):
            raise ValueError(f'TAS segment must start with a frame count: {segment!r}')
        count = int(head)
        tokens = [key.strip().upper() for key in keys.split(',') if key.strip()]
        if any(len(key) != 1 or key not in TAS_KEYS for key in tokens):
            raise ValueError(f'Unsupported TAS input {segment!r}; supported digital keys: {"".join(sorted(TAS_KEYS))}. '
                             'F/analog modes, A/M direction channels and P/custom bindings are not implemented.')
        if len(frames) + count > max_frames:raise ValueError(f'TAS input exceeds {max_frames} frames')
        frames.extend([','.join(dict.fromkeys(tokens))] * count)
    if not frames:raise ValueError('TAS input contains no frames')
    return frames


def compress(frames):
    out = []
    for keys in frames:
        if out and out[-1][1] == keys:out[-1][0] += 1
        else:out.append([1, keys])
    return [str(count) + (',' + keys if keys else '') for count, keys in out]


def to_sim(inputs, max_frames=MAX_SOURCE_FRAMES):
    """Convert only supported gameplay signals; never silently discard modal inputs."""
    converted = []
    for frame in expand(inputs, max_frames):
        keys = frame.split(',') if frame else []
        unsupported = set(keys) - SIM_KEYS.keys()
        if unsupported:
            raise ValueError(f'Simulator cannot reproduce TAS keys {sorted(unsupported)}; no prediction was run')
        converted.append(','.join(SIM_KEYS[key] for key in keys))
    return compress(converted)


def load(path, start_line=1, end_line=None):
    """Read an explicitly selected source region. Read/Set/console are NOT executed."""
    path = Path(path)
    if type(start_line) is not int or start_line < 1:raise ValueError('start_line must be >= 1')
    if end_line is not None and (type(end_line) is not int or end_line < start_line):raise ValueError('invalid end_line')
    with path.open('rb') as stream:raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:raise ValueError('TAS source exceeds 8 MiB')
    frames, lines, metadata = [], [], []
    for line_number, text in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
        if line_number < start_line or (end_line is not None and line_number > end_line):continue
        content = text.split('#', 1)[0].strip()
        if not content:continue
        if _METADATA.fullmatch(content):
            metadata.append({'line':line_number, 'text':content});continue
        try:part = expand([content], MAX_SOURCE_FRAMES - len(frames))
        except ValueError as exc:
            raise ValueError(f'{path.name}:{line_number}: {exc}. Select input lines explicitly; commands are not run.') from exc
        frames.extend(part)
        lines.extend((line_number, offset) for offset in range(len(part)))
    if not frames:raise ValueError('Selected TAS region has no input frames')
    return {'frames':frames, 'locations':lines, 'source':str(path),
            'sha256':hashlib.sha256(raw).hexdigest(), 'metadata':metadata,
            'start_line':start_line, 'end_line':end_line}


def window(document, offset=0, count=600):
    """A plan window consumes no game frames; the agent explicitly submits it to act."""
    frames = document['frames']
    if type(offset) is not int or not 0 <= offset < len(frames):raise ValueError('offset outside source frames')
    if type(count) is not int or not 1 <= count <= MAX_SOURCE_FRAMES:raise ValueError('count must be positive and bounded')
    selected = frames[offset:offset + count]
    return {'input_format':'celeste_tas', 'inputs':compress(selected),
            'input_source':{'path':document['source'], 'sha256':document['sha256'], 'offset':offset,
                            'total':len(frames), 'start_line':document['start_line'], 'end_line':document['end_line'],
                            'following_location':list(document['locations'][offset+len(selected)]) if offset+len(selected)<len(frames) else None,
                            'locations':[list(item) for item in document['locations'][offset:offset + count]]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path');parser.add_argument('--start-line', type=int, default=1)
    parser.add_argument('--end-line', type=int);parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--count', type=int, default=600);parser.add_argument('--out', required=True)
    args = parser.parse_args()
    plan = window(load(args.path, args.start_line, args.end_line), args.offset, args.count)
    Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'plan':args.out, 'input_source':{key:value for key,value in plan['input_source'].items() if key != 'locations'}}, ensure_ascii=False))


if __name__ == '__main__':main()
