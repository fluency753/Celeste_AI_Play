"""续跑时游戏停在哪里：在要续跑的关卡里就直接接着玩，否则用菜单宏进入（连同录像），关卡里是别的存档时报错。"""
import unittest
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.run import check_resume_position


class Client:
    def __init__(self, obs):
        self.obs, self.calls = obs, []

    def call(self, cmd, **args):
        self.calls.append(cmd)
        return {"obs": self.obs}

    def close(self):
        pass


def level(save="opus5.5", sid="Celeste/2-OldSite", slot=8):
    return {"scene": "Level", "save": {"name": save, "slot": slot}, "level": {"sid": sid, "room": "start"}}


class ResumePositionTests(unittest.TestCase):
    def check(self, obs, **cfg_values):
        cfg = RunConfig(agent="opus5.5", save="opus5.5", model="opus")
        cfg.chapter, cfg.chapters, cfg.save_slot = 2, [2], 8
        for k, v in cfg_values.items():
            setattr(cfg, k, v)
        client = Client(obs)
        with patch("tools.bridge_client.BridgeClient", return_value=client):
            return check_resume_position(cfg), client

    def test_continue_in_the_frozen_chapter(self):
        self.assertTrue(self.check(level())[0])

    def test_enter_with_menu_when_not_in_the_chapter(self):
        overworld = {"scene": "Overworld", "save": {"name": "opus5.5", "slot": 8}, "menu": {"current": "OuiChapterSelect"}}
        title = {"scene": "Overworld", "save": None, "menu": {"current": "OuiTitleScreen"}}
        other_menu = {"scene": "Overworld", "save": {"name": "test", "slot": 7}, "menu": {"current": "OuiChapterSelect"}}
        for obs in (overworld, title, other_menu):
            self.assertFalse(self.check(obs)[0], obs)
        self.assertFalse(self.check(level(), resume_pending=True)[0])   # 两截之间中断：进入下一截的起点

    def test_refuses_other_save_or_chapter_inside_a_level(self):
        for obs in (level(save="test", slot=7), level(sid="Celeste/1-ForsakenCity")):
            with self.assertRaises(SystemExit):
                self.check(obs)

    def test_agent_ending_and_between_piece_navigation_do_not_trigger_entry_macro(self):
        for scene in ('AreaComplete','LevelExit','OverworldLoader','Overworld'):
            obs={'scene':scene,'save':{'name':'opus5.5','slot':8}}
            self.assertTrue(self.check(obs,cutscene_mode='agent',resume_completion_pending=True)[0])
        # The previous chapter can still be on screen when the next target is queued.
        self.assertTrue(self.check(level(sid='Celeste/1-ForsakenCity'),cutscene_mode='agent',resume_pending=True)[0])
        with self.assertRaises(SystemExit):
            self.check(level(save='other'),cutscene_mode='agent',resume_pending=True)

    def test_agent_can_start_from_loaded_save_map_without_selecting_chapter(self):
        obs={'scene':'Overworld','save':{'name':'opus5.5','slot':8},'menu':{'current':'OuiChapterSelect','area':7}}
        self.assertTrue(self.check(obs,cutscene_mode='agent')[0])
        obs['save']['slot']=9
        self.assertFalse(self.check(obs,cutscene_mode='agent')[0])

    def test_restart_without_chapter_loads_native_session_without_exiting(self):
        from calibration.menu_navigation_test import GameIpc, file_select, reply
        loaded = level()
        initial = file_select()
        initial['menu']['slots'][0].update(name='opus5.5', fileSlot=8)

        class NativeContinue(GameIpc):
            step_options = None
            policies = []
            closed = False

            def call(client, command, **options):
                if command == 'step':
                    client.policies.append(client.step_options())
                return super().call(command, **options)

            def close(client):
                client.closed = True

        client = NativeContinue(initial, [reply(loaded, stop='chapter_loaded')])
        cfg = RunConfig(agent='opus5.5', save='opus5.5', model='opus', save_slot=8)
        cfg.chapter = None
        cfg.cutscene_mode = 'agent'
        with patch('tools.bridge_client.BridgeClient', return_value=client):
            self.assertTrue(check_resume_position(cfg))
        self.assertEqual(len(client.steps), 1)
        self.assertCountEqual(client.steps[0]['stop_on'], ['chapter_loaded', 'menu_prompt'])
        self.assertEqual(client.policies[0]['action_policy']['entry_target'], {'sid': None, 'slot': 8})
        self.assertEqual(client.policies[0]['action_policy']['departure'], 'current_level')
        self.assertIsNone(client.step_options)
        self.assertTrue(client.closed)

    def test_controller_navigation_grant_is_connection_local_and_revoked_on_error(self):
        from orchestrator.action_policy import controller_navigation
        client = Client({})
        strict = lambda: {'agent_policy': True}
        client.step_options = strict
        cfg = RunConfig(agent='test', save='test', model='test')
        with self.assertRaisesRegex(RuntimeError, 'failed'):
            with controller_navigation(client, cfg, sid='Celeste/3-CelestialResort', slot=10):
                options = client.step_options()
                self.assertTrue(options['allow_map_exit'])
                self.assertFalse(options['allow_chapter_reset'])
                self.assertEqual(options['entry_guard']['slot'], 10)
                raise RuntimeError('failed')
        self.assertIs(client.step_options, strict)

    def test_failed_native_restore_never_releases_the_loaded_state(self):
        from calibration.menu_navigation_test import GameIpc, file_select, reply, blocked
        for response, capable in ((reply(level(save='other', slot=9)), True),
                                  (blocked(file_select()), True), (None, False)):
            cfg = RunConfig(agent='opus5.5', save='opus5.5', model='opus', save_slot=8)
            cfg.chapter = None
            initial = file_select()
            initial['menu']['slots'][0].update(name='opus5.5', fileSlot=8)
            client = GameIpc(initial, [response] if response else [], capability=capable)
            client.step_options = None
            client.close = lambda: None
            with self.subTest(capable=capable, response=response), \
                 patch('tools.bridge_client.BridgeClient', return_value=client), \
                 self.assertRaises(RuntimeError):
                check_resume_position(cfg)
            self.assertNotIn('deactivate', [cmd for cmd, _ in client.calls])
            self.assertTrue(cfg._keep_frozen)
            self.assertIsNone(client.step_options)

    def test_startup_macro_denial_preserves_game_instead_of_unfreezing(self):
        from calibration.menu_navigation_test import GameIpc, level as state, pause_menu, blocked
        from orchestrator.run import enter_save
        initial = state(pauseMenu=pause_menu())
        client = GameIpc(initial, [blocked(initial)])
        client.close = lambda: None
        cfg = RunConfig(agent='test', save='test', model='test', save_slot=10)
        cfg.chapter = 1
        with patch('tools.bridge_client.BridgeClient', return_value=client), self.assertRaises(RuntimeError):
            enter_save(cfg)
        self.assertNotIn('deactivate', [cmd for cmd, _ in client.calls])
        self.assertTrue(cfg._keep_frozen)


if __name__ == "__main__":
    unittest.main()
