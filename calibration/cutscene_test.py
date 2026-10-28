import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession,ActError
from orchestrator.metrics import Metrics
from tools.enter_save import enter_save, open_chapter_select


def dialog(choices=False,ready=True):
    return {'scene':'Level','frame':10,'controllable':False,
            'storyPlaybackState':{'state':'playing','source':'fixture_textbox_animation'},
            'storyInput':('choice' if ready else None) if choices else 'dialogue','level':{
        'sid':'Celeste/6-Reflection','room':'test','inCutscene':True,'dialogWaiting':not choices,
        'dialogOpen':not choices,'dialogChoiceReady':ready,'dialogChoiceIndex':0,
        'dialogChoices':[{'index':0,'text':'选项甲','selected':True},{'index':1,'text':'选项乙','selected':False}] if choices else []}}


def loader_postcard(ready=True, text='解锁提示'):
    return {'scene': 'OverworldLoader', 'frame': 10, 'controllable': False, 'level': None,
            'storyInput': 'postcard' if ready else None, 'storyDecision': 'postcard' if ready else None,
            'ui': {'type': 'Postcard', 'inputReady': ready, 'backgroundLoaded': True},
            'story': [{'kind': 'postcard', 'text': text}],
            'storyPlaybackState': {'state': 'input' if ready else 'playing', 'source': 'postcard'}}


class SceneClient:
    def __init__(self,obs,change_choices=False):
        self.obs=copy.deepcopy(obs);self.obs.setdefault('storyInput',None)
        self.steps=[];self.change_choices=change_choices
    def call(self,cmd,**args):
        if cmd == 'hello':return {'pico8_entry_guard':True}
        if cmd in {'observe','activate'}:return {'obs':copy.deepcopy(self.obs)}
        if cmd=='step':
            inputs=args['inputs'];self.steps.append(inputs)
            if any(',D' in x for x in inputs):
                self.obs['level']['dialogChoiceIndex']+=1
                if self.change_choices:self.obs['level']['dialogChoices'][0]['text']='已变化'
            if self.obs.get('level') and any(',C' in x for x in inputs):
                self.obs['level'].update(inCutscene=False,dialogOpen=False,dialogWaiting=False,dialogChoices=[])
                self.obs['controllable']=True
                self.obs['storyInput']=None
            consumed=sum(int(x.split(',')[0]) for x in inputs)
            if (self.obs.get('level') or {}).get('completed') and 'complete' in args.get('stop_on',[]):consumed=min(1,consumed)
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
        with self.assertRaisesRegex(ActError,'已改变.*已执行 2 帧选项移动，确认键未发送'):self.s.cutscene('choose',choice=1)
        self.assertEqual(self.s.client.steps,[['1,D','1']])
        self.assertEqual(self.s.metrics.state.frames_played, 2)

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

    def test_initial_map_navigation_is_not_reported_as_ending(self):
        _,_,frames,notes=self.s._auto_advance({'scene':'Overworld','menu':{'current':'OuiChapterSelect'}},None)
        self.assertEqual(frames,0);self.assertNotIn('结算',notes[0]);self.assertIn('自行导航',notes[0])

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
        self.assertEqual(open_chapter_select(client,'player',save_slot=1),obs)
        with patch('orchestrator.game_session.BridgeClient',return_value=client):
            self.s.connect()
        self.assertEqual(client.steps,[]);self.assertFalse(self.s.done)

    def test_save_only_entry_backs_out_of_chapter_panel_without_confirming(self):
        selected={'scene':'Overworld','storyInput':None,'save':{'name':'player','slot':1},
                  'menu':{'current':'OuiChapterSelect','area':7}}
        panel=copy.deepcopy(selected);panel['menu']['current']='OuiChapterPanel'
        client=ScriptClient([panel,selected])
        self.assertEqual(open_chapter_select(client,'player',save_slot=1),selected)
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

    def test_loader_postcard_watch_reaches_input_without_ending_or_confirming(self):
        self.s.client = ScriptClient([loader_postcard(False), loader_postcard()])
        obs, _, _ = self.s.cutscene('watch')
        self.assertEqual(obs['storyInput'], 'postcard')
        self.assertEqual(self.s.client.steps, [['30']])
        self.assertFalse(self.s.done)

    def test_loader_postcard_advance_confirms_once_and_waits_for_second_prompt(self):
        self.s.client = ScriptClient([loader_postcard(), loader_postcard(False), loader_postcard(text='第二张')])
        obs, _, _ = self.s.cutscene('advance')
        self.assertEqual(obs['story'][0]['text'], '第二张')
        self.assertEqual(self.s.client.steps, [['1,C', '1'], ['30']])
        self.assertFalse(self.s.done)

    def test_loader_postcard_auto_mode_confirms_only_ready_input(self):
        self.s.cfg.cutscene_mode = 'auto'
        animation, ready = loader_postcard(False), loader_postcard()
        menu = {'scene': 'Overworld', 'frame': 80, 'menu': {'current': 'OuiChapterSelect'}}
        self.s.client = ScriptClient([animation, ready, menu])
        obs, _, _, _ = self.s._auto_advance(animation, None)
        self.assertEqual(self.s.client.steps, [['30'], ['1,C', '29']])
        self.assertEqual(obs['scene'], 'Overworld')

    def test_explicit_watch_on_loader_postcard_never_confirms_in_auto_mode(self):
        self.s.cfg.cutscene_mode = 'auto'
        self.s.client = SceneClient(loader_postcard())
        obs, _, _ = self.s.cutscene('watch')
        self.assertEqual(obs['storyInput'], 'postcard')
        self.assertEqual(self.s.client.steps, [])
        self.assertFalse(self.s.done)

    def test_loader_postcard_advance_crosses_tail_delay_to_next_prompt(self):
        tail = {'scene': 'OverworldLoader', 'frame': 20, 'level': None, 'ui': None,
                'storyInput': None, 'storyPlaybackState': {'state': 'none', 'source': 'no_cutscene'},
                'reload_boundary': {'mode': 'wait'}}
        self.s.client = ScriptClient([loader_postcard(), tail, tail, loader_postcard(text='下一张')])
        obs, _, _ = self.s.cutscene('advance')
        self.assertEqual(obs['story'][0]['text'], '下一张')
        self.assertEqual(self.s.client.steps, [['1,C', '1'], ['30'], ['30']])
        self.assertFalse(self.s.done)

    def test_unknown_loader_without_boundary_remains_zero_input_handback(self):
        self.s.client = SceneClient({'scene': 'OverworldLoader', 'frame': 20, 'level': None})
        with self.assertRaisesRegex(ActError, '当前没有待处理的剧情'):
            self.s.cutscene('watch')
        self.assertEqual(self.s.client.steps, [])
        self.assertFalse(self.s.done)

    def test_auto_mode_waits_for_authoritative_loader_before_marking_done(self):
        self.s.cfg.cutscene_mode = 'auto'
        tail = {'scene': 'OverworldLoader', 'frame': 20, 'level': None,
                'reload_boundary': {'mode': 'wait'}}
        menu = {'scene': 'Overworld', 'frame': 50, 'menu': {'current': 'OuiChapterSelect'}}
        self.s.client = ScriptClient([tail, menu])
        self.s._auto_advance(tail, None)
        self.assertEqual(self.s.client.steps, [['30']])

    def test_pending_completion_stays_pending_during_auto_loader_tail(self):
        self.s.cfg.cutscene_mode = 'auto'
        self.s._pending_complete = True
        tail = {'scene': 'OverworldLoader', 'frame': 20, 'level': None,
                'reload_boundary': {'mode': 'wait'}}
        self.s._commit_completion(tail)
        self.assertTrue(self.s._pending_complete)
        self.assertFalse(self.s.metrics.state.splits)
        self.assertFalse(self.s.done)

    def test_act_hands_off_before_first_dialogue_then_watch_reaches_real_input(self):
        # Model only the external frame source: the real act/auto-play/watch
        # consumers must decide when to stop. The first page needs 544 frames,
        # but pausing becomes possible on frame 4.
        from orchestrator.game_session import expand_inputs, LETTERS
        class Timeline:
            def __init__(self):
                self.frame=0;self.calls=[]
            def obs(self):
                return {'scene':'Level','frame':self.frame,'controllable':self.frame==0,
                        'storyPlaybackState':{'state':'playing','source':'fixture_cutscene_animation'},
                        'storyInput':'dialogue' if self.frame>=544 else None,
                        'storyDecision':'dialogue' if self.frame>=544 else ('skip' if self.frame>=4 else None),
                        'level':{'inCutscene':self.frame>0,'canPause':self.frame>=4,'room':'test',
                                 'sid':'Celeste/3-CelestialResort','dialogChoices':[],
                                 'dialogOpen':self.frame>=100,'dialogWaiting':self.frame>=544}}
            def call(self,cmd,**args):
                if cmd!='step':return {'obs':self.obs()}
                self.calls.append(args)
                before=self.obs();events=[];count=0;stop='done'
                for _ in expand_inputs(args['inputs'],10000,LETTERS):
                    self.frame+=1;count+=1;after=self.obs()
                    changes=[]
                    if before['controllable'] and not after['controllable']:changes.append('uncontrollable')
                    for key,event in [('storyInput','story_input'),('storyDecision','story_decision')]:
                        if after[key] and after[key]!=before[key]:changes.append(event)
                    events.extend({'type':item,'frame':self.frame} for item in changes)
                    before=after
                    hit=next((item for item in changes if item in args.get('stop_on',[])),None)
                    if hit:stop=hit;break
                return {'obs':self.obs(),'result':{'consumed':count,'planned':count,'stop':stop,'events':events}}
            def close(self):pass
        client=Timeline();self.s.client=client;self.s.last_obs=client.obs()
        self.s.cfg.thought_lead_s=0
        with patch.object(self.s,'_write_sim_state'):
            outcome=self.s.act(['40,R'],'触发旅馆对话',None)
            self.assertEqual(outcome.obs['frame'],4)
            self.assertIsNone(outcome.obs['storyInput'])
            self.assertEqual(outcome.obs['storyDecision'],'skip')
            self.assertEqual(outcome.auto_frames,3)
            calls=len(client.calls)
            observed,_=self.s.observe()
            self.assertEqual(observed['frame'],4)
            self.assertEqual(len(client.calls),calls)  # observe does not watch for the agent
            obs,_,info=self.s.cutscene('watch')
        self.assertEqual(obs['frame'],544)
        self.assertEqual(obs['storyInput'],'dialogue')
        self.assertEqual(info['auto_frames'],540)
        self.assertEqual(info['frames'],0)
        self.assertTrue(all(',' not in line for call in client.calls[1:] for line in call['inputs']))
        self.assertTrue(all('story_decision' not in call['stop_on'] for call in client.calls[calls:]))

    def test_early_skip_calls_menu_without_waiting_for_dialogue(self):
        early=dialog();early.update(storyInput=None,storyDecision='skip')
        early['level']['dialogWaiting']=False
        self.s.client=SceneClient(early)
        end=copy.deepcopy(early);end.update(controllable=True,storyDecision=None)
        end['level'].update(inCutscene=False,dialogOpen=False)
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(True,end,0,'skipped')) as skip:
            _,_,info=self.s.cutscene('skip')
        skip.assert_called_once()
        self.assertEqual(self.s.client.steps,[])
        self.assertTrue(info['skipped'])

    def test_advance_does_not_stop_again_at_same_skip_opportunity(self):
        typing=dialog();typing.update(storyInput=None,storyDecision='skip')
        typing['level']['dialogWaiting']=False
        ready=dialog();ready['storyDecision']='dialogue'
        self.s.client=ScriptClient([ready,typing,typing,ready])
        obs,_,info=self.s.cutscene('advance')
        self.assertEqual(obs['storyInput'],'dialogue')
        self.assertEqual(info['auto_frames'],60)
        self.assertEqual(self.s.client.steps,[['1,C','1'],['30'],['30']])

    def test_explicit_story_tools_do_not_auto_skip_or_confirm_in_auto_mode(self):
        typing=dialog();typing.update(storyInput=None,storyDecision='skip')
        typing['level']['dialogWaiting']=False
        for action in ('watch','advance','choose'):
            with self.subTest(action=action):
                self.s.cfg.cutscene_mode='auto'
                ready=dialog(action=='choose')
                after=dialog();after.update(storyInput=None,storyDecision='skip')
                after['level']['dialogWaiting']=False
                final=dialog()
                states=[typing,ready] if action=='watch' else [typing,ready,after,final]
                self.s.client=ScriptClient(states)
                obs,_,_=self.s.cutscene(action,choice=0 if action=='choose' else None)
                self.assertEqual(obs['storyInput'],'dialogue')
                self.assertEqual(self.s.client.steps,[['30']] if action=='watch' else [['30'],['1,C','1'],['30']])
                self.assertEqual(self.s.cfg.cutscene_mode,'auto')

    def test_watch_never_confirms_choices_or_tutorial_and_respects_user_pause(self):
        for node in ('choice','tutorial','postcard','confirm','breathing'):
            obs=dialog();obs.update(storyInput=node,storyDecision=node)
            self.s.client=SceneClient(obs)
            result=self.s.cutscene('watch')
            self.assertEqual(result[2]['frames'],0)
            self.assertEqual(self.s.client.steps,[])
        early=dialog();early.update(storyInput=None,storyDecision='skip')
        self.s.client=SceneClient(early);self.s.control_guard=lambda:'用户已暂停'
        _,_,info=self.s.cutscene('watch')
        self.assertEqual(self.s.client.steps,[])
        self.assertIn('暂停',info['auto_notes'][0])

    def test_advance_accepts_native_confirm_but_never_plays_breathing_for_agent(self):
        ready=dialog();ready.update(storyInput='confirm',storyDecision='confirm')
        ready['level'].update(dialogOpen=False,dialogWaiting=False)
        self.s.client=SceneClient(ready)
        _,_,info=self.s.cutscene('advance')
        self.assertEqual(info['frames'],2)
        self.assertEqual(self.s.client.steps,[['1,C','1']])
        breathing=copy.deepcopy(ready);breathing.update(storyInput='breathing',storyDecision='breathing')
        self.s.client=SceneClient(breathing)
        with self.assertRaisesRegex(ActError,'小游戏操作请用 act'):
            self.s.cutscene('advance')
        self.assertEqual(self.s.client.steps,[])

    def test_new_native_input_prompts_explain_actual_controls(self):
        from orchestrator.observation import describe_obs
        confirm=describe_obs({'scene':'Level','storyInput':'confirm'})
        self.assertIn('advance 或 act 的 C',confirm)
        breathing=describe_obs({'scene':'Level','storyInput':'breathing'})
        self.assertIn('J、X 或 U',breathing)
        self.assertIn('watch 不代替操作',breathing)

    def test_auto_mode_confirms_native_confirm_but_hands_over_breathing(self):
        self.s.cfg.cutscene_mode='auto'
        ready=dialog();ready.update(storyInput='confirm',storyDecision='confirm')
        ready['level'].update(dialogOpen=False,dialogWaiting=False)
        self.s.client=SceneClient(ready)
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(False,ready,0,'no_pause')):
            obs,_,frames,_=self.s._auto_advance(ready,None)
        self.assertTrue(obs['controllable'])
        self.assertEqual(frames,30)
        self.assertEqual(self.s.client.steps,[['1,C','29']])
        breathing=copy.deepcopy(ready);breathing.update(storyInput='breathing',storyDecision='breathing')
        self.s.client=SceneClient(breathing)
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(False,breathing,0,'no_pause')):
            _,_,frames,notes=self.s._auto_advance(breathing,None)
        self.assertEqual(frames,0)
        self.assertEqual(self.s.client.steps,[])
        self.assertIn('不以空帧代替游玩',notes[0])
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
            self.s.act(['40,R'],'继续移动并观察剧情',None)
        execute.assert_called_once()

    def test_automatic_intro_text_is_returned_after_playback(self):
        a={'scene':'IntroVignette','storyInput':None,'storyPlaybackState':{'state':'playing','source':'fixture_intro'},'story':[{'kind':'intro','text':'开始登山'}]}
        b={'scene':'IntroVignette','storyInput':None,'storyPlaybackState':{'state':'playing','source':'fixture_intro'},'story':[{'kind':'intro','text':'相信自己'}]}
        play={'scene':'Level','controllable':True,'storyInput':None,'level':{}}
        self.s.client=ScriptClient([a,b,play])
        obs,_,frames,_=self.s._auto_advance(a,None)
        self.assertEqual(frames,60)
        self.assertEqual([s['text'] for s in obs['storyPlayback']],['开始登山','相信自己'])

    def test_watch_continues_intro_fade_loader_and_player_entry_without_inputs(self):
        intro={'scene':'IntroVignette','storyInput':None,'storyDecision':'skip',
               'storyPlaybackState':{'state':'playing','source':'intro_text'}}
        fade=copy.deepcopy(intro);fade['storyDecision']=None
        fade['storyPlaybackState']['source']='intro_fade_out'
        loader={'scene':'LevelLoader','storyInput':None,'level':None}
        entry={'scene':'Level','controllable':False,'storyInput':None,'level':{},
               'storyPlaybackState':{'state':'playing','source':'player_intro'}}
        play={'scene':'Level','controllable':True,'storyInput':None,'level':{}}
        self.s.client=ScriptClient([intro,fade,loader,entry,play])
        obs,_,info=self.s.cutscene('watch')
        self.assertTrue(obs['controllable'])
        self.assertEqual(info['auto_frames'],120)
        self.assertEqual(info['frames'],0)
        self.assertFalse(info['skipped'])
        self.assertEqual(self.s.client.steps,[['30']]*4)

    def test_watch_can_resume_after_budget_stops_in_native_player_entry(self):
        intro={'scene':'IntroVignette','storyInput':None,'storyDecision':'skip',
               'storyPlaybackState':{'state':'playing','source':'intro_text'}}
        entry={'scene':'Level','controllable':False,'storyInput':None,'level':{'inCutscene':False},
               'storyPlaybackState':{'state':'playing','source':'player_intro_animation'}}
        play={'scene':'Level','controllable':True,'storyInput':None,'level':{}}
        self.s.client=ScriptClient([intro,entry,play])
        with patch('orchestrator.game_session.AGENT_STORY_WAIT_FRAMES',30):
            obs,_,first=self.s.cutscene('watch')
            self.assertFalse(obs['controllable'])
            self.assertIn('等待预算',first['auto_notes'][0])
            obs,_,second=self.s.cutscene('watch')
        self.assertTrue(obs['controllable'])
        self.assertEqual([first['auto_frames'],second['auto_frames']],[30,30])
        self.assertEqual(self.s.client.steps,[['30'],['30']])

    def test_explicit_watch_plays_controllable_cinematic_one_frame_at_a_time(self):
        lead=dialog();lead.update(storyInput=None,storyDecision=None,controllable=True)
        lead['level'].update(dialogOpen=False,dialogWaiting=False)
        locked=copy.deepcopy(lead);locked['controllable']=False
        ready=copy.deepcopy(locked);ready.update(storyInput='tutorial',storyDecision='tutorial')

        class CheckedClient(ScriptClient):
            def call(self,cmd,**args):
                if cmd=='step' and self.obs.get('controllable'):
                    self_test.assertNotIn('controllable',args['stop_on'])
                return super().call(cmd,**args)

        self_test=self
        self.s.client=CheckedClient([lead,lead,locked,ready])
        obs,_,info=self.s.cutscene('watch')
        self.assertEqual(obs['storyInput'],'tutorial')
        self.assertEqual(info['auto_frames'],32)
        self.assertEqual(self.s.client.steps,[['1'],['1'],['30']])

    def test_watch_does_not_overrun_cinematic_end_while_player_remains_controllable(self):
        lead=dialog();lead.update(storyInput=None,storyDecision=None,controllable=True)
        lead['level'].update(dialogOpen=False,dialogWaiting=False)
        end=copy.deepcopy(lead);end['level']['inCutscene']=False
        end['storyPlaybackState']={'state':'none','source':'no_cutscene'}
        self.s.client=ScriptClient([lead,end])
        _,_,info=self.s.cutscene('watch')
        self.assertEqual(info['auto_frames'],1)
        self.assertEqual(self.s.client.steps,[['1']])
        # Without an explicit story tool, playable scenes keep their act handoff.
        self.s.client=SceneClient(lead)
        _,_,frames,_=self.s._auto_advance(lead,None)
        self.assertEqual(frames,0)
        self.assertEqual(self.s.client.steps,[])

    def test_controllable_native_animation_prompt_offers_watch_without_forcing_it(self):
        from orchestrator.observation import describe_obs
        lead={'scene':'Level','controllable':True,'level':{'inCutscene':True,'room':'test','sid':'Celeste/0-Intro',
                    'chapterTimeTicks':0,'deaths':0,'deathsInRoom':0,'camera':[0,0],'bounds':[0,0,320,180]},
              'storyPlaybackState':{'state':'playing','source':'cutscene_animation'}}
        self.assertIn('也可明确调用 watch',describe_obs(lead))
        lead['storyPlaybackState']['state']='unknown'
        self.assertIn('不需要等待或确认',describe_obs(lead))

    def test_watch_plays_native_animation_until_dialogue_or_bird_input(self):
        for node in ('dialogue','tutorial'):
            with self.subTest(node=node):
                animation=dialog();animation.update(storyInput=None,storyDecision='skip')
                animation['level'].update(dialogOpen=False,dialogWaiting=False)
                animation['storyPlaybackState']={'state':'playing','source':'native_cutscene_animation'}
                ready=copy.deepcopy(animation);ready.update(storyInput=node,storyDecision=node)
                ready['storyPlaybackState']={'state':'input','source':node}
                self.s.client=ScriptClient([animation,ready])
                obs,_,info=self.s.cutscene('watch')
                self.assertEqual(obs['storyInput'],node)
                self.assertEqual(info['auto_frames'],30)
                self.assertEqual(self.s.client.steps,[['30']])
                # Repeated watch at the real input boundary must not press or wait.
                _,_,again=self.s.cutscene('watch')
                self.assertEqual(again['auto_frames'],0)
                self.assertEqual(self.s.client.steps,[['30']])

    def test_unknown_watch_stays_frozen_and_reports_classifier_reason(self):
        unknown=dialog();unknown.update(storyInput=None,storyDecision='skip')
        unknown['storyPlaybackState']={'state':'unknown','source':'dialogue_unrecognized_routine'}
        self.s.client=SceneClient(unknown)
        _,_,info=self.s.cutscene('watch')
        self.assertEqual(info['auto_frames'],0)
        self.assertEqual(self.s.client.steps,[])
        self.assertIn('unknown / dialogue_unrecognized_routine',info['auto_notes'][0])

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

    def test_skip_opportunity_is_distinct_in_observation_and_tool_schema(self):
        import json
        from orchestrator.observation import describe_obs,room_json
        obs={'scene':'Level','storyInput':None,'storyDecision':'skip',
             'level':{'inCutscene':True,'room':'test','sid':'Celeste/3-CelestialResort',
                      'chapterTimeTicks':0,'deaths':0,'deathsInRoom':0,'camera':[0,0],'bounds':[0,0,320,180]}}
        self.assertIn('剧情可尝试跳过',describe_obs(obs))
        self.assertNotIn('当前剧情输入节点',describe_obs(obs))
        self.assertEqual(json.loads(room_json(obs))['storyDecision'],'skip')

    def test_observation_only_recommends_watch_for_confirmed_playback(self):
        from orchestrator.observation import describe_obs,skip_opportunity_hint
        for state in ('playing','unknown',None):
            with self.subTest(state=state):
                obs={'scene':'IntroVignette','storyInput':None,'storyDecision':'skip'}
                if state:obs['storyPlaybackState']={'state':state,'source':'test'}
                rendered=describe_obs(obs)
                self.assertIn(skip_opportunity_hint(obs),rendered)
                if state=='playing':
                    self.assertIn('watch 不按键观看',rendered)
                else:
                    self.assertIn('watch 会停止',rendered)
                    self.assertNotIn('watch 不按键观看',rendered)
