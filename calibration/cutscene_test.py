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

    def test_regular_act_cannot_select_dialogue_options(self):
        self.s.last_obs=dialog(True)
        with patch.object(self.s,'_execute') as execute,self.assertRaisesRegex(ActError,'choose'):
            self.s.act(['1,D'],'尝试移动选项',None)
        execute.assert_not_called()

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
        self.assertTrue(self.s.done)

    def test_story_completion_continues_to_next_chapter_when_queued(self):
        self.s.chapter_queue=[1]
        self.s._handle_events({'events':[{'type':'complete'}]})
        with patch.object(self.s,'_enter_next_chapter',return_value=(dialog(),None,[])) as next_chapter:
            self.s.cutscene('advance')
        next_chapter.assert_called_once()
        self.assertFalse(self.s.done)

    def test_entry_hands_intro_to_agent_without_skip_or_inputs(self):
        obs={'scene':'IntroVignette','save':{'name':'player','slot':1}}
        client=Mock();client.call.return_value={'obs':obs}
        with patch('tools.enter_save.skip_by_pause_menu') as skip,patch('tools.enter_save.step') as step:
            self.assertEqual(enter_save(client,'player',chapter=0,save_slot=1,cutscene_mode='agent'),obs)
        skip.assert_not_called();step.assert_not_called()

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
