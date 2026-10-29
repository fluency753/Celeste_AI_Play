"""Workspace guide publication is complete, content addressed, and fail closed."""
from pathlib import Path
import os
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
        self.cfg = SimpleNamespace(workspace_dir=self.root / 'workspace')
        self.parts = [prompting.PromptPart('a', 'first', 'a.md'),
                      prompting.PromptPart('b', 'second', 'b.md', '\n\n'),
                      prompting.PromptPart('a continued', 'third', 'a.md', '\n')]
        replacement = patch.object(prompt_guide, 'prompt_parts', return_value=self.parts)
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_pure_document_preserves_full_order_and_stable_sections(self):
        guide = prompt_guide.guide_document(self.cfg)
        self.assertFalse(self.cfg.workspace_dir.exists())
        self.assertEqual(''.join(s['text'] for s in guide['sections']), 'first\n\nsecond\nthird')
        self.assertEqual([s['id'] for s in guide['sections']], ['a.md:1', 'b.md:1', 'a.md:2'])
        self.parts[1].text = 'new second'
        updated = prompt_guide.guide_document(self.cfg)
        self.assertNotEqual(guide['version'], updated['version'])
        self.assertEqual(guide['sections'][0]['sha256'], updated['sections'][0]['sha256'])
        self.assertNotEqual(guide['sections'][1]['sha256'], updated['sections'][1]['sha256'])
        self.assertEqual(guide['sections'][2]['sha256'], updated['sections'][2]['sha256'])

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
        with self.assertRaisesRegex(ValueError, '拒绝覆盖'):
            prompt_guide.publish_guide(self.cfg)
        self.assertEqual((destination / 'index.md').read_text(encoding='utf-8'), 'changed by user')

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
            self.assertEqual(guide['sections'][0]['text'], 'custom 40 / 150')


if __name__ == '__main__':
    unittest.main()
