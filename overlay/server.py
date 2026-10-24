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
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .codex_session import apply_session
from .history import read_history
from .layout import Layout
from .typography import load_font
from orchestrator.control import RunControl

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "hud.html"
CONFIG = REPO_ROOT / "configs" / "hud.json"
LOCAL_CONFIG = REPO_ROOT / "configs" / "local.hud.json"   # 本机覆盖（不入库）
DEFAULT_PORT = 47790
_wake_lock = threading.Lock()


def waking(directory):
    try:
        host = json.loads((directory / "chat_host.json").read_text(encoding="utf-8"))
        status = json.loads((Path(host["directory"]) / "status.json").read_text(encoding="utf-8"))
        return status.get("state") == "running" and time.time() - host["wall"] < 60
    except (OSError, ValueError, KeyError, TypeError):
        return False


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
    config["desktop_layout"] = Layout().custom
    config["font_scale"] = load_font()
    return config


def anchor_run(pinned: Path | None) -> Path | None:
    if pinned is not None:
        return pinned
    try:
        value=(REPO_ROOT / 'runs/latest.txt').read_text(encoding='utf-8').strip()
        if value and Path(value).is_dir():return Path(value)
    except OSError:
        pass
    from orchestrator.session_store import idle_anchor
    return idle_anchor(REPO_ROOT)


def current_run(pinned: Path | None) -> Path | None:
    from orchestrator.conversations import selected_run
    return selected_run(anchor_run(pinned))


def read_metrics(run_dir: Path | None) -> dict | None:
    if run_dir is None:
        return None
    try:
        return json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None   # 文件正在被替换的瞬间读不到：页面保留上一帧


def make_handler(pinned: Path | None, chat_thread: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安静运行
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            # OBS 浏览器源用本地文件打开页面时，页面来自另一个源，需要允许它跨源取数据
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/hud", "/hud/"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif path == '/api/saves':
                from orchestrator.saves import inventory, supports_unicode
                try:
                    self._send(200, json.dumps({'saves': inventory(), 'unicode_names': supports_unicode()}, ensure_ascii=False).encode('utf-8'), 'application/json')
                except ValueError as exc:
                    self._send(503, json.dumps({'saves': [], 'error': str(exc)}, ensure_ascii=False).encode('utf-8'), 'application/json')
            elif path == '/api/models':
                from orchestrator.model_catalog import load_models
                try:
                    from urllib.parse import parse_qs
                    refresh = parse_qs(urlparse(self.path).query).get('refresh') == ['1']
                    payload = load_models(refresh=refresh)
                    self._send(200, json.dumps(payload, ensure_ascii=False).encode('utf-8'), 'application/json')
                except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
                    self._send(503, json.dumps({'models': [], 'error': '无法读取模型列表：' + str(exc)}, ensure_ascii=False).encode('utf-8'), 'application/json')
            elif path == "/api/control":
                directory = current_run(pinned)
                metrics = read_metrics(directory) or {}
                payload = {"run": str(directory), "model": metrics.get("model"), "room": metrics.get("room"),
                           "control": RunControl(directory).snapshot() if directory else {"connected": False}}
                from orchestrator.chat import can_wake
                payload["can_wake"] = can_wake(directory)
                payload["waking"] = waking(directory) if directory and not payload["control"]["connected"] else False
                from orchestrator.conversations import catalog, read_json
                payload["sessions"] = [{k: record[k] for k in ("id", "label", "model")}
                                       for record in catalog(anchor_run(pinned))]
                payload["selected_session"] = read_json(directory / "run_state.json").get("session_id") if directory else None
                payload['save'] = read_json(directory / 'run_state.json').get('save') if directory else None
                payload['save_slot'] = read_json(directory / 'run_state.json').get('save_slot') if directory else None
                from orchestrator.session_actions import operation_status
                payload["operation"] = operation_status(anchor_run(pinned))
                payload["ready_to_play"] = bool(directory and read_json(directory / 'run_state.json').get('session_kind') == 'ready')
                payload['session_management'] = pinned is None
                idle=bool(directory and read_json(directory/'run_state.json').get('session_kind') in {'idle','standby'})
                payload['can_create_session']=payload['can_wake'] or idle
                from orchestrator.play_scope import sim_available
                payload['sim_available']=sim_available(REPO_ROOT)
                self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")
            elif path == "/hud/state":
                run_dir = current_run(pinned)
                metrics = read_metrics(run_dir)
                if metrics and chat_thread and metrics.get("telemetry_source") != "codex_app_server":
                    metrics = {**metrics, "telemetry_source": "codex_session", "chat_thread_id": chat_thread}
                metrics, config = apply_session(metrics, load_config())
                payload = {"now": time.time(), "config": config, "metrics": metrics,
                           "run": str(run_dir) if run_dir else None}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path == "/hud/history":
                run_dir = current_run(pinned)
                payload = {"run": str(run_dir) if run_dir else None,
                           "items": read_history(run_dir, read_metrics(run_dir))}
                self._send(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            try:
                if self.path != "/api/control":
                    raise ValueError("未知控制接口")
                origin = self.headers.get("Origin")
                if origin and (urlparse(origin).scheme != "http" or urlparse(origin).hostname not in {"127.0.0.1", "localhost"}):
                    raise ValueError("只接受本机控制页面")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("请求必须是 JSON")
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 20000:
                    raise ValueError("请求大小无效")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("请求必须是 JSON 对象")
                directory = current_run(pinned)
                if directory is None or data.get("run") != str(directory):
                    raise ValueError("当前运行已变化，请刷新后重试")
                control = RunControl(directory)
                from orchestrator.session_actions import operation_status, BUSY
                if operation_status(anchor_run(pinned)).get('status') in BUSY:
                    raise ValueError('会话正在准备或接入，请等待完成')
                if data.get('kind') in {'new_session', 'disconnect', 'play_session'}:
                    if pinned is not None:
                        raise ValueError('固定预览模式不提供会话管理，请使用跟随最新运行的 HUD 服务')
                    from orchestrator.session_actions import create_session, disconnect, play_session
                    with _wake_lock:
                        if current_run(pinned) != directory:
                            raise ValueError('当前会话已变化，请重试')
                        if data['kind'] == 'disconnect':
                            disconnect(anchor_run(pinned), directory)
                            result = {'disconnected': True}
                        else:
                            options = json.loads(data.get('text') or '{}')
                            result = (create_session if data['kind'] == 'new_session' else play_session)(anchor_run(pinned), directory, options)
                    self._send(200, json.dumps(result, ensure_ascii=False).encode('utf-8'), 'application/json')
                    return
                if data.get("kind") == "switch":
                    from orchestrator.conversations import switch_session
                    with _wake_lock:
                        # 重新核对，避免两个客户端同时切换到不同会话。
                        if current_run(pinned) != directory:
                            raise ValueError("当前会话已变化，请重试")
                        target = switch_session(anchor_run(pinned), directory, data.get("text"))
                    self._send(200, json.dumps({"run": str(target)}).encode(), "application/json")
                    return
                if data.get("kind") == "wake":
                    from orchestrator.chat import can_wake
                    from orchestrator.background import launch
                    with _wake_lock:
                        if not can_wake(directory):
                            raise ValueError("本次试玩尚未完成，不能唤起结束后的聊天")
                        if not control.snapshot()["connected"] and not waking(directory):
                            host, status = launch(["--run", str(directory)], service="chat")
                            (directory / "chat_host.json").write_text(json.dumps({
                                "directory": str(host), "wall": time.time()}), encoding="utf-8")
                    self._send(200, b'{"waking":true}', "application/json")
                    return
                if not control.snapshot()["connected"]:
                    raise ValueError("主控未连接，消息尚未发送")
                if data.get('kind') == 'effort':
                    if not control.snapshot().get('effort_control'):
                        raise ValueError('当前主控还不支持手动推理强度，请更新主控后重试')
                    if data.get('text') != 'auto':
                        from orchestrator.model_catalog import resolve_selection
                        resolve_selection((read_metrics(directory) or {}).get('model'),data.get('text'))
                if data.get('kind')=='cutscene_mode' and not control.snapshot().get('cutscene_control'):
                    raise ValueError('请断开并重新连接会话后再切换剧情模式')
                if data.get("kind") in {"goal", "goal_clear", "stop"} and not control.snapshot().get("goals_enabled"):
                    raise ValueError("当前未在试玩，不能修改或触发停止目标")
                ident = control.enqueue(data.get("kind"), data.get("text", ""))
                self._send(200, json.dumps({"id": ident}).encode(), "application/json")
            except (ValueError, TypeError, RuntimeError, OSError) as exc:
                self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8"), "application/json")

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
    ap.add_argument("--codex-thread", help="只读接入指定 Codex 聊天的遥测，不改写游戏统计")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    pinned = Path(args.run).resolve() if args.run else None
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(pinned, args.codex_thread))
    print(f"HUD：http://127.0.0.1:{args.port}/hud（Ctrl+C 结束）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
