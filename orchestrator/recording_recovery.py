"""Restart a stopped capture without overwriting its logical recording.

OBS writes unique attempts. Immutable sources and a replayable publication
journal maintain the cumulative media; a multi-file publication is not atomic.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import csv
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import uuid

from .purecut import _wav_header


class RecoveryError(RuntimeError):
    pass


MEDIA = ('video.mkv', 'audio.wav', 'audio_play.wav', 'frames.csv', 'segments.csv')
FIELDS = ('index', 'game_frame', 'chapter_ticks', 'obs_ts_ns')
SEG_FIELDS = ('segment', 'first_index', 'start_ts_ns', 'measured_ms', 'offset_ms', 'method')
BLOCK = 800 * 2 * 4
MAX_PCM_BYTES = 0xffffffff - 36  # RIFF header, not RF64.


def _root(saved):
    try:
        root = Path(saved['options']['dir']).resolve()
        ident = saved['id']
        if not isinstance(ident, str) or not ident:
            raise ValueError('missing session identity')
    except (KeyError, TypeError, ValueError) as exc:
        raise RecoveryError('Invalid recording identity') from exc
    return root


def _write(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise RecoveryError('Invalid recovery path')
    path = root / '.recovery' / relative
    # Do not follow junctions/symlinks back into a personal installation.
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise RecoveryError('Recovery paths cannot contain links')
        if current.parent == current:
            raise RecoveryError('Recovery path escapes its recording')
        current = current.parent
    resolved = path.resolve()
    if (root / '.recovery').resolve() not in resolved.parents:
        raise RecoveryError('Recovery path escapes its recording')
    return resolved


def _load(saved):
    root = _root(saved)
    path = root / '.recovery' / 'ledger.json'
    if not path.exists():
        directory = root / '.recovery'
        if directory.exists() and any(child.name != 'recovery.lock' and child.name != 'base'
                                      and not child.name.startswith('base-work-') for child in directory.iterdir()):
            raise RecoveryError('Recovery ledger is missing; refusing to fall back to the cumulative media directory')
        return None
    try:
        ledger = json.loads(path.read_text(encoding='utf-8'))
        if ledger['version'] != 1 or ledger['session_id'] != saved['id']:
            raise ValueError('recording identity mismatch')
        if ledger['phase'] not in {'prepared', 'publishing', 'committed'}:
            raise ValueError('invalid recovery phase')
        if not isinstance(ledger['chunks'], list) or len(set(ledger['chunks'])) != len(ledger['chunks']):
            raise ValueError('duplicate capture sources')
        for relative in [*ledger['chunks'], ledger['active_capture']]:
            if not isinstance(relative, str) or not re.fullmatch(r'base|attempt-[0-9a-f]{32}', relative):
                raise ValueError('invalid capture source')
            _inside(root, relative)
        if ledger['active_capture'] not in ledger['chunks']:
            raise ValueError('unregistered active capture')
        if ledger.get('transaction'):
            _validate_transaction(root, saved, ledger['transaction'])
        if ledger['phase'] == 'publishing' and not ledger.get('transaction'):
            raise ValueError('missing publication journal')
        for name in ('published', 'prefix_summary'):
            summary = ledger.get(name)
            if summary is not None and (not isinstance(summary, dict) or type(summary.get('frames')) is not int
                    or summary['frames'] < 0 or type(summary.get('replays')) is not int
                    or not 0 <= summary['replays'] <= summary['frames']):
                raise ValueError('invalid prefix accounting')
        if ledger['phase'] == 'committed' and not ledger.get('published'):
            raise ValueError('missing committed accounting')
        if ledger.get('published_files'):
            _validate_transaction(root, saved, {'generation': ledger['published']['generation'],
                                  'summary': ledger['published'], 'files': ledger['published_files']})
        elif (ledger.get('published') or {}).get('frames', 0):
            raise ValueError('missing committed media hashes')
        return ledger
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecoveryError('Recovery ledger is damaged or belongs to another recording; no files were overwritten') from exc


def _validate_transaction(root, saved, transaction):
    if not isinstance(transaction, dict) or not isinstance(transaction.get('files'), dict):
        raise RecoveryError('Invalid recovery publication journal')
    required = set(MEDIA) if saved['options']['audio_source'] == 'game_nrt' else set(MEDIA) - {'audio_play.wav'}
    if set(transaction['files']) != required:
        raise RecoveryError('Publication journal contains missing, unknown or unsafe file names')
    if not re.fullmatch(r'generation-[0-9a-f]{32}', transaction.get('generation', '')):
        raise RecoveryError('Invalid recovery generation')
    _inside(root, transaction['generation'])
    for digest in transaction['files'].values():
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise RecoveryError('Invalid publication digest')
    summary = transaction.get('summary')
    if (not isinstance(summary, dict) or type(summary.get('frames')) is not int or summary['frames'] <= 0
            or type(summary.get('replays')) is not int or not 0 <= summary['replays'] <= summary['frames']):
        raise RecoveryError('Invalid cumulative frame counts')


def capture_directory(saved):
    ledger = _load(saved)
    return _inside(_root(saved), ledger['active_capture']) if ledger else _root(saved)


def prefix_counts(saved):
    ledger = _load(saved)
    published = (ledger or {}).get('prefix_summary') or (ledger or {}).get('published') or {}
    return {key: int(published.get(key, 0)) for key in ('frames', 'replays')}


@contextmanager
def _lock(root):
    directory = root / '.recovery'
    if directory.is_symlink() or (hasattr(directory, 'is_junction') and directory.is_junction()):
        raise RecoveryError('Recovery directory cannot be a link')
    directory.mkdir(parents=True, exist_ok=True)
    with _inside(root, 'recovery.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RecoveryError('Another recovery operation owns this recording') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def _closed_file(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise RecoveryError('Capture sources cannot be links')
    if os.name != 'nt':
        yield
        return
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise RecoveryError(f'Capture file is still being written or cannot be read: {path.name}')
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


def _probe(ffmpeg, video):
    program = Path(ffmpeg)
    probe = program.with_name('ffprobe.exe' if program.suffix.lower() == '.exe' else 'ffprobe')
    probe = str(probe) if probe.is_file() else shutil.which(str(probe))
    if not probe:
        raise RecoveryError('Recording recovery requires ffprobe beside ffmpeg or on PATH')
    try:
        result = subprocess.run([probe, '-v', 'error', '-count_frames', '-select_streams', 'v:0',
            '-show_entries', 'stream=codec_name,width,height,pix_fmt,r_frame_rate,nb_read_frames,profile,level',
            '-of', 'json', str(video)], capture_output=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f'Video prefix probe failed: {video}') from exc
    try:
        stream = json.loads(result.stdout)['streams'][0]
        count = int(stream.pop('nb_read_frames'))
        if result.returncode or Fraction(stream['r_frame_rate']) != 60:
            raise ValueError('not a decodable 60 fps video')
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RecoveryError(f'Cannot determine a valid video prefix: {video}') from exc
    return count, stream


def _run(ffmpeg, args, log):
    with log.open('wb') as stream:
        try:
            result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'warning', '-y', *args],
                                    stdout=stream, stderr=subprocess.STDOUT, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RecoveryError(f'Media recovery failed; see {log}') from exc
    if result.returncode:
        raise RecoveryError(f'Media recovery failed (ffmpeg {result.returncode}); see {log}')


def _wave(path):
    with path.open('rb') as stream:
        head = stream.read(12)
        if head[:4] != b'RIFF' or head[8:] != b'WAVE':
            raise RecoveryError(f'Unsupported WAV: {path}')
        fmt = None
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise RecoveryError(f'Missing WAV data: {path}')
            name, size = header[:4], struct.unpack('<I', header[4:])[0]
            if name == b'fmt ':
                fmt = stream.read(size)
                if size & 1: stream.read(1)
            elif name == b'data':
                if fmt is None or len(fmt) < 16 or struct.unpack('<HHIIHH', fmt[:16]) != (3, 2, 48000, 384000, 8, 32):
                    raise RecoveryError('Recovery requires 48 kHz stereo float PCM')
                available = path.stat().st_size - stream.tell()
                if available > MAX_PCM_BYTES:
                    raise RecoveryError('PCM exceeds the supported RIFF WAV size limit; source files were preserved')
                return stream.tell(), min(size, available) if size else available
            else:
                stream.seek(size + (size & 1), 1)


def _copy_pcm(source, target, count):
    if count > MAX_PCM_BYTES:
        raise RecoveryError('PCM prefix exceeds the supported RIFF WAV size limit')
    offset, available = _wave(source)
    if available < count:
        raise RecoveryError('PCM prefix is shorter than the validated frame count')
    with source.open('rb') as src, target.open('wb') as dst:
        dst.write(_wav_header(count)); src.seek(offset)
        while count:
            block = src.read(min(count, 1024 * 1024))
            if not block: raise RecoveryError('PCM changed while recovering')
            dst.write(block); count -= len(block)


def _rows(path):
    with path.open(newline='', encoding='utf-8') as stream:
        if tuple(next(csv.reader([stream.readline()]), ())) != FIELDS:
            raise RecoveryError('Invalid frame CSV schema')
        expected = 0
        for line in stream:
            # fprintf commits complete records with a newline. A truncated
            # timestamp can still parse as an integer, so parsing is not enough.
            if not line.endswith('\n'):
                return
            try:
                row = next(csv.reader([line]))
                values = tuple(int(value) for value in row)
                if len(values) != 4 or values[2] < -2:
                    raise ValueError('invalid fields')
            except (ValueError, KeyError, TypeError, csv.Error):
                if any(later.strip() for later in stream):
                    raise RecoveryError('Frame CSV contains damaged interior rows')
                return
            if values[0] != expected:
                raise RecoveryError('Frame CSV index is not consecutive')
            expected += 1
            yield values


def _normalize(saved, relative):
    root = _root(saved)
    source = _inside(root, relative)
    output = _inside(root, 'normalized/' + relative)
    files = [name for name in MEDIA if (source / name).is_file()]
    for name in files:
        _inside(root, relative + '/' + name)
    with ExitStack() as locks:
        for name in files:
            locks.enter_context(_closed_file(source / name))
        hashes = {name: _hash(source / name) for name in files}
        report_path = output / 'report.json'
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if report['source_hashes'] != hashes:
                raise RecoveryError('An immutable capture source changed after validation')
            for name, digest in report['normalized_hashes'].items():
                if not (output / name).is_file() or _hash(output / name) != digest:
                    raise RecoveryError('Validated recovery prefix is damaged')
            return output, report
        if 'frames.csv' not in files:
            if files:
                raise RecoveryError('Capture has media but no frame ledger')
            return None, {'frames': 0, 'replays': 0, 'source': relative, 'source_hashes': hashes}
        count_csv = sum(1 for _ in _rows(source / 'frames.csv'))
        if count_csv == 0:
            return None, {'frames': 0, 'replays': 0, 'source': relative, 'source_hashes': hashes}
        needed = ('video.mkv', 'audio.wav', 'audio_play.wav') if saved['options']['audio_source'] == 'game_nrt' else ('video.mkv', 'audio.wav')
        if any(name not in files for name in needed):
            raise RecoveryError('Capture is missing required video or PCM files')
        if (source / 'video.mkv').stat().st_size == 0:
            # A killed encoder may never have flushed its first packet. There
            # is provably no complete video here, but earlier chunks can still
            # be recovered. Keep every source byte and report the lost tail.
            return None, {'frames': 0, 'replays': 0, 'source': relative, 'source_hashes': hashes,
                          'available_video_frames': 0, 'available_csv_rows': count_csv,
                          'unused_csv_rows': count_csv, 'reason': 'encoder produced no video packets',
                          'source_sizes': {name: (source / name).stat().st_size for name in files}}
        ffmpeg = saved['options']['ffmpeg']
        video_count, profile = _probe(ffmpeg, source / 'video.mkv')
        full_bytes = _wave(source / 'audio.wav')[1]
        play_bytes = _wave(source / 'audio_play.wav')[1] if 'audio_play.wav' in needed else None
        limit = min(count_csv, video_count, full_bytes // BLOCK)
        count, play = 0, 0
        for row in _rows(source / 'frames.csv'):
            if count >= limit or (row[2] >= 0 and play_bytes is not None and play >= play_bytes // BLOCK):
                break
            count += 1
            play += row[2] >= 0
        if not count:
            raise RecoveryError('No complete audiovisual frame can be recovered')
        stage = _inside(root, 'normalize-work-' + uuid.uuid4().hex)
        stage.mkdir()
        _run(ffmpeg, ['-i', str(source / 'video.mkv'), '-map', '0:v:0', '-an', '-c:v', 'copy',
                     '-frames:v', str(count), str(stage / 'video.mkv')], stage / 'ffmpeg.log')
        verified, checked_profile = _probe(ffmpeg, stage / 'video.mkv')
        if verified != count or checked_profile != profile:
            raise RecoveryError('Recovered video prefix could not be verified')
        _copy_pcm(source / 'audio.wav', stage / 'audio.wav', count * BLOCK)
        if play_bytes is not None:
            _copy_pcm(source / 'audio_play.wav', stage / 'audio_play.wav', play * BLOCK)
        with (stage / 'frames.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream); writer.writerow(FIELDS)
            for i, row in enumerate(_rows(source / 'frames.csv')):
                if i == count: break
                writer.writerow(row)
        with (stage / 'segments.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=SEG_FIELDS); writer.writeheader()
            if 'segments.csv' in files:
                with (source / 'segments.csv').open(newline='', encoding='utf-8') as src:
                    for row in csv.DictReader(src):
                        try:
                            if int(row['first_index']) >= count: break
                            writer.writerow({name: row[name] for name in SEG_FIELDS})
                        except (KeyError, TypeError, ValueError):
                            raise RecoveryError('Invalid segment CSV')
        csv_path = source / 'frames.csv'
        with csv_path.open(encoding='utf-8') as csv_stream:
            nonempty_lines = sum(1 for line in csv_stream if line.strip())
        with csv_path.open('rb') as csv_stream:
            csv_stream.seek(max(0, csv_path.stat().st_size - 4096))
            tail = csv_stream.read().rsplit(b'\n', 1)[-1]
        report = {'frames': count, 'replays': count - play, 'source': relative, 'profile': profile,
                  'source_hashes': hashes, 'available_video_frames': video_count, 'available_csv_rows': count_csv,
                  'unused_csv_rows': count_csv - count,
                  'unusable_csv_tail_rows': max(0, nonempty_lines - 1 - count_csv),
                  'unusable_csv_tail_bytes': len(tail),
                  'unused_video_frames': video_count - count, 'unused_full_pcm_bytes': full_bytes - count * BLOCK,
                  'unused_play_pcm_bytes': None if play_bytes is None else play_bytes - play * BLOCK,
                  'normalized_hashes': {name: _hash(stage / name) for name in MEDIA if (stage / name).exists()}}
        _write(stage / 'report.json', report)
        output.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage, output)
        return output, report


def _publish(root, ledger, transaction, saved):
    _validate_transaction(root, saved, transaction)
    generation = _inside(root, transaction['generation'])
    for name, digest in transaction['files'].items():
        target = root / name
        if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
            raise RecoveryError('Cumulative media target cannot be a link')
        if target.exists() and _hash(target) == digest:
            continue
        source = _inside(root, transaction['generation'] + '/' + name)
        if not source.is_file() or _hash(source) != digest:
            raise RecoveryError('Publication candidate is damaged; immutable capture sources remain available')
        temporary = root / (name + '.recovery-' + uuid.uuid4().hex + '.tmp')
        with source.open('rb') as src, temporary.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, target)
    # Completed movies from an older generation must not win finished_movie().
    for name in ('gameplay.mp4', 'gameplay_pure.mp4', 'gameplay.mkv', 'gameplay_pure.mkv', 'recording.json'):
        target = root / name
        if target.exists():
            archive = generation / ('previous-' + name)
            if not archive.exists(): shutil.copy2(target, archive)
            target.unlink()
    ledger['published'] = transaction['summary']
    ledger['published_files'] = transaction['files']
    ledger['phase'] = 'committed'
    ledger['transaction'] = None
    _write(root / '.recovery/ledger.json', ledger)
    return ledger['published']


def _finalize(saved, ledger):
    root = _root(saved)
    if ledger.get('transaction'):
        return _publish(root, ledger, ledger['transaction'], saved)
    if ledger.get('phase') == 'committed' and ledger.get('published'):
        files = ledger.get('published_files') or {}
        if not files or all((root / name).is_file() and not (root / name).is_symlink()
                            and _hash(root / name) == digest for name, digest in files.items()):
            return ledger['published']
        ledger.update(phase='publishing', transaction={'generation': ledger['published']['generation'],
                      'summary': ledger['published'], 'files': files})
        _write(root / '.recovery/ledger.json', ledger)
        return _publish(root, ledger, ledger['transaction'], saved)
    prefixes = [_normalize(saved, relative) for relative in ledger['chunks']]
    useful = [(path, report) for path, report in prefixes if path is not None and report['frames']]
    if not useful:
        summary = {'frames': 0, 'replays': 0, 'prefix_frames': 0, 'boundaries': [],
                   'recovery_history': [report for _, report in prefixes]}
        ledger.update(published=summary, phase='committed')
        _write(root / '.recovery/ledger.json', ledger)
        return summary
    profile = useful[0][1]['profile']
    if any(report['profile'] != profile for _, report in useful):
        raise RecoveryError('Recovery video codec, dimensions, pixel format or frame rate do not match')
    frames = sum(report['frames'] for _, report in useful)
    replays = sum(report['replays'] for _, report in useful)
    if frames * BLOCK > MAX_PCM_BYTES:
        raise RecoveryError('Cumulative PCM exceeds the RIFF WAV limit (about 186 minutes); original captures were preserved')
    relative = 'generation-' + uuid.uuid4().hex
    generation = _inside(root, relative); generation.mkdir()
    concat = generation / 'concat.txt'
    # Paths are private generated ASCII names, but the recording root can contain apostrophes.
    concat.write_text(''.join("file '" + str(path / 'video.mkv').replace('\\', '/').replace("'", "'\\''") +
                              f"'\nduration {report['frames'] / 60:.12f}\n"
                              for path, report in useful), encoding='utf-8')
    ffmpeg = saved['options']['ffmpeg']
    _run(ffmpeg, ['-f', 'concat', '-safe', '0', '-i', str(concat), '-map', '0:v:0', '-c:v', 'copy',
                 str(generation / 'video.mkv')], generation / 'ffmpeg.log')
    if _probe(ffmpeg, generation / 'video.mkv')[0] != frames:
        raise RecoveryError('Cumulative video frame count does not match its capture ledger')
    for name, count in [('audio.wav', frames), ('audio_play.wav', frames - replays)]:
        if name == 'audio_play.wav' and saved['options']['audio_source'] != 'game_nrt': continue
        with (generation / name).open('wb') as dst:
            dst.write(_wav_header(count * BLOCK))
            for path, _ in useful:
                offset, size = _wave(path / name)
                with (path / name).open('rb') as src:
                    src.seek(offset)
                    while size:
                        block = src.read(min(size, 1024 * 1024))
                        if not block: raise RecoveryError('Validated PCM source changed')
                        dst.write(block); size -= len(block)
    boundaries, index, segment = [], 0, 0
    with (generation / 'frames.csv').open('w', newline='', encoding='utf-8') as dst, \
            (generation / 'segments.csv').open('w', newline='', encoding='utf-8') as seg:
        writer = csv.writer(dst); writer.writerow(FIELDS)
        seg_writer = csv.DictWriter(seg, fieldnames=SEG_FIELDS); seg_writer.writeheader()
        for path, report in useful:
            if index:
                boundaries.append({'index': index, 'source': report['source']})
            for row in _rows(path / 'frames.csv'):
                writer.writerow((index + row[0], *row[1:]))
            with (path / 'segments.csv').open(newline='', encoding='utf-8') as src:
                for row in csv.DictReader(src):
                    row['segment'] = segment; segment += 1
                    row['first_index'] = index + int(row['first_index'])
                    seg_writer.writerow(row)
            index += report['frames']
    summary = {'frames': frames, 'replays': replays, 'prefix_frames': frames, 'boundaries': boundaries,
               'recovery_history': [report for _, report in prefixes], 'generation': relative}
    ledger['transaction'] = {'generation': relative, 'summary': summary,
                             'files': {name: _hash(generation / name) for name in MEDIA if (generation / name).exists()}}
    ledger['phase'] = 'publishing'
    _write(root / '.recovery/ledger.json', ledger)
    return _publish(root, ledger, ledger['transaction'], saved)


def finalize(saved):
    root = _root(saved)
    with _lock(root):
        ledger = _load(saved)
        return _finalize(saved, ledger) if ledger else None


def prepare(saved):
    root = _root(saved)
    with _lock(root):
        ledger = _load(saved)
        if ledger is None:
            base = _inside(root, 'base')
            if not base.exists():
                stage = _inside(root, 'base-work-' + uuid.uuid4().hex); stage.mkdir()
                with ExitStack() as locks:
                    for name in MEDIA:
                        source = root / name
                        if source.is_file():
                            locks.enter_context(_closed_file(source))
                            shutil.copy2(source, stage / name)
                _write(stage / 'identity.json', {'session_id': saved['id']})
                os.replace(stage, base)
            elif json.loads((base / 'identity.json').read_text(encoding='utf-8'))['session_id'] != saved['id']:
                raise RecoveryError('Existing recovery backup belongs to another recording')
            else:
                # The first ledger write may have failed before any attempt
                # started. Never reuse an older snapshot if the legacy capture
                # subsequently continued writing to the original root.
                with ExitStack() as locks:
                    for name in MEDIA:
                        source, backup = root / name, base / name
                        if source.exists() != backup.exists():
                            raise RecoveryError('Recording changed after the interrupted recovery snapshot')
                        if source.is_file():
                            locks.enter_context(_closed_file(source))
                            if _hash(source) != _hash(backup):
                                raise RecoveryError('Recording changed after the interrupted recovery snapshot; both versions were preserved')
            ledger = {'version': 1, 'session_id': saved['id'], 'chunks': ['base'],
                      'active_capture': 'base', 'phase': 'prepared'}
            _write(root / '.recovery/ledger.json', ledger)
        active = _inside(root, ledger['active_capture'])
        if ledger['phase'] == 'prepared' and ledger['active_capture'] != 'base' and not any((active / name).exists() for name in MEDIA):
            summary = ledger.get('prefix_summary') or ledger.get('published') or {}
        else:
            if ledger['active_capture'] == 'base' and not ledger.get('transaction'):
                _, report = _normalize(saved, 'base')
                summary = {'frames': report['frames'], 'replays': report['replays'],
                           'recovery_history': [report], 'boundaries': []}
            else:
                summary = _finalize(saved, ledger)
            # Publication mutates this in-memory ledger under the same lock.
            attempt = 'attempt-' + uuid.uuid4().hex
            _inside(root, attempt).mkdir()
            ledger['chunks'].append(attempt)
            ledger.update(active_capture=attempt, phase='prepared', prefix_summary=summary)
            _write(root / '.recovery/ledger.json', ledger)
        return {**summary, 'active_capture': str(_inside(root, ledger['active_capture'])),
                'capture_dir': str(_inside(root, ledger['active_capture'])), 'attempt_id': ledger['active_capture'],
                'prefix_frames': int(summary.get('frames', 0))}
