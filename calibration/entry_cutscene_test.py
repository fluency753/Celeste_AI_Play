"""入场剧情的暂时不可暂停、永久不可跳过及用户中断。"""
import copy
import unittest
from unittest.mock import Mock, patch

from tools.enter_save import enter_save


def state(*, controllable=False, can_pause=False):
    return {'scene': 'Level', 'controllable': controllable,
            'save': {'name': 'test', 'slot': 10},
            'level': {'sid': 'Celeste/2-OldSite', 'side': 'A', 'deaths': 0, 'room': '3',
                      'inCutscene': True, 'skippingCutscene': False,
                      'canPause': can_pause, 'dialogChoices': []}}


class EntryCutsceneTests(unittest.TestCase):
    def enter(self, initial, **kwargs):
        client = Mock()
        client.call.return_value = {'obs': initial}
        return enter_save(client, 'test', chapter=2, save_slot=10, **kwargs)

    def test_temporary_no_pause_retries_when_input_becomes_ready(self):
        start = state()
        ready = state(can_pause=True)
        end = state(controllable=True, can_pause=True)
        end['level']['inCutscene'] = False
        with patch('tools.enter_save.skip_by_pause_menu', side_effect=[
                (False, start, 0, 'no_pause'), (True, end, 32, 'skipped')]) as skip, \
             patch('tools.enter_save.step', return_value={'obs': ready}) as step:
            self.assertEqual(self.enter(start), end)
        self.assertEqual(skip.call_count, 2)
        self.assertEqual(step.call_args.args[1], ['10'])
        self.assertEqual(step.call_args.kwargs['stop_on'], ['controllable'])

    def test_temporary_failure_does_not_hand_back_controllable_intro(self):
        start = state(controllable=True)
        ready = state(controllable=True, can_pause=True)
        end = copy.deepcopy(ready)
        end['level']['inCutscene'] = False
        with patch('tools.enter_save.skip_by_pause_menu', side_effect=[
                (False, start, 0, 'no_pause'), (True, end, 32, 'skipped')]) as skip, \
             patch('tools.enter_save.step', return_value={'obs': ready}):
            self.assertEqual(self.enter(start), end)
        self.assertEqual(skip.call_count, 2)

    def test_no_skip_does_not_repeatedly_open_pause_menu(self):
        start = state(can_pause=True)
        end = state(controllable=True, can_pause=True)
        with patch('tools.enter_save.skip_by_pause_menu', return_value=(False, start, 30, 'no_skip')) as skip, \
             patch('tools.enter_save.step', return_value={'obs': end}):
            self.assertEqual(self.enter(start), end)
        skip.assert_called_once()

    def test_user_stop_is_checked_between_transient_retries(self):
        start = state()
        ready = state(can_pause=True)
        with patch('tools.enter_save.skip_by_pause_menu', return_value=(False, start, 0, 'no_pause')) as skip, \
             patch('tools.enter_save.step', return_value={'obs': ready}) as step:
            result = self.enter(start, should_stop=Mock(side_effect=[False, True]))
        self.assertEqual(result, ready)
        skip.assert_called_once()
        step.assert_called_once()

    def test_persistent_no_pause_respects_action_budget(self):
        start = state()
        with patch('tools.enter_save.skip_by_pause_menu', return_value=(False, start, 0, 'no_pause')) as skip, \
             patch('tools.enter_save.step', return_value={'obs': start}), \
             self.assertRaisesRegex(RuntimeError, '3 步'):
            self.enter(start, max_actions=3)
        self.assertEqual(skip.call_count, 3)


if __name__ == '__main__':
    unittest.main()
