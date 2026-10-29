#!/usr/bin/env python3
"""Own one App Server and its descendants across Docker stdio disconnections."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time


def process(pid):
    try:
        folder = Path('/proc')/str(pid)
        raw = (folder/'stat').read_text()
        fields = raw[raw.rfind(')')+2:].split()
        return {'pid':int(pid), 'ppid':int(fields[1]), 'start':fields[19],
                'uid':folder.stat().st_uid, 'state':fields[0]}
    except (OSError, ValueError, IndexError):
        return None


def matches(record):
    actual = process(record['pid'])
    return actual and actual['start'] == record['start'] and actual['uid'] == record['uid'] and actual['state'] != 'Z'


def collect(known):
    parents = {pid for pid, record in known.items() if matches(record)}
    table = {}
    for item in Path('/proc').iterdir():
        if item.name.isdigit() and (record := process(item.name)):
            table[record['pid']] = record
    changed = True
    while changed:
        changed = False
        for pid, record in table.items():
            if pid not in known and record['ppid'] in parents and record['uid'] == os.getuid():
                known[pid] = record
                parents.add(pid)
                changed = True


def send(record, sig):
    if record['pid'] != os.getpid() and matches(record):
        try:
            os.kill(record['pid'], sig)
        except ProcessLookupError:
            pass


def cleanup(known):
    collect(known)
    for record in list(known.values()):
        send(record, signal.SIGTERM)
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        collect(known)
        live = [r for r in known.values() if r['pid'] != os.getpid() and matches(r)]
        if not live:
            return
        for record in live:
            send(record, signal.SIGTERM)
        time.sleep(.05)
    for record in list(known.values()):
        send(record, signal.SIGKILL)
    # Catch a last child forked between the final scan and its parent's exit.
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        collect(known)
        live = [r for r in known.values() if r['pid'] != os.getpid() and matches(r)]
        if not live:
            return
        for record in live:
            send(record,signal.SIGKILL)
        time.sleep(.05)
    raise RuntimeError('Owned descendants did not stop')


def marker(owner):
    if not re.fullmatch(r'[a-f0-9]{32}', owner):
        raise ValueError('Invalid owner id')
    root = Path(os.environ['CODEX_HOME'])/'hosts'
    if root.is_symlink():
        raise ValueError('Owner directory is a symlink')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root/(owner+'.json')
    if path.is_symlink():
        raise ValueError('Owner marker is a symlink')
    return path


def terminate(owner):
    path = marker(owner)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {'stopped':True, 'already_stopped':True}
    if data.get('owner') != owner or data.get('uid') != os.getuid():
        raise ValueError('Owner marker does not match caller')
    launcher = data['launcher']
    if matches(launcher):
        send(launcher, signal.SIGTERM)
        deadline = time.monotonic()+8
        while time.monotonic() < deadline and matches(launcher):
            time.sleep(.05)
    # This also handles a stale launcher marker with a surviving exact child.
    known = {record['pid']:record for record in (launcher,data['server']) if matches(record)}
    if known:
        cleanup(known)
    live = [record for record in known.values() if matches(record)]
    if live:
        time.sleep(.1)
        live = [record for record in live if matches(record)]
    if live:
        raise RuntimeError('Owned processes did not stop')
    path.unlink(missing_ok=True)
    return {'stopped':True}


def child_setup():
    os.setsid()
    if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise OSError('Could not set parent-death signal')


def launch(owner, command):
    if not command:
        raise ValueError('Missing App Server command')
    libc = ctypes.CDLL(None)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError('Could not become child subreaper')
    path = marker(owner)
    stopped = threading.Event()
    eof = threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):
        signal.signal(sig, lambda *_:stopped.set())
    launcher = process(os.getpid())
    child = subprocess.Popen(command, stdin=subprocess.PIPE, preexec_fn=child_setup)
    known = {os.getpid():launcher, child.pid:process(child.pid)}
    published = False

    def stdin_copy():
        try:
            while chunk := os.read(sys.stdin.fileno(),65536):
                child.stdin.write(chunk)
                child.stdin.flush()
        except (OSError, BrokenPipeError):
            pass
        finally:
            child.stdin.close()
            eof.set()

    try:
        with path.open('x',encoding='utf-8') as output:
            json.dump({'owner':owner,'uid':os.getuid(),'launcher':launcher,'server':known[child.pid]},output)
            output.flush()
            os.fsync(output.fileno())
        published = True
        threading.Thread(target=stdin_copy,daemon=True).start()
        eof_at = None
        while child.poll() is None and not stopped.is_set():
            collect(known)
            if eof.is_set():
                eof_at = eof_at or time.monotonic()
                if time.monotonic()-eof_at > 3:
                    break
            time.sleep(.05)
        return child.returncode or 0
    finally:
        cleanup(known)
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            send(known[child.pid],signal.SIGKILL)
            child.wait(timeout=3)
        # As subreaper, reap every adopted child without waiting for unrelated work.
        while True:
            try:
                if os.waitpid(-1,os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if published:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--owner')
    mode.add_argument('--terminate')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.terminate:
        print(json.dumps(terminate(args.terminate)))
        return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    return launch(args.owner,command)


if __name__ == '__main__':
    raise SystemExit(main())
