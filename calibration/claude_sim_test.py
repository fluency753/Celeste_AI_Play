"""速通预置模拟器按是否限额区分：不限额时 Claude 直接调用 /sim，限额时两个执行器都经主控预测（orchestrator/sim_mode.py）。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.sim_mode import agent_sim, controller_sim, kb_variant, sim_readme


class SimModeTests(unittest.TestCase):
    def test_split_by_limit(self):
        claude = RunConfig(agent='a', save='s', model='m', engine='claude', sim=True)
        codex = RunConfig(agent='a', save='s', model='m', engine='codex', sim=True)
        self.assertTrue(agent_sim(claude))
        self.assertFalse(controller_sim(claude))
        self.assertEqual(list(sim_readme(claude)), ['/sim/README.md'])
        self.assertEqual(kb_variant(claude), 'direct')
        claude.pause_limit = True                          # 限额：改走主控，不挂 /sim
        self.assertFalse(agent_sim(claude))
        self.assertTrue(controller_sim(claude))
        self.assertIsNone(sim_readme(claude))
        self.assertEqual(kb_variant(claude), 'controller')
        self.assertFalse(agent_sim(codex))
        self.assertIsNone(sim_readme(codex))
        self.assertEqual(kb_variant(codex), 'controller')
        codex.pause_limit = True
        self.assertTrue(controller_sim(codex))
        explore = RunConfig(agent='a', save='s', model='m', engine='claude', mode='explore')
        self.assertEqual(kb_variant(explore), 'direct')


class SimStateTests(unittest.TestCase):
    def test_only_claude_sim_writes_start_state_into_workspace(self):
        from orchestrator.game_session import GameSession
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / 'game').mkdir()
            for engine, sim, limit, expected in (('claude', True, False, True), ('claude', False, False, False),
                                                 ('claude', True, True, False), ('codex', True, True, False)):
                with self.subTest(engine=engine, sim=sim, limit=limit):
                    path = workspace / 'game' / 'sim_state.json'
                    path.unlink(missing_ok=True)
                    session = GameSession.__new__(GameSession)
                    session.cfg = RunConfig(agent='a', save='s', model='m', engine=engine, sim=sim, pause_limit=limit)
                    session.workspace = workspace
                    session.client = SimpleNamespace(call=Mock(return_value={'state': {'frame': 7}}))
                    session._write_sim_state()
                    self.assertEqual(path.is_file(), expected)
                    if expected:
                        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'frame': 7})
                        session.client.call.assert_called_once_with('sim_state', margin=4)
                        session.client = SimpleNamespace(call=Mock(side_effect=RuntimeError('bridge')))
                        session._write_sim_state()
                        self.assertFalse(path.exists())   # 取不到时删掉旧文件，免得用错帧
                    else:
                        session.client.call.assert_not_called()


class KnowledgeExecutorBlockTests(unittest.TestCase):
    def test_blocks_keep_only_current_executor(self):
        from orchestrator.prompt_environment import select_sim_blocks
        text = ('前\n<!-- sim:direct -->\n直接段\n<!-- /sim -->\n<!-- sim:controller -->\n主控段\n'
                '<!-- /sim -->\n后\n')
        self.assertEqual(select_sim_blocks(text, 'direct'), '前\n直接段\n后\n')
        self.assertEqual(select_sim_blocks(text, 'controller'), '前\n主控段\n后\n')
        self.assertEqual(select_sim_blocks(text.replace('\n', '\r\n'), 'controller'), '前\r\n主控段\r\n后\r\n')

    def test_simulator_documents_render_per_executor(self):
        from orchestrator.kb_layout import _write_view
        root = Path(__file__).resolve().parent.parent / 'kb'
        with tempfile.TemporaryDirectory() as folder:
            for engine, limit in (('claude', False), ('claude', True), ('codex', True)):
                cfg = RunConfig(agent='a', save='s', model='m', engine=engine, mode='speedrun', knowledge_profile='full',
                                sim=True, pause_limit=limit)
                view = Path(folder) / f'{engine}-{limit}'
                with patch.object(RunConfig, 'kb_overlay_dir', property(lambda c: None)), \
                     patch.object(RunConfig, 'kb_extra_dir', property(lambda c: None)):
                    _write_view(cfg, root, view)
                methods = (view / 'search_methods.md').read_text(encoding='utf-8')
                files = (view / 'shared/tools/observe/files.md').read_text(encoding='utf-8')
                for text in (methods, files):
                    self.assertNotIn('<!-- sim:', text)
                    self.assertNotIn('<!-- /sim -->', text)
                if not limit:
                    self.assertIn('`/sim` 的 `Sim`、`SimPool`', methods)
                    self.assertIn('sim_state.json', files)
                    self.assertNotIn('request_id', methods)
                else:
                    self.assertIn('request_id', methods)
                    self.assertNotIn('sim_state.json', files)
                    self.assertIn('simulation/<receipt>.json', files)


if __name__ == '__main__':
    unittest.main()
