"""Isolated native pywebview lifecycle used by webview_host_test; never visits live services."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import time
import traceback

import webview

from overlay.webview_host import disable_unused_response_observer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
    result = {'responses': 0, 'requests': 0, 'phases': []}
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{server.server_port}/'
    window = webview.create_window('Isolated HUD host test', url, hidden=True, js_api=API())
    disable_unused_response_observer(window)

    def count_request(request):
        result['requests'] += 1

    def count_response(response):
        result['responses'] += 1

    def check():
        try:
            if not window.events.loaded.wait(15):
                raise TimeoutError('initial load did not finish')
            # Observe after native setup, so this verifies that the native delegate
            # really stopped firing rather than only testing a Python success flag.
            window.events.request_sent += count_request
            window.events.response_received += count_response
            for phase in ('initial', 'reloaded'):
                if phase == 'reloaded':
                    window.load_url(url + '?reload')
                    if not window.events.loaded.wait(15):
                        raise TimeoutError('reload did not finish')
                window.run_js('''window.testResult=null;
                    (async()=>{try{
                      const ack=await pywebview.api.echo('ready');
                      for(let i=0;i<100;i++) await (await fetch('/data',{cache:'no-store'})).json();
                      window.testResult={ack,fetches:100};
                    }catch(e){window.testResult={error:String(e)};}})();''')
                deadline = time.monotonic() + 15
                while True:
                    value = window.evaluate_js('window.testResult')
                    if value:
                        result['phases'].append(value)
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError('JS bridge or fetch did not finish')
                    time.sleep(.05)
        except Exception:
            result['error'] = traceback.format_exc()
        finally:
            window.destroy()
            server.shutdown()
            server.server_close()

    webview.start(check, gui='edgechromium')
    Path(sys.argv[1]).write_text(json.dumps(result), encoding='utf-8')
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
