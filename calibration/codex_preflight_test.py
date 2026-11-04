"""A real completion boundary gates game startup; RPC and bridge are isolated."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from orchestrator.codex_container import CodexLoginRequired
from orchestrator.codex_preflight import verify_model


class CodexPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_thread_and_turn_must_complete_with_target_model(self):
        for status in ('completed', 'failed'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                def rpc_factory(_, *, on_event, container):
                    self.assertEqual(container, 'workspace-container')

                    async def request(method, params):
                        if method == 'thread/start':
                            self.assertEqual(params['model'], 'selected-model')
                            self.assertEqual(params['permissions'], 'celeste-read')
                            self.assertTrue(params['ephemeral'])
                            self.assertNotIn('dynamicTools', params)
                            return {'thread': {'id': 'probe'}, 'activePermissionProfile': {'id': 'celeste-read'}}
                        self.assertEqual(method, 'turn/start')
                        self.assertEqual(params['threadId'], 'probe')
                        on_event('turn/completed', {'threadId': 'unrelated',
                                                    'turn': {'id': 'target', 'status': 'completed'}})
                        on_event('turn/completed', {'threadId': 'probe',
                                                    'turn': {'id': 'old', 'status': 'completed'}})
                        on_event('turn/completed', {'threadId': 'probe', 'turn': {'id': 'target', 'status': status,
                            **({'error': {'message': 'workspace routing discovery unauthorized (401)'}}
                               if status == 'failed' else {})}})
                        return {'turn': {'id': 'target'}}

                    return SimpleNamespace(start=AsyncMock(), close=close, request=request)

                close = AsyncMock()
                cfg = SimpleNamespace(model='selected-model', effort='low')
                with patch('orchestrator.codex_preflight.CodexRPC', side_effect=rpc_factory):
                    if status == 'failed':
                        with self.assertRaisesRegex(CodexLoginRequired, '--container workspace-container login'):
                            await verify_model(cfg, 'workspace-container', Path(directory))
                    else:
                        await verify_model(cfg, 'workspace-container', Path(directory))
                close.assert_awaited_once()


class GameStartupTests(unittest.TestCase):
    def test_auth_preflight_failure_happens_before_bridge_recording_and_run_publication(self):
        from orchestrator import run
        from orchestrator.config import RunConfig
        cfg = RunConfig(agent='fixture', save='test', engine='codex', model='selected-model')
        args = SimpleNamespace(prepared=None, previous=None, no_agent=False)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(run, 'REPO_ROOT', Path(folder)), \
                patch.object(run, 'parse_args', return_value=(cfg, args)), \
                patch.object(run, 'port_free', return_value=True), \
                patch.object(run, 'ensure_container', return_value='workspace-container'), \
                patch('orchestrator.codex_preflight.verify_model',
                      side_effect=CodexLoginRequired('workspace-container')), \
                patch('orchestrator.bridge_requirements.preflight_bridge') as bridge, \
                patch.object(run, 'ensure_obs') as obs, patch.object(run, 'make_run_dir') as publish:
            self.assertEqual(run.main(), 2)
        bridge.assert_not_called()
        obs.assert_not_called()
        publish.assert_not_called()

    def test_probe_success_passes_verified_container_to_game_startup(self):
        from orchestrator import run
        from orchestrator.config import RunConfig
        cfg = RunConfig(agent='fixture', save='test', engine='codex', model='selected-model')
        args = SimpleNamespace(prepared=None, previous=None, no_agent=False)
        verified = []

        async def probe(config, container, directory):
            self.assertIs(config, cfg)
            verified.append(container)

        def bridge_preflight(_):
            self.assertEqual(verified, ['workspace-container'])
            self.assertEqual(args.codex_preflight, ('workspace-container', cfg.mcp_port))
            raise RuntimeError('fixture bridge boundary')

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(run, 'REPO_ROOT', Path(folder)), \
                patch.object(run, 'parse_args', return_value=(cfg, args)), \
                patch.object(run, 'port_free', return_value=True), \
                patch.object(run, 'ensure_container', return_value='workspace-container') as ensure, \
                patch('orchestrator.codex_preflight.verify_model', side_effect=probe), \
                patch('orchestrator.bridge_requirements.preflight_bridge', side_effect=bridge_preflight), \
                self.assertRaisesRegex(RuntimeError, 'fixture bridge boundary'):
            run.main()
        ensure.assert_called_once()
