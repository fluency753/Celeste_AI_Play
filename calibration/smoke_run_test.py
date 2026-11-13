"""实机冒烟工具的核对部分：用造出来的运行目录核对各项判断；真正的启动只在 Windows 实机上做。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import smoke_run


class SmokeCheckTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.run = self.root / 'runs' / 'smoke' / '20261113-150000'
        self.video = self.run / 'recording' / 'chapter-001'
        self.video.mkdir(parents=True)
        game = self.root / 'workspaces' / 'smoke-explore' / 'game'
        (game / 'instructions' / 'abc123').mkdir(parents=True)
        (game / 'instructions' / 'abc123' / 'guide.md').write_text('guide', encoding='utf-8')
        (game / 'latest_obs.json').write_text('{}', encoding='utf-8')
        (self.video / 'gameplay.mp4').write_bytes(b'v' * 10)
        (self.video / 'frames.csv').write_bytes(b'f' * 4)
        self.summary = {
            'stop_reason': '分段完成', 'completed': True, 'game_released': True, 'end_scene': 'Overworld',
            'start_sid': 'Celeste/0-Intro', 'deaths': 1, 'frames_played': 900,
            'splits': [{'chapter': 'Celeste/0-Intro', 'ticks': 588030000, 'deaths': 1}],
            'recording': {'state': 'finished', 'chapters': [
                {'state': 'finished', 'error': '', 'frames': 900, 'dir': str(self.video),
                 'files': {'gameplay.mp4': 10, 'frames.csv': 4}, 'check': {'rows': 900, 'missing_frames': 0, 'repeats': 0}}]}}
        self.decisions = [{'i': 1, 'stop': 'done'}, {'i': 2, 'stop': 'death'}, {'i': 3, 'stop': 'room'}]
        self.write()
        (self.run / 'run_state.json').write_text(json.dumps({'agent': 'smoke', 'save': 'test', 'save_slot': 7, 'record': True}),
                                                 encoding='utf-8')
        (self.run / 'frames.jsonl').write_text('{"f":1}\n{"f":2}\n', encoding='utf-8')
        (self.run / 'controller.log').write_text('[run] 结束\n', encoding='utf-8')
        (self.run / 'agent_events.jsonl').write_text(
            json.dumps({'tool': 'Read', 'file_path': '/workspace/game/instructions/abc123/guide.md'}) + '\n', encoding='utf-8')
        self.enterContext(patch.object(smoke_run, 'ROOT', self.root))
        self.port = self.enterContext(patch.object(smoke_run, 'port_open', return_value=True))

    def write(self):
        (self.run / 'summary.json').write_text(json.dumps(self.summary), encoding='utf-8')
        (self.run / 'decisions.jsonl').write_text(''.join(json.dumps(d) + '\n' for d in self.decisions) + '{"i": 4, "st',
                                                  encoding='utf-8')          # 末尾没写完的一行不算

    def check(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = smoke_run.check(self.run)
        return code, output.getvalue()

    def test_a_complete_run_passes_every_item(self):
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertNotIn('[失败]', text)
        self.assertNotIn('[未覆盖]', text)
        self.assertIn('3 次动作，2 帧记录', text)
        self.assertIn('第一次死亡之后又执行了 1 次动作', text)
        self.assertIn('录像 chapter-001', text)
        self.assertIn('[smoke] 通过', text)

    def test_each_broken_part_is_named(self):
        cases = {
            '主控没有因异常结束': lambda: self.summary.update(error={'type': 'ConnectionError', 'message': 'bridge'}),
            '运行记录里没有异常堆栈': lambda: (self.run / 'controller.log').write_text(
                'Traceback (most recent call last):\n', encoding='utf-8'),
            '动作与逐帧记录': lambda: self.decisions.clear(),
            '操作指南发布到工作区并被读取': lambda: (self.run / 'agent_events.jsonl').write_text('{}\n', encoding='utf-8'),
            '录像 chapter-001': lambda: (self.video / 'gameplay.mp4').write_bytes(b'short'),
            '游戏进程没有中途退出': lambda: self.summary.update(game_exits=[{'pid': 1}]),
            '收尾后的游戏': lambda: self.summary.update(game_released=False),
            '游戏还开着': lambda: setattr(self.port, 'return_value', False),
        }
        for name, damage in cases.items():
            with self.subTest(part=name):
                self.setUp()
                damage()
                self.write()
                code, text = self.check()
                self.assertEqual(code, 1, text)
                self.assertIn(f'[失败] {name}', text)

    def test_uncovered_items_do_not_fail_and_a_frozen_unfinished_run_is_fine(self):
        self.decisions = [{'i': 1, 'stop': 'done'}, {'i': 2, 'stop': 'room'}, {'i': 3, 'stop': 'done'}]
        self.summary.update(completed=False, game_released=False, end_scene='Level', splits=[], deaths=0,
                            stop_reason='现实时间预算用完')
        self.write()
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertIn('[未覆盖] 死亡后重生并继续', text)
        self.assertIn('保持冻结（可续跑），停在 Level', text)
        self.assertIn('未覆盖：死亡后重生并继续、完成的段', text)

    def test_a_stop_condition_replaces_the_segment_row(self):
        self.summary.update(completed=False, game_released=False, end_scene='Level', splits=[],
                            goal={'text': '进入房间 3 就算完成', 'conditions': [{'field': 'room', 'op': 'eq', 'value': '3'}],
                                  'reached': True})
        self.write()
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertIn('[通过] 到达停止条件', text)
        self.assertNotIn('完成的段', text)
        self.summary['goal']['reached'] = False                 # 模型没打过去：不算主控的失败
        self.write()
        code, text = self.check()
        self.assertEqual(code, 0, text)
        self.assertIn('[未覆盖] 到达停止条件', text)

    def test_missing_summary_and_segment_syntax(self):
        (self.run / 'summary.json').unlink()
        code, text = self.check()
        self.assertEqual(code, 1)
        self.assertIn('没有 summary.json', text)
        self.assertEqual(smoke_run.parse_segments('0.0,1.0-1'), [[0, 0], [1, 0], [1, 1]])
        self.assertEqual(smoke_run.parse_segments('1.0'), [[1, 0]])
        for bad in ('1', '1.x', 'a.0', '1.0-b'):
            with self.assertRaises(ValueError):
                smoke_run.parse_segments(bad)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                smoke_run.main([])          # 启动要写明存档槽位（非 Windows 上则直接说明只能核对）


if __name__ == '__main__':
    unittest.main()
