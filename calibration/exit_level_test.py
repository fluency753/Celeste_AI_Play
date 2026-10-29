from pathlib import Path
import asyncio
import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession, ActError
from orchestrator.metrics import Metrics
from orchestrator.config import CHAPTER_SIDS
from orchestrator.action_policy import ActionPermissions, configure
from orchestrator.navigation import ChapterNavigation
from orchestrator.segments import Piece
from orchestrator.server import build_server
from mcp.server.mcpserver.exceptions import ToolError


class ExitLevelTests(unittest.TestCase):
    def make_session(self, folder):
        cfg=RunConfig(agent='exit-test',save='same',model='test',save_slot=11,runtime='host',allow_chapter_reset=True)
        metrics=Metrics(folder/'metrics.json','exit-test','test','high',60)
        session=GameSession(cfg,folder,metrics)
        session.client=Mock()
        session.last_obs={'frame':10,'level':{'sid':'Celeste/0-Intro','room':'0'}}
        session.client.call.return_value={'obs':copy.deepcopy(session.last_obs)}
        session.chapter_queue=[1,2]
        return session

    def test_agent_exit_uses_owned_connection_and_finishes_without_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            session=self.make_session(Path(folder))
            try:
                with patch('orchestrator.game_session.leave_level_by_pause_menu',return_value={'scene':'Overworld','frame':30}) as leave, \
                     patch('orchestrator.game_session.BridgeClient') as extra:
                    result=session.exit_level('用户要求结束整次试玩',finish_run=True)
                leave.assert_called_once_with(session.client, allowed_exit_ids=('menu_pause_return','intro_vignette_quit','menu_pause_savequit'))
                extra.assert_not_called()
                self.assertTrue(result['exited']);self.assertFalse(result['completed'])
                self.assertTrue(session.done and session.user_stopped)
                self.assertEqual(session.end_scene,'Overworld')
                self.assertEqual(session.chapter_queue,[])
                with self.assertRaises(ActError):session.exit_level('再次退出')
            finally:session.close()

    def test_failed_menu_exit_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            session=self.make_session(Path(folder))
            try:
                with patch('orchestrator.game_session.leave_level_by_pause_menu',return_value={'scene':'Level'}),self.assertRaises(ActError):
                    session.exit_level('退出')
                self.assertFalse(session.done)
                self.assertFalse((Path(folder)/'exit_level.json').exists())
            finally:session.close()

    def live_session(self,folder,*,pending=True):
        cfg=RunConfig(agent='map-exit',save='same',model='test',save_slot=11,runtime='host',
                      chapter=2,chapters=[2],cutscene_mode='agent',live=True,collision_image='off',
                      provider_info={'images':False})
        cfg.thought_lead_s=0
        configure(cfg,ActionPermissions(return_to_map=True))
        session=GameSession(cfg,folder,Metrics(folder/'metrics.json','map-exit','test','high',600))
        if pending:
            session._navigation=ChapterNavigation.start([Piece(2)],pending=True,previous_sid=CHAPTER_SIDS[1])
        else:
            session._navigation=ChapterNavigation.start([Piece(1),Piece(2)])
        client=MapMenuIpc();session.client=client;session.last_obs=copy.deepcopy(client.obs)
        session._update_metrics_from_obs(client.obs)
        session.entry_guard_supported=True;session.action_policy_supported=1
        return session,client

    def test_default_return_uses_real_menu_macro_then_actual_entry_consumes_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);session,client=self.live_session(root)
            session.workspace=root/'workspace';(session.workspace/'game').mkdir(parents=True)
            try:
                goal={'conditions':[{'field':'elapsed','op':'delta_gte','value':3600}]}
                session.goals.set(goal,session.goal_values())
                baseline=session.goals.snapshot()['baseline']
                result=asyncio.run(build_server(session).call_tool('exit_level',{'reason':'返回地图进入第二章'}))
                text='\n'.join(x.text for x in result.content if x.type=='text')
                self.assertIn('本次任务继续',text)
                self.assertFalse(session.done or session.user_stopped)
                self.assertEqual(session.chapter_queue,[Piece(2)])
                self.assertEqual(session.metrics.state.splits or [],[])
                self.assertEqual(session.goals.snapshot()['baseline'],baseline)
                saved=json.loads((session.workspace/'game/latest_obs.json').read_text())['obs']
                self.assertEqual(saved['scene'],'Overworld')
                exits=len(client.steps)
                again=session.exit_level('已经在地图，确认继续')
                self.assertTrue(again['already_exited'])
                self.assertEqual(len(client.steps),exits)
                session.act(['1,C'],'打开第二章面板',None)
                self.assertTrue(session.chapter_finished)
                session.act(['1,C'],'进入第二章',None)
                self.assertFalse(session.chapter_finished)
                self.assertEqual(session.piece,Piece(2))
                self.assertEqual(session.chapter_queue,[])
                self.assertFalse(session.done)
                self.assertFalse(any(command in {'record','deactivate'} for command in client.commands))
            finally:session.close(release=False)

    def test_return_is_not_completion_of_unfinished_current_chapter(self):
        with tempfile.TemporaryDirectory() as folder:
            session,client=self.live_session(Path(folder),pending=False)
            try:
                result=session.exit_level('返回地图')
                self.assertFalse(result['run_finished'] or session.chapter_finished)
                self.assertEqual(session.piece,Piece(1))
                self.assertEqual(session.chapter_queue,[Piece(2)])
                self.assertEqual(session.metrics.state.splits or [],[])
            finally:session.close(release=False)

    def test_only_save_quit_does_not_authorize_default_return_and_error_reaches_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            session,client=self.live_session(Path(folder))
            try:
                configure(session.cfg,ActionPermissions(save_quit=True))
                with self.assertRaises(ToolError) as caught:
                    asyncio.run(build_server(session).call_tool('exit_level',{'reason':'返回地图换章'}))
                self.assertIn('不能改用保存退出绕过',str(caught.exception))
                self.assertEqual(client.commands,[])
                self.assertFalse(session.done)
                for value in (1,None,'false'):
                    with self.assertRaisesRegex(ActError,'布尔值'):session.exit_level('返回',value)
                self.assertEqual(client.commands,[])
            finally:session.close(release=False)

    def test_explicit_finish_still_ends_run_and_clears_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            session,client=self.live_session(Path(folder))
            try:
                result=asyncio.run(build_server(session).call_tool('exit_level',{'reason':'用户结束整次试玩','finish_run':True}))
                text='\n'.join(x.text for x in result.content if x.type=='text')
                self.assertIn('本次任务已结束',text)
                self.assertTrue(session.done and session.user_stopped)
                self.assertEqual(session.chapter_queue,[])
                self.assertEqual(session.metrics.state.splits or [],[])
            finally:session.close(release=False)

    def test_auto_mode_can_still_operate_ready_map_after_return(self):
        with tempfile.TemporaryDirectory() as folder:
            session,client=self.live_session(Path(folder),pending=False)
            try:
                session.cfg.cutscene_mode='auto'
                session.exit_level('回到地图检查菜单')
                session.act(['1,C'],'打开当前章节面板',None)
                self.assertFalse(session.done)
                self.assertEqual(session.last_obs['menu']['current'],'OuiChapterPanel')
                self.assertEqual(session.chapter_queue,[Piece(2)])
                self.assertEqual(len(client.steps[-1]),1)
            finally:session.close(release=False)

    def test_fresh_native_completion_is_not_lost_and_only_real_completion_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            session,client=self.live_session(Path(folder),pending=False)
            try:
                # Only the live bridge knows completion; do not pre-seed the
                # session's pending flag, which concealed this boundary before.
                client.obs['level']['completed']=True
                self.assertFalse(session._pending_complete)
                result=session.exit_level('完成后返回地图')
                self.assertFalse(result['run_finished'])
                self.assertTrue(session.chapter_finished)
                self.assertEqual(session.chapter_queue,[Piece(2)])
                self.assertEqual(len(session.metrics.state.splits),1)
                self.assertEqual(session.metrics.state.splits[0]['chapter'],CHAPTER_SIDS[1])
            finally:session.close(release=False)

    def test_macro_failure_and_timeout_publish_actual_state_without_retrying(self):
        for mode,expected_steps in (('failure',1),('timeout',62)):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                session,_=self.live_session(Path(folder))
                client=MapMenuIpc(fail_mode=mode);session.client=client
                try:
                    with self.assertRaisesRegex(ActError,'已观测推进.*未重发退出'):
                        session.exit_level('返回地图')
                    self.assertFalse(session.done or session.user_stopped)
                    self.assertEqual(len(client.steps),expected_steps)
                    self.assertEqual(session.last_obs['frame'],client.obs['frame'])
                    self.assertEqual(session.metrics.state.frames_auto,client.obs['frame']-100)
                    self.assertEqual(session.chapter_queue,[Piece(2)])
                    self.assertFalse((Path(folder)/'exit_level.json').exists())
                finally:session.close(release=False)


class MapMenuIpc:
    """External native-menu state; the production exit macro decides every input."""
    def __init__(self,fail_mode=None):
        from calibration.story_wait_test import observation
        self.obs=observation(controllable=False,cutscene=False,sid=CHAPTER_SIDS[1],paused=True,canPause=True)
        self.obs['save']={'name':'same','slot':11}
        self.obs['pauseMenu']={'focused':True,'selection':0,'items':[
            {'index':0,'id':'menu_pause_return','selectable':True,'disabled':False}]}
        self.stage='pause';self.commands=[];self.steps=[];self.fail_mode=fail_mode

    def call(self,command,**options):
        self.commands.append(command)
        if command=='step':
            self.steps.append(options['inputs'])
            count=sum(int(part.split(',')[0]) for part in options['inputs'])
            frame=self.obs.get('frame',100)+count
            for part in options['inputs']:
                if 'C' not in part.split(',')[1:]:continue
                if self.stage=='pause':
                    self.stage='confirmation'
                    self.obs['pauseMenu']={'focused':True,'selection':1,'items':[
                        {'index':0,'id':'menu_pause_return','selectable':False,'disabled':False},
                        {'index':1,'id':'menu_pause_return','selectable':True,'disabled':False}]}
                elif self.stage=='confirmation':
                    if self.fail_mode=='timeout':
                        self.stage='exit';self.obs={'scene':'LevelExit','save':{'name':'same','slot':11}}
                    else:
                        self.stage='map'
                        self.obs={'scene':'Overworld','save':{'name':'same','slot':11},
                                  'menu':{'current':'OuiChapterSelect','area':2}}
                elif self.stage=='map':
                    self.stage='panel';self.obs['menu']={'current':'OuiChapterPanel','panelArea':2}
                elif self.stage=='panel':
                    from calibration.story_wait_test import observation
                    self.stage='level';self.obs=observation(controllable=True,cutscene=False,sid=CHAPTER_SIDS[2],room='start')
                    self.obs['save']={'name':'same','slot':11}
            self.obs['frame']=frame
            if self.fail_mode=='failure':raise RuntimeError('菜单输入后连接回执失败')
            return {'obs':copy.deepcopy(self.obs),'result':{'consumed':count,'planned':count,
                    'stop':'done','events':[],'trajectory':[]}}
        return {'obs':copy.deepcopy(self.obs)}

    def close(self):pass
