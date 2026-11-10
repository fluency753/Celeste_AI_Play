"""Shared policy and container transport contracts, without a live model or game."""
import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.codex_agent import CodexRunner
from orchestrator.codex_rpc import CodexRPC, process_options
from orchestrator.config import RunConfig
from orchestrator.execution import workspace_capabilities
from orchestrator.metrics import Metrics
from orchestrator.runtimes import runtime


class ContainerContractTests(unittest.IsolatedAsyncioTestCase):
    def test_deployment_diagnostic_uses_shared_http_gate_and_does_not_borrow_busy_port(self):
        from orchestrator.verify import DiagnosticGate
        from urllib.request import Request, urlopen
        gate = DiagnosticGate(0)
        try:
            request = Request(f'http://127.0.0.1:{gate.port}/gate',
                data=b'{"tool_name":"Bash","tool_use_id":"check"}',
                headers={'Content-Type':'application/json'})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(json.load(response)['decision'], 'allow')
            with self.assertRaises(OSError):
                DiagnosticGate(gate.port)
        finally:
            gate.close()

    def test_claude_gate_disconnect_and_malformed_input_deny_without_execution(self):
        source = Path(__file__).resolve().parents[1]/'agent-container/celeste-gate.py'
        spec = importlib.util.spec_from_file_location('claude_gate_failure', source)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        for body in ('{"tool_name":"Bash","tool_use_id":"one"}', '[]', 'broken'):
            output = io.StringIO()
            with patch.object(helper.sys, 'stdin', io.StringIO(body)), \
                 patch.object(helper.urllib.request, 'urlopen', side_effect=OSError('controller disconnected')), \
                 redirect_stdout(output):
                self.assertEqual(helper.main(), 0)
            result = json.loads(output.getvalue())['hookSpecificOutput']
            self.assertEqual(result['permissionDecision'], 'deny')
            self.assertIn('未执行', result['permissionDecisionReason'])

    def test_actual_capability_and_program_cap_match_claude(self):
        for mode in ('explore', 'speedrun'):
            for limit in (None, 7, 90):
                cfg = RunConfig(agent='test', save='test', model='test', engine='codex', mode=mode,
                                explore_program_s=limit, bash_max_s=45)
                for label in ('host', 'container'):
                    cfg.runtime = label
                    caps = workspace_capabilities(cfg)
                    self.assertEqual(caps.agent_location, 'container')
                    self.assertEqual(caps.program_location, 'container')
                    self.assertTrue(caps.builtin_tools)

    def test_native_options_keep_shell_and_hooks_without_interactive_sessions(self):
        flags = process_options(container=True)
        self.assertIn(['--enable', 'shell_tool'], [flags[i:i+2] for i in range(len(flags)-1)])
        self.assertIn(['--disable', 'unified_exec'], [flags[i:i+2] for i in range(len(flags)-1)])
        self.assertIn(['--enable', 'hooks'], [flags[i:i+2] for i in range(len(flags)-1)])
        self.assertIn('projects={"/workspace"={trust_level="untrusted"}}', flags)

    def test_host_simulation_capability_does_not_depend_on_agent_program_tools(self):
        from orchestrator.modes import game_tools
        cfg = RunConfig(agent='test', save='test', model='test', engine='codex',
                        mode='speedrun', sim=True, pause_limit=True)
        self.assertIn('simulate', game_tools(cfg))
        cfg.pause_limit = False
        self.assertNotIn('simulate', game_tools(cfg))
        cfg.sim, cfg.mode, cfg.sim_assist = False, 'explore', True
        self.assertIn('simulate', game_tools(cfg))
        cfg.sim_assist = False
        self.assertNotIn('simulate', game_tools(cfg))

    def test_claude_simulator_uses_simulate_only_when_limited(self):
        """Claude 不限额时直接调用 /sim，不注册主控 simulate；限额时与 Codex 相同，只经 simulate（本机运行方式也可）。"""
        from orchestrator.modes import game_tools
        for runtime in ('container', 'host'):
            for limit in (False, True):
                cfg = RunConfig(agent='test', save='test', model='test', engine='claude', runtime=runtime,
                                mode='speedrun', sim=True, pause_limit=limit)
                self.assertEqual('simulate' in game_tools(cfg), limit)
        cfg = RunConfig(agent='test', save='test', model='test', engine='claude', mode='explore', sim_assist=True)
        self.assertIn('simulate', game_tools(cfg))      # 探索模式的限额短路线预测两个执行器相同

    async def test_transport_starts_only_container_appserver_and_closes_owned_process(self):
        with tempfile.TemporaryDirectory() as folder:
            rpc = CodexRPC(Path(folder), container='test-container', environment={'MCP_PORT':'12345'})
            stream = asyncio.StreamReader()
            stream.feed_data(b'{"id":1,"result":{}}\n')
            proc = SimpleNamespace(stdout=stream, returncode=None)
            class Input:
                def write(self, data):
                    pass
                async def drain(self):
                    pass
                def close(self):
                    proc.returncode = 0
                    stream.feed_eof()
            proc.stdin = Input()
            proc.wait = AsyncMock(return_value=0)
            with patch('orchestrator.codex_container.ensure_runtime') as ready, \
                 patch('orchestrator.codex_container.find_docker_exe', return_value='docker'), \
                 patch('orchestrator.codex_container.terminate_owned_server') as stop, \
                 patch('orchestrator.codex_rpc.codex_executable', side_effect=AssertionError('no host CLI')), \
                 patch('orchestrator.codex_rpc.asyncio.create_subprocess_exec', AsyncMock(return_value=proc)) as spawn:
                await rpc.start()
                argv = spawn.call_args.args
                self.assertEqual(argv[:3], ('docker', 'exec', '-i'))
                self.assertIn('/usr/local/lib/celeste/codex-app-server.py', argv)
                self.assertIn('codex', argv)
                self.assertIn('MCP_PORT=12345', argv)
                ready.assert_called_once_with('test-container', require_login=True)
                await rpc.close()
                stop.assert_called_with('test-container', rpc.owner)

    async def test_runner_never_falls_back_to_host_without_container(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent='test', save='test', model='test', engine='codex')
            runner = CodexRunner(cfg, root, None, Metrics(root/'metrics.json', 'test', 'test', 'low', 10), None)
            runner.rpc.start = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, '必须在智能体容器'):
                await runner.run()
            runner.rpc.start.assert_not_awaited()

    def test_container_session_archive_status_cannot_be_taken_from_host_copy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'session-id.jsonl').write_text('archived legacy copy', encoding='utf-8')
            adapter = runtime('codex')
            self.assertTrue(adapter.archived('session-id', root))
            self.assertFalse(adapter.archived('session-id', root, state={'session_storage':'container'}))
            self.assertTrue(adapter.archived('session-id', root,
                            state={'session_storage':'container', 'archived':True}))


if __name__ == '__main__':
    unittest.main()
