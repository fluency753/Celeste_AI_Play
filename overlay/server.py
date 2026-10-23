"""HUD 服务：提供 HUD 页面与实时数据。

- GET /hud         HUD 页面（OBS 浏览器源与置顶窗口用同一个页面）
- GET /hud/state   当前运行的统计（metrics.json）、HUD 配置与服务器时间

默认显示最近一次运行（runs/latest.txt 指向的目录），每次请求都重新读取，所以 OBS 浏览器源可以一直开着，
换一局运行也不用刷新。主控运行时会在进程内自动启动它（端口被占用时说明已经有一个在跑，直接复用）。

单独启动（仓库根目录）：
    python -m overlay.server                 # http://127.0.0.1:47790/hud
    python -m overlay.server --run runs/opus/20261022-234505
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "hud.html"
CONFIG = REPO_ROOT / "configs" / "hud.json"
LOCAL_CONFIG = REPO_ROOT / "configs" / "local.hud.json"   # 本机覆盖（不入库）
DEFAULT_PORT = 47790


def load_config() -> dict:
    config: dict = {}
    for path in (CONFIG, LOCAL_CONFIG):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for key, value in data.items():
            if isinstance(value, dict) and isinstance(config.get(key), dict):
                config[key] = {**config[key], **value}
            else:
                config[key] = value
    return config


def current_run(pinned: Path | None) -> Path | None:
    if pinned is not None:
        return pinned
    try:
        return Path((REPO_ROOT / "runs" / "latest.txt").read_text(encoding="utf-8").strip())
    except OSError:
        return None


def read_metrics(run_dir: Path | None) -> dict | None:
    if run_dir is None:
        return None
    try:
        return json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None   # 文件正在被替换的瞬间读不到：页面保留上一帧


def make_handler(pinned: Path | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静运行
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/hud", "/hud/"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == "/hud/state":
                run_dir = current_run(pinned)
                payload = {"now": time.time(), "config": load_config(), "metrics": read_metrics(run_dir),
                           "run": str(run_dir) if run_dir else None}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def start_in_thread(port: int = DEFAULT_PORT, pinned: Path | None = None) -> ThreadingHTTPServer | None:
    """在后台线程启动 HUD 服务；端口已被占用（已有 HUD 服务在跑）时返回 None。"""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(pinned))
    except OSError:
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="hud-server", daemon=True).start()
    return server


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--run", help="固定显示某一次运行的目录（默认跟随 runs/latest.txt）")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    pinned = Path(args.run).resolve() if args.run else None
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(pinned))
    print(f"HUD：http://127.0.0.1:{args.port}/hud（Ctrl+C 结束）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
