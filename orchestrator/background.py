"""Windows 独立试玩宿主；由 WMI 服务启动，不依赖 Codex 的命令进程树。"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def console_python() -> str:
    """独立宿主与它启动的服务都用控制台版的 python.exe（窗口隐藏）。从 pythonw.exe 里启动时（例如控制台的启动器
    CelesteAIConsole.exe）也换成同一目录下的 python.exe：pythonw 没有控制台，它启动的 tasklist、docker、powershell 等
    命令行程序每次都会各自弹出一个黑色窗口；python.exe 的隐藏控制台则由这些子进程继承，不会弹窗。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return sys.executable


class LaunchRejected(RuntimeError):
    """Windows 明确没有创建宿主进程（WMI 返回失败或 PowerShell 调用失败），可以安全地换目录重试。"""


def launch(arguments, *, probe=False, service="play", directory=None):
    if os.name != "nt":
        raise RuntimeError("独立宿主目前只支持 Windows")
    if service not in {"play", "chat", "prepare", "hud", "hud-window"}:
        raise ValueError("未知后台服务")
    base = (ROOT / "local" / "background").resolve()
    directory = Path(directory).resolve() if directory is not None else base / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    if directory.parent != base:
        raise ValueError("后台宿主目录必须直接位于 local/background")
    directory.mkdir(parents=True, exist_ok=False)
    manifest = directory / "launch.json"
    manifest.write_text(json.dumps({"arguments": arguments, "probe": probe, "service": service,
        "codex_cli": os.environ.get("CODEX_APP_RUNTIME_CLI_PATH")}, ensure_ascii=False), encoding="utf-8")
    command = subprocess.list2cmdline([console_python(), "-X", "utf8", "-m", "orchestrator.background",
                                       "--worker", str(manifest)])
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = ("$ErrorActionPreference='Stop'; "
              "$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
              "$result=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
              f"CommandLine={quote(command)};CurrentDirectory={quote(ROOT)};ProcessStartupInformation=$startup" + "}; "
              "$result | Select-Object ReturnValue,ProcessId | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8", creationflags=subprocess.CREATE_NO_WINDOW,
                                timeout=30, check=True)
    except subprocess.CalledProcessError as exc:
        # ErrorActionPreference=Stop: Invoke-CimMethod failed, so no process exists.
        raise LaunchRejected(f"Windows 独立启动失败：{(exc.stderr or exc.stdout or '').strip() or exc}") from exc
    created = json.loads(result.stdout.strip())
    if created["ReturnValue"]:
        raise LaunchRejected(f"Windows 独立启动失败：{created}")
    for _ in range(100):
        status = directory / "status.json"
        if status.exists():
            data = json.loads(status.read_text(encoding="utf-8"))
            if data.get("error"):
                raise RuntimeError(data["error"])
            return directory, data
        time.sleep(.1)
    raise RuntimeError(f"宿主未确认就绪，请检查 {directory}")


def worker(manifest):
    directory = manifest.parent
    data = json.loads(manifest.read_text(encoding="utf-8"))
    status = {"host_pid": os.getpid(), "started_wall": time.time(), "state": "starting"}
    def save():
        temporary = directory / "status.tmp"
        temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        temporary.replace(directory / "status.json")
    mutex = None
    try:
        if not data.get("probe") and data.get("service") not in {"hud", "hud-window"}:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.restype = ctypes.c_void_p
            ctypes.set_last_error(0)
            mutex = kernel.CreateMutexW(None, False, "Local\\CelesteAIPlayController")
            if not mutex or ctypes.get_last_error() == 183:
                raise RuntimeError("独立试玩主控已经运行，拒绝重复启动")
            arguments = data["arguments"]
            port = 47780
            for index, value in enumerate(arguments):
                if value == "--mcp-port":
                    port = int(arguments[index + 1])
                elif value.startswith("--mcp-port="):
                    port = int(value.split("=", 1)[1])
            with socket.socket() as check:
                check.settimeout(1)
                if check.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"游戏主控端口 {port} 已被占用，拒绝接管现有连接")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["CELESTE_CONTROLLER_HOST"] = str(directory.resolve())
        docker = Path("D:/Program Files/Docker/Docker/resources/bin")
        if docker.is_dir():
            env["PATH"] = str(docker) + os.pathsep + env.get("PATH", "")
        if data.get("codex_cli"):
            env["CODEX_APP_RUNTIME_CLI_PATH"] = data["codex_cli"]
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_APP_TOOLS_PIPE_PATH", "CODEX_TOOL_CALL_ID"):
            env.pop(name, None)
        if data.get("probe"):
            command = [console_python(), "-c", "import time; time.sleep(12)"]
        else:
            module = {"play": "orchestrator.run", "chat": "orchestrator.chat", "prepare": "orchestrator.preparation", "hud": "overlay.server",
                      "hud-window": "overlay.window"}[data.get("service", "play")]
            command = [console_python(), "-X", "utf8", "-m", module, *data["arguments"]]
        with (directory / "output.log").open("ab", buffering=0) as output:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            status.update(child_pid=process.pid, state="running")
            save()
            status.update(exit_code=process.wait(), state="exited", ended_wall=time.time())
    except Exception as exc:
        status.update(state="failed", error=str(exc), ended_wall=time.time())
    finally:
        save()
        if mutex:
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(mutex)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker)
        return
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    if not arguments and not args.probe:
        parser.error("请在 -- 后提供主控参数")
    directory, status = launch(arguments, probe=args.probe)
    print(json.dumps({"directory": str(directory), **status}, ensure_ascii=False))


if __name__ == "__main__":
    main()
