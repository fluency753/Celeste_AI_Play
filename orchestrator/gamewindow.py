"""游戏窗口的可见性（只在 Windows 上起作用）。

游戏窗口最小化时，FNA 会把交换链缩到 8×8 像素，OBS 的游戏采集只能采到一块深色小方块，录像里没有游戏画面
（HUD 是浏览器源，照常录进去）。所以录制开始前、以及录制中每次推进游戏前，都确认 Celeste 的窗口没有最小化；
最小化了就还原它——用"显示但不激活"的方式，不抢走你正在用的窗口的焦点。窗口被别的窗口挡住不影响采集。
"""
from __future__ import annotations

import functools
import os
import sys
import time

_IS_WINDOWS = sys.platform == "win32"


@functools.lru_cache(maxsize=1)
def _api():
    import ctypes
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wt.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    user32.GetWindow.argtypes = [wt.HWND, ctypes.c_uint]
    user32.GetWindow.restype = wt.HWND
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    return ctypes, wt, user32, kernel32, enum_proc


def _image_name(ctypes, wt, kernel32, pid: int) -> str:
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_game_window() -> int | None:
    """Celeste.exe 的主窗口（顶层、没有所有者、可见或最小化）；找不到或不是 Windows 时返回 None。"""
    if not _IS_WINDOWS:
        return None
    ctypes, wt, user32, kernel32, enum_proc = _api()
    found: list[int] = []
    names: dict[int, str] = {}

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER：跳过附属窗口
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            names[pid.value] = _image_name(ctypes, wt, kernel32, pid.value)
        if names[pid.value] == "celeste.exe":
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found[0] if found else None


def capture_size() -> tuple[int, int] | None:
    """游戏采集的实际客户区尺寸（与屏幕 DPI 缩放后的窗口截图尺寸不同）。"""
    if not _IS_WINDOWS:
        return None
    ctypes, wt, user32, _kernel32, _enum = _api()
    hwnd = find_game_window()
    if hwnd is None:
        return None
    rect = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    return (rect.right, rect.bottom) if rect.right > 64 and rect.bottom > 64 else None


def ensure_visible(log=print, wait_s: float = 3.0) -> str:
    """确认游戏窗口没有最小化；最小化了就不激活地还原，并等它恢复正常大小。
    返回 "ok"、"restored"、"restore_failed"、"not_found" 或 "unsupported"。"""
    if not _IS_WINDOWS:
        return "unsupported"
    ctypes, wt, user32, _kernel32, _enum = _api()
    hwnd = find_game_window()
    if hwnd is None:
        return "not_found"
    if not user32.IsIconic(hwnd):
        return "ok"
    user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE：还原到原来的大小和位置，但不激活、不抢焦点
    deadline = time.monotonic() + wait_s
    rect = wt.RECT()
    while time.monotonic() < deadline:
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        if not user32.IsIconic(hwnd) and rect.right > 64 and rect.bottom > 64:
            time.sleep(0.3)  # 留一点时间给游戏按新的窗口大小重建交换链、OBS 重新挂上采集
            log(f"[record] 游戏窗口是最小化的（录像里会没有游戏画面），已还原为 {rect.right}×{rect.bottom}（没有抢焦点）")
            return "restored"
        time.sleep(0.05)
    log("[record] 警告：游戏窗口是最小化的，还原失败；录像里会没有游戏画面，请手动把游戏窗口还原")
    return "restore_failed"
