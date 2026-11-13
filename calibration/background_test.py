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

from orchestrator.background import HOST_READY_WAIT_S, POWERSHELL_WAIT_S

ROOT = Path(__file__).resolve().parents[1]
# 这里等的都是真实的系统组件（PowerShell、WMI、新的 Python 进程），快慢由机器决定；上限只判断“卡住了”。
LAUNCH_LIMIT_S = POWERSHELL_WAIT_S + HOST_READY_WAIT_S + 30     # 比 launch() 自己的两段等待加起来还长
EXIT_LIMIT_S = 120                                              # 探测用的子进程睡 12 秒后退出；它启动得慢也等


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
                # 上限比 launch() 自己的等待长：慢的机器上该由 launch() 报出它的原因，而不是测试先放弃。
                began = time.monotonic()
                deadline = began + LAUNCH_LIMIT_S
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
                    self.fail(f"launch() {LAUNCH_LIMIT_S} 秒内没有返回；local/background 里最近的文件：{background}")
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
                waited = time.monotonic()
                deadline = waited + EXIT_LIMIT_S
                state = launched
                while time.monotonic() < deadline:
                    try:
                        state = json.loads(status_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):     # The host is replacing the file at this instant.
                        time.sleep(.1)
                        continue
                    if state["state"] == "exited":
                        break
                    time.sleep(.2)
                print(f"[background_test] 宿主在启动命令结束后 {time.monotonic() - waited:.1f} 秒写出退出记录",
                      file=sys.stderr, flush=True)
                self.assertEqual(state["state"], "exited", f"{EXIT_LIMIT_S} 秒内没有等到退出记录：{state}")
                self.assertEqual(state["exit_code"], 0)
            finally:
                if launcher.poll() is None:
                    launcher.terminate()
                    launcher.wait(timeout=5)
                launcher.stderr.close()


if __name__ == "__main__":
    unittest.main()
