"""Isolated native pywebview lifecycle used by webview_host_test; never visits live services."""
import json
import ctypes
import os
import ctypes.wintypes as wt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import traceback
import struct
import uuid
from unittest.mock import patch

import webview

from overlay.webview_host import disable_unused_response_observer
from overlay.hud_startup import find_hud_window


def hidden_dialog(title):
    """Create a real #32770 dialog without showing it or touching another app."""
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    callback = callback_type(lambda hwnd, msg, wparam, lparam: 0)
    template = ctypes.create_string_buffer(struct.pack('<IIHhhhh', 0x80C00000, 0, 0, 0, 0, 100, 100)
                                          + b'\0\0\0\0' + title.encode('utf-16-le') + b'\0\0')
    user32.CreateDialogIndirectParamW.argtypes = [wt.HINSTANCE, ctypes.c_void_p, wt.HWND, callback_type, wt.LPARAM]
    user32.CreateDialogIndirectParamW.restype = wt.HWND
    hwnd = user32.CreateDialogIndirectParamW(None, template, None, callback, 0)
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd, callback, template


# Both limits only tell "stuck" from "slow". A warm machine finishes the whole check in a few seconds;
# the first WebView2 start on a cold, busy machine has taken more than 15 s before its first page loaded
# (CI windows-2025 runner, 2026-11-13). The test passes its own values so its subprocess limit stays above them.
CHECK_LIMIT_S = 180     # window load, two page phases and one reload, together
CLOSE_LIMIT_S = 60      # window teardown after the check
SERVED = {'page': 0, 'data': 0}     # what the browser actually asked this server for


class Handler(BaseHTTPRequestHandler):
    # Keep-alive: the 2 x 100 fetches share a connection. With HTTP/1.0 each fetch opened a new
    # loopback connection, and under the parallel suite's connection churn Windows occasionally
    # refused one before it reached this server ("TypeError: Failed to fetch", about 1 in 50 reloads).
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        SERVED['data' if self.path == '/data' else 'page'] += 1
        body = b'{}' if self.path == '/data' else b'<html><body>Isolated HUD host test</body></html>'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json' if self.path == '/data' else 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class API:
    def echo(self, value):
        return 'ack:' + value


def main():
    began = time.monotonic()
    check_limit = float(sys.argv[2]) if len(sys.argv) > 2 else CHECK_LIMIT_S
    close_limit = float(sys.argv[3]) if len(sys.argv) > 3 else CLOSE_LIMIT_S
    result = {'responses': 0, 'requests': 0, 'phases': [], 'timings': {}, 'served': SERVED}

    def mark(stage):
        result['timings'][stage] = round(time.monotonic() - began, 2)

    def report_if_stuck():
        # If the native window never closes, still hand back what happened instead of
        # leaving the caller with a bare timeout.
        def report():
            result.setdefault('error', f'fixture did not finish within {close_limit:g} s of closing; timings show the last step reached')
            Path(sys.argv[1]).write_text(json.dumps(result), encoding='utf-8')
            os._exit(1)
        timer = threading.Timer(close_limit, report)
        timer.daemon = True
        timer.start()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/'
    title = 'Isolated HUD host test ' + uuid.uuid4().hex
    dialog, dialog_callback, dialog_template = hidden_dialog(title)
    result['dialog_only_found'] = find_hud_window(title) is not None
    from orchestrator.run import start_hud_window
    with patch('overlay.hud_startup.HUD_TITLE', title), patch('orchestrator.run.subprocess.Popen') as spawn:
        owned = start_hud_window(server.server_port, Path(sys.argv[1]).parent)
    result['automatic_dialog_starts_owned'] = owned is spawn.return_value and spawn.call_count == 1
    if spawn.call_count:
        spawn.call_args.kwargs['stdout'].close()
    window = webview.create_window(title, url, hidden=True, js_api=API())
    disable_unused_response_observer(window)

    def count_request(request):
        result['requests'] += 1

    def count_response(response):
        result['responses'] += 1

    def check():
        deadline = time.monotonic() + check_limit

        def left():
            return max(.1, deadline - time.monotonic())

        try:
            mark('check started')
            if not window.events.loaded.wait(left()):
                raise TimeoutError(f'initial load did not finish within {check_limit:g} s')
            mark('loaded')
            from System import Action
            handles = []
            window.native.Invoke(Action(lambda: handles.append(int(window.native.Handle.ToInt64()))))
            result['real_hidden_form_found'] = find_hud_window(title) == handles[0]
            with patch('overlay.hud_startup.HUD_TITLE', title), patch('orchestrator.run.subprocess.Popen') as spawn:
                reused = start_hud_window(server.server_port, Path(sys.argv[1]).parent)
            result['automatic_form_reused'] = reused is None and spawn.call_count == 0
            # Observe after native setup, so this verifies that the native delegate
            # really stopped firing rather than only testing a Python success flag.
            window.events.request_sent += count_request
            window.events.response_received += count_response
            for phase in ('initial', 'reloaded'):
                if phase == 'reloaded':
                    window.load_url(url + '?reload')
                    if not window.events.loaded.wait(left()):
                        raise TimeoutError(f'reload did not finish within {check_limit:g} s of the start of the check')
                    mark('reload loaded')
                window.run_js('''window.testResult=null;
                    (async()=>{try{
                      const ack=await pywebview.api.echo('ready');
                      for(let i=0;i<100;i++) await (await fetch('/data',{cache:'no-store'})).json();
                      window.testResult={ack,fetches:100};
                    }catch(e){window.testResult={error:String(e)};}})();''')
                while True:
                    value = window.evaluate_js('window.testResult')
                    if value:
                        result['phases'].append(value)
                        mark(phase)
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'JS bridge or fetch did not finish within {check_limit:g} s of the start of the check')
                    time.sleep(.05)
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            mark('closing')
            report_if_stuck()
            window.destroy()
            mark('window destroyed')
            server.shutdown()
            server.server_close()
            mark('server closed')

    try:
        webview.start(check, gui='edgechromium')
        mark('window loop ended')
    finally:
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.DestroyWindow.argtypes = [wt.HWND]
        user32.DestroyWindow(dialog)
    Path(sys.argv[1]).write_text(json.dumps(result), encoding='utf-8')
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
