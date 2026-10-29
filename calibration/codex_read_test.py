"""The read worker's format, budget and sandbox contracts, without user services."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator import codex_read as reader


def image_bytes(format='PNG', size=(32, 24)):
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', size, 'red').save(output, format=format)
    return output.getvalue()


class ReadWorkerTests(unittest.TestCase):
    def read(self, data, **arguments):
        with patch.object(reader, '_read_bytes', return_value=data) as opened:
            result = reader._read({'file_path': '/workspace/test.txt', **arguments})
        opened.assert_called_once_with(result['path'])
        return result

    def test_text_alias_paging_line_receipt_and_crlf(self):
        result = self.read(b'one\r\ntwo\r\nthree', file_path='/kb/README.md',
                           path='/kb/README.md', offset=2, limit=1)
        self.assertEqual(result['text'], '     2\ttwo\n')
        self.assertEqual(result['file'], {'filePath': '/kb/README.md', 'startLine': 2,
                                         'numLines': 1, 'totalLines': 3})
        self.assertTrue(result['truncated'])
        self.assertEqual(result['next_offset'], 3)
        self.assertEqual(result['images'], [])

    def test_default_2000_lines_and_relative_paths(self):
        result = self.read(('row\n' * 2001).encode(), file_path='notes.txt')
        self.assertEqual(result['path'], '/workspace/notes.txt')
        self.assertEqual(result['file']['numLines'], 2000)
        self.assertEqual(result['next_offset'], 2001)

    def test_readonly_roots_alias_mismatch_and_types_reject_before_open(self):
        for arguments in ({'file_path': '/home/agent/.codex/auth.json'},
                          {'file_path': '/workspace/../home/agent/.codex/auth.json'},
                          {'file_path': '/workspace/a', 'path': '/kb/a'},
                          {'path': '/kb/a', 'offset': True},
                          {'path': '/kb/a', 'limit': 0},
                          {'path': '/kb/a', 'offset': '1'},
                          {'path': '/kb/a', 'limit': 20001}):
            with self.subTest(arguments=arguments), patch.object(reader, '_read_bytes') as opened:
                with self.assertRaises(ValueError):
                    reader._read(arguments)
                opened.assert_not_called()

    def test_supported_text_encodings_and_binary_rejection(self):
        for data in ('中文\nnext'.encode('utf-8-sig'), '中文\nnext'.encode('utf-16')):
            self.assertEqual(self.read(data)['text'], '     1\t中文\n     2\tnext\n')
        for data in (b'hello\x00world', b'\xffbad'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.read(data)

    def test_control_characters_stay_visible_and_kb_rows_match_host_totals(self):
        from orchestrator.kb_study import KbStudy
        source = 'esc \x1b[31mred\x1b[0m\nvt\x0bsplit\nfs\x1cgs\x1drs\x1eend\nnel\x85ls\u2028ff\x0cbell\x07\r\nlast'
        with tempfile.TemporaryDirectory() as folder:
            kb = Path(folder)
            (kb / 'README.md').write_bytes(source.encode('utf-8'))
            study = KbStudy(kb, 10)
            result = self.read(source.encode('utf-8'), file_path='/kb/README.md')
            self.assertEqual(result['file']['totalLines'], study.total['README.md'])
            self.assertEqual(result['file']['numLines'], study.total['README.md'])
            self.assertIn('esc \\x1b[31mred\\x1b[0m', result['text'])
            self.assertIn('bell\\x07', result['text'])
            self.assertFalse(any(ord(char) < 32 and char not in '\t\n' for char in result['text']))
            self.assertEqual(result['text'].count('\n'), study.total['README.md'])
            self.assertTrue(study.note_result({'tool_use_result': {'file': result['file']}}))
            self.assertTrue(study.complete())

    def test_empty_and_past_eof_do_not_invent_kb_lines(self):
        empty = self.read(b'')
        self.assertEqual(empty['file']['totalLines'], 0)
        self.assertEqual(empty['file']['numLines'], 0)
        self.assertFalse(empty['truncated'])
        self.assertIn('内容为空', empty['notice'])
        past = self.read(b'one\n', offset=4)
        self.assertEqual(past['file']['numLines'], 0)
        self.assertIn('超过文件末尾', past['notice'])

    def test_char_budget_counts_line_numbers_and_keeps_only_complete_rows(self):
        line = 'a' * 49992  # 8 prefix/end characters => exactly 50000 per row.
        result = self.read((line + '\n' + line + '\nthird\n').encode())
        self.assertEqual(len(result['text']), reader.MAX_CHARS)
        self.assertEqual(result['file']['numLines'], 2)
        self.assertEqual(result['next_offset'], 3)
        huge = self.read(('a' * reader.MAX_CHARS + '\n').encode())
        self.assertEqual(len(huge['text']), reader.MAX_CHARS)
        self.assertEqual(huge['file']['numLines'], 0)
        self.assertTrue(huge['truncated'])
        self.assertIsNone(huge['next_offset'])
        self.assertIn('单行过长', huge['error'])

    def test_supported_images_are_real_decodable_payloads_without_kb_receipts(self):
        from PIL import Image
        for format in ('PNG', 'JPEG', 'GIF', 'WEBP'):
            with self.subTest(format=format):
                result = self.read(image_bytes(format), file_path='/workspace/picture')
                self.assertEqual(result['kind'], 'image')
                self.assertNotIn('file', result)
                self.assertEqual(len(result['images']), 1)
                payload = result['images'][0]
                data = base64.b64decode(payload['data'], validate=True)
                self.assertLessEqual(len(data), reader.MAX_IMAGE_BYTES)
                with Image.open(io.BytesIO(data)) as rendered:
                    rendered.load()
                    self.assertEqual(rendered.getpixel((0, 0))[0] > 200, True)
                    self.assertLessEqual(max(rendered.size), 1280)
                self.assertNotIn(payload['data'], result['text'])

    def test_notebook_cell_paging_text_and_images_are_not_kb_receipts(self):
        png = base64.b64encode(image_bytes()).decode('ascii')
        document = {'cells': [
            {'cell_type': 'markdown', 'source': ['# title\n']},
            {'cell_type': 'code', 'source': ['print(1)\n'], 'outputs': [
                {'output_type': 'stream', 'text': ['1\n']},
                {'output_type': 'display_data', 'data': {'text/plain': 'red plot', 'image/png': png}}]},
            {'cell_type': 'code', 'source': 'later'}]}
        result = self.read(json.dumps(document).encode(), file_path='/workspace/test.ipynb', offset=2, limit=1)
        self.assertEqual(result['kind'], 'notebook')
        self.assertNotIn('file', result)
        self.assertIn('print(1)\n', result['text'])
        self.assertIn('red plot', result['text'])
        self.assertNotIn('# title', result['text'])
        self.assertEqual(len(result['images']), 1)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['next_offset'], 3)

    def test_pdf_page_bounds_and_real_page_consumers(self):
        commands = []
        def poppler(command):
            commands.append(command)
            if command[0].endswith('pdfinfo'):
                return b'Pages: 12\n'
            if command[0].endswith('pdftotext'):
                return b'page content\n'
            return image_bytes()
        with patch.object(reader, '_poppler', side_effect=poppler):
            result = self.read(b'%PDF-1.4', file_path='/workspace/test.pdf', pages='2-3')
        self.assertEqual(result['kind'], 'pdf')
        self.assertEqual(result['pages'], [2, 3])
        self.assertEqual(len(result['images']), 2)
        self.assertNotIn('file', result)
        self.assertTrue(any(command[0] == '/usr/bin/pdftotext' for command in commands))
        self.assertTrue(any(command[0] == '/usr/bin/pdftoppm' for command in commands))
        self.assertEqual(reader._pages(None, 12), [1, 2, 3, 4, 5])
        for pages in ('0', '1-21', '4-2', '13', '1;echo x', '1,2,3-24'):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                reader._pages(pages, 24 if pages != '13' else 12)

    def test_multimodal_budget_bounds_images_and_notebook_text(self):
        item = reader._Presentation('image', '/workspace/a')
        with patch.object(reader, 'MAX_IMAGES_BYTES', 1):
            self.assertFalse(item.image(image_bytes()))
        self.assertTrue(item.result['truncated'])
        self.assertEqual(item.result['images'], [])
        document = {'cells': [{'source': 'a' * (reader.MAX_CHARS + 10)}]}
        result = self.read(json.dumps(document).encode(), file_path='/workspace/large.ipynb')
        self.assertEqual(len(result['text']), reader.MAX_CHARS)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['next_offset'], 1)

    def test_non_pdf_pages_reject_and_partial_pdf_pages_report_actual_images(self):
        with self.assertRaisesRegex(ValueError, 'pages 仅用于 PDF'):
            self.read(image_bytes(), file_path='/workspace/a.png', pages='1')
        def poppler(command):
            if command[0].endswith('pdfinfo'):
                return b'Pages: 3\n'
            if command[0].endswith('pdftotext'):
                return b'page\n'
            return image_bytes()
        with patch.object(reader, '_poppler', side_effect=poppler), patch.object(reader, 'MAX_IMAGES', 1):
            result = self.read(b'%PDF-1.4', file_path='/workspace/a.pdf', pages='1-3')
        self.assertEqual(result['requested_pages'], [1, 2, 3])
        self.assertEqual(result['pages'], [1])
        self.assertEqual(result['text_pages'], [1, 2])
        self.assertEqual(len(result['images']), 1)
        self.assertTrue(result['truncated'])
        self.assertIn('第 2 页及后续页图像未返回', result['notice'])

    def test_host_transport_reuses_sandbox_without_shell_and_passes_json_stdin(self):
        from orchestrator.program_sandbox import bwrap_args
        for call, operation in ((lambda: reader.read_file('isolated', {'file_path': '/kb/a'}), 'read'),
                                (lambda: reader.search_files('isolated', 'grep', {'pattern': 'x'}), 'grep')):
            with self.subTest(operation=operation), patch('orchestrator.codex_container._run', return_value=SimpleNamespace(
                    returncode=0, stdout='{"kind":"text"}', stderr='')) as run:
                self.assertEqual(call(), {'kind': 'text'})
            args, kwargs = run.call_args
            self.assertEqual(args[0], 'isolated')
            prefix = args.index('/usr/bin/bwrap')
            self.assertEqual(list(args[prefix:prefix + len(bwrap_args())]), bwrap_args())
            self.assertIn('--unshare-net', args)
            self.assertIn('--clearenv', args)
            self.assertEqual(args[-4:-1], ('/opt/py/bin/python', '-I', '-c'))
            request = json.loads(kwargs['input'])
            self.assertEqual(request['payload']['operation'], operation)
            self.assertEqual(request['source'], Path(reader.__file__).read_text(encoding='utf-8'))
            # Only the short bootstrap is a command-line argument (Windows 32767-character limit).
            self.assertEqual(args[-1], reader.BOOTSTRAP)
            self.assertLess(len(subprocess.list2cmdline(args)), 4000)
            self.assertNotIn('shell', kwargs)
        with self.assertRaises(ValueError):
            reader.search_files('isolated', 'read', {'pattern': 'x'})

    def test_worker_errors_are_structured_without_a_line_receipt(self):
        process = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', Path(reader.__file__).read_text(encoding='utf-8')],
            input=json.dumps({'operation': 'read', 'arguments': {'file_path': '/home/agent/.codex/auth.json'}}),
            text=True, encoding='utf-8', capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['kind'], 'error')
        self.assertIn('只允许', result['text'])
        self.assertNotIn('file', result)
        # The transported form: bootstrap argument plus source and request on stdin.
        process = subprocess.run([sys.executable, '-I', '-X', 'utf8', '-c', reader.BOOTSTRAP],
            input=json.dumps({'source': Path(reader.__file__).read_text(encoding='utf-8'),
                              'payload': {'operation': 'grep', 'arguments': {'pattern': 'x', 'path': '/etc'}}}),
            text=True, encoding='utf-8', capture_output=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['kind'], 'error')
        self.assertIn('只允许', result['error'])


@unittest.skipUnless(sys.platform == 'linux', 'requires Linux dirfd/O_NOFOLLOW and /proc')
class ReadPathSafetyTests(unittest.TestCase):
    def test_real_files_symlink_components_hardlinks_and_fifo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'ordinary'
            target.write_bytes(b'safe')
            outside = root / 'outside'
            outside.write_bytes(b'secret')
            (root / 'link').symlink_to(outside)
            (root / 'directory_link').symlink_to(root, target_is_directory=True)
            os.link(outside, root / 'hardlink')
            os.mkfifo(root / 'fifo')
            with patch.object(reader, 'ROOTS', (str(root),)):
                self.assertEqual(reader._read_bytes(str(target)), b'safe')
                for path in (root, root / 'link', root / 'directory_link' / 'ordinary',
                             root / 'hardlink', root / 'fifo'):
                    with self.subTest(path=path), self.assertRaises((OSError, ValueError)):
                        reader._read_bytes(str(path))

    def layout(self, folder):
        root = Path(folder)
        (root / 'game' / 'deep').mkdir(parents=True)
        (root / 'notes.md').write_text('wallbounce here\nother\n', encoding='utf-8')
        (root / 'game' / 'plan.json').write_text('{"route": "wallbounce"}\n', encoding='utf-8')
        (root / 'game' / 'deep' / 'tool.py').write_text('a = 1\n# WallBounce helper\nb = 2\n', encoding='utf-8')
        (root / 'game' / 'blob.bin').write_bytes(b'wallbounce\x00\x01')
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(outside, ignore_errors=True))
        (outside / 'secret.md').write_text('wallbounce secret\n', encoding='utf-8')
        (root / 'escape').symlink_to(outside, target_is_directory=True)
        (root / 'escape.md').symlink_to(outside / 'secret.md')
        os.link(outside / 'secret.md', root / 'linked.md')
        return root

    def test_directory_listing_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.layout(folder)
            with patch.object(reader, 'ROOTS', (str(root),)):
                result = reader._read({'file_path': str(root)})
                self.assertEqual(result['kind'], 'directory')
                self.assertNotIn('file', result)
                self.assertIn('game/', result['text'])
                self.assertIn('escape  （符号链接，不跟随）', result['text'])
                page = reader._read({'file_path': str(root), 'offset': 2, 'limit': 1})
                self.assertEqual(page['next_offset'], 3)
                for path in (root / 'escape', root / 'escape' / 'secret.md'):
                    with self.subTest(path=path):
                        self.assertEqual(json.loads(json.dumps(self.worker(root, 'read', {'file_path': str(path)})))['kind'],
                                         'error')

    def worker(self, root, operation, arguments):
        with patch.object(reader, 'ROOTS', (str(root),)):
            try:
                return reader._dispatch({'operation': operation, 'arguments': arguments})
            except (OSError, ValueError) as error:
                return {'kind': 'error', 'error': str(error)}

    def test_glob_matches_within_roots_without_following_links(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.layout(folder)
            found = self.worker(root, 'glob', {'pattern': '**/*.md', 'path': str(root)})
            self.assertEqual(found['kind'], 'glob')
            self.assertIn(f'{root}/notes.md', found['text'])
            self.assertNotIn('secret', found['text'])
            self.assertIn('符号链接', found['notice'])
            nested = self.worker(root, 'glob', {'pattern': f'{root}/game/**/*.{{py,json}}'})
            self.assertEqual(nested['matches'], 2)
            self.assertIn('tool.py', nested['text'])
            self.assertEqual(self.worker(root, 'glob', {'pattern': 'game/*.py', 'path': str(root)})['matches'], 0)

    def test_grep_modes_glob_filter_binary_and_link_skips(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.layout(folder)
            files = self.worker(root, 'grep', {'pattern': 'wallbounce', 'path': str(root)})
            self.assertEqual(files['kind'], 'grep')
            self.assertIn(f'{root}/notes.md', files['text'])
            self.assertIn(f'{root}/game/plan.json', files['text'])
            self.assertNotIn('blob.bin', files['text'])
            self.assertNotIn('secret', files['text'])
            self.assertIn('二进制', files['notice'])
            self.assertIn('硬链接', files['notice'])
            content = self.worker(root, 'grep', {'pattern': 'wallbounce', 'path': str(root), 'output_mode': 'content',
                                                 'ignore_case': True, 'glob': '*.py', 'context': 1})
            self.assertIn(f'{root}/game/deep/tool.py:2:# WallBounce helper', content['text'])
            self.assertIn(f'{root}/game/deep/tool.py-1-a = 1', content['text'])
            count = self.worker(root, 'grep', {'pattern': 'wall', 'path': str(root / 'game' / 'plan.json'),
                                               'output_mode': 'count'})
            self.assertIn(f'{root}/game/plan.json:1', count['text'])
            paged = self.worker(root, 'grep', {'pattern': 'w', 'path': str(root), 'limit': 1})
            self.assertEqual(paged['next_offset'], 2)
            for arguments in ({'pattern': '('}, {'pattern': 'x', 'output_mode': 'all'},
                              {'pattern': 'x', 'path': '/etc'}, {'pattern': 'x', 'context': 99}):
                with self.subTest(arguments=arguments):
                    self.assertEqual(self.worker(root, 'grep', {'path': str(root), **arguments})['kind'], 'error')


if __name__ == '__main__':
    unittest.main()
