"""工具门禁超时的诊断：主控事件循环被占住时，看门狗记下当时的栈与进程 CPU，恢复时记下停了多久；
门禁的第一次请求就在主控的循环上挂好看门狗，处理慢的门禁请求也记一笔。"""
import asyncio
import json
import time
import unittest
from unittest import mock


class FakeLoop:
    def __init__(self):
        self.running, self.closed = True, False

    def is_running(self):
        return self.running

    def is_closed(self):
        return self.closed


class WatchdogCheckTests(unittest.TestCase):
    def make(self):
        from orchestrator.loop_watchdog import LoopWatchdog
        self.now, self.cpu, logged = [100.0], [5.0], []
        dog = LoopWatchdog(FakeLoop(), stall_s=5, log=logged.append, clock=lambda: self.now[0], cpu=lambda: self.cpu[0])
        dog.last, dog.last_cpu = 100.0, 5.0
        return dog, logged

    def test_a_late_heartbeat_logs_the_loop_stack_and_cpu_then_the_recovery(self):
        dog, logged = self.make()
        self.now[0] = 104.0
        self.assertIsNone(dog.check())                     # 4 秒：还不算卡住
        self.now[0], self.cpu[0] = 106.0, 5.5
        first = dog.check()
        self.assertIn('6.0 秒没有响应', first)
        self.now[0], self.cpu[0] = 107.0, 5.6
        self.assertIsNone(dog.check())                     # 同一次卡住不重复刷屏
        self.now[0], self.cpu[0] = 117.0, 8.0
        self.assertIn('17.0 秒没有响应', dog.check())       # 卡得久时隔一段再取一次栈
        dog.last, self.now[0], self.cpu[0] = 130.0, 130.2, 9.0
        recovered = dog.check()
        self.assertIn('恢复：停了约 30.0 秒', recovered)
        self.assertIn('4.0 秒 CPU', recovered)
        self.assertEqual(len(logged), 3)
        self.now[0] = 131.0
        self.assertIsNone(dog.check())

    def test_a_stopped_loop_is_not_reported_as_a_stall(self):
        dog, logged = self.make()
        dog.loop.running = False
        self.now[0] = 200.0
        self.assertIsNone(dog.check())
        self.assertEqual(logged, [])


class WatchdogOnLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_blocking_the_loop_logs_the_blocking_code(self):
        from orchestrator import loop_watchdog
        logged = []
        loop = asyncio.get_running_loop()
        with mock.patch.object(loop_watchdog, 'CHECK_S', 0.05):
            dog = loop_watchdog.LoopWatchdog(loop, stall_s=0.3, beat_s=0.05, log=logged.append)
            dog.start()
            try:
                await asyncio.sleep(0.2)

                def hold_the_loop_like_sync_bridge_call():
                    time.sleep(0.8)

                hold_the_loop_like_sync_bridge_call()
                await asyncio.sleep(0.3)
            finally:
                dog.stop()
        text = '\n'.join(logged)
        self.assertIn('没有响应', text)
        self.assertIn('hold_the_loop_like_sync_bridge_call', text)
        self.assertIn('恢复', text)


class GateStartsWatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def post(self, port, body):
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        try:
            writer.write((f'POST /gate HTTP/1.1\r\nHost: host.docker.internal\r\nContent-Type: application/json\r\n'
                          f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n').encode() + body)
            await writer.drain()
            raw = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            await writer.wait_closed()
        return json.loads(raw.partition(b'\r\n\r\n')[2])

    async def test_first_gate_request_starts_a_watchdog_and_slow_requests_are_logged(self):
        from starlette.applications import Starlette
        from orchestrator import loop_watchdog, tool_gate
        from orchestrator.http_service import http_service

        class Session:
            @staticmethod
            def workspace_hook_guard(data):
                time.sleep(0.05)
                return None

        app = Starlette()
        tool_gate.add_gate_route(app, session=Session)
        loop = asyncio.get_running_loop()
        self.addCleanup(loop_watchdog.stop_watchdog, loop)
        printed = []
        with mock.patch.object(tool_gate, 'SLOW_GATE_S', 0.01), \
                mock.patch('builtins.print', lambda *args, **kwargs: printed.append(' '.join(map(str, args)))):
            async with http_service(app, port=0) as service:
                reply = await self.post(service.actual_port, b'{"tool_name":"Bash"}')
        self.assertEqual(reply['decision'], 'allow')
        self.assertIn(id(loop), loop_watchdog._watchdogs)
        self.assertTrue(any('工具门禁处理 Bash 用了' in line for line in printed), printed)


if __name__ == '__main__':
    unittest.main()
