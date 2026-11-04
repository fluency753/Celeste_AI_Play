"""Workspace guide publication is complete, content addressed, and never follows links;
agent edits inside a published version are republished instead of blocking startup."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator import prompt_guide, prompting


class PromptGuideTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.cfg = SimpleNamespace(workspace_dir=self.root / 'workspace', engine='codex', provider='codex')
        self.parts = [prompting.PromptPart('a', 'first', 'a.md'),
                      prompting.PromptPart('b', 'second', 'b.md', '\n\n'),
                      prompting.PromptPart('a continued', 'third', 'a.md', '\n')]
        replacement = patch.object(prompt_guide, 'prompt_parts', return_value=self.parts)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.enterContext(patch.object(prompt_guide, 'GUIDE_READ_ROOT', self.root / 'private'))

    def test_pure_document_preserves_full_order_and_stable_sections(self):
        guide = prompt_guide.guide_document(self.cfg)
        self.assertFalse(self.cfg.workspace_dir.exists())
        self.assertEqual(''.join(s['text'] for s in guide['sections']), 'first\n\nsecond\nthird')
        self.assertEqual(guide['documents']['guide.md'], 'first\n\nsecond\nthird')
        self.assertEqual(set(guide['documents']), {'guide.md', 'index.md', 'manifest.json'})
        self.assertEqual([(s['offset'], s['limit']) for s in guide['sections']], [(1, 1), (3, 1), (4, 1)])
        self.assertEqual([s['id'] for s in guide['sections']], ['a.md:1', 'b.md:1', 'a.md:2'])
        self.parts[1].text = 'new second'
        updated = prompt_guide.guide_document(self.cfg)
        self.assertNotEqual(guide['version'], updated['version'])
        self.assertEqual(guide['sections'][0]['sha256'], updated['sections'][0]['sha256'])
        self.assertNotEqual(guide['sections'][1]['sha256'], updated['sections'][1]['sha256'])
        self.assertEqual(guide['sections'][2]['sha256'], updated['sections'][2]['sha256'])

    def test_controller_requests_only_ranges_without_confirmed_current_content(self):
        guide = prompt_guide.publish_guide(self.cfg)
        reading = prompt_guide.GuideReading(self.cfg, guide, 'same-session')
        self.assertIn('一次读取正文', reading.read_plan())
        self.assertFalse(reading.state_path.exists())
        result = {'filePath': guide['path'], 'startLine': 1, 'numLines': 4, 'totalLines': 4}
        reading.note_result(dict(result, filePath=guide['index_path']))
        reading.note_result(dict(result, numLines=5))
        self.assertFalse(reading.state_path.exists())
        reading.note_result(dict(result, numLines=1))
        resumed = prompt_guide.GuideReading(self.cfg, guide, 'same-session')
        self.assertNotIn('offset=1', resumed.read_plan())
        self.assertIn('offset=3，limit=1', resumed.read_plan())
        resumed.note_result(result)
        self.assertIn('无需重读', prompt_guide.GuideReading(self.cfg, guide, 'same-session').read_plan())
        self.assertIn('一次读取正文', prompt_guide.GuideReading(self.cfg, guide, 'new-session').read_plan())

        self.parts[1].text = 'updated\nsecond'
        updated = prompt_guide.publish_guide(self.cfg)
        resumed = prompt_guide.GuideReading(self.cfg, updated, 'same-session')
        self.assertIn('每次调用的 file_path 都是上面的 guide.md', resumed.read_plan())
        self.assertIn('- offset=3，limit=2（b）', resumed.read_plan())
        self.assertNotIn('（a continued）', resumed.read_plan())
        resumed.note_result(dict(result, filePath=updated['path'], totalLines=5, startLine=3, numLines=1))
        self.assertIn('（b）', resumed.read_plan())
        resumed.note_result(dict(result, filePath=updated['path'], totalLines=5, startLine=4, numLines=1))
        self.assertNotIn('（b）', prompt_guide.GuideReading(self.cfg, updated, 'same-session').read_plan())

        removed = self.parts.pop(1)
        reduced = prompt_guide.GuideReading(self.cfg, prompt_guide.publish_guide(self.cfg), 'same-session')
        self.assertIn('当前指南已移除以下章节，旧内容不再适用：b.md:1', reduced.read_plan())
        reduced.confirm_scope()
        self.assertNotIn('已移除', reduced.read_plan())
        self.parts.insert(1, removed)
        restored = prompt_guide.GuideReading(self.cfg, updated, 'same-session')
        self.assertIn('- offset=3，limit=2（b）', restored.read_plan())

    def test_offsets_match_reader_for_adjacent_sections_with_a_shared_line(self):
        from orchestrator.codex_read import _text_result
        self.parts[1].sep = ''
        guide = prompt_guide.guide_document(self.cfg)
        data = guide['documents']['guide.md'].encode('utf-8')
        result = _text_result(guide['path'], data, 1, 1)
        self.assertEqual(result['file']['totalLines'], guide['total_lines'])
        self.assertIn('firstsecond', result['text'])
        self.assertEqual([s['offset'] for s in guide['sections']], [1, 1, 2])

    def test_publish_reuses_only_verified_content_and_preserves_user_files(self):
        self.cfg.workspace_dir.mkdir()
        notes = self.cfg.workspace_dir / 'notes.md'
        notes.write_text('user notes', encoding='utf-8')
        guide = prompt_guide.publish_guide(self.cfg)
        destination = self.cfg.workspace_dir / 'game' / 'instructions' / guide['version']
        for name, text in guide['documents'].items():
            self.assertEqual((destination / name).read_bytes(), text.encode('utf-8'))
        before = (destination / 'index.md').stat().st_mtime_ns
        self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assertEqual((destination / 'index.md').stat().st_mtime_ns, before)
        self.assertEqual(notes.read_text(encoding='utf-8'), 'user notes')
        (destination / 'index.md').write_text('changed by user', encoding='utf-8')
        self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assert_published(destination, guide)
        stale, = self.stale_dirs()
        self.assertEqual((stale / 'index.md').read_text(encoding='utf-8'), 'changed by user')
        self.assertEqual(notes.read_text(encoding='utf-8'), 'user notes')

    def assert_published(self, destination, guide):
        self.assertFalse(destination.is_symlink())
        for name, text in guide['documents'].items():
            self.assertFalse((destination / name).is_symlink(), name)
            self.assertEqual((destination / name).read_bytes(), text.encode('utf-8'), name)

    def stale_dirs(self):
        base = self.cfg.workspace_dir / 'game' / 'instructions'
        return sorted(p for p in base.iterdir() if p.name.startswith('.stale-'))

    def test_files_the_agent_adds_inside_the_guide_are_ignored(self):
        # Claude Code 在每个执行过 Bash 的工作目录里自动创建 .claude/.cc-writes；智能体也可能另存文件。
        guide = prompt_guide.publish_guide(self.cfg)
        destination = self.cfg.workspace_dir / 'game' / 'instructions' / guide['version']
        (destination / '.claude').mkdir()
        (destination / '.claude' / '.cc-writes').write_text('{}', encoding='utf-8')
        (destination / 'scratch.txt').write_text('agent scratch', encoding='utf-8')
        before = (destination / 'index.md').stat().st_mtime_ns
        for _ in range(2):  # 每次启动、重接都能直接复用
            self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assert_published(destination, guide)
        self.assertEqual((destination / 'index.md').stat().st_mtime_ns, before)
        self.assertTrue((destination / '.claude' / '.cc-writes').is_file())
        self.assertEqual((destination / 'scratch.txt').read_text(encoding='utf-8'), 'agent scratch')
        self.assertEqual(self.stale_dirs(), [])

    def test_modified_or_missing_section_is_republished_and_old_copy_kept_aside(self):
        guide = prompt_guide.publish_guide(self.cfg)
        destination = self.cfg.workspace_dir / 'game' / 'instructions' / guide['version']
        section = destination / 'guide.md'
        section.write_text('edited by agent', encoding='utf-8')
        (destination / '.claude').mkdir()
        (destination / '.claude' / '.cc-writes').write_text('{}', encoding='utf-8')
        with patch('builtins.print') as log:
            self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assertIn('重新发布', log.call_args.args[0])
        self.assert_published(destination, guide)
        self.assertFalse((destination / '.claude').exists())
        stale, = self.stale_dirs()
        self.assertEqual((stale / section.name).read_text(encoding='utf-8'), 'edited by agent')
        self.assertTrue((stale / '.claude' / '.cc-writes').is_file())
        (destination / 'manifest.json').unlink()
        self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assert_published(destination, guide)
        self.assertEqual(len(self.stale_dirs()), 2)

    def test_links_inside_the_guide_are_set_aside_without_following_them(self):
        guide = prompt_guide.publish_guide(self.cfg)
        base = self.cfg.workspace_dir / 'game' / 'instructions'
        destination = base / guide['version']
        outside = self.root / 'outside'
        outside.mkdir()
        target = outside / 'index.md'
        target.write_text(guide['documents']['index.md'], encoding='utf-8')
        (destination / 'index.md').unlink()
        try:
            (destination / 'index.md').symlink_to(target)
        except OSError as exc:
            self.skipTest(f'symlink unsupported: {exc}')
        # 内容相同的链接也不算已发布：不能让主控读写链接指向的位置
        self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assert_published(destination, guide)
        shutil.rmtree(destination)
        destination.symlink_to(outside, target_is_directory=True)
        self.assertEqual(prompt_guide.publish_guide(self.cfg), guide)
        self.assert_published(destination, guide)
        self.assertEqual([p.name for p in outside.iterdir()], ['index.md'])
        self.assertEqual(target.read_text(encoding='utf-8'), guide['documents']['index.md'])
        self.assertEqual(len(self.stale_dirs()), 2)
        self.assertTrue(any(p.is_symlink() for p in self.stale_dirs()))

    def test_failed_publication_never_exposes_partial_final_directory(self):
        guide = prompt_guide.guide_document(self.cfg)
        with patch.object(prompt_guide.os, 'rename', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(OSError, 'publish failed'):
                prompt_guide.publish_guide(self.cfg)
        base = self.cfg.workspace_dir / 'game' / 'instructions'
        self.assertFalse((base / guide['version']).exists())
        self.assertEqual(list(base.iterdir()), [])

    def test_reparse_point_in_any_managed_ancestor_is_rejected(self):
        self.cfg.workspace_dir.mkdir()
        target = self.root / 'outside'
        target.mkdir()
        try:
            (self.cfg.workspace_dir / 'game').symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if os.name != 'nt':
                self.skipTest(f'symlink unsupported: {exc}')
            junction = subprocess.run(['cmd', '/c', 'mklink', '/J',
                                      str(self.cfg.workspace_dir / 'game'), str(target)],
                                     capture_output=True)
            self.assertEqual(junction.returncode, 0, junction.stderr)
        with self.assertRaisesRegex(ValueError, '符号链接|重解析点'):
            prompt_guide.publish_guide(self.cfg)
        self.assertEqual(list(target.iterdir()), [])
        # 重接时同样拒绝：不在链接指向的位置发布、移动或删除任何内容
        (target / 'instructions').mkdir()
        (target / 'instructions' / 'keep.txt').write_text('outside', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '符号链接|重解析点'):
            prompt_guide.publish_guide(self.cfg)
        self.assertEqual([p.name for p in (target / 'instructions').iterdir()], ['keep.txt'])

    def test_no_workspace_reader_retains_inline_prompt_without_publication(self):
        self.cfg.engine, self.cfg.runtime = 'claude', 'host'
        self.assertIsNone(prompt_guide.publish_guide(self.cfg))
        self.assertFalse(self.cfg.workspace_dir.exists())

    def test_guide_uses_actual_rendered_parts_including_local_overrides(self):
        cfg = RunConfig(agent='test', save='test', model='test', engine='codex', mode='explore')
        override = self.root / 'overrides'
        override.mkdir()
        (override / 'explore_zh.md').write_text('custom {act_min_frames} / {act_max_frames}', encoding='utf-8')
        with patch.object(prompting, 'OVERRIDES', override), \
                patch.object(prompt_guide, 'prompt_parts', prompting.prompt_parts):
            guide = prompt_guide.guide_document(cfg)
            self.assertEqual(''.join(s['text'] for s in guide['sections']), prompting.play_prompt(cfg))
            self.assertEqual(guide['sections'][0]['text'], 'custom 1 / 600')


if __name__ == '__main__':
    unittest.main()
