"""Windows 实测：关闭启动命令后，独立宿主与子进程继续运行并写入退出记录。"""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == "nt", "Windows WMI 生命周期测试")
class BackgroundTest(unittest.TestCase):
    def test_host_survives_launcher_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "launched.json"
            code = ("from orchestrator.background import launch; import json,time; from pathlib import Path; "
                    "directory,status=launch([],probe=True); "
                    f"Path({str(marker)!r}).write_text(json.dumps({{'directory':str(directory),**status}})); "
                    "time.sleep(60)")
            launcher = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 20
                while not marker.exists() and time.monotonic() < deadline:
                    if launcher.poll() is not None:
                        self.fail(launcher.stderr.read().decode(errors="replace"))
                    time.sleep(.1)
                self.assertTrue(marker.exists())
                launched = json.loads(marker.read_text())
                launcher.terminate()
                launcher.wait(timeout=5)
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                time.sleep(.3)
                for pid in (launched["host_pid"], launched["child_pid"]):
                    handle = kernel.OpenProcess(0x1000, False, pid)
                    self.assertTrue(handle, f"后台进程 {pid} 随启动命令退出")
                    kernel.CloseHandle(handle)
                status_path = Path(launched["directory"]) / "status.json"
                deadline = time.monotonic() + 18
                while time.monotonic() < deadline:
                    state = json.loads(status_path.read_text(encoding="utf-8"))
                    if state["state"] == "exited":
                        break
                    time.sleep(.2)
                self.assertEqual(state["state"], "exited")
                self.assertEqual(state["exit_code"], 0)
            finally:
                if launcher.poll() is None:
                    launcher.terminate()
                    launcher.wait(timeout=5)
                launcher.stderr.close()


if __name__ == "__main__":
    unittest.main()
