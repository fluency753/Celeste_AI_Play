"""Exploration keeps native map permissions separate from completion and time goals."""
import json
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from orchestrator.config import CHAPTER_SIDS, RunConfig
from orchestrator.game_session import ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.free_roam import validate_maps
from orchestrator.action_policy import block_note


class FreeRoamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.maps = [{'sid': CHAPTER_SIDS[1], 'side': 'A'}]
        self.cfg = RunConfig(agent='test', save='test', save_slot=14, model='test', runtime='host',
                             play_scope='visited', completion_policy='continue', cutscene_mode='agent',
                             chapters=[1, 2], visited_maps=None)
        self.metrics = Metrics(self.root/'metrics.json', 'test', 'test', 'low', 600)
        self.session = GameSession(self.cfg, self.root, self.metrics)
        self.addCleanup(self.session.close, release=False)
        (self.root/'run_state.json').write_text('{}', encoding='utf-8')

    def obs(self, *, level=True, completed=False):
        return {'scene': 'Level' if level else 'Overworld', 'frame': 1, 'storyInput': None,
                'save': {'name': 'test', 'slot': 14, 'visited_maps': self.maps},
                'menu': {'current': 'OuiChapterSelect'},
                'level': {'sid': CHAPTER_SIDS[1], 'side': 'A', 'completed': completed,
                          'room': 'start', 'chapterTimeTicks': 100} if level else {}}

    def test_snapshot_is_fixed_and_persisted_before_inputs(self):
        self.session._bind_visited_maps(self.obs())
        self.assertEqual(json.loads((self.root/'run_state.json').read_text())['visited_maps'], self.maps)
        other = self.obs(level=False)
        other['save']['visited_maps'] = self.maps + [{'sid': CHAPTER_SIDS[2], 'side': 'A'}]
        self.session._bind_visited_maps(other)
        self.assertEqual(self.cfg.visited_maps, self.maps)
        self.session.action_policy_supported = 1
        self.session.map_entry_set_supported = True
        self.assertEqual(self.session._step_options()['action_policy']['entry_target'],
                         {'sid': None, 'slot': 14, 'maps': self.maps})
        self.assertFalse(self.session.chapter_queue)
        self.assertIsNone(self.session.piece)

    def test_bad_snapshot_and_wrong_current_side_fail_closed(self):
        for value in (None, {}, [{'sid': 'x', 'side': 'D'}], [{'sid': '', 'side': 'A'}]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_maps(value)
        self.cfg.visited_maps = self.maps
        obs = self.obs()
        obs['level']['side'] = 'B'
        with self.assertRaisesRegex(RuntimeError, '不在'):
            self.session._bind_visited_maps(obs)
        obs = self.obs(level=False)
        obs.update(scene='IntroVignette', reload_boundary={'session': {'sid': CHAPTER_SIDS[1], 'side': 'B'}})
        with self.assertRaisesRegex(RuntimeError, '不在'):
            self.session._bind_visited_maps(obs)
        obs['scene'] = 'Overworld'
        self.session._bind_visited_maps(obs)  # stale menu Session is not current gameplay

    def test_production_start_publishes_state_before_binding_save_permissions(self):
        from orchestrator.run import _run_connected_session
        (self.root/'run_state.json').unlink()
        bridge = Mock()
        def call(command, **kwargs):
            if command == 'hello':
                return {'pico8_entry_guard': True, 'map_entry_set': True, 'entry_guard': True, 'action_policy': 1}
            return {'obs': self.obs(level=False)}
        bridge.call.side_effect = call
        args = SimpleNamespace(prepared=None)
        # Stop at the first external save-backup boundary, after the production
        # connect/bind chain. No pre-created run_state conceals ordering errors.
        with patch('orchestrator.game_session.BridgeClient', return_value=bridge), \
             patch('orchestrator.run.backup_save', side_effect=RuntimeError('backup boundary')):
            with self.assertRaisesRegex(RuntimeError, 'backup boundary'):
                asyncio.run(_run_connected_session(self.cfg, args, self.root, None, None, None,
                                                   self.metrics, None, None, self.session))
        saved = json.loads((self.root/'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['visited_maps'], self.maps)
        self.assertEqual(saved['save_slot'], 14)

    def test_persistence_failure_does_not_publish_permission(self):
        with patch.object(Path, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.session._bind_visited_maps(self.obs())
        self.assertIsNone(self.cfg.visited_maps)

    def test_old_bridge_rejected_before_activate(self):
        bridge = Mock()
        bridge.call.return_value = {'pico8_entry_guard': True, 'action_policy': 1}
        with patch('orchestrator.game_session.BridgeClient', return_value=bridge):
            with self.assertRaisesRegex(RuntimeError, 'map_entry_set'):
                self.session.connect()
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list], ['hello'])

    def test_repeated_completion_and_checkpoint_reentry_keep_exploring(self):
        for _ in range(2):
            self.session._update_metrics_from_obs(self.obs(completed=True))
            self.session._handle_events({'events': [{'type': 'complete'}]})
            self.session._commit_completion(self.obs(completed=True))
            self.assertTrue(self.session._pending_complete)
            self.session._commit_completion(self.obs(level=False))
            self.assertFalse(self.session.done)
            self.assertFalse(self.session._pending_complete)
            self.assertFalse(self.session.chapter_finished)
            # A native unlocked checkpoint is freely navigable; no forward-only
            # Piece or end-room guard is installed for experimentation.
            self.assertEqual(self.session._stop_rooms(), {})
            self.assertFalse(self.session._note_room(CHAPTER_SIDS[1], 'earlier'))
            self.session._update_metrics_from_obs(self.obs())
            self.assertFalse(self.metrics.state.piece_done)
        self.assertEqual(len(self.metrics.state.splits), 2)
        self.assertEqual([split['side'] for split in self.metrics.state.splits], ['A', 'A'])
        with self.assertRaisesRegex(ValueError, '自由探索'):
            self.session.set_plan([])
        self.assertFalse(self.session.chapter_plan()['editable'])

    def test_completion_target_still_stops_free_roam(self):
        self.session._update_metrics_from_obs(self.obs())
        self.session.goals.set({'conditions': [{'field': 'map_complete', 'op': 'eq', 'value': True}]},
                               self.session.goal_values(self.obs()))
        self.session._handle_events({'events': [{'type': 'complete'}]})
        self.session._commit_completion(self.obs(level=False))
        self.assertTrue(self.session.done)
        self.assertTrue(self.session.goals.stop_motion.is_set())

    def test_time_goal_still_stops_and_does_not_refresh_on_reentry(self):
        self.session.game_process_id = 1
        baseline = self.session.goal_values(self.obs())
        self.session.goals.set({'conditions': [{'field': 'game_elapsed', 'op': 'delta_gte', 'value': 1}]}, baseline)
        after = self.obs()
        after['frame'] = 61
        self.assertTrue(self.session.goals.check(self.session.goal_values(after)))
        self.assertTrue(self.session.goals.stop_motion.is_set())

    def test_block_message_names_side_scope_and_unexecuted_remainder(self):
        note = block_note({'action': 'enter_map', 'rule_id': 'target.maps', 'expected': self.maps,
                           'actual': {'sid': CHAPTER_SIDS[1], 'side': 'B'}})
        for text in ('B面', 'A面', '未执行', '不要重试'):
            self.assertIn(text, note)
        note = block_note({'action': 'enter_map', 'rule_id': 'target.new_save'})
        for text in ('新建存档', '没有创建', '未执行', '原存档'):
            self.assertIn(text, note)

    def test_continue_plan_cannot_be_replaced_with_partial_checkpoint_stop(self):
        from orchestrator.segments import Piece
        from calibration.chapter_plan_test import TABLE
        self.cfg.play_scope = 'chapter'
        self.cfg.checkpoints = TABLE
        self.session.piece = Piece(1)
        with self.assertRaisesRegex(ValueError, '原计划未改变'):
            self.session.set_plan([])
        self.assertEqual(self.session.piece, Piece(1))
        message = self.session.set_plan([(1, i) for i in range(1, len(TABLE[1]))])
        self.assertIn('继续探索', message)
        self.assertNotIn('运行结束', message)


class ContinueAfterPlanTests(unittest.TestCase):
    """整章／顺序范围 + 通关后继续：计划完成后重进、再通关时计时照常累计，每次通关只记一条成绩。"""

    def make(self, chapters):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        cfg = RunConfig(agent='test', save='test', save_slot=14, model='test', runtime='host',
                        chapter=chapters[0], chapters=chapters, cutscene_mode='agent', completion_policy='continue')
        self.session = GameSession(cfg, self.root, Metrics(self.root/'metrics.json', 'test', 'test', 'low', 600))
        self.addCleanup(self.session.close, release=False)
        (self.root/'run_state.json').write_text('{}', encoding='utf-8')
        return self.session

    @staticmethod
    def level(chapter, ticks, completed=False):
        return {'scene': 'Level', 'frame': 1, 'storyInput': None, 'save': {'name': 'test', 'slot': 14},
                'level': {'sid': CHAPTER_SIDS[chapter], 'side': 'A', 'completed': completed, 'room': 'start',
                          'chapterTimeTicks': ticks}}

    def see(self, obs):
        self.session._commit_completion(obs)
        self.session._update_metrics_from_obs(obs)

    def clear(self, chapter, ticks):
        """原生通关事件 → 结算 → agent 自己回到选章地图，走主控的真实确认链。"""
        self.see(self.level(chapter, ticks, completed=True))
        self.session._handle_events({'events': [{'type': 'complete'}]})
        for obs in (self.level(chapter, ticks, completed=True), {'scene': 'AreaComplete', 'storyInput': None},
                    {'scene': 'Overworld', 'menu': {'current': 'OuiChapterSelect'}},
                    {'scene': 'Overworld', 'menu': {'current': 'OuiChapterSelect'}}):
            self.see(obs)

    def hud(self):
        from overlay.modhud import view_model
        return view_model(json.loads((self.root/'metrics.json').read_text(encoding='utf-8')), {}, [], 0)

    def hud_timer(self):
        """游戏内 HUD 的主计时：基数加上（计时进行中时）当前章节计时，与 ModHud.DrawTimer 相同。"""
        vm = self.hud()
        return vm.timer_base + (vm.timer_extra if vm.timer_live else 0)

    def test_reentry_after_completed_plan_keeps_timer_running_and_records_each_clear_once(self):
        s = self.make([1])
        self.see(self.level(1, 600))
        self.assertEqual(self.hud_timer(), 600)
        self.clear(1, 1000)
        self.assertFalse(s.done or s.chapter_finished)
        self.assertEqual([split['ticks'] for split in s.metrics.state.splits], [1000])
        self.assertEqual(self.hud_timer(), 1000)
        self.see(self.level(1, 0))                 # 计划完成后重新进入同一章
        self.assertFalse(s.metrics.state.piece_done)
        self.see(self.level(1, 400))
        self.assertEqual(self.hud_timer(), 1400)    # 不再停在上一次通关的总计时
        self.clear(1, 900)
        self.assertEqual([split['ticks'] for split in s.metrics.state.splits], [1000, 900])
        self.assertEqual(s.metrics.state.completed_ticks, 1900)
        self.assertEqual(self.hud_timer(), 1900)    # 当前章已计入成绩，不再重复加
        self.see({'scene': 'Overworld', 'menu': {'current': 'OuiChapterSelect'}})
        self.assertEqual(len(s.metrics.state.splits), 2)
        self.assertFalse(s.done)

    def test_switching_to_next_planned_chapter_is_not_reset_by_old_level_frames(self):
        s = self.make([1, 2])
        self.see(self.level(1, 600))
        self.clear(1, 1000)
        self.assertTrue(s.chapter_finished and s.metrics.state.piece_done)
        self.see(self.level(1, 1000))               # 换章途中的旧关卡帧不能让上一章计时重复计入
        self.assertTrue(s.metrics.state.piece_done)
        self.assertEqual(self.hud_timer(), 1000)
        s.client = Mock()
        s.client.call.return_value = {'obs': self.level(2, 0), 'image': None}
        s._observe_next_chapter()
        self.assertFalse(s.chapter_finished or s.metrics.state.piece_done)
        self.clear(2, 500)                          # 计划完成：继续探索
        self.assertFalse(self.hud().timer_live)
        self.see(self.level(2, 0))
        self.see(self.level(2, 70))
        self.assertTrue(self.hud().timer_live)      # 重进后当前计时照常走，不停在上一次的成绩
        self.assertEqual(self.hud().timer_extra, 70)
        self.clear(2, 450)
        self.assertEqual([split['ticks'] for split in s.metrics.state.splits], [1000, 500, 450])
        self.assertEqual(s.metrics.state.completed_ticks, 1950)
        self.assertFalse(self.hud().timer_live)


if __name__ == '__main__':
    unittest.main()
