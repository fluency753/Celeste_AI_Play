"""原生 GPT 执行器的契约测试，不连接游戏或调用模型。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.types import CallToolResult
from orchestrator.codex_agent import CodexRunner, public_event
from orchestrator.prepared import load_preparation
from orchestrator.config import RunConfig
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.sandbox import ProgramResult


class NativeAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.kb = self.root / "kb"
        self.kb.mkdir()
        (self.kb / "README.md").write_text("one\ntwo\n", encoding="utf-8")
        self.cfg = RunConfig(agent="native-test", save="test", model="test-model", engine="codex", live=True)
        self.study = KbStudy(self.kb, 10)
        self.metrics = Metrics(self.root / "metrics.json", "native-test", "test-model", "medium", 600, study=True)
        self.server = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult.model_validate({
            "content": [{"type": "text", "text": "observed"},
                        {"type": "image", "mimeType": "image/png", "data": "AAAA"}]})))
        self.runner = CodexRunner(self.cfg, self.root, None, self.metrics, "container", self.study,
                                  game_server=self.server)
        self.runner.session_id, self.runner.turn_id, self.runner.turn_active = 'session', 'turn', True
        self.runner.rpc.request = AsyncMock(return_value={'turnId': 'turn'})

    def tearDown(self):
        self.temp.cleanup()

    async def read(self, offset):
        result = {"path": "/kb/README.md", "startLine": offset, "numLines": 1,
                  "totalLines": 2, "text": "one" if offset == 1 else "two"}
        with patch("orchestrator.codex_agent.workspace_call", return_value=result):
            return await self.runner._request("item/tool/call", {"callId": str(offset), "tool": "celeste_read",
                                                                 "arguments": {"path": "/kb/README.md", "offset": offset}})

    async def test_interrupt_cancels_codex_turn_and_waits_for_active_action(self):
        runner = self.runner
        runner.session = SimpleNamespace(done=False)
        runner.session_id, runner.turn_id, runner.turn_active = 'session', 'turn', True
        runner.rpc.request = AsyncMock(return_value={})
        runner._check_goal = AsyncMock()
        await runner.action_lock.acquire()
        ident = runner.control.enqueue('interrupt')
        task = asyncio.create_task(runner._controls())
        try:
            for _ in range(30):
                if runner.interrupted_for_resume:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(runner.interrupted_for_resume)
            self.assertTrue(runner.stop_requested)
            runner.rpc.request.assert_awaited_once_with('turn/interrupt', {'threadId': 'session', 'turnId': 'turn'})
            self.assertIn('中断', runner._game_guard())
            row = lambda: next(a for a in runner.control.snapshot()['actions'] if a['id'] == ident)
            self.assertNotEqual(row()['status'], 'submitted')
            stopping = asyncio.create_task(runner._stopped_reason())
            await asyncio.sleep(.01)
            self.assertFalse(stopping.done())
            runner.action_lock.release()
            for _ in range(30):
                if row()['status'] == 'submitted':
                    break
                await asyncio.sleep(.01)
            self.assertEqual(row()['status'], 'submitted')
            self.assertIn('用户中断', await asyncio.wait_for(stopping, 1))
            self.assertTrue(runner._effort_status()['interrupt_control'])
        finally:
            if runner.action_lock.locked():
                runner.action_lock.release()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_chat_detach_keeps_its_existing_stop_reason(self):
        self.assertEqual(await self.runner._stopped_reason(), '聊天会话切换')

    async def test_read_audit_gates_game_then_forwards_text_and_image(self):
        await self.read(1)
        self.assertEqual(self.study.read["README.md"], {1})
        denied = await self.runner._request("item/tool/call", {"tool": "celeste_observe", "arguments": {}})
        self.assertFalse(denied["success"])
        self.server.call_tool.assert_not_called()
        await self.read(2)
        self.assertTrue(self.study.complete())
        allowed = await self.runner._request("item/tool/call", {"tool": "celeste_observe", "arguments": {}})
        self.assertTrue(allowed["success"])
        self.assertEqual(allowed["contentItems"][0]["text"], "observed")
        self.assertNotIn('data:image/', json.dumps(allowed))
        attachment = self.runner.rpc.request.call_args.args[1]
        self.assertEqual(attachment['input'][1], {'type': 'image', 'url': 'data:image/png;base64,AAAA'})
        self.assertEqual(attachment['expectedTurnId'], 'turn')

    async def test_stop_run_can_report_blockage_while_paused_or_study_incomplete(self):
        self.server.call_tool.return_value = CallToolResult.model_validate({'content': [{'type': 'text', 'text': 'paused'}]})
        self.runner.paused=True
        self.runner.gate=SimpleNamespace(enabled=True,decide=AsyncMock(return_value=(False,'无额度')))
        result=await self.runner._request('item/tool/call',{'tool':'celeste_stop_run','arguments':{'reason':'工具故障'}})
        self.assertTrue(result['success'])
        self.server.call_tool.assert_awaited_with('stop_run',{'reason':'工具故障'})
        self.runner.gate.decide.assert_not_awaited()
        self.runner.chat_only=True
        denied=await self.runner._request('item/tool/call',{'tool':'celeste_stop_run','arguments':{'reason':'工具故障'}})
        self.assertFalse(denied['success'])

    async def test_exit_tool_is_forwarded_and_denied_when_paused_or_chat_only(self):
        self.runner.study=None
        result=await self.runner._request('item/tool/call',{'tool':'celeste_exit_level','arguments':{'reason':'用户要求退出'}})
        self.assertTrue(result['success'])
        self.server.call_tool.assert_awaited_with('exit_level',{'reason':'用户要求退出'})
        for paused,chat_only in [(True,False),(False,True)]:
            self.runner.paused,self.runner.chat_only=paused,chat_only
            self.server.call_tool.reset_mock()
            denied=await self.runner._request('item/tool/call',{'tool':'celeste_exit_level','arguments':{'reason':'退出'}})
            self.assertFalse(denied['success']);self.server.call_tool.assert_not_awaited()

    async def test_exit_tool_never_uses_container_shell(self):
        self.runner.study=None
        self.runner.session=SimpleNamespace()
        with patch('orchestrator.codex_agent.run_program') as shell:
            result=await self.runner._request('item/tool/call',{'tool':'celeste_exit_level',
                'arguments':{'reason':'按用户要求退出'}})
        self.assertTrue(result['success'])
        shell.assert_not_called()
        self.server.call_tool.assert_awaited_with('exit_level',{'reason':'按用户要求退出'})

    async def test_workspace_chat_can_run_sandbox_programs_but_game_tools_stay_closed(self):
        self.runner.chat_only = self.runner.paused = True
        self.runner.allow_programs = True
        with patch('orchestrator.codex_agent.run_program', return_value=ProgramResult(0, 'offline', '', .1, False)) as shell:
            result = await self.runner._request('item/tool/call', {
                'tool': 'celeste_bash', 'arguments': {'command': 'python offline.py'}})
            self.assertTrue(result['success'])
            shell.assert_called_once()
            for name in ('celeste_act', 'celeste_observe', 'celeste_simulate', 'celeste_exit_level', 'celeste_cutscene'):
                denied = await self.runner._request('item/tool/call', {'tool': name, 'arguments': {}})
                self.assertFalse(denied['success'])
            self.server.call_tool.assert_not_awaited()
        self.runner.allow_programs = False
        with patch('orchestrator.codex_agent.run_program') as shell:
            denied = await self.runner._request('item/tool/call', {
                'tool': 'celeste_bash', 'arguments': {'command': 'python offline.py'}})
            self.assertFalse(denied['success'])
            shell.assert_not_called()

    async def test_writer_conflict_forks_history_and_records_recovery(self):
        expected = {"thread": {"id": "recovered"}}
        self.runner.rpc.request = AsyncMock(side_effect=[
            RuntimeError("thread original already has an active writer"), expected])
        params = {"threadId": "original", "sandbox": "read-only", "selectedCapabilityRoots": []}
        self.assertEqual(await self.runner._resume_thread(params), expected)
        self.runner.rpc.request.assert_awaited_with("thread/fork", {
            "threadId": "original", "sandbox": "read-only", "excludeTurns": True})
        self.assertEqual(json.loads((self.root / "session_recovery.json").read_text())["thread"], "recovered")

    async def test_cutscene_tool_returns_screenshot_alongside_text(self):
        self.runner.study=None
        self.runner.session=SimpleNamespace()
        with patch('orchestrator.codex_agent.run_program') as shell:
            result=await self.runner._request('item/tool/call',{'tool':'celeste_cutscene',
                'arguments':{'action':'advance','frames':30}})
        shell.assert_not_called()
        self.server.call_tool.assert_awaited_with('cutscene',{'action':'advance'})
        self.assertTrue(result['success'])
        self.assertNotIn('data:image/', json.dumps(result))
        self.assertEqual(self.runner.rpc.request.call_args.args[1]['input'][1],
                         {'type':'image','url':'data:image/png;base64,AAAA'})

    async def test_image_delivery_failure_keeps_executed_text_and_pauses_without_retry(self):
        self.runner.study = None
        self.runner.rpc.request = AsyncMock(side_effect=TimeoutError('attachment timeout'))
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.assertEqual(result['contentItems'][0]['text'], 'observed')
        self.assertIn('勿重复已执行动作', result['contentItems'][-1]['text'])
        self.assertNotIn('data:image/', json.dumps(result))
        self.assertEqual(self.server.call_tool.await_count, 1)
        self.assertEqual(self.runner.rpc.request.await_count, 1)

    async def test_image_delivery_never_targets_replacement_turn(self):
        self.runner.study = None
        async def changed_turn(*args):
            self.runner.turn_id = 'replacement'
            return self.server.call_tool.return_value
        self.server.call_tool.side_effect = changed_turn
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}})
        self.assertFalse(result['success'])
        self.runner.rpc.request.assert_not_awaited()
        self.assertNotIn('data:image/', json.dumps(result))

    async def test_pause_during_game_call_does_not_deliver_images_or_resume(self):
        self.runner.study = None
        async def paused_during_action(*args):
            self.runner.paused = True
            return self.server.call_tool.return_value
        self.server.call_tool.side_effect = paused_during_action
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.runner.rpc.request.assert_not_awaited()

    async def test_invalid_image_acknowledgment_is_not_reported_delivered(self):
        self.runner.study = None
        self.runner.rpc.request.return_value = {'turnId': 'another-turn'}
        result = await self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}})
        self.assertFalse(result['success'])
        self.assertTrue(self.runner.paused)
        self.assertIn('交付失败', result['contentItems'][-1]['text'])

    async def test_action_lock_covers_image_delivery_before_next_game_call(self):
        self.runner.study = None
        sending, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            sending.set()
            await release.wait()
            return {'turnId': 'turn'}
        self.runner.rpc.request.side_effect = send
        first = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}}))
        await sending.wait()
        second = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}}))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(self.server.call_tool.await_count, 1)
        finally:
            release.set()
            results = await asyncio.gather(first, second)
        self.assertTrue(all(r['success'] for r in results))
        self.assertEqual(self.server.call_tool.await_count, 2)

    async def test_queued_old_turn_action_is_rejected_before_game_execution(self):
        self.runner.study = None
        sending, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            sending.set()
            await release.wait()
            return {'turnId': 'turn'}
        self.runner.rpc.request.side_effect = send
        first = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_observe', 'arguments': {}}))
        await sending.wait()
        second = asyncio.create_task(self.runner._request('item/tool/call', {'tool': 'celeste_act', 'arguments': {}}))
        await asyncio.sleep(.01)
        self.runner.turn_id = 'replacement'
        release.set()
        results = await asyncio.gather(first, second)
        self.assertEqual(self.server.call_tool.await_count, 1)
        self.assertFalse(results[1]['success'])
        self.assertIn('动作未执行', results[1]['contentItems'][0]['text'])

    def test_public_image_inputs_are_redacted_for_all_protocol_aliases(self):
        value = public_event({'input': [{'type': 'image', 'url': 'data:image/png;base64,PRIVATE'}],
                              'image_url': 'data:image/png;base64,PRIVATE'})
        self.assertNotIn('PRIVATE', json.dumps(value))

    async def test_other_resume_errors_do_not_fork(self):
        self.runner.rpc.request = AsyncMock(side_effect=RuntimeError("connection failed"))
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            await self.runner._resume_thread({"threadId": "original"})
        self.assertEqual(self.runner.rpc.request.await_count, 1)

    def test_resumed_session_reports_missing_dynamic_tools_without_bash_fallback(self):
        path = self.root / 'thread.jsonl'
        tools = ['celeste_observe', 'celeste_act', 'celeste_cutscene', 'celeste_exit_level',
                 'celeste_narrate', 'celeste_set_effort', 'celeste_stop_run']
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})
        self.cfg.sim_assist = True
        with self.assertRaisesRegex(RuntimeError, 'celeste_simulate'):
            self.runner._validate_resumed_tools({'path': str(path)})
        tools.append('celeste_simulate')
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})

    def test_resume_does_not_require_tools_disabled_by_play_mode(self):
        self.cfg.live = False
        self.cfg.mode = 'speedrun'
        self.cfg.cutscene_mode = 'auto'
        path = self.root / 'thread.jsonl'
        tools = ['celeste_observe', 'celeste_act', 'celeste_narrate', 'celeste_set_effort', 'celeste_stop_run']
        path.write_text(json.dumps({'payload': {'dynamic_tools': [{'name': name} for name in tools]}}), encoding='utf-8')
        self.runner._validate_resumed_tools({'path': str(path)})
        self.cfg.live = True
        with self.assertRaisesRegex(RuntimeError, 'celeste_exit_level'):
            self.runner._validate_resumed_tools({'path': str(path)})

    async def test_bash_has_hard_time_limit(self):
        # 探索模式：单次规划程序的上限（explore_program_s，默认 15 秒，与提示词一致）；速通模式：Bash 最长时长 bash_max_s
        async def call():
            with patch("orchestrator.codex_agent.run_program", return_value=ProgramResult(0, "ok", "", 0.1, False)) as run:
                await self.runner._request("item/tool/call", {"tool": "celeste_bash", "arguments": {
                    "command": "python3 planner.py", "description": "测试下一处落脚点", "timeout_s": 1000}})
            return run.call_args.args[2]
        self.cfg.mode, self.cfg.knowledge_profile = "explore", "explore"
        self.assertEqual(await call(), 15)
        self.cfg.explore_program_s = 40
        self.assertEqual(await call(), 40)
        self.cfg.mode, self.cfg.knowledge_profile = "speedrun", "full"
        self.assertEqual(await call(), 600)

    async def test_bash_exposes_head_tail_and_recoverable_output_metadata(self):
        import subprocess
        self.cfg.live=False
        raw='frame=22 bounds=room exits=right\n'+'spinner\n'*1000+'LAST SPINNER'
        replies=[subprocess.CompletedProcess([],0,raw,''),subprocess.CompletedProcess([],0,
                 json.dumps({'path':'/workspace/program-output-'+'a'*24+'/output.txt'}),'')]
        with patch('orchestrator.sandbox.find_docker_exe',return_value='docker'),patch('orchestrator.sandbox.subprocess.run',side_effect=replies):
            response=await self.runner._request('item/tool/call',{'tool':'celeste_bash',
                'arguments':{'command':'python geometry.py','description':'读取几何'}})
        self.assertTrue(response['success'])
        data=json.loads(response['contentItems'][0]['text'])
        self.assertTrue(data['stdout'].startswith('frame=22 bounds='))
        self.assertTrue(data['stdout'].endswith('LAST SPINNER'))
        self.assertTrue(data['stdout_truncated'])
        self.assertEqual(data['stdout_chars'],len(raw))
        self.assertTrue(data['stdout_path'].startswith('/workspace/program-output-'))

    async def test_host_permission_request_is_never_approved(self):
        with self.assertRaises(RuntimeError):
            await self.runner._request("item/permissions/requestApproval", {})

    async def test_gate_denial_prevents_sandbox_program(self):
        self.runner.gate = SimpleNamespace(enabled=True, note_tool_use=lambda *args: None,
                                          decide=AsyncMock(return_value=(False, "轮次已用完")))
        with patch("orchestrator.codex_agent.run_program") as run:
            result = await self.runner._request("item/tool/call", {"callId": "b1", "tool": "celeste_bash",
                "arguments": {"command": "python3 planner.py", "description": "规划"}})
        self.assertFalse(result["success"])
        run.assert_not_called()
        self.runner.gate.decide.assert_awaited_once_with("Bash", "b1")

    def test_usage_and_compaction_are_actual_events(self):
        self.runner._event("thread/tokenUsage/updated", {"tokenUsage": {
            "last": {"inputTokens": 100, "outputTokens": 20}, "modelContextWindow": 1000,
            "total": {"inputTokens": 999999, "outputTokens": 70, "reasoningOutputTokens": 30}}})
        for _ in range(2):
            self.runner._event("item/completed", {"item": {"type": "contextCompaction", "id": "one"}})
        self.assertEqual(self.metrics.state.context_tokens, 120)
        self.assertEqual(self.metrics.state.context_window, 1000)
        self.assertEqual(self.metrics.state.compactions, 1)
        self.assertEqual(self.metrics.state.output_tokens, 70)

    def test_nested_reasoning_is_not_written_to_event_log(self):
        data = public_event({"turn": {"items": [{"type": "reasoning", "id": "r", "summary": "PRIVATE", "content": "PRIVATE"},
                                               {"type": "agentMessage", "text": "公开解说"}]}})
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertIn("公开解说", json.dumps(data, ensure_ascii=False))

    def preparation(self):
        return {"quick_check": False, "native_game_tools": True, "session_id": "native-thread", "metrics": {"agent": self.cfg.agent},
                "kb_study": {"complete": True, "files": {"README.md": [2, 2]}},
                "kb_hashes": {"README.md": hashlib.sha256((self.kb / "README.md").read_bytes()).hexdigest()}}

    def test_prepared_session_requires_same_files_and_complete_reading(self):
        data = self.preparation()
        (self.root / "prepare_summary.json").write_text(json.dumps(data), encoding="utf-8")
        restored = load_preparation(str(self.root), self.cfg, self.study)
        self.assertEqual(restored["session_id"], "native-thread")
        self.assertTrue(self.study.complete())
        (self.kb / "README.md").write_text("modified\ntwo\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_preparation(str(self.root), self.cfg, KbStudy(self.kb, 10))

    def test_codex_preparation_uses_actual_workspace_capability_not_client_label(self):
        data = {**self.preparation(), 'runtime': 'container', 'engine': 'codex'}
        (self.root / 'prepare_summary.json').write_text(json.dumps(data), encoding='utf-8')
        self.cfg.runtime = 'host'
        restored = load_preparation(str(self.root), self.cfg, self.study)
        self.assertEqual(restored['session_id'], 'native-thread')
        self.assertTrue(self.study.complete())

    def test_quick_check_cannot_be_used_as_full_preparation(self):
        data = {**self.preparation(), "quick_check": True}
        (self.root / "prepare_summary.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_preparation(str(self.root), self.cfg, self.study)

    def test_explore_profile_isolates_kb_workspace_and_rejects_old_session(self):
        old_workspace = self.cfg.workspace_dir
        self.cfg.knowledge_profile = "explore"
        self.assertNotEqual(old_workspace, self.cfg.workspace_dir)
        self.assertEqual(self.cfg.kb_dir.name, "explore")
        self.assertNotIn("reference", str(self.cfg.kb_dir))
        study = KbStudy(self.cfg.kb_dir, 10)
        self.assertEqual(set(study.files), {"README.md", "basics.md", "data.md", "tools.md",
                                            "techniques.md", "mechanics.md", "search_methods.md"})   # 后三份是 4A 起新增的资料
        self.assertNotIn("help/controls.md", study.files)
        data = self.preparation()
        (self.root / "prepare_summary.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "知识范围不同"):
            load_preparation(str(self.root), self.cfg, study)

    async def test_prepared_manifest_reaches_game_session(self):
        from orchestrator.run import run_async
        manifest = self.preparation()
        args = SimpleNamespace(no_agent=False, prepared=str(self.root), no_hud=True, no_hud_window=True)
        with patch("orchestrator.prepared.load_preparation", return_value=manifest), \
             patch("orchestrator.run._run_session", new_callable=AsyncMock, return_value={}) as run:
            await run_async(self.cfg, args, self.root, None)
        self.assertIs(run.call_args.args[-1], manifest)


if __name__ == "__main__":
    unittest.main()
