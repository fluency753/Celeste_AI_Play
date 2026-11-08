"""Private, disposable storage for already-sanitized export projections."""
import json
import math
from pathlib import Path
import tempfile
import weakref


def finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None, 1
    if isinstance(value, (dict, list)):
        result = {} if isinstance(value, dict) else []
        count = 0
        for key, item in value.items() if isinstance(value, dict) else enumerate(value):
            item, replaced = finite_tree(item)
            if isinstance(result, dict):
                result[key] = item
            else:
                result.append(item)
            count += replaced
        return result, count
    return value, 0


class ExportSpool:
    CHUNK_BYTES = 24 * 1024 * 1024

    def __init__(self):
        temporary = tempfile.TemporaryDirectory(prefix='celeste-export-')
        self.root = Path(temporary.name)
        handles = []
        def cleanup():
            for stream in handles:
                stream.close()
            temporary.cleanup()
        self._cleanup = weakref.finalize(self, cleanup)
        self._handles = handles
        self.chunks, self.bytes, self.records, self.nonfinite_replaced = [], 0, 0, 0

    def append(self, value):
        value, replaced = finite_tree(value)
        self.nonfinite_replaced += replaced
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        if not self.chunks or self.chunks[-1][1] + len(raw) > self.CHUNK_BYTES:
            if self._handles:
                self._handles[-1].close()
            self.chunks.append([self.root / f'{len(self.chunks):06d}.jsonl', 0])
            self._handles.append(self.chunks[-1][0].open('ab'))
        path, offset = self.chunks[-1]
        self._handles[-1].write(raw)
        self.chunks[-1][1] += len(raw)
        self.bytes += len(raw)
        self.records += 1
        return len(self.chunks) - 1, offset, len(raw)

    def read(self, reference):
        chunk, offset, size = reference
        if chunk == len(self.chunks) - 1:
            self._handles[-1].flush()
        with self.chunks[chunk][0].open('rb') as stream:
            stream.seek(offset)
            raw = stream.read(size)
        if len(raw) != size:
            raise ValueError('导出临时详情不完整')
        return json.loads(raw)

    def close(self):
        self._cleanup()


class SpoolRows:
    def __init__(self):
        self.store, self.references = ExportSpool(), []

    def append(self, value):
        self.references.append(self.store.append(value))

    def __len__(self):
        return len(self.references)

    def __iter__(self):
        for reference in self.references:
            yield self.store.read(reference)

    def close(self):
        self.store.close()


class SpilledDict(dict):
    """Keep ordinary dict/JSON access while large values live in private chunks."""
    def __init__(self, store, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.store, self.references = store, {}

    def spill(self, values):
        if not values:
            return
        reference = self.store.append(values)
        for key in values:
            super().__setitem__(key, None)
            self.references[key] = reference

    def __getitem__(self, key):
        if key in self.references:
            return self.store.read(self.references[key])[key]
        return super().__getitem__(key)

    def __iter__(self):
        # Disable dict-subclass copy fast paths which would copy None placeholders.
        return super().__iter__()

    def get(self, key, default=None):
        return self[key] if key in self else default

    def __setitem__(self, key, value):
        self.references.pop(key, None)
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def __delitem__(self, key):
        super().__delitem__(key)
        self.references.pop(key, None)

    def clear(self):
        super().clear()
        self.references.clear()

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def copy(self):
        return self.materialize()

    def values(self):
        return self.materialize().values()

    def popitem(self):
        if not self:
            raise KeyError('popitem(): dictionary is empty')
        key = next(reversed(self))
        return key, self.pop(key)

    def __or__(self, other):
        return self.materialize() | other

    def __ror__(self, other):
        return other | self.materialize()

    def __ior__(self, other):
        self.update(other)
        return self

    def __eq__(self, other):
        return self.materialize() == (other.materialize() if isinstance(other, SpilledDict) else other)

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return repr(self.materialize())

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        self.references.pop(key, None)
        super().pop(key)
        return value

    def materialize(self):
        values, loaded = dict(super().items()), {}
        for key, reference in self.references.items():
            if reference not in loaded:
                loaded[reference] = self.store.read(reference)
            values[key] = loaded[reference][key]
        return values

    def items(self):
        return self.materialize().items()
