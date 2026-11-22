"""Report when the controller's event loop stops answering.

The container hook (agent-container/celeste-gate.py) waits 25 seconds for /gate. The gate shares the controller's
asyncio loop with the game tools, so a hook timeout means the loop did not run for that long: some code held it,
or the process got no CPU. A heartbeat task on the loop and a watcher thread off it log the loop thread's stack
and the process CPU time while a heartbeat is late, so the next timeout names the code (or the starvation) behind it.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import traceback

STALL_S = 5.0          # heartbeat this late counts as a stall (the hook gives up at 25 s)
BEAT_S = 0.5
CHECK_S = 1.0
RESAMPLE_S = 10.0      # while a stall lasts, sample the stack again this often
MAX_SAMPLES = 3

_watchdogs: dict[int, 'LoopWatchdog'] = {}
_guard = threading.Lock()


def _print(message: str) -> None:
    print(message, flush=True)


class LoopWatchdog:
    def __init__(self, loop, *, stall_s=STALL_S, beat_s=BEAT_S, log=_print,
                 clock=time.monotonic, cpu=time.process_time):
        self.loop, self.stall_s, self.beat_s, self.log = loop, stall_s, beat_s, log
        self.clock, self.cpu = clock, cpu
        self.last, self.last_cpu = clock(), cpu()
        self.loop_thread: int | None = None
        self.stalled_at: float | None = None      # last heartbeat before the stall
        self.stall_cpu: float | None = None
        self.samples = 0
        self.next_sample = 0.0
        self.stopped = threading.Event()
        self._task = None
        self._thread = None

    def start(self) -> None:
        """Call on the loop's own thread."""
        self.loop_thread = threading.get_ident()
        self.last = self.clock()
        self._task = self.loop.create_task(self._heartbeat())
        self._thread = threading.Thread(target=self._watch, name='loop-watchdog', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopped.set()
        if self._task is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self._task.cancel)

    async def _heartbeat(self) -> None:
        while not self.stopped.is_set():
            self.last, self.last_cpu = self.clock(), self.cpu()
            await asyncio.sleep(self.beat_s)

    def _watch(self) -> None:
        while not self.stopped.wait(CHECK_S):
            if self.loop.is_closed():
                return
            try:
                self.check()
            except Exception as exc:  # noqa: BLE001 (diagnostics only)
                self.log(f'[gate] 事件循环看门狗出错：{exc!r}')
                return

    def _stack(self) -> str:
        frame = sys._current_frames().get(self.loop_thread) if self.loop_thread is not None else None
        if frame is None:
            return '（取不到事件循环线程的栈）'
        return ''.join(traceback.format_stack(frame)[-14:]).rstrip()

    def check(self) -> str | None:
        """One watcher pass; returns what it logged."""
        if not self.loop.is_running():
            self.stalled_at = None         # a stopped loop sends no heartbeat; that is not a stall
            return None
        now, last, last_cpu = self.clock(), self.last, self.last_cpu
        late = now - last
        if late >= self.stall_s:
            if self.stalled_at != last:
                self.stalled_at, self.stall_cpu, self.samples, self.next_sample = last, last_cpu, 0, now
            if self.samples >= MAX_SAMPLES or now < self.next_sample:
                return None
            self.samples += 1
            self.next_sample = now + RESAMPLE_S
            used = self.cpu() - self.stall_cpu
            message = (f'[gate] 主控事件循环已 {late:.1f} 秒没有响应（工具门禁在 25 秒时超时）；'
                       f'这段时间主控进程用了 {used:.1f} 秒 CPU。事件循环线程当前在：\n{self._stack()}')
            self.log(message)
            return message
        if self.stalled_at is not None and self.samples:
            took = last - self.stalled_at
            used = self.cpu() - self.stall_cpu
            self.stalled_at = None
            message = f'[gate] 主控事件循环恢复：停了约 {took:.1f} 秒，期间主控进程用了 {used:.1f} 秒 CPU'
            self.log(message)
            return message
        self.stalled_at = None
        return None


def ensure_watchdog(loop=None, **options) -> LoopWatchdog:
    """Start one watchdog per running loop (the first /gate request starts it)."""
    loop = loop or asyncio.get_running_loop()
    with _guard:
        for key in [key for key, dog in _watchdogs.items() if dog.loop.is_closed()]:
            _watchdogs.pop(key).stop()
        dog = _watchdogs.get(id(loop))
        if dog is None or dog.loop is not loop:
            dog = _watchdogs[id(loop)] = LoopWatchdog(loop, **options)
            dog.start()
        return dog


def stop_watchdog(loop) -> None:
    with _guard:
        dog = _watchdogs.pop(id(loop), None)
    if dog is not None:
        dog.stop()
