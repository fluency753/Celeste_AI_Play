"""速通模式整章地图：导出的位置、只在速通模式与本机有模拟器和地图数据时导出、失败不留半个文件、每章每面只试一次。"""
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator import chapter_map
from orchestrator.game_session import GameSession

FAKE = '''#!{python}
import json, sys
args = sys.argv[1:]
if {fail}:
    sys.stderr.write('no map data for ' + args[1])
    sys.exit(1)
out = args[args.index('--out') + 1]
open(out, 'w', encoding='utf-8').write(json.dumps({{'sid': args[1], 'side': args[args.index('--side') + 1], 'rooms': []}}))
open(out + '.calls', 'a', encoding='utf-8').write('x')
'''


class ChapterMapTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cfg = SimpleNamespace(sim_dir=self.root / 'sim' / 'dist', mode='speedrun', knowledge_profile='full')
        self.workspace = self.root / 'workspace'

    def install(self, fail=False, maps=True):
        self.cfg.sim_dir.mkdir(parents=True, exist_ok=True)
        if maps:
            (self.root / 'sim' / 'maps').mkdir(parents=True, exist_ok=True)
        exe = self.cfg.sim_dir / ('celeste-sim.exe' if os.name == 'nt' else 'celeste-sim')
        exe.write_text(FAKE.format(python=sys.executable, fail=fail), encoding='utf-8')
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return exe

    def test_path_replaces_slash_and_appends_side(self):
        self.assertEqual(chapter_map.relative_path('Celeste/1-ForsakenCity', 'B'), 'game/map/Celeste_1-ForsakenCity_B.json')

    def test_only_speedrun_mode_wants_the_map(self):
        self.assertTrue(chapter_map.wanted(self.cfg))
        self.assertFalse(chapter_map.wanted(SimpleNamespace(mode='explore')))

    @unittest.skipIf(os.name == 'nt', '用脚本冒充模拟器只在类 Unix 系统上可行')
    def test_export_writes_once_and_reuses_the_file(self):
        self.install()
        path, error = chapter_map.export(self.cfg, self.workspace, 'Celeste/1-ForsakenCity', 'A')
        self.assertIsNone(error)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['sid'], 'Celeste/1-ForsakenCity')
        again, error = chapter_map.export(self.cfg, self.workspace, 'Celeste/1-ForsakenCity', 'A')
        self.assertEqual((again, error), (path, None))
        self.assertEqual(Path(str(path.with_suffix('.tmp')) + '.calls').read_text(encoding='utf-8'), 'x')

    @unittest.skipIf(os.name == 'nt', '用脚本冒充模拟器只在类 Unix 系统上可行')
    def test_failed_export_leaves_no_file(self):
        self.install(fail=True)
        path, error = chapter_map.export(self.cfg, self.workspace, 'Celeste/9-Core', 'C')
        self.assertIsNone(path)
        self.assertIn('no map data', error)
        self.assertFalse(list((self.workspace / 'game' / 'map').glob('*.json')))
        self.assertFalse(list((self.workspace / 'game' / 'map').glob('*.tmp')))

    def test_missing_simulator_or_maps_is_reported_not_raised(self):
        path, error = chapter_map.export(self.cfg, self.workspace, 'Celeste/1-ForsakenCity', 'A')
        self.assertIsNone(path)
        self.assertIn('sim/maps', error)
        self.install(maps=False)
        self.assertIsNone(chapter_map.exporter(self.cfg))

    def test_session_tries_each_chapter_side_once_and_skips_explore(self):
        session = object.__new__(GameSession)
        session.cfg, session.workspace = self.cfg, self.workspace
        with patch.object(chapter_map, 'export', return_value=(None, 'x')) as export:
            session._write_chapter_map({'sid': 'Celeste/1-ForsakenCity', 'side': 'A'})
            session._write_chapter_map({'sid': 'Celeste/1-ForsakenCity', 'side': 'A'})
            session._write_chapter_map({'sid': 'Celeste/1-ForsakenCity', 'side': 'B'})
            session._write_chapter_map({'sid': None, 'side': 'A'})
            self.assertEqual([c.args[2:] for c in export.call_args_list],
                             [('Celeste/1-ForsakenCity', 'A'), ('Celeste/1-ForsakenCity', 'B')])
            explore = object.__new__(GameSession)
            explore.cfg, explore.workspace = SimpleNamespace(mode='explore', sim_dir=self.cfg.sim_dir), self.workspace
            explore._write_chapter_map({'sid': 'Celeste/1-ForsakenCity', 'side': 'A'})
            self.assertEqual(export.call_count, 2)


if __name__ == '__main__':
    unittest.main()
