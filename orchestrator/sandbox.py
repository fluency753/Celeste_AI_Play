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
import json
import re
import tempfile
from dataclasses import dataclass, field

from .config import find_docker_exe

PATH = "/opt/py/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PREVIEW_CHARS = 2000

# Runs outside the command's bwrap, as the same container's unprivileged agent.
# A fresh directory and directory-relative exclusive opens avoid following
# agent-provided output paths or overwriting other runs' artifacts.
OUTPUT_WORKER = r'''
import json,os,secrets,shutil,sys
root=os.open('/workspace',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
name='program-output-'+secrets.token_hex(12)
try:
 os.mkdir(name,0o700,dir_fd=root)
 folder=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
 try:
  fd=os.open('output.txt',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=folder)
  with os.fdopen(fd,'wb') as dest:shutil.copyfileobj(sys.stdin.buffer,dest,65536)
 finally:os.close(folder)
 print(json.dumps({'path':'/workspace/'+name+'/output.txt'}))
finally:os.close(root)
'''


@dataclass
class ProgramResult:
    returncode: int
    stdout: str
    stderr: str
    seconds: float
    timed_out: bool
    output_files: dict[str, str] = field(default_factory=dict)
    output_save_errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tool_payload(self) -> dict:
        """Human-facing preview; raw streams remain intact for machine consumers."""
        data = {'returncode': self.returncode, 'seconds': round(self.seconds, 2), 'timed_out': self.timed_out}
        for name in ('stdout', 'stderr'):
            raw = getattr(self, name)
            truncated = len(raw) > PREVIEW_CHARS
            head = PREVIEW_CHARS // 2
            data[name] = (raw[:head] + f'\n[省略中间 {len(raw)-PREVIEW_CHARS} 字符；此处仅首尾预览]\n'
                          + raw[-(PREVIEW_CHARS-head):]) if truncated else raw
            data[name + '_chars'] = len(raw)
            data[name + '_truncated'] = truncated
            data[name + '_path'] = self.output_files.get(name)
        if self.output_save_errors:
            data['output_save_errors'] = self.output_save_errors
        if any(data[name + '_truncated'] for name in ('stdout', 'stderr')):
            data['output_note'] = ('完整输出见对应 *_path，可用 Read 分页；超长单行请用 Bash 筛选字段或分段读取。'
                                   '路径为空表示未保存完整输出，检查 output_save_errors；也可重新运行并重定向到工作区文件。')
        return data


def _text(value) -> str:
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''


def _save_output(container: str, text: str) -> str:
    with tempfile.TemporaryFile() as stream:
        for offset in range(0, len(text), 65536):
            stream.write(text[offset:offset+65536].encode('utf-8'))
        stream.seek(0)
        reply = subprocess.run([find_docker_exe(), 'exec', '-i', '-u', 'agent', container, 'python3', '-I', '-c', OUTPUT_WORKER],
                               stdin=stream, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
    if reply.returncode:
        raise RuntimeError((reply.stderr or '无法保存输出')[-1000:])
    data = json.loads(reply.stdout)
    path = data.get('path') if isinstance(data, dict) else None
    if not isinstance(path, str) or not re.fullmatch(r'/workspace/program-output-[0-9a-f]{24}/output\.txt', path):
        raise ValueError('输出发布器返回了无效路径')
    return path


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


def run_program(container: str, command: str, timeout_s: float, env: dict[str, str] | None = None, *,
                save_output: bool = False) -> ProgramResult:
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
        stdout, stderr = _text(exc.stdout), _text(exc.stderr)
    seconds = time.monotonic() - t0
    result = ProgramResult(returncode, _text(stdout), _text(stderr), seconds, timed_out=returncode in (124, 137))
    if save_output:
        for name in ('stdout', 'stderr'):
            stream = getattr(result, name)
            if len(stream) > PREVIEW_CHARS:
                try:
                    result.output_files[name] = _save_output(container, stream)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    result.output_save_errors[name] = str(exc)[-1000:]
    return result
