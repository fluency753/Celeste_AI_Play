"""Own regression workers and their helper processes; never search for user apps."""
import os
import signal
import subprocess


def windows_job():
    import ctypes
    from ctypes import wintypes

    class Limits(ctypes.Structure):
        _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('min_working_set', ctypes.c_size_t),
                    ('max_working_set', ctypes.c_size_t), ('active_processes', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

    class Extended(ctypes.Structure):
        _fields_ = [('limits', Limits), ('io', Counters), ('process_memory', ctypes.c_size_t),
                    ('job_memory', ctypes.c_size_t), ('peak_process_memory', ctypes.c_size_t),
                    ('peak_job_memory', ctypes.c_size_t)]

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.SetInformationJobObject.restype = wintypes.BOOL
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.AssignProcessToJobObject.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    info.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.WinError(ctypes.get_last_error())
        api.CloseHandle(handle)
        raise error
    return api, handle


class OwnedWorker:
    def __init__(self, command, log, ready):
        self.proc, self.job = None, None
        self.log = log
        try:
            flags = 0
            if os.name == 'nt':
                self.job = windows_job()
                flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            self.proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=flags, start_new_session=os.name != 'nt')
            if self.job:
                import ctypes
                api, handle = self.job
                if not api.AssignProcessToJobObject(handle, self.proc._handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            # Worker imports/tests are forbidden until ownership is established.
            ready.write_text('ready', encoding='ascii')
        except BaseException:
            if self.proc is not None:
                self.proc.terminate()
                self.proc.wait()
            self.close()
            raise

    def force_stop(self):
        if os.name == 'nt':
            if self.job:
                api, handle = self.job
                api.CloseHandle(handle)
                self.job = None
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        else:
            self._kill_group()
        if self.proc:
            self.proc.wait()

    def _kill_group(self):
        if self.proc is None:
            return
        if self.proc.poll() is not None:
            try:
                os.getpgid(self.proc.pid)
            except ProcessLookupError:
                pass  # Leader is gone; its group can still contain our helpers.
            else:
                return  # The reaped PID has been reused; never touch the new owner.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def close(self):
        if self.job:
            api, handle = self.job
            api.CloseHandle(handle)  # Also reaps any helper left behind after worker exit.
            self.job = None
        elif os.name != 'nt':
            self._kill_group()
        if self.log:
            self.log.close()
            self.log = None
