import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from calibration import cutscene_test
from orchestrator.game_session import ActError, parse_plan
from orchestrator.observation import describe_step
from orchestrator.workspace_files import tas


class TasParsingTests(unittest.TestCase):
    def test_digital_keys_and_simulation_mapping_preserve_independent_edges(self):
        self.assertEqual(tas.expand(['1,J','1,J,K','1','1,X,C']),['J','J,K','','X,C'])
        self.assertEqual(tas.to_sim(['1,J','1,J,K','1','1,X,C']),['1,J','1,J,K','1','1,X,T,Y'])
        for keys in ('S','Q','N','O','H'):
            with self.assertRaises(ValueError):tas.to_sim(['1,'+keys])

    def test_unknown_formats_commands_and_analog_are_explicit_errors(self):
        for value in ('tas','TAS','',None):
            with self.assertRaises(ValueError):tas.check_format(value)
        for segment in ('console load 1','Read,other.tas','Set,CheatMode,true','1,F,90','1,P,A','1,Y','-1,J'):
            with self.assertRaises(ValueError):tas.expand([segment])
        with self.assertRaises(ValueError):tas.expand(['1000001'])

    def test_file_selection_retains_source_lines_offsets_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'route.tas'
            path.write_text('console load 1\n# start\n  2,J\n3,C # second dash\nChapterTime: 0:00.085(5)\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'route.tas:1'):tas.load(path)
            document=tas.load(path,start_line=2)
            self.assertEqual(document['frames'],['J','J','C','C','C'])
            plan=tas.window(document,offset=1,count=3)
            self.assertEqual(plan['inputs'],['1,J','2,C'])
            self.assertEqual(plan['input_source']['locations'],[[3,1],[4,0],[4,1]])
            self.assertEqual(plan['input_source']['offset'],1)
            self.assertEqual(plan['input_source']['total'],5)
            self.assertEqual(len(document['metadata']),1)
            parsed=parse_plan(json.loads(json.dumps(plan)))
            self.assertEqual(parsed.input_format,'celeste_tas')
            self.assertEqual(tas.window(document,offset=4,count=600)['inputs'],['1,C'])
            with self.assertRaises(ValueError):tas.window(document,offset=5)


class TasSessionTests(unittest.TestCase):
    setUp=cutscene_test.CutsceneTests.setUp
    tearDown=cutscene_test.CutsceneTests.tearDown

    def test_act_forwards_format_retains_cursor_and_does_not_auto_advance(self):
        self.s.cfg.thought_lead_s=0;self.s.cfg.min_frames_per_act=1
        self.s.workspace=self.path
        (self.path/'game').mkdir()
        self.s._input_formats=['celeste_ai','celeste_tas']
        self.s.last_obs={'scene':'Level','controllable':True,'level':{}}
        obs={'scene':'Level','controllable':False,'input_format':'celeste_tas','level':{'inCutscene':True}}
        def call(command,**args):
            if command=='deactivate':return {}
            self.assertEqual(command,'step')
            self.assertEqual(args['input_format'],'celeste_tas')
            self.assertNotIn('story_decision',args['stop_on'])
            return {'obs':copy.deepcopy(obs),'result':{'stop':'room','consumed':2,'planned':6,'events':[]}}
        self.s.client=Mock(call=call)
        source={'offset':10,'total':20,'locations':[[4,0],[4,1],[5,0],[5,1],[5,2],[5,3]],'path':'route.tas','sha256':'abc'}
        plan=parse_plan({'inputs':['2,J','4,C'],'input_format':'celeste_tas','input_source':source})
        with patch.object(self.s,'resolve_plan_file',return_value=plan),patch.object(self.s,'_auto_advance') as auto:
            result=self.s.act(None,'按参考输入推进并保留未执行的后缀',None,plan_file='route.json')
        auto.assert_not_called()
        self.assertEqual(result.result['input_cursor']['next'],12)
        self.assertEqual(result.result['input_cursor']['next_location'],[5,0])
        self.assertEqual(result.result['remaining_inputs'],['4,C'])
        self.assertEqual(result.auto_frames,0)
        self.assertIn('TAS输入游标',describe_step(result.result))
        saved=json.loads((self.path/'game/last_act.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['result']['input_cursor']['next'],12)

    def test_older_bridge_rejects_tas_before_any_step(self):
        self.s.client=Mock(call=Mock(return_value={}))
        with self.assertRaisesRegex(ActError,'1.0.20'):
            self.s.act(['1,C'],'按第二冲刺键测试输入',None,input_format='celeste_tas')
        self.assertEqual(self.s.client.call.call_args_list[0].args[0],'hello')
        self.assertFalse(any(call.args[0]=='step' for call in self.s.client.call.call_args_list))

    def test_invalid_source_cursor_is_rejected_before_gameplay(self):
        self.s._input_formats=['celeste_ai','celeste_tas']
        plan=parse_plan({'input_format':'celeste_tas','inputs':['2,J'],
                         'input_source':{'offset':0,'total':2,'locations':[[1,0]]}})
        with self.assertRaisesRegex(ActError,'input_source'):self.s._check_plan(plan)

    def test_tas_observe_does_not_advance_typing_or_animation(self):
        obs={'scene':'Level','input_format':'celeste_tas','level':{'inCutscene':True}}
        self.s.client=Mock(call=Mock(return_value={'obs':obs}))
        with patch.object(self.s,'_auto_advance') as auto:
            self.s.observe()
        auto.assert_not_called()

    def test_old_tool_signature_executes_tas_declared_in_json_plan(self):
        import asyncio
        from orchestrator.server import build_server
        self.s.cfg.thought_lead_s=0;self.s.cfg.min_frames_per_act=1;self.s.workspace=self.path
        (self.path/'game').mkdir()
        (self.path/'tas-plan.json').write_text(json.dumps({'input_format':'celeste_tas','inputs':['1,C']}),encoding='utf-8')
        self.s._input_formats=['celeste_ai','celeste_tas']
        obs={'scene':'Overworld','input_format':'celeste_tas','menu':{'current':'OuiChapterPanel'},'level':None,'player':None}
        calls=[]
        def call(command,**options):
            if command=='deactivate':return {}
            calls.append((command,options))
            return {'obs':obs,'result':{'stop':'done','consumed':1,'planned':1,'events':[]}}
        self.s.client=Mock(call=call)
        server=build_server(self.s)
        result=asyncio.run(server.call_tool('act',{'plan_file':'tas-plan.json','thought':'使用计划中声明的TAS第二冲刺输入'}))
        self.assertEqual(calls[0][1]['input_format'],'celeste_tas')
        self.assertIn('TAS输入游标',result.content[0].text)
