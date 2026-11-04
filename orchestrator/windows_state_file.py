"""Windows handles for publishing a snapshot while readers retain the old one."""
import ctypes
from ctypes import wintypes
import msvcrt


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_open = _kernel.CreateFileW
_open.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_open.restype = wintypes.HANDLE
_close = _kernel.CloseHandle
_close.argtypes = [wintypes.HANDLE]
_close.restype = wintypes.BOOL
_rename = _kernel.SetFileInformationByHandle
_rename.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
_rename.restype = wintypes.BOOL

_SHARE_READ_WRITE_DELETE = 7
_OPEN_EXISTING = 3
_GENERIC_READ = 0x80000000
_DELETE = 0x10000
_FILE_RENAME_INFO_EX = 22
_REPLACE_EXISTING_POSIX = 3


def _handle(path, access):
    handle = _open(str(path), access, _SHARE_READ_WRITE_DELETE, None, _OPEN_EXISTING, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        raise OSError(error.errno, error.strerror, path, error.winerror)
    return handle


def shared_reader(path, flags):
    handle = _handle(path, _GENERIC_READ)
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException:
        _close(handle)
        raise


class _RenameInfo(ctypes.Structure):
    _fields_ = [('flags', wintypes.DWORD), ('root', wintypes.HANDLE),
                ('name_size', wintypes.DWORD), ('name', wintypes.WCHAR * 1)]


def replace_shared(source, destination):
    """FileRenameInfoEx leaves open readers on the old snapshot."""
    name = str(destination.absolute()).encode('utf-16-le', errors='surrogatepass')
    offset = _RenameInfo.name.offset
    size = max(ctypes.sizeof(_RenameInfo), offset + len(name) + 2)
    buffer = ctypes.create_string_buffer(size)
    info = _RenameInfo.from_buffer(buffer)
    info.flags, info.name_size = _REPLACE_EXISTING_POSIX, len(name)
    ctypes.memmove(ctypes.addressof(buffer) + offset, name, len(name))
    handle = _handle(source, _DELETE)
    try:
        if not _rename(handle, _FILE_RENAME_INFO_EX, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _close(handle)
