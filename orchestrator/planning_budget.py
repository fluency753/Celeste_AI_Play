"""Host-owned allowance for preset simulation batches, independent of Shell access."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import threading


def room_rounds(base: int, bounds, scale: float = 2.0) -> int:
    try:
        width, height = float(bounds[2]), float(bounds[3])
    except (TypeError, IndexError, ValueError):
        return base
    if not all(math.isfinite(n) and n > 0 for n in (width, height)):
        return base
    exponent = math.log2(scale)
    return max(base, math.ceil(base * (width / 320) ** exponent * (height / 180) ** exponent - 1e-9))


def _atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix=f'.{path.stem}-',
                                         suffix='.tmp', dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False)
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A post-replace cleanup failure is not a failed commit. It
                # must also never mask the original write/replace exception.
                pass


class PlanningBudget:
    """Only the private ledger authorizes execution; workspace files are projections.

    A reserved request is already charged. An interrupted or failed worker must
    not receive a second execution authorization when its request ID is retried.
    The caller stores actual results separately before marking them completed.
    """
    enabled = True

    def __init__(self, metrics, private_path: Path, state_path: Path | None = None,
                 rounds_per_room: int = 3, death_bonus: int = 1, room_scale: float = 2.0,
                 previous_path: Path | None = None, legacy: bool = False):
        if type(rounds_per_room) is not int or rounds_per_room < 1:
            raise ValueError('每个房间的规划轮次必须是正整数')
        if type(death_bonus) is not int or death_bonus < 0:
            raise ValueError('死亡补充轮次必须是非负整数')
        if not isinstance(room_scale, (int, float)) or not math.isfinite(room_scale) or room_scale < 1:
            raise ValueError('房间缩放系数必须是不小于1的有限数')
        self.metrics, self.private_path = metrics, Path(private_path)
        self.state_path = Path(state_path) if state_path is not None else None
        self.rounds_per_room, self.death_bonus, self.room_scale = rounds_per_room, death_bonus, room_scale
        self.lock = threading.RLock()
        source = self.private_path if self.private_path.exists() else previous_path
        if source is not None and Path(source).exists():
            self._data = self._read(Path(source))
        else:
            self._data = {'version': 1, 'bank': 0, 'used': 0, 'chapter': None, 'visited': [],
                          'last_deposit': 0, 'started': False, 'migration_required': bool(legacy),
                          'receipts': {}}
        # Copy a prior run's authoritative ledger before any execution is possible.
        _atomic_json(self.private_path, self._data)
        self._publish()

    @staticmethod
    def _read(path):
        data = json.loads(path.read_text(encoding='utf-8'))
        valid = isinstance(data, dict) and data.get('version') == 1
        valid = valid and all(type(data.get(k)) is int and data[k] >= 0
                              for k in ('bank', 'used', 'last_deposit'))
        valid = valid and (data.get('chapter') is None or isinstance(data.get('chapter'), str))
        valid = valid and isinstance(data.get('visited'), list) and all(isinstance(r, str) for r in data['visited'])
        valid = valid and all(type(data.get(k)) is bool for k in ('started', 'migration_required'))
        valid = valid and isinstance(data.get('receipts'), dict)
        if valid:
            valid = all(isinstance(key, str) and isinstance(row, dict) and row.get('id') == key
                        and isinstance(row.get('digest'), str) and row.get('cost') == 1
                        and row.get('status') in {'reserved', 'completed', 'failed', 'refunded'}
                        for key, row in data['receipts'].items())
        if not valid:
            raise ValueError('主控规划账本无效，未重置额度；请保留原账本并检查运行记录')
        return data

    @property
    def bank(self):
        with self.lock:
            return self._data['bank']

    @property
    def rounds_used(self):
        with self.lock:
            return self._data['used']

    @property
    def started(self):
        with self.lock:
            return self._data['started']

    def snapshot(self):
        with self.lock:
            return {'limited': True, 'left': self.bank, 'used': self.rounds_used,
                    'this_room_deposit': self._data['last_deposit'], 'per_screen': self.rounds_per_room,
                    'room_scale': self.room_scale, 'death_bonus': self.death_bonus, 'sim_only': True,
                    'chapter': self._data['chapter'], 'rooms_seen': len(self._data['visited']),
                    'migration_required': self._data['migration_required'], 'unit': 'prediction_batch'}

    def _publish(self):
        """Publication can fail after commit; every later operation retries it."""
        try:
            self.metrics.update(plan_rounds_left=self.bank, plan_rounds_used=self.rounds_used)
        except OSError:
            pass
        if self.state_path is not None:
            try:
                _atomic_json(self.state_path, self.snapshot())
            except OSError:
                pass

    def _commit(self, data):
        # Keep memory unchanged until the authoritative write has succeeded.
        _atomic_json(self.private_path, data)
        self._data = data
        self._publish()

    def start(self):
        with self.lock:
            if not self.started:
                data = deepcopy(self._data)
                data['started'] = True
                self._commit(data)
            else:
                self._publish()

    def on_room(self, chapter: str | None, room: str | None, bounds=None) -> bool:
        if not room:
            return False
        if not isinstance(room, str) or chapter is not None and not isinstance(chapter, str):
            raise ValueError('规划账本房间与章节标识必须是文本')
        with self.lock:
            data = deepcopy(self._data)
            if chapter != data['chapter']:
                data.update(chapter=chapter, visited=[], bank=0)
            if room in data['visited']:
                self._publish()
                return False
            data['visited'].append(room)
            # A legacy run has no trusted room history. Its current room must
            # not become a fresh grant just because the host has been restarted.
            migrated = data['migration_required']
            data['migration_required'] = False
            data['last_deposit'] = 0 if migrated else room_rounds(self.rounds_per_room, bounds, self.room_scale)
            data['bank'] += data['last_deposit']
            self._commit(data)
            return not migrated

    def on_death(self, count: int = 1):
        if type(count) is not int or count < 0:
            raise ValueError('死亡补充次数必须是非负整数')
        with self.lock:
            if count:
                data = deepcopy(self._data)
                data['bank'] += self.death_bonus * count
                self._commit(data)
            else:
                self._publish()

    def _receipt(self, request_id, digest):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('模拟请求编号须为1到128个字符')
        if not isinstance(digest, str) or not 1 <= len(digest) <= 256:
            raise ValueError('模拟请求指纹无效')
        receipt = self._data['receipts'].get(request_id)
        if receipt is not None and receipt['digest'] != digest:
            raise ValueError('模拟请求编号已用于不同内容，本次未执行')
        return receipt

    def receipt(self, request_id: str, digest: str):
        """Look up a retry before taking another game snapshot or starting work."""
        with self.lock:
            return deepcopy(self._receipt(request_id, digest))

    def reserve(self, request_id: str, digest: str):
        with self.lock:
            receipt = self._receipt(request_id, digest)
            if receipt is not None:
                self._publish()
                return deepcopy(receipt), False
            if self.bank <= 0:
                raise ValueError(self.deny_reason())
            data = deepcopy(self._data)
            receipt = {'id': request_id, 'digest': digest, 'status': 'reserved', 'cost': 1,
                       'chapter': data['chapter']}
            data['receipts'][request_id] = receipt
            data['bank'] -= 1
            data['used'] += 1
            self._commit(data)
            return deepcopy(receipt), True

    def finish(self, request_id: str, status='completed'):
        if status not in {'completed', 'failed'}:
            raise ValueError('模拟回执终态无效')
        with self.lock:
            receipt = self._data['receipts'].get(request_id)
            if receipt is None:
                raise ValueError('找不到主控模拟请求回执')
            if receipt['status'] != 'reserved':
                self._publish()
                return deepcopy(receipt)
            data = deepcopy(self._data)
            data['receipts'][request_id]['status'] = status
            self._commit(data)
            return deepcopy(data['receipts'][request_id])

    def refund(self, request_id: str):
        """Return a reservation only when the caller proved the worker never ran.

        Ambiguous submission, timeout and cancellation must stay charged. A
        late refund from an old chapter cannot grant allowance in a new one.
        A refunded ID remains consumed and never authorizes a fresh execution.
        """
        with self.lock:
            receipt = self._data['receipts'].get(request_id)
            if receipt is None:
                raise ValueError('找不到主控模拟请求回执')
            if receipt['status'] != 'reserved':
                self._publish()
                return deepcopy(receipt)
            data = deepcopy(self._data)
            data['receipts'][request_id]['status'] = 'refunded'
            if receipt['chapter'] == data['chapter']:
                data['bank'] += receipt['cost']
            data['used'] -= 1
            self._commit(data)
            return deepcopy(data['receipts'][request_id])

    def status_line(self):
        return (f'规划轮次：剩余 {self.bank} 轮；一次有效批量模拟请求消耗一轮。'
                '额度由主控执行前核验，文件和普通程序不受此额度限制。')

    def deny_reason(self, **_):
        return ('模拟规划额度已用完，本次未执行模拟。文件和普通程序仍可使用；'
                '依据已有结果处理当前任务，保持用户指定的试玩状态。')
