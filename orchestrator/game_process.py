"""Windows 维护进程身份及主控互斥锁；不操作游戏窗口。"""
import ctypes
import os
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
    return k


@contextmanager
def controller_lease():
    k = _kernel()
    ctypes.set_last_error(0)
    handle = k.CreateMutexW(None, False, 'Local\\CelesteAIPlayController')
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

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
