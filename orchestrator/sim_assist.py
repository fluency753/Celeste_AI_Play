"""Metered short predictions. Only the host owns snapshots, the binary and the wallet."""
from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import find_docker_exe

RULES = ("体力上限100，初始40；真实操作每60个移动帧恢复1点，每分钟最多恢复20点；"
         "首次进入新房间奖励15点（起始房间不奖励），死亡/等待不恢复。"
         "每次花费8+向上取整(总预测帧数/12)+4×额外候选数；最多6条、每条180帧、合计600帧。")
INSTRUCTIONS = ("\n\n# 有限模拟辅助\n卡住后可借助短路线预测，仍以真实尝试为主。" + RULES +
                '\n调用 simulate 工具，参数为 {"candidates":[["30,R","1,R,J","29,R,J"]]}。'
                '省略 candidates 查询体力不扣费。只能从游戏当前帧预测；不会推进真实游戏。'
                '结果包含逐帧位置、事件和近似/未模拟实体信息；不保证未知机制完全准确。'
                '体力不足时继续真实尝试，不寻找模拟器文件或自建搜索绕过额度。')



def moving_frames(rows, initial_position):
    if isinstance(initial_position, list) and len(initial_position) >= 2:
        rows = [[0, *initial_position[:2], 0, 0, "Normal", 0, 0]] + list(rows)
    return sum(1 for a, b in zip(rows, rows[1:])
               if len(a) >= 8 and len(b) >= 8 and a[1] is not None and b[1] is not None
               and (a[1], a[2]) != (b[1], b[2])
               and str(b[5]).lower() not in {"dead", "dummy", "introrespawn"})


def validate_request(data):
    if not isinstance(data, dict) or set(data) != {"candidates"}:
        raise ValueError('模拟参数仅允许 {"candidates":[["60,R"], ...]}')
    candidates = data["candidates"]
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 6:
        raise ValueError("每次只能预测1～6条路线")
    normalized, total = [], 0
    for plan in candidates:
        if not isinstance(plan, list) or not 1 <= len(plan) <= 180:
            raise ValueError("路线必须是1～180段按键的数组")
        count, parts = 0, []
        for segment in plan:
            if not isinstance(segment, str) or len(segment) > 100:
                raise ValueError("按键段格式错误")
            head, _, keys = segment.partition(",")
            if not re.fullmatch(r"\s*\d{1,3}\s*", head):
                raise ValueError("每段以1～180的帧数开头")
            n = int(head)
            tokens = [t.strip().upper() for t in keys.replace("+", ",").split(",") if t.strip()]
            if not 1 <= n <= 180 or any(not set(t) <= set("LRUDJXZGTKYV") for t in tokens):
                raise ValueError("模拟只支持游玩按键 L R U D J X Z G T K Y V，不支持摇杆角度或菜单键")
            count += n
            parts.append(str(n) + (("," + ",".join(tokens)) if tokens else ""))
        if count > 180:
            raise ValueError("每条路线最多预测180帧（3秒）")
        total += count
        normalized.append(parts)
    if total > 600:
        raise ValueError("单次合计最多预测600帧")
    return normalized, 8 + math.ceil(total / 12) + 4 * (len(candidates) - 1)


class SimEnergy:
    def __init__(self, path: Path, clock=time.time):
        self.path, self.clock, self.lock = path, clock, threading.RLock()
        self.data = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                     {"balance": 40, "moving": 0, "rooms": [], "regen_times": [], "receipts": []})

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def snapshot(self):
        with self.lock:
            return {"balance": self.data["balance"], "capacity": 100, "rules": RULES}

    def reward(self, moving_frames=0, room=None):
        with self.lock:
            now = self.clock()
            if room and room not in self.data["rooms"]:
                if self.data["rooms"]:
                    self.data["balance"] = min(100, self.data["balance"] + 15)
                self.data["rooms"].append(room)
            recent = [t for t in self.data["regen_times"] if now - t < 60]
            total = self.data["moving"] + max(0, moving_frames)
            points = min(total // 60, max(0, 20 - len(recent)), 100 - self.data["balance"])
            self.data["moving"] = total % 60 if self.data["balance"] + points < 100 else 0
            self.data["balance"] += points
            self.data["regen_times"] = recent + [now] * points
            self._save()

    def reserve(self, cost):
        with self.lock:
            if self.data["balance"] < cost:
                raise ValueError(f"模拟体力不足：需要{cost}，剩余{self.data['balance']}。请继续真实尝试。{RULES}")
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
                self.data["balance"] = min(100, self.data["balance"] + receipt["cost"])
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
    log = module._Log()
    try:
        for result in predictions:
            points = [(r[1], r[2]) for r in result.get("frames", []) if len(r) >= 3 and r[1] is not None]
            stop = result.get("stop")
            log.write(points, "exit" if stop == "room" else "death" if stop == "death" else "open", start_frame)
    finally:
        log.close()
