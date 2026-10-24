from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
import urllib.request
from overlay.server import make_handler
from orchestrator.session_actions import require_idle


class BootstrapTests(unittest.TestCase):
    def test_empty_install_can_create_first_session_without_fake_completed_run(self):
        with tempfile.TemporaryDirectory() as folder,patch('overlay.server.REPO_ROOT',Path(folder)),patch('orchestrator.session_actions.create_session',return_value={'status':'preparing'}) as create:
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(None));worker=Thread(target=server.serve_forever,daemon=True);worker.start()
            try:
                url=f'http://127.0.0.1:{server.server_address[1]}/api/control'
                state=json.load(urllib.request.urlopen(url));self.assertTrue(state['can_create_session']);self.assertFalse(state['can_wake'])
                directory=Path(state['run']);require_idle(directory);self.assertFalse((directory/'summary.json').exists())
                request=urllib.request.Request(url,data=json.dumps({'run':state['run'],'kind':'new_session','text':'{}'}).encode(),headers={'Content-Type':'application/json'})
                self.assertEqual(json.load(urllib.request.urlopen(request))['status'],'preparing');create.assert_called_once()
            finally:server.shutdown();server.server_close();worker.join()
