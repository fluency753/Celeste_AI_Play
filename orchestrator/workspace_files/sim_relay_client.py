#!/usr/bin/env python3
"""Compatibility client for the controller-owned, networkless simulator."""
from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import sys
import time
import uuid
from pathlib import Path


def load_request(path):
    state = Path(path).read_bytes().strip()
    if b'\n' in state:
        state = json.dumps(json.loads(state), separators=(',', ':')).encode()
    return b'{"snapshot":' + state + b'}\n'


class Relay:
    def __init__(self):
        endpoint = Path('/sim/endpoint').read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[a-f0-9]{32}', endpoint):
            raise ValueError('无效的主控模拟端点')
        self.directory = Path('/workspace/.sim-relay') / endpoint / uuid.uuid4().hex

    def __enter__(self):
        self.directory.mkdir(mode=0o700)
        try:
            for name in ('request', 'response'):
                os.mkfifo(self.directory / name, 0o600)
            # Open both ends before publishing the lane, so neither side waits
            # forever opening a FIFO whose peer may have exited.
            self.writer = os.open(self.directory / 'request', os.O_RDWR | os.O_NONBLOCK)
            self.reader = os.open(self.directory / 'response', os.O_RDWR | os.O_NONBLOCK)
            (self.directory / 'ready').touch()
            deadline = time.monotonic() + 10
            while not (self.directory / 'attached').exists():
                if (self.directory / 'busy').exists():
                    raise RuntimeError('主控模拟并行通道已满，请减少并行进程后再试')
                if time.monotonic() >= deadline:
                    raise TimeoutError('主控模拟未接入请求管道')
                time.sleep(.005)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_):
        for name in ('writer', 'reader'):
            if (fd := getattr(self, name, None)) is not None:
                os.close(fd)
        shutil.rmtree(self.directory, ignore_errors=True)

    def request(self, data):
        if len(data) > 32 * 1024 * 1024:
            raise ValueError('模拟请求超过运输上限，请缩小快照或单批候选')
        if not data.endswith(b'\n'):
            data += b'\n'
        deadline = time.monotonic() + 90
        pending = memoryview(data)
        while pending:
            if not select.select([], [self.writer], [], max(0, deadline - time.monotonic()))[1]:
                raise TimeoutError('主控模拟未接入请求管道')
            pending = pending[os.write(self.writer, pending):]
        response = bytearray()
        while not response.endswith(b'\n'):
            if not select.select([self.reader], [], [], max(0, deadline - time.monotonic()))[0]:
                raise TimeoutError('主控模拟未返回结果')
            response.extend(os.read(self.reader, 65536))
            if len(response) > 48 * 1024 * 1024:
                raise ValueError('模拟返回数据超过运输上限，请缩小单批候选')
        return bytes(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('serve', 'info', 'run'))
    parser.add_argument('--state')
    parser.add_argument('--inputs', nargs='+')
    args = parser.parse_args()
    with Relay() as relay:
        info = None
        if args.state:
            info = json.loads(relay.request(load_request(args.state)))
            if not info.get('ok'):
                raise ValueError(info['error'])
        if args.command == 'info':
            print(json.dumps(info['info']))
        elif args.command == 'run':
            result = json.loads(relay.request(json.dumps({'inputs': args.inputs}).encode() + b'\n'))
            if not result.get('ok'):
                raise ValueError(result['error'])
            for row in result['frames']:
                print(','.join('' if value is None else str(value) for value in row))
            print('stop:', result.get('stop'), file=sys.stderr)
        else:
            for line in sys.stdin.buffer:
                try:
                    # Most traffic is batch requests. Only decode state changes
                    # on this side; every message is parsed by the broker.
                    if re.match(rb'\s*\{\s*"state"\s*:', line):
                        line = load_request(json.loads(line)['state'])
                    response = relay.request(line)
                except (FileNotFoundError, ValueError) as exc:
                    response = json.dumps({'ok': False, 'error': str(exc)}).encode() + b'\n'
                sys.stdout.buffer.write(response)
                sys.stdout.buffer.flush()


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        if sys.argv[1:2] == ['serve']:
            print(json.dumps({'ok': False, 'error': str(exc)}), flush=True)
        else:
            raise SystemExit(str(exc)) from None
