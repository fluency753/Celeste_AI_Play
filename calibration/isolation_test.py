"""Test helpers that delete things must only ever remove what they were meant to."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from calibration.isolation import unlink_links


class UnlinkLinksTests(unittest.TestCase):
    def test_removes_links_without_entering_or_touching_their_targets(self):
        tree, outside = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(tree.cleanup)
        self.addCleanup(outside.cleanup)
        root, target = Path(tree.name), Path(outside.name)
        (target / 'keep.txt').write_text('target', encoding='utf-8')
        (target / 'nested').mkdir()
        (root / 'sub').mkdir()
        (root / 'sub' / 'real.txt').write_text('real', encoding='utf-8')
        try:
            (target / 'nested' / 'link-in-target').symlink_to(target / 'keep.txt')
            self.addCleanup(unlink_links, target)
            (root / 'dir-link').symlink_to(target, target_is_directory=True)
            (root / 'sub' / 'file-link').symlink_to(target / 'keep.txt')
        except OSError as exc:
            self.skipTest(f'symlink unavailable: {exc}')
        links = ['dir-link', 'sub/file-link']
        if os.name == 'nt':
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(root / 'junction'), str(target)],
                           capture_output=True, check=True)
            links.append('junction')
        unlink_links(root)
        for name in links:
            self.assertFalse(os.path.lexists(root / name), name)
        self.assertEqual((root / 'sub' / 'real.txt').read_text(encoding='utf-8'), 'real')
        self.assertEqual((target / 'keep.txt').read_text(encoding='utf-8'), 'target')
        self.assertTrue((target / 'nested' / 'link-in-target').is_symlink())   # Never walked into the target.


if __name__ == '__main__':
    unittest.main()
