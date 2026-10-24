"""打开控制台窗口（Windows 用 pywebview，其他系统或没装 pywebview 时用浏览器）。

    python -m overlay.console              # 打开 http://127.0.0.1:47790/console
    python -m overlay.console --browser    # 用默认浏览器打开

控制台的数据来自 HUD 服务。没有 HUD 服务在运行时，这里把它作为独立的后台进程启动（Windows 用
orchestrator/background.py 的独立宿主），关掉控制台窗口不会让正在进行的运行失去 HUD；窗口开着时每几秒检查一次，
服务断了（例如它原本跟着某次运行的主控一起退出）就重新启动。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TITLE = "Celeste AI 控制台"


def _status(port: int, path: str) -> int | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None


def _serving(port: int) -> bool:
    return _status(port, "/console") == 200


def stop_service(port: int) -> None:
    """结束监听这个端口的旧 HUD 服务（只在 Windows；有主控在运行时不做）。"""
    from overlay.console_api import orchestrator_status
    if orchestrator_status()["running"]:
        raise SystemExit("有主控在运行，不能重启 HUD 服务；等运行结束再试")
    script = (f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | "
              "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
    time.sleep(1)


def ensure_service(port: int, restart: bool = False) -> None:
    """HUD 服务（含控制台接口）在不在；不在就作为独立进程启动，最多等 10 秒。端口上是没有控制台的旧版服务时，
    --restart-service 才结束它，否则提示。"""
    if _serving(port):
        return
    if _status(port, "/hud/state") is not None:
        if not restart or sys.platform != "win32":
            raise SystemExit(f"端口 {port} 上是旧版 HUD 服务（没有控制台）：用 python -m overlay.console --restart-service "
                             "结束它并启动新版，或手动结束那个 python -m overlay.server 进程")
        stop_service(port)
    if sys.platform == "win32":
        from orchestrator.background import launch
        launch(["--port", str(port)], service="hud")
    else:
        log = REPO_ROOT / "build" / "hud-service.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as stream:
            subprocess.Popen([sys.executable, "-X", "utf8", "-m", "overlay.server", "--port", str(port)],
                             cwd=REPO_ROOT, stdout=stream, stderr=stream, stdin=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _serving(port):
            return
        time.sleep(0.2)
    raise SystemExit("HUD 服务没有启动起来；可以手动运行 python -m overlay.server 看报错")


def _watch(port: int, stop: threading.Event) -> None:
    while not stop.wait(5):
        try:
            ensure_service(port)
        except SystemExit:
            pass        # 旧版服务占着端口：不在后台擅自结束它


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=47790, help="HUD 服务端口（默认 47790）")
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，不开独立窗口")
    ap.add_argument("--restart-service", action="store_true", help="端口上是旧版 HUD 服务时结束它、启动新版（没有主控在运行时）")
    args = ap.parse_args()
    ensure_service(args.port, restart=args.restart_service)
    url = f"http://127.0.0.1:{args.port}/console"
    try:
        import webview  # pywebview（requirements.txt），用系统自带的 WebView2
    except ImportError:
        webview = None
    if args.browser or webview is None:
        webbrowser.open(url)
        print(f"控制台：{url}", flush=True)
        return 0
    stop = threading.Event()
    threading.Thread(target=_watch, args=(args.port, stop), daemon=True).start()
    webview.create_window(TITLE, url, width=1440, height=920, min_size=(760, 600), background_color="#141729")
    webview.start()
    stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
