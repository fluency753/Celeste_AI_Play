"""Controller-owned, unmetered simulation for container speedruns."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from .config import REPO_ROOT, find_docker_exe
from .session_store import write_json
from .sim_assist import LEFTOVERS, _read_leftovers, _remove_container, _write_leftovers, retry_leftovers

RELAY_PATH = '/workspace/.sim-relay'


def enabled(cfg):
    return getattr(cfg, 'sim_relay', False) is True


def identity(cfg):
    return hashlib.sha256(str(cfg.workspace_dir.resolve()).encode()).hexdigest()[:24]


def public_dir(cfg):
    return REPO_ROOT / 'local' / 'sim-relay' / identity(cfg)


def volume(cfg):
    return 'celeste-sim-relay-' + identity(cfg)


def worker_mounts(cfg) -> list[str]:
    """私有模拟进程的只读挂载：发布目录挂到 /sim；本机有官方地图数据（sim/maps，与发布目录同级、不入库）时挂到 /maps，
    模拟器据此跨房间推演（它在自身目录旁找不到 maps 时找上一级的 maps）。地图不进智能体容器的 /sim。"""
    mounts = ['-v', f'{cfg.sim_dir}:/sim:ro']
    maps = cfg.sim_dir.parent / 'maps'
    if maps.is_dir():
        mounts += ['-v', f'{maps}:/maps:ro']
    return mounts


def prepare(cfg):
    from .container import docker
    public = public_dir(cfg)
    public.mkdir(parents=True, exist_ok=True)
    for name in ('README.md', 'VERSION', 'statemap.json', 'sim_assets.json'):
        source = cfg.sim_dir / name
        if source.is_file():
            shutil.copyfile(source, public / name)
    source = (cfg.sim_dir / 'sim.py').read_text(encoding='utf-8')
    # The broker owns telemetry, including calls made directly by Bash.
    (public / 'sim.py').write_text(source + '\n_LOG_DIR = "/dev/null"\n', encoding='utf-8', newline='\n')
    cli = public / 'celeste-sim'
    source = Path(__file__).parent / 'workspace_files' / 'sim_relay_client.py'
    cli.write_text(source.read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
    cli.chmod(0o755)
    docker('volume', 'create', '--driver', 'local', '--opt', 'type=tmpfs', '--opt', 'device=tmpfs',
           '--opt', 'o=size=256m,mode=1777', volume(cfg))


class SimRelay:
    def __init__(self, session):
        self.session = session
        self.docker = find_docker_exe()
        self.endpoint = uuid.uuid4().hex
        self.name = None
        self.process = self.reader = None
        self.ready = threading.Event()
        self.lifecycle = threading.Lock()
        self.closed = False
        self.lock = threading.Lock()
        self.calls = self.count = 0
        self.timer = None

    def start(self):
        with self.lifecycle:
            if self.closed:
                return
            self._start()

    def _start(self):
        cfg = self.session.cfg
        private = self.session.run_dir / 'sim_private'
        self.ledger = private / LEFTOVERS
        rows = retry_leftovers(private, self.docker)
        name = 'celeste-sim-assist-' + uuid.uuid4().hex
        write_json(self.ledger, [*rows, {'name': name, 'run': str(self.session.run_dir), 'attempts': 0}], indent=1)
        self.name = name
        engine = REPO_ROOT / 'agent-container' / 'sim-relay.py'
        available_cpus = subprocess.check_output([self.docker, 'info', '--format', '{{.NCPU}}'], timeout=8, text=True)
        cpus = str(max(1, int(available_cpus)))
        command = [self.docker, 'run', '--rm', '--init', '--pull', 'never', '--name', name, '--network', 'none',
                   '--read-only', '--log-driver', 'none', '--user', '1500:1500', '--cap-drop', 'ALL',
                   '--security-opt', 'no-new-privileges', '--pids-limit', '768', '--cpus', cpus, '--memory', '3g',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=256m,mode=1777',
                   '--label', 'celeste.sim-assist=1', '--label', f'celeste.run={self.session.run_dir.name}',
                   '-e', 'DOTNET_EnableDiagnostics=0', *worker_mounts(cfg),
                   '-v', f'{engine}:/engine.py:ro', '-v', f'{volume(cfg)}:/relay', '-v', f'{private}:/control:ro',
                   '--entrypoint', 'python3', cfg.image, '-u', '/engine.py', self.endpoint]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()
        if not self.ready.wait(15) or self.process.poll() is not None:
            raise RuntimeError('主控速通模拟容器未就绪')
        public = public_dir(cfg)
        (public / 'endpoint').write_text(self.endpoint, encoding='ascii')

    def read(self):
        for line in self.process.stdout:
            try:
                event = json.loads(line)
                if event.get('ready'):
                    self.ready.set()
                elif type(event.get('simulated')) is int:
                    self.accept(event)
            except Exception as exc:  # noqa: BLE001 – keep draining worker audit even if playback has failed
                print(f'[sim] 主控模拟的统计或回放读取失败：{exc}', flush=True)

    def accept(self, event):
        with self.lock:
            self.calls += 1
            self.count += event['simulated']
            if self.timer is None:
                self.timer = threading.Timer(.5, self.flush)
                self.timer.daemon = True
                self.timer.start()
        playback = self.session.playback
        if playback is None or not playback.enabled or 'trace' not in event:
            return
        obs = self.session.last_obs or {}
        level = obs.get('level') or {}
        start = event['start']
        if (start['sid'] and start.get('digest') == getattr(self.session, 'sim_snapshot_digest', None)
                and (start['sid'], start['room'], start['frame']) == (level.get('sid'), level.get('room'), obs.get('frame'))):
            playback.collect_prediction([event['trace']], start['frame'])

    def flush(self):
        with self.lock:
            calls, count = self.calls, self.count
            self.calls = self.count = 0
            self.timer = None
            if count:
                try:
                    self.session.metrics.add(sim_calls=calls, sim_rollouts=count)
                except OSError as exc:
                    print(f'[sim] 主控模拟统计落盘失败：{exc}', flush=True)

    def close(self):
        with self.lifecycle:
            self.closed = True
            self._close()

    def _close(self):
        if self.name is not None:
            try:
                subprocess.run([self.docker, 'stop', '-t', '2', self.name], timeout=6, capture_output=True)
            except (OSError, subprocess.TimeoutExpired):
                pass
            if self.process is not None:
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        self.process.kill()
                        self.process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        print(f'[sim] 主控速通模拟的 Docker 客户端退出未确认：{exc}', flush=True)
            gone, error = _remove_container(self.docker, self.name)
            if gone:
                _write_leftovers(self.ledger, [row for row in _read_leftovers(self.ledger) if row['name'] != self.name])
            else:
                print(f'[sim] 主控速通模拟容器待清理：{error}', flush=True)
            self.name = None
        if self.reader is not None:
            self.reader.join(timeout=2)
        if self.process is not None and self.process.stdout is not None and (self.reader is None or not self.reader.is_alive()):
            self.process.stdout.close()
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
        self.flush()
