"""Live action caps preserve current action boundaries and resume configuration."""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.codex_agent import CodexRunner
from orchestrator.game_session import ActError, GameSession, PlanSpec
from orchestrator.metrics import Metrics
from orchestrator.modes import act_frame_cap
from orchestrator.tuning import apply
from orchestrator.server import build_server
from overlay.server import make_handler


class ActLimitTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = RunConfig(agent='test', save='test', model='test', runtime='host', mode='explore')
        self.metrics = Metrics(self.root/'metrics.json', 'test', 'test', 'high', 60)
        self.session = GameSession(self.cfg, self.root, self.metrics)
        self.addCleanup(self.session.close, release=False)
        self.state = self.root/'run_state.json'
        self.state.write_text(json.dumps({'agent': 'test', 'tuning': {}}), encoding='utf-8')

    def test_cap_changes_real_plan_validation_and_resumes_without_changing_prediction_limit(self):
        with self.assertRaisesRegex(ActError, '最多推进 150'):
            self.session._check_plan(PlanSpec(['200,R']))
        result = self.session.update_act_limit({'frames': 300})
        self.assertEqual(result['frames'], 300)
        self.assertEqual(len(self.session._check_plan(PlanSpec(['200,R']))), 200)
        self.assertEqual(self.cfg.explore_search_frames, 150)
        state = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(state['agent'], 'test')
        self.assertEqual(state['tuning'], {'explore_act_frames': 300})
        resumed = RunConfig(agent='test', save='test', model='test', mode='explore')
        apply(resumed, state['tuning'])
        self.assertEqual(act_frame_cap(resumed), 300)
        self.session.update_act_limit({'frames': 100})
        with self.assertRaisesRegex(ActError, '最多推进 100'):
            self.session._check_plan(PlanSpec(['200,R']))

    def test_speedrun_mode_only_changes_its_own_cap_and_restoring_default_removes_override(self):
        self.cfg.mode = 'speedrun'
        self.session.update_act_limit({'frames': 900})
        self.assertEqual(self.cfg.max_frames_per_act, 900)
        self.assertEqual(self.cfg.explore_act_frames, 150)
        self.assertEqual(self.session.act_limit_settings()['min'], 60)
        self.session.update_act_limit({'frames': 600})
        self.assertEqual(json.loads(self.state.read_text())['tuning'], {})

    def test_invalid_values_and_persistence_failure_do_not_change_effective_cap(self):
        before = self.state.read_bytes()
        for data in ({'frames': 29}, {'frames': 3601}, {'frames': True}, {'frames': 150.5},
                     {'frames': '300'}, {}, {'frames': 300, 'other': 1}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.session.update_act_limit(data)
        with patch.object(Path, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaisesRegex(OSError, 'disk unavailable'):
                self.session.update_act_limit({'frames': 300})
        self.assertEqual(act_frame_cap(self.cfg), 150)
        self.assertEqual(self.cfg.tuning, {})
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.state.with_suffix('.act-limit.tmp').exists())

    def test_same_value_is_idempotent_and_stopped_session_rejects_update(self):
        before = self.state.stat().st_mtime_ns
        self.assertFalse(self.session.update_act_limit({'frames': 150})['changed'])
        self.assertEqual(self.state.stat().st_mtime_ns, before)
        self.session.done = True
        with self.assertRaisesRegex(ActError, '试玩已结束'):
            self.session.update_act_limit({'frames': 300})

    def test_control_accepts_only_integer_frames_payload(self):
        control = RunControl(self.root)
        ident = control.enqueue('act_limit', json.dumps({'frames': 300}))
        self.assertEqual(control.pending()[0]['id'], ident)
        for text in ('{}', '{"frames":true}', '{"frames":300.5}', '{"frames":300,"key":"explore_act_frames"}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                control.enqueue('act_limit', text)

    def test_observe_reports_current_range_even_when_tool_was_registered_with_old_cap(self):
        self.cfg.collision_image = 'off'
        obs = {'frame': 5, 'scene': 'Level', 'level': {'sid': 'Celeste/1-ForsakenCity', 'room': 'test',
               'bounds': [0, 0, 320, 180], 'camera': [0, 0], 'chapterTimeTicks': 0, 'deaths': 0, 'deathsInRoom': 0},
               'player': None, 'entities': [], 'exits': []}
        self.session.observe = lambda: (obs, None)
        server = build_server(self.session)
        self.session.update_act_limit({'frames': 300})
        result = asyncio.run(server.call_tool('observe', {}))
        text = '\n'.join(item.text for item in result.content if getattr(item, 'type', None) == 'text')
        self.assertIn('当前 act 帧数范围：40～300', text)

    def test_real_http_endpoint_checks_capability_and_queues_payload(self):
        control = RunControl(self.root)
        control.heartbeat(connected=True, act_limit_control=False)
        http = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.root))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def post():
            request = urllib.request.Request(f'http://127.0.0.1:{http.server_port}/api/control',
                data=json.dumps({'run': str(self.root), 'kind': 'act_limit', 'text': '{"frames":300}'}).encode(),
                headers={'Content-Type': 'application/json'})
            return opener.open(request)
        try:
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                post()
            self.assertEqual(rejected.exception.code, 400)
            self.assertIn('在线动作帧数设置', rejected.exception.read().decode())
            rejected.exception.close()
            self.assertFalse(control.pending())
            control.heartbeat(connected=True, act_limit_control=True)
            with post() as response:
                ident = json.load(response)['id']
            self.assertEqual(control.pending()[0]['id'], ident)
            self.assertEqual(control.pending()[0]['kind'], 'act_limit')
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


class ActLimitControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_waits_for_action_boundary_then_publishes_canonical_settings_and_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent='test', save='test', model='test', runtime='host', mode='explore')
            metrics = Metrics(root/'metrics.json', 'test', 'test', 'high', 60)
            session = GameSession(cfg, root, metrics)
            runner = CodexRunner(cfg, root, session, metrics, '')
            task = None
            try:
                await runner.action_lock.acquire()
                ident = runner.control.enqueue('act_limit', '{"frames":300}')
                task = asyncio.create_task(runner._controls())
                await asyncio.sleep(.05)
                self.assertEqual(act_frame_cap(cfg), 150)
                runner.action_lock.release()
                async with asyncio.timeout(3):
                    while True:
                        row = next(row for row in runner.control.snapshot()['actions'] if row['id'] == ident)
                        if row['status'] in {'submitted', 'error'}:
                            break
                        await asyncio.sleep(.01)
                self.assertEqual(row['status'], 'submitted', row)
                self.assertEqual(runner._act_limit_status()['act_limit_settings']['frames'], 300)
                self.assertEqual(json.loads((root/'run_state.json').read_text())['tuning'], {'explore_act_frames': 300})
                self.assertEqual(len(runner.pending_messages), 1)
                self.assertTrue(runner.pending_messages[0]['next_turn'])
                self.assertIn('300', runner.pending_messages[0]['text'])
                self.assertIn('旧工具说明', runner.pending_messages[0]['text'])
                runner.chat_only = True
                self.assertFalse(runner._act_limit_status()['act_limit_control'])
            finally:
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                session.close(release=False)


if __name__ == '__main__':
    unittest.main()
