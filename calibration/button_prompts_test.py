"""Editable button prose cannot remove controller provenance or current facts."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import button_prompts, prompting


class ButtonPromptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.overrides = Path(folder.name) / 'prompts'
        replacement = patch.object(prompting, 'OVERRIDES', self.overrides)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_catalog_lists_known_editable_templates_and_returns_independent_records(self):
        catalog = button_prompts.catalog()
        self.assertEqual(len(catalog), 21)
        self.assertEqual(len({item['key'] for item in catalog}), len(catalog))
        for item in catalog:
            self.assertEqual(set(item), {'key', 'name', 'label'})
            self.assertEqual(item['name'], f"button_{item['key']}_zh.md")
            self.assertTrue((prompting.PROMPTS / item['name']).is_file())
            self.assertTrue(button_prompts.notification(item['key']).startswith(f"【主控系统消息｜{item['label']}】"))
        catalog[0]['key'] = 'changed by caller'
        self.assertEqual(button_prompts.catalog()[0]['key'], 'start_play')

    def test_local_override_is_read_on_each_event_without_mutating_repository_template(self):
        template = prompting.PROMPTS / 'button_goal_zh.md'
        original = template.read_bytes()
        self.overrides.mkdir()
        local = self.overrides / template.name
        local.write_text('只整理与新目标有关的笔记。', encoding='utf-8')
        first = button_prompts.notification('goal', '实际目标：到达房间 d0。')
        self.assertIn('只整理与新目标有关的笔记。', first)
        local.write_text('先核对最新观测。', encoding='utf-8')
        second = button_prompts.notification('goal', '实际目标：到达房间 d0。')
        self.assertNotIn('只整理与新目标有关的笔记。', second)
        self.assertIn('先核对最新观测。', second)
        self.assertEqual(template.read_bytes(), original)

    def test_empty_body_preserves_fixed_source_and_facts(self):
        self.overrides.mkdir()
        (self.overrides / 'button_act_limit_zh.md').write_text(' \n', encoding='utf-8')
        self.assertEqual(button_prompts.notification('act_limit', '单次 act 最多 300 帧。'),
                         '【主控系统消息｜更新动作帧数上限】\n单次 act 最多 300 帧。')

    def test_continuation_source_facts_and_non_user_notice_survive_empty_override(self):
        self.overrides.mkdir()
        for key, label in (('continue_play', '自动续轮'), ('continue_prepare', '自动续轮')):
            (self.overrides / f'button_{key}_zh.md').write_text('', encoding='utf-8')
            text = button_prompts.notification(key, '主控事实：任务仍在运行。')
            self.assertEqual(text, f'【主控系统消息｜{label}】\n主控事实：任务仍在运行。\n这不是用户新消息，不改变任务范围、权限或停止条件。')

    def test_defaults_keep_start_and_neutral_continuation_semantics(self):
        self.assertIn('开始游玩。先调用 observe 看清当前画面，然后用 act 推进，直到通关。',
                      button_prompts.notification('start_play'))
        self.assertIn('通关后继续，直到停止目标触发或用户停止。', button_prompts.notification('start_explore'))
        text = button_prompts.notification('continue_play')
        self.assertIn('自行选择下一步操作', text)
        for old in ('还没有通关', '调用 act', '先更新一句公开解说'):
            self.assertNotIn(old, text)

    def test_unknown_key_and_non_text_facts_are_rejected(self):
        for key in ('not_a_button', '../goal', None, []):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, '未知按钮提示词'):
                button_prompts.notification(key)
        with self.assertRaisesRegex(ValueError, '事实须为文字'):
            button_prompts.notification('goal', {'goal': 'd0'})

    def test_broken_local_encoding_falls_back_to_repository_body_with_fixed_warning(self):
        self.overrides.mkdir()
        (self.overrides / 'button_goal_zh.md').write_bytes(b'\xff')
        text = button_prompts.notification('goal', '实际目标已更新：d0。')
        self.assertTrue(text.startswith('【主控系统消息｜更新目标】\n实际目标已更新：d0。'))
        self.assertIn((prompting.PROMPTS / 'button_goal_zh.md').read_text(encoding='utf-8').strip(), text)
        self.assertIn('本机消息模板读取失败，已用默认说明。', text)

    def test_io_failure_does_not_replace_or_drop_already_committed_facts(self):
        with patch.object(prompting, 'read_part', side_effect=OSError('locked template')):
            text = button_prompts.notification('action_permissions', '返回地图=允许。')
        self.assertIn('返回地图=允许。', text)
        self.assertIn('按更新后的操作权限继续当前任务', text)
        self.assertIn('本机消息模板读取失败，已用默认说明。', text)

    def test_unreadable_repository_also_retains_source_facts_and_continuation_notice(self):
        with patch.object(prompting, 'PROMPTS', self.overrides / 'missing-repository'):
            text = button_prompts.notification('continue_play', '任务仍在运行。')
        self.assertTrue(text.startswith('【主控系统消息｜自动续轮】\n任务仍在运行。'))
        self.assertIn('请依据当前状态与配置继续当前任务。', text)
        self.assertIn('本机消息模板读取失败，已用默认说明。', text)
        self.assertTrue(text.endswith('这不是用户新消息，不改变任务范围、权限或停止条件。'))

    def test_unknown_key_never_falls_back_to_another_message(self):
        with patch.object(prompting, 'read_part', side_effect=OSError('should not be read')) as read:
            with self.assertRaisesRegex(ValueError, '未知按钮提示词'):
                button_prompts.notification('unknown')
        read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
