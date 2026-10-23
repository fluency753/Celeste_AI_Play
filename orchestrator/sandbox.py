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

PATH = "/opt/py/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TAIL = 2000


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


def bwrap_args(env: dict[str, str] | None = None) -> list[str]:
    args = ["bwrap", "--ro-bind", "/", "/", "--bind", "/workspace", "/workspace", "--bind", "/tmp", "/tmp",
            "--dev", "/dev", "--proc", "/proc",
            "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
            "--die-with-parent", "--new-session", "--clearenv",
            "--setenv", "PATH", PATH, "--setenv", "HOME", "/tmp",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "LC_ALL", "C.UTF-8",
            "--chdir", "/workspace"]
    for key, value in (env or {}).items():
        args += ["--setenv", key, str(value)]
    return args


def run_program(container: str, command: str, timeout_s: float, env: dict[str, str] | None = None) -> ProgramResult:
    timeout_s = max(1.0, float(timeout_s))
    argv = [find_docker_exe(), "exec", "-u", "agent", "-w", "/workspace", container,
            "timeout", "-k", "2", f"{timeout_s:g}", *bwrap_args(env), "--", "sh", "-c", command]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, input="", capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_s + 15)
        returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    seconds = time.monotonic() - t0
    return ProgramResult(returncode, (stdout or "")[-TAIL:], (stderr or "")[-TAIL:], seconds,
                         timed_out=returncode in (124, 137))
