"""HUD lifecycle contracts: no model calls, game connection, or actual background launch."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.chat import can_wake
from orchestrator.control import RunControl
from orchestrator.conversations import read_json, selected_run
from orchestrator.session_actions import create_session, disconnect, play_session, _finish, require_idle, write_json, operation_status, store_operation


class SessionActionTests(unittest.TestCase):
    def test_claude_creation_dispatches_provider_without_codex_catalog(self):
        with patch('orchestrator.session_actions.release_chat'), \
             patch('orchestrator.session_actions.launch',return_value=(self.root/'host',{})) as launch, \
             patch('orchestrator.session_actions._monitor'), \
             patch('orchestrator.runtimes.Runtime.token',return_value='stub'), \
             patch('orchestrator.model_catalog.load_models') as codex_catalog:
            create_session(self.anchor,self.anchor,{'engine':'claude','provider':'anthropic',
                           'model':'opus','effort':'high','name':'Claude测试'})
        args = launch.call_args.args[0]
        self.assertEqual(args[args.index('--engine')+1],'claude')
        self.assertEqual(args[args.index('--provider')+1],'anthropic')
        self.assertEqual(args[args.index('--model')+1],'opus')
        codex_catalog.assert_not_called()

    def test_invalid_claude_model_keeps_current_host(self):
        with patch('orchestrator.session_actions.release_chat') as release:
            with self.assertRaises(ValueError):
                create_session(self.anchor,self.anchor,{'engine':'claude','provider':'anthropic',
                               'model':'a-typo','effort':'high'})
        release.assert_not_called()

    def test_prepared_claude_connects_and_play_command_keeps_engine_provider_and_model(self):
        from orchestrator.session_actions import _launch_play
        operation = self.operation()
        operation.update(engine='claude',provider='anthropic')
        directory = Path(operation['directory'])
        proof = read_json(directory/'prepare_summary.json')
        proof.update(engine='claude',provider='anthropic',game_tools_registered=True,requested_model='opus')
        write_json(directory/'prepare_summary.json',proof)
        with patch('orchestrator.session_actions.start_chat'),patch('orchestrator.session_actions.launch') as launch:
            _finish(self.anchor,operation)
            target = selected_run(self.anchor)
            self.assertTrue(can_wake(target))
            state = read_json(target/'run_state.json')
            self.assertEqual(state['engine'],'claude')
            _launch_play(target,{'save':'new','create_save':True,'chapter':0})
        args = launch.call_args.args[0]
        self.assertEqual(args[args.index('--engine')+1],'claude')
        self.assertEqual(args[args.index('--provider')+1],'anthropic')
        self.assertEqual(args[args.index('--model')+1],'opus')
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.root_patch = patch('orchestrator.session_actions.REPO_ROOT', self.root)
        self.root_patch.start()
        self.save_root_patch = patch('orchestrator.saves.REPO_ROOT', self.root)
        self.save_root_patch.start()
        (self.root / 'game/Saves').mkdir(parents=True)
        (self.root / 'game/Saves/0.celeste').write_text('<SaveData><Name>test</Name></SaveData>', encoding='utf-8')
        (self.root / 'game/Mods/CelesteAIBridge').mkdir(parents=True)
        (self.root / 'game/Mods/CelesteAIBridge/everest.yaml').write_text('- Name: CelesteAIBridge\n  Version: 0.12.1\n')
        write_json(self.root / 'configs/local.deploy.json', {'celeste_dir': str(self.root / 'game')})
        self.catalog_patch=patch('orchestrator.model_catalog.load_models',return_value={
            'default_model':'gpt-test','models':[{'id':'gpt-test','efforts':['high']}]})
        self.catalog_patch.start()
        self.anchor = self.root / 'runs/old/20261024-010101'
        self.anchor.mkdir(parents=True)
        write_json(self.anchor / 'run_state.json', {'engine':'codex','session_id':'old-thread','agent':'old',
            'save':'test','model':'old-model','effort':'high','knowledge_profile':'explore'})
        write_json(self.anchor / 'summary.json', {'completed':True})
        write_json(self.anchor / 'metrics.json', {'chapter':'Celeste/1-ForsakenCity','room':'end', 'decisions':289})
        (self.anchor / 'codex_events.jsonl').write_text('old conversation', encoding='utf-8')
        RunControl(self.anchor).set_fast(False)

    def tearDown(self):
        self.catalog_patch.stop()
        self.root_patch.stop()
        self.save_root_patch.stop()
        self.temp.cleanup()

    def test_new_preparation_uses_unique_workspace_and_preserves_fast_off(self):
        host = self.root / 'host'
        with patch('orchestrator.session_actions.REPO_ROOT', self.root), \
             patch('orchestrator.session_actions.release_chat') as release, \
             patch('orchestrator.session_actions.launch', return_value=(host, {})) as launch, \
             patch('orchestrator.session_actions._monitor'):
            operation = create_session(self.anchor, self.anchor, {'name':'新玩家','model':'gpt-test','effort':'high'})
        self.assertEqual(operation['status'], 'preparing')
        self.assertEqual(selected_run(self.anchor), self.anchor)
        release.assert_called_once_with(self.anchor)
        arguments = launch.call_args.args[0]
        self.assertEqual(launch.call_args.kwargs['service'], 'prepare')
        self.assertTrue(arguments[arguments.index('--agent')+1].startswith('codex-hud-'))
        self.assertEqual(arguments[arguments.index('--fast')+1], 'off')
        self.assertIn('--hud-managed', arguments)

    def test_starting_status_follows_new_run_before_its_controller_connects(self):
        operation={'id':'test-start','agent':'new-agent','kind':'play','status':'starting','host':'fake'}
        store_operation(self.anchor,operation,new=True)
        new_run=self.root/'runs/new-agent/20261024-030303'
        new_run.mkdir(parents=True)
        with patch('orchestrator.session_actions._monitor'):
            self.assertEqual(operation_status(new_run)['status'],'starting')
            unrelated=self.root/'runs/another/20261024-030303'
            self.assertEqual(operation_status(unrelated),{})

    def test_new_session_accepts_catalog_declared_effort(self):
        with patch('orchestrator.model_catalog.load_models',return_value={
                'default_model':'gpt-test', 'models':[{'id':'gpt-test','efforts':['minimal','ultra']}]}), \
             patch('orchestrator.session_actions.release_chat'), \
             patch('orchestrator.session_actions.launch',return_value=(self.root/'host',{})) as launch, \
             patch('orchestrator.session_actions._monitor'):
            create_session(self.anchor,self.anchor,{'model':'gpt-test','effort':'ultra'})
        args=launch.call_args.args[0]
        self.assertEqual(args[args.index('--effort')+1], 'ultra')

    def test_invalid_options_never_release_current_session(self):
        with patch('orchestrator.session_actions.release_chat') as release:
            for options in ({'name':None}, {'model':'bad; command'}, {'effort':'unknown'}, {'extra':True}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    create_session(self.anchor, self.anchor, options)
        release.assert_not_called()

    def test_unknown_model_or_unsupported_effort_keeps_existing_chat(self):
        with patch('orchestrator.session_actions.release_chat') as release:
            for options in ({'model':'gpt-made-up','effort':'high'}, {'model':'gpt-test','effort':'medium'}):
                with self.subTest(options=options),self.assertRaises(ValueError):
                    create_session(self.anchor,self.anchor,options)
        release.assert_not_called()

    def test_active_game_and_active_reply_reject_management(self):
        control = RunControl(self.anchor)
        control.heartbeat(chat_only=False, phase='paused')
        with self.assertRaisesRegex(ValueError, '停止试玩'):
            require_idle(self.anchor)
        control.heartbeat(chat_only=True, phase='chatting')
        with self.assertRaisesRegex(ValueError, '正在回复'):
            disconnect(self.anchor, self.anchor)

    def operation(self, complete=True):
        directory = self.root / 'runs/new/20261024-020202'
        directory.mkdir(parents=True)
        metrics = {'model':'new-model','effort':'high','context_tokens':123,'tool_calls':4}
        write_json(directory / 'metrics.json', metrics)
        write_json(directory / 'prepare_summary.json', {'kb_study':{'complete':complete},'session_id':'new-thread',
            'native_game_tools':True,'metrics':metrics})
        (directory / 'codex_events.jsonl').write_text('new preparation', encoding='utf-8')
        host = self.root / 'host'
        write_json(host / 'status.json', {'state':'exited','exit_code':0})
        return {'id':'test-op','kind':'new','status':'preparing','previous':str(self.anchor),'directory':str(directory),
                'host':str(host),'agent':'new','name':'新的会话','fast':False}

    def test_completed_preparation_connects_chat_without_starting_game_or_copying_old_messages(self):
        operation = self.operation()
        with patch('orchestrator.session_actions.start_chat') as start, patch('orchestrator.session_actions.launch') as launch:
            _finish(self.anchor, operation)
        target = selected_run(self.anchor)
        self.assertNotEqual(target, self.anchor)
        self.assertTrue(can_wake(target))
        self.assertEqual(read_json(target / 'run_state.json')['session_kind'], 'ready')
        self.assertEqual(read_json(target / 'metrics.json')['room'], 'end')
        self.assertEqual((target / 'codex_events.jsonl').read_text(), 'new preparation')
        self.assertFalse(RunControl(target).fast_preference())
        self.assertEqual(read_json(self.anchor / 'session_operation.json')['status'], 'ready')
        start.assert_called_once_with(target)
        launch.assert_not_called()

    def test_failed_preparation_restores_old_chat_and_selection(self):
        operation = self.operation(complete=False)
        with patch('orchestrator.session_actions.start_chat') as start:
            _finish(self.anchor, operation)
        self.assertEqual(selected_run(self.anchor), self.anchor)
        start.assert_called_once_with(self.anchor)
        self.assertEqual(read_json(self.anchor / 'session_operation.json')['status'], 'error')

    def test_unprepared_old_session_cannot_silently_start_new_game(self):
        with patch('orchestrator.session_actions.release_chat') as release, self.assertRaisesRegex(ValueError, '新建并准备'):
            play_session(self.anchor, self.anchor, {'chapter':1})
        release.assert_not_called()

    def test_ready_session_requires_valid_proof_to_wake(self):
        state = read_json(self.anchor / 'run_state.json')
        state['session_kind'] = 'ready'
        write_json(self.anchor / 'run_state.json', state)
        self.assertFalse(can_wake(self.anchor))
        write_json(self.anchor / 'prepare_summary.json', {'kb_study':{'complete':True},'quick_check':True})
        self.assertFalse(can_wake(self.anchor))
        write_json(self.anchor / 'prepare_summary.json', {'kb_study':{'complete':True}})
        self.assertTrue(can_wake(self.anchor))

    def test_game_start_is_separate_click_and_starts_paused(self):
        operation = self.operation()
        with patch('orchestrator.session_actions.start_chat'):
            _finish(self.anchor, operation)
        current = selected_run(self.anchor)
        RunControl(current).set_cutscene_mode('agent')
        with patch('orchestrator.session_actions.REPO_ROOT', self.root), \
             patch('orchestrator.session_actions.release_chat') as release, \
             patch('orchestrator.session_actions.launch', return_value=(self.root / 'play-host', {})) as launch, \
             patch('orchestrator.session_actions._monitor'), \
             patch('orchestrator.prepared.load_preparation'):
            play_session(self.anchor, current, {'chapter':1,'save':'test','create_save':False,'save_slot':0})
        release.assert_called_once_with(current)
        arguments = launch.call_args.args[0]
        self.assertIn('--start-paused', arguments)
        self.assertIn('--playback', arguments)
        self.assertEqual(arguments[arguments.index('--chapter')+1], '1')
        self.assertEqual(arguments[arguments.index('--save')+1], 'test')
        self.assertNotIn('--create-save', arguments)
        self.assertEqual(arguments[arguments.index('--cutscene-mode')+1],'agent')

    def test_new_save_is_explicit_and_forwarded_without_touching_game_files(self):
        operation = self.operation()
        with patch('orchestrator.session_actions.start_chat'):
            _finish(self.anchor, operation)
        current = selected_run(self.anchor)
        self.assertEqual(read_json(current/'run_state.json')['save'], '')
        with patch('orchestrator.session_actions.release_chat'), \
             patch('orchestrator.session_actions.launch', return_value=(self.root/'host2',{})) as launch, \
             patch('orchestrator.session_actions._monitor'),patch('orchestrator.prepared.load_preparation'):
            play_session(self.anchor,current,{'chapter':0,'save':'小雪','create_save':True,'save_slot':None})
        arguments=launch.call_args.args[0]
        self.assertIn('--create-save',arguments)
        self.assertEqual(arguments[arguments.index('--save')+1],'小雪')
        self.assertEqual(len(list((self.root/'game/Saves').iterdir())),1)

    def test_missing_selection_or_invalid_slot_keeps_chat_connected(self):
        operation=self.operation()
        with patch('orchestrator.session_actions.start_chat'):_finish(self.anchor,operation)
        current=selected_run(self.anchor)
        with patch('orchestrator.session_actions.release_chat') as release:
            for options in [{'chapter':0},{'chapter':0,'save':None,'create_save':True,'save_slot':None},
                            {'chapter':0,'save':'missing','create_save':False,'save_slot':999},
                            {'chapter':1,'save':'new','create_save':True,'save_slot':None}]:
                with self.subTest(options=options),self.assertRaises(ValueError):
                    play_session(self.anchor,current,options)
        release.assert_not_called()

    def test_blank_new_name_asks_same_conversation_before_any_game_launch(self):
        operation=self.operation()
        with patch('orchestrator.session_actions.start_chat'):_finish(self.anchor,operation)
        current=selected_run(self.anchor)
        with patch('orchestrator.session_actions.release_chat'), \
             patch('orchestrator.session_actions.launch',return_value=(self.root/'namer',{})) as launch, \
             patch('orchestrator.session_actions._monitor'),patch('orchestrator.prepared.load_preparation'):
            status=play_session(self.anchor,current,{'chapter':0,'save':'','create_save':True,'save_slot':None})
        self.assertEqual(status['status'],'naming')
        self.assertEqual(launch.call_args.kwargs,{'service':'chat'})
        args=launch.call_args.args[0]
        self.assertEqual(args[args.index('--run')+1],str(current))
        self.assertIn('--name-save',args)
        self.assertNotIn('--create-save',args)

    def naming_operation(self, name, request_id='name-op'):
        host=self.root/'name-host'
        write_json(host/'status.json',{'state':'exited','exit_code':0})
        write_json(self.anchor/'save_name_result.json',{'request_id':request_id,'name':name})
        return {'id':'name-op','kind':'play','status':'naming','previous':str(self.anchor),
                'host':str(host),'latest_before':'','agent':'old',
                'play_options':{'chapter':0,'save':'','create_save':True,'save_slot':None}}

    def test_generated_name_is_used_to_launch_after_naming_succeeds(self):
        operation=self.naming_operation('test') # reusing an existing name is allowed
        new=self.root/'runs/old/new'
        def launch_game(current,options):
            RunControl(new).heartbeat(chat_only=False,phase='idle')
            (self.root/'runs/latest.txt').write_text(str(new))
            return self.root/'play-host',{}
        with patch('orchestrator.session_actions._launch_play',side_effect=launch_game) as launch:
            _finish(self.anchor,operation)
        self.assertEqual(launch.call_args.args[1]['save'],'test')
        self.assertEqual(operation['status'],'ready')
        self.assertEqual(operation['save_name'],'test')

    def test_invalid_or_stale_name_result_never_starts_game(self):
        for name,request in [('a'*21,'name-op'),('valid','previous-request')]:
            operation=self.naming_operation(name,request)
            with patch('orchestrator.session_actions._launch_play') as launch, \
                 patch('orchestrator.session_actions.start_chat'):
                _finish(self.anchor,operation)
            launch.assert_not_called()
            self.assertEqual(operation['status'],'error')


if __name__ == '__main__':
    unittest.main()
