"""Exercise preparation's cold model discovery before any container or game work."""
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from orchestrator.preparation import prepare


class ContainerBoundary(Exception):
    pass


class PreparationModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.args = SimpleNamespace(engine='codex', provider='codex', agent='preparation-fixture',
                                    model='gpt-6.1-terra', effort='high', knowledge='explore',
                                    max_minutes=1, output=str(Path(self.temp.name)/'prepared'))
        self.catalog_threads = []

        async def start():
            self.catalog_threads.append(threading.get_ident())

        self.rpc = SimpleNamespace(start=AsyncMock(side_effect=start), close=AsyncMock(),
                                   request=AsyncMock(return_value={'data': [{
                                       'model': 'gpt-6.1-terra', 'displayName': 'Terra',
                                       'inputModalities': ['text', 'image'],
                                       'supportedReasoningEfforts': [{'reasoningEffort': 'high'}],
                                       'defaultReasoningEffort': 'high'}]}))
        self.container = Mock(side_effect=ContainerBoundary('no real container is created'))
        for target, value in [('orchestrator.model_catalog._cached', None),
                              ('orchestrator.model_catalog._loaded', 0),
                              ('orchestrator.model_catalog.CodexRPC', Mock(return_value=self.rpc)),
                              ('orchestrator.model_catalog.ensure_account_container', Mock(return_value='account-fixture')),
                              ('orchestrator.preparation.ensure_container', self.container)]:
            replacement = patch(target, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def test_cold_discovery_resolves_before_container_outside_preparation_loop(self):
        preparation_thread = threading.get_ident()
        with self.assertRaises(ContainerBoundary):
            await prepare(self.args)
        cfg = self.container.call_args.args[0]
        self.assertEqual((cfg.model, cfg.effort), ('gpt-6.1-terra', 'high'))
        self.assertEqual(self.rpc.request.call_args.args[0], 'model/list')
        self.assertEqual(len(self.catalog_threads), 1)
        self.assertNotEqual(self.catalog_threads[0], preparation_thread)
        self.rpc.close.assert_awaited_once()

    async def test_unknown_model_is_rejected_before_container_or_output_creation(self):
        self.args.model = 'unlisted-model'
        with self.assertRaisesRegex(ValueError, '模型不在当前列表中'):
            await prepare(self.args)
        self.container.assert_not_called()
        self.assertFalse(Path(self.args.output).exists())
        self.rpc.close.assert_awaited_once()

    async def test_discovery_failure_closes_rpc_without_creating_container(self):
        self.rpc.request.side_effect = RuntimeError('catalog offline')
        with self.assertRaisesRegex(RuntimeError, 'catalog offline'):
            await prepare(self.args)
        self.container.assert_not_called()
        self.assertFalse(Path(self.args.output).exists())
        self.rpc.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
