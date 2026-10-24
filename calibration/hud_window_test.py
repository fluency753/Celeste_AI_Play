"""Independent HUD window lifecycle; no game-dependent hide or forced pinning."""
import queue
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from overlay.window import manage
from overlay.placement import PlacementAPI


class WindowTests(unittest.TestCase):
    def run_window(self, commands=(), minimized=False):
        user=MagicMock();user.IsWindow.side_effect=[True,True,False];user.IsIconic.return_value=minimized
        window=MagicMock();placement=PlacementAPI();placement._window=window
        for command in commands:placement._commands.put(command)
        args=SimpleNamespace(placement=placement,interval=.25)
        rect=(500,100,420,760)
        layout=MagicMock();layout.topmost=True;layout.window_target.return_value=rect
        with patch('overlay.window._user32',return_value=user),patch('overlay.window.initialize_style',return_value=1), \
             patch('overlay.window.per_pixel_alpha'),patch('overlay.window.Layout',return_value=layout), \
             patch('overlay.window.TrayControl') as tray,patch('overlay.window.client_rect',return_value=rect), \
             patch('overlay.window.window_work_area',return_value=(0,0,1920,1080)),patch('overlay.window.time.sleep'):
            manage(window,args)
        return user,window,placement,layout,tray

    def test_window_never_auto_hides_or_repositions_after_initial_restore(self):
        user,window,_,_,_=self.run_window()
        user.ShowWindow.assert_not_called()
        self.assertEqual(user.SetWindowPos.call_count,1)
        self.assertTrue(user.SetWindowPos.call_args.args[-1]&4) # preserve z-order
        window.minimize.assert_not_called()
        window.restore.assert_not_called()

    def test_user_controls_pin_minimize_restore_and_close(self):
        user,window,placement,layout,tray=self.run_window(['pin','minimize','restore','close'])
        self.assertFalse(window.on_top)
        self.assertFalse(placement._topmost)
        tray.return_value.set_topmost.assert_called_once_with(False)
        window.minimize.assert_not_called()
        window.hide.assert_called_once()
        window.show.assert_called_once()
        window.restore.assert_called_once()
        window.destroy.assert_called_once()
        user.ShowWindow.assert_not_called()

    def test_minimized_geometry_does_not_overwrite_saved_normal_bounds(self):
        _,_,_,layout,_=self.run_window(minimized=True)
        self.assertEqual(layout.save_window.call_count,1)

    def test_hidden_window_does_not_resave_geometry_on_close(self):
        _,window,_,layout,_=self.run_window(['minimize','close'])
        window.hide.assert_called_once()
        window.minimize.assert_not_called()
        self.assertEqual(layout.save_window.call_count,2) # initial restore and explicit hide

    def test_page_bridge_only_accepts_window_commands_when_enabled(self):
        placement=PlacementAPI()
        self.assertFalse(placement.window_action('close'))
        placement._enabled=True
        self.assertFalse(placement.window_action('unknown'))
        self.assertTrue(placement.window_action('minimize'))
        self.assertEqual(placement._commands.get_nowait(),'minimize')


if __name__=='__main__':unittest.main()
