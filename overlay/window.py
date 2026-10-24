"""置顶 HUD 窗口（Windows）：把 HUD 页面放进一个透明、无边框、置顶、点击穿透、不抢焦点的小窗口，
贴在游戏窗口客户区的右上角，随游戏窗口移动与缩放。

用法（仓库根目录；先启动主控或 `python -m overlay.server`）：
    python -m overlay.window
    python -m overlay.window --width-ratio 0.182 --margin 0.02

主控运行时会自动启动它（--no-hud-window 关闭）。游戏窗口按进程名 Celeste.exe 查找（--game-title 可改成按标题找），
不会认错成同名的资源管理器窗口。

需要 pywebview（pip install pywebview，使用系统自带的 WebView2）。游戏需运行在窗口化或无边框全屏模式。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x2
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

HUD_TITLE = "Celeste AI HUD"
HEIGHT_RATIO = 1.08   # 窗口高 / 宽：卡片最高约占 1.04（统计五行、思考语句五到六行时），余下透明


def _user32():
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.FindWindowW.restype = wt.HWND
    user32.IsWindow.argtypes = [wt.HWND]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    user32.GetForegroundWindow.restype = wt.HWND
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    return user32


def set_dpi_awareness() -> None:
    """按物理像素定位，避免高 DPI 下窗口位置与大小被系统缩放。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # 每显示器 V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()


_game_hwnd = {"value": None}


def find_game(user32, title: str | None):
    """游戏窗口：给了标题就按标题找；否则按进程名 Celeste.exe 找（找到后缓存，窗口还在就不再枚举）。"""
    if title:
        return user32.FindWindowW(None, title)
    hwnd = _game_hwnd["value"]
    if hwnd and user32.IsWindow(hwnd):
        return hwnd
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from orchestrator.gamewindow import find_game_window  # noqa: WPS433
    _game_hwnd["value"] = find_game_window()
    return _game_hwnd["value"]


def game_client_rect(user32, title: str | None):
    """游戏窗口客户区在屏幕上的位置与大小（物理像素）；找不到或最小化时返回 None。"""
    hwnd = find_game(user32, title)
    if not hwnd or user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
        return None
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, rect.right - rect.left, rect.bottom - rect.top


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitor_work_area(user32, title: str | None):
    """游戏窗口所在显示器的工作区（物理像素）。"""
    hwnd = find_game(user32, title)
    if not hwnd:
        return None
    user32.MonitorFromWindow.restype = wt.HMONITOR
    monitor = user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    r = info.rcWork
    return r.left, r.top, r.right, r.bottom


class DWM_BLURBEHIND(ctypes.Structure):
    _fields_ = [("dwFlags", wt.DWORD), ("fEnable", wt.BOOL), ("hRgnBlur", wt.HRGN), ("fTransitionOnMaximized", wt.BOOL)]


def per_pixel_alpha(window, hwnd) -> None:
    """让窗口按像素透明：页面透明的地方露出游戏，卡片按自己的半透明底色叠在游戏上。

    pywebview 的 transparent 在 Windows 上只把网页背景设成透明，透出来的是窗体自己的底色（浅灰），
    所以卡片四周有白边、卡片也不是真的半透明。这里把窗体底色改成黑色（GDI 画出的黑色 alpha 为 0），
    再用一个空区域打开 DWM 的"背后模糊"——不会真的模糊，只是让 DWM 按像素的 alpha 合成窗口。
    """
    try:
        import clr  # noqa: F401（pywebview 已加载 pythonnet）
        from System import Action
        from System.Drawing import Color
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "BackColor", Color.Black)))
    except Exception as exc:  # noqa: BLE001
        print(f"设置窗体底色失败：{exc}", flush=True)
    try:
        gdi32 = ctypes.windll.gdi32
        gdi32.CreateRectRgn.restype = wt.HRGN
        gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
        region = gdi32.CreateRectRgn(0, 0, -1, -1)
        blur = DWM_BLURBEHIND(0x1 | 0x2, True, region, False)   # DWM_BB_ENABLE | DWM_BB_BLURREGION
        result = ctypes.windll.dwmapi.DwmEnableBlurBehindWindow(wt.HWND(hwnd), ctypes.byref(blur))
        gdi32.DeleteObject(region)
        if result != 0:
            print(f"DwmEnableBlurBehindWindow 失败：0x{result & 0xFFFFFFFF:08X}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"开启按像素透明失败：{exc}", flush=True)


def make_overlay(user32, hwnd) -> None:
    """点击穿透、不激活、不进任务栏、置顶。"""
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    layered = bool(style & WS_EX_LAYERED)
    style = (style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED) & ~WS_EX_APPWINDOW
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style)
    if not layered:
        # 新加的分层样式必须设一次透明度，否则窗口不显示；整体不透明度 255，按像素的透明由 per_pixel_alpha 负责
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, LWA_ALPHA)


def follow(window, args) -> None:
    """等窗口出现后改成覆盖层样式，然后一直贴着游戏窗口右上角。"""
    user32 = _user32()
    window.events.shown.wait(30)
    hwnd = None
    for _ in range(100):
        hwnd = user32.FindWindowW(None, HUD_TITLE)
        if hwnd:
            break
        time.sleep(0.1)
    if not hwnd:
        print("找不到 HUD 窗口", flush=True)
        return
    per_pixel_alpha(window, hwnd)
    make_overlay(user32, hwnd)
    # pywebview 在页面开始加载时会激活一次窗口，WebView2 获得焦点也可能稍晚发生：
    # 启动后的一段时间里只要发现自己成了前台窗口，就把焦点还给启动前的窗口
    settle_until = time.monotonic() + 15.0
    last = None
    hidden = False
    while True:
        if args.previous_foreground and time.monotonic() < settle_until and user32.GetForegroundWindow() == hwnd:
            user32.SetForegroundWindow(args.previous_foreground)
        rect = game_client_rect(user32, args.game_title)
        if rect is None:
            if not hidden:
                user32.ShowWindow(hwnd, SW_HIDE)
                hidden = True
        else:
            gx, gy, gw, gh = rect
            width = max(160, round(gw * args.width_ratio))
            height = round(width * HEIGHT_RATIO)
            margin = round(gw * args.margin)
            x, y = gx + gw - margin - width, gy + margin
            work = monitor_work_area(user32, args.game_title)
            if work is not None:
                # 游戏窗口有一部分在屏幕外时，把 HUD 挪回屏幕内，免得被切掉
                x = max(work[0], min(x, work[2] - width))
                y = max(work[1], min(y, work[3] - height))
            target = (x, y, width, height)
            if target != last or hidden:
                user32.SetWindowPos(hwnd, HWND_TOPMOST, *target, SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER)
                last, hidden = target, False
            else:
                # 别的置顶窗口可能盖上来：定期把自己放回最上层（不激活）
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOACTIVATE | 0x0001 | 0x0002)
        time.sleep(args.interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:47790/hud")
    ap.add_argument("--game-title", default=None, help="按窗口标题找游戏窗口（默认按进程名 Celeste.exe 找）")
    ap.add_argument("--width-ratio", type=float, default=0.182, help="HUD 宽度占游戏窗口宽度的比例（1080p 下 350 像素）")
    ap.add_argument("--margin", type=float, default=0.02, help="距游戏窗口右上角的边距（占游戏窗口宽度的比例）")
    ap.add_argument("--interval", type=float, default=0.25, help="跟随游戏窗口的检查间隔（秒）")
    args = ap.parse_args()
    if sys.platform != "win32":
        raise SystemExit("置顶窗口只支持 Windows；其他系统请在 OBS 或浏览器里打开 HUD 页面")
    set_dpi_awareness()
    import urllib.request
    import webview  # noqa: WPS433（只在 Windows 上需要）

    # HUD 服务还没起来时页面会加载失败且不会自动重试：先等服务就绪
    waited = False
    while True:
        try:
            urllib.request.urlopen(args.url, timeout=2).close()
            break
        except OSError:
            if not waited:
                print(f"等待 HUD 服务 {args.url} ……（启动主控或 python -m overlay.server）", flush=True)
                waited = True
            time.sleep(1)

    args.previous_foreground = _user32().GetForegroundWindow()
    window = webview.create_window(HUD_TITLE, args.url, width=420, height=336, frameless=True, transparent=True,
                                   on_top=True, focus=False, resizable=False, easy_drag=False, shadow=False,
                                   text_select=False, zoomable=False)
    webview.start(follow, (window, args), gui="edgechromium")
    return 0


if __name__ == "__main__":
    sys.exit(main())
