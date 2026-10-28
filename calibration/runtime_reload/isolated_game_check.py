"""Actual Everest reload acceptance against a private game, never a live controller.

The clock receiver uses the production line protocol and shared PCM layout, but
is deliberately not OBS. Its two output files remain open through all reloads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
from pathlib import Path
import socket
import struct
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.bridge_client import BridgeClient
from tools.enter_save import load_save_and_freeze
from orchestrator.action_policy import controller_navigation


class ClockReceiver:
    def __init__(self, port: int, output: Path):
        self.port, self.output = port, output
        self.sock = socket.create_connection(('127.0.0.1', port), timeout=10)
        self.sock.settimeout(0.25)
        self.stream = self.sock.makefile('rwb', buffering=0)
        self.send_lock = threading.Lock()
        self.recording = False
        self.closed = False
        self.error = None
        self.mapping = None
        self.frames = []
        self.no_frames = []
        self.files = []
        self.file_handles = []
        self.phase = 'before'
        self.phase_frames = {}
        self.thread = threading.Thread(target=self.run, daemon=True)

    def send(self, text):
        with self.send_lock:
            self.sock.sendall((text + '\n').encode())

    def status(self):
        self.send('S ' + json.dumps({'state': 'recording' if self.recording else 'idle',
            'recording': self.recording, 'audio_nrt': True, 'audio_tracks': 2,
            'frames': len(self.frames), 'dir': str(self.output)}))

    def start(self):
        self.send('L 1')
        self.status()
        self.thread.start()

    def on_line(self, line):
        if line.startswith('R '):
            command = json.loads(line[2:])
            if command['action'] == 'start':
                assert not self.files, 'Recording outputs were reopened'
                self.mapping = mmap.mmap(-1, 64 + 1024 * (64 + 12800),
                                         tagname=f'Local\\CelesteAIRecordAudio-{self.port}', access=mmap.ACCESS_READ)
                for name in ('audio-full.wav', 'audio-play.wav'):
                    file = (self.output / name).open('w+b')
                    file.write(bytes(44)); self.files.append(file)
                self.file_handles = [file.fileno() for file in self.files]
                self.recording = True
            elif command['action'] == 'stop':
                self.recording = False
                self.finish_files()
            self.status()
            return None
        if line.startswith('N '):
            self.no_frames.append(int(line.split()[1])); return int(line.split()[1])
        if line.startswith('F '):
            _, token, frame, ticks = line.split()
            token, frame, ticks = int(token), int(frame), int(ticks)
            if self.recording:
                assert self.mapping is not None
                offset = 64 + token % 1024 * (64 + 12800)
                begin = struct.unpack_from('<q', self.mapping, offset)[0]
                error = struct.unpack_from('<i', self.mapping, offset + 8)[0]
                assert begin == token and not error, (begin, token, error)
                for track, file in enumerate(self.files):
                    file.write(self.mapping[offset + 64 + track * 6400:offset + 64 + (track + 1) * 6400])
                end = struct.unpack_from('<q', self.mapping, offset)[0]
                assert end == token, 'PCM slot changed during read'
                self.frames.append((token, frame, ticks))
                self.phase_frames[self.phase] = self.phase_frames.get(self.phase, 0) + 1
            return token
        return None

    def run(self):
        pending = b''
        token = 1
        outstanding = None
        last_status = time.monotonic()
        try:
            while not self.closed:
                if outstanding is None:
                    outstanding = token; self.send(f'T {token}'); token += 1
                try:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise RuntimeError('Resident clock socket disconnected')
                    pending += chunk
                except socket.timeout:
                    continue
                while b'\n' in pending:
                    raw, pending = pending.split(b'\n', 1)
                    reply = self.on_line(raw.decode().strip())
                    if reply is not None:
                        assert reply == outstanding, (reply, outstanding)
                        outstanding = None
                if time.monotonic() - last_status > 1:
                    self.status(); last_status = time.monotonic()
                time.sleep(1 / 120)
        except Exception as exc:
            if not self.closed:
                self.error = repr(exc)

    def finish_files(self):
        for file in self.files:
            file.seek(0, 2)
            size = file.tell() - 44
            header = b'RIFF' + struct.pack('<I', size + 36) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 3, 2, 48000, 384000, 8, 32) + b'data' + struct.pack('<I', size)
            file.seek(0); file.write(header); file.flush()

    def close(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.thread.join(3)
        self.finish_files()
        for file in self.files:
            file.close()
        if self.mapping:
            self.mapping.close()
        self.sock.close()


def binding_probe(client, before_obs):
    """Everest 重载会按玩家设置重建按键；默认设置下合成的抓（手柄 X）会变成冲刺，对话（Y）会变成跳。
    重载后实际按一次抓和对话，确认核心已重新套用桥接绑定，而不只是发送中立帧。"""
    player = before_obs.get('player') or {}
    result = {}
    if (player.get('dashes') or 0) > 0:
        grab = client.call('step', inputs=['1,G'], image=False, trajectory=False)['obs'].get('player') or {}
        result.update(grab_state=grab.get('state'), grab_dashes=grab.get('dashes'))
        assert grab.get('state') != 'Dash' and grab.get('dashes') == player.get('dashes'), \
            ('Grab was interpreted as a dash after reload', grab)
        client.call('step', inputs=['20'], image=False, trajectory=False)
    if player.get('onGround') is True:
        talk = client.call('step', inputs=['1,T'], image=False, trajectory=False)['obs'].get('player') or {}
        speed_y = (talk.get('speed') or [0, 0])[1]
        result['talk_speed_y'] = speed_y
        assert speed_y >= 0, ('Talk was interpreted as a jump after reload', talk)
        client.call('step', inputs=['20'], image=False, trajectory=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--isolation', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=2)
    parser.add_argument('--bad-dll', action='store_true', help='Fail the first actual reload, restore DLL and retry the same lease')
    args = parser.parse_args()
    isolation = args.isolation.resolve()
    manifest = json.loads((isolation / 'isolation.json').read_text())
    ipc_port, clock_port = manifest['ipc_port'], manifest['clock_port']
    assert ipc_port not in {47774, 47776} and clock_port not in {47774, 47776}
    output = isolation / ('reload-acceptance-' + time.strftime('%Y%m%d-%H%M%S'))
    output.mkdir()
    report = {'boundary': 'Actual private Celeste + ordinary Everest.Loader.ReloadMod; protocol/PCM receiver replaces OBS',
              'cycles': [], 'isolation': str(isolation), 'ports': [ipc_port, clock_port]}
    client = BridgeClient(port=ipc_port, timeout=180)
    receiver = None
    hud = None
    try:
        initial = client.call('hello')
        assert not initial['active'], 'Private game already has an owner'
        with controller_navigation(client, slot=manifest['slot']):
            # A fresh, private Everest settings file shows OuiOOBE. Verified
            # against OuiOOBE.CreateMenu(0): the fourth selectable item is Skip.
            startup = client.call('activate', image=False, entities=False, tiles=False)['obs']
            if (startup.get('menu') or {}).get('current') == 'OuiOOBE':
                for _ in range(3):
                    client.call('step', inputs=['1,D', '12'], image=False, trajectory=False)
                client.call('step', inputs=['1,C', '60'], image=False, trajectory=False)
            observation = load_save_and_freeze(client, 'test', save_slot=manifest['slot'])
            for _ in range(30):
                if observation['scene'] == 'Level':
                    break
                assert observation['scene'] in {'LevelEnter', 'IntroVignette'}, observation
                observation = client.call('step', inputs=['1,C', '60'], image=False, trajectory=False)['obs']
        assert observation['scene'] == 'Level', observation
        client.call('step', inputs=['60'], image=False, trajectory=False)
        hud = mmap.mmap(-1, 4096 + 7 * 1024 * 1024,
                        tagname=f'Local\\CelesteAIHud-{ipc_port}', access=mmap.ACCESS_READ)
        receiver = ClockReceiver(clock_port, output); receiver.start()
        deadline = time.monotonic() + 10
        while not (client.call('hello').get('clock') or {}).get('plugin'):
            assert time.monotonic() < deadline
            time.sleep(.05)
        client.call('record', action='start', audio_source='game_nrt', dir=str(output))
        deadline = time.monotonic() + 5
        while not receiver.recording:
            assert time.monotonic() < deadline
            time.sleep(.01)
        for cycle in range(args.cycles):
            receiver.phase = f'cycle-{cycle}-before'
            client.call('step', inputs=['120'], image=False, trajectory=False)
            before = client.call('hello')
            assert before.get('input_reinit_guard') is True and before.get('input_bindings_ok') is True, \
                ('Runtime cannot re-apply or verify injected bindings', before.get('input_binding_mismatches'))
            before_obs = client.call('observe', image=False, entities=False, tiles=False)['obs']
            prepared = client.call('reload_prepare')
            assert prepared['stage'] == 'prepared', prepared
            lease = prepared['lease']
            receiver.phase = f'cycle-{cycle}-hold'
            max_heartbeat_age_ms = 0
            if args.bad_dll and cycle == 0:
                payload = Path(prepared['payload_path']).resolve()
                assert payload.is_relative_to(isolation / 'Mods'), payload
                original_payload = payload.read_bytes()
                (output / 'payload-before-reload.dll.backup').write_bytes(original_payload)
                try:
                    payload.write_bytes(b'Intentionally invalid isolated acceptance DLL\n')
                    client.call('reload_commit', lease=lease, expected_version=before['bridge'])
                    deadline = time.monotonic() + 130
                    while True:
                        failed = client.call('reload_status', lease=lease)
                        report['bad_dll_status'] = failed
                        if failed['stage'] == 'failed':
                            break
                        assert failed['stage'] != 'ready', failed
                        assert time.monotonic() < deadline, failed
                        time.sleep(.05)
                    assert failed['holding'] and failed['frozen'] and failed['record_audio_active'], failed
                    assert failed['payload_version'] is None, 'Bad DLL did not remove the old payload module'
                    assert failed['frame'] == before['frame'], failed
                    assert failed['record_audio_frames'] == before['record_audio_frames'], failed
                    no_frames = len(receiver.no_frames)
                    time.sleep(.5)
                    assert len(receiver.no_frames) > no_frames, 'Clock stopped replying while failed'
                    assert receiver.phase_frames.get(receiver.phase, 0) == 0, receiver.phase_frames
                    assert not receiver.error, receiver.error
                finally:
                    payload.write_bytes(original_payload)
                report['bad_dll_restored_sha256'] = hashlib.sha256(payload.read_bytes()).hexdigest()
            client.call('reload_commit', lease=lease, expected_version=before['bridge'])
            deadline = time.monotonic() + 120
            while True:
                status = client.call('reload_status', lease=lease)
                report['latest_status'] = status
                heartbeat = struct.unpack_from('<q', hud, 2064)[0]
                max_heartbeat_age_ms = max(max_heartbeat_age_ms, int(time.time() * 1000) - heartbeat)
                report['max_hold_heartbeat_age_ms'] = max_heartbeat_age_ms
                if status['stage'] == 'ready': break
                assert status['stage'] != 'failed', status
                assert time.monotonic() < deadline, status
                time.sleep(.05)
            after = client.call('hello')
            after_obs = client.call('observe', image=False, entities=False, tiles=False)['obs']
            for field in ('frame', 'record_audio_frames', 'record_audio_replay_frames'):
                assert before[field] == after[field], (field, before[field], after[field])
            for field in ('record_music_ms', 'record_music_full_ms'):
                if field in before:
                    assert before[field] == after[field], (field, before[field], after.get(field))
            assert after['record_audio_active'] and after['frozen'], after
            # Everest's LateInitializeModules re-initialized inputs for the new module; the core must have re-applied them.
            assert after.get('input_bindings_ok') is True, ('Bindings not restored after reload', after.get('input_binding_mismatches'))
            assert after.get('input_bindings_reapplied', 0) > before.get('input_bindings_reapplied', 0), \
                'Everest did not re-initialize inputs, or the core did not observe it'
            assert after.get('reload_hold') is True, after
            assert before_obs['save']['slot'] == after_obs['save']['slot'] == manifest['slot']
            for field in ('sid', 'room', 'chapterTimeTicks'):
                assert before_obs['level'][field] == after_obs['level'][field], field
            assert receiver.phase_frames.get(receiver.phase, 0) == 0, receiver.phase_frames
            assert receiver.file_handles == [file.fileno() for file in receiver.files]
            report['cycles'].append({'before': before, 'after': after, 'status': status,
                'before_obs': before_obs, 'after_obs': after_obs, 'ipc_reconnects': client.reconnects})
            client.call('reload_finish', lease=lease)
            receiver.phase = f'cycle-{cycle}-after'
            report['cycles'][-1]['binding_probe'] = binding_probe(client, after_obs)
            client.call('step', inputs=['120'], image=False, trajectory=False)
        client.call('record', action='stop')
        deadline = time.monotonic() + 5
        while receiver.recording:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert not receiver.error, receiver.error
        assert client.reconnects == 0, 'IPC connection was replaced'
        assert all(b[1] == a[1] + 1 for a, b in zip(receiver.frames, receiver.frames[1:])), 'Recorded game frames have gaps or repeats'
        energies = []
        for file in receiver.files:
            file.flush()
            raw = Path(file.name).read_bytes()[44:]
            assert len(raw) == len(receiver.frames) * 6400, (file.name, len(raw), len(receiver.frames))
            samples = struct.unpack('<%df' % (len(raw) // 4), raw)
            energy = sum(sample * sample for sample in samples)
            assert energy > 0, 'Actual private game recording track is silent: ' + file.name
            energies.append(energy)
        report.update(passed=True, frames=len(receiver.frames), no_frame_tokens=len(receiver.no_frames),
                      file_handles=receiver.file_handles, phase_frames=receiver.phase_frames, energies=energies)
    except BaseException as error:
        report['error'] = f'{type(error).__name__}: {error}'
        if receiver:
            report.update(frames=len(receiver.frames), no_frame_tokens=len(receiver.no_frames),
                          phase_frames=receiver.phase_frames, receiver_error=receiver.error)
        raise
    finally:
        if receiver:
            receiver.close()
        if hud:
            hud.close()
        client.close()
        report['source_hashes_unchanged'] = all(hashlib.sha256(Path(path).read_bytes()).hexdigest() == value
                                               for path, value in manifest['source_hashes'].items())
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'cycles'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
