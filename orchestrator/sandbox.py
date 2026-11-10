"""在智能体容器里、零网络的沙箱中运行智能体自写的程序（自动重试时的重算程序）。

与 Claude Code 的 Bash 沙箱同一种隔离（bubblewrap）：
- 独立的网络命名空间，没有任何网卡（连 Anthropic 和主控端口也连不上）；
- 整个文件系统只读，只有 /workspace 与 /tmp 可写；
- 清空环境变量（看不到登录令牌），只设 PATH、HOME、LANG 和主控传入的 CELESTE_* 变量；
- 外面再套一层 timeout，超时连同子进程一起结束。
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from .config import find_docker_exe

from .program_sandbox import PATH, SHELL, TIMEOUT, bwrap_args


@dataclass
class ProgramResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _text(value) -> str:
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''


def run_program(container: str, command: str, timeout_s: float, env: dict[str, str] | None = None) -> ProgramResult:
    timeout_s = max(1.0, float(timeout_s))
    argv = [find_docker_exe(), "exec", "-u", "agent", "-w", "/workspace", container,
            TIMEOUT, "-k", "2", f"{timeout_s:g}", *bwrap_args(env), "--", SHELL, "-c", command]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, input="", capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_s + 15)
        returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout, stderr = _text(exc.stdout), _text(exc.stderr)
    seconds = time.monotonic() - t0
    result = ProgramResult(returncode, _text(stdout), _text(stderr), seconds, timed_out=returncode in (124, 137))
    return result
