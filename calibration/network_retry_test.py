"""Provider retry events preserve turn/tool ownership and stop thinking time."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from overlay.modhud import view_model


ERROR = {'error': {'message':'Reconnecting... waiting for network',
                  'codexErrorInfo':{'responseStreamDisconnected':{'httpStatusCode':None}},
                  'additionalDetails':'Connection failed: error sending request'},
         'willRetry':True, 'threadId':'session', 'turnId':'turn'}


class NetworkRetryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.now = 100.0
        self.stack.enter_context(patch('time.monotonic', side_effect=lambda:self.now))
        self.stack.enter_context(patch('time.time', side_effect=lambda:1000 + self.now))
        self.m = Metrics(self.root/'metrics.json', 'fixture', 'fixture', 'medium', 600)
        self.cfg = RunConfig(agent='fixture', save='fixture', model='fixture', engine='codex', sim=False)
        self.r = CodexRunner(self.cfg, self.root, None, self.m, 'no-container')
        self.r.session_id, self.r.turn_id, self.r.turn_active = 'session', 'turn', True
        self.m.turn_begin()
        self.m.update(status='thinking')

    def advance(self, seconds):
        self.now += seconds

    def event(self, method, **params):
        self.r._event(method, {'threadId':'session', 'turnId':'turn', **params})

    def retry(self):
        self.r._event('error', ERROR)

    def test_retry_is_immediately_visible_and_only_model_progress_restores_thinking(self):
        self.advance(5)
        self.retry()
        self.assertEqual(self.m.state.thinking_s, 5)
        self.assertTrue(self.m._in_turn)
        start = self.m.state.network_retry_since_wall
        self.advance(30)
        self.retry()
        self.assertEqual(self.m.state.network_retry_episodes, 1)
        self.assertEqual(self.m.state.network_retry_since_wall, start)
        self.assertEqual(self.m.snapshot()['status'], 'net_wait')
        self.assertEqual(json.loads((self.root/'metrics.json').read_text())['status'], 'net_wait')
        self.event('item/commandExecution/outputDelta', itemId='old-shell', delta='local output')
        self.event('item/completed', item={'id':'old-shell', 'type':'commandExecution', 'status':'completed'})
        self.assertTrue(self.m.state.network_retrying)
        self.advance(15)
        self.event('item/agentMessage/delta', itemId='public', delta='继续观察。')
        self.assertFalse(self.m.state.network_retrying)
        self.assertEqual((self.m.state.thinking_s, self.m.state.network_retry_s), (5, 45))
        self.advance(3)
        self.m.turn_end()
        self.assertEqual(self.m.state.thinking_s, 8)
        rows = [json.loads(line) for line in (self.root/'decision_timing.jsonl').read_text().splitlines()]
        self.assertEqual([r['event'] for r in rows].count('network_retry_started'), 1)
        self.assertEqual(next(r for r in rows if r['event']=='network_retry_ended')['outcome'], 'model_progress')

    def test_running_tool_is_preserved_and_counted_during_retry(self):
        self.m.tool_begin('shell', label='Bash')
        self.advance(2)
        self.retry()
        self.advance(10)
        self.m.network_retry_begin('still retrying')
        snap = self.m.snapshot()
        self.assertTrue(self.m.tools_running)
        self.assertEqual((snap['status'], snap['activity_status']), ('net_wait', 'tool'))
        self.assertEqual((snap['thinking_s'], snap['tool_s']), (0, 12))
        vm = view_model(snap, {}, [], 1000+self.now+5)
        self.assertIn('连接', vm.status)
        self.assertEqual(next(value for key, value, *_ in vm.stats if key=='工具'), '0:00:17')
        self.m.tool_end('shell')
        self.advance(5)
        self.m.network_retry_end()
        self.assertEqual((self.m.state.thinking_s, self.m.state.tool_s, self.m.state.network_retry_s), (0, 12, 15))

    def test_unrelated_or_stale_events_cannot_change_the_current_retry(self):
        self.r._event('error', {**ERROR, 'threadId':'other'})
        self.assertFalse(self.m.state.network_retrying)
        self.event('thread/tokenUsage/updated', tokenUsage={'total':{'totalTokens':99}})
        self.retry()
        self.r._event('item/agentMessage/delta', {'threadId':'session', 'turnId':'old', 'delta':'late'})
        self.r._event('item/agentMessage/delta', {'threadId':'other', 'turnId':'turn', 'delta':'other'})
        self.assertTrue(self.m.state.network_retrying)
        self.event('thread/tokenUsage/updated', tokenUsage={'total':{'totalTokens':100}})
        self.assertFalse(self.m.state.network_retrying)
        self.retry()
        self.event('thread/tokenUsage/updated', tokenUsage={'total':{'totalTokens':100}})
        self.assertTrue(self.m.state.network_retrying)
        self.event('thread/tokenUsage/updated', tokenUsage={'total':{'totalTokens':101}})
        self.assertFalse(self.m.state.network_retrying)

    def test_first_usage_snapshot_is_only_a_baseline_not_proof_of_recovery(self):
        self.retry()
        self.event('thread/tokenUsage/updated', tokenUsage={'total':{'totalTokens':100}})
        self.assertTrue(self.m.state.network_retrying)
        self.event('thread/tokenUsage/updated', tokenUsage={'total':{'totalTokens':101}})
        self.assertFalse(self.m.state.network_retrying)

    def test_pause_and_turn_end_settle_retry_without_reviving_it_on_resume(self):
        self.advance(5)
        self.retry()
        self.advance(7)
        self.m.pause(status='paused')
        self.assertEqual(self.m.snapshot()['status'], 'paused')
        self.assertEqual(self.m.state.network_retry_s, 7)
        self.advance(30)
        self.m.resume(quota=False)
        self.assertFalse(self.m.state.network_retrying)
        self.m.turn_begin()
        self.retry()
        self.advance(4)
        self.event('turn/completed', turn={'id':'turn', 'status':'interrupted'})
        self.m.turn_end()
        self.assertEqual((self.m.state.thinking_s, self.m.state.network_retry_s), (5, 11))
        self.r._event('error', ERROR)
        self.assertFalse(self.m.state.network_retrying)

    def test_wait_setting_does_not_hide_cli_internal_retries(self):
        self.cfg.net_wait = False
        self.retry()
        self.assertEqual(self.m.snapshot()['status'], 'net_wait')
        self.assertTrue(self.r.turn_active)

    def test_authentication_and_nonretryable_errors_are_not_network_waits(self):
        for value in ({**ERROR, 'willRetry':False},
                      {**ERROR, 'error':{'message':'HTTP 401 Unauthorized'}}):
            self.r._event('error', value)
            self.assertFalse(self.m.state.network_retrying)

    def test_claude_short_retry_ends_on_actual_stream_response(self):
        cfg = RunConfig(agent='fixture', save='fixture', model='fixture', sim=False)
        r = ClaudeRunner(cfg, self.root, SimpleNamespace(done=False), self.m, 'no-token', naming_only=True)
        self.addCleanup(r.close_events)
        r.turn_active = True
        self.advance(2)
        r._handle({'type':'system', 'subtype':'api_retry', 'error_status':503,
                   'error':'network connection reset', 'attempt':1})
        self.assertEqual(self.m.snapshot()['status'], 'net_wait')
        self.advance(9)
        r._handle({'type':'stream_event', 'event':{'type':'message_start',
                  'message':{'id':'new-response', 'model':'fixture', 'usage':{}}}})
        self.assertFalse(self.m.state.network_retrying)
        self.assertEqual((self.m.state.thinking_s, self.m.state.network_retry_s), (2, 9))

    def test_restore_keeps_totals_but_not_live_retry_state(self):
        self.retry()
        self.advance(9)
        self.m.network_retry_end('turn_ended')
        saved = self.m.snapshot()
        m = Metrics(self.root/'next.json', 'fixture', 'fixture', 'medium', 600)
        m.restore({**saved, 'network_retrying':True}, 'previous')
        self.assertEqual(m.state.network_retry_s, 9)
        self.assertEqual(m.state.network_retry_episodes, 1)
        self.assertFalse(m.state.network_retrying)

    def test_late_retry_after_pause_does_not_poison_a_new_claude_turn(self):
        cfg = RunConfig(agent='fixture', save='fixture', model='fixture', sim=False)
        r = ClaudeRunner(cfg, self.root, SimpleNamespace(done=False), self.m, 'no-token', naming_only=True)
        self.addCleanup(r.close_events)
        r.turn_active = True
        self.m.pause(status='paused')
        r._net_trouble(503, 'late network failure', give_up=False)
        self.assertIsNone(r.net_down_since)
        self.assertFalse(self.m.state.network_retrying)
        self.m.resume(quota=False)
        self.m.turn_begin()
        self.advance(4)
        self.m.turn_end()
        self.assertEqual(self.m.state.thinking_s, 4)

    def test_claude_formal_wait_does_not_subtract_already_excluded_thinking(self):
        self.advance(5)
        since = self.now
        self.retry()
        self.advance(15)
        self.m.turn_end()
        self.m.pause(status='net_wait', since=since, thinking_already_excluded=True)
        self.assertEqual((self.m.elapsed(), self.m.state.thinking_s), (5, 5))
        self.advance(10)
        self.m.resume(net=True)
        self.assertEqual((self.m.state.network_retry_s, self.m.state.net_wait_s), (15, 25))

    def test_other_thread_turn_end_does_not_report_network_recovery(self):
        self.m.tool_begin('shell', label='Bash')
        self.retry()
        self.r._event('turn/completed', {'threadId':'other', 'turn':{'id':'turn', 'status':'completed'}})
        self.r._event('turn/completed', {'threadId':'session', 'turn':{'id':'old', 'status':'completed'}})
        self.assertTrue(self.m.state.network_retrying)
        self.assertFalse(self.r.turn_done.is_set())
        self.assertTrue(self.m.tools_running)


if __name__ == '__main__':
    unittest.main()
