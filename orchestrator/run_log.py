"""运行目录里的主控记录：带时间戳的输出副本与异常退出证据。

标准输出／错误照旧去原来的地方（终端，或控制台启动时的 local/background/<宿主>/output.log）；
这里另抄一份到 `<目录>/controller.log`，每行带本地时间，不管主控从哪里启动。
Python 未捕获异常的堆栈也经标准错误写出，所以同样会进 controller.log。
主控因异常退出时另把完整异常链追加到 `<目录>/controller_error.txt`。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys
import threading
import time
import traceback

LOG_NAME = 'controller.log'
ERROR_NAME = 'controller_error.txt'
EARLY_LINES = 2000      # Output before the directory is known; kept and written on attach.
REASON_CHARS = 300


def stamp(wall: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if wall is None else wall)
    return moment.strftime('%Y-%m-%d %H:%M:%S.') + f'{moment.microsecond // 1000:03d}'


class RunLog:
    """Timestamped copy of everything written to stdout/stderr; never breaks printing."""

    def __init__(self):
        self.lock = threading.Lock()
        self.file = None
        self.path = None
        self.early = []
        self.line_start = True
        self.broken = None

    def _stamped(self, text: str) -> str:
        pieces = []
        for piece in text.splitlines(keepends=True):
            if self.line_start:
                pieces.append(stamp() + ' ')
            pieces.append(piece)
            self.line_start = piece.endswith(('\n', '\r'))
        return ''.join(pieces)

    def write(self, text: str) -> None:
        if not text or self.broken is not None:
            return
        with self.lock:
            stamped = self._stamped(text)
            if self.file is None:
                if len(self.early) < EARLY_LINES:
                    self.early.append(stamped)
                return
            try:
                self.file.write(stamped)
                self.file.flush()
            except (OSError, ValueError) as error:
                self.broken = error

    def attach(self, directory: Path) -> Path:
        """Start writing into this directory (appending); earlier output of this process goes first."""
        path = Path(directory) / LOG_NAME
        stream = open(path, 'a', encoding='utf-8', errors='backslashreplace')
        with self.lock:
            previous, self.file, self.path, self.broken = self.file, stream, path, None
            header = (f'{stamp()} ==== 主控进程 {os.getpid()} 开始记录：'
                      f'{" ".join([Path(sys.argv[0]).name, *sys.argv[1:]]) if sys.argv else ""} ====\n')
            try:
                stream.write(('' if self.line_start else '\n') + header + ''.join(self.early))
                stream.flush()
            except (OSError, ValueError) as error:
                self.broken = error
            self.early = []
        if previous is not None:
            try:
                previous.close()
            except OSError:
                pass
        return path

    def close(self) -> None:
        with self.lock:
            stream, self.file = self.file, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class _Mirror:
    """Stands in for sys.stdout/sys.stderr: the original stream first, then the run log."""

    def __init__(self, stream, log: RunLog):
        self._stream, self._log = stream, log

    def write(self, text):
        result = self._stream.write(text) if self._stream is not None else len(text)
        self._log.write(text)
        return result

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        if self._stream is not None:
            self._stream.flush()

    def isatty(self):
        return bool(self._stream is not None and self._stream.isatty())

    def __getattr__(self, name):
        if self._stream is None:
            raise AttributeError(name)
        return getattr(self._stream, name)


_ACTIVE: RunLog | None = None


def install() -> RunLog:
    """Mirror this process's stdout/stderr (idempotent). Output is held until attach().

    Only the entry modules' ``__main__`` blocks call this, so tests that call main() keep
    their own streams.
    """
    global _ACTIVE
    if _ACTIVE is None:
        _ACTIVE = RunLog()
        sys.stdout = _Mirror(sys.stdout, _ACTIVE)
        sys.stderr = _Mirror(sys.stderr, _ACTIVE)
    return _ACTIVE


def attach(directory: Path) -> Path | None:
    """Write the mirrored output into directory/controller.log; no-op unless installed."""
    if _ACTIVE is None:
        return None
    try:
        return _ACTIVE.attach(directory)
    except OSError as error:
        print(f'[run] 运行目录的主控记录 {Path(directory) / LOG_NAME} 打不开：{error}', file=sys.stderr, flush=True)
        return None


def current_directory() -> Path | None:
    return _ACTIVE.path.parent if _ACTIVE is not None and _ACTIVE.path is not None else None


def describe(error: BaseException) -> dict:
    """Exception type, message, notes and its cause/context chain, for summaries."""
    chain, seen, current = [], {id(error)}, error
    while len(chain) < 8:
        nested = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if nested is None or id(nested) in seen:
            break
        seen.add(id(nested))
        chain.append({'type': type(nested).__name__, 'message': str(nested),
                      'relation': 'cause' if nested is current.__cause__ else 'context'})
        current = nested
    return {'type': type(error).__name__, 'message': str(error),
            'notes': [str(note) for note in getattr(error, '__notes__', None) or []],
            'chain': chain, 'traceback_file': ERROR_NAME, 'wall': time.time()}


def crash_reason(error: BaseException) -> str:
    text = f'主控异常：{type(error).__name__}: {error}'
    return text if len(text) <= REASON_CHARS else text[:REASON_CHARS - 1] + '…'


def is_crash(error: BaseException | None) -> bool:
    """Interrupts and deliberate exits are not crashes."""
    return error is not None and not isinstance(
        error, (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError))


def record_crash(directory: Path | None, error: BaseException) -> Path | None:
    """Append the full exception chain (with notes) to directory/controller_error.txt."""
    if directory is None or not is_crash(error):
        return None
    path = Path(directory) / ERROR_NAME
    try:
        with open(path, 'a', encoding='utf-8', errors='backslashreplace') as stream:
            stream.write(f'==== {stamp()} 主控进程 {os.getpid()} 因异常退出 ====\n')
            stream.write(''.join(traceback.format_exception(error)))
            stream.write('\n')
    except OSError as failure:
        print(f'[run] 异常记录写入失败（{path}）：{failure}', file=sys.stderr, flush=True)
        return None
    return path
