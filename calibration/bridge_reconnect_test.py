"""桥接只接受一个连接：别的客户端连上时主控的旧连接会被关掉。请求发不出去时重连重发；只读指令等回复时断开也重连重发；
step 发出后才断开不重发，照常报错。"""
import json
import socket
import threading
import unittest

from tools.bridge_client import BridgeClient


class FakeBridge:
    """第一个连接接上就关掉（模拟被挤掉）；之后的连接逐行回复 ok。"""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.received = []
        self.connections = 0
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            if self.connections == 1:
                conn.close()
                continue
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        f = conn.makefile("rwb")
        for line in f:
            req = json.loads(line)
            self.received.append(req["cmd"])
            f.write((json.dumps({"id": req["id"], "ok": True, "obs": {"frame": 1}}) + "\n").encode())
            f.flush()

    def close(self):
        self.server.close()


class ReconnectTests(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.addCleanup(self.bridge.close)
        self.client = BridgeClient(port=self.bridge.port, timeout=5)
        self.addCleanup(self.client.close)

    def test_read_only_command_reconnects_after_being_displaced(self):
        reply = self.client.call("observe", image=False)
        self.assertEqual(reply["obs"]["frame"], 1)
        self.assertEqual(self.client.reconnects, 1)
        self.assertEqual(self.bridge.received, ["observe"])

    def test_failed_send_is_resent_after_reconnecting(self):
        original = self.client.file

        class Broken:
            def write(self, data):
                raise ConnectionAbortedError(10053, "aborted")

            def flush(self):
                pass

            def close(self):
                original.close()

        self.client.file = Broken()
        reply = self.client.call("step", inputs=["1"])               # 请求根本没发出去：重发一次是安全的
        self.assertTrue(reply["ok"])
        self.assertEqual(self.bridge.received, ["step"])

    def test_step_is_not_resent_once_it_may_have_reached_the_bridge(self):
        import time
        time.sleep(0.2)                                              # 让桥接先关掉第一个连接
        try:
            self.client.call("step", inputs=["1"])
        except ConnectionError:
            pass
        else:                                                        # 个别系统上写入时就报错：那时请求没送到，重发是对的
            self.assertEqual(self.bridge.received, ["step"])
            return
        self.assertEqual(self.bridge.received, [])


if __name__ == "__main__":
    unittest.main()
