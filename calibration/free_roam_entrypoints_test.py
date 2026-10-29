"""Free exploration configuration travels through real CLI, resume and launch consumers."""
import json
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from orchestrator.config import RunConfig
from orchestrator.run import parse_args, write_run_state, enter_save, check_resume_position, startup_step_options
from orchestrator.runtimes import config_from_state
from orchestrator.prompting import prompt_parts, first_play_prompt, continue_play_prompt


class FreeRoamEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def parse(self, *extra, previous=None):
        argv = ['run','--engine','codex','--agent','test','--save','selected',
                '--chapter','1','--no-sim','--no-agent',*extra]
        with patch.object(sys,'argv',argv), patch('orchestrator.config.REPO_ROOT',self.root), \
             patch('orchestrator.run.load_previous',return_value=previous):
            return parse_args()[0]

    def previous(self, **updates):
        state = {'agent':'test','save':'selected','save_slot':14,'model':'test-model','effort':'high',
                 'engine':'codex','provider':'anthropic','sim':False,'runtime':'container',
                 'play_scope':'campaign','all_chapters':[0,1,2], 'completion_pending':True,
                 'plan_pieces':[[0,0,None],[1,0,None],[2,0,None]],'piece_pending':True,
                 'knowledge_profile':'explore','mode':'explore'}
        state.update(updates)
        old = self.root/'previous'
        old.mkdir(exist_ok=True)
        return {'state':state,'metrics':{'splits':[{'chapter':'Celeste/0-Intro'}]},'dir':old}

    def test_visited_preset_and_explicit_permission_override(self):
        cfg = self.parse('--scope','visited','--save-slot','14','--no-allow-restart-chapter')
        self.assertEqual(cfg.completion_policy,'continue')
        self.assertEqual(cfg.cutscene_mode,'agent')
        self.assertEqual(cfg.action_permissions,{'restart_chapter':False,'return_to_map':True,'save_quit':False})
        self.assertIsNone(cfg.visited_maps)
        cfg = self.parse('--scope','visited','--save-slot','14','--completion-policy','stop')
        self.assertEqual(cfg.completion_policy,'stop')

    def test_visited_rejects_new_save_missing_slot_and_auto(self):
        for flags in [('--create-save',), (), ('--save-slot','14','--cutscene-mode','auto'),
                      ('--save-slot','14','--chapters','1,2')]:
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.parse('--scope','visited',*flags)

    def test_continue_is_independent_of_range_but_explicit_agent_navigation(self):
        cfg = self.parse('--scope','chapter','--completion-policy','continue')
        self.assertEqual(cfg.play_scope,'chapter')
        self.assertEqual(cfg.cutscene_mode,'agent')
        self.assertFalse(any(cfg.action_permissions.values()))
        with self.assertRaises(SystemExit):
            self.parse('--completion-policy','continue','--cutscene-mode','auto')

    def test_switching_existing_campaign_clears_only_future_plan(self):
        previous=self.previous()
        cfg=self.parse('--resume',str(previous['dir']),'--scope','visited',previous=previous)
        self.assertEqual(cfg.save_slot,14)
        self.assertIsNone(cfg.plan)
        self.assertIsNone(cfg.chapters)
        self.assertIsNone(cfg.chapter)
        self.assertFalse(cfg.resume_pending)
        self.assertTrue(cfg.resume_completion_pending)
        self.assertEqual(cfg.completion_policy,'continue')
        self.assertIsNone(cfg.visited_maps)
        self.assertEqual(previous['state']['all_chapters'],[0,1,2])

    def test_resume_preserves_fixed_maps_permissions_and_explicit_finish_policy(self):
        maps=[{'sid':'Celeste/1-ForsakenCity','side':'A'}]
        permissions={'restart_chapter':False,'return_to_map':True,'save_quit':False}
        previous=self.previous(play_scope='visited',visited_maps=maps,completion_policy='stop',
                               action_permissions=permissions)
        cfg=self.parse('--resume',str(previous['dir']),previous=previous)
        self.assertEqual(cfg.visited_maps,maps)
        self.assertEqual(cfg.action_permissions,permissions)
        self.assertEqual(cfg.completion_policy,'stop')
        restored=config_from_state(previous['state'])
        self.assertEqual(restored.visited_maps,maps)
        self.assertEqual(restored.save_slot,14)
        self.assertEqual(restored.completion_policy,'stop')

    def test_continue_restores_authoritative_navigation_not_repeat_completion_count(self):
        previous=self.previous(completion_policy='continue',navigation_remaining=[[1,0,None],[2,0,None]],
                               piece_pending=False)
        previous['metrics']['splits']=[{'chapter':'Celeste/1-ForsakenCity'}]*10
        cfg=self.parse('--resume',str(previous['dir']),previous=previous)
        self.assertEqual([p.chapter for p in cfg.plan],[1,2])
        self.assertEqual(cfg.chapter,1)

    def test_legacy_exhausted_continue_reuses_last_map_but_stop_still_rejects(self):
        previous=self.previous(completion_policy='continue')
        previous['metrics']['splits']=[{'chapter':'Celeste/1-ForsakenCity'}]*10
        cfg=self.parse('--resume',str(previous['dir']),previous=previous)
        self.assertEqual([p.chapter for p in cfg.plan],[2])
        self.assertFalse(cfg.resume_pending)
        self.assertFalse(cfg.resume_completion_pending)
        previous['state']['completion_policy']='stop'
        with self.assertRaises(SystemExit):
            self.parse('--resume',str(previous['dir']),previous=previous)

    def test_written_resume_state_keeps_permission_set_without_old_campaign_queue(self):
        maps=[{'sid':'Celeste/1-ForsakenCity','side':'A'}]
        previous=self.previous(play_scope='visited',visited_maps=maps,completion_policy='continue')
        cfg=self.parse('--resume',str(previous['dir']),previous=previous)
        target=self.root/'next'
        target.mkdir()
        write_run_state(target,cfg,'original-thread',previous)
        state=json.loads((target/'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['visited_maps'],maps)
        self.assertEqual(state['completion_policy'],'continue')
        self.assertIsNone(state['all_chapters'])
        self.assertIsNone(state['plan_pieces'])
        self.assertEqual(state['navigation_remaining'],[])
        self.assertTrue(state['completion_pending'])
        self.assertEqual(state['session_id'],'original-thread')

    def test_prompt_replaces_traversal_plan_and_finish_messages(self):
        cfg=RunConfig(agent='test',save='selected',model='test',mode='explore',play_scope='visited',
                      completion_policy='continue',chapters=[0,1,2],cutscene_mode='agent')
        parts=prompt_parts(cfg)
        self.assertFalse(any(p.label.startswith('本次指定章节') for p in parts))
        self.assertIn('通关后继续',first_play_prompt(cfg))
        self.assertNotIn('还没有通关',continue_play_prompt(cfg))
        self.assertTrue(any('主控不会代按选章' in p.text for p in parts))

    def test_startup_rejects_old_bridge_before_activate(self):
        cfg=RunConfig(agent='test',save='selected',model='test',play_scope='visited',save_slot=14)
        client=Mock()
        client.call.return_value={'map_entry_set':False}
        with patch('tools.bridge_client.BridgeClient',return_value=client):
            with self.assertRaisesRegex(RuntimeError,'未发送游戏输入'):
                enter_save(cfg)
        self.assertEqual([c.args[0] for c in client.call.call_args_list],['hello'])

    def startup_fixture(self, *, maps=None, side='A', known=True):
        cfg=RunConfig(agent='test',save='selected',model='test',play_scope='visited',save_slot=14,
                      chapter=2,cutscene_mode='agent')
        wire=SaveLoadWire(maps=maps,side=side,known=known)
        from tools.bridge_client import BridgeClient
        client=BridgeClient.__new__(BridgeClient)
        client.file,client.sock,client.next_id=wire,Mock(),1
        client.cancel_check=client.before_step=client.after_step=client.step_options=None
        return cfg,client,wire

    def test_startup_real_load_macro_and_wire_keep_saved_session_and_fixed_guard(self):
        cfg,client,wire=self.startup_fixture()
        with patch('tools.bridge_client.BridgeClient',return_value=client):
            enter_save(cfg)
        self.assertEqual(wire.obs['scene'],'Level')
        self.assertEqual(wire.obs['level']['side'],'A')
        self.assertEqual(cfg.visited_maps,wire.maps)
        self.assertEqual(len(wire.steps),2)
        self.assertTrue(all('chapter_loaded' in step['stop_on'] for step in wire.steps))
        self.assertTrue(all(step['action_policy']['entry_target']==
                            {'sid':None,'slot':14,'maps':wire.maps} for step in wire.steps))
        self.assertFalse(any(row['cmd'] in {'enter_checkpoint','create_save'} for row in wire.requests))

    def test_resume_position_real_load_macro_uses_same_pre_entry_permissions(self):
        cfg,client,wire=self.startup_fixture()
        cfg.chapter=None
        with patch('tools.bridge_client.BridgeClient',return_value=client):
            self.assertTrue(check_resume_position(cfg))
        self.assertEqual(cfg.visited_maps,wire.maps)
        self.assertEqual(wire.obs['scene'],'Level')
        self.assertTrue(all(step['action_policy']['entry_target']['maps']==wire.maps for step in wire.steps))

    def test_real_macro_rejects_saved_session_outside_snapshot_before_entry(self):
        from tools.menu_navigation import MenuActionBlocked
        for resume in (False,True):
            cfg,client,wire=self.startup_fixture(side='B')
            cfg.chapter=None if resume else cfg.chapter
            with self.subTest(resume=resume),patch('tools.bridge_client.BridgeClient',return_value=client):
                with self.assertRaisesRegex(MenuActionBlocked,'剩余|未执行'):
                    (check_resume_position if resume else enter_save)(cfg)
            self.assertEqual(wire.obs['scene'],'Overworld')
            self.assertIsNone(wire.obs['save'])
            self.assertEqual(cfg.visited_maps,wire.maps)

    def test_missing_candidate_fails_closed_without_caching_missing_as_empty(self):
        from tools.menu_navigation import MenuActionBlocked
        cfg,client,wire=self.startup_fixture(known=False)
        with patch('tools.bridge_client.BridgeClient',return_value=client):
            with self.assertRaises(MenuActionBlocked):
                enter_save(cfg)
        self.assertIsNone(cfg.visited_maps)
        self.assertTrue(all(step['action_policy']['entry_target']['maps']==[] for step in wire.steps))
        wire.obs['menu']['slots'][0]['visited_maps']=wire.maps
        options=startup_step_options(client,cfg)
        self.assertEqual(options['action_policy']['entry_target']['maps'],wire.maps)
        self.assertEqual(cfg.visited_maps,wire.maps)

    def test_null_candidate_history_does_not_authorize_or_cache_unknown(self):
        from tools.menu_navigation import MenuActionBlocked
        cfg,client,wire=self.startup_fixture(known=False)
        wire.obs['menu']['slots'][0]['visited_maps']=None
        with patch('tools.bridge_client.BridgeClient',return_value=client):
            with self.assertRaises(MenuActionBlocked):
                enter_save(cfg)
        self.assertIsNone(cfg.visited_maps)
        self.assertIsNone(wire.obs['save'])

    def test_existing_empty_snapshot_never_expands_from_file_candidate(self):
        cfg,client,wire=self.startup_fixture()
        cfg.visited_maps=[]
        self.assertEqual(startup_step_options(client,cfg)['action_policy']['entry_target']['maps'],[])
        self.assertFalse(wire.requests)


class SaveLoadWire:
    """External bridge IO only; real BridgeClient, load macro and menu consumers run."""
    def __init__(self, *, maps=None, side='A', known=True):
        self.maps=maps if maps is not None else [{'sid':'Celeste/1-ForsakenCity','side':'A'}]
        self.side=side
        slot={'index':0,'fileSlot':14,'name':'selected','exists':True}
        if known:slot['visited_maps']=self.maps
        self.obs={'scene':'Overworld','frame':1,'save':None,
                  'menu':{'current':'OuiFileSelect','slotIndex':0,'slotSelected':False,'slots':[slot]}}
        self.requests=[]
        self.steps=[]
        self.reply=None

    def write(self,data):
        request=json.loads(data)
        self.requests.append(request)
        reply={'id':request['id'],'ok':True}
        if request['cmd']=='hello':
            reply.update(map_entry_set=True,chapter_loaded_stop=True)
        elif request['cmd']=='step':
            self.steps.append(request)
            result={'stop':'done','consumed':1,'planned':31}
            if not self.obs['menu']['slotSelected']:
                self.obs['menu']['slotSelected']=True
            else:
                target=request['action_policy']['entry_target']
                identity={'sid':'Celeste/1-ForsakenCity','side':self.side}
                if target.get('slot')!=14 or identity not in target.get('maps',[]):
                    result.update(stop='entry_blocked',consumed=0,
                                  block={'rule_id':'target.maps','action':'enter_map',
                                         'reason':'Destination is outside allowed visited maps',
                                         'expected':target.get('maps'),'actual':identity})
                else:
                    self.obs={'scene':'Level','frame':2,'save':{'name':'selected','slot':14,'visited_maps':self.maps},
                              'level':{**identity,'room':'saved-room'},'controllable':False}
                    result.update(stop='chapter_loaded')
            reply['result']=result
        if request['cmd']!='hello':reply['obs']=copy.deepcopy(self.obs)
        self.reply=json.dumps(reply).encode()+b'\n'
        return len(data)

    def flush(self):pass
    def readline(self):return self.reply
    def close(self):pass


if __name__ == '__main__':
    unittest.main()
