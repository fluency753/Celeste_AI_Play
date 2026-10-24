import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from overlay.typography import load_font, change_font


class FontTests(unittest.TestCase):
    def test_font_scale_persists_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "font.json"
            self.assertEqual(load_font(path), 1)
            self.assertEqual(change_font(1, path), 1.05)
            self.assertEqual(load_font(path), 1.05)
            for _ in range(30):
                change_font(1, path)
            self.assertEqual(load_font(path), 1.8)
            for _ in range(40):
                change_font(-1, path)
            self.assertEqual(load_font(path), .6)


if __name__ == "__main__":
    unittest.main()
