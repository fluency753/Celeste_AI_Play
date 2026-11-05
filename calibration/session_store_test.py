"""Exercise real Windows file sharing without a running game or controller."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import shutil
from threading import Event
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from orchestrator.session_store import load_json, rename_directory, write_json


@unittest.skipUnless(os.name == 'nt', 'Windows file sharing semantics')
class SessionSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'run_state.json'

    def test_hud_read_finishes_old_snapshot_after_new_one_is_published(self):
        from overlay.server import start_in_thread

        write_json(self.path, {'engine': 'codex', 'save': '旧存档', 'save_slot': 1, 'session_kind': 'ready'})
        opened, published = Event(), Event()
        load = json.load

        def held_read(stream, *args, **kwargs):
            if Path(stream.name) == self.path and not opened.is_set():
                opened.set()
                if not published.wait(5):
                    raise TimeoutError('Writer did not publish while the HUD was reading')
            return load(stream, *args, **kwargs)

        server = start_in_thread(0, pinned=self.path.parent)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def control():
            with urlopen(f'http://127.0.0.1:{server.server_address[1]}/api/control', timeout=10) as response:
                return json.loads(response.read())

        with patch('orchestrator.conversations.ROOT', self.path.parent), \
             patch('orchestrator.session_store.json.load', side_effect=held_read), ThreadPoolExecutor() as pool:
            request = pool.submit(control)
            try:
                self.assertTrue(opened.wait(5))
                write_json(self.path, {'engine': 'codex', 'save': '新存档', 'save_slot': 2, 'session_kind': 'idle'})
            finally:
                published.set()
            old = request.result(timeout=10)
            new = control()
        self.assertEqual((old['save'], old['save_slot'], old['ready_to_play']), ('旧存档', 1, True))
        self.assertEqual((new['save'], new['save_slot'], new['ready_to_play']), ('新存档', 2, False))

    def test_retries_legacy_reader_and_preserves_old_file_if_still_held(self):
        write_json(self.path, {'version': 'old'})
        # Ordinary open does not share delete access on Windows.
        with self.path.open(encoding='utf-8'):
            with self.assertRaises(OSError):
                write_json(self.path, {'version': 'unpublished'})
        self.assertEqual(load_json(self.path), {'version': 'old'})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

        blocked = Event()
        replace = Path.replace

        def signal_conflict(source, target):
            try:
                return replace(source, target)
            except OSError:
                blocked.set()
                raise

        with ThreadPoolExecutor() as pool, patch.object(Path, 'replace', signal_conflict):
            with self.path.open(encoding='utf-8'):
                task = pool.submit(write_json, self.path, {'version': 'new'})
                self.assertTrue(blocked.wait(5))
            task.result(timeout=5)
        self.assertEqual(load_json(self.path), {'version': 'new'})
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_falls_back_if_filesystem_does_not_support_extended_rename(self):
        write_json(self.path, {'version': 'old'})
        blocked = Event()

        def unsupported(source, target):
            blocked.set()
            raise OSError(22, 'extended rename unsupported', source, 87)

        with ThreadPoolExecutor() as pool, patch('orchestrator.session_store.replace_shared', side_effect=unsupported):
            with self.path.open(encoding='utf-8'):
                task = pool.submit(write_json, self.path, {'version': 'new'})
                self.assertTrue(blocked.wait(5))
            task.result(timeout=5)
        self.assertEqual(load_json(self.path), {'version': 'new'})

    def test_missing_snapshot_preserves_filename(self):
        with self.assertRaises(FileNotFoundError) as missing:
            load_json(self.path)
        self.assertEqual(Path(missing.exception.filename), self.path)

    def test_directory_publication_waits_for_a_real_windows_reader(self):
        from orchestrator.windows_state_file import _open, _close, _GENERIC_READ, wintypes

        source = self.path.parent / 'staging'
        target = self.path.parent / 'published'
        source.mkdir()
        (source / 'guide.md').write_text('complete guide', encoding='utf-8')
        blocked = Event()
        rename = os.rename

        def signal_conflict(old, new):
            try:
                return rename(old, new)
            except OSError:
                blocked.set()
                raise

        handle = _open(str(source), _GENERIC_READ, 3, None, 3, 0x02000000, None)
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        with ThreadPoolExecutor() as pool, patch('orchestrator.session_store.os.rename', signal_conflict):
            try:
                task = pool.submit(rename_directory, source, target)
                self.assertTrue(blocked.wait(5))
                self.assertFalse(target.exists())
            finally:
                _close(handle)
            task.result(timeout=5)
        self.assertFalse(source.exists())
        self.assertEqual((target / 'guide.md').read_text(encoding='utf-8'), 'complete guide')

    def test_directory_publication_failure_preserves_source_and_competing_target(self):
        source, target = self.path.parent / 'staging', self.path.parent / 'published'
        source.mkdir()
        (source / 'guide.md').write_text('original', encoding='utf-8')
        denied = OSError(13, 'sharing violation', source, 5)
        with patch('orchestrator.session_store.os.rename', side_effect=denied) as rename, \
             patch('orchestrator.session_store.time.sleep'):
            with self.assertRaises(OSError):
                rename_directory(source, target)
        self.assertEqual(rename.call_count, 7)
        self.assertEqual((source / 'guide.md').read_text(encoding='utf-8'), 'original')
        self.assertFalse(target.exists())
        target.mkdir()
        (target / 'guide.md').write_text('winner', encoding='utf-8')
        with patch('orchestrator.session_store.os.rename', side_effect=denied) as rename:
            with self.assertRaises(OSError):
                rename_directory(source, target)
        self.assertEqual(rename.call_count, 1)
        self.assertEqual((target / 'guide.md').read_text(encoding='utf-8'), 'winner')

    def test_knowledge_publisher_validates_concurrent_directory(self):
        from orchestrator import kb_layout
        from orchestrator.config import RunConfig

        kb = self.path.parent / 'kb'
        for name in ('shared/data.md', 'shared/tools.md', 'explore/README.md', 'speedrun/README.md'):
            path = kb / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture knowledge', encoding='utf-8')
        cfg = RunConfig(agent='fixture', save='', model='', engine='codex', knowledge_profile='explore')
        for complete in (True, False):
            with self.subTest(complete=complete):
                view = self.path.parent / ('complete' if complete else 'incomplete')

                def concurrent_winner(source, target):
                    if complete:
                        shutil.copytree(source, target)
                    else:
                        target.mkdir()
                    raise OSError(13, 'concurrent destination', source, 5)

                with patch('orchestrator.session_store.os.rename', side_effect=concurrent_winner):
                    if complete:
                        self.assertEqual(kb_layout.build(cfg, kb, view), view)
                        self.assertIsNotNone(kb_layout.study_identity(view))
                    else:
                        with self.assertRaisesRegex(ValueError, '不完整'):
                            kb_layout.build(cfg, kb, view)
