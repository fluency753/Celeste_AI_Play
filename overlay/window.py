"""独立 HUD 窗口（Windows）：无边框，可拖动、缩放、最小化、关闭和切换置顶。
位置与大小独立保存；不根据游戏窗口的位置或可见性改变显示。

用法（仓库根目录；先启动主控或 `python -m overlay.server`）：
    python -m overlay.window

主控运行时会自动启动它（--no-hud-window 关闭）。最小化到托盘，任务栏保持隐藏；双击红色 AI 图标恢复窗口。

需要 pywebview（pip install pywebview，使用系统自带的 WebView2）。
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from .layout import Layout
from .placement import PlacementAPI, MIN_HEIGHT, DEFAULT_HEIGHT

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
HEIGHT_RATIO = 1.45   # 窗口高 / 宽：卡片最高约占 1.43（调用工具折成三行、思考语句六行、回放说明两行时），余下透明


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
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
    user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = ctypes.c_short
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


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def window_work_area(user32, hwnd):
    """HUD 所在显示器的工作区，与游戏是否存在、是否可见无关。"""
    user32.MonitorFromWindow.restype = wt.HMONITOR
    monitor = user32.MonitorFromWindow(hwnd, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        r = info.rcWork
        return r.left, r.top, r.right, r.bottom
    return 0, 0, max(200, user32.GetSystemMetrics(0)), max(260, user32.GetSystemMetrics(1))


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


def initialize_style(window, user32):
    """初始化无边框及隐藏任务栏条目，不再提供旧点击穿透模式。"""
    from System import Action
    from System.Windows.Forms import FormBorderStyle

    handles = []
    def apply():
        form = window.native
        form.ControlBox = False
        form.FormBorderStyle = getattr(FormBorderStyle, "None")
        handles.append(int(form.Handle.ToInt64()))
    window.native.Invoke(Action(apply))
    hwnd = handles[0]
    # 不改 ShowInTaskbar 属性：它会重建 HWND，打断 WebView2 初始化。
    user32.ShowWindow(hwnd, SW_HIDE)
    style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                            (style | WS_EX_TOOLWINDOW) & ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_APPWINDOW))
    user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    return hwnd


def client_rect(user32, hwnd):
    r, origin = wt.RECT(), wt.POINT(0, 0)
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return None
    user32.ClientToScreen(hwnd, ctypes.byref(origin))
    return origin.x, origin.y, r.right - r.left, r.bottom - r.top


def make_tray_icon():
    """Owned, opaque high-contrast icon; no shared SystemIcons handle is destroyed."""
    from System.Drawing import Bitmap, Color, Font, FontStyle, Graphics, GraphicsUnit, Icon, RectangleF, SolidBrush, StringAlignment, StringFormat
    from System.Drawing.Drawing2D import SmoothingMode
    bitmap = Bitmap(32, 32)
    graphics = Graphics.FromImage(bitmap)
    font = Font('Segoe UI', 19, FontStyle.Bold, GraphicsUnit.Pixel)
    brush = SolidBrush(Color.White)
    format_ = StringFormat()
    handle = None
    try:
        graphics.SmoothingMode = SmoothingMode.AntiAlias
        graphics.Clear(Color.FromArgb(224, 71, 91))
        format_.Alignment = StringAlignment.Center
        format_.LineAlignment = StringAlignment.Center
        graphics.DrawString('AI', font, brush, RectangleF(0, 0, 32, 32), format_)
        handle = bitmap.GetHicon()
        borrowed = Icon.FromHandle(handle)
        try:
            return borrowed.Clone()
        finally:
            borrowed.Dispose()
    finally:
        if handle is not None:
            destroy = ctypes.windll.user32.DestroyIcon
            destroy.argtypes = [wt.HICON]
            destroy(wt.HICON(int(handle.ToInt64())))
        format_.Dispose()
        brush.Dispose()
        font.Dispose()
        graphics.Dispose()
        bitmap.Dispose()


class TrayControl:
    """任务栏隐藏时，托盘提供恢复、最小化、置顶和关闭入口。"""
    def __init__(self, window, placement):
        from System import Action
        from System.Drawing import Color, SystemIcons
        from System.Windows.Forms import NotifyIcon, ContextMenuStrip, ToolStripMenuItem
        self.window, self.commands = window, placement._commands

        def create():
            self.menu = ContextMenuStrip()
            self.menu.BackColor = Color.FromArgb(22, 26, 40)
            self.menu.ForeColor = Color.White
            self.pin = ToolStripMenuItem("置顶")
            self.pin.Checked = placement._topmost
            for label, command in (("显示 HUD", "restore"), ("最小化", "minimize")):
                item = ToolStripMenuItem(label)
                item.Click += lambda *_, command=command: self.commands.put(command)
                self.menu.Items.Add(item)
            self.pin.Click += lambda *_: self.commands.put("pin")
            self.menu.Items.Add(self.pin)
            for label, command in (("恢复默认位置和大小", "reset"), ("关闭桌面 HUD", "close")):
                item = ToolStripMenuItem(label)
                item.Click += lambda *_, command=command: self.commands.put(command)
                self.menu.Items.Add(item)
            self.icon = NotifyIcon()
            try:
                self.tray_icon = make_tray_icon()
            except Exception as exc:
                print(f'[hud] 托盘图标绘制失败：{exc}', flush=True)
                self.tray_icon = SystemIcons.Application.Clone()
            self.icon.Icon = self.tray_icon
            self.icon.Text = "Celeste AI HUD · 双击显示 · 右键菜单"
            self.icon.ContextMenuStrip = self.menu
            self.icon.DoubleClick += lambda *_: self.commands.put("restore")
            self.icon.Visible = True
            def dispose(*_):
                self.icon.Visible = False
                self.icon.Dispose()
                self.tray_icon.Dispose()
                self.menu.Dispose()
            window.native.FormClosed += dispose
        window.native.Invoke(Action(create))
        print("[hud] 托盘：双击恢复窗口；菜单可最小化、置顶、恢复布局或关闭", flush=True)

    def set_topmost(self, value):
        from System import Action
        self.window.native.Invoke(Action(lambda: setattr(self.pin, 'Checked', value)))


def manage(window, args) -> None:
    """独立窗口：只保存自身的正常位置，不根据游戏执行显隐或置顶。"""
    user32 = _user32()
    window.events.shown.wait(30)
    window.events.loaded.wait(30)
    hwnd = initialize_style(window, user32)
    per_pixel_alpha(window, hwnd)
    placement = args.placement
    placement._user32 = user32
    layout = Layout()
    placement._topmost = layout.topmost
    window.on_top = layout.topmost
    placement._enable(True, hwnd)
    target = layout.window_target(window_work_area(user32, hwnd))
    user32.SetWindowPos(hwnd, 0, *target, SWP_NOACTIVATE | SWP_NOOWNERZORDER | 0x0004)
    saved_rect = client_rect(user32, hwnd) or target
    layout.save_window(saved_rect, placement._topmost)
    tray = TrayControl(window, placement)
    hidden = False
    saved_at = 0.0
    print("[hud] 独立窗口已就绪：显示、最小化和置顶均由用户控制", flush=True)
    while user32.IsWindow(hwnd):
        while not placement._commands.empty():
            command = placement._commands.get_nowait()
            if command == 'close':
                if not hidden and not user32.IsIconic(hwnd):
                    current = client_rect(user32, hwnd)
                    if current:
                        layout.save_window(current, placement._topmost)
                window.destroy()
                return
            elif command == 'minimize':
                if not hidden and not user32.IsIconic(hwnd):
                    current = client_rect(user32, hwnd)
                    if current:
                        layout.save_window(current, placement._topmost)
                # A transparent WS_EX_TOOLWINDOW leaves a white iconic caption on
                # the desktop when minimized. Keep its normal geometry and hide it.
                window.hide()
                hidden = True
            elif command == 'restore':
                window.show()
                window.restore()
                hidden = False
            elif command == 'pin':
                placement._topmost = not placement._topmost
                window.on_top = placement._topmost
                tray.set_topmost(placement._topmost)
                placement._sync_page()
                layout.save_window(saved_rect, placement._topmost)
            elif command == 'reset':
                layout.reset()
                target = layout.window_target(window_work_area(user32, hwnd))
                user32.SetWindowPos(hwnd, 0, *target, SWP_NOACTIVATE | SWP_NOOWNERZORDER | 0x0004)
                saved_rect = target
                layout.save_window(saved_rect, placement._topmost)
        if not hidden and not user32.IsIconic(hwnd) and not placement._dragging:
            current = client_rect(user32, hwnd)
            if current and current != saved_rect and time.monotonic() - saved_at >= .15:
                layout.save_window(current, placement._topmost)
                saved_rect, saved_at = current, time.monotonic()
        time.sleep(args.interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:47790/hud")
    ap.add_argument("--game-title", default=None, help=argparse.SUPPRESS)  # 兼容旧命令，不再跟随游戏
    ap.add_argument("--width-ratio", type=float, default=0.182, help=argparse.SUPPRESS)
    ap.add_argument("--margin", type=float, default=0.02, help=argparse.SUPPRESS)
    ap.add_argument("--interval", type=float, default=0.25, help="保存窗口布局与处理托盘命令的间隔（秒）")
    ap.add_argument("--edit", action="store_true", help=argparse.SUPPRESS)  # 兼容旧命令，所有窗口默认可调整
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

    args.placement = PlacementAPI()
    window = webview.create_window(HUD_TITLE, args.url, width=420, height=DEFAULT_HEIGHT, frameless=True, transparent=True,
                                   js_api=args.placement,
                                   min_size=(200, MIN_HEIGHT),
                                   on_top=Layout().topmost, focus=True, resizable=True, easy_drag=False, shadow=False,
                                   text_select=False, zoomable=False)
    args.placement._window = window
    window.events.loaded += args.placement._sync_page
    webview.start(manage, (window, args), gui="edgechromium")
    return 0


if __name__ == "__main__":
    sys.exit(main())
