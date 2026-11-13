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

ROOT = Path(__file__).resolve().parents[1]


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
                # launch() 自己最多等 PowerShell 30 秒、再等宿主报到 10 秒；这里的上限要比它长，
                # 否则慢的机器上（PowerShell 冷启动就要十几秒）是测试先放弃，而不是 launch() 失败。
                began = time.monotonic()
                deadline = began + 60
                while time.monotonic() < deadline:
                    if launcher.poll() is not None:
                        self.fail(launcher.stderr.read().decode(errors="replace"))
                    try:
                        launched = json.loads(marker.read_text(encoding='utf-8'))
                        break
                    except (OSError, ValueError):     # Not written yet, or only partly.
                        time.sleep(.1)
                else:
                    background = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "local" / "background").rglob("*"))[-12:]
                    self.fail(f"launch() 60 秒内没有返回；local/background 里最近的文件：{background}")
                print(f"[background_test] launch() 用时 {time.monotonic() - began:.1f} 秒", file=sys.stderr, flush=True)
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
