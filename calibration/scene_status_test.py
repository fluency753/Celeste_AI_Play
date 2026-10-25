"""共享内存状态区分桥接连接、章节冻结和菜单输入归属。"""
import struct
import time
import unittest

from overlay import modhud


class SceneStatusTests(unittest.TestCase):
    def status(self, version, frozen=False, owns_input=False):
        buffer = bytearray(modhud.MAPPING_SIZE)
        struct.pack_into('<q', buffer, modhud.OFF_BRIDGE_BEAT, round(time.time() * 1000))
        struct.pack_into('<i', buffer, modhud.OFF_BRIDGE_ACTIVE, 1)
        struct.pack_into('<ii', buffer, modhud.OFF_BRIDGE_FROZEN, int(frozen), int(owns_input))
        buffer[modhud.OFF_BRIDGE_VERSION:modhud.OFF_BRIDGE_VERSION + len(version)] = version.encode()
        return modhud.bridge_status(buffer=buffer)

    def test_connected_menu_is_not_frozen_or_input_owned(self):
        status = self.status('1.0.7')
        self.assertTrue(status['running'] and status['active'])
        self.assertFalse(status['frozen'] or status['input_owned'])

    def test_explicit_menu_job_can_own_input_without_idle_freeze(self):
        status = self.status('1.0.7', owns_input=True)
        self.assertFalse(status['frozen'])
        self.assertTrue(status['input_owned'])

    def test_chapter_and_result_can_remain_frozen(self):
        status = self.status('1.0.7', frozen=True, owns_input=True)
        self.assertTrue(status['frozen'] and status['input_owned'])

    def test_old_bridge_does_not_read_unimplemented_status_fields(self):
        status = self.status('1.0.6')
        self.assertTrue(status['frozen'] and status['input_owned'])


if __name__ == '__main__':
    unittest.main()
