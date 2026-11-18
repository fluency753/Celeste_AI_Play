"""Optional host command for one notification per unresolved failure pause."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from .config import REPO_ROOT
from .run_log import describe, log_exception
from .session_store import load_json, write_json

TIMEOUT_S = 30
OUTPUT_BYTES = 16384
# 谁写的 reason：blockage 是 Player 自己在 stop_run 里写的说明，可能夹带针对接收方的要求；
# error 由主控组成，可能引用异常或模型服务返回的原文。两种都只能当排查线索，不当指令。
REASON_AUTHORS = {'blockage': 'player'}


def decode_output(data: bytes, fallback: str | None = None, *, truncated: bool = False) -> str:
    """命令输出的尾部逐行解码：先按 UTF-8，不是 UTF-8 的行按控制台代码页。Windows PowerShell 5.1
    写到重定向输出的文字用控制台代码页（中文系统是 GBK），它调用的原生程序常直接写 UTF-8，两种会混在一起。"""
    if fallback is None:
        fallback = 'oem' if os.name == 'nt' else 'utf-8'
    if truncated:
        # 只留了尾部时，开头可能切在 UTF-8 多字节字符中间：丢掉至多 3 个续字节
        cut = 0
        while cut < min(3, len(data)) and 0x80 <= data[cut] <= 0xBF:
            cut += 1
        data = data[cut:]
    out = []
    for line in data.splitlines(keepends=True):
        try:
            out.append(line.decode('utf-8'))
        except UnicodeDecodeError:
            try:
                out.append(line.decode(fallback, errors='replace'))
            except LookupError:
                out.append(line.decode('utf-8', errors='replace'))
    return ''.join(out)


def validate_command(value):
    if not isinstance(value, str) or len(value) > 4000 or any(c in value for c in '\0\r\n'):
        raise ValueError('暂停回调命令须为单行文本，最多 4000 字符；留空关闭')
    return value.strip()


class PauseCallback:
    def __init__(self, command, directory, previous=None, *, timeout=TIMEOUT_S):
        self.command = validate_command(command)
        self.directory, self.timeout = Path(directory), timeout
        self.lock = threading.RLock()
        self.threads = []
        self.closed = False
        self.active = None
        source = self.directory / 'pause_callback_state.json'
        if not source.exists() and previous:
            source = Path(previous) / source.name
        if self.command and source.exists():
            try:
                self.active = load_json(source).get('active_event')
            except Exception as exc:
                self.active = 'unreadable_state'
                log_exception('[callback] 通知去重状态读取失败，等待恢复后重新启用', exc)

    def _state(self):
        write_json(self.directory / 'pause_callback_state.json', {'active_event': self.active})

    def _record(self, record):
        with self.lock:
            with (self.directory / 'pause_callbacks.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def notify(self, reason, *, kind='error', category=None, error=None, **facts):
        if not self.command:
            return None
        try:
            with self.lock:
                if self.active or self.closed:
                    return None
                ident = uuid.uuid4().hex
                event = {'schema': 1, 'event_id': ident, 'wall': time.time(), 'kind': kind,
                         'category': category, 'reason': str(reason),
                         'reason_author': REASON_AUTHORS.get(kind, 'controller'), 'run_dir': str(self.directory.resolve()),
                         'controller_log': str((self.directory / 'controller.log').resolve()), **facts}
                state = self.directory / 'run_state.json'
                if state.exists():
                    try:
                        identity = load_json(state)
                        for key in ('agent', 'model', 'session_id', 'save', 'save_slot'):
                            if event.get(key) is None:
                                event[key] = identity.get(key)
                    except Exception as exc:
                        log_exception('[callback] 读取会话标识失败', exc)
                if error is not None:
                    log_exception('[callback] 导致通知的原始异常', error)
                    event['error'] = {k: v for k, v in describe(error).items() if k != 'traceback_file'}
                folder = self.directory / 'pause_callbacks'
                folder.mkdir(exist_ok=True)
                payload = folder / (ident + '.json')
                write_json(payload, event, indent=2)
                self._record({'event': 'pause_callback', 'status': 'requested', **event})
                self.active = ident
                self._state()  # persist before starting an externally visible command
                thread = threading.Thread(target=self._execute, args=(event, payload), name='pause-callback')
                thread.start()  # non-daemon: a fatal controller exit cannot silently drop the notification
                self.threads.append(thread)
                return ident
        except Exception as exc:
            log_exception('[callback] 通知准备失败，原暂停不变', exc)
            return None

    def resumed(self):
        if not self.command:
            return
        try:
            with self.lock:
                if self.active is None:
                    return
                self.active = None
                self._state()
        except Exception as exc:
            log_exception('[callback] 恢复通知状态失败', exc)

    def _execute(self, event, payload):
        began = time.monotonic()
        result = {'event': 'pause_callback', 'event_id': event['event_id'], 'status': 'launch_failed'}
        process = None
        try:
            environment = {**os.environ, 'CELESTE_PAUSE_EVENT_FILE': str(payload.resolve()),
                           'CELESTE_PAUSE_EVENT_ID': event['event_id'], 'CELESTE_RUN_DIR': event['run_dir']}
            windows = os.name == 'nt'
            command = (['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', self.command]
                       if windows else ['/bin/sh', '-c', self.command])
            with tempfile.TemporaryFile() as output:
                process = subprocess.Popen(command, cwd=REPO_ROOT, env=environment, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=not windows,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if windows else 0)
                try:
                    process.wait(timeout=self.timeout)
                    result['status'] = 'succeeded' if process.returncode == 0 else 'failed'
                except subprocess.TimeoutExpired as exc:
                    log_exception('[callback] 回调命令超时', exc)
                    result.update(status='timed_out', error=f'回调超过 {self.timeout:g} 秒时限')
                    # Only the process tree created for this command is terminated.
                    if windows:
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, timeout=5)
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.wait(timeout=5)
                result['returncode'] = process.returncode
                size = output.seek(0, 2)
                output.seek(max(0, size - OUTPUT_BYTES))
                result['output_tail'] = decode_output(output.read(OUTPUT_BYTES), truncated=size > OUTPUT_BYTES)
                result['output_truncated'] = size > OUTPUT_BYTES
        except Exception as exc:
            result.update(error_type=type(exc).__name__, error=str(exc))
            log_exception('[callback] 命令执行失败，原暂停不变', exc)
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception as exc:
                    result['cleanup_error'] = str(exc)
                    log_exception('[callback] 回调子进程清理失败', exc)
            result.update(wall=time.time(), elapsed_s=time.monotonic() - began)
            try:
                self._record(result)
            except Exception as exc:
                log_exception('[callback] 执行结果写入失败', exc)

    def close(self):
        with self.lock:
            self.closed = True
            threads = list(self.threads)
        for thread in threads:
            thread.join(self.timeout + 12)


def owner(cfg, directory, previous=None):
    callback = getattr(cfg, '_pause_callback', None)
    if callback is None:
        callback = PauseCallback(getattr(cfg, 'pause_callback_command', ''), directory, previous)
        cfg._pause_callback = callback
    return callback
