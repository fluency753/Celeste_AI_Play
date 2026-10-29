"""Automatic turn fallbacks identify the controller and do not dictate gameplay actions."""
import asyncio
import json
from pathlib import Path
import tempfile
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.config import RunConfig
from orchestrator.codex_agent import CodexRunner
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.prompting import continue_play_prompt, controller_turn_prompt
from orchestrator.agent import ClaudeRunner


class ContinuationPromptTests(unittest.TestCase):
    def test_both_completion_policies_use_neutral_controller_message(self):
        for policy in ('stop', 'continue'):
            cfg = RunConfig(agent='test', save='test', model='test', completion_policy=policy)
            text = continue_play_prompt(cfg)
            self.assertTrue(text.startswith('【主控系统消息｜自动续轮】'))
            self.assertIn('不是用户新消息', text)
            self.assertIn('不改变任务范围、权限或停止条件', text)
            self.assertIn('自行选择下一步操作', text)
            for stale in ('还没有通关', '调用 act', '调用 celeste_act', '先更新一句公开解说'):
                self.assertNotIn(stale, text)


class CodexContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def run_two_turns(self, *, preparing=False, user_message=None, initial_notice=False, later_notice=False, initial_user=None, return_all=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent='test', save='test', model='test', engine='codex', mode='explore')
            session = SimpleNamespace(done=False, done_reason='fixture finished')
            study = None
            if preparing:
                kb = root/'kb'
                kb.mkdir()
                (kb/'README.md').write_text('unread line\n', encoding='utf-8')
                study = KbStudy(kb, 10)
            runner = CodexRunner(cfg, root, session, Metrics(root/'metrics.json', 'test', 'test', 'high', 60),
                                 'unused', study, resume_note='fixture first turn')
            from orchestrator.button_prompts import notification
            if initial_notice:
                runner.resume_notice = notification('resume')
            if initial_user:
                runner.pending_messages.append({'id': -1, 'text': initial_user})
            sent = []

            async def request(method, params):
                if method == 'thread/start':
                    return {'thread': {'id': 'fixture'}, 'model': 'test', 'sandbox': {'type': 'readOnly'}, 'activePermissionProfile': {'id': params['permissions']}}
                if method == 'turn/start':
                    sent.append(params['input'][0]['text'])
                    if len(sent) == 2:
                        session.done = True
                    def complete():
                        runner._event('turn/completed', {'turn': {'status': 'completed'}})
                        if len(sent) == 1 and user_message:
                            runner.pending_messages.append({'id': -1, 'text': user_message})
                        if len(sent) == 1 and later_notice:
                            runner.resume_notice = notification('resume')
                    asyncio.get_running_loop().call_soon(complete)
                    return {'turn': {'id': str(len(sent))}}
                return {}

            runner.rpc.start, runner.rpc.close = AsyncMock(), AsyncMock()
            runner.rpc.request = AsyncMock(side_effect=request)
            runner.rpc.reader = asyncio.get_running_loop().create_future()
            with patch('orchestrator.config.REPO_ROOT', root), \
                 patch('orchestrator.model_catalog.load_models', return_value={'models': []}):
                await asyncio.wait_for(runner.run(), 5)
            self.assertEqual(len(sent), 2)
            return sent if return_all else sent[1]

    async def test_normal_fallback_reaches_turn_input_without_extra_action_or_narration_command(self):
        text = await self.run_two_turns()
        self.assertEqual(text, continue_play_prompt(None))

    async def test_preparation_fallback_identifies_controller(self):
        text = await self.run_two_turns(preparing=True)
        self.assertEqual(text, controller_turn_prompt('继续准备，用 celeste_read 分页读取尚未读完的资料。'))

    async def test_real_user_message_replaces_fallback_without_being_relabelled(self):
        text = await self.run_two_turns(user_message='先复盘笔记，不推进游戏。')
        self.assertEqual(text, '先复盘笔记，不推进游戏。')

    async def test_opening_and_later_resume_each_send_one_lifecycle_message_and_keep_user_note(self):
        from orchestrator.button_prompts import notification
        first, second = await self.run_two_turns(initial_notice=True, later_notice=True,
            initial_user='保留用户本次任务。', return_all=True)
        self.assertEqual(first, 'fixture first turn\n\n保留用户本次任务。')
        self.assertEqual(second, notification('resume'))
        self.assertNotIn('自动续轮', second)


class ClaudeContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_and_preparation_fallbacks_reach_real_stdio_child(self):
        from calibration.lifecycle_test import STUB
        for preparing in (False, True):
            with self.subTest(preparing=preparing), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                captured = root/'sent.jsonl'
                stub = root/'stub.py'
                stub.write_text(STUB.replace(" text=p['message']['content']",
                    " text=p['message']['content']\n with open(sys.argv[1],'a',encoding='utf-8') as f: f.write(json.dumps(text,ensure_ascii=False)+'\\n')"), encoding='utf-8')
                cfg = RunConfig(agent='test', save='test', model='test', mode='explore')
                study = None
                if preparing:
                    kb = root/'kb'
                    kb.mkdir()
                    (kb/'README.md').write_text('unread line\n', encoding='utf-8')
                    study = KbStudy(kb, 10)
                runner = ClaudeRunner(cfg, root, SimpleNamespace(done=False, done_reason=''),
                    Metrics(root/'metrics.json', 'test', 'test', 'high', 60), 'stub-token',
                    container='unused', study=study, resume_note='fixture first turn')
                original = runner._command

                def command():
                    original()  # Keep production prompt and guide publication; replace only the child executable.
                    return [sys.executable, '-X', 'utf8', '-u', str(stub), str(captured)]

                with patch('orchestrator.config.REPO_ROOT', root), patch.object(runner, '_command', side_effect=command):
                    await asyncio.wait_for(runner.run(), 10)
                sent = [json.loads(line) for line in captured.read_text(encoding='utf-8').splitlines()]
                self.assertGreaterEqual(len(sent), 2)
                expected = controller_turn_prompt(study.reminder()) if study else continue_play_prompt(cfg)
                self.assertEqual(sent[1], expected)


if __name__ == '__main__':
    unittest.main()
