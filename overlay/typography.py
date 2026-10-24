"""独立于窗口大小的字号设置。"""
import json
import threading
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "configs/local.hud-font.json"
_lock = threading.Lock()


def load_font(path=PATH):
    try:
        scale = float(json.loads(path.read_text(encoding="utf-8"))["font_scale"])
        if .6 <= scale <= 1.8:
            return scale
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return 1.0


def change_font(direction, path=PATH):
    with _lock:
        scale = round(max(.6, min(1.8, load_font(path) + (.05 if direction > 0 else -.05))), 2)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"font_scale": scale}), encoding="utf-8")
        temp.replace(path)
        return scale
