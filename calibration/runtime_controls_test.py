"""Shared controls exercised through the Claude adapter and real MCP dispatch.

No game, Docker process or paid model request is started by these tests.
"""
import asyncio
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from orchestrator.agent import ClaudeRunner
from orchestrator.prompting import play_prompt
from orchestrator.bridge_requirements import validate_bridge
from orchestrator.config import RunConfig
from orchestrator.goals import GoalManager
from orchestrator.metrics import Metrics
from orchestrator.server import build_server
from orchestrator.telemetry import public_event
from overlay.history import PublicHistory


class ClaudeControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent='test', save='test', model='sonnet', runtime='host',
                             knowledge_profile='explore', sim=False, sim_assist=True)
        self.cfg.provider_info = {'models': [{'id':'sonnet', 'efforts':['low', 'medium', 'high', 'ultra', 'adaptive-high']}]}
        self.metrics = Metrics(self.root / 'metrics.json', 'test', 'sonnet', 'medium', 100)
        self.session = SimpleNamespace(cfg=self.cfg, metrics=self.metrics, workspace=None, action_policy_supported=1,
            done=False, done_reason='目标达成', goals=GoalManager(self.root),
            goal_values=lambda: {'room': 'start', 'map_complete': False},
            sim_energy=SimpleNamespace(snapshot=lambda: {'balance': 40}),
            simulate=lambda data: {'cost': 13, 'candidates': data['candidates']})
        def finish():
            self.session.done = True
        self.session.finish_user_stop = Mock(side_effect=finish)
        self.server = build_server(self.session)
        self.runner = ClaudeRunner(self.cfg, self.root, self.session, self.metrics, '')
        self.packets = []
        def write(raw):
            packet = json.loads(raw)
            self.packets.append(packet)
            if packet['type'] == 'control_request':
                self.runner._handle({'type': 'control_response', 'response': {
                    'request_id': packet['request_id'], 'subtype': 'success'}})
        self.runner.proc = SimpleNamespace(stdin=SimpleNamespace(write=write, drain=AsyncMock()))
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.runner._events.close()
        self.temp.cleanup()

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(.01)

    def controls(self):
        task = asyncio.create_task(self.runner._controls())
        self.tasks.append(task)
        self.runner.control_task = task

    def action(self, ident):
        return next(a for a in self.runner.control.snapshot()['actions'] if a['id'] == ident)

    async def test_message_interrupts_then_is_sent_once_in_next_turn(self):
        await self.runner._send('first')
        ident = self.runner.control.enqueue('message', '试试左边的通路')
        self.controls()
        await self.until(lambda: any(p['type'] == 'control_request' for p in self.packets))
        self.assertEqual(self.packets[-1]['request'], {'subtype': 'interrupt'})
        self.assertEqual(self.action(ident)['status'], 'waiting')
        self.runner.turn_active = False
        self.runner.turn_done.set()
        await self.runner._next_user_turn('continue')
        self.assertEqual(self.packets[-1]['message']['content'], '试试左边的通路')
        self.assertEqual(self.action(ident)['status'], 'submitted')
        self.assertEqual(self.runner.pending_messages, [])

    async def test_chapter_reset_policy_is_saved_published_and_delivered(self):
        await self.runner._send('first')
        self.cfg.allow_chapter_reset = True
        ident = self.runner.control.enqueue('chapter_reset', 'deny')
        self.controls()
        await self.until(lambda: self.action(ident)['status'] == 'waiting')
        self.assertFalse(self.cfg.allow_chapter_reset)
        self.assertIs(self.runner.control.chapter_reset_preference(), False)
        await self.until(lambda: self.runner.control.snapshot().get('chapter_reset_control'))
        self.assertIs(self.runner.control.snapshot()['allow_chapter_reset'], False)
        self.runner.turn_active = False
        self.runner.turn_done.set()
        await self.runner._next_user_turn('continue')
        self.assertIn('重启章节=禁止', self.packets[-1]['message']['content'])
        self.assertEqual(self.action(ident)['status'], 'submitted')

    async def test_chapter_reset_change_waits_for_current_action(self):
        self.controls()
        async with self.runner.action_lock:
            ident = self.runner.control.enqueue('chapter_reset', 'allow')
            await asyncio.sleep(.15)
            self.assertFalse(self.cfg.allow_chapter_reset)
            self.assertEqual(self.action(ident)['status'], 'queued')
        await self.until(lambda: self.cfg.allow_chapter_reset)
        self.assertIs(self.runner.control.chapter_reset_preference(), True)

    async def test_individual_permission_updates_persist_notify_and_preserve_pause(self):
        self.runner.paused = True
        self.controls()
        ident = self.runner.control.enqueue('action_permissions', '{"save_quit":true}')
        await self.until(lambda: self.runner.control.action_permissions_preference() is not None)
        self.assertEqual(self.runner.control.action_permissions_preference(),
                         {'restart_chapter':False, 'return_to_map':False, 'save_quit':True})
        await self.runner._next_user_turn('continue')
        self.assertTrue(self.runner.paused)
        self.assertIn('重启章节=禁止', self.packets[-1]['message']['content'])
        self.assertIn('保存并退出=允许', self.packets[-1]['message']['content'])
        self.assertEqual(self.action(ident)['status'], 'submitted')

    async def test_paused_chat_turn_does_not_resume_play(self):
        self.runner.paused = True
        self.controls()
        turn = asyncio.create_task(self.runner._next_user_turn('continue'))
        self.tasks.append(turn)
        await self.until(lambda: self.metrics.state.status == 'paused')
        ident = self.runner.control.enqueue('message', '先解释一下当前障碍')
        await turn
        self.assertIn('试玩仍处于暂停状态', self.packets[-1]['message']['content'])
        self.assertTrue(self.runner.paused)
        self.assertEqual(self.action(ident)['status'], 'submitted')

    async def test_agent_blockage_report_pauses_until_user_resumes(self):
        # stop_run 报告任务阻塞：与 HUD 暂停相同（游戏工具被挡住、游戏保持冻结），用户点继续后恢复
        self.assertTrue(self.session.request_pause('桥接持续报错'))
        self.assertTrue(self.runner.paused)
        self.assertEqual(self.metrics.state.status, 'paused')
        self.assertIn('暂停', self.runner._game_guard())
        self.assertFalse(self.session.done)
        ident = self.runner.control.enqueue('resume')
        self.controls()
        await self.until(lambda: self.action(ident)['status'] == 'submitted')
        self.assertFalse(self.runner.paused)
        self.assertIsNone(self.runner._game_guard())

    async def test_workspace_chat_allows_programs_but_not_game_tools(self):
        # 通关后唤起会话整理工作区（chat --allow-programs）：沙箱里的 Bash 放行，游戏工具仍然不可用
        self.runner.chat_only, self.runner.paused = True, True
        self.assertIsNotNone(self.runner._program_guard())
        self.runner.allow_programs = True
        self.assertIsNone(self.runner._program_guard())
        self.assertIn('暂停', self.runner._game_guard())
        self.assertIn('整理工作区', self.runner._lifecycle_prompt())

    async def test_pause_waits_for_mcp_call_then_blocks_next_simulation(self):
        entered, release = threading.Event(), threading.Event()
        def simulate(data):
            entered.set()
            release.wait(3)
            return {'cost': 13}
        self.session.simulate = simulate
        call = asyncio.create_task(self.server.call_tool('simulate', {'candidates': [['60,R']]}))
        self.tasks.append(call)
        await self.until(entered.is_set)
        ident = self.runner.control.enqueue('pause')
        self.controls()
        try:
            await self.until(lambda: self.runner.paused)
            self.assertNotEqual(self.action(ident)['status'], 'submitted')
        finally:
            release.set()
        await call
        await self.until(lambda: self.action(ident)['status'] == 'submitted')
        denied = await self.server.call_tool('simulate', {'candidates': [['60,R']]})
        self.assertIn('暂停', denied.content[0].text)
        resume = self.runner.control.enqueue('resume')
        await self.until(lambda: self.action(resume)['status'] == 'submitted')
        self.assertFalse(self.runner.paused)

    async def test_pause_also_denies_container_bash_without_blocking_note_reads(self):
        from orchestrator.run import add_gate_route
        app = SimpleNamespace(router=SimpleNamespace(routes=[]))
        gate = SimpleNamespace(decide=AsyncMock(return_value=(True, '')))
        add_gate_route(app, gate, session=self.session)
        self.runner.paused = True
        endpoint = app.router.routes[0].endpoint
        response = await endpoint(SimpleNamespace(json=AsyncMock(return_value={'tool_name': 'Bash'})))
        self.assertEqual(json.loads(response.body)['decision'], 'deny')
        gate.decide.assert_not_awaited()
        response = await endpoint(SimpleNamespace(json=AsyncMock(return_value={'tool_name': 'Read'})))
        self.assertEqual(json.loads(response.body)['decision'], 'allow')

    async def test_preparation_can_organize_notes_but_cannot_use_game_tools(self):
        self.runner.prepare_only = True
        self.assertIsNone(self.session.program_guard())
        self.assertIn('仅准备', self.session.control_guard())
        result = await self.server.call_tool('simulate', {'candidates': [['60,R']]})
        self.assertIn('仅准备', result.content[0].text)

    async def test_stop_and_goals_use_shared_controller(self):
        self.controls()
        goal = self.runner.control.enqueue('goal', json.dumps({'text': '观察左边', 'conditions': []}))
        await self.until(lambda: self.action(goal)['status'] == 'waiting')
        self.assertTrue(self.session.goals.snapshot()['text'])
        cleared = self.runner.control.enqueue('goal_clear')
        await self.until(lambda: self.action(cleared)['status'] == 'waiting')
        self.assertFalse(self.session.goals.snapshot()['text'])
        stopped = self.runner.control.enqueue('stop')
        await self.until(lambda: self.action(stopped)['status'] == 'submitted')
        self.session.finish_user_stop.assert_called_once()
        self.assertTrue(self.session.done)

    async def test_fast_is_rejected_and_not_advertised(self):
        ident = self.runner.control.enqueue('fast', 'on')
        self.controls()
        await self.until(lambda: self.action(ident)['status'] == 'error')
        self.assertIn('不支持', self.action(ident)['detail'])
        self.assertFalse(self.runner.control.snapshot()['fast_control'])

    async def test_effort_capabilities_and_manual_override_apply_to_mcp(self):
        self.controls()
        invalid = self.runner.control.enqueue('effort', 'xhigh')
        await self.until(lambda: self.action(invalid)['status'] == 'error')
        valid = self.runner.control.enqueue('effort', 'high')
        await self.until(lambda: self.action(valid)['status'] == 'submitted')
        result = await self.server.call_tool('set_effort', {'level': 'low'})
        self.assertIn('固定', result.content[0].text)
        self.assertEqual(self.metrics.state.effort_pending, 'high')
        auto = self.runner.control.enqueue('effort', 'auto')
        await self.until(lambda: self.action(auto)['status'] == 'submitted')
        await self.server.call_tool('set_effort', {'level': 'low'})
        self.assertEqual(self.metrics.state.effort_pending, 'low')

    async def test_provider_declared_effort_passes_controls_preferences_and_mcp(self):
        self.controls()
        ident = self.runner.control.enqueue('effort', 'ultra')
        await self.until(lambda: self.action(ident)['status'] == 'submitted')
        self.assertEqual(self.runner.control.effort_preference(), 'ultra')
        self.assertIn('ultra', self.runner._effort_status()['effort_levels'])
        ident = self.runner.control.enqueue('effort', 'auto')
        await self.until(lambda: self.action(ident)['status'] == 'submitted')
        result = await self.server.call_tool('set_effort', {'level': 'adaptive-high'})
        self.assertIn('已登记', result.content[0].text)
        self.assertEqual(self.metrics.state.effort_pending, 'adaptive-high')

    async def test_claude_public_messages_and_compaction_reach_history_on_cold_load(self):
        self.runner._handle({'type': 'assistant', 'message': {'id': 'm1', 'content': [
            {'type': 'thinking', 'thinking': 'PRIVATE'}, {'type': 'text', 'text': '左边有个开口，先试一下。'}]}})
        self.runner._handle({'type': 'system', 'subtype': 'status', 'status': 'compacting'})
        self.runner._handle({'type': 'system', 'subtype': 'compact_boundary', 'compact_metadata': {}})
        history = PublicHistory(self.root).snapshot({})
        self.assertEqual(len(history), 2)
        self.assertIn('上下文压缩完成', history[-1]['text'])
        self.assertNotIn('PRIVATE', json.dumps(history))
        self.assertEqual(history[0]['text'], '左边有个开口，先试一下。')

    async def test_compaction_with_uuid_replaces_the_in_progress_entry(self):
        # Claude Code 的 compact_boundary 带 uuid：完成时仍替换"正在压缩"那一条，历史里不留两条
        self.runner._handle({'type': 'system', 'subtype': 'status', 'status': 'compacting'})
        self.runner._handle({'type': 'system', 'subtype': 'compact_boundary', 'uuid': 'c-1', 'compact_metadata': {}})
        history = PublicHistory(self.root).snapshot({})
        self.assertEqual([item['text'] for item in history], ['上下文压缩完成。'])

    def test_woken_chat_has_its_own_time_budget(self):
        """通关后唤起的聊天会话：整次运行的现实用时早已超过预算，也不能一开始就因"时间预算用完"被结束。"""
        import time
        budget = self.cfg.budget_seconds
        self.metrics.start_play()
        self.metrics._offset = budget + 6000            # 恢复的整次运行用时（例如 1A + 2A 已用约 5.7 小时）
        started = time.monotonic()
        self.assertLessEqual(self.runner._budget_remaining(started), 0)       # 试玩：按整次运行算
        self.runner.chat_only = True
        self.assertGreater(self.runner._budget_remaining(started), budget - 5)  # 唤起的会话：从这次开始算
        self.runner.chat_only, self.runner.naming_only = False, True
        self.assertGreater(self.runner._budget_remaining(started), budget - 5)


class CommonContracts(unittest.TestCase):
    def test_explore_and_metered_prompts_are_engine_independent(self):
        for engine in ('codex', 'claude'):
            cfg = RunConfig(agent='test', save='test', model='test', engine=engine,
                            knowledge_profile='explore', sim=False, sim_assist=True)
            prompt = play_prompt(cfg)
            self.assertIn('体力', prompt)
            self.assertIn('调用 simulate 工具', prompt)
            self.assertNotIn('通过 celeste_bash', prompt)

    def test_protocol_logs_strip_both_engines_private_blocks(self):
        value = public_event([{'type': kind, 'text': 'PRIVATE', 'thinking': 'PRIVATE'}
                              for kind in ('reasoning', 'thinking', 'redacted_thinking')])
        self.assertNotIn('PRIVATE', json.dumps(value))

    def test_minimum_bridge_is_explicit(self):
        valid = {'bridge': '1.4.0', 'create_save': True, 'dialog_choices': True, 'story_text': True,
                 'entry_guard': True, 'pico8_entry_guard': True}
        validate_bridge(valid)
        for change in ({'bridge': '1.3.3'}, {'pico8_entry_guard': False}, {'entry_guard':False}, {'story_text': False}, {'bridge': ''}):
            with self.subTest(change=change), self.assertRaisesRegex(RuntimeError, '1.4.0'):
                validate_bridge({**valid, **change})


if __name__ == '__main__':
    unittest.main()
