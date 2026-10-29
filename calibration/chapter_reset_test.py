"""Controller policy uses the real bridge dispatch and isolated run preferences."""
import json
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from orchestrator.config import RunConfig
from orchestrator.control import RunControl
from orchestrator.game_session import ActError, GameSession
from orchestrator.launch_options import build, normalize
from orchestrator.metrics import Metrics
from orchestrator.prompting import play_prompt
from orchestrator.bridge_requirements import preflight_bridge
from tools.bridge_client import BridgeClient
from calibration.map_entry_guard_test import MenuClient
from orchestrator.config import CHAPTER_SIDS


class ChapterResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = RunConfig(agent='test', save='test', model='test', runtime='host')
        self.session = GameSession(self.cfg, self.root, Metrics(self.root/'metrics.json', 'test', 'test', 'high', 60))
        self.session.entry_guard_supported = True
        self.session.chapter_reset_guard_supported = True
        self.session.action_policy_supported = 1

    def tearDown(self):
        self.session.close()
        self.temp.cleanup()

    def client(self):
        client = BridgeClient.__new__(BridgeClient)
        client.next_id, client.cancel_check = 1, None
        client.step_options = self.session._step_options
        client.file = Mock()
        client.file.readline.side_effect = [b'{"id":1,"ok":true}\n', b'{"id":2,"ok":true}\n']
        return client

    def test_default_and_explicit_configuration_reach_prompt_and_launcher(self):
        self.assertFalse(self.cfg.allow_chapter_reset)
        self.assertFalse(normalize({'agent':'test'})['allow_chapter_reset'])
        self.assertIn('--no-allow-chapter-reset', build({'agent':'test'}))
        self.assertIn('--allow-chapter-reset', build({'agent':'test', 'allow_chapter_reset': True}))
        self.assertIn('重启章节=禁止', play_prompt(self.cfg))

    def test_every_caller_is_constrained_and_cannot_override_session_policy(self):
        client = self.client()
        client.call('step', inputs=['1,P', '1,D,C'], allow_chapter_reset=True, allow_map_exit=True)
        sent = json.loads(client.file.write.call_args.args[0])
        self.assertIs(sent['allow_chapter_reset'], False)
        self.assertIs(sent['allow_map_exit'], False)
        self.cfg.allow_chapter_reset = True
        client.call('step', inputs=['1,C'])
        self.assertIs(json.loads(client.file.write.call_args.args[0])['allow_chapter_reset'], True)

    def test_unsupported_bridge_does_not_receive_inputs_even_when_all_permissions_allowed(self):
        self.session.action_policy_supported = None
        client = self.client()
        with self.assertRaisesRegex(ValueError, 'action_policy=1'):
            client.call('step', inputs=['1,C'])
        client.file.write.assert_not_called()
        self.cfg.allow_chapter_reset = True
        with self.assertRaisesRegex(ValueError, 'action_policy=1'):
            client.call('step', inputs=['1,C'])
        client.file.write.assert_not_called()

    def test_preflight_rejects_old_bridge_before_any_activate_or_step(self):
        client = Mock()
        client.call.return_value = {'bridge':'1.0.27', 'create_save':True, 'dialog_choices':True,
                                    'story_text':True, 'entry_guard':True}
        with patch('tools.bridge_client.BridgeClient', return_value=client):
            with self.assertRaisesRegex(RuntimeError, '未开始录制或进档'):
                preflight_bridge(1)
        self.assertEqual([c.args[0] for c in client.call.call_args_list], ['hello'])
        client.close.assert_called_once()

    def test_explicit_exit_tool_is_blocked_before_any_menu_operations(self):
        self.session.client = Mock()
        with patch('orchestrator.game_session.leave_level_by_pause_menu') as leave:
            with self.assertRaisesRegex(ActError, '禁止'):
                self.session.exit_level('脱困')
        leave.assert_not_called()
        self.session.client.call.assert_not_called()
        self.assertFalse(self.session.done)

    def test_explicit_exit_uses_only_the_individually_allowed_menu_action(self):
        from orchestrator.action_policy import ActionPermissions, configure
        configure(self.cfg, ActionPermissions(save_quit=True))
        session = self.session
        class Menu:
            def __init__(client):
                client.exits = []
                client.obs = {'scene':'Level','frame':1,'level':{'paused':True}, 'pauseMenu':{
                    'focused':True,'selection':0,'items':[
                        {'index':0,'id':'menu_pause_return','selectable':True,'disabled':False},
                        {'index':1,'id':'menu_pause_savequit','selectable':True,'disabled':False}]}}
            def call(client, cmd, **args):
                if cmd == 'step':
                    for segment in args['inputs']:
                        keys = segment.split(',')[1:]
                        if 'D' in keys: client.obs['pauseMenu']['selection'] = 1
                        if 'C' in keys:
                            action = client.obs['pauseMenu']['items'][client.obs['pauseMenu']['selection']]['id']
                            client.exits.append(action)
                            client.obs = {'scene':'Overworld','frame':20}
                return {'obs':copy.deepcopy(client.obs)}
            def close(client): pass
        client = Menu(); session.client = client; session.last_obs = copy.deepcopy(client.obs)
        result = session.exit_level('用户要求保存并结束试玩',finish_run=True)
        self.assertEqual(client.exits, ['menu_pause_savequit'])
        self.assertTrue(result['exited'])
        self.assertFalse(result['completed'])

    def test_blocks_are_explained_in_chinese_and_old_bridges_get_an_act_error(self):
        from orchestrator.action_policy import block_note
        from orchestrator.observation import describe_step
        note = block_note({'rule_id': 'permission.return_to_map', 'action': 'return_to_map', 'reason': 'x',
                           'expected': True, 'actual': False})
        self.assertIn('「返回地图」', note)
        self.assertIn('暂停菜单仍开着', note)
        self.assertIn('stop_run', note)
        unknown = block_note({'rule_id': 'menu.unknown_item', 'action': 'menu_item', 'reason': 'x',
                              'expected': 'resume/retry/skip_cutscene/cancel', 'actual': 'Mod Options'})
        self.assertIn('Mod Options', unknown)
        self.assertIn('只能选继续、重试、跳过剧情或取消', unknown)
        entry = block_note({'rule_id': 'target.map', 'action': 'enter_map', 'reason': 'x',
                            'expected': 'Celeste/5-MirrorTemple', 'actual': 'Celeste/4-GoldenRidge'})
        self.assertIn('地图与任务不符', entry)
        self.assertNotIn('暂停菜单', entry)
        self.assertIn('主控操作权限拦截', describe_step({'consumed': 1, 'planned': 2, 'stop': 'chapter_reset_blocked', 'events': []}))
        self.assertIn('只能选继续、重试、跳过剧情或取消', play_prompt(self.cfg))
        self.session.action_policy_supported = 0
        with self.assertRaises(ActError):
            self.session._step_options()

    def test_verified_handoff_allows_departure_but_never_restart(self):
        self.session.chapter_finished = True
        options = self.session._step_options()
        self.assertTrue(options['allow_map_exit'])
        self.assertFalse(options['allow_chapter_reset'])

    def test_policy_survives_reopen_and_rejects_bad_values(self):
        control = RunControl(self.root)
        self.assertIsNone(control.chapter_reset_preference())
        for enabled in (False, False, True, False):
            control.set_chapter_reset(enabled)
            self.assertIs(RunControl(self.root).chapter_reset_preference(), enabled)
        with self.assertRaises(ValueError):
            control.enqueue('chapter_reset', 'maybe')
        with self.assertRaises(ValueError):
            control.set_chapter_reset('false')

    def test_user_requested_startup_navigation_can_leave_old_level_before_agent_policy(self):
        from orchestrator.run import enter_save
        cfg = self.cfg
        cfg.chapter, cfg.save_slot = 1, 10
        for recording in (False, True):
            for allowed in (False, True):
                cfg.allow_chapter_reset = allowed
                class StartupClient(MenuClient):
                    def __init__(client):
                        super().__init__(lambda: {'entry_guard':{'sid':CHAPTER_SIDS[1], 'slot':10}})
                        client.step_options = None
                        client.departures = 0
                        client.obs = {'scene':'Level', 'save':{'name':'test','slot':10}, 'controllable':True,
                                      'level':{'sid':CHAPTER_SIDS[3], 'paused':True},
                                      'pauseMenu':{'focused':True, 'selection':0, 'items':[
                                          {'index':0,'id':'menu_pause_return','selectable':True,'disabled':False}]}}
                    def call(client, cmd, **args):
                        if cmd != 'step':
                            return super().call(cmd, **args)
                        policy = client.step_options()
                        self.assertIs(policy['allow_chapter_reset'], allowed)
                        self.assertTrue(policy['allow_map_exit'])
                        keys = ','.join(args['inputs'])
                        if client.obs['scene'] == 'Level':
                            if ',C' in keys:
                                client.departures += 1
                                client.obs = {'scene':'Overworld', 'save':{'name':'test','slot':10},
                                              'menu':{'current':'OuiChapterSelect','area':1}}
                        elif (client.obs.get('menu') or {}).get('current') == 'OuiChapterSelect' and ',B' in keys:
                            client.obs['menu'] = {'current':'OuiFileSelect','slotIndex':0,'slotSelected':False,
                                'slots':[{'index':0,'name':'test','fileSlot':10,'exists':True}], 'buttons':['Continue'],'buttonIndex':0}
                        elif (client.obs.get('menu') or {}).get('current') == 'OuiFileSelect':
                            if ',C' in keys:
                                if client.obs['menu']['slotSelected']:
                                    client.obs['menu'] = {'current':'OuiChapterSelect','area':1}
                                else:
                                    client.obs['menu']['slotSelected'] = True
                        else:
                            return super().call(cmd, **args)
                        return {'obs':copy.deepcopy(client.obs), 'result':{'consumed':1,'stop':'done'}}
                client = StartupClient()
                with self.subTest(recording=recording, allowed=allowed), \
                     patch('tools.bridge_client.BridgeClient', return_value=client):
                    enter_save(cfg, Mock() if recording else None)
                    self.assertEqual(client.departures, 1)
                    self.assertEqual(client.obs['level']['sid'], CHAPTER_SIDS[1])
        # The startup-only permission does not leak into the agent connection.
        cfg.allow_chapter_reset = False
        self.assertFalse(self.session._step_options()['allow_map_exit'])
