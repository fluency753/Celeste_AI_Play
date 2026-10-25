"""Read append-only rollout logs without consuming incomplete records or changing writers' files."""
from pathlib import Path


class RecordTail:
    def __init__(self, magic, header):
        self.magic, self.header = magic, header
        self.offsets = {}

    def read(self, paths, budget=8 * 1024 * 1024, on_skip=None):
        sources = []
        paths = list(paths)
        allowance = max(65536, budget // max(1, len(paths)))
        for path in paths:
            path = Path(path)
            try:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                previous, offset = self.offsets.get(path, (None, len(self.magic)))
                if previous != identity or stat.st_size < offset:
                    offset = len(self.magic)
                with path.open('rb') as stream:
                    if stream.read(len(self.magic)) != self.magic:
                        continue
                    stream.seek(offset)
                    records, consumed = [], 0
                    while consumed < allowance:
                        header = stream.read(self.header.size)
                        if len(header) < self.header.size:
                            break
                        count = self.header.unpack(header)[3]
                        size = self.header.size + 4 * max(0, count - 1)
                        if count <= 0 or offset + consumed + size > stat.st_size:
                            break
                        # The fair-share window is soft: a complete record must
                        # make progress even when it is larger than that window.
                        # Keep the per-record memory limit explicit and bounded.
                        if size > budget:
                            if on_skip:
                                on_skip(f'[playback] 增量预览略过超出 {budget} 字节上限的单条路线：{path.name}，{size} 字节；原日志保留用于完整计数')
                            stream.seek(size - self.header.size, 1)
                        else:
                            body = stream.read(size - self.header.size)
                            if len(body) != size - self.header.size:
                                break
                            records.append(header + body)
                        consumed += size
                self.offsets[path] = (identity, offset + consumed)
                if records:
                    sources.append(self.magic + b''.join(records))
            except OSError:
                continue
        return sources
