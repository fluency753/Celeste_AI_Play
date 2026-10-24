"""Route-independent checks for the recorder's float32 stereo WAV output."""
import math
from pathlib import Path
import struct


# Linear full-scale amplitude (-120 dBFS), separate from onset calibration.
SILENCE_THRESHOLD = 1e-6


def _stereo_frames(path):
    """Read bounded chunks; do not load an entire long recording into memory."""
    with Path(path).open('rb') as stream:
        header = stream.read(12)
        if len(header) != 12 or header[:4] != b'RIFF' or header[8:] != b'WAVE':
            raise ValueError('需要 RIFF/WAVE 格式的录制音频')
        format_info = None
        while chunk := stream.read(8):
            if len(chunk) != 8:
                raise ValueError('WAV 块头不完整')
            kind, size = struct.unpack('<4sI', chunk)
            start = stream.tell()
            if kind == b'fmt ':
                raw = stream.read(min(size, 16))
                if len(raw) != 16:
                    raise ValueError('WAV 格式信息不完整')
                format_info = struct.unpack('<HHIIHH', raw)
            elif kind == b'data':
                if (format_info is None or format_info[0] != 3 or format_info[1] != 2
                        or format_info[4:] != (8, 32)):
                    raise ValueError('需要录制器输出的 32 位浮点立体声 WAV')
                if size % 8:
                    raise ValueError('WAV 音频块未按立体声采样对齐')
                remaining = size
                while remaining:
                    block = stream.read(min(remaining, 65536 * 8))
                    if not block or len(block) % 8:
                        raise ValueError('WAV 音频块被截断')
                    remaining -= len(block)
                    yield from struct.iter_unpack('<ff', block)
                return
            stream.seek(start + size + (size & 1))
        raise ValueError('WAV 缺少音频 data 块')


def wav_dropouts(path: Path, silence_threshold: float = SILENCE_THRESHOLD) -> list[int]:
    """Find isolated stereo zeros surrounded by signal above the silence floor.

    Either channel can supply the surrounding signal, but it must be the same
    channel on both sides. Silence, fade tails and floating-point residue alone
    do not establish a dropout. These are candidates, not an audibility proof.
    """
    if not math.isfinite(silence_threshold) or silence_threshold < 0:
        raise ValueError('静音阈值必须是有限的非负振幅')
    previous = center = None
    drops = []
    for index, following in enumerate(_stereo_frames(path)):
        if (previous is not None and center == (0.0, 0.0)
                and any(abs(previous[c]) > silence_threshold and abs(following[c]) > silence_threshold
                        for c in (0, 1))):
            drops.append(index - 1)
        previous, center = center, following
    return drops
