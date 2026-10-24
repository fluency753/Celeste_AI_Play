import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from orchestrator.saves import inventory, validate_selection
from tools.enter_save import enter_save, handle_file_select


class SaveCatalogTests(unittest.TestCase):
    def test_catalog_reads_only_main_saves_and_decodes_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'configs').mkdir();(root/'game/Saves').mkdir(parents=True)
            (root/'configs/local.deploy.json').write_text(json.dumps({'celeste_dir':str(root/'game')}))
            for name,xml in [('10.celeste','<SaveData><Name>直播 &amp; 探索</Name></SaveData>'),
                             ('10-modsavedata.celeste','<SaveData><Name>ignore</Name></SaveData>'),
                             ('11.celeste','broken')]:
                (root/'game/Saves'/name).write_text(xml,encoding='utf-8')
            with patch('orchestrator.saves.REPO_ROOT',root):
                self.assertEqual(inventory(),[{'slot':10,'name':'直播 & 探索'}])

    def test_selection_is_explicit_and_never_overwrites_existing_names(self):
        with patch('orchestrator.saves.inventory',return_value=[{'slot':10,'name':'test'}]), \
             patch('orchestrator.saves.supports_unicode',return_value=True):
            self.assertEqual(validate_selection('test',False,1),'test')
            self.assertEqual(validate_selection(' 小雪 ',True,0),'小雪')
            self.assertEqual(validate_selection('test',True,0),'test')
            for args in [('',False,0),('missing',False,0),('小雪',True,1),
                         ('a\nb',True,0),('a'*21,True,0),('test',None,0)]:
                with self.subTest(args=args),self.assertRaises(ValueError):validate_selection(*args)

    def test_duplicate_existing_names_are_selected_by_slot(self):
        with patch('orchestrator.saves.inventory',return_value=[{'name':'same','slot':0},{'name':'same','slot':1}]):
            with self.assertRaises(ValueError):validate_selection('same',False,0)
            self.assertEqual(validate_selection('same',False,0,1),'same')
            with self.assertRaises(ValueError):validate_selection('same',False,0,2)

    def test_old_bridge_rejects_chinese_name_before_game_start(self):
        with patch('orchestrator.saves.supports_unicode',return_value=False):
            with self.assertRaisesRegex(ValueError,'桥接0.12.1'):validate_selection('小雪',True,0)


class SaveMenuTests(unittest.TestCase):
    def test_old_bridge_is_rejected_before_game_activation(self):
        client = Mock()
        client.call.return_value = {'create_save': False}
        with self.assertRaisesRegex(RuntimeError, '0.12.4'):
            enter_save(client, 'test', chapter=0, create_if_missing=True)
        client.call.assert_called_once_with('hello')

    def test_farewell_id_matches_shared_chapter_catalog(self):
        from tools.enter_save import area_id
        from orchestrator.config import CHAPTER_SIDS
        for ident, sid in CHAPTER_SIDS.items():
            self.assertEqual(area_id(sid), ident)
    def menu(self):
        return {'scene':'Overworld','menu':{'current':'OuiFileSelect','slotSelected':True,
            'slotIndex':1,'buttonIndex':0,'slots':[{'index':0,'name':'test','exists':True},
            {'index':1,'name':'Madeline','exists':False}]}}


    def test_create_flow_enters_new_save_without_renaming_old_slot(self):
        obs=self.menu();named=copy.deepcopy(obs);named['menu']['slots'][1]['name']='小雪'
        level={'scene':'Level','save':{'name':'小雪','slot':1},'level':{'sid':'Celeste/0-Intro'},'controllable':True}
        client=Mock();client.call.side_effect=[{'create_save':True},{'obs':obs},{'slot':1},{'obs':level}]
        with patch('tools.enter_save.press',return_value={'obs':level}):
            self.assertEqual(enter_save(client,'小雪',chapter=0,create_if_missing=True),level)
        self.assertEqual(obs['menu']['slots'][0]['name'],'test')

    def test_new_same_name_uses_empty_slot_instead_of_existing_save(self):
        obs=self.menu();named=copy.deepcopy(obs);named['menu']['slots'][1]['name']='test'
        level={'scene':'Level','save':{'name':'test','slot':1},'level':{'sid':'Celeste/0-Intro'},'controllable':True}
        client=Mock();client.call.side_effect=[{'create_save':True},{'obs':obs},{'slot':1},{'obs':level}]
        with patch('tools.enter_save.press',return_value={'obs':level}):
            self.assertEqual(enter_save(client,'test',chapter=0,create_if_missing=True),level)
        self.assertEqual(client.call.call_args_list[2].args,('create_save',))

    def test_existing_same_name_menu_uses_exact_slot(self):
        obs=self.menu();obs['menu']['slots'][1].update(name='test',exists=True)
        client=Mock()
        with patch('tools.enter_save.press',return_value={'obs':obs}) as press:
            handle_file_select(client,obs,'test',save_slot=1)
        self.assertEqual(press.call_args.args,(client,'C'))
        client.call.assert_not_called()

    def test_native_creation_is_one_action_without_cursor_or_keyboard_steps(self):
        menu=self.menu()
        level={'scene':'Level','save':{'name':'test','slot':11},'level':{'sid':'Celeste/0-Intro'},'controllable':True}
        client=Mock();client.call.side_effect=[{'create_save':True},{'obs':menu},
                                              {'slot':11,'name':'test','started':True,'persisted':False},{'obs':level}]
        with patch('tools.enter_save.press') as press,patch('tools.enter_save.handle_file_select') as select:
            result=enter_save(client,'test',chapter=0,create_if_missing=True)
        self.assertEqual(result,level)
        self.assertEqual(client.call.call_args_list[2].args,('create_save',))
        self.assertEqual(client.call.call_args_list[2].kwargs,{'name':'test'})
        press.assert_not_called();select.assert_not_called()

    def test_native_creation_runs_once_while_waiting_for_transition(self):
        menu=self.menu()
        level={'scene':'Level','save':{'name':'same','slot':1},'level':{'sid':'Celeste/0-Intro'},'controllable':True}
        client=Mock();client.call.side_effect=[{'create_save':True},{'obs':menu},
                                              {'slot':1,'name':'same','started':True},{'obs':menu},{'obs':level}]
        self.assertEqual(enter_save(client,'same',chapter=0,create_if_missing=True),level)
        self.assertEqual(sum(call.args==('create_save',) for call in client.call.call_args_list),1)

    def test_native_creation_failure_never_falls_back_to_keyboard_creation(self):
        client=Mock();client.call.side_effect=[{'create_save':True},{'obs':self.menu()},RuntimeError('disk slot occupied')]
        with patch('tools.enter_save.handle_file_select') as select,self.assertRaisesRegex(RuntimeError,'occupied'):
            enter_save(client,'same',chapter=0,create_if_missing=True)
        select.assert_not_called()

    def test_new_same_name_leaves_loaded_old_save_before_creating(self):
        old={'scene':'Level','save':{'name':'test','slot':0},'level':{'sid':'Celeste/0-Intro'},'controllable':True}
        menu=self.menu();named=copy.deepcopy(menu);named['menu']['slots'][1]['name']='test'
        new=copy.deepcopy(old);new['save']['slot']=1
        client=Mock();client.call.side_effect=[{'create_save':True},{'obs':old},{'slot':1},{'obs':new}]
        with patch('tools.enter_save.leave_level_by_pause_menu',return_value=menu) as leave, \
             patch('tools.enter_save.press',return_value={'obs':new}):
            self.assertEqual(enter_save(client,'test',chapter=0,create_if_missing=True)['save']['slot'],1)
        leave.assert_called_once_with(client)

    def test_game_connection_rejects_wrong_slot_even_with_same_name(self):
        from orchestrator.game_session import GameSession
        from orchestrator.config import RunConfig
        session=object.__new__(GameSession)
        session.cfg=RunConfig(agent='test',save='same',model='test',save_slot=1)
        client=Mock();client.call.return_value={'obs':{'scene':'Level','save':{'name':'same','slot':0}}}
        with patch('orchestrator.game_session.BridgeClient',return_value=client), \
             self.assertRaisesRegex(RuntimeError,'槽位'):
            session.connect()



if __name__=='__main__':unittest.main()
