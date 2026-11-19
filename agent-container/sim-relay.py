"""Run native simulations on a private worker and exchange framed JSON over FIFOs."""
from __future__ import annotations

import json
import hashlib
from bisect import bisect_right
from contextlib import nullcontext
import multiprocessing as mp
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_REQUEST = 32 * 1024 * 1024
MAX_RESPONSE = 48 * 1024 * 1024
PREVIEW_TRACKS = 48
PREVIEW_TRACKS_PER_REPLY = 24
PREVIEW_POINTS = 24_000
PREVIEW_POINTS_PER_TRACK = 4096
NAME = re.compile(r'[a-f0-9]{32}')
INPUT_KEYS = {'inputs', 'stop_on_death', 'stop_on_room', 'track'}


def reply(p, request):
    p.stdin.write(request)
    p.stdin.flush()
    line = p.stdout.readline(MAX_RESPONSE + 1)
    if len(line) > MAX_RESPONSE or not line.endswith(b'\n'):
        raise RuntimeError('模拟器没有返回完整的有界响应')
    return line


def prediction(request):
    def candidate(value):
        return isinstance(value, dict) and 'inputs' in value and value.keys() <= INPUT_KEYS

    if 'batch' in request:
        batch = request['batch']
        return request.keys() == {'batch'} and isinstance(batch, list) and all(candidate(part) for part in batch)
    return candidate(request)


def pipe(lane, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=lane)
    if not stat.S_ISFIFO(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError('模拟通道必须使用管道')
    os.set_blocking(fd, True)
    return os.fdopen(fd, 'rb' if flags == os.O_RDONLY else 'wb')


def preview_tracks(results, track_limit, point_limit, exit_distance):
    if track_limit <= 0 or point_limit <= 0:
        return []
    eligible = []
    limit = min(point_limit, PREVIEW_POINTS_PER_TRACK)
    for result in results:
        frames = result.get('frames', [])
        exit_frame = next((frame for frame, kind, _ in result.get('events', ()) if kind in {'room_exit', 'room'}), None)
        size = len(frames) if exit_frame is None else bisect_right(frames, exit_frame, key=lambda row: row[0])
        if 0 < size <= limit and len(frames[0]) >= 3 and len(frames[size - 1]) >= 3:
            eligible.append((frames, size, 'room' if exit_frame is not None else result.get('stop')))
    if not eligible:
        return []

    def valid_exit(track):
        frames, size, stop = track
        first, last = frames[0], frames[size - 1]
        return stop == 'room' and (last[1] - first[1]) ** 2 + (last[2] - first[2]) ** 2 > exit_distance ** 2

    exits = [i for i, track in enumerate(eligible) if valid_exit(track)]
    best = min(exits, key=lambda i: eligible[i][1], default=None)
    count = min(track_limit, len(eligible))
    if best is not None and count == 1:
        indexes = [best]
    else:
        ordinary = [i for i in range(len(eligible)) if i != best]
        slots = count - (best is not None)
        indexes = [ordinary[(j * (len(ordinary) - 1)) // max(1, slots - 1)] for j in range(slots)]
        if best is not None:
            indexes.insert(0, best)
    selected = []
    for i in indexes:
        frames, size, stop = eligible[i]
        if size <= point_limit:
            selected.append((i, {'stop': stop, 'frames': [row[:3] for row in frames[:size]]}))
            point_limit -= size
    return [track for _, track in sorted(selected)]


def simulation_event(data, request, start, digest, audit, budget, decoding, exit_distance):
    with (decoding if len(data) > 2 * 1024 * 1024 else nullcontext()):
        response = json.loads(data)
        results = response.get('results', [response] if 'inputs' in request else [])
        completed = [r for r in results if r.get('ok') is True]
        if not completed:
            return None
        event = {'simulated': len(completed)}
        if digest and start and start['digest'] == digest:
            event['start'] = start
            now = time.monotonic()
            with audit:
                if now >= budget[0]:
                    budget[:] = [now + .5, PREVIEW_TRACKS, PREVIEW_POINTS]
                traces = preview_tracks(completed, min(int(budget[1]), PREVIEW_TRACKS_PER_REPLY),
                                        int(budget[2]), exit_distance)
                if traces:
                    budget[1] -= len(traces)
                    budget[2] -= sum(len(track['frames']) for track in traces)
                    event['traces'] = traces
        return event


def simulate(p, reader, writer, audit, budget, decoding, exit_distance):
    start = None
    digest = None
    next_check = 0.0
    while True:
        signal.alarm(120)
        line = reader.readline(MAX_REQUEST + 1)
        if not line:
            break
        signal.alarm(85)
        is_prediction = False
        if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
            break
        request = json.loads(line)
        if not isinstance(request, dict):
            raise ValueError('无效模拟请求')
        if request.keys() == {'snapshot'} and isinstance(request['snapshot'], dict):
            state = request['snapshot']
            base = state.get('base', state)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
                json.dump(state, f, separators=(',', ':'))
                f.flush()
                data = reply(p, json.dumps({'state': f.name}).encode() + b'\n')
            info = json.loads(data).get('info') or {}
            level = (base.get('level') or {}) if isinstance(base, dict) else {}
            snapshot_bytes = line[len(b'{"snapshot":'):-2] if line.startswith(b'{"snapshot":') else b''
            start = {'frame': info.get('frame'), 'room': info.get('room'), 'sid': (level.get('session') or {}).get('area'),
                     'digest': hashlib.sha256(snapshot_bytes).hexdigest() if snapshot_bytes else None}
            next_check = 0.0
        elif (is_prediction := prediction(request)) or request == {'info': True}:
            data = reply(p, line)
        else:
            data = b'{"ok":false,"error":"unsupported simulation request"}\n'
        try:
            writer.write(data)
            writer.flush()
        finally:
            if is_prediction:
                try:
                    now = time.monotonic()
                    if now >= next_check:
                        next_check = now + .5
                        try:
                            digest = json.loads(Path('/control/relay_snapshot.json').read_text(encoding='utf-8'))
                        except (OSError, ValueError):
                            digest = None
                    event = simulation_event(data, request, start, digest, audit, budget, decoding, exit_distance)
                    if event:
                        with audit:
                            print(json.dumps(event, separators=(',', ':')), flush=True)
                except Exception as exc:  # do not replace a completed simulation reply
                    print(f'simulation telemetry failed: {exc}', file=sys.stderr, flush=True)
        del data


def lane_timeout(*_):
    raise TimeoutError('主控模拟通道超时')


def worker(path, audit, budget, decoding, exit_distance):
    signal.signal(signal.SIGALRM, lane_timeout)
    lane = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink('ready', dir_fd=lane)
        with pipe(lane, 'request', os.O_RDONLY) as reader, pipe(lane, 'response', os.O_WRONLY) as writer:
            mark(lane, 'attached')
            with subprocess.Popen(['/sim/celeste-sim', 'serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as p:
                try:
                    simulate(p, reader, writer, audit, budget, decoding, exit_distance)
                except (OSError, ValueError, RuntimeError) as exc:
                    try:
                        signal.alarm(2)
                        writer.write(json.dumps({'ok': False, 'error': str(exc)[:256]}).encode() + b'\n')
                        writer.flush()
                    except OSError:
                        pass
                finally:
                    if p.poll() is None:
                        p.kill()
    finally:
        signal.alarm(0)
        os.close(lane)
        shutil.rmtree(path, ignore_errors=True)


def mark(directory, name):
    os.close(os.open(name, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600, dir_fd=directory))


def reject(path):
    try:
        lane = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.unlink('ready', dir_fd=lane)
            mark(lane, 'busy')
        finally:
            os.close(lane)
    except OSError:
        pass


def main(endpoint, exit_distance):
    if not NAME.fullmatch(endpoint):
        raise ValueError('invalid simulation endpoint')
    root = Path('/relay') / endpoint
    root.mkdir()
    audit = mp.Lock()
    budget = mp.Array('d', [0, 0, 0], lock=False)  # 窗口截止时间、剩余路线和剩余轨迹点
    decoding = mp.Semaphore(3)
    workers = {}
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        print('{"ready":true}', flush=True)
        while True:
            for path, child in list(workers.items()):
                if not child.is_alive():
                    child.join()
                    del workers[path]
            for path in root.iterdir():
                if NAME.fullmatch(path.name) and path not in workers and (path / 'ready').is_file():
                    if len(workers) >= 24:
                        reject(path)
                        continue
                    child = mp.Process(target=worker, args=(path, audit, budget, decoding, exit_distance), daemon=True)
                    child.start()
                    workers[path] = child
            time.sleep(.002)
    finally:
        for child in workers.values():
            child.terminate()
        for child in workers.values():
            child.join(timeout=.2)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    main(sys.argv[1], float(sys.argv[2]))
