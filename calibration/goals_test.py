import asyncio
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from orchestrator.goals import GoalManager, validate_goal
from orchestrator.game_session import GameSession
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from tools.bridge_client import BridgeClient


BASE = {"room":"start", "map":"Celeste/1-ForsakenCity:B", "side":"B", "map_complete":False,
        "deaths":2, "frame":100, "elapsed":10, "dead":False}


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.goal = GoalManager(self.directory)

    def tearDown(self):
        self.temp.cleanup()

    def set_goal(self, *conditions, match="all"):
        self.goal.set({"text":"探索支路", "conditions":list(conditions), "match":match}, BASE)

    def test_text_goal_can_be_updated_cleared_and_never_stops_by_itself(self):
        self.set_goal()
        self.assertFalse(self.goal.check({**BASE,"frame":9999}))
        self.set_goal({"field":"room","op":"changed"})
        revision = self.goal.snapshot()["revision"]
        self.goal.clear()
        self.assertGreater(self.goal.snapshot()["revision"], revision)
        self.assertFalse(self.goal.check({**BASE,"room":"next"}))
        self.assertFalse(GoalManager(self.directory).snapshot()["active"])

    def test_room_rule_ignores_death_and_other_maps(self):
        self.set_goal({"field":"room","op":"changed"})
        self.assertFalse(self.goal.check({**BASE,"room":"next","dead":True}))
        self.assertFalse(self.goal.check({**BASE,"room":"next","map":"another:A"}))
        self.assertTrue(self.goal.check({**BASE,"room":"next"}))

    def test_current_map_completion_includes_side(self):
        self.set_goal({"field":"map_complete","op":"eq","value":True})
        self.assertFalse(self.goal.check({**BASE,"map":"Celeste/1-ForsakenCity:A","map_complete":True}))
        self.assertTrue(self.goal.check({**BASE,"map_complete":True}))

    def test_any_all_and_relative_thresholds(self):
        death={"field":"deaths","op":"delta_gte","value":2}
        frame={"field":"frame","op":"delta_gte","value":60}
        self.set_goal(death, frame)
        self.assertFalse(self.goal.check({**BASE,"deaths":4,"frame":159}))
        self.assertTrue(self.goal.check({**BASE,"deaths":4,"frame":160}))
        other = GoalManager(self.directory / "other")
        (self.directory / "other").mkdir()
        other.set({"text":"", "conditions":[death,frame], "match":"any"}, BASE)
        self.assertTrue(other.check({**BASE,"frame":160}))

    def test_invalid_rules_are_rejected(self):
        for condition in ({"field":"unknown","op":"eq","value":1},
                          {"field":"frame","op":"gte","value":float('inf')},
                          {"field":"room","op":"gte","value":2}):
            with self.assertRaises(ValueError):
                validate_goal({"conditions":[condition]})

    def session(self):
        session=GameSession.__new__(GameSession)
        session.goals=self.goal; session._goal_progress={}; session.done=False; session.done_reason=""; session.user_stopped=False
        session.last_obs={"frame":100,"level":{"sid":"Celeste/1-ForsakenCity","side":"B","room":"start","deaths":2},"player":{"dead":False}}
        session.metrics=SimpleNamespace(state=SimpleNamespace(chapter="Celeste/1-ForsakenCity",room="start",chapter_time_ticks=100),
                                        elapsed=lambda:10, update=Mock(), add_split=Mock())
        session.client=SimpleNamespace(cancel_pending=Mock())
        session.chapter_queue=[2];session.chapter_finished=False
        return session

    def test_running_action_cancel_uses_progress_and_is_sent_once(self):
        session=self.session()
        self.set_goal({"field":"room","op":"changed"})
        event={"event":"progress","sid":"Celeste/1-ForsakenCity","room":"next","frame":106,"dead":False}
        session._on_progress(event);session._on_progress(event)
        session.client.cancel_pending.assert_called_once()
        session.finish_user_stop()
        self.assertTrue(session.done)
        self.assertTrue(session.user_stopped)

    def test_map_stop_prevents_automatic_next_chapter(self):
        session=self.session()
        self.set_goal({"field":"map_complete","op":"eq","value":True})
        session._chapter_complete()
        self.assertEqual(session.chapter_queue, [])
        self.assertTrue(session.done)
        self.assertEqual(session.done_reason,"章节完成")
        self.assertTrue(session.user_stopped)

    def test_prologue_can_finish_on_overworld_without_advancing_next_chapter(self):
        session=self.session()
        session.done=True;session.done_reason='章节完成';session.chapter_queue=[]
        session.start_sid='Celeste/0-Intro';session.lock=threading.Lock();session.metrics.add=Mock()
        session.client.call=Mock(return_value={'obs':{'scene':'Overworld'}})
        session.finish_at_results()
        self.assertEqual(session.end_scene,'Overworld')
        session.client.call.assert_called_once_with('observe',image=False,entities=False,tiles=False)

    def test_regular_chapter_still_requires_expected_result_scene(self):
        session=self.session()
        session.done=True;session.done_reason='章节完成';session.chapter_queue=[]
        session.start_sid='Celeste/1-ForsakenCity';session.lock=threading.Lock()
        session.client.call=Mock(return_value={'obs':{'scene':'Overworld'}})
        with self.assertRaisesRegex(RuntimeError,'意外场景'):
            session.finish_at_results()

    def test_same_connection_cancel_preserves_next_reply(self):
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
        errors=[]
        def serve():
            try:
                conn,_=listener.accept()
                with conn, conn.makefile('rwb') as stream:
                    request=json.loads(stream.readline())
                    stream.write(b'{"event":"progress"}\n');stream.flush()
                    cancel=json.loads(stream.readline())
                    self.assertEqual(cancel,{"id":0,"cmd":"cancel"})
                    stream.write((json.dumps({"id":request['id'],"ok":True,"result":{"stop":"cancelled"}})+'\n').encode())
                    stream.write(b'{"id":0,"ok":true}\n');stream.flush()
                    observe=json.loads(stream.readline())
                    stream.write((json.dumps({"id":observe['id'],"ok":True,"obs":{"frame":106}})+'\n').encode());stream.flush()
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        client=BridgeClient(port=listener.getsockname()[1],timeout=3)
        stop_requested=False
        client.cancel_check=lambda:stop_requested
        def request_stop(event):
            nonlocal stop_requested
            stop_requested=True
        try:
            self.assertEqual(client.call('step',on_event=request_stop)['result']['stop'],'cancelled')
            with self.assertRaises(InterruptedError):client.call('step')
            self.assertEqual(client.call('observe')['obs']['frame'],106)
        finally:client.close();listener.close();thread.join(3)
        self.assertFalse(errors)


class StopControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_stop_waits_for_current_action_before_finalizing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);goal=GoalManager(root)
            metrics=Metrics(root/'metrics.json','test','model','medium',600)
            session=SimpleNamespace(done=False,goals=goal,goal_values=lambda:BASE)
            session.finish_user_stop=lambda:setattr(session,'done',True)
            runner=CodexRunner(RunConfig(agent='test',save='test',model='model'),root,session,metrics,'container')
            runner.rpc.request=AsyncMock(return_value={});runner.turn_active=True;runner.turn_id='turn';runner.session_id='thread'
            await runner.action_lock.acquire()
            runner.control.enqueue('stop')
            task=asyncio.create_task(runner._controls())
            try:
                async with asyncio.timeout(3):
                    while not goal.stop_motion.is_set():await asyncio.sleep(.01)
                self.assertFalse(session.done)
                runner.action_lock.release()
                async with asyncio.timeout(3):
                    while not session.done:await asyncio.sleep(.01)
                self.assertTrue(runner.paused)
            finally:
                task.cancel();await asyncio.gather(task,return_exceptions=True)


if __name__=='__main__':unittest.main()
