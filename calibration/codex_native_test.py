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
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.codex_native import finish_native_tools, native_tool_hook
from orchestrator.metrics import Metrics
from orchestrator.pausegate import PauseGate
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
        self.charges = []
        self.session = SimpleNamespace(cfg=self.cfg, program_guard=lambda: None,
                        program_run_denied=lambda: self.charges.append('program') or None)
        self.gate = PauseGate(self.metrics, enabled=False)
        self.runner = SimpleNamespace(cfg=self.cfg, metrics=self.metrics, gate=self.gate,
                    session=self.session, session_id='thread-a', turn_id='turn-a', turn_active=True,
                    turn_done=asyncio.Event(), playback=None, native_token='test-secret')

    def data(self, **values):
        return {'engine':'codex', 'token':'test-secret', 'hook_event_name':'PreToolUse', 'tool_name':'Bash',
                'tool_input':{'command':'python3 experiment.py', 'timeout_ms':3000},
                'session_id':'thread-a', 'turn_id':'turn-a', 'tool_use_id':'call-a', **values}

    async def test_shared_gate_decision_and_program_charge_are_used_once(self):
        data = self.data()
        result = await native_tool_hook(self.runner, data)
        self.assertEqual(result['decision'], 'allow')
        self.assertEqual(result['timeout_s'], 3)
        self.assertEqual(self.charges, ['program'])
        self.assertTrue(self.metrics.tools_running)
        self.assertEqual(await native_tool_hook(self.runner, data), result)
        self.assertEqual(self.charges, ['program'])
        self.assertEqual(self.metrics.state.tool_calls, 1)
        post = self.data(hook_event_name='PostToolUse')
        await native_tool_hook(self.runner, post)
        await native_tool_hook(self.runner, post)
        self.assertFalse(self.metrics.tools_running)
        self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'deny')
        self.assertEqual(self.charges, ['program'])

    async def test_claude_and_codex_receive_same_pause_or_program_denial(self):
        for reason in ('用户已暂停试玩', '停止目标已触发'):
            self.session.program_guard = lambda: reason
            expected = await decide_tool(self.gate, 'Bash', 'test', session=self.session)
            result = await native_tool_hook(self.runner, self.data(tool_use_id=reason))
            self.assertEqual(result, expected)
        self.assertEqual(self.charges, [])
        self.assertEqual(self.metrics.state.tool_calls, 2)

    async def test_retried_running_call_rechecks_current_turn_and_shared_guard(self):
        data = self.data()
        await native_tool_hook(self.runner, data)
        self.session.program_guard = lambda: '用户已暂停试玩'
        self.assertIn('暂停', (await native_tool_hook(self.runner, data))['reason'])
        self.runner.turn_id = 'next-turn'
        self.assertIn('轮次', (await native_tool_hook(self.runner, data))['reason'])
        self.assertEqual(self.charges, ['program'])
        self.assertEqual(self.metrics.state.tool_calls, 1)
        finish_native_tools(self.runner)

    async def test_file_change_maps_to_shared_write_without_extra_codex_rule(self):
        self.session.program_guard = lambda: '用户已暂停试玩'
        expected = await decide_tool(self.gate, 'Write', 'call-a', session=self.session)
        result = await native_tool_hook(self.runner, self.data(tool_name='apply_patch'))
        self.assertEqual(result['decision'], expected['decision'])
        self.assertEqual(self.charges, [])
        finish_native_tools(self.runner)
        self.assertFalse(self.metrics.tools_running)

    async def test_session_and_turn_mismatch_does_not_charge(self):
        for change in ({'session_id':'other'}, {'turn_id':'old'}, {'tool_input':[]}, {'tool_use_id':''}, {'token':'wrong'}):
            result = await native_tool_hook(self.runner, self.data(**change))
            self.assertEqual(result['decision'], 'deny')
        self.assertEqual(self.charges, [])

    async def test_protocol_adapter_uses_injected_session_for_shared_guard(self):
        self.runner.native_session = SimpleNamespace(cfg=self.cfg, program_guard=lambda: '本次仅聊天')
        self.assertIn('仅聊天', (await native_tool_hook(self.runner, self.data()))['reason'])
        self.assertEqual(self.charges, [])

    async def test_execution_transport_cannot_replace_trusted_shell_or_open_pty(self):
        for arguments in ({'shell':'/workspace/custom-shell'}, {'shell':'bash'},
                          {'tty':True}, {'pty':True}, {'login':True}):
            data = self.data(tool_input={'command':'python3 experiment.py', **arguments})
            self.assertEqual((await native_tool_hook(self.runner, data))['decision'], 'deny')
        self.assertEqual(self.charges, [])
        self.assertEqual(self.metrics.state.tool_calls, 0)
        allowed = self.data(tool_input={'command':'pwd', 'shell':'/bin/bash', 'login':False})
        self.assertEqual((await native_tool_hook(self.runner, allowed))['decision'], 'allow')
        self.assertEqual(self.runner._native_tool_calls['call-a']['input_keys'], ['command', 'login', 'shell'])
        finish_native_tools(self.runner)

    async def test_native_view_image_uses_read_gate_and_timing_without_program_charge(self):
        self.runner.gate = PauseGate(self.metrics, enabled=True, started=True, sim_only=True)
        data = self.data(tool_name='view_image', tool_input={'path':'/workspace/preview.png'})
        decision = await native_tool_hook(self.runner, data)
        self.assertEqual(decision['decision'], 'allow')
        self.assertEqual(self.charges, [])
        self.assertEqual(self.runner.gate.bank, 0)
        self.assertEqual(self.metrics.state.tool_calls, 1)
        self.assertTrue(self.metrics.tools_running)
        self.assertIn('view_image', self.metrics.state.tool_call)
        await native_tool_hook(self.runner, {**data, 'hook_event_name':'PostToolUse'})
        self.assertFalse(self.metrics.tools_running)

    async def test_native_view_image_obeys_same_read_round_budget(self):
        self.runner.gate = PauseGate(self.metrics, enabled=True, started=True, sim_only=False)
        self.runner.gate.note_tool_use('read-budget', 'read-budget')
        expected = await decide_tool(self.runner.gate, 'Read', 'read-budget', session=self.session)
        data = self.data(tool_name='view_image', tool_input={'path':'/workspace/preview.png'})
        decision = await native_tool_hook(self.runner, data)
        self.assertEqual(decision['decision'], expected['decision'])
        self.assertEqual(decision['decision'], 'deny')
        self.assertEqual(self.charges, [])
        self.assertFalse(self.metrics.tools_running)

    async def test_readonly_chat_native_file_reads_use_read_gate_without_program_charge(self):
        guard_calls = []
        self.runner.native_session = SimpleNamespace(cfg=self.cfg,
            program_guard=lambda: guard_calls.append('guard') or '本次仅聊天，不允许运行程序',
            program_run_denied=self.session.program_run_denied)
        for index, command in enumerate(("cat '/kb/file with spaces.md'",
                                         "sed -n '2,4p' /kb/README.md",
                                         'head -n3 /kb/README.md',
                                         'tail -n+2 /kb/README.md', 'pwd', 'ls /kb')):
            with self.subTest(command=command):
                data = self.data(tool_use_id=f'read-{index}', tool_input={'command': command})
                decision = await native_tool_hook(self.runner, data)
                self.assertEqual(decision['decision'], 'allow')
                self.assertEqual(self.runner._native_tool_calls[f'read-{index}']['canonical'], 'Read')
                await native_tool_hook(self.runner, {**data, 'hook_event_name': 'PostToolUse'})
        self.assertEqual(guard_calls, [])
        self.assertEqual(self.charges, [])
        self.assertFalse(self.metrics.tools_running)

    async def test_unsafe_read_expressions_keep_bash_policy_and_are_rejected_in_readonly_chat(self):
        self.runner.native_session = SimpleNamespace(cfg=self.cfg,
            program_guard=lambda: '本次仅聊天，不允许运行程序',
            program_run_denied=self.session.program_run_denied)
        for index, command in enumerate(('cat /kb/a; python3 exploit.py',
                                         'cat /kb/a | python3 transform.py',
                                         'cat /kb/a > /workspace/copy',
                                         'cat $(python3 discover.py)',
                                         "sed -n '1e' /kb/a", '/workspace/cat /kb/a')):
            with self.subTest(command=command):
                decision = await native_tool_hook(self.runner,
                    self.data(tool_use_id=f'unsafe-{index}', tool_input={'command': command}))
                self.assertEqual(decision['decision'], 'deny')
                self.assertIn('仅聊天', decision['reason'])
                self.assertEqual(self.runner._native_tool_calls[f'unsafe-{index}']['canonical'], 'Bash')
        self.assertEqual(self.charges, [])
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
        self.assertEqual(command[4], '/usr/bin/bwrap')
        self.assertEqual(command[-3], '/bin/sh')
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
        requirements = tomllib.loads((ROOT/'agent-container/codex-requirements.toml').read_text())
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
