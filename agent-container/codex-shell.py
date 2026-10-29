#!/usr/bin/env python3
"""Keep native shell commands foreground, bounded and in the existing bwrap sandbox."""
import argparse
import base64
import ctypes
import math
import os
from pathlib import Path
import subprocess
import sys
import signal

from program_sandbox import BASH, TIMEOUT, bwrap_args


def command_args(command, seconds, cwd):
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('无效的程序时长上限')
    path = Path(cwd).resolve()
    # Codex already creates the zero-network namespace and blocks network syscalls.
    # A second --unshare-net would need NETLINK_ROUTE, which that sandbox denies.
    return [TIMEOUT, '-k', '2', f'{seconds:g}',
            *bwrap_args(cwd=path.as_posix(), network_isolated=True), '--', BASH, '-c', command]


def parent_death_signal(parent):
    """Attach timeout's lifetime to this native shell wrapper, including SIGKILL."""
    if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), '无法绑定程序父进程生命周期')
    if os.getppid() != parent:
        os.kill(os.getpid(), signal.SIGKILL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', required=True, type=float)
    parser.add_argument('--command-base64', required=True)
    args = parser.parse_args()
    try:
        command = base64.b64decode(args.command_base64, validate=True).decode('utf-8')
        argv = command_args(command, args.seconds, os.getcwd())
        # Native shell owns stdout/stderr; do not stringify or truncate its result here.
        parent = os.getpid()
        result = subprocess.run(argv, timeout=args.seconds + 15,
                                preexec_fn=lambda: parent_death_signal(parent))
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except subprocess.TimeoutExpired:
        print('程序超过主控时长上限，已结束。', file=sys.stderr)
        return 124
    except (ValueError, OSError) as exc:
        print(f'原生 Shell 未执行：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
