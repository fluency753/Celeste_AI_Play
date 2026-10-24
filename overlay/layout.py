"""桌面 HUD 独立窗口布局；保留旧比例布局的读取以便一次性迁移。"""
import json
import math
from pathlib import Path
from .placement import MIN_HEIGHT, DEFAULT_HEIGHT

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "configs/local.hud-window.json"


class Layout:
    def __init__(self, path=DEFAULT_PATH):
        self.path = path
        self.window = None
        self.topmost = True
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.topmost = data.get('topmost', True) is True
            if data.get('mode') == 'window' and all(
                isinstance(data.get(k), (int, float)) and math.isfinite(data[k]) for k in ('x','y','width','height')
            ) and data['width'] > 0 and data['height'] > 0:
                self.window = data
            self.custom = data if data.get("mode") == "custom" and all(
                isinstance(data.get(k), (int, float)) and math.isfinite(data[k]) for k in ("x", "y", "width", "height")
            ) and data["width"] > 0 and data["height"] > 0 else None
        except (OSError, ValueError, AttributeError):
            self.custom = None

    def window_target(self, work):
        left, top, right, bottom = work
        if self.window:
            x,y,width,height = (round(self.window[k]) for k in ('x','y','width','height'))
        elif self.custom:
            # 旧文件只有游戏宽度；迁移使用已保存的参照值，不查找或依赖当前游戏。
            c=self.custom; reference=c.get('game_width',1920)
            reference=reference if isinstance(reference,(int,float)) and math.isfinite(reference) and reference>0 else 1920
            x,y=left+round(c['x']*reference),top+round(c['y']*reference*9/16)
            width,height=round(c['width']*reference),round(c['height']*reference*9/16)
        else:
            width,height=420,DEFAULT_HEIGHT
            x,y=right-width-24,top+40
        width,height=min(max(200,width),right-left),min(max(MIN_HEIGHT,height),bottom-top)
        return max(left,min(x,right-width)),max(top,min(y,bottom-height)),width,height

    def save_window(self, rect, topmost):
        x,y,width,height=rect
        if width<=0 or height<=0:
            return
        self.window={'mode':'window','x':x,'y':y,'width':width,'height':height,'topmost':bool(topmost)}
        self.topmost=bool(topmost)
        self.custom=None
        self._write(self.window)

    def target(self, game, width_ratio, margin_ratio, height_ratio, work=None):
        gx, gy, gw, gh = game
        if self.custom:
            c = self.custom
            x, y = gx + round(c["x"] * gw), gy + round(c["y"] * gh)
            width, height = max(200, round(c["width"] * gw)), max(260, round(c["height"] * gh))
        else:
            width = max(200, round(gw * width_ratio))
            height = max(260, round(width * height_ratio))
            margin = round(gw * margin_ratio)
            x, y = gx + gw - margin - width, gy + margin
        if work:
            width, height = min(width, work[2] - work[0]), min(height, work[3] - work[1])
            x = max(work[0], min(x, work[2] - width))
            y = max(work[1], min(y, work[3] - height))
        return x, y, width, height

    def save(self, rect, game):
        x, y, width, height = rect
        gx, gy, gw, gh = game
        if gw <= 0 or gh <= 0:
            return
        self.custom = {"mode": "custom", "x": (x - gx) / gw, "y": (y - gy) / gh,
                       "width": width / gw, "height": height / gh, "game_width": gw}
        self._write(self.custom)

    def reset(self):
        self.custom = None
        self.window = None
        self._write({"mode": "auto", "topmost":self.topmost})

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.path)
