"""Button templates are edited through the real API and read at the next event."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from orchestrator import prompting
from orchestrator.button_prompts import catalog, notification
from orchestrator.config import RunConfig
from orchestrator.prompting import first_play_prompt, continue_play_prompt, resume_prompt
from overlay import console_api
from overlay.server import make_handler


class ButtonPromptIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        replacement = patch.object(prompting, 'OVERRIDES', self.root/'overrides')
        replacement.start()
        self.addCleanup(replacement.stop)
        status = patch.object(console_api, 'orchestrator_status', return_value={'running': True})
        status.start()
        self.addCleanup(status.stop)

    def test_live_api_edit_and_reset_affect_next_notification_but_not_fixed_facts(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(None))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post(path, payload):
            request = urllib.request.Request(f'http://127.0.0.1:{http.server_port}{path}',
                data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
            with opener.open(request) as response:
                return json.load(response)
        try:
            before = console_api.prompt_read('button_act_limit_zh.md')
            self.assertTrue(before['live_editable'])
            saved = post('/api/console/prompt/file', {'name': before['name'], 'text': '先按新上限做一次短实验。', 'version': before['version']})
            self.assertTrue(saved['overridden'])
            text = notification('act_limit', facts='实际当前上限：300帧。')
            self.assertIn('先按新上限做一次短实验。', text)
            self.assertIn('实际当前上限：300帧。', text)
            self.assertTrue(text.startswith('【主控系统消息'))
            cleared = post('/api/console/prompt/file', {'name': before['name'], 'text': '', 'version': saved['version']})
            self.assertEqual(notification('act_limit', '实际当前上限：100帧。'), '【主控系统消息｜更新动作帧数上限】\n实际当前上限：100帧。')
            with self.assertRaises(urllib.error.HTTPError) as stale:
                post('/api/console/prompt/file', {'name': before['name'], 'text': '过期编辑', 'version': saved['version']})
            stale.exception.close()
            reset = post('/api/console/prompt/reset', {'name': before['name']})
            self.assertFalse(reset['overridden'])
            self.assertEqual(reset['text'], before['text'])
            stable = console_api.prompt_read('explore_zh.md')
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                post('/api/console/prompt/file', {'name': stable['name'], 'text': stable['text'], 'version': stable['version']})
            blocked.exception.close()
        finally:
            http.shutdown(); http.server_close(); thread.join()

    def test_catalog_is_editable_but_not_inlined_in_guide_or_startup(self):
        preview = console_api.prompt_preview({'engine': 'codex', 'mode': 'explore', 'sim_assist': False})
        self.assertEqual({i['name'] for i in preview['button_prompts']}, {i['name'] for i in catalog()})
        self.assertFalse(any(p['source'] and p['source'].startswith('button_') for p in preview['guide_parts']))

    def test_start_continue_and_resume_read_current_body_and_preserve_resume_facts(self):
        for key, build in [('start_play', lambda: first_play_prompt(RunConfig(agent='test', save='test', model='test'))),
                           ('continue_play', lambda: continue_play_prompt(None)),
                           ('resume', lambda: resume_prompt('原会话重接', 3))]:
            name = f'button_{key}_zh.md'
            doc = console_api.prompt_read(name)
            console_api.prompt_write(name, '此按钮本机定制正文。', doc['version'])
            text = build()
            self.assertIn('此按钮本机定制正文。', text)
            if key == 'resume':
                self.assertIn('原会话重接', text)
                self.assertIn('约 3 分钟', text)


if __name__ == '__main__':
    unittest.main()
