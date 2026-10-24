"""给观众看的展示接口（主控在每次运行开始时写进 /workspace/game/show.py；只用标准库）。

    import sys; sys.path.insert(0, "/workspace/game")
    from show import status, trajectory

    status("第 3 轮搜索，已试 2 万条")             # 运行程序期间，HUD 的工具栏实时显示这句进度（开了实时展示时）
    trajectory([(x0, y0), (x1, y1), ...], "exit")  # 记一条算出来的 Madeline 逐帧轨迹，系统按轮画进游戏给观众看

只用于展示，不影响游戏，也不影响成绩。
- status(text)：一句中文进度；程序结束（这次工具调用返回）后自动清掉。
- trajectory(points, result="open", start_frame=None)：points 是每帧推进之后 Madeline 的位置（与 act 返回的轨迹
  x、y 同一口径），按帧的先后排列；result 是这条轨迹的结局："exit"（到达出口、进入下一个房间）、"death"（死亡）、
  "open"（其他，例如还没走完）。start_frame 是轨迹出发的那一帧的帧号，不写时用游戏当前停下的这一帧。
  轨迹按工具调用分轮：一条消息的工具都返回后，这期间记下的轨迹作为一轮画出来（大约每 100 条画 1 条，至少 40 条）。
"""
from __future__ import annotations

import array
import json
import os
import struct
import time

_GAME = "/workspace/game"
_STATUS = os.path.join(_GAME, "hud_status.json")
_LOG_DIR = os.path.join(_GAME, "simlog")
_LOG_MAGIC = b"CSL2"
_LOG_HEAD = struct.Struct("<dqBIQiiii16h")
_RESULTS = {"open": 0, "done": 0, "death": 1, "exit": 2, "room": 2}
_LOG_LIMIT = 768 * 1024 * 1024      # 单个文件超过这个大小后只记四分之一

_last_status = 0.0


def status(text: str, min_interval: float = 0.2) -> None:
    """HUD 工具栏的实时进度（一句话）。调用很频繁时自动限流。"""
    global _last_status
    now = time.time()
    if now - _last_status < min_interval:
        return
    _last_status = now
    try:
        tmp = f"{_STATUS}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"status": str(text)[:100], "t": now, "pid": os.getpid()}, f, ensure_ascii=False)
        os.replace(tmp, _STATUS)
    except OSError:
        pass


def _current_frame() -> int:
    try:
        with open(os.path.join(_GAME, "latest_obs.json"), encoding="utf-8") as f:
            obs = json.load(f)
        obs = obs.get("obs", obs) if isinstance(obs, dict) else {}
        return int(obs.get("frame") or 0)
    except (OSError, ValueError, TypeError):
        return 0


class _Log:
    def __init__(self):
        self.file = None
        self.size = 0
        self.count = 0
        self.frame = None

    def write(self, points, result: str, start_frame) -> None:
        if not os.path.isdir(_LOG_DIR):
            return              # 这次运行没有开规划回放
        rows = [(float(p[0]), float(p[1])) for p in points]
        if not rows:
            return
        self.count += 1
        if self.size > _LOG_LIMIT and self.count % 4:
            return
        if start_frame is None:
            if self.frame is None:
                self.frame = _current_frame()
            start_frame = self.frame
        if self.file is None:
            name = f"{time.time_ns()}-{os.getpid()}-show.bin"
            self.file = open(os.path.join(_LOG_DIR, name), "ab", buffering=1 << 20)
            self.file.write(_LOG_MAGIC)
            self.size = len(_LOG_MAGIC)
        xs = [int(round(x * 8)) for x, _ in rows]
        ys = [int(round(y * 8)) for _, y in rows]
        n = len(rows)
        sig = []
        for k in range(8):
            j = (k * (n - 1)) // 7
            sig.append(max(-32768, min(32767, (xs[j] - xs[0]) // 4)))
            sig.append(max(-32768, min(32767, (ys[j] - ys[0]) // 4)))
        deltas = array.array("h")
        px, py = xs[0], ys[0]
        for x, y in zip(xs[1:], ys[1:]):
            dx = max(-32768, min(32767, x - px))
            dy = max(-32768, min(32767, y - py))
            deltas.append(dx)
            deltas.append(dy)
            px, py = px + dx, py + dy
        head = _LOG_HEAD.pack(time.time(), int(start_frame), _RESULTS.get(str(result).lower(), 0), n, 0,
                              xs[0], ys[0], xs[-1], ys[-1], *sig)
        body = deltas.tobytes()
        self.file.write(head)
        self.file.write(body)
        self.size += len(head) + len(body)
        if self.count % 256 == 0:
            self.file.flush()

    def close(self) -> None:
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                pass
            self.file = None


_log = _Log()


def trajectory(points, result: str = "open", start_frame: int | None = None) -> None:
    """记一条算出来的 Madeline 逐帧轨迹（只用于展示）。"""
    try:
        _log.write(points, result, start_frame)
    except Exception:
        pass


def flush() -> None:
    if _log.file is not None:
        try:
            _log.file.flush()
        except OSError:
            pass


import atexit  # noqa: E402

atexit.register(_log.close)
