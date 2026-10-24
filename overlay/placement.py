"""无边框 HUD 的移动与缩放；只操作所属窗口，不注入键盘鼠标输入。"""
import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time
from .typography import change_font

MIN_HEIGHT = 260
DEFAULT_HEIGHT = 760

def resized_rect(rect, dx, dy, region):
    x, y, width, height = rect
    if region == "move":
        return x + dx, y + dy, width, height
    if "e" in region:
        width = max(200, width + dx)
    if "s" in region:
        height = max(MIN_HEIGHT, height + dy)
    if "w" in region:
        next_width = max(200, width - dx)
        x, width = x + width - next_width, next_width
    if "n" in region:
        next_height = max(MIN_HEIGHT, height - dy)
        y, height = y + height - next_height, next_height
    return x, y, width, height


class PlacementAPI:
    def __init__(self):
        self._commands = queue.SimpleQueue()
        self._enabled, self._dragging = False, False
        self._topmost = True
        self._window, self._user32, self._hwnd = None, None, None

    def _sync_page(self, *_):
        if self._window:
            self._window.run_js("document.documentElement.classList.toggle('hud-desktop', " +
                                ("true" if self._enabled else "false") + ");" +
                                "window.dispatchEvent(new CustomEvent('hud-window-state',{detail:{topmost:" +
                                ('true' if self._topmost else 'false') + "}}));")

    def _enable(self, enabled, hwnd):
        self._enabled, self._hwnd = enabled, hwnd
        self._sync_page()

    def drag_region(self, region):
        if not self._enabled or self._dragging or region not in {"move", "n", "s", "e", "w", "ne", "nw", "se", "sw"}:
            return False
        self._dragging = True
        threading.Thread(target=self._drag, args=(region,), daemon=True).start()
        return True

    def change_font(self, direction):
        if not self._enabled or direction not in (-1, 1):
            return None
        return change_font(direction)

    def window_action(self, action):
        if not self._enabled or action not in {'minimize','close','pin'}:
            return False
        self._commands.put(action)
        return True


    def _drag(self, region):
        try:
            user32, hwnd = self._user32, self._hwnd
            rect, start = wt.RECT(), wt.POINT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)) or not user32.GetCursorPos(ctypes.byref(start)):
                return
            original = rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
            while self._enabled and user32.IsWindow(hwnd) and user32.GetAsyncKeyState(1) & 0x8000:
                cursor = wt.POINT()
                user32.GetCursorPos(ctypes.byref(cursor))
                target = resized_rect(original, cursor.x - start.x, cursor.y - start.y, region)
                user32.SetWindowPos(hwnd, 0, *target, 0x0010 | 0x0040 | 0x0200 | 0x0004)  # 保留用户的置顶选择
                time.sleep(0.016)
        finally:
            self._dragging = False
