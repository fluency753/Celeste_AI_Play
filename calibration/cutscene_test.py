import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession,ActError
from orchestrator.metrics import Metrics
from tools.enter_save import enter_save


def dialog(choices=False,ready=True):
    return {'scene':'Level','frame':10,'controllable':False,
            'storyInput':('choice' if ready else None) if choices else 'dialogue','level':{
        'sid':'Celeste/6-Reflection','room':'test','inCutscene':True,'dialogWaiting':not choices,
        'dialogOpen':not choices,'dialogChoiceReady':ready,'dialogChoiceIndex':0,
        'dialogChoices':[{'index':0,'text':'选项甲','selected':True},{'index':1,'text':'选项乙','selected':False}] if choices else []}}


class SceneClient:
    def __init__(self,obs,change_choices=False):
        self.obs=copy.deepcopy(obs);self.obs.setdefault('storyInput',None)
        self.steps=[];self.change_choices=change_choices
    def call(self,cmd,**args):
        if cmd in {'observe','activate'}:return {'obs':copy.deepcopy(self.obs)}
        if cmd=='step':
            inputs=args['inputs'];self.steps.append(inputs)
            if any(',D' in x for x in inputs):
                self.obs['level']['dialogChoiceIndex']+=1
                if self.change_choices:self.obs['level']['dialogChoices'][0]['text']='已变化'
            if any(',C' in x for x in inputs):
                self.obs['level'].update(inCutscene=False,dialogOpen=False,dialogWaiting=False,dialogChoices=[])
                self.obs['controllable']=True
                self.obs['storyInput']=None
            consumed=sum(int(x.split(',')[0]) for x in inputs)
            if self.obs.get('level',{}).get('completed') and 'complete' in args.get('stop_on',[]):consumed=min(1,consumed)
            return {'result':{'consumed':consumed,'events':[]},'obs':copy.deepcopy(self.obs)}
        return {}
    def close(self):pass


class CutsceneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)
        cfg=RunConfig(agent='story-test',save='player',model='test',runtime='host',cutscene_mode='agent')
        self.s=GameSession(cfg,self.path,Metrics(self.path/'metrics.json','test','test','high',60))
        self.s.client=SceneClient(dialog());self.s.last_obs=dialog()
    def tearDown(self):self.s.close();self.temp.cleanup()

    def test_stop_run_reports_blockage_and_pauses_without_ending(self):
        import asyncio,json
        from orchestrator.server import build_server
        self.s.control_guard=lambda: '用户已暂停试玩，不能执行游戏操作。'
        self.s.request_pause=Mock(return_value=True)
        server=build_server(self.s,study=Mock(ready=Mock(return_value=False)))
        result=asyncio.run(server.call_tool('stop_run',{'reason':'游戏动作持续报桥接协议错误'}))
        self.assertIn('主控已把试玩暂停',result.content[0].text)
        self.s.request_pause.assert_called_once_with('游戏动作持续报桥接协议错误')
        self.assertFalse(self.s.done)                      # 不结束运行：游戏保持冻结，等用户继续或停止
        self.assertFalse(self.s.user_stopped)
        self.assertEqual(self.s.client.steps,[])
        report=json.loads((self.path/'agent_stop.json').read_text(encoding='utf-8'))
        self.assertEqual((report['source'],len(report['reports'])),('agent',1))
        blocked=asyncio.run(server.call_tool('act',{'inputs':['1,R'],'thought':'继续移动'}))
        self.assertIn('用户已暂停',blocked.content[0].text)  # 暂停期间游戏工具不执行
        self.assertEqual(self.s.client.steps,[])
        with self.assertRaises(ActError):self.s.stop_run('原因'*150)   # 原因限 200 字

    def test_stop_run_requires_reason_and_preserves_existing_completion(self):
        with self.assertRaises(ActError):self.s.stop_run('  ')
        self.assertFalse(self.s.done)
        self.s.done=True;self.s.done_reason='章节完成'
        with self.assertRaises(ActError):self.s.stop_run('迟到的停止请求')
        self.assertEqual(self.s.done_reason,'章节完成')

    def test_advance_waits_for_confirmation_ready_then_next_node(self):
        initial=dialog();initial['storyInput']=None
        ready=dialog();after=dialog();after['storyInput']=None
        next_page=dialog();next_page['frame']=99
        client=Mock()
        client.call.side_effect=[{'obs':initial},
            {'result':{'consumed':7},'obs':ready},{'obs':ready},
            {'result':{'consumed':2},'obs':after},
            {'result':{'consumed':30},'obs':next_page},{'obs':next_page},
            {'obs':next_page}]
        self.s.client=client
        obs,_,info=self.s.cutscene('advance')
        steps=[c.kwargs['inputs'] for c in client.call.call_args_list if c.args[0]=='step']
        self.assertEqual(steps,[['30'],['1,C','1'],['30']])
        self.assertEqual(info['auto_frames'],37)
        self.assertEqual(obs['frame'],99)
        client.call.side_effect=None

    def test_agent_mode_does_not_skip_or_confirm_story(self):
        obs=dialog()
        with patch('orchestrator.game_session.skip_by_pause_menu') as skip:
            result=self.s._auto_advance(obs,None)
        self.assertEqual(result[2],0);self.assertEqual(self.s.client.steps,[]);skip.assert_not_called()

    def test_auto_mode_keeps_existing_skip_behavior(self):
        self.s.cfg.cutscene_mode='auto';end=dialog();end['controllable']=True
        end['level'].update(inCutscene=False,dialogOpen=False,dialogWaiting=False)
        end['storyInput']=None
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(True,end,0,'skipped')) as skip:
            self.s._auto_advance(dialog(),None)
        skip.assert_called_once()

    def test_advance_never_confirms_a_branch_choice(self):
        self.s.client=SceneClient(dialog(True))
        with self.assertRaisesRegex(ActError,'明确指定'):self.s.cutscene('advance')
        self.assertEqual(self.s.client.steps,[])

    def test_interactive_story_allows_real_game_inputs(self):
        self.s.last_obs=dialog()
        self.s.last_obs['level'].update(dialogOpen=False,dialogWaiting=False)
        self.s.last_obs['storyInput']='tutorial'
        with patch.object(self.s,'_execute',return_value=({'stop':'done','consumed':1,'planned':1},self.s.last_obs,None,0,0,[])) as execute:
            self.s.act(['1,R,U,X'],'尝试画面提示的教学输入',None)
        execute.assert_called_once()
        self.assertNotIn('uncontrollable',execute.call_args.args[3])
        self.assertIn('controllable',execute.call_args.args[3])

    def test_agent_can_explicitly_navigate_dialogue_options_with_raw_inputs(self):
        self.s.last_obs=dialog(True)
        with patch.object(self.s,'_execute',return_value=({'stop':'done','consumed':1,'planned':1},self.s.last_obs,None,0,0,[])) as execute:
            self.s.act(['1,D'],'尝试移动选项',None)
        execute.assert_called_once()

    def test_agent_has_all_bridge_menu_keys_but_auto_mode_keeps_play_inputs(self):
        from orchestrator.game_session import PlanSpec
        self.assertEqual(self.s._check_plan(PlanSpec(['1,C','1,B','1,P','1,N'])),['C','B','P','N'])
        self.s.cfg.cutscene_mode='auto'
        with self.assertRaises(ActError):self.s._check_plan(PlanSpec(['1,P']))

    def test_next_chapter_navigation_is_observed_without_menu_macro(self):
        from orchestrator.segments import Piece
        self.s.chapter_finished=True;self.s.chapter_queue=[Piece(1)];self.s.piece=Piece(0)
        self.s.client=SceneClient({'scene':'Overworld','menu':{'current':'OuiChapterSelect'}})
        with patch('tools.enter_save.enter_save') as macro:
            obs,_,notes=self.s._enter_next_chapter()
            self.assertTrue(self.s.chapter_finished);self.assertIn('自行',notes[0])
            self.s.client.obs={'scene':'Level','level':{'sid':'Celeste/1-ForsakenCity','room':'1'}}
            self.s._enter_next_chapter()
        self.assertFalse(self.s.chapter_finished);self.assertEqual(self.s.chapter_queue,[])
        self.assertEqual(self.s.client.steps,[]);macro.assert_not_called()

    def test_agent_checkpoint_cleanup_does_not_exit_or_send_inputs(self):
        self.s.done=True;self.s.done_reason='分段完成';self.s.last_obs={'scene':'Level'}
        self.s.finish_at_checkpoint()
        self.assertEqual(self.s.client.steps,[]);self.assertEqual(self.s.end_scene,'Level')

    def test_choose_moves_to_explicit_index_before_confirm(self):
        self.s.client=SceneClient(dialog(True))
        _,_,info=self.s.cutscene('choose',choice=1)
        self.assertEqual(self.s.client.steps,[['1,D','1'],['1,C','1']])
        self.assertEqual(info['frames'],4)

    def test_changed_choices_never_receive_confirmation(self):
        self.s.client=SceneClient(dialog(True),change_choices=True)
        with self.assertRaisesRegex(ActError,'已改变'):self.s.cutscene('choose',choice=1)
        self.assertEqual(self.s.client.steps,[['1,D','1']])

    def test_wait_is_not_an_agent_action_and_advance_only_confirms_once(self):
        with self.assertRaisesRegex(ActError,'无需 wait'):self.s.cutscene('wait')
        self.s.cutscene('advance')
        self.assertEqual(self.s.client.steps,[['1,C','1']])

    def test_ending_confirmation_after_completion_has_no_complete_stop(self):
        self.s.client.obs['level']['completed']=True
        _,_,info=self.s.cutscene('advance')
        self.assertEqual(info['frames'],2)

    def test_completed_event_waits_for_agent_story_to_finish(self):
        self.s._handle_events({'events':[{'type':'complete'}]})
        self.s._commit_completion(dialog())
        self.assertFalse(self.s.done)
        self.s._commit_completion({'scene':'AreaComplete'})
        self.assertFalse(self.s.done)
        self.s._commit_completion({'scene':'Overworld','menu':{'current':'OuiChapterPanel'}})
        self.assertFalse(self.s.done)
        self.s._commit_completion({'scene':'Overworld','menu':{'current':'OuiChapterSelect'}})
        self.assertTrue(self.s.done)

    def test_story_completion_does_not_enter_next_chapter_before_agent_returns_to_map(self):
        self.s.chapter_queue=[1]
        self.s._handle_events({'events':[{'type':'complete'}]})
        with patch.object(self.s,'_enter_next_chapter',return_value=(dialog(),None,[])) as next_chapter:
            self.s.cutscene('advance')
        next_chapter.assert_not_called()
        self.assertFalse(self.s.done)

    def test_agent_heart_prompt_and_results_never_receive_automatic_inputs(self):
        self.s._pending_complete=True
        for obs in ({'scene':'Level','level':{'completed':True,'frozen':True}},
                    {'scene':'AreaComplete'}, {'scene':'LevelExit'},
                    {'scene':'Overworld','menu':{'current':'OuiChapterPanel'}}):
            with self.subTest(scene=obs['scene']):
                _,_,frames,notes=self.s._auto_advance(obs,None)
                self.assertEqual(frames,0);self.assertFalse(self.s.done)
                self.assertIn('由你操作',notes[0])
        self.assertEqual(self.s.client.steps,[])

    def test_agent_completed_flag_does_not_truncate_later_result_inputs(self):
        self.s._pending_complete=True
        self.s.last_obs={'scene':'Level','level':{'completed':True,'frozen':True}}
        with patch.object(self.s,'_execute',return_value=({'stop':'done','consumed':30,'planned':30},self.s.last_obs,None,0,0,[])) as execute:
            self.s.act(['1,C','29'],'确认吃心界面并播放后续动画',None)
        self.assertNotIn('complete',execute.call_args.args[3])
        self.assertIn('death',execute.call_args.args[3])

    def test_agent_explicit_inputs_drive_entire_heart_to_map_flow(self):
        poem={'scene':'Level','level':{'completed':True,'frozen':True}}
        states=[{'scene':'LevelExit'}, {'scene':'AreaComplete'},
                {'scene':'Overworld','menu':{'current':'OuiChapterPanel'}},
                {'scene':'Overworld','menu':{'current':'OuiChapterSelect'}}]
        self.s.last_obs=poem;self.s._pending_complete=True
        self.s.cfg.thought_lead_s=0
        sent=[]
        def call(cmd,**args):
            if cmd=='deactivate':return {}
            self.assertEqual(cmd,'step')
            self.assertNotIn('complete',args['stop_on'])
            sent.append(args['inputs'])
            count=sum(int(item.split(',')[0]) for item in args['inputs'])
            return {'obs':states.pop(0),'result':{'consumed':count,'planned':count,'stop':'done','events':[]}}
        self.s.client=Mock(call=call)
        with patch.object(self.s,'_write_sim_state'),patch.object(self.s,'_update_metrics_from_obs'):
            for inputs in (['1,C','29'], ['60'], ['1,C','29'], ['1,B','29']):
                outcome=self.s.act(inputs,'根据当前界面操作并查看后续结算',None)
                self.assertEqual(outcome.auto_frames,0)
                self.assertEqual(self.s.done,not states)
        self.assertEqual(sent,[['1,C','29'],['60'],['1,C','29'],['1,B','29']])
        self.s.finish_at_results()
        self.assertEqual(len(sent),4)
        self.assertEqual(self.s.end_scene,'Overworld')

    def test_entry_hands_intro_to_agent_without_skip_or_inputs(self):
        obs={'scene':'IntroVignette','save':{'name':'player','slot':1}}
        client=Mock();client.call.return_value={'obs':obs}
        with patch('tools.enter_save.skip_by_pause_menu') as skip,patch('tools.enter_save.step') as step:
            self.assertEqual(enter_save(client,'player',chapter=0,save_slot=1,cutscene_mode='agent'),obs)
        skip.assert_not_called();step.assert_not_called()

    def test_save_only_entry_and_agent_connection_do_not_select_chapter(self):
        obs={'scene':'Overworld','storyInput':None,'save':{'name':'player','slot':1},
             'menu':{'current':'OuiChapterSelect','area':7}}
        client=SceneClient(obs)
        self.assertEqual(enter_save(client,'player',save_slot=1,stop_at_chapter_select=True),obs)
        with patch('orchestrator.game_session.BridgeClient',return_value=client):
            self.s.connect()
        self.assertEqual(client.steps,[]);self.assertFalse(self.s.done)

    def test_save_only_entry_backs_out_of_chapter_panel_without_confirming(self):
        selected={'scene':'Overworld','storyInput':None,'save':{'name':'player','slot':1},
                  'menu':{'current':'OuiChapterSelect','area':7}}
        panel=copy.deepcopy(selected);panel['menu']['current']='OuiChapterPanel'
        client=ScriptClient([panel,selected])
        self.assertEqual(enter_save(client,'player',save_slot=1,stop_at_chapter_select=True),selected)
        self.assertEqual(client.steps,[['1,B','30']])
        with self.assertRaises(ValueError):
            enter_save(client,'player',chapter=1,stop_at_chapter_select=True)

    def test_auto_entry_skips_cutscene_that_starts_while_player_state_is_normal(self):
        # 桥接 1.0.3 起剧情的头几帧也算可操作：自动跳过剧情时，进章节先跳过剧情，再交给智能体
        start={'scene':'Level','controllable':True,'save':{'name':'player','slot':1},
               'level':{'sid':'Celeste/2-OldSite','room':'start','inCutscene':True,'skippingCutscene':False}}
        end=copy.deepcopy(start);end['level']['inCutscene']=False
        client=Mock();client.call.return_value={'obs':start}
        with patch('tools.enter_save.skip_by_pause_menu',return_value=(True,end,0,'skipped')) as skip:
            self.assertEqual(enter_save(client,'player',chapter=2,save_slot=1,cutscene_mode='auto'),end)
        skip.assert_called_once()

    def test_session_connect_accepts_intro_only_in_agent_mode(self):
        client=SceneClient({'scene':'IntroVignette','save':{'name':'player','slot':1}})
        self.s.cfg.chapter=0;self.s.cfg.save_slot=1
        with patch('orchestrator.game_session.BridgeClient',return_value=client):
            self.assertEqual(self.s.connect()['scene'],'IntroVignette')
        self.s.cfg.cutscene_mode='auto'
        with patch('orchestrator.game_session.BridgeClient',return_value=client),self.assertRaises(RuntimeError):self.s.connect()

    def test_resume_in_completed_level_restores_pending_ending(self):
        obs=dialog();obs['level']['completed']=True;obs['save']={'name':'player','slot':1}
        with patch('orchestrator.game_session.BridgeClient',return_value=SceneClient(obs)):
            self.s.connect()
        self.assertTrue(self.s._pending_complete)
        self.assertFalse(self.s.done)

    def test_resume_at_results_preserves_pending_completion_and_side(self):
        import json
        self.s.cfg.resume_completion_pending=True
        self.s._pending_complete=True
        self.s.metrics.update(chapter_side='C')
        (self.path/'run_state.json').write_text('{}',encoding='utf-8')
        obs={'scene':'AreaComplete','storyInput':None,'save':{'name':'player','slot':1}}
        with patch('orchestrator.game_session.BridgeClient',return_value=SceneClient(obs)):
            self.s.connect()
        self.assertTrue(self.s._pending_complete);self.assertFalse(self.s.done)
        self.assertEqual(self.s.metrics.state.chapter_side,'C')
        self.assertTrue(json.loads((self.path/'run_state.json').read_text())['completion_pending'])


class ScriptClient(SceneClient):
    def __init__(self, states):
        super().__init__(states[0]);self.pending=copy.deepcopy(states[1:])
    def call(self,cmd,**args):
        reply=super().call(cmd,**args)
        if cmd=='step' and self.pending:
            self.obs=self.pending.pop(0)
            reply['obs']=copy.deepcopy(self.obs)
        return reply


class StoryFlowTests(unittest.TestCase):
    setUp=CutsceneTests.setUp
    tearDown=CutsceneTests.tearDown
    def test_host_plays_typing_to_input_without_any_confirm(self):
        typing=dialog();typing['level']['dialogWaiting']=False;typing['storyInput']=None
        self.s.client=ScriptClient([typing,dialog()])
        with patch('orchestrator.game_session.skip_by_pause_menu') as skip:
            obs,_,frames,_=self.s._auto_advance(typing,None)
        self.assertEqual(obs['storyInput'],'dialogue')
        self.assertEqual(frames,30)
        self.assertEqual(self.s.client.steps,[['30']]);skip.assert_not_called()

    def test_advance_then_host_plays_to_next_page(self):
        typing=dialog();typing['level']['dialogWaiting']=False;typing['storyInput']=None
        self.s.client=ScriptClient([dialog(),typing,dialog()])
        obs,_,info=self.s.cutscene('advance')
        self.assertEqual(obs['storyInput'],'dialogue')
        self.assertEqual(info['auto_frames'],30)
        self.assertEqual(self.s.client.steps,[['1,C','1'],['30']])

    def test_choice_animation_is_played_before_explicit_selection(self):
        self.s.client=ScriptClient([dialog(True,False),dialog(True,True)])
        self.s.cutscene('choose',choice=1)
        self.assertEqual(self.s.client.steps,[['30'],['1,D','1'],['1,C','1']])

    def test_tutorial_intro_with_player_control_does_not_auto_wait(self):
        obs=dialog();obs['level'].update(dialogOpen=False,dialogWaiting=False)
        obs.update(controllable=True,storyInput=None)
        self.s.client=SceneClient(obs)
        _,_,frames,_=self.s._auto_advance(obs,None)
        self.assertEqual(frames,0);self.assertEqual(self.s.client.steps,[])

    def test_story_text_does_not_forbid_act_when_player_is_actually_controllable(self):
        self.s.last_obs=dialog();self.s.last_obs['controllable']=True
        with patch.object(self.s,'_execute',return_value=({'stop':'done','consumed':1,'planned':1},self.s.last_obs,None,0,0,[])) as execute:
            self.s.act(['1,R'],'继续移动并观察剧情',None)
        execute.assert_called_once()

    def test_automatic_intro_text_is_returned_after_playback(self):
        a={'scene':'IntroVignette','storyInput':None,'story':[{'kind':'intro','text':'开始登山'}]}
        b={'scene':'IntroVignette','storyInput':None,'story':[{'kind':'intro','text':'相信自己'}]}
        play={'scene':'Level','controllable':True,'storyInput':None,'level':{}}
        self.s.client=ScriptClient([a,b,play])
        obs,_,frames,_=self.s._auto_advance(a,None)
        self.assertEqual(frames,60)
        self.assertEqual([s['text'] for s in obs['storyPlayback']],['开始登山','相信自己'])

    def test_ending_animation_does_not_stop_each_frame_on_completed_flag(self):
        typing=dialog();typing['level'].update(completed=True,dialogWaiting=False);typing['storyInput']=None
        ready=dialog();ready['level']['completed']=True
        self.s._pending_complete=True;self.s.client=ScriptClient([typing,ready])
        _,_,frames,_=self.s._auto_advance(typing,None)
        self.assertEqual(frames,30);self.assertFalse(self.s.done)

    def test_user_pause_stops_automatic_playback_without_waiting_for_a_node(self):
        obs=dialog();obs['level']['dialogWaiting']=False;obs['storyInput']=None
        self.s.control_guard=lambda:'用户已暂停'
        _,_,frames,notes=self.s._auto_advance(obs,None)
        self.assertEqual(frames,0);self.assertEqual(self.s.client.steps,[])
        self.assertIn('暂停',notes[0])

    def test_auto_skip_mode_still_skips_when_actor_is_in_control(self):
        obs=dialog();obs['level'].update(dialogOpen=False,dialogWaiting=False)
        obs.update(controllable=True,storyInput=None)
        end=copy.deepcopy(obs);end['level']['inCutscene']=False
        self.s.cfg.cutscene_mode='auto'
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(True,end,0,'skipped')) as skip:
            self.s._auto_advance(obs,None)
        skip.assert_called_once()

    def test_auto_mode_does_not_reject_act_at_dialogue_node(self):
        # 自动跳过剧情（速通、非直播时没有剧情工具）：停在对白上时不拒绝 act，由主控接着自动推进
        self.s.cfg.cutscene_mode='auto';self.s.last_obs=dialog()
        with patch.object(self.s,'_execute',return_value=({'stop':'done','consumed':1,'planned':1},self.s.last_obs,None,0,0,[])) as execute:
            self.s.act(['1,R'],'继续移动',None)
        execute.assert_called_once()

    def test_auto_mode_resumes_interrupted_story_before_agent_inputs(self):
        self.s.cfg.cutscene_mode='auto';self.s.last_obs=dialog()
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(False,dialog(),0,'no_skip')):
            notes=self.s._resume_auto_handling()
        self.assertEqual(self.s.client.steps[-1],['1,C','29'])   # 主控确认对白，没有落下智能体的按键
        self.assertTrue(self.s.last_obs['controllable'])
        self.assertIn('执行前',notes[0])

    def test_auto_mode_does_not_run_inputs_when_paused_again(self):
        self.s.cfg.cutscene_mode='auto';self.s.last_obs=dialog()
        self.s.control_guard=lambda:'用户已暂停'
        with self.assertRaisesRegex(ActError,'没有执行'):
            self.s._resume_auto_handling()
        self.assertEqual(self.s.client.steps,[])

    def test_auto_mode_closes_leftover_pause_menu(self):
        self.s.cfg.cutscene_mode='auto'
        obs=dialog();obs['level'].update(dialogWaiting=False,dialogOpen=False,inCutscene=False,paused=True);obs['storyInput']=None
        class Menu(SceneClient):
            def call(self,cmd,**args):
                reply=super().call(cmd,**args)
                if cmd=='step' and any(',B' in x for x in args['inputs']):
                    self.obs['level']['paused']=False;self.obs['controllable']=True
                    reply['obs']=copy.deepcopy(self.obs)
                return reply
        self.s.client=Menu(obs)
        result=self.s._auto_advance(obs,None)
        self.assertEqual(self.s.client.steps,[['1,B','20']])
        self.assertTrue(result[0]['controllable'])

    def test_agent_mode_observe_returns_host_notes(self):
        obs,_=self.s.observe()
        self.assertTrue(any('advance' in n for n in self.s.observe_notes))

    def test_native_pause_menu_does_not_get_neutral_frames(self):
        obs=dialog();obs['level'].update(dialogWaiting=False,paused=True);obs['storyInput']=None
        _,_,frames,_=self.s._auto_advance(obs,None)
        self.assertEqual(frames,0);self.assertEqual(self.s.client.steps,[])


class StorySchemaTests(unittest.IsolatedAsyncioTestCase):
    setUp=CutsceneTests.setUp
    tearDown=CutsceneTests.tearDown

    async def test_tool_interface_has_no_wait_frame_parameter(self):
        from orchestrator.server import build_server
        server=build_server(self.s)
        tool=next(t for t in await server.list_tools() if t.name=='cutscene')
        self.assertNotIn('frames',tool.input_schema['properties'])
        self.assertIn('无需wait',tool.description)

    def test_node_and_played_text_are_in_agent_observation(self):
        import json
        from orchestrator.observation import describe_obs,room_json
        obs={'scene':'Level','storyInput':'tutorial','storyPlayback':[{'kind':'intro','text':'相信自己'}]}
        rendered=describe_obs(obs)
        self.assertIn('当前剧情输入节点：tutorial',rendered)
        self.assertIn('刚才由主控自动播放的文字：相信自己',rendered)
        self.assertEqual(json.loads(room_json(obs))['storyInput'],'tutorial')
