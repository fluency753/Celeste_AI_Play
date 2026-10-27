"""Opt-in recovery acceptance against an already running private game and OBS.

Uses the production Recorder and real encoder/PCM; only window selection is
replaced with a PID-scoped check because the test game uses a private dotnet host.
Never launches, stops or connects to a personal game or OBS instance.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
from unittest.mock import patch

from PIL import Image, ImageStat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orchestrator.action_policy import controller_navigation
from orchestrator.recording import Recorder, RecordOptions, find_ffmpeg, load_config
from tools.bridge_client import BridgeClient
from tools.enter_save import load_save_and_freeze

QUIET = dict(image=False, entities=False, tiles=False, trajectory=False)


def powershell_json(command):
    result = subprocess.run(['powershell', '-NoProfile', '-Command', command],
                            check=True, capture_output=True, encoding='utf-8', timeout=20)
    return json.loads(result.stdout) if result.stdout.strip() else None


def process(pid):
    return powershell_json(f"Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\" | "
                           'Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress')


def snapshots(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def read_rows(directory):
    with (directory / 'frames.csv').open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def wav_info(ffprobe, path):
    info = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(path)]))
    stream = info['streams'][0]
    assert stream['channels'] == 2 and stream['sample_rate'] == '48000', stream
    raw = path.read_bytes()
    cursor = 12
    while cursor + 8 <= len(raw):
        name, size = raw[cursor:cursor + 4], struct.unpack_from('<I', raw, cursor + 4)[0]
        cursor += 8
        if name == b'data':
            payload = raw[cursor:cursor + size]
            assert len(payload) == size and size % 8 == 0, path
            samples = struct.unpack(f'<{size // 4}f', payload)
            return {'sample_frames': size // 8, 'energy': sum(value * value for value in samples),
                    'duration': float(stream.get('duration', size / 8 / 48000))}
        cursor += size + (size & 1)
    raise AssertionError(f'Missing WAV data: {path}')


def wait(check, seconds=60):
    deadline = time.monotonic() + seconds
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() >= deadline:
            raise TimeoutError('Private acceptance condition did not become ready')
        time.sleep(.1)


def decoded_hashes(ffmpeg, video, count):
    result = subprocess.check_output([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(video),
                                      '-frames:v', str(count), '-f', 'framemd5', '-'], timeout=120).decode('ascii')
    return [line.rsplit(',', 1)[-1].strip() for line in result.splitlines() if line and not line.startswith('#')]


def check(args):
    directory, game, obs_root = args.output.resolve(), args.isolation.resolve(), args.obs_dir.resolve()
    assert (ROOT / 'local').resolve() in directory.parents, 'Evidence must stay under ignored local/'
    assert (ROOT / 'local').resolve() in game.parents and (ROOT / 'local').resolve() in obs_root.parents
    directory.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((game / 'isolation.json').read_text())
    ipc, clock = manifest['ipc_port'], manifest['clock_port']
    assert ipc == 48974 and clock == 48976, 'This check only permits the prepared private ports'
    game_process, obs_process = process(args.game_pid), process(args.obs_pid)
    assert game_process and Path(game_process['ExecutablePath']).resolve() == game / 'piton-runtime/dotnet.exe'
    assert obs_process and Path(obs_process['ExecutablePath']).resolve() == obs_root / 'bin/64bit/obs64.exe'
    user_obs = Path(os.environ['APPDATA']) / 'obs-studio'
    source_files = [Path(p) for p in manifest['source_hashes']]
    source_files += [p for p in user_obs.rglob('*') if p.is_file() and p.suffix in {'.ini', '.json'}
                     and ('basic' in p.parts or p.name in {'global.ini', 'user.ini'})]
    before_original = snapshots(source_files)
    ffmpeg = find_ffmpeg(load_config().get('ffmpeg'))
    ffprobe = str(Path(ffmpeg).with_name('ffprobe.exe'))
    report = {'boundary': 'Production Recorder + actual private Celeste, portable OBS, FFmpeg and dual PCM',
              'external_override': 'Only gamewindow.ensure_visible selects the verified private PID',
              'output': str(directory), 'ports': [ipc, clock], 'private_game': game_process,
              'private_obs': obs_process, 'phases': [], 'source_hashes_before': before_original}
    report['encoder_flush_packets'] = args.flush_packets
    client = None
    active = None

    def visible(_log):
        from orchestrator.gamewindow import _api
        ctypes, wt, user32, _kernel, enum_proc = _api()
        windows = []
        def visit(hwnd, _):
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == args.game_pid:
                text = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, text, 256)
                if text.value == 'SDL_app':
                    windows.append(hwnd)
            return True
        user32.EnumWindows(enum_proc(visit), 0)
        assert windows, 'Private game window missing'
        if user32.IsIconic(windows[0]) or not user32.IsWindowVisible(windows[0]):
            user32.ShowWindow(windows[0], 4)  # SW_SHOWNOACTIVATE

    def encoders():
        rows = powershell_json("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
                               'Select-Object ProcessId,CreationDate,ExecutablePath,CommandLine | ConvertTo-Json -Compress') or []
        rows = [rows] if isinstance(rows, dict) else rows
        return [p for p in rows if p.get('CommandLine') and str(directory).lower() in p['CommandLine'].lower()
                and 'video.mkv' in p['CommandLine'].lower()]

    def record_phase(name, recorder):
        report['phases'].append({'name': name, 'session_id': recorder.session_id,
                                 'logical_dir': str(recorder.opts.dir), 'encoders': encoders(),
                                 'hello': client.call('hello')})

    def playback(count):
        obs = client.call('observe', **QUIET)['obs']
        x, y = obs['player']['pos']
        return client.call('playback', frames=count, speed=1, tracks=[{'p': [0, x, y, count, x + 12, y], 'k': 3}], **{'return': 0})

    try:
        client = BridgeClient(port=ipc, timeout=180)
        hello = client.call('hello')
        plugin = (hello.get('clock') or {}).get('plugin') or {}
        assert not plugin.get('recording') and plugin.get('state') != 'recording', plugin
        with controller_navigation(client, slot=manifest['slot']):
            obs = load_save_and_freeze(client, 'test', save_slot=manifest['slot'])
            for _ in range(120):
                if obs['scene'] == 'Level':
                    break
                assert obs['scene'] == 'LevelEnter', obs['scene']
                obs = client.call('step', inputs=['1,C', '6'], **QUIET)['obs']
        assert obs['scene'] == 'Level' and obs['save']['slot'] == manifest['slot']
        client.call('step', inputs=['60'], **QUIET)
        wait(lambda: (client.call('hello').get('clock') or {}).get('plugin'))
        visible(print)
        # Game capture hooks the private D3D device asynchronously. Let actual
        # unrecorded draws warm it before trusting the first encoded prefix.
        for _ in range(25):
            client.call('step', inputs=['1'], **QUIET)
            time.sleep(.1)
        run_dirs = [directory / f'run-{i}' for i in range(1, 6)]
        for run in run_dirs:
            run.mkdir()
        logical = run_dirs[0] / 'recording'
        with patch('orchestrator.recording.gamewindow.ensure_visible', visible):
            video_args = '-c:v libx264 -preset ultrafast -crf 18 -g 60'
            if args.flush_packets:
                video_args += ' -flush_packets 1'
            active = Recorder(client.call, RecordOptions(logical, ffmpeg,
                              video_args, audio_source='game_nrt'))
            active.bind_run(run_dirs[0])
            active.start()
            first_id = active.session_id
            record_phase('initial', active)
            client.call('step', inputs=['60'], **QUIET)
            playback(24)
            client.call('step', inputs=['60'], **QUIET)
            first_result = active.stop(timeout=180)
            prefix_rows = read_rows(logical)
            prefix_video = hashlib.sha256((logical / 'video.mkv').read_bytes()).hexdigest()
            prefix_decoded = decoded_hashes(ffmpeg, logical / 'video.mkv', len(prefix_rows))
            report.update(prefix_frames=len(prefix_rows), initial_result=first_result,
                          prefix_video_sha256=prefix_video)
            saved = Recorder.saved_session(run_dirs[0])
            active = Recorder.recover(client.call, saved, run_dirs[1])
            assert active.session_id == first_id and active.opts.dir.resolve() == logical.resolve()
            assert hashlib.sha256((logical / 'video.mkv').read_bytes()).hexdigest() == prefix_video
            record_phase('recovered-closed-encoder', active)
            client.call('step', inputs=['60'], **QUIET)
            playback(12)
            active.retain()
            kept_encoder = encoders()
            active = Recorder.adopt(client.call, Recorder.saved_session(run_dirs[1]), run_dirs[2])
            assert active.session_id == first_id and encoders() == kept_encoder
            client.call('step', inputs=['60'], **QUIET)
            second_result = active.stop(timeout=180)
            second_count = len(read_rows(logical))
            assert second_count > len(prefix_rows)
            report['second_result'] = second_result
            active = Recorder.recover(client.call, Recorder.saved_session(run_dirs[2]), run_dirs[3])
            record_phase('second-recovery', active)
            client.call('step', inputs=['120'], **QUIET)
            if args.kill_encoder:
                victims = wait(encoders, 10)
                assert len(victims) == 1, victims
                victim = victims[0]
                assert process(victim['ProcessId']) == victim, 'Private encoder identity changed'
                assert Path(victim['ExecutablePath']).resolve() == Path(ffmpeg).resolve()
                from orchestrator.recording_recovery import capture_directory
                physical = capture_directory(Recorder.saved_session(run_dirs[3]))
                report['killed_video_bytes'] = (physical / 'video.mkv').stat().st_size
                if args.require_empty_encoder_output:
                    assert report['killed_video_bytes'] == 0, 'This scenario requires a proven empty encoded video'
                # The unique new evidence path binds this side effect to this
                # test encoder, even when personal ffmpeg processes also exist.
                command = (f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(victim['ProcessId'])}\"; "
                           f"if (-not $p -or $p.CommandLine -notlike '*{directory.name}*video.mkv*') {{ throw 'Encoder target changed' }}; "
                           f"Stop-Process -Id {int(victim['ProcessId'])} -ErrorAction Stop")
                subprocess.run(['powershell', '-NoProfile', '-Command', command], check=True, timeout=20)
                report['killed_encoder'] = victim
                client.call('step', inputs=['1'], **QUIET)
                try:
                    report['failed_result'] = active.stop(timeout=180)
                except Exception as error:
                    report['failed_finalization'] = f'{type(error).__name__}: {error}'
                active = Recorder.recover(client.call, Recorder.saved_session(run_dirs[3]), run_dirs[4])
                record_phase('recovered-killed-encoder', active)
                client.call('step', inputs=['60'], **QUIET)
            final_result = active.stop(timeout=180)
            rows = read_rows(logical)
            assert final_result['frames'] == len(rows), (final_result['frames'], len(rows))
            assert [int(r['index']) for r in rows] == list(range(len(rows)))
            assert rows[:len(prefix_rows)] == prefix_rows, 'Old CSV prefix changed'
            assert len(rows) > second_count
            if not args.kill_encoder:
                assert final_result['check']['missing_frames'] == 0, final_result['check']
            play_count = sum(int(row['chapter_ticks']) >= 0 for row in rows)
            assert play_count < len(rows), 'Real replay frames were not recorded'
            full_audio, play_audio = wav_info(ffprobe, logical / 'audio.wav'), wav_info(ffprobe, logical / 'audio_play.wav')
            assert full_audio['sample_frames'] == len(rows) * 800
            assert play_audio['sample_frames'] == play_count * 800
            assert full_audio['energy'] > 0 and play_audio['energy'] > 0
            probe = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-count_frames', '-show_streams',
                                                       '-of', 'json', str(logical / 'video.mkv')]))
            video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
            assert int(video['nb_read_frames']) == len(rows), (video, len(rows))
            assert decoded_hashes(ffmpeg, logical / 'video.mkv', len(prefix_rows)) == prefix_decoded, 'Decoded prefix changed'
            for name, at in [('before-recovery.png', .5), ('after-recovery.png', len(prefix_rows) / 60 + .5)]:
                subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', str(at), '-i',
                                str(logical / 'video.mkv'), '-frames:v', '1', str(directory / name)], check=True, timeout=60)
                picture = Image.open(directory / name).convert('RGB')
                assert sum(ImageStat.Stat(picture).mean) > 30, f'Black or nearly black capture: {name}'
                assert len(picture.getcolors(picture.width * picture.height)) > 100, f'Missing game content: {name}'
            subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-i', str(logical / 'video.mkv'),
                            '-f', 'null', '-'], check=True, timeout=120)
            pointers = [Recorder.saved_session(run) for run in run_dirs if (run / 'recording_session.json').exists()]
            assert all(p['id'] == first_id and Path(p['options']['dir']).resolve() == logical.resolve() for p in pointers)
            report.update(passed=True, session_id=first_id, logical_dir=str(logical), frames=len(rows),
                          play_frames=play_count, full_audio=full_audio, play_audio=play_audio,
                          video=video, final_result=final_result, ipc_reconnects=client.reconnects)
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        if client:
            try:
                if active and active.started:
                    try:
                        active.stop(timeout=60)
                    except Exception as error:
                        report['cleanup_error'] = f'{type(error).__name__}: {error}'
            finally:
                client.close()
        report['source_hashes_after'] = snapshots(source_files)
        report['source_hashes_unchanged'] = before_original == report['source_hashes_after']
        report['private_processes_after'] = {'game': process(args.game_pid), 'obs': process(args.obs_pid)}
        (directory / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('passed', 'frames', 'play_frames', 'session_id', 'logical_dir')}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolation', type=Path, required=True)
    parser.add_argument('--obs-dir', type=Path, required=True)
    parser.add_argument('--game-pid', type=int, required=True)
    parser.add_argument('--obs-pid', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--kill-encoder', action='store_true', help='Also terminate only this test encoder and recover it')
    parser.add_argument('--flush-packets', action='store_true', help='Flush MKV packets to test a readable crash prefix')
    parser.add_argument('--require-empty-encoder-output', action='store_true', help='Require the killed encoder file to be zero bytes')
    check(parser.parse_args())


if __name__ == '__main__':
    main()
