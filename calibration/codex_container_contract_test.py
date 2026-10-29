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

from orchestrator.agent import bash_limits_ms
from orchestrator.codex_agent import CodexRunner, program_cap
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
                    self.assertEqual(program_cap(cfg), bash_limits_ms(cfg)[1]/1000)

    def test_native_options_keep_shell_and_hooks_without_interactive_sessions(self):
        flags = process_options(container=True)
        self.assertIn(['--enable', 'shell_tool'], [flags[i:i+2] for i in range(len(flags)-1)])
        self.assertIn(['--disable', 'unified_exec'], [flags[i:i+2] for i in range(len(flags)-1)])
        self.assertIn(['--enable', 'hooks'], [flags[i:i+2] for i in range(len(flags)-1)])
        self.assertIn('projects={"/workspace"={trust_level="untrusted"}}', flags)

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
                ready.assert_called_once_with('test-container')
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
            (root/'session-id.jsonl').write_text('archived legacy copy')
            adapter = runtime('codex')
            self.assertTrue(adapter.archived('session-id', root))
            self.assertFalse(adapter.archived('session-id', root, state={'session_storage':'container'}))
            self.assertTrue(adapter.archived('session-id', root,
                            state={'session_storage':'container', 'archived':True}))

    async def test_container_session_cannot_be_modified_by_host_legacy_sync(self):
        from orchestrator.codex_session_environment import synchronize
        with patch('orchestrator.codex_session_environment.thread_metadata', AsyncMock()) as metadata:
            with self.assertRaisesRegex(ValueError, '容器会话不得用宿主'):
                await synchronize(Path('.'), {'engine':'codex','session_storage':'container'}, Path('.'), apply=True)
            metadata.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
