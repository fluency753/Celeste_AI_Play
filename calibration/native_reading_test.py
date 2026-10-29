import unittest

from orchestrator.native_reading import ReadIntent, matched_read_lines, parse_read_command


class NativeReadingTest(unittest.TestCase):
    def test_native_paging_intents(self):
        cases = {
            "cat '/kb/file with spaces.md'": ReadIntent('/kb/file with spaces.md'),
            "cat -- -file": ReadIntent('/workspace/-file'),
            "/usr/bin/cat ../kb/input.md": ReadIntent('/kb/input.md'),
            "sed -n '2,4p' /kb/a.md": ReadIntent('/kb/a.md', 2, 4),
            "sed -n '3p' /kb/a.md": ReadIntent('/kb/a.md', 3, 3),
            "sed -n '3,$p' /kb/a.md": ReadIntent('/kb/a.md', 3),
            "head -n2 /kb/a.md": ReadIntent('/kb/a.md', end=2),
            "head -n 2 /kb/a.md": ReadIntent('/kb/a.md', end=2),
            "head --lines=0 /kb/a.md": ReadIntent('/kb/a.md', end=0),
            "tail -n+3 /kb/a.md": ReadIntent('/kb/a.md', start=3),
            "tail -n 2 /kb/a.md": ReadIntent('/kb/a.md', tail=2),
            "head /kb/a.md": ReadIntent('/kb/a.md', end=10),
            "tail /kb/a.md": ReadIntent('/kb/a.md', tail=10),
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(parse_read_command(command), expected)
        self.assertEqual(parse_read_command('cat ../README.md', '/workspace/code').path,
                         '/workspace/README.md')

    def test_unknown_or_shell_execution_keeps_bash(self):
        commands = [
            'cat /kb/a | head', 'cat /kb/a; pwd', 'cat /kb/a && pwd',
            'cat /kb/a > /workspace/out', 'cat < /kb/a', 'cat $(pwd)',
            'cat `pwd`', 'cat "$FILE"', 'cat /kb/*', 'cat ~/a',
            'cat /kb/{a,b}', 'cat /kb/a /kb/b', 'cat -n /kb/a',
            'cat -file', '/workspace/cat /kb/a', 'python -c "print(1)"',
            'env cat /kb/a', 'X=1 cat /kb/a', 'sed -n "1p;1e" /kb/a',
            "sed -n '1w /workspace/x' /kb/a", "sed -n '0,3p' /kb/a",
            "sed -n '4,2p' /kb/a", 'head -c10 /kb/a', 'tail -f /kb/a',
            'tail -n+0 /kb/a', 'cat /kb/a\npwd', 'cat "unterminated',
            'cat -- -', 'head -- -', 'tail -n' + '9' * 5000 + ' /kb/a',
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(parse_read_command(command))

    def test_metadata_queries_do_not_claim_content(self):
        for command in ('pwd', 'pwd -P', 'ls', 'ls -alh /kb', 'ls -- "/kb/a b"'):
            with self.subTest(command=command):
                intent = parse_read_command(command)
                self.assertIsNotNone(intent)
                self.assertFalse(intent.content)
                self.assertEqual(matched_read_lines(intent, 'text\n', 'text\n'), set())
        self.assertIsNone(parse_read_command('ls --ignore=foo /kb'))

    def test_visible_full_rows_only_and_requested_range(self):
        source = 'one\ntwo\nthree\nfour\n'
        intent = parse_read_command("sed -n '2,3p' /kb/a")
        self.assertEqual(matched_read_lines(intent, source, 'one\ntwo\nthree\nfour\n'), {2, 3})
        self.assertEqual(matched_read_lines(intent, source, 'two\nthre'), {2})
        self.assertEqual(matched_read_lines(intent, source, 'two\nthree'), {2})
        self.assertEqual(matched_read_lines(intent, source, '2\ttwo\n3\tthree\n'), set())
        self.assertEqual(matched_read_lines(intent, source, 'two\n', successful=False), set())

    def test_repeats_blank_rows_and_json_cannot_inflate_credit(self):
        intent = ReadIntent('/kb/a')
        source = 'same\nsame\n\nother\n'
        matches = matched_read_lines(intent, source, 'same\n')
        self.assertEqual(len(matches), 1)
        self.assertEqual(matched_read_lines(intent, source, '\n'), set())
        self.assertEqual(matched_read_lines(intent, source, '{"numLines":999,"totalLines":999}\n'), set())
        self.assertEqual(matched_read_lines(intent, source, 'same\\nsame\\n'), set())
        self.assertEqual(matched_read_lines(intent, source, 'same\nsame\n\nother\n'), {1, 2, 3, 4})

    def test_tail_head_and_zero_line_ranges(self):
        source = 'one\ntwo\nthree\nfour\n'
        for command, expected in [('tail -n2 /kb/a', {3, 4}),
                                  ('tail -n+2 /kb/a', {2, 3, 4}),
                                  ('head -n2 /kb/a', {1, 2}),
                                  ('head -n0 /kb/a', set()),
                                  ('tail -n0 /kb/a', set())]:
            with self.subTest(command=command):
                self.assertEqual(matched_read_lines(parse_read_command(command), source, source), expected)

    def test_eof_without_newline_is_complete_only_when_exact(self):
        source = 'first\nlast row'
        intent = ReadIntent('/kb/a')
        self.assertEqual(matched_read_lines(intent, source, source), {1, 2})
        self.assertEqual(matched_read_lines(intent, source, 'first\nlast'), {1})
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a', end=1), source, 'last row'), set())
        self.assertEqual(matched_read_lines(intent, source + '\n', source), {1})

    def test_windows_line_endings_match_without_whitespace_normalization(self):
        intent = ReadIntent('/kb/a')
        self.assertEqual(matched_read_lines(intent, 'one\r\n two \r\n', 'one\n two \n'), {1, 2})
        self.assertEqual(matched_read_lines(intent, 'one\n two \n', 'one\ntwo\n'), {1})

    def test_truncated_duplicate_prefix_does_not_claim_unterminated_eof(self):
        source = 'one\none more\none'
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a'), source, 'one\none'), {1})
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a'), source, source), {1, 2, 3})
        # A separate explicit last-line page proves this is EOF, even though its
        # contents also occur earlier and it has no terminating newline.
        final_page = parse_read_command("sed -n '3p' /kb/a")
        self.assertEqual(matched_read_lines(final_page, source, 'one'), {3})
        self.assertEqual(matched_read_lines(final_page, source, 'on'), set())
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a'), 'first\nlast row',
                                           'first\r\nlast row'), {1, 2})
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a'), 'first\r\nlast row',
                                           'first\nlast row'), {1, 2})
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a'), 'one\none more\none',
                                           'one\r\none'), {1})
        self.assertEqual(matched_read_lines(ReadIntent('/kb/a', start=2), source, 'one'), set())


if __name__ == '__main__':
    unittest.main()
