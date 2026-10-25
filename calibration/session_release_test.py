"""Every finished run releases its own bridge, including stopped/error/cancelled runs."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from orchestrator.game_session import GameSession
from orchestrator.run import _keeps_game_frozen, _run_session


class ReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_interrupt_closes_socket_without_deactivation_and_records_resumability(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            cfg = SimpleNamespace(engine='codex', _keep_frozen=False)
            args = SimpleNamespace(no_agent=False)
            client = SimpleNamespace(call=Mock(), close=Mock())
            session = GameSession.__new__(GameSession)
            session.client = client
            session.done = False
            session._frames_log = Mock()
            session._decisions_log = Mock()

            async def finish(*_):
                cfg._keep_frozen = _keeps_game_frozen(cfg, args, session, interrupted=True)
                return {'completed': False, 'stop_reason': '用户中断'}

            with patch('orchestrator.run.GameSession', return_value=session), \
                 patch('orchestrator.run._run_connected_session', side_effect=finish):
                result = await _run_session(cfg, args, directory, None, None, None, None)
            client.call.assert_not_called()
            client.close.assert_called_once()
            session._frames_log.close.assert_called_once()
            self.assertFalse(result['game_released'])
            self.assertTrue((directory/'summary.json').exists())

    async def test_recording_finishes_before_game_is_released_for_all_stop_reasons(self):
        for completed,user_stopped in [(True,False),(True,True),(False,True),(False,False)]:
            with self.subTest(completed=completed,user_stopped=user_stopped),tempfile.TemporaryDirectory() as folder:
                events=[]
                session=SimpleNamespace(close=Mock(side_effect=lambda **_:events.append('release')))
                async def finish(*args):
                    events.append('recording_finished')
                    return {'completed':completed,'user_stopped':user_stopped}
                with patch('orchestrator.run.GameSession',return_value=session),patch('orchestrator.run._run_connected_session',side_effect=finish):
                    result=await _run_session(None,None,Path(folder),None,None,None,None)
                session.close.assert_called_once_with(release=True)
                self.assertEqual(events,['recording_finished','release'])
                self.assertTrue(result['game_released'])

    async def test_early_error_and_cancellation_still_release_game(self):
        for error in [RuntimeError('startup/cleanup failed'),asyncio.CancelledError()]:
            with self.subTest(error=type(error).__name__),tempfile.TemporaryDirectory() as folder:
                session=SimpleNamespace(close=Mock())
                with patch('orchestrator.run.GameSession',return_value=session), \
                     patch('orchestrator.run._run_connected_session',AsyncMock(side_effect=error)), \
                     self.assertRaises(type(error)):
                    await _run_session(None,None,Path(folder),None,None,None,None)
                session.close.assert_called_once_with(release=True)


class CloseTests(unittest.TestCase):
    def test_bridge_socket_and_logs_close_even_if_deactivate_fails(self):
        session=GameSession.__new__(GameSession)
        client=SimpleNamespace(call=Mock(side_effect=RuntimeError('lost reply')),close=Mock())
        session.client=client
        session._frames_log=Mock();session._decisions_log=Mock()
        with self.assertRaises(RuntimeError):session.close()
        client.call.assert_called_once_with('deactivate')
        client.close.assert_called_once()
        session._frames_log.close.assert_called_once()
        session._decisions_log.close.assert_called_once()
        self.assertIsNone(session.client)
        session.close() # idempotent: no second command on a closed socket
        client.call.assert_called_once()


if __name__=='__main__':unittest.main()
