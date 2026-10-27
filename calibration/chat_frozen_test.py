"""中断后保持冻结的运行可以唤起聊天 / 整理工作区（之后再 --resume），会话管理仍要求先停止试玩。"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from orchestrator.chat import can_wake, can_wake_frozen, chat


def make_run(folder, **summary):
    run = Path(folder)
    (run / "run_state.json").write_text(json.dumps({"engine": "claude", "session_id": "abc"}), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return run


class ChatFrozenTests(unittest.TestCase):
    def test_interrupted_frozen_run_can_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, user_stopped=False, game_released=False)
            self.assertTrue(can_wake_frozen(run))
            self.assertFalse(can_wake(run))       # 会话管理仍不允许

    def test_released_or_completed_runs_are_not_frozen(self):
        for summary in ({"completed": False, "user_stopped": False, "game_released": True},
                        {"completed": False, "user_stopped": False},
                        {"completed": True, "game_released": False}):
            with tempfile.TemporaryDirectory() as folder:
                self.assertFalse(can_wake_frozen(make_run(folder, **summary)), summary)

    def test_no_session_or_unknown_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, game_released=False)
            (run / "run_state.json").write_text(json.dumps({"engine": "claude"}), encoding="utf-8")
            self.assertFalse(can_wake_frozen(run))
            (run / "run_state.json").write_text(json.dumps({"engine": "nope", "session_id": "x"}), encoding="utf-8")
            self.assertFalse(can_wake_frozen(run))
        self.assertFalse(can_wake_frozen(None))

    def test_naming_is_not_allowed_on_frozen_run(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, game_released=False)
            with self.assertRaises(ValueError):
                asyncio.run(chat(run, name_request="r1"))

    def test_neither_state_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            run = make_run(folder, completed=False, user_stopped=False, game_released=True)
            with self.assertRaises(ValueError) as caught:
                asyncio.run(chat(run, allow_programs=True))
            self.assertIn("中断后保持冻结", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class SuccessorSessionTests(unittest.TestCase):
    STATE = {'engine': 'claude', 'provider': 'anthropic', 'agent': 'claude-test', 'model': 'opus', 'requested_model': 'opus',
             'effort': 'high', 'save': 's', 'runtime': 'container', 'mcp_port': 0, 'knowledge_profile': 'explore',
             'session_id': 'old-session', 'session_name': '旧名字', 'kb_extra': 'local/kb_extra/x'}

    def make(self, folder):
        run = Path(folder) / 'runs' / 'claude-test' / '20261026-195131'
        run.mkdir(parents=True)
        (run / 'run_state.json').write_text(json.dumps(self.STATE), encoding='utf-8')
        (run / 'summary.json').write_text(json.dumps({'completed': True}), encoding='utf-8')
        (run / 'metrics.json').write_text(json.dumps({'elapsed_s': 5.0, 'decisions': 7}), encoding='utf-8')
        return run

    def test_successor_copies_settings_and_gets_a_new_pending_session(self):
        from orchestrator.chat import successor_run
        with tempfile.TemporaryDirectory() as folder:
            run = self.make(folder)
            new = successor_run(run)
            self.assertNotEqual(new, run)
            self.assertEqual(new.parent, run.parent)
            state = json.loads((new / 'run_state.json').read_text(encoding='utf-8'))
            self.assertNotEqual(state['session_id'], 'old-session')
            self.assertTrue(state['session_pending'])
            self.assertEqual(state['fresh_session_from'], 'old-session')
            self.assertEqual(state['resumed_from'], str(run))
            self.assertEqual(state['kb_extra'], 'local/kb_extra/x')
            self.assertNotIn('session_name', state)
            self.assertEqual(json.loads((new / 'metrics.json').read_text())['decisions'], 7)
            self.assertTrue(can_wake(new))
            self.assertEqual(json.loads((run / 'run_state.json').read_text())['session_id'], 'old-session')
            self.assertNotEqual(successor_run(run), new)          # 同一秒再开一次也不覆盖

    def test_fresh_chat_starts_a_new_session_then_resumes_it(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        from unittest.mock import patch
        calls, configs = [], []

        @asynccontextmanager
        async def fake_host(cfg, directory, metrics, container, **options):
            runner = SimpleNamespace(turns=0, control=SimpleNamespace(enqueue=lambda *a: None))

            async def run():
                runner.turns += 1
            runner.run = run
            calls.append((directory, options['session_id'], options['resume']))
            configs.append(cfg)
            yield runner
        caps = SimpleNamespace(needs_container=False, validate=lambda **k: None)
        with tempfile.TemporaryDirectory() as folder:
            run = self.make(folder)
            with patch('orchestrator.chat.host_runner', fake_host), \
                 patch('orchestrator.chat.workspace_capabilities', return_value=caps), \
                 patch('orchestrator.runtimes.Runtime.token', return_value='t'):
                asyncio.run(chat(run, allow_programs=True, fresh_session=True))
                new, session, resume = calls[0]
                self.assertNotEqual(new, run)
                self.assertFalse(resume)
                state = json.loads((new / 'run_state.json').read_text(encoding='utf-8'))
                self.assertEqual(state['session_id'], session)
                self.assertNotIn('session_pending', state)           # 第一轮之后按编号接续
                asyncio.run(chat(new, allow_programs=True))
                self.assertEqual(calls[1], (new, session, True))
                self.assertFalse(configs[0].formal_thought)
                state = json.loads((new / 'run_state.json').read_text(encoding='utf-8'))
                state.update(formal_thought=True, stiffness=True, save_context=True, collision_image='room')
                (new / 'run_state.json').write_text(json.dumps(state), encoding='utf-8')
                asyncio.run(chat(new, allow_programs=True))
                cfg = configs[2]
                self.assertTrue(cfg.formal_thought and cfg.stiffness and cfg.save_context)
                self.assertEqual(cfg.collision_image, 'room')


class ChooseSessionTests(unittest.TestCase):
    def test_choices(self):
        from orchestrator.run import choose_session
        self.assertEqual(choose_session({'state': {'session_id': 'a'}}, False), ('a', 'a'))
        self.assertEqual(choose_session({'state': {'session_id': 'a', 'session_pending': True}}, False), (None, 'a'))
        fresh = choose_session({'state': {'session_id': 'a'}}, True)
        self.assertIsNone(fresh[0])
        self.assertNotEqual(fresh[1], 'a')
        self.assertEqual(choose_session(None, False, {'session_id': 'p'}), ('p', 'p'))
        self.assertIsNone(choose_session(None, False)[0])
