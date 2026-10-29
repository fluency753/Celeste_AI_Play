"""消息投递、暂停在途动作与恢复的契约验收，不连接游戏或模型。"""
import asyncio
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.types import CallToolResult
from orchestrator.control import RunControl
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from overlay.server import make_handler


class ControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent="control-test", save="test", model="model", live=True)
        self.metrics = Metrics(self.root / "metrics.json", "control-test", "model", "medium", 600)
        self.server = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[])))
        self.runner = CodexRunner(self.cfg, self.root, None, self.metrics, "container", game_server=self.server)
        self.runner.session_id = "thread"
        self.runner.turn_id = "turn"
        self.runner.turn_active = True
        self.runner.rpc.request = AsyncMock(return_value={})
        self.task = None

    async def asyncTearDown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.temp.cleanup()

    async def wait_for(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(.02)

    async def test_idle_effort_change_waits_for_next_turn_without_calling_model(self):
        self.runner.turn_active=False;self.runner.turn_id=None
        with patch('orchestrator.model_catalog.resolve_selection',return_value=('model','high')):
            self.runner.control.enqueue('effort','high')
            self.task=asyncio.create_task(self.runner._controls())
            await self.wait_for(lambda:self.runner.manual_effort=='high')
        self.assertEqual(self.metrics.state.effort,'medium')
        self.assertEqual(self.metrics.state.effort_pending,'high')
        self.assertEqual(RunControl(self.root).effort_preference(),'high')
        self.runner.rpc.request.assert_not_called()
        self.runner._record_turn_effort('high','high')
        self.assertEqual(self.metrics.state.effort,'high')
        self.assertIsNone(self.metrics.state.effort_pending)

    async def test_free_roam_rejects_auto_mode_without_changing_config_or_preferences(self):
        self.cfg.play_scope = 'visited'
        self.cfg.completion_policy = 'continue'
        self.cfg.cutscene_mode = 'agent'
        self.runner.turn_active = False
        self.runner.turn_id = None
        self.runner.control.set_cutscene_mode('agent')
        self.runner.session = SimpleNamespace(done=False, cutscene_tool=True, cfg=self.cfg,
            goals=SimpleNamespace(check=lambda values: False, snapshot=lambda: None), goal_values=lambda: {})
        ident = self.runner.control.enqueue('cutscene_mode', 'auto')
        self.task = asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda: not self.runner.control.pending())
        row = next(r for r in self.runner.control.snapshot()['actions'] if r['id'] == ident)
        self.assertEqual(row['status'], 'error')
        self.assertIn('自由探索', row['detail'])
        self.assertEqual(self.cfg.cutscene_mode, 'agent')
        self.assertEqual(self.runner.control.cutscene_preference(), 'agent')

    async def test_codex_catalog_effort_is_not_blocked_by_shared_controls(self):
        self.runner.turn_active=False;self.runner.turn_id=None
        with patch('orchestrator.model_catalog.load_models',return_value={
            'default_model':'model', 'models':[{'id':'model', 'efforts':['minimal','ultra']}]}):
            self.runner.control.enqueue('effort','ultra')
            self.task=asyncio.create_task(self.runner._controls())
            await self.wait_for(lambda:self.runner.manual_effort=='ultra')
        self.assertEqual(self.metrics.state.effort_pending,'ultra')
        self.assertEqual(RunControl(self.root).effort_preference(),'ultra')
        self.runner.rpc.request.assert_not_called()

    async def test_active_effort_change_steers_without_interrupting_or_waiting_for_action(self):
        with patch('orchestrator.model_catalog.resolve_selection',return_value=('model','low')):
            async with self.runner.action_lock:
                self.runner.control.enqueue('effort','low')
                self.task=asyncio.create_task(self.runner._controls())
                await self.wait_for(lambda:self.runner.rpc.request.await_count>0)
        self.assertEqual(self.runner.rpc.request.await_args.args[0],'turn/steer')
        self.assertFalse(self.runner.paused)
        self.assertNotIn('turn/interrupt',[call.args[0] for call in self.runner.rpc.request.await_args_list])
        self.assertEqual(self.metrics.state.effort,'medium')
        self.assertEqual(self.metrics.state.effort_pending,'low')

    async def test_manual_effort_blocks_agent_changes_until_auto_is_restored(self):
        self.runner.manual_effort='high';self.runner.control.set_effort('high')
        denied=await self.runner._request('item/tool/call',{'tool':'celeste_set_effort','arguments':{'level':'low'}})
        self.assertFalse(denied['success']);self.server.call_tool.assert_not_called()
        self.runner.turn_active=False;self.runner.turn_id=None
        self.runner.control.enqueue('effort','auto')
        self.task=asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda:self.runner.manual_effort is None)
        self.assertIsNone(self.runner.control.effort_preference())
        allowed=await self.runner._request('item/tool/call',{'tool':'celeste_set_effort','arguments':{'level':'low'}})
        self.assertTrue(allowed['success'])

    async def test_new_manual_choice_survives_inflight_turn_start(self):
        self.metrics.update(effort='high');self.runner.manual_effort='high'
        self.metrics.request_effort(None)
        self.runner._record_turn_effort('low','low')
        self.assertEqual(self.metrics.state.effort,'low')
        self.assertEqual(self.metrics.state.effort_pending,'high')

    async def test_pause_waits_for_inflight_action_and_blocks_next_action(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def action(*args):
            entered.set()
            await release.wait()
            return CallToolResult(content=[])
        self.server.call_tool.side_effect = action
        first = asyncio.create_task(self.runner._request("item/tool/call", {"tool": "celeste_act", "arguments": {}}))
        await entered.wait()
        ident = self.runner.control.enqueue("pause")
        self.task = asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda: self.runner.paused)
        self.assertEqual(self.runner.control.pending()[0]["id"], ident)
        denied = await self.runner._request("item/tool/call", {"tool": "celeste_act", "arguments": {}})
        self.assertFalse(denied["success"])
        self.assertEqual(self.server.call_tool.call_count, 1)
        release.set()
        await first
        await self.wait_for(lambda: not self.runner.control.pending())
        self.assertTrue(self.runner.control.snapshot()["paused"])
        self.assertEqual(self.metrics.state.status, "paused")

    async def test_user_message_steers_same_thread_while_paused(self):
        self.runner.paused = True
        self.runner.control.enqueue("message", "解释一下你卡在哪里")
        self.task = asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda: self.runner.control.snapshot()["messages"][0]["status"] == "submitted")
        call = self.runner.rpc.request.call_args
        self.assertEqual(call.args[0], "turn/steer")
        self.assertEqual(call.args[1]["threadId"], "thread")
        self.assertEqual(call.args[1]["expectedTurnId"], "turn")
        self.assertIn("解释一下", call.args[1]["input"][0]["text"])
        self.server.call_tool.assert_not_called()

    async def test_resume_does_not_count_as_quota_wait(self):
        self.runner.paused = True
        self.runner.pause_at = __import__('time').monotonic()
        self.metrics.pause()
        self.runner.control.enqueue("resume")
        self.task = asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda: not self.runner.paused)
        self.assertEqual(self.metrics.state.quota_waits, 0)
        self.assertEqual(self.metrics.state.waited_s, 0)

    async def test_chat_only_cannot_resume_or_access_game(self):
        self.runner.chat_only = True
        self.runner.paused = True
        self.runner.control.enqueue("resume")
        self.task = asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda: not self.runner.control.pending())
        self.assertTrue(self.runner.paused)
        self.assertEqual(self.runner.control.snapshot()["actions"][0]["status"], "error")
        for name in ("celeste_act", "celeste_observe"):
            result = await self.runner._request("item/tool/call", {"tool": name, "arguments": {}})
            self.assertFalse(result["success"])
            self.assertIn("试玩已经结束", result["contentItems"][0]["text"])
        from orchestrator.codex_native import native_tool_hook
        self.metrics.update(tool_calls=0)
        denied = await native_tool_hook(self.runner, {'token':self.runner.native_token,
            'hook_event_name':'PreToolUse', 'tool_name':'Bash', 'tool_use_id':'chat-plan',
            'session_id':'thread', 'turn_id':'turn', 'tool_input':{'command':'python planner.py'}})
        self.assertEqual(denied['decision'], 'deny')
        self.assertIn('只读聊天', denied['reason'])
        self.assertFalse(self.metrics.tools_running)
        self.server.call_tool.assert_not_called()

    async def test_fast_toggle_persists_without_interrupting_active_reply(self):
        self.runner.fast_mode = False
        self.runner.turn_fast = False
        self.runner.control.enqueue("fast", "on")
        self.task = asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda: not self.runner.control.pending())
        self.assertTrue(self.runner.fast_mode)
        self.assertTrue(RunControl(self.root).fast_preference())
        self.assertTrue(self.runner.control.snapshot()["fast_pending"])
        self.assertEqual(self.runner.rpc.request.await_args.args[0],'turn/steer')
        self.assertNotIn('turn/interrupt',[call.args[0] for call in self.runner.rpc.request.await_args_list])
        with self.assertRaises(ValueError):
            self.runner.control.enqueue("fast", "unexpected")

    async def test_fast_change_does_not_wait_for_action_lock_and_pending_clears_next_turn(self):
        self.runner.fast_mode=False;self.runner.turn_fast=False
        async with self.runner.action_lock:
            self.runner.control.enqueue('fast','on')
            self.task=asyncio.create_task(self.runner._controls())
            await self.wait_for(lambda:self.runner.rpc.request.await_count==1)
        self.assertEqual(self.runner.rpc.request.await_args.args[0],'turn/steer')
        self.runner.turn_fast=True;self.runner.turn_id='next-turn'
        await self.wait_for(lambda:not self.runner.control.snapshot().get('fast_pending'))
        self.assertEqual(self.runner.rpc.request.await_count,1)

    async def test_fast_off_requests_natural_turn_end_and_deduplicates(self):
        self.runner.fast_mode=True;self.runner.turn_fast=True
        self.runner.control.enqueue('fast','off')
        self.task=asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda:self.runner.rpc.request.await_count==1)
        self.assertFalse(self.runner.fast_mode)
        self.assertTrue(self.runner.control.snapshot()['fast_pending'])
        await self.runner._yield_for_fast_change()
        self.assertEqual(self.runner.rpc.request.await_count,1)
        self.assertEqual(self.runner.rpc.request.await_args.args[0],'turn/steer')
        self.runner.turn_fast=False;self.runner.turn_id='next-turn'
        await self.wait_for(lambda:not self.runner.control.snapshot().get('fast_pending'))

    async def test_fast_changed_during_turn_start_steers_after_acknowledgment(self):
        self.runner.fast_mode=False;self.runner.turn_fast=False;self.runner.turn_id=None
        self.runner.control.enqueue('fast','on')
        self.task=asyncio.create_task(self.runner._controls())
        await self.wait_for(lambda:not self.runner.control.pending())
        self.runner.rpc.request.assert_not_called()
        self.runner.turn_id='acknowledged-turn'
        await self.wait_for(lambda:self.runner.rpc.request.await_count==1)
        self.assertEqual(self.runner.rpc.request.await_args.args[1]['expectedTurnId'],'acknowledged-turn')

    def test_disconnected_controller_never_reports_fast_pending(self):
        self.runner.control.heartbeat(stopped=True,fast_pending=True,fast=False)
        self.assertFalse(self.runner.control.snapshot()['fast_pending'])

    def test_wake_requires_completed_native_run(self):
        from orchestrator.chat import can_wake
        self.assertFalse(can_wake(self.root))
        (self.root / "run_state.json").write_text(json.dumps({"engine":"codex", "session_id":"thread"}))
        (self.root / "summary.json").write_text(json.dumps({"completed":False}))
        self.assertFalse(can_wake(self.root))
        (self.root / "summary.json").write_text(json.dumps({"completed":True}))
        self.assertTrue(can_wake(self.root))

    def test_server_rejects_wrong_run_and_cross_site_posts(self):
        control = RunControl(self.root)
        control.heartbeat(paused=True)
        http = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.root))
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            for run, origin in (("wrong-run", "http://127.0.0.1"), (str(self.root), "https://evil.example")):
                req = urllib.request.Request(f"http://127.0.0.1:{http.server_port}/api/control",
                    data=json.dumps({"run": run, "kind": "message", "text": "hello"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(req)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            self.assertEqual(control.pending(), [])
        finally:
            http.shutdown()
            http.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
