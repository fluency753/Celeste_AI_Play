"""Merged source, capability selection and existing per-tool editing consumers."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import profiles, prompting
from orchestrator.config import RunConfig
from orchestrator.interface_sections import PART_NAMES, parse_interface
from overlay import console_api


class InterfaceSectionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.overrides = Path(temp.name) / 'prompts'
        self.overrides.mkdir()
        self.enterContext(patch.object(prompting, 'OVERRIDES', self.overrides))
        self.enterContext(patch.object(profiles, 'selected', return_value=profiles.LEGACY))
        self.enterContext(profiles.using(profiles.LEGACY))
        self.enterContext(patch.object(console_api, 'orchestrator_status', return_value={'running': False}))
        self.template = prompting.repo_part_text('interface_zh.md')
        self.cfg = RunConfig(agent='interface-test', save='none', model='test', engine='codex',
                             mode='explore', runtime='container', sim=False, sim_assist=False)

    def test_default_alias_baselines_are_sections_of_one_source(self):
        source = prompting.repo_part_path('interface_zh.md')
        for name in PART_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, prompting.prompt_files())
                self.assertEqual(prompting.repo_part_path(name), source)
                doc = console_api.prompt_read(name)
                self.assertEqual(doc['text'], doc['repo_text'])
                self.assertNotIn('<!-- tool:', doc['repo_text'])
                self.assertEqual(doc['repo_text'].count('## '), 1)
                self.assertTrue(doc['source_path'].endswith('shared/interface_zh.md'))

    def test_merged_and_single_section_overrides_have_explicit_precedence(self):
        original = prompting.repo_part_text('tool_observe_zh.md')
        merged = self.template.replace(original, original + '合并文件的观测说明。\n')
        interface = console_api.prompt_read('interface_zh.md')
        console_api.prompt_write('interface_zh.md', merged, interface['version'])
        inherited = console_api.prompt_read('tool_observe_zh.md')
        self.assertIn('合并文件的观测说明', inherited['text'])
        self.assertEqual(inherited['repo_text'], original)
        self.assertEqual(inherited['inherited_from'], 'interface_zh.md')
        self.assertFalse(inherited['overridden'])
        parts = prompting.prompt_parts(self.cfg)
        observed = next(p for p in parts if '合并文件的观测说明' in p.text)
        self.assertEqual(observed.source, 'interface_zh.md')
        console_api.prompt_write('tool_observe_zh.md', '单节优先。', inherited['version'])
        text = prompting.play_prompt(self.cfg)
        self.assertIn('单节优先', text)
        self.assertNotIn('合并文件的观测说明', text)
        self.assertNotIn('<!-- tool:', text)
        restored = console_api.prompt_reset('tool_observe_zh.md')
        self.assertEqual(restored['text'], inherited['text'])
        self.assertFalse((self.overrides / 'tool_observe_zh.md').exists())
        self.assertEqual(prompting.overridden_parts(), ['interface_zh.md'])
        console_api.prompt_reset('interface_zh.md')
        self.assertEqual(prompting.read_part('tool_observe_zh.md'), original)

    def test_old_slot_wrapper_and_its_per_tool_override_still_work(self):
        (self.overrides / 'interface_zh.md').write_text('旧导语。\n{tool_sections}\n旧结尾。', encoding='utf-8')
        (self.overrides / 'tool_act_zh.md').write_text('旧动作覆盖。', encoding='utf-8')
        (self.overrides / 'tool_travel_zh.md').write_text('不开放的章节。', encoding='utf-8')
        text = prompting.play_prompt(self.cfg)
        self.assertIn('旧导语', text)
        self.assertIn('旧结尾', text)
        self.assertIn('旧动作覆盖', text)
        self.assertIn('## celeste_observe：', text)
        self.assertNotIn('不开放的章节', text)
        self.assertNotIn('{tool_sections}', text)

    def test_empty_section_override_and_reset_do_not_change_other_sections(self):
        doc = console_api.prompt_read('tool_act_zh.md')
        saved = console_api.prompt_write('tool_act_zh.md', '', doc['version'])
        self.assertTrue(saved['overridden'])
        self.assertNotIn('## celeste_act：', prompting.play_prompt(self.cfg))
        self.assertIn('## celeste_observe：', prompting.play_prompt(self.cfg))
        restored = console_api.prompt_write('tool_act_zh.md', doc['repo_text'], saved['version'])
        self.assertFalse(restored['overridden'])
        self.assertIn('## celeste_act：', prompting.play_prompt(self.cfg))

    def test_inherited_edit_version_detects_concurrent_merged_change(self):
        doc = console_api.prompt_read('tool_act_zh.md')
        (self.overrides / 'interface_zh.md').write_text(
            self.template.replace(doc['text'], doc['text'] + '并发修改。\n'), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '被改过'):
            console_api.prompt_write('tool_act_zh.md', '旧版本编辑。', doc['version'])
        self.assertFalse((self.overrides / 'tool_act_zh.md').exists())

    def test_invalid_merged_edits_are_rejected_without_writing(self):
        malformed = [
            self.template.replace('<!-- /tool:act -->', ''),
            self.template.replace('<!-- /tool:act -->', '<!-- /tool:observe -->'),
            self.template.replace('<!-- tool:act -->', '<!-- tool:observe -->'),
            self.template.replace('<!-- tool:act -->', '<!-- tool:unknown -->'),
            self.template.replace('<!-- tool:act -->', '前文<!-- tool:act -->'),
            self.template.replace('<!-- tool:act -->', '<!-- tool:act -->尾文'),
            self.template + '\n{tool_sections}',
            self.template.replace('<!-- /tool:act -->', '<!-- tool:act -->'),
        ]
        act = prompting.repo_part_text('tool_act_zh.md')
        malformed.append(self.template.replace('<!-- tool:act -->\n' + act + '<!-- /tool:act -->', ''))
        doc = console_api.prompt_read('interface_zh.md')
        for text in malformed:
            with self.subTest(text=text[:80]):
                with self.assertRaisesRegex(ValueError, 'interface_zh.md'):
                    console_api.prompt_write('interface_zh.md', text, doc['version'])
                self.assertFalse((self.overrides / 'interface_zh.md').exists())
        # The new format can intentionally leave a registered tool's prose empty.
        emptied = self.template.replace(act, '')
        self.assertEqual(parse_interface(emptied)[1]['act'], '')


if __name__ == '__main__':
    unittest.main()
