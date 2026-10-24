"""位置与大小保存、跨运行恢复、缩放及屏幕边界。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from overlay.layout import Layout
from overlay.placement import resized_rect


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "layout.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_window_restores_absolute_bounds_and_pin_choice(self):
        layout=Layout(self.path)
        layout.save_window((500,100,420,760),False)
        restored=Layout(self.path)
        self.assertEqual(restored.window_target((0,0,1920,1080)),(500,100,420,760))
        self.assertFalse(restored.topmost)
        self.assertIsNone(restored.custom)

    def test_independent_window_clamps_offscreen_position_without_game(self):
        layout=Layout(self.path)
        layout.save_window((5000,-500,3000,2000),True)
        self.assertEqual(Layout(self.path).window_target((0,0,1920,1080)),(0,0,1920,1080))

    def test_saved_layout_roundtrips_and_tracks_game(self):
        layout = Layout(self.path)
        layout.save((400, 200, 300, 500), (100, 100, 1200, 800))
        restored = Layout(self.path)
        self.assertEqual(restored.target((100, 100, 1200, 800), .182, .02, 1.45), (400, 200, 300, 500))
        self.assertEqual(restored.target((0, 0, 2400, 1600), .182, .02, 1.45), (600, 200, 600, 1000))

    def test_offscreen_layout_is_recovered_without_clipping(self):
        layout = Layout(self.path)
        layout.save((5000, -500, 3000, 2000), (0, 0, 1920, 1080))
        self.assertEqual(layout.target((0, 0, 1920, 1080), .182, .02, 1.45, (0, 0, 1920, 1040)), (0, 0, 1920, 1040))

    def test_reset_and_invalid_data_return_to_default(self):
        layout = Layout(self.path)
        layout.save((400, 200, 300, 500), (0, 0, 1920, 1080))
        layout.reset()
        self.assertIsNone(Layout(self.path).custom)

    def test_borderless_move_and_resize_keep_opposite_edges_fixed(self):
        self.assertEqual(resized_rect((100, 100, 300, 500), 50, -20, "move"), (150, 80, 300, 500))
        self.assertEqual(resized_rect((100, 100, 300, 500), 50, 80, "se"), (100, 100, 350, 580))
        self.assertEqual(resized_rect((100, 100, 300, 500), 500, 500, "nw"), (200, 340, 200, 260))
        self.path.write_text(json.dumps({"mode": "custom", "x": 0, "y": 0, "width": float("nan"), "height": 1}))
        self.assertIsNone(Layout(self.path).custom)


if __name__ == "__main__":
    unittest.main()
