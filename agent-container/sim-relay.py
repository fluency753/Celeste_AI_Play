"""Run native simulations on a private worker and exchange framed JSON over FIFOs."""
from __future__ import annotations

import json
import hashlib
import base64
from bisect import bisect_right
from contextlib import nullcontext
import multiprocessing as mp
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_REQUEST = 32 * 1024 * 1024
MAX_RESPONSE = 48 * 1024 * 1024
NAME = re.compile(r'[a-f0-9]{32}')
INPUT_KEYS = {'inputs', 'stop_on_death', 'stop_on_room', 'track', 'rows'}
PREVIEW_RERUN = 24      # 精简回复（rows）的一批里，最多挑这么多条补全逐帧轨迹给规划回放


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


def room_tracks(results):
    """每条候选在当前房间里的部分：画到第一次出房或第一次死亡为止（死亡不停时，之后是死亡动画与重生）。"""
    tracks = []
    for result in results:
        frames = result.get('frames', [])
        end = next(((frame, kind) for frame, kind, _ in result.get('events', ())
                    if kind in {'room_exit', 'room', 'death'}), None)
        size = len(frames) if end is None else bisect_right(frames, end[0], key=lambda row: row[0])
        if size and len(frames[0]) >= 3 and len(frames[size - 1]) >= 3:
            stop = result.get('stop') if end is None else 'death' if end[1] == 'death' else 'room'
            tracks.append((frames, size, stop))
    return tracks


def preview_candidates(results, indexes, origin, exit_distance, limit=PREVIEW_RERUN):
    """精简回复的候选只有最后一帧：按结局挑出少数几条补全轨迹回放——往前出房的按出房先后、死亡的按位置散开，
    其余按终点位置散开。origin 是起点（读入状态时 Madeline 的位置）。"""
    def end(i):
        frames = results[i].get('frames') or []
        row = frames[-1] if frames else None
        return (row[1], row[2]) if row and len(row) >= 3 else None

    def away(i):
        e = end(i)
        return e is not None and origin is not None and (e[0] - origin[0]) ** 2 + (e[1] - origin[1]) ** 2 > exit_distance ** 2

    def exit_frame(i):
        r = results[i]
        frames = [frame for frame, kind, _ in r.get('events', ()) if kind in {'room', 'room_exit'}]
        if frames:
            return min(frames)
        return (r.get('frames') or [[0]])[-1][0] if r.get('stop') == 'room' else None

    chosen = []
    exits = sorted((i for i in indexes if exit_frame(i) is not None and away(i)), key=exit_frame)
    chosen += exits[:max(1, limit // 3)]
    cells = set()
    for kind in ('death', None):
        for i in indexes:
            if len(chosen) >= (limit * 2 // 3 if kind else limit):
                break
            if i in chosen or (kind and results[i].get('stop') != kind) or (e := end(i)) is None:
                continue
            cell = (kind, int(e[0] // 16), int(e[1] // 16))
            if cell not in cells:
                cells.add(cell)
                chosen.append(i)
    return chosen[:limit]


def pack_preview(tracks, needed, available, frame, codec, exit_distance):
    if available < len(codec._LOG_MAGIC) + codec._LOG_HEAD.size:
        return b''

    def valid_exit(track):
        frames, size, stop = track
        first, last = frames[0], frames[size - 1]
        return stop == 'room' and (last[1] - first[1]) ** 2 + (last[2] - first[2]) ** 2 > exit_distance ** 2

    indexes = range(len(tracks))
    if needed > available:
        limit = available - len(codec._LOG_MAGIC)
        best = min((i for i, track in enumerate(tracks) if valid_exit(track)
                    and codec._LOG_HEAD.size + 4 * (track[1] - 1) <= limit),
                   key=lambda i: tracks[i][1], default=None)
        # The existing renderer chooses routes when there is enough memory. If
        # the transport is full, spread its slots across the response.
        count = max(1, len(tracks) * available // needed)
        indexes = [(i * (len(tracks) - 1)) // max(1, count - 1) for i in range(count)]
        if best is not None:
            indexes = [best, *(i for i in indexes if i != best)]
    selected = []
    remaining = available - len(codec._LOG_MAGIC)
    for i in indexes:
        frames, size, stop = tracks[i]
        if codec._LOG_HEAD.size + 4 * (size - 1) <= remaining:
            record = codec._pack_record([(row[1], row[2]) for row in frames[:size] if len(row) >= 3], stop, frame)
            selected.append((i, record))
            remaining -= len(record)
    return codec._LOG_MAGIC + b''.join(record for _, record in sorted(selected)) if selected else b''


def snapshot_start(line, state, info):
    """读入的起始状态是哪一帧：回放只画从游戏当前这一帧出发的推演，主控按（章节、房间、帧号、快照摘要）核对。
    检查点按它所依据的快照核对（与主控写快照时同样的紧凑写法），帧号与房间也取快照的；
    origin 是推演真正的起点（检查点那一帧、所在的房间与 Madeline 的位置），回放从那里开始画。"""
    checkpoint = isinstance(state.get('base'), dict)
    base = state['base'] if checkpoint else state
    level = (base.get('level') or {}) if isinstance(base, dict) else {}
    if checkpoint:
        snapshot_bytes = json.dumps(base, ensure_ascii=False, separators=(',', ':')).encode()
        frame = (info.get('checkpoint') or {}).get('baseFrame', base.get('frame'))
        room = level.get('room')
    else:
        snapshot_bytes = line[len(b'{"snapshot":'):-2] if line.startswith(b'{"snapshot":') else b''
        frame, room = info.get('frame'), info.get('room')
    player = info.get('player') or {}
    return {'frame': frame, 'room': room, 'sid': (level.get('session') or {}).get('area'),
            'digest': hashlib.sha256(snapshot_bytes).hexdigest() if snapshot_bytes else None,
            'origin': info.get('room'), 'origin_frame': info.get('frame'),
            'origin_xy': (player['x'], player['y']) if 'x' in player and 'y' in player else None}


def simulation_event(data, request, start, digest, audit, budget, decoding, byte_limit, codec, exit_distance, rerun=None):
    with (decoding if len(data) > 2 * 1024 * 1024 else nullcontext()):
        response = json.loads(data)
        results = response.get('results', [response] if 'inputs' in request else [])
        completed = [r for r in results if r.get('ok') is True]
        if not completed:
            return None
        event = {'simulated': len(completed)}
        if digest and start and start['digest'] == digest:
            event['start'] = {key: start[key] for key in ('frame', 'room', 'sid', 'digest', 'origin') if key in start}
            try:
                now = time.monotonic()
                with audit:
                    if now >= budget[0]:
                        budget[:] = [now + .5, byte_limit]
                    remaining = int(budget[1])
                if remaining < len(codec._LOG_MAGIC) + codec._LOG_HEAD.size:
                    return event
                full = [r for r in completed if 'count' not in r]
                partial = [i for i, r in enumerate(results) if r.get('ok') is True and 'count' in r]
                if partial and rerun is not None:
                    # 精简回复（rows）没有逐帧轨迹：挑几条按原设置补推一次完整轨迹，只用来回放，不回给智能体
                    candidates = request['batch'] if 'batch' in request else [request]
                    picks = preview_candidates(results, partial, start.get('origin_xy'), exit_distance)
                    if picks:
                        again = [{k: v for k, v in candidates[i].items() if k not in {'rows', 'track'}} for i in picks]
                        full += [r for r in rerun(again) if r.get('ok') is True and 'count' not in r]
                tracks = room_tracks(full)
                if tracks:
                    needed = len(codec._LOG_MAGIC) + sum(codec._LOG_HEAD.size + 4 * (size - 1) for _, size, _ in tracks)
                    with audit:
                        available = min(int(budget[1]), needed)
                        budget[1] -= available
                    packed = pack_preview(tracks, needed, available, start.get('origin_frame', start['frame']), codec,
                                          exit_distance)
                    if packed:
                        event['replay'] = base64.b64encode(packed).decode('ascii')
            except (ArithmeticError, ValueError, TypeError, KeyError, struct.error) as exc:
                print(f'simulation replay encoding failed: {exc}', file=sys.stderr, flush=True)
        return event


def simulate(p, reader, writer, audit, budget, decoding, byte_limit, codec, exit_distance):
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
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8') as f:
                json.dump(state, f, separators=(',', ':'))
                f.flush()
                data = reply(p, json.dumps({'state': f.name}).encode() + b'\n')
            start = snapshot_start(line, state, json.loads(data).get('info') or {})
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
                    event = simulation_event(data, request, start, digest, audit, budget, decoding,
                                             byte_limit, codec, exit_distance,
                                             rerun=lambda batch: json.loads(reply(p, json.dumps({'batch': batch}).encode()
                                                                                  + b'\n')).get('results', []))
                    if event:
                        with audit:
                            print(json.dumps(event, separators=(',', ':')), flush=True)
                except Exception as exc:  # do not replace a completed simulation reply
                    print(f'simulation telemetry failed: {exc}', file=sys.stderr, flush=True)
        del data


def lane_timeout(*_):
    raise TimeoutError('主控模拟通道超时')


def worker(path, audit, budget, decoding, byte_limit, exit_distance):
    import replay_codec
    signal.signal(signal.SIGALRM, lane_timeout)
    lane = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink('ready', dir_fd=lane)
        with pipe(lane, 'request', os.O_RDONLY) as reader, pipe(lane, 'response', os.O_WRONLY) as writer:
            mark(lane, 'attached')
            with subprocess.Popen(['/sim/celeste-sim', 'serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL) as p:
                try:
                    simulate(p, reader, writer, audit, budget, decoding, byte_limit, replay_codec, exit_distance)
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


def main(endpoint, byte_limit, exit_distance):
    if not NAME.fullmatch(endpoint):
        raise ValueError('invalid simulation endpoint')
    root = Path('/relay') / endpoint
    root.mkdir()
    audit = mp.Lock()
    budget = mp.Array('d', [0, 0], lock=False)  # 窗口截止时间、剩余编码字节数
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
                    child = mp.Process(target=worker, args=(path, audit, budget, decoding, byte_limit, exit_distance), daemon=True)
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
    main(sys.argv[1], int(sys.argv[2]), float(sys.argv[3]))
