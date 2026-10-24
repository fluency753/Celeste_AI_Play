"""可编辑的文字目标和可核验停止条件，不依赖模型口头宣称完成。"""
import json
import math
import threading
import time

FIELDS = {"room", "map", "side", "map_complete", "deaths", "frame", "elapsed"}
NUMERIC = {"deaths", "frame", "elapsed"}


def validate_goal(data):
    if not isinstance(data, dict):
        raise ValueError("目标必须是对象")
    text = data.get("text", "")
    if not isinstance(text, str):
        raise ValueError("目标文字必须是字符串")
    text = text.strip()
    conditions = data.get("conditions", [])
    match = data.get("match", "all")
    if len(text) > 4000 or not isinstance(match, str) or match not in {"all", "any"}:
        raise ValueError("目标文字过长或条件组合无效")
    if not isinstance(conditions, list) or len(conditions) > 12:
        raise ValueError("最多设置 12 个停止条件")
    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("停止条件格式无效")
        field, op, value = condition.get("field"), condition.get("op"), condition.get("value")
        if not isinstance(field, str) or field not in FIELDS:
            raise ValueError("未知条件字段")
        if not isinstance(op, str) or op not in ({"gte", "lte", "delta_gte", "eq"} if field in NUMERIC else {"eq", "changed"}):
            raise ValueError("条件运算符不适用")
        if field in NUMERIC:
            if not isinstance(value, (float, int)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("数值条件须为非负有限数字")
        elif op == "eq" and field == "map_complete":
            if value is not True:
                raise ValueError("地图完成条件的值须为 true")
        elif op == "eq" and (not isinstance(value, str) or not value or len(value) > 200):
            raise ValueError("条件值须为非空文字")
    if not text and not conditions:
        raise ValueError("请填写目标或停止条件；移除目标请使用清除")
    return {"text": text, "conditions": conditions, "match": match}


class GoalManager:
    def __init__(self, directory):
        self.path = directory / "goal.json"
        self.lock = threading.RLock()
        self.data = {"revision": 0, "active": False, "text": "", "conditions": [], "match": "all", "reached": False}
        if self.path.exists():
            self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
        self.stop_motion = threading.Event()
        self.immediate = False
        self.cancel_sent = False

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def set(self, data, values):
        spec = validate_goal(data)
        with self.lock:
            if self.stop_motion.is_set():
                raise ValueError("已经开始停止并保存，不能撤回")
            self.data = {**spec, "revision": self.data["revision"]+1, "active": True, "reached": False,
                         "baseline": dict(values), "updated_wall": time.time()}
            self._save()

    def clear(self):
        with self.lock:
            if self.stop_motion.is_set():
                raise ValueError("已经开始停止并保存，不能撤回")
            self.data = {"revision": self.data["revision"]+1, "active": False, "reached": False,
                         "text": "", "conditions": [], "match": "all", "updated_wall": time.time()}
            self._save()

    def stop_now(self):
        with self.lock:
            self.immediate = True
            self.stop_motion.set()
            self.data.update(active=False, reached=True, stop_reason="用户立即停止", updated_wall=time.time())
            self._save()

    def check(self, values):
        with self.lock:
            if self.stop_motion.is_set() or not self.data["active"] or not self.data["conditions"]:
                return self.stop_motion.is_set()
            baseline = self.data["baseline"]
            matches = []
            for condition in self.data["conditions"]:
                field, op = condition["field"], condition["op"]
                actual, initial = values.get(field), baseline.get(field)
                if field in {"room", "map_complete", "deaths"} and values.get("map") != baseline.get("map"):
                    actual = None
                if field == "room" and values.get("dead"):
                    actual = None
                value = condition.get("value")
                if actual is None:
                    result = False
                elif op == "changed":
                    result = initial is not None and actual != initial
                elif op == "delta_gte":
                    result = initial is not None and actual-initial >= value
                elif op == "gte":
                    result = actual >= value
                elif op == "lte":
                    result = actual <= value
                else:
                    result = actual == value
                matches.append(result)
            if (all(matches) if self.data["match"] == "all" else any(matches)):
                self.data.update(active=False, reached=True, stop_reason="自定义停止条件已满足", updated_wall=time.time())
                self.stop_motion.set()
                self._save()
            return self.stop_motion.is_set()
