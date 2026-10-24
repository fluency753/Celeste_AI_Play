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
    return {'scene':'Level','frame':10,'controllable':False,'level':{
        'sid':'Celeste/6-Reflection','room':'test','inCutscene':True,'dialogWaiting':not choices,
        'dialogOpen':not choices,'dialogChoiceReady':ready,'dialogChoiceIndex':0,
        'dialogChoices':[{'index':0,'text':'选项甲','selected':True},{'index':1,'text':'选项乙','selected':False}] if choices else []}}


class SceneClient:
    def __init__(self,obs,change_choices=False):self.obs=copy.deepcopy(obs);self.steps=[];self.change_choices=change_choices
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
            return {'result':{'consumed':sum(int(x.split(',')[0]) for x in inputs),'events':[]},'obs':copy.deepcopy(self.obs)}
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
        with patch('orchestrator.game_session.skip_by_pause_menu',return_value=(True,end,0,'skipped')) as skip:
            self.s._auto_advance(dialog(),None)
        skip.assert_called_once()

    def test_advance_never_confirms_a_branch_choice(self):
        self.s.client=SceneClient(dialog(True))
        with self.assertRaisesRegex(ActError,'明确指定'):self.s.cutscene('advance')
        self.assertEqual(self.s.client.steps,[])

    def test_interactive_story_allows_real_game_inputs(self):
        self.s.last_obs=dialog()
        with patch.object(self.s,'_execute',return_value=({'stop':'done','consumed':1,'planned':1},self.s.last_obs,None,0,0,[])) as execute:
            self.s.act(['1,R,U,X'],'尝试画面提示的教学输入',None)
        execute.assert_called_once()

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

    def test_unready_or_changed_choices_never_receive_confirmation(self):
        self.s.client=SceneClient(dialog(True,False))
        with self.assertRaisesRegex(ActError,'动画'):self.s.cutscene('choose',choice=0)
        self.assertEqual(self.s.client.steps,[])
        self.s.client=SceneClient(dialog(True),change_choices=True)
        with self.assertRaisesRegex(ActError,'已改变'):self.s.cutscene('choose',choice=1)
        self.assertEqual(self.s.client.steps,[['1,D','1']])

    def test_wait_and_advance_are_distinct(self):
        self.s.cutscene('wait',frames=60)
        self.assertEqual(self.s.client.steps,[['60']])
        self.s.client.steps=[]
        self.s.cutscene('advance',frames=10)
        self.assertEqual(self.s.client.steps,[['1,C','9']])

    def test_completed_event_waits_for_agent_story_to_finish(self):
        self.s._handle_events({'events':[{'type':'complete'}]})
        self.s._commit_completion(dialog())
        self.assertFalse(self.s.done)
        self.s._commit_completion({'scene':'AreaComplete'})
        self.assertTrue(self.s.done)

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
