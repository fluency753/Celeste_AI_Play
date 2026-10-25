"""规划回放：把智能体每一轮规划在预置模拟器里推演过的路线画进游戏，给观众看试错的过程。

数据从哪来：/sim/sim.py（本机的预置模拟器接口，不入库）在工作区存在 game/simlog/ 目录时，把每一次推演
记成一条二进制记录（格式见 read_logs）：时刻、开始帧、结果（按键用完 / 死亡 / 换房间 / 剧情 / 不支持）、
帧数、按键序列的哈希、起点与终点、8 个点的形状摘要，以及每一帧的位置。主控按 Claude Code 输出流里的
消息分轮：一条带工具调用的消息的全部工具返回之后，把这期间写出的记录文件移出工作区（运行目录的 playback/pending/），
交给回放线程。准备阶段（第一次执行游戏工具之前）的推演也回放：智能体常在准备阶段就开始规划第一个房间，
之前不画，录像里每章第一个房间（序章）看不到绘制过程；限制规划轮次时准备阶段照旧不扣轮次。

逐轮回放：回放线程只把表头读进内存，挑好要画的路线，马上把这一轮画进游戏（智能体同时在想下一轮；下一轮先结束时排队，
录像里一轮接一轮），放完删掉这一轮的记录文件。一轮放完保留最后的彩色画面；下一轮开始时旧线转成淡灰，真实执行开始时清理。
智能体调用 act 时，主控先等排队的轮次放完（最后一轮变色传导完成），镜头回到 Madeline，开始真实执行，画面上的线全部清掉。

挑哪些路线（select）：线条数随推演条数等比增长——每 playback_ratio 条（默认 100）推演画一条，至少 playback_min_tracks 条
（默认 40；推演更少时全画），不设上限：
1. 必选：金色的一条——这一轮往前走的出口里最快的（离起点不到 40 像素的出口多半是掉头回上一个房间，不算）；
   这一轮没有往前走的出口就没有金色路线；出口路线按出口位置（32 像素一格）各留最快的一条，再补几条最快的；
2. 死亡按死亡地点（12 像素一格）归并，每个地点留一条离目标最近的，"差一点"的排在前面，最多占三分之一；
3. 剩下的名额按路线形状（8 个点的摘要）做最远点采样：每次挑一条和已选的都最不像的；
4. 按推演的先后出场，看起来像搜索一步步往前推进。
路线每帧一个点；画之前把共线的点合并（误差不到 0.3 个游戏像素），点上带着帧号，回放时照样按帧推进。

怎么画（桥接 0.11.1 的 playback 命令，见 game-bridge/Source/Playback.cs）：游戏不推进，桥接只重画画面。
所有轮次用同一个倍速（playback_speed，默认 3 倍）：路线按推演先后在 playback_stagger_s 秒里陆续从 Madeline
身上长出来，延伸中一律淡绿色细线（线多时整体再调淡）；一条线走到头的那一帧定下结果：死亡的从死亡点往起点一路变成淡红，
金色的一条从终点往起点一路变成实心金色（传导速度 playback_recolor，默认 8 倍速），其余的保持淡绿；只画线，不画角色。
镜头先跟着伸得最远的绿线，变金时跟着变色点往回移；没有金色时画完沿最远的线回到起点；所有线变完色之后这一轮再转成淡灰。
进章节的圆圈转场还没放完时，回放帧先把转场放完，黑屏消失后再画线。
录制时这些帧照常录进录像：音乐照常播放、音效暂停，frames.csv 里它们的章节计时记为 -1；画一帧跟不上节拍时，
录制插件把漏掉的节拍补上（记为 -2），音乐保持原速。前几轮留下的淡灰线桥接缓存成一张图，不再每帧重画。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

MAGIC = b"CSL2"
# 时刻、开始帧、结果、帧数（= 点数）、按键哈希、起点 x/y 与终点 x/y（像素 ×8）、8 个点的形状摘要（相对起点，像素 ×2）
HEADER = struct.Struct("<dqBIQiiii16h")
STOPS = {0: "done", 1: "death", 2: "room", 3: "cutscene", 4: "unsupported", 5: "other"}
KIND_OPEN, KIND_DEATH, KIND_EXIT, KIND_BEST = 0, 1, 2, 3
BACKTRACK_PX = 40.0     # 出口离起点这么近的，多半是掉头回上一个房间


def canon_hash(inputs) -> int:
    """按键序列的规范化哈希（与 /sim/sim.py 里的同名函数一致）：展开成逐帧按键、每帧按键排序。"""
    frames: list[str] = []
    for seg in inputs or []:
        seg = str(seg)
        head, _, keys = seg.partition(",")
        try:
            n = int(head.strip())
        except ValueError:
            n, keys = 1, seg
        toks = sorted(t.strip().upper() for t in keys.replace("+", ",").split(",") if t.strip())
        frames.extend([",".join(toks)] * max(0, n))
    digest = hashlib.blake2b(";".join(frames).encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "little")


@dataclass
class LogIndex:
    """一批记录的表头（numpy 数组）；逐帧位置用到时再从来源（文件或字节）里解出来。"""
    t: np.ndarray
    stop: np.ndarray
    npts: np.ndarray
    hashes: np.ndarray
    first: np.ndarray       # (N, 2)
    end: np.ndarray         # (N, 2)
    sig: np.ndarray         # (N, 8, 2)，绝对坐标
    src: np.ndarray         # 每条记录在哪个来源里（sources 的下标）
    off: np.ndarray         # 增量部分在来源里的字节位置
    sources: list           # 来源：Path（按需读取）或 bytes
    start: np.ndarray | None = None   # 每条推演的起点帧号（快照或检查点那一帧）；终点帧 = start + npts

    def __len__(self) -> int:
        return len(self.t)

    def subset(self, idx) -> "LogIndex":
        return LogIndex(self.t[idx], self.stop[idx], self.npts[idx], self.hashes[idx], self.first[idx], self.end[idx],
                        self.sig[idx], self.src[idx], self.off[idx], self.sources, self.ends_at()[idx] - self.npts[idx])

    def ends_at(self) -> np.ndarray:
        """每条推演最后一帧的帧号。从检查点（路线中途）出发的推演帧数少，比快慢要比终点帧号，不能只比帧数。"""
        return (self.start if self.start is not None else np.zeros(len(self.t), dtype=np.int64)) + self.npts

    def _read(self, s: int, off: int, size: int) -> bytes:
        src = self.sources[s]
        if isinstance(src, (bytes, bytearray)):
            return bytes(src[off:off + size])
        with open(src, "rb") as f:
            f.seek(off)
            return f.read(size)

    def path(self, i: int) -> np.ndarray:
        """第 i 条的逐帧位置 (npts, 2)。"""
        n = int(self.npts[i])
        out = np.empty((n, 2), dtype=np.float64)
        out[0] = self.first[i]
        if n > 1:
            raw = self._read(int(self.src[i]), int(self.off[i]), 4 * (n - 1))
            d = np.frombuffer(raw, dtype="<i2", count=2 * (n - 1)).reshape(-1, 2).astype(np.float64) / 8.0
            out[1:] = self.first[i] + np.cumsum(d, axis=0)
        return out


def _empty_log() -> LogIndex:
    z = np.zeros((0,))
    zi = np.zeros((0,), dtype=int)
    return LogIndex(z, zi, zi, np.zeros((0,), dtype=np.uint64), np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0, 8, 2)),
                    zi, zi, [])


def read_logs(sources: list) -> LogIndex:
    """日志文件：MAGIC，然后若干条记录：HEADER，再是其余各帧相对上一帧的增量（int16 ×2，像素 ×8）。
    sources 里是文件路径或原始字节；文件可能在写到一半时被截断（推演进程被结束），读到不完整的记录就停。
    只读表头：逐帧位置留在来源里，LogIndex.path 用到时再读。"""
    rows, where, kept = [], [], []
    for src in sources:
        try:
            data = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
        except OSError:
            continue
        if not data.startswith(MAGIC):
            continue
        s = len(kept)
        kept.append(src)
        pos = len(MAGIC)
        while pos + HEADER.size <= len(data):
            h = HEADER.unpack_from(data, pos)
            npts = h[3]
            body = 4 * max(0, npts - 1)
            if npts <= 0 or pos + HEADER.size + body > len(data):
                break
            rows.append(h)
            where.append((s, pos + HEADER.size))
            pos += HEADER.size + body
    if not rows:
        return _empty_log()
    arr = np.array([r[:5] for r in rows], dtype=np.float64)
    order = np.argsort(arr[:, 0], kind="stable")       # 按推演时刻排序（几个进程并行时交错）
    xy = np.array([r[5:9] for r in rows], dtype=np.float64) / 8.0
    sig = np.array([r[9:] for r in rows], dtype=np.float64).reshape(-1, 8, 2) / 2.0
    first, end = xy[:, 0:2], xy[:, 2:4]
    sig = sig + first[:, None, :]
    hashes = np.array([r[4] for r in rows], dtype=np.uint64)
    loc = np.array(where, dtype=np.int64)
    return LogIndex(arr[order, 0], arr[order, 2].astype(int), arr[order, 3].astype(int), hashes[order],
                    first[order], end[order], sig[order], loc[order, 0], loc[order, 1], kept,
                    arr[order, 1].astype(np.int64))


def select(log: LogIndex, k: int, ref: tuple[float, float] | None = None) -> list[tuple[int, int]]:
    """挑出 k 条有代表性的路线，返回 [(下标, 种类)]，按推演先后排序。"""
    n = len(log)
    if n == 0:
        return []
    if n <= k:
        best = _best(log)
        return [(i, KIND_BEST if i == best else _kind(log, i)) for i in range(n)]
    if ref is None:
        ref = tuple(_far_end(log))
    prog = -np.hypot(log.end[:, 0] - ref[0], log.end[:, 1] - ref[1])
    chosen: dict[int, int] = {}
    best = _best(log)
    if best is not None:
        chosen[best] = KIND_BEST
    exits = np.flatnonzero(log.stop == 2)
    if len(exits):
        fastest = exits[np.argsort(log.ends_at()[exits], kind="stable")]
        gates: dict[tuple[int, int], int] = {}
        for i in fastest:     # 每个出口位置（32 像素一格）先留最快的一条，再按快慢补
            gates.setdefault(tuple(np.floor(log.end[i] / 32).astype(int)), int(i))
        added, cap, seen = 0, max(3, k // 10), set()
        for i in list(gates.values()) + [int(i) for i in fastest]:
            if added >= cap or len(chosen) >= k:
                break
            key = (int(log.ends_at()[i]), int(round(log.end[i, 0])), int(round(log.end[i, 1])))
            if i in chosen or key in seen:     # 同样帧数、同一终点的出口路线几乎重合，只画一条
                continue
            seen.add(key)
            chosen[i] = KIND_EXIT
            added += 1
    deaths = np.flatnonzero(log.stop == 1)
    if len(deaths):
        cells = np.floor(log.end[deaths] / 12).astype(int)
        spot: dict[tuple[int, int], int] = {}
        for i, c in zip(deaths, map(tuple, cells)):
            if c not in spot or prog[i] > prog[spot[c]]:
                spot[c] = int(i)
        for i in sorted(spot.values(), key=lambda j: -prog[j])[: max(1, k // 3)]:
            if len(chosen) >= k:
                break
            chosen.setdefault(i, KIND_DEATH)
    if not chosen:   # 没有金色、出口与死亡路线可选：先放进伸得最远的一条，最远点采样从它出发
        far = int(np.argmax(np.hypot(*(log.end - log.first).T)))
        chosen[far] = _kind(log, far)
    # 最远点采样：在剩下的路线里，每次挑和已选的形状差得最多的
    pool = np.setdiff1d(np.arange(n), np.fromiter(chosen, dtype=int))
    if len(pool) > 20000:
        pool = np.random.default_rng(0).choice(pool, 20000, replace=False)
    if len(pool) and len(chosen) < k:
        ps = log.sig[pool]
        mind = np.full(len(pool), np.inf)
        for i in chosen:
            mind = np.minimum(mind, np.linalg.norm(ps - log.sig[i], axis=2).mean(axis=1))
        while len(chosen) < k:
            j = int(np.argmax(mind))
            if not np.isfinite(mind[j]) or mind[j] < 1.0:   # 剩下的都和已选的几乎重合
                break
            i = int(pool[j])
            chosen[i] = _kind(log, i)
            mind = np.minimum(mind, np.linalg.norm(ps - log.sig[i], axis=2).mean(axis=1))
            mind[j] = -1.0
    return sorted(chosen.items())


def _kind(log: LogIndex, i: int) -> int:
    return KIND_DEATH if log.stop[i] == 1 else KIND_EXIT if log.stop[i] == 2 else KIND_OPEN


def _far_end(log: LogIndex) -> np.ndarray:
    start = log.first[0]
    return log.end[int(np.argmax(np.hypot(*(log.end - start).T)))]


def _best(log: LogIndex, ref=None) -> int | None:
    """一轮的金色路线：往前走的出口里最快的一条（离起点太近的出口多半是掉头回上一个房间，不算）。
    这一轮没有往前走的出口时没有金色路线（返回 None）。"""
    exits = np.flatnonzero(log.stop == 2)
    if not len(exits):
        return None
    away = exits[np.hypot(*(log.end[exits] - log.first[exits]).T) > BACKTRACK_PX]
    if not len(away):
        return None
    return int(away[np.argmin(log.ends_at()[away])])


def simplify(path: np.ndarray, tol: float = 0.3) -> list[int]:
    """合并共线的点（Ramer–Douglas–Peucker，误差不超过 tol 像素），返回保留下来的帧号（下标）。"""
    n = len(path)
    if n <= 2:
        return list(range(n))
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        seg = path[b] - path[a]
        pts = path[a + 1:b] - path[a]
        length = math.hypot(seg[0], seg[1])
        if length < 1e-9:
            dist = np.hypot(pts[:, 0], pts[:, 1])
        else:
            dist = np.abs(pts[:, 0] * seg[1] - pts[:, 1] * seg[0]) / length
        j = int(np.argmax(dist))
        if dist[j] > tol:
            m = a + 1 + j
            keep[m] = True
            stack += [(a, m), (m, b)]
    return list(np.flatnonzero(keep))


def encode_path(path: np.ndarray) -> list[float]:
    """[帧, x, y, 帧, x, y, ...]（只留合并共线点之后的点）。"""
    out: list[float] = []
    for i in simplify(path):
        out += [int(i), round(float(path[i, 0]), 2), round(float(path[i, 1]), 2)]
    return out


@dataclass
class Track:
    kind: int
    path: np.ndarray        # (n, 2) 逐帧位置
    stop: int
    order: int


@dataclass
class Round:
    label: str
    rollouts: int
    tracks: list[Track]
    log: LogIndex | None = None                     # 这一轮的推演表头（挑完路线就不再用）
    best_exit: int = 0                              # 往前走的出口里最快的帧数（0 = 没有到出口的）


@dataclass
class _Msg:
    pending: set = field(default_factory=set)
    label: str = ""
    started: bool = False
    cli: bool = False


class Playback:
    def __init__(self, cfg, session, metrics, gate, log=print):
        self.cfg = cfg
        self.session = session
        self.metrics = metrics
        self.gate = gate
        self.log = log
        # collect：收集推演记录（HUD 的模拟器调用与推演统计、限制规划轮次时按"调用了模拟器的消息"记账，都靠它）；
        # enabled：画进游戏（规划回放）
        # 开模拟器时记录来自 /sim 的 sim.py；关模拟器时来自智能体用 /workspace/game/show.py 的 trajectory() 记下的轨迹
        self.collect = session.workspace is not None
        self.enabled = bool(self.collect and cfg.playback)
        self.dir = session.workspace / "game" / "simlog" if session.workspace is not None else None
        self.archive = session.run_dir / "playback"
        self.pending = self.archive / "pending"
        self.rounds: list[Round] = []          # 上次执行以来收进的各轮（回放线程写、act 读，都在 _cv 下）
        self._msgs: dict[str, _Msg] = {}
        self._tool_msg: dict[str, str] = {}
        self._seq = 0
        self._jobs: "queue.Queue" = queue.Queue()
        self._cv = threading.Condition()
        self._backlog = 0                      # 还没放完的轮次
        self._submit_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        if self.collect:
            self.dir.mkdir(parents=True, exist_ok=True)
            for old in self.dir.glob("*.bin"):
                old.unlink(missing_ok=True)
        if self.collect:
            self._worker = threading.Thread(target=self._run, name="playback", daemon=True)
            self._worker.start()

    # ---- 从输出流分轮（在读输出流的线程里调用，只做轻活） ----

    def note_tool_use(self, message_id: str | None, tool_use_id: str, name: str, tool_input: dict | None) -> None:
        if not self.collect or name.startswith("mcp__celeste__"):
            return
        mid = message_id or tool_use_id
        msg = self._msgs.setdefault(mid, _Msg())
        msg.pending.add(tool_use_id)
        msg.started = msg.started or bool(self.gate is not None and self.gate.started)
        self._tool_msg[tool_use_id] = mid
        if name == "Bash" and isinstance(tool_input, dict):
            if not msg.label:
                msg.label = str(tool_input.get("description") or "").strip()[:80]
            if "celeste-sim" in str(tool_input.get("command") or ""):
                msg.cli = True     # 直接用命令行调用模拟器：没有推演记录，但同样占一轮

    def note_tool_result(self, tool_use_id: str) -> None:
        mid = self._tool_msg.pop(tool_use_id, None)
        if mid is None:
            return
        msg = self._msgs.get(mid)
        if msg is None:
            return
        msg.pending.discard(tool_use_id)
        if msg.pending:
            return
        del self._msgs[mid]
        self._submit(msg.label, msg.started, msg.cli)

    def _submit(self, label: str, started: bool, cli: bool = False) -> None:
        """一条消息的工具全部返回：调用了模拟器就扣一轮规划轮次，并把这期间的记录文件移出工作区、交给回放线程。"""
        with self._submit_lock:
            files = sorted(self.dir.glob("*.bin"))
            if files or cli:
                self.metrics.add(sim_calls=1)
                if started and self.gate is not None:   # 准备阶段不扣轮次（只在限制规划轮次时有）
                    self.gate.charge(label)
            if not files:
                return
            sources, moved = self._take(files)
        with self._cv:
            self._backlog += 1
        self._jobs.put((label, sources, moved, True))

    def collect_prediction(self, predictions: list[dict], start_frame: int) -> None:
        """Collect native predictions immediately; GameSession already counted/charged them."""
        if not self.collect:
            return
        from .sim_assist import write_replay
        # Claim precisely these files before a concurrent Bash collector can see them.
        with self._submit_lock:
            paths = write_replay(self.session.workspace, predictions, start_frame)
            if not paths:
                return
            sources, moved = self._take(paths)
        with self._cv:
            self._backlog += 1
        self._jobs.put(("短路线模拟", sources, moved, False))

    def _take(self, files: list[Path]) -> tuple[list, list[Path]]:
        """把这一轮的日志移出工作区（智能体看不到，也不会被它的下一轮混进来）；移不动的读成字节再删。"""
        self.pending.mkdir(parents=True, exist_ok=True)
        sources, moved = [], []
        for path in files:
            self._seq += 1
            dest = self.pending / f"{self._seq:06d}-{path.name}"
            try:
                os.replace(path, dest)
                sources.append(dest)
                moved.append(dest)
            except OSError:
                try:
                    sources.append(path.read_bytes())
                except OSError:
                    pass
                path.unlink(missing_ok=True)
        return sources, moved

    @staticmethod
    def _delete(files: list[Path]) -> None:
        for path in files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def close(self) -> None:
        """运行结束：停掉回放线程，删掉还没用完的日志。"""
        if self._worker is not None:
            self._jobs.put(None)
            self._worker.join(timeout=10)
            self._worker = None
        with self._cv:
            self.rounds = []
        if self.collect and self.pending.exists():
            self._delete(list(self.pending.glob("*.bin")))

    # ---- 回放线程：逐轮读、挑、画 ----

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            label, sources, moved, account = job
            try:
                self._play_round(label, sources, moved, account)
            except Exception as exc:  # 回放出问题不影响游玩
                self.log(f"[playback] 回放失败：{exc}")
            finally:
                with self._cv:
                    self._backlog -= 1
                    self._cv.notify_all()

    def _play_round(self, label: str, sources: list, moved: list[Path], account: bool = True) -> None:
        t0 = time.monotonic()
        try:
            rd = self._digest(read_logs(sources), label)
        finally:
            self._delete(moved)   # 挑好的路线已经解出逐帧位置，记录文件用完就删
        if rd is None:
            return
        with self._cv:
            if self.enabled:
                self.rounds.append(rd)
            number = len(self.rounds)
        self.log(f"[playback] 第 {number} 轮：推演 {rd.rollouts} 条，挑出 {len(rd.tracks)} 条（{time.monotonic() - t0:.1f} 秒）")
        # HUD：这次调用推演的路线条数（这一轮画完清零）与当前房间推演出的最快出房帧数；总条数另记
        if account:
            self.metrics.add(sim_rollouts=rd.rollouts)
        self.metrics.update(sim_round_rollouts=rd.rollouts)
        if rd.best_exit:
            current = self.metrics.state.sim_best_frames
            if not current or rd.best_exit < current:
                self.metrics.update(sim_best_frames=rd.best_exit)
        if not self.enabled or not rd.tracks:
            self.metrics.update(sim_round_rollouts=0)
            return
        speed = float(self.cfg.playback_speed)
        stagger = int(self.cfg.playback_stagger_s * 60)
        recolor = float(self.cfg.playback_recolor)
        seg = build_segment(rd.tracks, speed, stagger, recolor)
        cap = self.round_cap_frames()
        factor = 1.0
        for _ in range(8):        # 太长：这一轮整体按比例加速（出场间隔、停留与淡出也一起缩短），直到在上限内画完
            if seg["frames"] <= cap:
                break
            factor *= seg["frames"] / cap
            seg = build_segment(rd.tracks, speed * factor, max(1, int(stagger / factor)), recolor * factor,
                                hold=max(1, int(18 / factor)), fade=0)
        seg["camera"] = "keep"
        try:
            reply = self._call(seg, round_no=number, label=rd.label or f"第 {number} 轮规划", runs=rd.rollouts)
        finally:
            self.metrics.update(sim_round_rollouts=0)   # 这一轮画完：HUD 的"推演路径"清零
        record = {"t": time.time(), "decision": self.session.decision_index + 1, "round": number, "label": rd.label,
                  "rollouts": rd.rollouts, "frames": seg["frames"], "speed": seg["speed"], "dim": (reply or {}).get("dim", seg["dim"]),
                  "fade_frames": (reply or {}).get("fade_frames", seg["fade"]),
                  "trail_half_life_frames": (reply or {}).get("trail_half_life_frames"),
                  "spinner_visual_peak": (reply or {}).get("spinner_visual_peak"),
                  "skipped": (reply or {}).get("skipped"),
                  "render_avg_ms": (reply or {}).get("render_avg_ms"), "render_max_ms": (reply or {}).get("render_max_ms"),
                  "tracks": [{"kind": t.kind, "stop": STOPS.get(t.stop), "frames": len(t.path),
                              "end": [round(float(v), 1) for v in t.path[-1]]} for t in rd.tracks]}
        self._archive(record)

    def round_cap_frames(self) -> int:
        """一轮回放最多画多少帧：playback_round_s（1A 第一间那样一屏大的房间）× 当前房间面积 / (320×180)。"""
        bounds = ((self.session.last_obs or {}).get("level") or {}).get("bounds")
        scale = (bounds[2] * bounds[3]) / (320 * 180) if bounds and bounds[2] > 0 and bounds[3] > 0 else 1.0
        return max(1, round(self.cfg.playback_round_s * scale * 60))

    def _call(self, seg: dict, round_no: int, label: str, runs: int, in_act: bool = False) -> dict | None:
        """发一段回放给桥接并等它放完（持有游戏连接的锁）。HUD 在这期间显示"规划回放"。"""
        self.metrics.update(replaying=True, replay_round=round_no, replay_rounds=len(self.rounds),
                            replay_label=label, replay_runs=runs)
        t0 = time.monotonic()
        reply = None
        try:
            with self.session.lock:
                reply = self.session.client.call("playback", **seg)
        except Exception as exc:  # 回放失败不影响执行
            self.log(f"[playback] 回放失败：{exc}")
        finally:
            dt = time.monotonic() - t0
            self.metrics.add(replay_s=dt, replays=1, **({"replay_act_s": dt} if in_act else {}))
            self.metrics.update(replaying=False, replay_round=0, replay_label="", replay_runs=0)
        if reply and reply.get("skipped"):
            self.log(f"[playback] 画面还在转场（{reply['skipped']}），这一段不回放")
        elif reply and reply.get("render_avg_ms") and float(reply["render_avg_ms"]) > 14:
            # 一帧画不完 1/60 秒：录像里靠插件补帧保住声音，画面会显得慢一些
            self.log(f"[playback] 这一段渲染偏慢：平均每帧 {reply['render_avg_ms']} ms，最慢 {reply.get('render_max_ms')} ms")
        return reply

    def _archive(self, record: dict) -> None:
        try:
            self.archive.mkdir(exist_ok=True)
            with open(self.archive / "playbacks.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _digest(self, log: LogIndex, label: str) -> Round | None:
        """一轮结束：只留当前房间里起步的推演，挑出要画的路线并解出它们的逐帧位置。"""
        if len(log) == 0:
            return None
        bounds = ((self.session.last_obs or {}).get("level") or {}).get("bounds")
        if bounds:
            bx, by, bw, bh = bounds
            ok = ((log.first[:, 0] >= bx - 16) & (log.first[:, 0] <= bx + bw + 16)
                  & (log.first[:, 1] >= by - 16) & (log.first[:, 1] <= by + bh + 16))
            if not ok.all():
                log = log.subset(np.flatnonzero(ok))
        n = len(log)
        if n == 0:
            return None
        k = max(self.cfg.playback_min_tracks, math.ceil(n / max(1, self.cfg.playback_ratio)))
        tracks = [Track(kind, log.path(i), int(log.stop[i]), i) for i, kind in select(log, k)]
        exits = np.flatnonzero(log.stop == 2)
        away = exits[np.hypot(*(log.end[exits] - log.first[exits]).T) > BACKTRACK_PX] if len(exits) else exits
        # 最快出房用时：从进入这个房间的那一帧算起（推演可能从房间中途的检查点或停下的地方出发）
        entry = getattr(self.session, "room_entry_frame", None)
        if len(away):
            ends = log.ends_at()[away]
            best_exit = int(ends.min() - entry) if entry is not None else int(log.npts[away].min())
        else:
            best_exit = 0
        return Round(label, n, tracks, best_exit=best_exit)

    # ---- 执行前：等排队的轮次放完，画最终路线 ----

    def drain(self, timeout: float = 600.0) -> None:
        """act 执行之前调用（不持有游戏连接的锁）：等输出流里还没收尾的轮次，把排队的轮次放完。"""
        if not self.collect:
            return
        deadline = time.monotonic() + 2.0
        while any(m.pending for m in list(self._msgs.values())) and time.monotonic() < deadline:
            time.sleep(0.05)   # 上一轮的工具结果可能还没从输出流读到
        if any(self.dir.glob("*.bin")):   # 输出流里还没收尾的那一轮：直接收进来（之后读到它的结果时不再重复记账）
            try:
                stale = list(self._msgs)
                label = next((self._msgs[mid].label for mid in reversed(stale) if self._msgs[mid].label), "")
                for mid in stale:
                    self._msgs.pop(mid, None)
                for tid, mid in list(self._tool_msg.items()):
                    if mid in stale:
                        self._tool_msg.pop(tid, None)
            except (RuntimeError, KeyError):
                label = ""
            self._submit(label, bool(self.gate is not None and self.gate.started))
        with self._cv:
            self._cv.wait_for(lambda: self._backlog <= 0, timeout=timeout)

    def final(self) -> None:
        """GameSession 在执行一次计划之前调用（排队的轮次已经放完；游戏冻结着，调用方持有游戏连接的锁）：
        镜头回到 Madeline（本来就在那里时桥接直接结束），然后开始真实执行。"""
        if not self.enabled:
            return
        with self._cv:
            rounds, self.rounds = self.rounds, []
        if not rounds:
            return
        self.metrics.update(replaying=True, replay_round=0, replay_rounds=len(rounds), replay_label="", replay_runs=0)
        t0 = time.monotonic()
        try:
            self.session.client.call("playback", final=True, frames=0, tracks=[], focus=[], focus_every=4, **{"return": 24})
        except Exception as exc:
            self.log(f"[playback] 镜头回到 Madeline 失败：{exc}")
        finally:
            dt = time.monotonic() - t0
            self.metrics.add(replay_s=dt, replay_act_s=dt)
            self.metrics.update(replaying=False, replay_round=0, replay_rounds=0, replay_label="", replay_runs=0)
        self._archive({"t": time.time(), "decision": self.session.decision_index + 1, "final": True,
                       "rounds": len(rounds), "rollouts": sum(rd.rollouts for rd in rounds)})


def build_segment(tracks: list[Track], speed: float, stagger: int, recolor: float = 4.0, hold: int = 18,
                  fade: int = 0) -> dict:
    """一轮的回放：路线按推演先后在 stagger 帧里陆续出场，每个画面帧推进 speed 帧游戏时间；延伸中一律绿色，
    走到头时死亡的从终点往起点变红、金色的变金（每个画面帧传导 recolor 帧）。所有线都画完、变完色之后再停 hold 帧。
    镜头先跟着伸得最远的绿线，金色路线走到头后跟着变金的那一点往回走；没有金色路线时，所有线画完后沿伸得最远的那条往回走。"""
    n = len(tracks)
    delays = [int(i * stagger / max(1, n - 1)) for i in range(n)]

    def settled(d: int, t: Track) -> float:
        last = max(0, len(t.path) - 1)
        return d + last / speed + (last / recolor if t.kind in (KIND_DEATH, KIND_BEST) else 0.0)

    finish = [d + max(0, len(t.path) - 1) / speed for d, t in zip(delays, tracks)]
    end_at = max(settled(d, t) for d, t in zip(delays, tracks))
    best_i = next((i for i, t in enumerate(tracks) if t.kind == KIND_BEST), None)
    origin = tracks[0].path[0]
    # 没有金色路线时，镜头最后沿着伸得最远的那条线往回走到起点（同样的传导速度）
    lead_i = best_i if best_i is not None else int(np.argmax(
        [float(np.hypot(*(t.path[-1] - origin))) for t in tracks]))
    lead = tracks[lead_i].path
    lead_last = len(lead) - 1
    if best_i is None:
        back_from = max(finish)                 # 所有线都画完之后再往回走
        end_at = max(end_at, back_from + lead_last / recolor)
    else:
        back_from = finish[best_i]              # 金色路线走到头、开始往回变金
    frames = int(math.ceil(end_at)) + hold

    # 镜头目标（每 4 帧一个）：往回走之前跟着"伸得最远的绿线"——此刻所有已出场路线里，前端离起点最远的那一点；
    # 之后沿着金色路线（没有金色时是伸得最远的那条）从终点往回移，回到起点后停住
    times = np.arange(0, frames + 1, 4, dtype=np.float64)
    far_d = np.full(len(times), -1.0)
    far_xy = np.repeat(origin[None, :], len(times), axis=0).astype(np.float64)
    for d, t in zip(delays, tracks):
        last_t = len(t.path) - 1
        idx = np.floor((times - d) * speed).astype(np.int64)
        ok = idx >= 0
        if not ok.any():
            continue
        idx = np.clip(idx, 0, last_t)
        pts = t.path[idx]
        dist = np.hypot(pts[:, 0] - origin[0], pts[:, 1] - origin[1])
        better = ok & (dist > far_d)
        far_d[better] = dist[better]
        far_xy[better] = pts[better]
    focus = []
    for i, f in enumerate(times):
        if f <= back_from:
            x, y = far_xy[i]
        else:
            j = max(0, int(lead_last - (f - back_from) * recolor))
            x, y = lead[j]
        focus += [round(float(x), 1), round(float(y), 1)]
    out = [{"p": encode_path(t.path), "n": len(t.path), "k": t.kind, "d": delays[i]} for i, t in enumerate(tracks)]
    # 线多时整体调淡（淡色线叠在一起会发亮）：60 条以内不变，之后按 sqrt 变淡，最低 0.4 倍
    dim = round(min(1.0, max(0.4, math.sqrt(60.0 / max(1, n)))), 3)
    return {"frames": frames, "speed": round(speed, 4), "recolor": round(recolor, 4), "tracks": out, "focus": focus,
            "focus_every": 4, "fade": fade, "dim": dim}
