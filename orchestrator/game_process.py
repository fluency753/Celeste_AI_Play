"""Windows 维护进程身份及主控互斥锁；不操作游戏窗口。"""
import ctypes
import os
import socket
import struct
from contextlib import contextmanager


def _kernel():
    if os.name != 'nt':
        raise RuntimeError('维护退出的进程确认目前仅支持 Windows')
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, w.BOOL, w.LPCWSTR]
    k.CreateMutexW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.WaitForSingleObject.restype = w.DWORD
    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k.GetProcessTimes.argtypes = [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)]
    k.GetProcessTimes.restype = w.BOOL
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.QueryFullProcessImageNameW.restype = w.BOOL
    return k


CONTROLLER_MUTEX = 'Local\\CelesteAIPlayController'
ERROR_ALREADY_EXISTS = 183


def hold_cli_controller_lease(environ=None, *, kernel=None, last_error=None, attempts=5, delay=0.2):
    """命令行直接启动的主控持有与后台宿主相同的互斥锁，直到进程退出。

    后台宿主（CELESTE_CONTROLLER_HOST）已为其子进程持有该锁，此时不再申请，避免自锁。
    返回需在进程生命周期内保留的句柄；非 Windows 返回 None。互斥锁已存在时说明另一个主控
    （或维护工具）正在运行，短暂重试以容忍其他工具的瞬时存在检查，仍存在就拒绝启动。
    """
    environ = os.environ if environ is None else environ
    if environ.get('CELESTE_CONTROLLER_HOST'):
        return None
    if kernel is None:
        if os.name != 'nt':
            return None
        kernel = _kernel()
    import time
    for attempt in range(attempts):
        if last_error is None:
            ctypes.set_last_error(0)
        handle = kernel.CreateMutexW(None, False, CONTROLLER_MUTEX)
        error = last_error() if last_error is not None else ctypes.get_last_error()
        if not handle:
            raise OSError(f'无法创建主控互斥锁（Windows 错误 {error}）')
        if error != ERROR_ALREADY_EXISTS:
            return handle
        kernel.CloseHandle(handle)
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError('已有主控或维护工具在运行（主控互斥锁已被持有）；未连接桥接或启动录制。'
                       '请先中断并等待它退出后再启动')


@contextmanager
def controller_lease():
    k = _kernel()
    ctypes.set_last_error(0)
    handle = k.CreateMutexW(None, False, CONTROLLER_MUTEX)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    try:
        if error == 183:
            raise RuntimeError('主控仍在运行，请先中断并等待它交还连接；没有另连桥接')
        yield
    finally:
        k.CloseHandle(handle)


class GameProcess:
    def __init__(self, pid):
        if type(pid) is not int or pid <= 0:
            raise RuntimeError('桥接未报告游戏进程身份，请更新 Bridge；未执行维护操作')
        self.pid = pid
        self.kernel = _kernel()
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if self.kernel.WaitForSingleObject(self.handle, 0) != 258:
            self.close()
            raise RuntimeError('目标游戏进程已退出；未执行维护操作')

    def wait(self, timeout):
        result = self.kernel.WaitForSingleObject(self.handle, int(timeout * 1000))
        if result == 258:
            raise RuntimeError('已发送菜单退出确认，但原游戏进程仍在运行；未重复确认或强制关闭')
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        from ctypes.wintypes import DWORD
        code = DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def identity(self):
        """只读核对原进程仍存活，保存创建时间以拒绝 PID 复用。"""
        from ctypes import wintypes as w
        if self.kernel.WaitForSingleObject(self.handle, 0) != 258:
            raise RuntimeError('原游戏进程已退出；未继续维护')
        times = [w.FILETIME() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        size = w.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, path, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return {'pid': self.pid, 'created': times[0].dwLowDateTime | (times[0].dwHighDateTime << 32),
                'executable': path.value}

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def idle_bridge_listener(port):
    """在建立维护连接之前检查监听者和已有连接；调用方须持有 controller_lease。"""
    if os.name != 'nt':
        raise RuntimeError('空闲桥接维护目前只支持 Windows；未连接游戏')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('桥接端口须为 1–65535')
    from ctypes import wintypes as w
    api = ctypes.WinDLL('iphlpapi', use_last_error=True).GetExtendedTcpTable
    api.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.DWORD), w.BOOL, w.ULONG, w.ULONG, w.ULONG]
    api.restype = w.DWORD
    size = w.DWORD()
    result = api(None, ctypes.byref(size), False, socket.AF_INET, 5, 0)
    if result not in (0, 122):
        raise OSError(result, '无法核对游戏端口占用；未连接')
    for _ in range(4):
        buffer = ctypes.create_string_buffer(size.value)
        result = api(buffer, ctypes.byref(size), False, socket.AF_INET, 5, 0)
        if result != 122:
            break
    if result:
        raise OSError(result, '无法核对游戏端口占用；未连接')
    listeners = set()
    count = struct.unpack_from('<I', buffer)[0]
    for index in range(count):
        state, addr, local_port, _, _, pid = struct.unpack_from('<6I', buffer, 4 + index * 24)
        if socket.ntohs(local_port & 0xffff) != port or addr not in (0, 0x0100007f):
            continue
        if state == 2:
            listeners.add(pid)
        elif state not in (1, 11, 12):
            raise RuntimeError('游戏桥接端口已有连接；未连接或挤掉原控制者，请经在用主控更新')
    if len(listeners) != 1:
        raise RuntimeError('没有确认唯一的游戏桥接监听进程；未连接')
    return listeners.pop()
