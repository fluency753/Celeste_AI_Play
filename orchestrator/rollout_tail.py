"""Read append-only rollout logs without consuming incomplete records or changing writers' files."""
from pathlib import Path


class RecordTail:
    def __init__(self, magic, header):
        self.magic, self.header = magic, header
        self.offsets = {}
        self.next_path = None

    def read(self, paths, budget=8 * 1024 * 1024, on_skip=None, max_record_bytes=None):
        sources = []
        paths = [Path(path) for path in paths]
        if budget < len(self.magic) + self.header.size:
            raise ValueError('rollout read budget must fit a record header and magic')
        remaining = budget
        record_limit = budget if max_record_bytes is None else max_record_bytes
        allowance = max(65536, budget // max(1, len(paths)))
        start = paths.index(self.next_path) if self.next_path in paths else 0
        for turn in range(len(paths)):
            index = (start + turn) % len(paths)
            self.next_path = paths[(index + 1) % len(paths)]
            path = paths[index]
            deferred = False
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
                        # A record may exceed its fair share, but all returned
                        # blocks (including magic) must fit the whole-call budget.
                        if size + len(self.magic) > record_limit:
                            if on_skip:
                                on_skip(f'[playback] 增量预览略过超出 {record_limit} 字节上限的单条路线：{path.name}，{size} 字节；原日志保留用于完整计数')
                            stream.seek(size - self.header.size, 1)
                        else:
                            needed = size + (0 if records else len(self.magic))
                            if needed > remaining:
                                deferred = True
                                break
                            body = stream.read(size - self.header.size)
                            if len(body) != size - self.header.size:
                                break
                            records.append(header + body)
                            remaining -= needed
                        consumed += size
                self.offsets[path] = (identity, offset + consumed)
                if records:
                    sources.append(self.magic + b''.join(records))
                if deferred:
                    # Start with this unread file next time, rather than letting
                    # earlier active writers consume its budget on every poll.
                    self.next_path = path
                    break
            except OSError:
                continue
        return sources
