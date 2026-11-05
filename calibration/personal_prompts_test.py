"""Personal text is local, additive, scoped, and saved without losing concurrent edits."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator import personal_prompts as personal


class PersonalPromptTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root/'local/personal_prompts.json'
        replacement = patch.object(personal, 'STORE', self.path)
        replacement.start()
        self.addCleanup(replacement.stop)

    def save(self, scope, text):
        return personal.save(scope, text, personal.read(scope)['version'])

    def test_missing_store_has_empty_defaults_and_reads_do_not_create_files(self):
        cfg = SimpleNamespace(engine='codex', mode='explore')
        self.assertEqual(len(personal.catalog()), 5)
        for item in personal.catalog():
            record = personal.read(item['scope'])
            self.assertEqual(record['text'], '')
            self.assertTrue(record['version'])
        self.assertEqual(personal.selected(cfg), [])
        self.assertEqual(personal.instructions(cfg), '')
        self.assertEqual(personal.fingerprint(cfg), personal.fingerprint(cfg))
        self.assertFalse(self.path.parent.exists())

    def test_common_then_selected_engine_mode_without_other_scope_leakage(self):
        self.save('all', '共同的个人要求。')
        self.save('codex:explore', '验证自己的预测。')
        self.save('claude:explore', '仅 Claude 要求。')
        self.save('codex:speedrun', '仅速通要求。')
        cfg = SimpleNamespace(engine='codex', mode='explore')
        self.assertEqual([x['scope'] for x in personal.selected(cfg)], ['all', 'codex:explore'])
        text = personal.instructions(cfg)
        self.assertLess(text.index('共同的个人要求'), text.index('验证自己的预测'))
        self.assertNotIn('仅 Claude', text)
        self.assertNotIn('仅速通', text)
        self.assertIn('操作权限与实际工具限制', text)
        before = personal.fingerprint(cfg)
        self.save('claude:explore', '别的范围更新。')
        self.assertEqual(personal.fingerprint(cfg), before)
        self.save('codex:explore', '本范围更新。')
        self.assertNotEqual(personal.fingerprint(cfg), before)

    def test_startup_snapshot_reads_once_and_returns_matching_entries_text_and_version(self):
        self.save('all', '共同要求')
        self.save('codex:explore', '专项要求')
        cfg = SimpleNamespace(engine='codex', mode='explore')
        with patch.object(personal, '_load', wraps=personal._load) as load:
            snapshot = personal.snapshot(cfg)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(snapshot['entries'], personal.selected(cfg))
        self.assertEqual(snapshot['text'], personal.instructions(cfg))
        self.assertEqual(snapshot['version'], personal.fingerprint(cfg))

    def test_quoted_rendering_preserves_raw_headings_blank_lines_lists_and_code(self):
        raw = '# 一级标题\n\n## 二级标题\n\n- 条目\n> 已有引用\n```python\nprint("{tool:act}")\n```\n'
        saved = self.save('all', raw)
        before = self.path.read_bytes()
        cfg = SimpleNamespace(engine='codex', mode='explore')
        snapshot = personal.snapshot(cfg)
        quoted = snapshot['text'].split('## 通用（全部执行器与模式）\n\n', 1)[1]
        self.assertTrue(all(line.startswith('>') for line in quoted.split('\n')))
        restored = '\n'.join(line[2:] if line.startswith('> ') else '' for line in quoted.split('\n'))
        self.assertEqual(restored, raw)
        self.assertIn('> > 已有引用', quoted)
        self.assertIn('> ```python\n> print("{tool:act}")\n> ```', quoted)
        self.assertIn('以下引用块是本次需要遵循的个人追加要求', snapshot['text'])
        self.assertEqual(snapshot['entries'][0], saved)
        self.assertEqual(personal.read('all'), saved)
        self.assertEqual(self.path.read_bytes(), before)

    def test_quoted_rendering_covers_bare_carriage_returns_without_rewriting_store(self):
        raw = '# 标题\r正文\r\r结束'
        self.save('all', raw)
        snapshot = personal.snapshot(SimpleNamespace(engine='codex', mode='explore'))
        self.assertIn('> # 标题\n> 正文\n>\n> 结束', snapshot['text'])
        self.assertEqual(personal.read('all')['text'], raw)

    def test_delivery_version_covers_envelope_while_edit_version_stays_raw(self):
        saved = self.save('all', '# 标题')
        cfg = SimpleNamespace(engine='codex', mode='explore')
        original = personal.snapshot(cfg)
        with patch.dict(personal._LABELS, {'all': '新的显示名称'}):
            changed = personal.snapshot(cfg)
        self.assertNotEqual(changed['text'], original['text'])
        self.assertNotEqual(changed['version'], original['version'])
        self.assertEqual(changed['entries'][0]['version'], saved['version'])

    def test_scope_versions_allow_other_scope_changes_but_reject_stale_same_scope(self):
        old_a = personal.read('all')
        old_b = personal.read('codex:explore')
        self.save('all', 'A')
        personal.save('codex:explore', 'B', old_b['version'])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, '已被修改'):
            personal.save('all', 'stale', old_a['version'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(personal.read('codex:explore')['text'], 'B')

    def test_unicode_and_literal_program_text_preserve_content_and_normalize_crlf(self):
        value = '实验要求\r\n{"inputs":["30,R"]}\r\n`x` ${HOME} $(cmd) 😀'
        record = self.save('all', value)
        self.assertEqual(record['text'], value.replace('\r\n', '\n'))
        self.assertEqual(personal.read('all'), record)
        self.assertNotIn(b'\r', self.path.read_bytes())

    def test_reset_empty_only_removes_selected_scope_and_noop_does_not_create_config(self):
        blank = personal.read('all')
        personal.save('all', '', blank['version'])
        self.assertFalse(self.path.exists())
        self.save('all', 'A')
        self.save('codex:explore', 'B')
        self.save('codex:explore', '')
        self.assertEqual(personal.read('all')['text'], 'A')
        self.assertEqual(personal.read('codex:explore')['text'], '')
        self.assertNotIn('codex:explore', json.loads(self.path.read_text(encoding='utf-8'))['scopes'])

    def test_invalid_scope_text_and_version_do_not_write_configuration(self):
        for scope in ('codex', '../all', [], None):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                personal.read(scope)
        for text in (None, 2, 'a'*(personal.MAX_TEXT_CHARS+1), 'abc\x00def'):
            with self.subTest(text_type=type(text)), self.assertRaises(ValueError):
                personal.save('all', text, personal.read('all')['version'])
        with self.assertRaises(ValueError):
            personal.save('all', 'text', None)
        self.assertFalse(self.path.exists())

    def test_bad_or_unknown_configuration_is_never_silently_replaced(self):
        self.path.parent.mkdir()
        invalid = [b'', b'bad json', b'\xff', b'{}', b'[]',
                   b'{"version":true,"scopes":{}}', b'{"version":2,"scopes":{}}',
                   b'{"version":1,"scopes":{"codex":"bad"}}',
                   b'{"version":1,"scopes":{"all":3}}',
                   b'{"version":1,"scopes":{},"unknown":true}',
                   b'{"version":1,"scopes":{"all":"first","all":"second"}}']
        for raw in invalid:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    personal.read('all')
                with self.assertRaises(ValueError):
                    personal.save('all', 'replacement', 'not-a-version')
                self.assertEqual(self.path.read_bytes(), raw)

    def test_replace_failure_keeps_old_file_and_removes_temporary_file(self):
        self.save('all', 'old')
        before = self.path.read_bytes()
        with patch.object(Path, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaisesRegex(OSError, 'replace failed'):
                self.save('all', 'new')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.glob('.personal-prompts-*.tmp')), [])

    def test_symlink_store_is_rejected_without_touching_target(self):
        self.path.parent.mkdir()
        target = self.root/'outside.json'
        target.write_text('{"version":1,"scopes":{}}', encoding='utf-8')
        try:
            self.path.symlink_to(target)
        except OSError as exc:
            self.skipTest(f'File symlink unavailable: {exc}')
        with self.assertRaisesRegex(ValueError, '符号链接|重解析点'):
            personal.read('all')
        with self.assertRaisesRegex(ValueError, '符号链接|重解析点'):
            personal.save('all', 'new', 'version')
        self.assertEqual(target.read_text(encoding='utf-8'), '{"version":1,"scopes":{}}')

    def test_linked_parent_is_rejected_including_windows_junctions(self):
        outside = self.root/'outside'
        outside.mkdir()
        target = outside/'personal_prompts.json'
        original = '{"version":1,"scopes":{}}'
        target.write_text(original, encoding='utf-8')
        link = self.root/'linked'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            if os.name != 'nt':
                self.skipTest(f'Directory symlink unavailable: {exc}')
            created = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(created.returncode, 0, created.stderr)
        with patch.object(personal, 'STORE', link/'personal_prompts.json'):
            with self.assertRaisesRegex(ValueError, '符号链接|重解析点'):
                personal.read('all')
            with self.assertRaisesRegex(ValueError, '符号链接|重解析点'):
                personal.save('all', 'new', 'version')
        self.assertEqual(target.read_text(encoding='utf-8'), original)

    def test_separate_processes_merge_different_scopes_without_losing_data(self):
        script = ('import sys; from pathlib import Path; '
                  'from orchestrator import personal_prompts as p; '
                  'p.STORE=Path(sys.argv[1]); '
                  'p.save(sys.argv[2],sys.argv[3],sys.argv[4])')
        records = [personal.read(scope) for scope in ('all', 'codex:explore')]
        children = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(self.path),
                    item['scope'], text, item['version']], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for item, text in zip(records, ('parallel A', 'parallel B'))]
        for child in children:
            out, error = child.communicate(timeout=20)
            self.assertEqual(child.returncode, 0, (out, error))
        self.assertEqual(personal.read('all')['text'], 'parallel A')
        self.assertEqual(personal.read('codex:explore')['text'], 'parallel B')

    @unittest.skipUnless(os.name == 'nt', 'Windows byte-range locks beyond EOF')
    def test_writer_waits_on_an_empty_lock_instead_of_writing_before_acquisition(self):
        import msvcrt

        self.path.parent.mkdir()
        lock = self.path.with_suffix(self.path.suffix + '.lock')
        ready = self.root/'locking.ready'
        script = '''import msvcrt,sys
from pathlib import Path
from orchestrator import personal_prompts as p
p.STORE=Path(sys.argv[1])
locking=msvcrt.locking
def observed_lock(fd,mode,count):
    if mode==msvcrt.LK_LOCK: Path(sys.argv[2]).touch()
    return locking(fd,mode,count)
msvcrt.locking=observed_lock
p.save('all','saved after release',p.read('all')['version'])
print('saved',flush=True)
'''
        with lock.open('a+b') as stream:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                child = subprocess.Popen([sys._base_executable, '-X', 'utf8', '-c', script,
                                          str(self.path), str(ready)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding='utf-8')

                def reap():
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)

                self.addCleanup(reap)
                deadline = time.monotonic() + 10
                while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.is_file(), 'Writer did not reach the real blocking lock')
                self.assertIsNone(child.poll())
                self.assertFalse(self.path.exists())
                self.assertEqual(lock.stat().st_size, 0)
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            output, error = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, error)
        self.assertEqual(output, 'saved\n')
        self.assertEqual(personal.read('all')['text'], 'saved after release')
        self.assertEqual(lock.stat().st_size, 0)

    def test_separate_processes_cannot_overwrite_stale_same_scope(self):
        script = ('import sys; from pathlib import Path; '
                  'from orchestrator import personal_prompts as p; '
                  'p.STORE=Path(sys.argv[1]); '
                  'p.save("all",sys.argv[2],sys.argv[3])')
        version = personal.read('all')['version']
        children = [subprocess.Popen([sys.executable, '-X', 'utf8', '-c', script, str(self.path), text, version],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE) for text in ('writer A', 'writer B')]
        results = []
        for child in children:
            out, error = child.communicate(timeout=20)
            results.append((child.returncode, error.decode('utf-8')))
        self.assertEqual(sorted(code for code, error in results), [0, 1], results)
        self.assertTrue(any('已被修改' in error for code, error in results if code))
        self.assertIn(personal.read('all')['text'], ('writer A', 'writer B'))


if __name__ == '__main__':
    unittest.main()
