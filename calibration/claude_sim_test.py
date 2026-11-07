"""Claude 执行器的速通预置模拟器沿用 /sim 直接调用；Codex 的主控限额预测不受影响（见 orchestrator/sim_mode.py）。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.pausegate import PauseGate
from orchestrator.playback import Playback, MAGIC, HEADER
from orchestrator.sim_mode import agent_sim, controller_sim, sim_readme


def record(index):
    return HEADER.pack(float(index), 0, 0, 2, index, index * 8, 0, index * 8 + 8, 0, *([0] * 16)) + b'\x08\0\0\0'


class SimModeTests(unittest.TestCase):
    def test_executor_split(self):
        claude = RunConfig(agent='a', save='s', model='m', engine='claude', sim=True)
        codex = RunConfig(agent='a', save='s', model='m', engine='codex', sim=True)
        self.assertTrue(agent_sim(claude))
        self.assertFalse(controller_sim(claude))
        claude.pause_limit = True
        self.assertFalse(controller_sim(claude))          # Claude 的 --limit-planning 只记账，不改成主控预测
        self.assertEqual(list(sim_readme(claude)), ['/sim/README.md'])
        self.assertFalse(agent_sim(codex))
        self.assertIsNone(sim_readme(codex))
        self.assertFalse(controller_sim(codex))
        codex.pause_limit = True
        self.assertTrue(controller_sim(codex))


class PauseGateTests(unittest.TestCase):
    def test_accounting_only_and_published(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            metrics = Metrics(root / 'metrics.json', 'a', 'm', 'high', 60)
            path = root / 'game' / 'rounds.json'
            gate = PauseGate(metrics, enabled=True, log=lambda _: None, rounds_per_room=3, state_path=path)
            gate.charge('before start')                   # 准备阶段不扣
            self.assertEqual(gate.rounds_used, 0)
            gate.start()
            self.assertTrue(gate.on_room('sid', 'a-00', [0, 0, 640, 360]))
            self.assertEqual(gate.bank, 12)               # 两倍长、两倍高：3 × 4
            self.assertFalse(gate.on_room('sid', 'a-00', [0, 0, 640, 360]))
            gate.on_death(2)
            self.assertEqual(gate.bank, 14)
            for _ in range(20):
                gate.charge('search')
            self.assertEqual((gate.bank, gate.rounds_used), (0, 20))   # 用完只记账，不为负
            state = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual((state['limited'], state['left'], state['used']), (True, 0, 20))
            self.assertIn('只有调用模拟器的消息占一轮', gate.status_line())
            gate.on_room('other', 'b-00')                 # 换章节清零重算
            self.assertEqual(gate.bank, 3)
            disabled = PauseGate(metrics, enabled=False, state_path=path)
            self.assertFalse(json.loads(path.read_text(encoding='utf-8'))['limited'])
            self.assertEqual(disabled.status_line(), '')


class ClaudePlaybackChargeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = RunConfig(agent='test', save='test', model='test', engine='claude', sim=True, pause_limit=True)
        cfg.playback = True
        cfg.playback_stream_interval_s = 0
        self.metrics = Metrics(root / 'metrics.json', 'test', 'test', 'high', 60)
        session = SimpleNamespace(workspace=root / 'workspace', run_dir=root / 'run',
                                  last_obs={'level': {'bounds': [0, -100, 10000, 200]}}, decision_index=0,
                                  room_entry_frame=0)
        self.gate = SimpleNamespace(started=True, charge=Mock())
        self.playback = Playback(cfg, session, self.metrics, log=lambda _: None, gate=self.gate)
        self.addCleanup(self.playback.close)
        self.playback._call = Mock(return_value={'render_avg_ms': 1})

    def test_one_round_per_message_that_used_the_simulator(self):
        # 同一条消息里并行的两次推演：一轮
        self.playback.note_tool_use('m1', 'c1', 'Bash', {'command': 'python search.py', 'description': 'first search'})
        self.playback.note_tool_use('m1', 'c2', 'Bash', {'command': 'python search2.py', 'description': 'x'})
        (self.playback.dir / 'a.bin').write_bytes(MAGIC + record(1))
        (self.playback.dir / 'b.bin').write_bytes(MAGIC + record(2))
        self.playback.note_tool_result('c1')
        self.gate.charge.assert_not_called()
        self.playback.note_tool_result('c2')
        self.gate.charge.assert_called_once_with('first search')
        # 没有推演记录、也没用命令行的消息不占轮次
        self.playback.note_tool_use('m2', 'c3', 'Bash', {'command': 'ls', 'description': 'list'})
        self.playback.note_tool_result('c3')
        self.gate.charge.assert_called_once()
        # 命令行直接调用 celeste-sim：没有推演记录也记一轮
        self.playback.note_tool_use('m3', 'c4', 'Bash', {'command': '/sim/celeste-sim run x', 'description': 'cli'})
        self.playback.note_tool_result('c4')
        self.assertEqual([c.args[0] for c in self.gate.charge.call_args_list], ['first search', 'cli'])
        self.assertEqual(self.metrics.state.sim_calls, 2)

    def test_preparation_rollouts_are_not_charged(self):
        self.gate.started = False
        self.playback.note_tool_use('m1', 'c1', 'Bash', {'command': 'python search.py', 'description': 'prep'})
        (self.playback.dir / 'a.bin').write_bytes(MAGIC + record(1))
        self.playback.note_tool_result('c1')
        self.gate.charge.assert_not_called()
        self.assertEqual(self.metrics.state.sim_calls, 1)


class SimStateTests(unittest.TestCase):
    def test_only_claude_sim_writes_start_state_into_workspace(self):
        from orchestrator.game_session import GameSession
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            (workspace / 'game').mkdir()
            for engine, sim, expected in (('claude', True, True), ('claude', False, False), ('codex', True, False)):
                with self.subTest(engine=engine, sim=sim):
                    path = workspace / 'game' / 'sim_state.json'
                    path.unlink(missing_ok=True)
                    session = GameSession.__new__(GameSession)
                    session.cfg = RunConfig(agent='a', save='s', model='m', engine=engine, sim=sim, pause_limit=True)
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
        from orchestrator.prompt_environment import select_executor_blocks
        text = ('前\n<!-- executor:claude -->\nClaude 段\n<!-- /executor -->\n<!-- executor:codex -->\nCodex 段\n'
                '<!-- /executor -->\n后\n')
        self.assertEqual(select_executor_blocks(text, 'claude'), '前\nClaude 段\n后\n')
        self.assertEqual(select_executor_blocks(text, 'codex'), '前\nCodex 段\n后\n')
        self.assertEqual(select_executor_blocks(text.replace('\n', '\r\n'), 'codex'), '前\r\nCodex 段\r\n后\r\n')

    def test_simulator_documents_render_per_executor(self):
        from orchestrator.kb_layout import _write_view
        root = Path(__file__).resolve().parent.parent / 'kb'
        with tempfile.TemporaryDirectory() as folder:
            for engine in ('claude', 'codex'):
                cfg = RunConfig(agent='a', save='s', model='m', engine=engine, mode='speedrun', knowledge_profile='full')
                view = Path(folder) / engine
                with patch.object(RunConfig, 'kb_overlay_dir', property(lambda c: None)), \
                     patch.object(RunConfig, 'kb_extra_dir', property(lambda c: None)):
                    _write_view(cfg, root, view)
                methods = (view / 'search_methods.md').read_text(encoding='utf-8')
                files = (view / 'shared/tools/observe/files.md').read_text(encoding='utf-8')
                for text in (methods, files):
                    self.assertNotIn('<!-- executor:', text)
                    self.assertNotIn('<!-- /executor -->', text)
                if engine == 'claude':
                    self.assertIn('`/sim` 的 `Sim`、`SimPool`', methods)
                    self.assertIn('sim_state.json', files)
                    self.assertNotIn('request_id', methods)
                else:
                    self.assertIn('request_id', methods)
                    self.assertNotIn('sim_state.json', files)
                    self.assertIn('simulation/<receipt>.json', files)


if __name__ == '__main__':
    unittest.main()
