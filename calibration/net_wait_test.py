"""模型连不上（断网、接口不可用）时主控暂停智能体、等网络恢复后接着同一个会话：不连接游戏或模型。
用一个假的 Claude Code 子进程（stdio 流式 JSON）重放 api_retry 连接错误与恢复后的正常回复。"""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator.agent import ClaudeRunner, network_error
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.prompting import net_resume_prompt

# 第一个进程：收到第一条消息后报告连接错误重试，然后挂着不回（像断网时那样）；
# 之后的进程（--resume）：正常回复。用 argv[1] 的标记文件区分。
STUB = r'''import sys, json, time, os, uuid
def emit(v): print(json.dumps(v, ensure_ascii=False), flush=True)
mark = sys.argv[1]
first = not os.path.exists(mark)
open(mark, "a").close()
for line in sys.stdin:
    p = json.loads(line)
    if p.get("type") == "control_request":
        emit({"type": "control_response", "response": {"request_id": p["request_id"], "subtype": "success"}}); continue
    text = p["message"]["content"]
    with open(mark, "a", encoding="utf-8") as f:
        f.write(json.dumps(text, ensure_ascii=False) + "\n")
    emit({"type": "system", "subtype": "init", "model": "claude-stub", "tools": [], "mcp_servers": []})
    if first:
        for attempt in range(1, 7):
            emit({"type": "system", "subtype": "api_retry", "attempt": attempt, "max_retries": 10,
                  "retry_delay_ms": 10, "error_status": None, "error": "Connection error."})
            time.sleep(0.05)
        time.sleep(30)        # 不再输出：等主控结束进程
        continue
    emit({"type": "assistant", "message": {"id": str(uuid.uuid4()), "model": "claude-stub",
          "content": [{"type": "text", "text": "已恢复，接着处理。"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "已恢复", "usage": {"output_tokens": 3}})
'''


class ClassificationTests(unittest.TestCase):
    def test_network_errors_are_told_apart_from_auth_and_limits(self):
        self.assertTrue(network_error(None, "Connection error."))
        self.assertTrue(network_error(None, ""))
        self.assertTrue(network_error(529, "overloaded_error"))
        self.assertTrue(network_error(503, "Service Unavailable"))
        self.assertTrue(network_error(504, ""))
        self.assertFalse(network_error(401, "authentication_error"))
        self.assertFalse(network_error(403, "forbidden"))
        self.assertFalse(network_error(429, "rate_limit_error"))
        self.assertFalse(network_error(None, "Claude AI usage limit reached|1792766400"))
        self.assertFalse(network_error(400, "invalid_request_error"))

    def test_resume_prompt_says_the_game_stayed_frozen(self):
        text = net_resume_prompt(12)
        self.assertIn("约 12 分钟", text)
        self.assertIn("游戏一直冻结", text)
        self.assertIn("思考僵直", text)
        self.assertIn("observe", text)
        self.assertIn("用户刚才的消息", net_resume_prompt(3, chat=True))


class MetricsTests(unittest.TestCase):
    def test_backdated_pause_takes_the_retry_time_out_of_thinking_and_real_time(self):
        with tempfile.TemporaryDirectory() as folder:
            m = Metrics(Path(folder) / "metrics.json", "t", "opus", "high", 600)
            m.start_play()
            m.turn_begin()
            time.sleep(0.3)
            since = time.monotonic()
            time.sleep(0.3)                    # 这段是"连不上、Claude Code 在重试"
            m.pause(status="net_wait", since=since)
            self.assertEqual(m.state.status, "net_wait")
            think = m.state.thinking_s
            self.assertLess(think, 0.45)       # 只剩连不上之前的约 0.3 秒
            self.assertGreater(think, 0.15)
            elapsed = m.elapsed()
            time.sleep(0.2)
            self.assertAlmostEqual(m.elapsed(), elapsed, delta=0.05)   # 暂停中不走表
            waited = m.resume(net=True)
            self.assertGreater(waited, 0.45)
            self.assertEqual(m.state.net_waits, 1)
            self.assertGreater(m.state.net_wait_s, 0.45)
            self.assertEqual(m.state.quota_waits, 0)
            self.assertEqual(m.state.waited_s, 0)


class NetWaitIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_errors_pause_then_resume_same_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stub = root / "stub.py"
            stub.write_text(STUB, encoding="utf-8")
            mark = root / "mark.txt"
            cfg = RunConfig(agent="net-test", save="test", model="opus", runtime="host")
            cfg.net_retry_attempts, cfg.net_probe_s, cfg.net_wait_cap_s = 4, 0.05, 5
            metrics = Metrics(root / "metrics.json", "net-test", "opus", "high", 600)
            metrics.start_play()
            session = SimpleNamespace(done=False, done_reason="")
            runner = ClaudeRunner(cfg, root, session, metrics, "", resume_note="第一条消息")
            probes = []

            async def probe(self):
                probes.append(time.monotonic())
                return len(probes) >= 3        # 前两次连不上，之后恢复

            commands = []

            def command(self):
                commands.append(list(self._common_args()))
                return [sys.executable, "-X", "utf8", "-u", str(stub), str(mark)]

            stopped_after = []

            async def next_turn(self, fallback, orig=ClaudeRunner._next_user_turn):
                # 第二个进程回复之后就结束本次运行
                if mark.exists() and len(mark.read_text(encoding="utf-8").splitlines()) >= 2 and self.turns >= 2:
                    stopped_after.append(self.turns)
                    self.stop_reason = "测试结束"
                    return False
                return await orig(self, fallback)

            with patch.object(ClaudeRunner, "_command", command), \
                 patch.object(ClaudeRunner, "_probe_network", probe), \
                 patch.object(ClaudeRunner, "_next_user_turn", next_turn):
                reason = await asyncio.wait_for(runner.run(), 30)
            self.assertEqual(reason, "测试结束")
            self.assertEqual(len(commands), 2, "network wait should restart Claude Code once")
            self.assertIn("--resume", commands[1])
            self.assertEqual(commands[1][commands[1].index("--resume") + 1], runner.session_id)
            sent = [json.loads(line) for line in mark.read_text(encoding="utf-8").splitlines() if line.startswith('"')]
            self.assertEqual(sent[0], "第一条消息")
            self.assertIn("模型连不上", sent[1])
            self.assertIn("游戏一直冻结", sent[1])
            self.assertGreaterEqual(len(probes), 3)
            self.assertEqual(metrics.state.net_waits, 1)
            self.assertGreater(metrics.state.net_wait_s, 0)
            self.assertEqual(metrics.state.quota_waits, 0)
            events = (root / "public_events.jsonl").read_text(encoding="utf-8")
            self.assertIn("主控暂停了智能体", events)
            self.assertIn("模型接口已恢复", events)
            self.assertFalse(runner.net_outage)
            self.assertIsNone(runner.net_down_since)

    async def test_disabled_net_wait_leaves_retries_to_claude_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent="net-test", save="test", model="opus", runtime="host")
            cfg.net_wait = False
            metrics = Metrics(root / "metrics.json", "net-test", "opus", "high", 600)
            runner = ClaudeRunner(cfg, root, SimpleNamespace(done=False), metrics, "")
            for attempt in range(1, 8):
                runner._handle({"type": "system", "subtype": "api_retry", "attempt": attempt, "error_status": None,
                                "error": "Connection error."})
            self.assertFalse(runner.net_outage)
            runner._handle({"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "result": "API Error: Connection error.", "usage": {}})
            self.assertFalse(runner.net_outage)
            runner.close_events()

    async def test_retries_that_recover_by_themselves_do_not_pause(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent="net-test", save="test", model="opus", runtime="host")
            metrics = Metrics(root / "metrics.json", "net-test", "opus", "high", 600)
            runner = ClaudeRunner(cfg, root, SimpleNamespace(done=False), metrics, "")
            for attempt in (1, 2):
                runner._handle({"type": "system", "subtype": "api_retry", "attempt": attempt, "error_status": 529,
                                "error": "overloaded_error"})
            self.assertIsNotNone(runner.net_down_since)
            self.assertFalse(runner.net_outage)
            runner._handle({"type": "assistant", "message": {"id": "m", "model": "claude-opus-5-5",
                                                             "content": [{"type": "text", "text": "继续"}]}})
            self.assertIsNone(runner.net_down_since)
            # 401 / 429 不当作断网
            runner._handle({"type": "system", "subtype": "api_retry", "attempt": 5, "error_status": 429, "error": "rate_limit_error"})
            self.assertIsNone(runner.net_down_since)
            # 这一轮以连接错误结束（Claude Code 放弃重试）：直接认定断网
            runner._handle({"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "result": "API Error: Connection error.", "usage": {}})
            self.assertTrue(runner.net_outage)
            runner.close_events()


if __name__ == "__main__":
    unittest.main()
