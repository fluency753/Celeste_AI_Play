"""Native Codex transport uses the shared tool gate and program sandbox."""
import asyncio
import base64
from importlib.util import module_from_spec, spec_from_file_location
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.codex_native import finish_native_tools, native_tool_hook
from orchestrator.metrics import Metrics
from orchestrator.planning_budget import PlanningBudget
from orchestrator.program_sandbox import bwrap_args
from orchestrator.tool_gate import decide_tool


ROOT = Path(__file__).resolve().parents[1]


def load_helper(name):
    spec = spec_from_file_location('_test_' + name.replace('-', '_'), ROOT/'agent-container'/f'{name}.py')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NativeHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.metrics = Metrics(Path(folder.name)/'metrics.json', 'test', 'test', 'high', 60)
        self.metrics.update(tool_calls=0)  # CodexRunner.run initializes native/dynamic call accounting.
        self.cfg = RunConfig(agent='test', save='test', model='test', mode='explore', engine='codex', live=False)
        self.budget = PlanningBudget(self.metrics, Path(folder.name)/'planning_budget.json')
        self.session = SimpleNamespace(cfg=self.cfg, planning_budget=self.budget)
        self.runner = SimpleNamespace(cfg=self.cfg, metrics=self.metrics,
                    session=self.session, session_id='thread-a', turn_id='turn-a', turn_active=True,
                    turn_done=asyncio.Event(), playback=None, native_token='test-secret')

    def data(self, **values):
        return {'engine':'codex', 'token':'test-secret', 'hook_event_name':'PreToolUse', 'tool_name':'Bash',
                'tool_input':{'command':'python3 experiment.py', 'timeout_ms':3000},
                'session_id':'thread-a', 'turn_id':'turn-a', 'tool_use_id':'call-a', **values}

    async def test_native_authorization_and_timing_are_idempotent(self):
        data = self.data()
        result = await native_tool_hook(self.runner, data)
        self.assertEqual(result['decision'], 'allow')
        self.assertEqual(result['timeout_s'], 3)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertTrue(self.metrics.tools_running)
        self.assertEqual(await native_tool_hook(self.runner, data), result)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.metrics.state.tool_calls, 1)
        post = self.data(hook_event_name='PostToolUse')
        await native_tool_hook(self.runner, post)
        await native_tool_hook(self.runner, post)
        self.assertFalse(self.metrics.tools_running)
        self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'deny')
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)

    async def test_omitted_timeout_uses_claude_default_and_explicit_timeout_is_capped(self):
        from orchestrator.agent import bash_limits_ms
        self.cfg.mode = 'speedrun'
        self.cfg.bash_default_s, self.cfg.bash_max_s = 120, 600
        self.assertEqual(bash_limits_ms(self.cfg), (120000, 600000))
        for ident, tool_input, expected in (('default', {'command': 'make'}, 120),
                                            ('explicit', {'command': 'make', 'timeout_ms': 300000}, 300),
                                            ('capped', {'command': 'make', 'timeout_ms': 9000000}, 600)):
            with self.subTest(ident=ident):
                result = await native_tool_hook(self.runner, self.data(tool_use_id=ident, tool_input=tool_input))
                self.assertEqual(result['decision'], 'allow')
                self.assertEqual(result['timeout_s'], expected)
        finish_native_tools(self.runner)

    async def test_native_decision_does_not_consume_reminders_without_runner_delivery(self):
        from orchestrator.note_reminders import NoteReminders
        with tempfile.TemporaryDirectory() as folder:
            notes = NoteReminders(Path(folder), lambda: 60)
            notes.compacted('one')
            self.session.note_reminders = notes
            self.cfg.live = True
            result = await native_tool_hook(self.runner, self.data())
            self.assertEqual(result['decision'], 'allow')
            self.assertEqual(result['context'], '')
            self.assertTrue(notes.peek())
            finish_native_tools(self.runner)

    async def test_workspace_does_not_borrow_game_guard_for_either_transport(self):
        for reason in ('用户已暂停试玩', '停止目标已触发'):
            self.session.control_guard = lambda: reason
            expected = await decide_tool('Bash', session=self.session)
            result = await native_tool_hook(self.runner, self.data(tool_use_id=reason))
            self.assertEqual(result['decision'], expected['decision'])
            self.assertEqual(result['decision'], 'allow')
        finish_native_tools(self.runner)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.metrics.state.tool_calls, 2)

    async def test_workspace_ignores_game_budgets_but_rejects_expired_owner(self):
        self.session.control_guard = lambda: '旧游戏阶段限制'
        self.assertEqual((await native_tool_hook(self.runner, self.data()))['decision'], 'allow')
        await native_tool_hook(self.runner, self.data(hook_event_name='PostToolUse'))
        for change in ('stopping', 'turn_changed'):
            self.runner.stop_requested = change == 'stopping'
            self.runner.turn_id = 'turn-b' if change == 'turn_changed' else 'turn-a'
            result = await native_tool_hook(self.runner, self.data(tool_use_id=change))
            self.assertEqual(result['decision'], 'deny')
            self.assertEqual(self.budget.bank, 0)
            self.assertEqual(self.budget.rounds_used, 0)
            self.assertFalse(self.metrics.tools_running)

    async def test_retried_running_call_rechecks_current_turn_not_game_phase(self):
        data = self.data()
        await native_tool_hook(self.runner, data)
        self.session.control_guard = lambda: '用户已暂停试玩'
        self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'allow')
        self.runner.turn_id = 'next-turn'
        self.assertIn('轮次', (await native_tool_hook(self.runner, data))['reason'])
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.metrics.state.tool_calls, 1)
        finish_native_tools(self.runner)

    async def test_file_change_maps_to_shared_write_without_extra_codex_rule(self):
        self.session.control_guard = lambda: '用户已暂停试玩'
        expected = await decide_tool('Write', session=self.session)
        result = await native_tool_hook(self.runner, self.data(tool_name='apply_patch'))
        self.assertEqual(result['decision'], expected['decision'])
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        finish_native_tools(self.runner)
        self.assertFalse(self.metrics.tools_running)

    async def test_session_and_turn_mismatch_does_not_charge(self):
        for change in ({'session_id':'other'}, {'turn_id':'old'}, {'tool_input':[]}, {'tool_use_id':''}, {'token':'wrong'}):
            result = await native_tool_hook(self.runner, self.data(**change))
            self.assertEqual(result['decision'], 'deny')
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)

    async def test_post_receipt_observes_the_injected_session_notes(self):
        observer = Mock()
        self.runner.native_session = SimpleNamespace(cfg=self.cfg, note_reminders=SimpleNamespace(observe_file=observer))
        self.assertEqual((await native_tool_hook(self.runner, self.data()))['decision'], 'allow')
        await native_tool_hook(self.runner, self.data(hook_event_name='PostToolUse'))
        observer.assert_called_once_with()
        finish_native_tools(self.runner)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)

    async def test_execution_transport_cannot_replace_trusted_shell_or_open_pty(self):
        for arguments in ({'shell':'/workspace/custom-shell'}, {'shell':'bash'},
                          {'tty':True}, {'pty':True}, {'login':True}):
            data = self.data(tool_input={'command':'python3 experiment.py', **arguments})
            self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'deny')
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.metrics.state.tool_calls, 0)
        allowed = self.data(tool_input={'command':'pwd', 'shell':'/bin/bash', 'login':False})
        self.assertEqual((await native_tool_hook(self.runner, allowed))['decision'], 'allow')
        self.assertEqual(self.runner._native_tool_calls['call-a']['input_keys'], ['command', 'login', 'shell'])
        finish_native_tools(self.runner)

    async def test_native_view_image_uses_read_gate_and_timing_without_program_charge(self):
        data = self.data(tool_name='view_image', tool_input={'path':'/workspace/preview.png'})
        decision = await native_tool_hook(self.runner, data)
        self.assertEqual(decision['decision'], 'allow')
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.metrics.state.tool_calls, 1)
        self.assertTrue(self.metrics.tools_running)
        self.assertIn('view_image', self.metrics.state.tool_call)
        await native_tool_hook(self.runner, {**data, 'hook_event_name':'PostToolUse'})
        self.assertFalse(self.metrics.tools_running)

    async def test_native_view_image_is_independent_of_game_round_budget(self):
        expected = await decide_tool('Read', session=self.session)
        data = self.data(tool_name='view_image', tool_input={'path':'/workspace/preview.png'})
        decision = await native_tool_hook(self.runner, data)
        self.assertEqual(decision['decision'], expected['decision'])
        self.assertEqual(decision['decision'], 'allow')
        finish_native_tools(self.runner)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertFalse(self.metrics.tools_running)

    async def test_native_shell_commands_are_available_in_chat(self):
        self.runner.native_session = SimpleNamespace(cfg=self.cfg, planning_budget=self.budget)
        self.runner.chat_only = True
        for index, command in enumerate(("cat '/kb/file with spaces.md'",
                                         "sed -n '2,4p' /kb/README.md",
                                         'head -n3 /kb/README.md',
                                         'tail -n+2 /kb/README.md', 'pwd', 'ls /kb')):
            with self.subTest(command=command):
                data = self.data(tool_use_id=f'read-{index}', tool_input={'command': command})
                decision = await native_tool_hook(self.runner, data)
                self.assertEqual(decision['decision'], 'allow')
                self.assertEqual(self.runner._native_tool_calls[f'read-{index}']['canonical'], 'Bash')
        finish_native_tools(self.runner)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertFalse(self.metrics.tools_running)

    async def test_native_cat_does_not_charge_game_program_counter(self):
        data = self.data(tool_input={'command': 'cat /kb/README.md'})
        result = await native_tool_hook(self.runner, data)
        self.assertEqual(result['decision'], 'allow')
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.runner._native_tool_calls['call-a']['canonical'], 'Bash')
        await native_tool_hook(self.runner, {**data, 'hook_event_name': 'PostToolUse'})
        self.assertFalse(self.metrics.tools_running)

    async def test_compound_shell_expressions_keep_sandbox_timeout_in_chat(self):
        self.runner.native_session = SimpleNamespace(cfg=self.cfg, planning_budget=self.budget)
        self.runner.chat_only = True
        for index, command in enumerate(('cat /kb/a; python3 exploit.py',
                                         'cat /kb/a | python3 transform.py',
                                         'cat /kb/a > /workspace/copy',
                                         'cat $(python3 discover.py)',
                                         "sed -n '1e' /kb/a", '/workspace/cat /kb/a')):
            with self.subTest(command=command):
                decision = await native_tool_hook(self.runner,
                    self.data(tool_use_id=f'unsafe-{index}', tool_input={'command': command}))
                self.assertEqual(decision['decision'], 'allow')
                self.assertEqual(self.runner._native_tool_calls[f'unsafe-{index}']['canonical'], 'Bash')
        finish_native_tools(self.runner)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertFalse(self.metrics.tools_running)


class NativeSandboxTests(unittest.TestCase):
    def test_shell_rewrite_preserves_exact_command_and_requires_host_limit(self):
        helper = load_helper('codex-tool-hook')
        command = 'printf "%s" "$HOME `literal`"\npython3 -c "print(1)"'
        data = {'hook_event_name':'PreToolUse', 'tool_name':'Bash', 'tool_input':{'command':command}}
        denied = helper.translate(data, {'decision':'allow'})
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        result = helper.translate(data, {'decision':'allow','timeout_s':15,'context':'主控提醒'})
        updated = result['hookSpecificOutput']['updatedInput']['command']
        self.assertEqual(base64.b64decode(updated.split()[-1]).decode(), command)
        self.assertIn('--seconds 15', updated)
        self.assertEqual(result['hookSpecificOutput']['additionalContext'], '主控提醒')

    def test_shared_sandbox_hides_both_executor_homes_and_clears_network_environment(self):
        from orchestrator import program_sandbox
        with patch.dict(sys.modules, {'program_sandbox':program_sandbox}):
            helper = load_helper('codex-shell')
        with patch.object(helper.Path, 'resolve', return_value=Path('/workspace')):
            command = helper.command_args('python3 experiment.py', 15, '/workspace')
        self.assertEqual(command[4:-4], bwrap_args(network_isolated=True))
        self.assertEqual(command[0], '/usr/bin/timeout')
        # Native Shell runs bash like Claude's Bash tool; the controller retry runner keeps its shell.
        self.assertEqual(command[-3], '/bin/bash')
        self.assertEqual(command[-4], '--')
        from orchestrator.program_sandbox import BASH, SHELL
        self.assertEqual(BASH, '/bin/bash')
        self.assertEqual(SHELL, '/bin/sh')
        for option in ('--unshare-pid', '--clearenv'):
            self.assertIn(option, command)
        self.assertNotIn('--unshare-net', command)
        self.assertIn('--unshare-net', bwrap_args())
        home = command.index('--tmpfs')
        self.assertEqual(command[home + 1], '/home')
        self.assertIn('/opt/py/bin', command[command.index('PATH') + 1])
        with patch.object(helper.Path, 'resolve', return_value=Path('/tmp')):
            temp_command = helper.command_args('pwd', 15, '/tmp')
        self.assertEqual(temp_command[temp_command.index('--chdir') + 1], '/tmp')

    def test_managed_requirements_pin_foreground_tools_and_protect_credentials(self):
        requirements = tomllib.loads((ROOT/'agent-container/codex-requirements.toml').read_text(encoding='utf-8'))
        self.assertTrue(requirements['allow_managed_hooks_only'])
        self.assertFalse(requirements['allow_login_shell'])
        self.assertFalse(requirements['features']['unified_exec'])
        self.assertTrue(requirements['features']['shell_tool'])
        self.assertEqual(requirements['allowed_approval_policies'], ['never'])
        self.assertEqual(requirements['permissions']['filesystem']['deny_read'],
                         ['/home/agent/.codex/auth.json', '/home/agent/.claude'])
        for name in ('celeste-read','celeste-write'):
            self.assertFalse(requirements['permissions'][name]['network']['enabled'])
        for event in ('PreToolUse','PostToolUse'):
            self.assertIn('view_image', requirements['hooks'][event][0]['matcher'])
            hook = requirements['hooks'][event][0]['hooks'][0]
            self.assertEqual(hook['command'], '/usr/bin/python3 /usr/local/lib/celeste/codex-tool-hook.py')

    def test_hook_process_fails_closed_and_post_failure_does_not_claim_unexecuted(self):
        import os
        env = {**os.environ, 'MCP_PORT':'invalid'}
        for phase in ('PreToolUse', 'PostToolUse'):
            result = subprocess.run([sys.executable, '-X', 'utf8', str(ROOT/'agent-container/codex-tool-hook.py')],
                       input=json.dumps({'hook_event_name':phase}), env=env,
                       text=True, encoding='utf-8', capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            if phase == 'PreToolUse':
                self.assertEqual(payload['hookSpecificOutput']['permissionDecision'], 'deny')
            else:
                self.assertIn('已执行', payload['systemMessage'])
                self.assertNotIn('本次未执行', payload['systemMessage'])
                self.assertEqual(payload['hookSpecificOutput']['additionalContext'], payload['systemMessage'])
        helper = load_helper('codex-tool-hook')
        rejected = helper.translate({'hook_event_name':'PostToolUse'}, {'decision':'deny'})
        self.assertEqual(rejected, helper.post_notice())


if __name__ == '__main__':
    unittest.main()
