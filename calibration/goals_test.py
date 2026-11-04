import asyncio
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

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

    def test_failed_completion_write_can_be_retried_without_a_partial_commit(self):
        self.set_goal()
        before = self.goal.snapshot()
        with patch.object(Path, 'replace', side_effect=PermissionError('fixture')):
            with self.assertRaises(PermissionError):
                self.goal.report_completion('探索支路', '走到了尽头', BASE)
        self.assertEqual(self.goal.snapshot(), before)
        self.assertEqual(GoalManager(self.directory).snapshot(), before)
        report = self.goal.report_completion('探索支路', '走到了尽头', BASE)
        self.assertEqual(GoalManager(self.directory).snapshot()['completion_report'], report)
        self.assertFalse(self.goal.check(BASE))

    def test_clearing_registered_goal_withdraws_permission_to_report_it(self):
        self.set_goal()
        self.goal.clear()
        restored = GoalManager(self.directory)
        with self.assertRaisesRegex(ValueError, '用户已清除自定义目标'):
            restored.report_completion('探索支路', '走到了尽头', BASE)

    def test_registered_goal_is_added_to_opening_without_replacing_preparation(self):
        from orchestrator.session_controls import SessionControls
        self.set_goal({'field': 'room', 'op': 'changed'})
        controller = SimpleNamespace(session=self.session(), resume_notice='')
        message = SessionControls._turn_message(controller, '先读必读清单。', [], opening=True)
        self.assertIn('先读必读清单。', message)
        self.assertIn('探索支路', message)
        self.assertIn('房间发生变化', message)

    def test_pending_start_goal_survives_reload_and_excludes_menu_time_and_rooms(self):
        conditions = [{'field': 'game_elapsed', 'op': 'delta_gte', 'value': 1},
                      {'field': 'room', 'op': 'changed'}]
        self.goal.set({'conditions': conditions}, BASE, wait_for_entry=True)
        restored = GoalManager(self.directory)
        self.assertFalse(restored.check({**BASE, 'frame': 5000, 'room': 'stale'}))
        self.assertTrue(restored.time_status(BASE)['awaiting_entry'])
        entered = {**BASE, 'frame': 6000, 'room': 'first', 'in_level': True}
        self.assertFalse(restored.check(entered))
        self.assertFalse(restored.check({**entered, 'frame': 6059, 'room': 'second'}))
        self.assertTrue(restored.check({**entered, 'frame': 6060, 'room': 'second'}))

    def test_first_entry_seeds_confirmed_side_for_progress_in_same_action(self):
        session = self.session()
        level = {**session.last_obs, 'scene': 'Level'}
        session.last_obs = {'scene': 'Overworld', 'frame': 90}
        session.goals.set({'conditions': [{'field': 'side', 'op': 'eq', 'value': 'A'}]},
                          session.goal_values(), wait_for_entry=True)
        session._note_first_goal_entry(level)
        session._on_progress({'event': 'progress', 'sid': BASE['map'].rsplit(':', 1)[0], 'frame': 106})
        self.assertEqual(session.goal_values()['side'], 'B')
        self.assertFalse(session.goals.stop_motion.is_set())

    def test_park_preserves_goal_progress_and_excludes_other_runs_and_menu_frames(self):
        self.set_goal({'field':'game_elapsed', 'op':'delta_gte', 'value':1},
                      {'field':'frame', 'op':'delta_gte', 'value':60})
        before = {**BASE, 'frame':145, 'in_level':True}
        self.assertFalse(self.goal.check(before))
        original = self.goal.snapshot()
        self.goal.resume_from_park(145)
        restored = GoalManager(self.directory)
        self.assertFalse(restored.check({**BASE, 'frame':5000, 'in_level':False}))
        self.assertEqual(restored.time_status({'frame':5000})['time_conditions'][0]['used_seconds'], .75)
        entered = {**BASE, 'frame':6000, 'in_level':True}
        self.assertFalse(restored.check(entered))
        self.assertEqual(restored.snapshot()['revision'], original['revision'])
        self.assertFalse(restored.check({**entered, 'frame':6014}))
        self.assertTrue(restored.check({**entered, 'frame':6015}))

    def session(self):
        session=GameSession.__new__(GameSession)
        session.goals=self.goal; session._goal_progress={}; session.done=False; session.done_reason=""; session.user_stopped=False
        session.last_obs={"frame":100,"level":{"sid":"Celeste/1-ForsakenCity","side":"B","room":"start","deaths":2},"player":{"dead":False}}
        session.metrics=SimpleNamespace(state=SimpleNamespace(chapter="Celeste/1-ForsakenCity",room="start",chapter_time_ticks=100),
                                        elapsed=lambda:10, update=Mock(), add_split=Mock(),add=Mock())
        session.client=SimpleNamespace(cancel_pending=Mock())
        session.chapter_queue=[2];session.chapter_finished=False;session._pending_complete=False
        session.cfg=SimpleNamespace(auto_speed=1,cutscene_mode='auto')
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

    def test_map_complete_goal_waits_for_ending_story(self):
        session=self.session()
        self.set_goal({'field':'map_complete','op':'eq','value':True})
        obs={'scene':'Level','level':{'sid':'Celeste/1-ForsakenCity','side':'B','completed':True,'inCutscene':True}}
        self.assertFalse(session.goal_values(obs)['map_complete'])
        self.assertFalse(self.goal.check(session.goal_values(obs)))
        obs['level']['inCutscene']=False
        self.assertTrue(self.goal.check(session.goal_values(obs)))

    def test_completed_goal_stop_still_uses_chapter_results_path(self):
        session=self.session();session._pending_complete=True
        session.finish_user_stop()
        self.assertEqual(session.chapter_queue,[])
        self.assertEqual(session.done_reason,'章节完成')
        self.assertTrue(session.user_stopped)

    def test_immediate_stop_does_not_wait_for_results(self):
        session=self.session();session._pending_complete=True;self.goal.immediate=True
        session.finish_user_stop()
        self.assertNotEqual(session.done_reason,'章节完成')

    def test_agent_map_goal_waits_for_chapter_select_and_preserves_c_side(self):
        session=self.session();session.cfg.cutscene_mode='agent';session._pending_complete=True
        session.metrics.state.chapter_side='C'
        for obs in ({'scene':'Level','level':{'completed':True}}, {'scene':'AreaComplete'},
                    {'scene':'Overworld','menu':{'current':'OuiChapterPanel'}},
                    {'scene':'Overworld','menu':{'current':'OuiChapterSelect','next':'OuiChapterPanel'}}):
            self.assertFalse(session.goal_values(obs)['map_complete'])
        values=session.goal_values({'scene':'Overworld','menu':{'current':'OuiChapterSelect'}})
        self.assertTrue(values['map_complete']);self.assertEqual(values['side'],'C')

    def test_agent_results_cleanup_never_supplies_inputs(self):
        session=self.session();session.cfg.cutscene_mode='agent';session.done=True
        session.done_reason='章节完成';session.chapter_queue=[];session.end_scene='Level'
        session.client.call=Mock()
        with self.assertRaisesRegex(RuntimeError,'不会代按'):session.finish_at_results()
        session.end_scene='Overworld';session.finish_at_results()
        session.client.call.assert_not_called()

    def test_results_are_displayed_then_confirmed_and_no_confirm_is_sent_on_map(self):
        session=self.session();session.done=True;session.done_reason='章节完成';session.chapter_queue=[]
        session.start_sid='Celeste/1-ForsakenCity';session.lock=threading.Lock()
        session.metrics.add=Mock();session._before_advance=Mock();session._log_frames=Mock()
        session.client=SimpleNamespace(call=Mock(side_effect=[{'obs':{'scene':'AreaComplete'}},
            {'obs':{'scene':'AreaComplete'},'result':{'consumed':120}},
            {'obs':{'scene':'Overworld','menu':{'current':'OuiChapterPanel'}},'result':{'consumed':30}},
            {'obs':{'scene':'Overworld','menu':{'current':'OuiChapterSelect'}},'result':{'consumed':30}},
            {'obs':{'scene':'Overworld','menu':{'current':'OuiChapterSelect'}},'result':{'consumed':60}}]))
        session.finish_at_results()
        self.assertEqual([call.kwargs['inputs'] for call in session.client.call.call_args_list[1:]],[['120'],['1,C','29'],['1,B','29'],['60']])
        self.assertEqual(session.end_scene,'Overworld')

    def test_prologue_can_finish_on_overworld_without_advancing_next_chapter(self):
        session=self.session()
        session.done=True;session.done_reason='章节完成';session.chapter_queue=[]
        session.start_sid='Celeste/0-Intro';session.lock=threading.Lock();session.metrics.add=Mock()
        session._before_advance=Mock();session._log_frames=Mock()
        session.client.call=Mock(return_value={'obs':{'scene':'Overworld','menu':{'current':'OuiChapterSelect'}},'result':{'consumed':60}})
        session.finish_at_results()
        self.assertEqual(session.end_scene,'Overworld')
        self.assertEqual(session.client.call.call_args.kwargs['inputs'],['60'])

    def test_regular_chapter_can_stop_on_map_without_entering_next_chapter(self):
        session=self.session()
        session.done=True;session.done_reason='章节完成';session.chapter_queue=[]
        session.start_sid='Celeste/1-ForsakenCity';session.lock=threading.Lock()
        session._before_advance=Mock();session._log_frames=Mock()
        session.client.call=Mock(return_value={'obs':{'scene':'Overworld','menu':{'current':'OuiChapterSelect'}},'result':{'consumed':60}})
        session.finish_at_results()
        self.assertEqual(session.client.call.call_count,2)
        self.assertEqual(session.end_scene,'Overworld')

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
    async def test_park_refuses_live_level_then_accepts_saved_menu_without_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = RunConfig(agent='test', save='test', save_slot=8, model='test', engine='codex')
            metrics = Metrics(root / 'metrics.json', 'test', 'test', 'medium', 600)
            session = SimpleNamespace(done=False, goals=GoalManager(root), goal_values=lambda: BASE)
            session.bridge_call = Mock(side_effect=[
                {'obs':{'scene':'Level', 'frame':100}},
                {'obs':{'scene':'Overworld', 'menu':{'current':'OuiFileSelect'}, 'saving':False, 'frame':105}},
            ])
            runner = CodexRunner(cfg, root, session, metrics, 'container', start_paused=True)
            first = runner.control.enqueue('park')
            task = asyncio.create_task(runner._controls())
            try:
                async with asyncio.timeout(3):
                    while runner.control.snapshot()['actions'][0]['status'] == 'queued':
                        await asyncio.sleep(.01)
                failed = runner.control.snapshot()['actions'][0]
                self.assertEqual((failed['id'], failed['status']), (first, 'error'))
                self.assertFalse(runner.stop_requested)
                accepted = runner.control.enqueue('park')
                await asyncio.wait_for(task, 3)
                self.assertEqual((runner.parked_for_switch, runner.parked_frame), (True, 105))
                self.assertEqual(runner.control.snapshot()['actions'][0]['id'], accepted)
                self.assertEqual([call.args for call in session.bridge_call.call_args_list],
                                 [('observe',), ('observe',)])
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                runner.close_events()

    async def test_wall_duration_expires_without_any_game_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with patch('orchestrator.metrics.time.monotonic',return_value=1000):
                metrics=Metrics(root/'metrics.json','test','model','medium',600)
                metrics.start_play()
            goal=GoalManager(root)
            goal.set({'conditions':[{'field':'elapsed','op':'delta_gte','value':2}]},{**BASE,'elapsed':0})
            session=SimpleNamespace(done=False,goals=goal,goal_values=lambda:{**BASE,'elapsed':metrics.elapsed()})
            session.finish_user_stop=Mock(side_effect=lambda:setattr(session,'done',True))
            runner=CodexRunner(RunConfig(agent='test',save='test',model='model'),root,session,metrics,'container')
            try:
                with patch('orchestrator.metrics.time.monotonic',return_value=1002):await runner._check_goal()
                self.assertTrue(session.done);self.assertTrue(runner.paused)
                self.assertIn('现实运行时长',goal.snapshot()['stop_reason'])
            finally:runner.close_events()

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
