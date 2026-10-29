"""Button templates are edited through the real API and read at the next event."""
import json
import asyncio
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
from orchestrator.prompting import first_play_prompt, continue_play_prompt, resume_prompt, net_resume_prompt
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
                           ('reconnect', lambda: resume_prompt('原会话重接', 3))]:
            name = f'button_{key}_zh.md'
            doc = console_api.prompt_read(name)
            console_api.prompt_write(name, '此按钮本机定制正文。', doc['version'])
            text = build()
            self.assertIn('此按钮本机定制正文。', text)
            if key == 'reconnect':
                self.assertIn('原会话重接', text)
                self.assertIn('约 3 分钟', text)

    def test_pause_resume_and_reconnect_templates_do_not_leak_into_other_phases(self):
        normal = notification('resume')
        self.assertIn('自行选择下一步操作', normal)
        self.assertNotIn('observe', normal)
        doc = console_api.prompt_read('button_resume_zh.md')
        console_api.prompt_write(doc['name'], '普通暂停后继续的本机正文。', doc['version'])
        self.assertIn('普通暂停后继续的本机正文', notification('resume'))
        for text in (resume_prompt('主控重接', 2), net_resume_prompt(2)):
            self.assertIn('【主控系统消息｜主控中断后恢复】', text)
            self.assertIn('observe', text)
            self.assertNotIn('普通暂停后继续的本机正文', text)
        self.assertIn('【主控系统消息｜恢复准备】', resume_prompt('重接', 2, '还有未读资料'))
        self.assertIn('【主控系统消息｜恢复准备】', net_resume_prompt(2, study_note='还有未读资料'))
        self.assertIn('【主控系统消息｜唤起聊天】', net_resume_prompt(2, chat=True))


class PauseResumeNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_hud_resume_uses_neutral_template_without_replacing_the_next_turn(self):
        from orchestrator.codex_agent import CodexRunner
        from orchestrator.game_session import GameSession
        from orchestrator.metrics import Metrics
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('orchestrator.config.REPO_ROOT', root), patch.object(prompting, 'OVERRIDES', root/'overrides'):
                cfg = RunConfig(agent='test', save='test', model='test', engine='codex')
                metrics = Metrics(root/'metrics.json', 'test', 'test', 'high', 60)
                session = GameSession(cfg, root, metrics)
                runner = CodexRunner(cfg, root, session, metrics, '', start_paused=True)
                ident = runner.control.enqueue('resume')
                task = asyncio.create_task(runner._controls())
                try:
                    async with asyncio.timeout(3):
                        while runner.paused:
                            await asyncio.sleep(.01)
                    self.assertIn('【主控系统消息｜恢复试玩】', runner.resume_notice)
                    self.assertIn('自行选择下一步操作', runner.resume_notice)
                    self.assertNotIn('observe', runner.resume_notice)
                    self.assertEqual(runner.pending_messages, [])
                    row = next(r for r in runner.control.snapshot()['actions'] if r['id'] == ident)
                    self.assertEqual(row['status'], 'submitted')
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    session.close(release=False)


if __name__ == '__main__':
    unittest.main()
