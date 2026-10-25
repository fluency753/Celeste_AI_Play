"""Metered short predictions. Only the host owns snapshots, the binary and the wallet."""
from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path

from .config import find_docker_exe

@dataclass(frozen=True)
class AssistRules:
    """限额预测的规则（可调参数 assist_*，见 tuning.py；默认值就是一直以来的规则）。"""
    capacity: int = 100
    initial: int = 40
    regen_frames: int = 60
    regen_per_min: int = 20
    room_bonus: int = 15
    cost_base: int = 8
    cost_frames: int = 12
    cost_extra: int = 4
    max_candidates: int = 6
    max_frames: int = 180
    max_total: int = 600

    @classmethod
    def of(cls, cfg) -> "AssistRules":
        return cls(**{name: getattr(cfg, "assist_" + name, default)
                      for name, default in ((f.name, f.default) for f in fields(cls))})

    def text(self) -> str:
        return (f"体力上限{self.capacity}，初始{self.initial}；真实操作每{self.regen_frames}个移动帧恢复1点，"
                f"每分钟最多恢复{self.regen_per_min}点；"
                f"首次进入新房间奖励{self.room_bonus}点（起始房间不奖励），死亡/等待不恢复。"
                f"每次花费{self.cost_base}+向上取整(总预测帧数/{self.cost_frames})+{self.cost_extra}×额外候选数；"
                f"最多{self.max_candidates}条、每条{self.max_frames}帧、合计{self.max_total}帧。")

    def instructions(self) -> str:
        return ("\n\n# 有限模拟辅助\n几何核验后仍有路线歧义，或第一次受阻时，就可主动用它比较 2–3 条能区分假设的短路线（如换起跳点或改变方向），看终点、碰撞与死亡结果，不必等待连续失败。仍以真实尝试为主，不制造无意义的预测，不自动耗尽体力。"
                + self.text() +
                '\n调用 simulate 工具，参数为 {"candidates":[["30,R","1,R,J","29,R,J"]]}。'
                '省略 candidates 查询体力不扣费。只能从游戏当前帧预测；不会推进真实游戏。'
                '结果包含逐帧位置、事件和近似/未模拟实体信息；不保证未知机制完全准确。'
                '体力不足时继续真实尝试，不寻找模拟器文件绕过额度。')


DEFAULT_RULES = AssistRules()
RULES = DEFAULT_RULES.text()
INSTRUCTIONS = DEFAULT_RULES.instructions()


def moving_frames(rows, initial_position):
    if isinstance(initial_position, list) and len(initial_position) >= 2:
        rows = [[0, *initial_position[:2], 0, 0, "Normal", 0, 0]] + list(rows)
    return sum(1 for a, b in zip(rows, rows[1:])
               if len(a) >= 8 and len(b) >= 8 and a[1] is not None and b[1] is not None
               and (a[1], a[2]) != (b[1], b[2])
               and str(b[5]).lower() not in {"dead", "dummy", "introrespawn"})


def validate_request(data, rules: AssistRules = DEFAULT_RULES):
    r = rules
    if not isinstance(data, dict) or set(data) != {"candidates"}:
        raise ValueError('模拟参数仅允许 {"candidates":[["60,R"], ...]}')
    candidates = data["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= r.max_candidates:
        raise ValueError(f"每次只能预测1～{r.max_candidates}条路线")
    normalized, total = [], 0
    for plan in candidates:
        if not isinstance(plan, list) or not 1 <= len(plan) <= r.max_frames:
            raise ValueError(f"路线必须是1～{r.max_frames}段按键的数组")
        count, parts = 0, []
        for segment in plan:
            if not isinstance(segment, str) or len(segment) > 100:
                raise ValueError("按键段格式错误")
            head, _, keys = segment.partition(",")
            if not re.fullmatch(r"\s*\d{1,4}\s*", head):
                raise ValueError(f"每段以1～{r.max_frames}的帧数开头")
            n = int(head)
            tokens = [t.strip().upper() for t in keys.replace("+", ",").split(",") if t.strip()]
            if not 1 <= n <= r.max_frames or any(not set(t) <= set("LRUDJXZGTKYV") for t in tokens):
                raise ValueError("模拟只支持游玩按键 L R U D J X Z G T K Y V，不支持摇杆角度或菜单键")
            count += n
            parts.append(str(n) + (("," + ",".join(tokens)) if tokens else ""))
        if count > r.max_frames:
            raise ValueError(f"每条路线最多预测{r.max_frames}帧（{r.max_frames / 60:g}秒）")
        total += count
        normalized.append(parts)
    if total > r.max_total:
        raise ValueError(f"单次合计最多预测{r.max_total}帧")
    return normalized, r.cost_base + math.ceil(total / r.cost_frames) + r.cost_extra * (len(candidates) - 1)


class SimEnergy:
    def __init__(self, path: Path, clock=time.time, rules: AssistRules = DEFAULT_RULES):
        self.path, self.clock, self.lock, self.rules = path, clock, threading.RLock(), rules
        self.data = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                     {"balance": rules.initial, "moving": 0, "rooms": [], "regen_times": [], "receipts": []})

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def snapshot(self):
        with self.lock:
            return {"balance": self.data["balance"], "capacity": self.rules.capacity, "rules": self.rules.text()}

    def reward(self, moving_frames=0, room=None):
        with self.lock:
            r = self.rules
            now = self.clock()
            if room and room not in self.data["rooms"]:
                if self.data["rooms"]:
                    self.data["balance"] = min(r.capacity, self.data["balance"] + r.room_bonus)
                self.data["rooms"].append(room)
            recent = [t for t in self.data["regen_times"] if now - t < 60]
            total = self.data["moving"] + max(0, moving_frames)
            points = min(total // r.regen_frames, max(0, r.regen_per_min - len(recent)), r.capacity - self.data["balance"])
            self.data["moving"] = total % r.regen_frames if self.data["balance"] + points < r.capacity else 0
            self.data["balance"] += points
            self.data["regen_times"] = recent + [now] * points
            self._save()

    def reserve(self, cost):
        with self.lock:
            if self.data["balance"] < cost:
                raise ValueError(f"模拟体力不足：需要{cost}，剩余{self.data['balance']}。请继续真实尝试。{self.rules.text()}")
            ident = uuid.uuid4().hex
            self.data["balance"] -= cost
            self.data["receipts"].append({"id": ident, "cost": cost, "status": "reserved"})
            self._save()
            return ident

    def finish(self, ident, *, refund=False):
        with self.lock:
            receipt = next(r for r in self.data["receipts"] if r["id"] == ident)
            if receipt["status"] != "reserved":
                return
            if refund:
                self.data["balance"] = min(self.rules.capacity, self.data["balance"] + receipt["cost"])
            receipt["status"] = "refunded" if refund else "used"
            self._save()


def run_predictions(cfg, directory: Path, candidates):
    """A private, networkless worker; never expose /sim or full scene state to agent Bash."""
    docker, name = find_docker_exe(), "celeste-sim-assist-" + uuid.uuid4().hex
    command = [docker, "run", "--rm", "-i", "--name", name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "32",
               "--memory", "512m", "--cpus", "2", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
               "-e", "DOTNET_EnableDiagnostics=0", "-v", f"{cfg.sim_dir}:/sim:ro",
               "-v", f"{directory.resolve()}:/state:ro", "--entrypoint", "/sim/celeste-sim",
               cfg.image, "serve"]
    requests = [{"state": "/state/state.json"}, {"batch": [
        {"inputs": p, "stop_on_death": True, "stop_on_room": True} for p in candidates]}]
    try:
        result = subprocess.run(command, input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if result.returncode or len(replies) != 2 or not all(r.get("ok") for r in replies):
            raise RuntimeError("模拟辅助服务失败：" + (result.stderr or str(replies))[-1000:])
        results = replies[1]["results"]
        if len(results) != len(candidates) or any(not r.get("ok") for r in results):
            raise RuntimeError("模拟辅助返回了不完整的结果")
        return {"info": replies[0]["info"], "results": results}
    finally:
        # Killing a timed-out Docker client does not reliably stop its container.
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=10)


def write_replay(workspace, predictions, start_frame):
    """Use the existing binary trajectory format without running agent-controlled Python."""
    import importlib.util
    source = Path(__file__).parent / "workspace_files" / "show.py"
    spec = importlib.util.spec_from_file_location("_assist_show", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._LOG_DIR = str(workspace / "game" / "simlog")
    (workspace / "game" / "simlog").mkdir(parents=True, exist_ok=True)
    log = module._Log()
    path = None
    try:
        for result in predictions:
            points = [(r[1], r[2]) for r in result.get("frames", []) if len(r) >= 3 and r[1] is not None]
            stop = result.get("stop")
            log.write(points, "exit" if stop == "room" else "death" if stop == "death" else "open", start_frame)
    except Exception:
        path = Path(log.file.name) if log.file else None
        log.close()
        if path:
            path.unlink(missing_ok=True)  # partial files must not be recounted as legacy predictions
        raise
    finally:
        if log.file:
            path = Path(log.file.name)
        log.close()
    return [path] if path else []
