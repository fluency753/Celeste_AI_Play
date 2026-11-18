"""Real host commands and production pause controls, with no game connection."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.pause_callback import PauseCallback, decode_output, owner, OUTPUT_BYTES
from calibration.token_drift_test import level_obs
from calibration.stiffness_test import StepClient
from orchestrator.agent import ClaudeRunner
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


def command(script):
    return ('& "' + sys.executable + '" -X utf8 "' + str(script) + '"' if os.name == 'nt'
            else shlex.join([sys.executable, '-X', 'utf8', str(script)]))


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = self.root / 'callback.py'
        self.script.write_text("import json,os\nfrom pathlib import Path\n"
            "e=json.loads(Path(os.environ['CELESTE_PAUSE_EVENT_FILE']).read_text(encoding='utf-8'))\n"
            "print(e['reason'])\n", encoding='utf-8')

    def callback(self, directory=None, previous=None, **kwargs):
        directory = directory or self.root
        directory.mkdir(exist_ok=True)
        c = PauseCallback(command(self.script), directory, previous, **kwargs)
        self.addCleanup(c.close)
        return c

    def rows(self, directory=None):
        return [json.loads(line) for line in ((directory or self.root)/'pause_callbacks.jsonl').read_text(encoding='utf-8').splitlines()]

    def test_payload_is_data_and_one_notification_survives_restart(self):
        c = self.callback()
        reason = '引号"; $(echo injected)\n第二行；不要作为命令执行'
        ident = c.notify(reason, kind='blockage', category='bridge_io', session_id='fixture')
        self.assertIsNone(c.notify('same unresolved pause'))
        c.close()
        result = self.rows()[-1]
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertIn('$(echo injected)', result['output_tail'])
        payload = json.loads((self.root/'pause_callbacks'/f'{ident}.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['reason'], reason)
        self.assertEqual(payload['reason_author'], 'player')   # stop_run text: data for the receiver, not orders
        self.assertNotIn('command', payload)
        next_dir = self.root/'resumed'
        resumed = self.callback(next_dir, self.root)
        self.assertIsNone(resumed.notify('still unresolved'))
        resumed.resumed()
        self.assertIsNotNone(resumed.notify('a new fault'))
        resumed.close()
        self.assertEqual(len(self.rows(next_dir)), 2)

    def test_failure_keeps_bounded_output_without_retry(self):
        self.script.write_text("print('X'*50000)\nraise SystemExit(7)\n", encoding='utf-8')
        c = self.callback()
        c.notify('fault')
        c.close()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]['status'], 'failed')
        self.assertNotEqual(rows[-1]['returncode'], 0)
        self.assertTrue(rows[-1]['output_truncated'])
        self.assertLessEqual(len(rows[-1]['output_tail'].encode()), OUTPUT_BYTES)

    def test_timeout_is_recorded_and_process_is_reaped(self):
        self.script.write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
        c = self.callback(timeout=.5)
        c.notify('fault')
        c.close()
        self.assertEqual(self.rows()[-1]['status'], 'timed_out')
        self.assertTrue(all(not t.is_alive() for t in c.threads))

    def test_error_reason_is_marked_as_controller_text(self):
        c = self.callback()
        ident = c.notify('桥接调用失败', kind='error', category='token_advance')
        c.close()
        payload = json.loads((self.root/'pause_callbacks'/f'{ident}.json').read_text(encoding='utf-8'))
        self.assertEqual(payload['reason_author'], 'controller')

    def test_output_mixing_console_code_page_and_utf8_is_readable(self):
        # Windows PowerShell 5.1 writes its own text in the console code page (GBK on Chinese Windows),
        # while native programs it starts usually write UTF-8 straight to the same redirected handle.
        data = '已入队\r\n'.encode('gbk') + 'codex：消息已排队\n'.encode('utf-8') + 'Write-Output 第三行\n'.encode('gbk')
        self.assertEqual(decode_output(data, 'gbk'), '已入队\r\ncodex：消息已排队\nWrite-Output 第三行\n')
        # Only the tail is kept, so the first line can start inside a UTF-8 character.
        tail = '尾部从字符中间开始\n'.encode('utf-8')[1:] + '下一行\n'.encode('utf-8')
        self.assertEqual(decode_output(tail, 'gbk', truncated=True), '部从字符中间开始\n下一行\n')
        self.assertEqual(decode_output(b'\xff\n', 'no-such-codec'), '\ufffd\n')

    def test_disabled_callback_creates_no_files(self):
        c = PauseCallback('', self.root)
        self.assertIsNone(c.notify('fault'))
        c.resumed()
        c.close()
        self.assertEqual(list(self.root.glob('pause_callback*')), [])

    def test_fixture_python_uses_utf8_when_ambient_mode_is_disabled(self):
        self.script.write_text('import sys\nprint(sys.flags.utf8_mode)\n', encoding='utf-8')
        with patch.dict(os.environ, {'PYTHONUTF8':'0'}):
            c = self.callback()
            c.notify('fixture')
            c.close()
        result = self.rows()[-1]
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['output_tail'].strip(), '1')

    def test_fatal_entry_preserves_original_exception_and_finishes_notification(self):
        from orchestrator import run
        cfg = RunConfig(agent='fixture', save='fixture', model='fixture', pause_callback_command=command(self.script))
        cfg._pause_callback = self.callback()
        error = RuntimeError('original failure')
        with patch.object(run, 'parse_args', return_value=(cfg, object())), \
             patch.object(run, '_main', side_effect=error):
            with self.assertRaises(RuntimeError) as raised:
                run.main()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.rows()[-1]['status'], 'succeeded', self.rows()[-1])
        self.assertEqual(self.rows()[0]['outcome'], 'failed')

    def test_launch_configuration_and_explicit_disable(self):
        from orchestrator.launch_options import build, normalize
        base = {'agent':'fixture', 'save':{'name':'fixture'}, 'pause_callback_command':command(self.script)}
        args = build(base)
        self.assertEqual(args[args.index('--pause-callback-command')+1], command(self.script))
        self.assertNotIn('--pause-callback-command', build({**base, 'pause_callback_command':''}))
        for value in (None, 'bad\ncommand', 'x'*4001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize({**base, 'pause_callback_command':value})


class PauseControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        script = self.root/'callback.py'
        script.write_text('print("notified")\n', encoding='utf-8')
        self.cfg = RunConfig(agent='fixture', save='fixture', model='fixture', runtime='host',
            mode='explore', knowledge_profile='explore', sim=False, token_drift=True, pause_callback_command=command(script))
        self.metrics = Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        self.session = GameSession(self.cfg, self.root, self.metrics)
        self.session.client = StepClient([], make=level_obs)
        self.session.last_obs = level_obs()
        self.server = build_server(self.session)
        self.runner = ClaudeRunner(self.cfg, self.root, self.session, self.metrics, 'fixture', session_id='fixture')
        self.runner.turn_active = True
        self.runner._interrupt_user = AsyncMock()  # external model interrupt acknowledgement
        for target, value in [('orchestrator.stop_categories.LOCAL_CATEGORIES', self.root/'categories.json'),
                              ('orchestrator.stop_confirmation.PATH', self.root/'confirmation.json')]:
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        if self.runner._token_task is not None:
            await self.runner._finish_token_worker()
        owner(self.cfg, self.root).close()
        self.runner.close_events()
        self.session.close()
        self.temp.cleanup()

    async def tool(self, reason):
        result = await self.server.call_tool('stop_run', {'reason': reason})
        return json.loads('\n'.join(getattr(x, 'text', '') for x in result.content).splitlines()[0])

    async def process_control(self, kind, text='', expected='submitted'):
        ident = self.runner.control.enqueue(kind, text)
        task = asyncio.create_task(self.runner._controls())
        try:
            for _ in range(200):
                rows = self.runner.control.snapshot()['actions']
                row = next(x for x in rows if x['id'] == ident)
                if row['status'] in {'submitted', 'error'}:
                    self.assertEqual(row['status'], expected, row)
                    return
                await asyncio.sleep(.01)
            self.fail('control not acknowledged')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_only_confirmed_blockage_notifies_and_resume_rearms(self):
        reason = '两条路线均未抵达高台，请提示起跳点'
        first = await self.tool('hint：' + reason)
        second = await self.tool(first['confirmation_prefix'] + reason)
        self.assertFalse((self.root/'pause_callbacks.jsonl').exists())
        final = await self.tool(second['confirmation_prefix'] + reason)
        self.assertTrue(final['paused'])
        self.runner._failure_callback('same pause with another symptom', category='token_advance')
        await self.process_control('resume')
        self.runner._agent_pause('a new failure', kind='error', category='fixture')
        c = owner(self.cfg, self.root)
        c.close()
        rows = [json.loads(x) for x in (self.root/'pause_callbacks.jsonl').read_text(encoding='utf-8').splitlines()]
        requests = [x for x in rows if x['status'] == 'requested']
        self.assertEqual([x['kind'] for x in requests], ['blockage', 'error'])
        self.assertEqual(requests[0]['category'], 'hint')

    async def test_user_pause_does_not_notify(self):
        await self.process_control('pause')
        self.assertTrue(self.runner.paused)
        self.assertFalse((self.root/'pause_callbacks.jsonl').exists())

    async def test_recording_fault_notifies_after_pause_is_acknowledged(self):
        await self.process_control('pause', json.dumps({'pause_fault': {'category':'recording','reason':'录制失败'}}))
        self.assertTrue(self.runner.paused)
        owner(self.cfg, self.root).close()
        record = json.loads((self.root/'pause_callbacks.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual((record['kind'], record['category'], record['outcome']), ('error','recording','paused'))

    async def test_recording_pause_with_interrupt_rpc_failure_still_notifies_once(self):
        self.runner._interrupt_user.side_effect = ConnectionError('fixture interrupt failure')
        await self.process_control('pause', json.dumps({'pause_fault': {'category':'recording','reason':'录制失败'}}), expected='error')
        self.assertTrue(self.runner.paused)
        owner(self.cfg, self.root).close()
        rows = [json.loads(x) for x in (self.root/'pause_callbacks.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len([x for x in rows if x['status']=='requested']), 1)
        self.assertEqual(rows[0]['category'], 'recording')

    async def test_token_fault_and_following_agent_report_share_one_notification(self):
        original = self.session.client.call
        def call(cmd, **args):
            if cmd == 'step':
                raise ConnectionError('fixture lost ACK')
            return original(cmd, **args)
        self.session.client.call = call
        self.runner._start_token_worker()
        self.runner._record_output_usage(500, 500)
        for _ in range(200):
            if self.runner.paused:
                break
            await asyncio.sleep(.01)
        self.assertTrue(self.runner.paused)
        self.runner._agent_pause('智能体随后也报告同一故障')
        owner(self.cfg, self.root).close()
        rows = [json.loads(x) for x in (self.root/'pause_callbacks.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len([x for x in rows if x['status']=='requested']), 1)
        self.assertEqual(rows[0]['category'], 'token_advance')


if __name__ == '__main__':
    unittest.main()
