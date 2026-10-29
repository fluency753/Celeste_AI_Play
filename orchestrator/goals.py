"""可编辑的文字目标和可核验停止条件，不依赖模型口头宣称完成。"""
import json
import math
import threading
import time

FIELDS = {"room", "map", "side", "map_complete", "deaths", "frame", "elapsed", "game_elapsed"}
NUMERIC = {"deaths", "frame", "elapsed", "game_elapsed"}
LABELS = {'room':'房间','map':'地图','side':'面别','map_complete':'当前地图通关',
          'deaths':'本地图死亡次数','frame':'游戏推进帧数','elapsed':'现实运行时长（秒）','game_elapsed':'游戏推进时长（秒）'}


def describe_goal(spec):
    operators = {'changed':'发生变化','delta_gte':'从本次更新起增加至少','gte':'累计达到至少','lte':'不高于','eq':'等于'}
    conditions = [f"{LABELS[c['field']]}{operators[c['op']]}" + (f" {c['value']}" if c['op'] != 'changed' else '')
                  for c in spec.get('conditions', [])]
    return '；'.join(filter(None, [spec.get('text',''), ('任一条件：' if spec.get('match')=='any' else '全部条件：') + '，'.join(conditions) if conditions else '']))


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
        allowed = {"delta_gte"} if field=='game_elapsed' else {"gte", "lte", "delta_gte", "eq"} if field in NUMERIC else {"eq", "changed"}
        if not isinstance(op, str) or op not in allowed:
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
            if any(c['field']=='game_elapsed' for c in spec['conditions']):
                self.data['game_clock'] = {'frame':values.get('frame'), 'epoch':values.get('game_epoch'), 'advanced_frames':0}
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
            clock = self.data.get('game_clock')
            clock_changed = False
            if clock is not None and values.get('frame') is not None:
                frame, epoch = values['frame'], values.get('game_epoch')
                previous = clock.get('frame')
                if previous is None or epoch != clock.get('epoch'):
                    # A restarted game has a new frame origin, not negative play time.
                    clock.update(frame=frame, epoch=epoch)
                    clock_changed = True
                elif frame > previous:
                    clock['advanced_frames'] += frame-previous
                    clock['frame'] = frame
                    clock_changed = True
                values = {**values, 'game_elapsed':baseline.get('game_elapsed',0) + clock['advanced_frames']/60}
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
                    result = (clock['advanced_frames']/60 >= value if field=='game_elapsed' and clock is not None
                              else initial is not None and actual-initial >= value)
                elif op == "gte":
                    result = actual >= value
                elif op == "lte":
                    result = actual <= value
                else:
                    result = actual == value
                matches.append(result)
            if (all(matches) if self.data["match"] == "all" else any(matches)):
                reached = '，'.join(LABELS[c['field']] for c, match in zip(self.data['conditions'],matches) if match)
                self.data.update(active=False, reached=True, stop_reason="自定义停止条件已满足：" + reached, updated_wall=time.time())
                self.stop_motion.set()
                self._save()
            elif clock_changed:
                self._save()
            return self.stop_motion.is_set()
