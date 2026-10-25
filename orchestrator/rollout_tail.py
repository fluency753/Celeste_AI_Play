"""Read append-only rollout logs without consuming incomplete records or changing writers' files."""
from pathlib import Path


class RecordTail:
    def __init__(self, magic, header):
        self.magic, self.header = magic, header
        self.offsets = {}

    def read(self, paths, budget=8 * 1024 * 1024):
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
                    data = stream.read(allowance)
                end = 0
                while end + self.header.size <= len(data):
                    count = self.header.unpack_from(data, end)[3]
                    size = self.header.size + 4 * max(0, count - 1)
                    if count <= 0 or end + size > len(data):
                        break
                    end += size
                self.offsets[path] = (identity, offset + end)
                if end:
                    sources.append(self.magic + data[:end])
            except OSError:
                continue
        return sources
