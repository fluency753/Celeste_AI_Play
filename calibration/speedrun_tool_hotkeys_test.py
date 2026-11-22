"""Speedrun Tool 的快捷键自己读真实键盘与手柄（不看桥接的合成输入），接管期间会被手柄或前台键盘触发，改动受控的关卡
（2026-11-22：冻结期间手柄刷出望远镜，开局冲刺被拉进 Dummy）。桥接 1.22.1 起接管期间停用它们并在握手里报告；
主控开跑前核对，旧桥接或没挂上时提醒。"""
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class HotkeyNoticeTests(unittest.TestCase):
    def test_notice_only_when_hotkeys_can_still_fire(self):
        from orchestrator.bridge_requirements import speedrun_tool_hotkey_notice
        self.assertIsNone(speedrun_tool_hotkey_notice({'speedrun_tool_hotkeys': 'guarded'}))
        self.assertIsNone(speedrun_tool_hotkey_notice({'speedrun_tool_hotkeys': 'absent'}))
        self.assertIn('1.22.1', speedrun_tool_hotkey_notice({}))
        self.assertIn('没能在接管期间停用', speedrun_tool_hotkey_notice({'speedrun_tool_hotkeys': 'unguarded'}))

    def test_preflight_prints_the_notice(self):
        from orchestrator import bridge_requirements
        client = Mock()
        client.call.return_value = {'speedrun_tool_hotkeys': 'unguarded'}
        printed = []
        with patch('tools.bridge_client.BridgeClient', return_value=client), \
                patch.object(bridge_requirements, 'validate_bridge'), \
                patch('orchestrator.action_policy.require_capability'), \
                patch('orchestrator.game_watch.require_live_steam'), \
                patch('builtins.print', lambda *args, **kwargs: printed.append(' '.join(map(str, args)))):
            bridge_requirements.preflight_bridge(47774)
        self.assertTrue(any('Speedrun Tool' in line for line in printed), printed)
        client.close.assert_called_once()


class BridgeGuardSourceTests(unittest.TestCase):
    """桥接这边没有不带游戏的原生检查：核对挂钩的目标、接管期间返回 false、握手字段与装卸顺序（实机验证见更新记录）。"""

    def test_bridge_wraps_the_speedrun_tool_hotkey_gate_while_active(self):
        source = (ROOT / 'game-bridge/Source/SpeedrunToolHotkeys.cs').read_text(encoding='utf-8')
        self.assertIn('"Celeste.Mod.SpeedrunTool.Other.Hotkeys_Rebase"', source)
        self.assertIn('GetMethod("Update"', source)
        self.assertRegex(source, r'bool enabled = orig\(scene\);\s*return enabled && !Bridge\.Active;')
        bridge = (ROOT / 'game-bridge/Source/Bridge.cs').read_text(encoding='utf-8')
        self.assertIn('w.WriteString("speedrun_tool_hotkeys", SpeedrunToolHotkeys.State);', bridge)
        module = (ROOT / 'game-bridge/Source/BridgeModule.cs').read_text(encoding='utf-8')
        self.assertIn('SpeedrunToolHotkeys.Bind();', module)
        self.assertEqual(len(re.findall(r'SpeedrunToolHotkeys\.Unbind\(\);', module)), 2)


if __name__ == '__main__':
    unittest.main()
