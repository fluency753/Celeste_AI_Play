"""服务模型被换掉（例如 Opus 5.5 的安全分类器拦下回复后 Claude Code 回退到 Opus 4.8）时主控当场拦下、重启换回，
换不回就保持暂停：不连接游戏或模型，用假的 Claude Code 子进程（stdio 流式 JSON）重放回退事件。"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator.agent import MODEL_RESTARTS, ClaudeRunner, model_key
from orchestrator.config import RunConfig
from orchestrator.game_policy import denial
from orchestrator.metrics import Metrics

# 每个进程开始时在日志里记一行 START；mode=once 时只有第一个进程回退，mode=always 时每个进程都回退。
STUB = r'''import sys, json, time, uuid
def emit(v): print(json.dumps(v, ensure_ascii=False), flush=True)
log, mode = sys.argv[1], sys.argv[2]
with open(log, "a", encoding="utf-8") as f:
    f.write("START " + json.dumps(sys.argv[3:]) + "\n")
starts = open(log, encoding="utf-8").read().count("START")
for line in sys.stdin:
    p = json.loads(line)
    if p.get("type") == "control_request":
        emit({"type": "control_response", "response": {"request_id": p["request_id"], "subtype": "success"}}); continue
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps(p["message"]["content"], ensure_ascii=False) + "\n")
    emit({"type": "system", "subtype": "init", "model": "claude-opus-5-5", "tools": [], "mcp_servers": []})
    if mode == "always" or starts == 1:
        emit({"type": "system", "subtype": "informational", "level": "notice",
              "content": "Opus 5.5's safeguards stopped the response above · continuing once with that noted"})
        emit({"type": "assistant", "message": {"id": str(uuid.uuid4()), "model": "claude-opus-4-8", "content": [
            {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}}]}})
        time.sleep(30)        # 不再输出：等主控结束进程
        continue
    emit({"type": "assistant", "message": {"id": str(uuid.uuid4()), "model": "claude-opus-5-5",
          "content": [{"type": "text", "text": "接着玩。"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "ok", "usage": {"output_tokens": 3}})
'''


def runner_for(root, model='opus', third_party=False):
    cfg = RunConfig(agent='model-test', save='test', model=model, runtime='host')
    if third_party:
        cfg.provider, cfg.provider_info = 'deepseek', {'label': 'DeepSeek', 'base_url': 'https://example.invalid'}
    metrics = Metrics(root / 'metrics.json', 'model-test', model, 'high', 600)
    metrics.start_play()
    return ClaudeRunner(cfg, root, SimpleNamespace(done=False, done_reason=''), metrics, ''), metrics


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_model_names_compare_without_suffix_or_date(self):
        self.assertEqual(model_key('claude-sonnet-4-5-20250929'), model_key('claude-sonnet-4-5'))
        self.assertEqual(model_key('claude-opus-5-5[1m]'), model_key('claude-opus-5-5'))
        self.assertNotEqual(model_key('claude-opus-4-8'), model_key('claude-opus-5-5'))

    def test_fallback_reply_blocks_every_tool(self):
        runner, metrics = runner_for(self.root)
        self.addCleanup(runner.close_events)
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5', 'tools': [], 'mcp_servers': []})
        self.assertEqual(runner._model_name(), 'opus')                 # 平时照旧用别名启动
        runner._handle({'type': 'assistant', 'message': {'id': 'a', 'model': 'claude-opus-5-5',
                                                          'content': [{'type': 'text', 'text': '正常'}]}})
        self.assertIsNone(runner.model_mismatch)
        runner._handle({'type': 'system', 'subtype': 'informational', 'content': "Opus 5.5's safeguards stopped the response"})
        runner._handle({'type': 'assistant', 'message': {'id': 'b', 'model': 'claude-opus-4-8', 'content': [
            {'type': 'fallback', 'from': {'model': 'claude-opus-5-5'}, 'to': {'model': 'claude-opus-4-8'}}]}})
        self.assertEqual(runner.model_mismatch['actual'], 'claude-opus-4-8')
        self.assertEqual(runner.model_mismatch['expected'], 'claude-opus-5-5')
        self.assertEqual(metrics.state.status, 'model_wait')
        self.assertEqual(runner._model_name(), 'claude-opus-5-5')      # 换回时用具体名字，不让别名解析成别的
        for tool in ('act', 'observe', 'stop_run', 'narrate'):
            self.assertIn('claude-opus-4-8', denial(runner, 'mcp__celeste__' + tool))
        self.assertIn('主控已拦下', runner._workspace_hook_guard({'session_id': runner.session_id, 'tool_name': 'Bash'}))
        events = (self.root / 'public_events.jsonl').read_text(encoding='utf-8')
        self.assertIn('服务模型被换成了 claude-opus-4-8', events)
        self.assertIn('safeguards', events)
        # 重启后的会话第一条回复是指定模型：放行
        runner._handle({'type': 'assistant', 'message': {'id': 'c', 'model': 'claude-opus-5-5',
                                                          'content': [{'type': 'text', 'text': '回来了'}]}})
        self.assertIsNone(runner.model_mismatch)
        self.assertIn('已换回指定模型', (self.root / 'public_events.jsonl').read_text(encoding='utf-8'))

    def test_a_different_model_without_fallback_block_is_also_caught(self):
        runner, _ = runner_for(self.root)
        self.addCleanup(runner.close_events)
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5', 'tools': [], 'mcp_servers': []})
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-4-8', 'tools': [], 'mcp_servers': []})
        self.assertEqual(runner.model_mismatch['actual'], 'claude-opus-4-8')

    def test_subagent_and_synthetic_messages_are_ignored(self):
        runner, _ = runner_for(self.root)
        self.addCleanup(runner.close_events)
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5', 'tools': [], 'mcp_servers': []})
        runner._handle({'type': 'assistant', 'parent_tool_use_id': 'toolu_1',
                        'message': {'id': 'd', 'model': 'claude-haiku-5-5', 'content': []}})
        runner._handle({'type': 'assistant', 'message': {'id': 'e', 'model': '<synthetic>', 'content': []}})
        runner._handle({'type': 'assistant', 'message': {'id': 'f', 'model': 'claude-opus-5-5-20260601', 'content': []}})
        self.assertIsNone(runner.model_mismatch)

    def test_explicit_model_name_is_the_expected_one(self):
        runner, _ = runner_for(self.root, model='claude-opus-5-5')
        self.addCleanup(runner.close_events)
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-4-8', 'tools': [], 'mcp_servers': []})
        self.assertEqual(runner.model_mismatch['expected'], 'claude-opus-5-5')

    def test_expected_model_follows_the_run_across_resumes(self):
        (self.root / 'run_state.json').write_text('{}', encoding='utf-8')
        runner, _ = runner_for(self.root)
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-5', 'tools': [], 'mcp_servers': []})
        runner.close_events()
        state = json.loads((self.root / 'run_state.json').read_text(encoding='utf-8'))
        self.assertEqual(state['model_expected'], {'requested': 'opus', 'resolved': 'claude-opus-5-5'})
        again, _ = runner_for(self.root)
        self.addCleanup(again.close_events)
        self.assertEqual(again._expected_model, 'claude-opus-5-5')
        again._handle({'type': 'system', 'subtype': 'init', 'model': 'claude-opus-5-6', 'tools': [], 'mcp_servers': []})
        self.assertEqual(again.model_mismatch['actual'], 'claude-opus-5-6')   # 续跑时别名解析成了别的模型
        other, _ = runner_for(self.root, model='sonnet')         # 换了指定模型：不沿用
        self.addCleanup(other.close_events)
        self.assertIsNone(other._expected_model)

    def test_third_party_names_are_not_compared_but_fallback_still_counts(self):
        runner, _ = runner_for(self.root, model='deepseek-v4.1-flash', third_party=True)
        self.addCleanup(runner.close_events)
        runner._handle({'type': 'system', 'subtype': 'init', 'model': 'deepseek-v4.1-flash', 'tools': [], 'mcp_servers': []})
        runner._handle({'type': 'assistant', 'message': {'id': 'g', 'model': 'deepseek-chat', 'content': []}})
        self.assertIsNone(runner.model_mismatch)
        runner._handle({'type': 'assistant', 'message': {'id': 'h', 'model': 'deepseek-chat', 'content': [
            {'type': 'fallback', 'to': {'model': 'deepseek-lite'}}]}})
        self.assertEqual(runner.model_mismatch['actual'], 'deepseek-lite')


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_stub(self, mode, *, stop_when_paused=False):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        stub, log = root / 'stub.py', root / 'log.txt'
        stub.write_text(STUB, encoding='utf-8')
        runner, metrics = runner_for(root)
        runner.resume_note = '第一条消息'
        original_command = ClaudeRunner._command

        def command(self):
            original_command(self)
            return [sys.executable, '-X', 'utf8', '-u', str(stub), str(log), mode, *self._common_args()]

        async def next_turn(self, fallback, orig=ClaudeRunner._next_user_turn, **kwargs):
            if self.turns >= 1 and self.model_mismatch is None and log.exists() and '接着玩' not in fallback \
                    and log.read_text(encoding='utf-8').count('START') >= 2:
                self.stop_reason = '测试结束'
                return False
            return await orig(self, fallback, **kwargs)

        async def stop_once_paused():
            while not (runner.paused and runner.model_mismatch is not None):
                await asyncio.sleep(.05)
            runner.stop_requested = True

        watcher = asyncio.create_task(stop_once_paused()) if stop_when_paused else None
        with patch('orchestrator.agent.find_claude_exe', return_value=sys.executable), \
             patch('orchestrator.personal_prompts.STORE', root / 'personal.json'), \
             patch.object(ClaudeRunner, '_command', command), \
             patch.object(ClaudeRunner, '_next_user_turn', next_turn):
            reason = await asyncio.wait_for(runner.run(), 60)
        if watcher is not None:
            await watcher
        text = log.read_text(encoding='utf-8')
        starts = [json.loads(line[6:]) for line in text.splitlines() if line.startswith('START ')]
        sent = [json.loads(line) for line in text.splitlines() if line.startswith('"')]
        events = (root / 'public_events.jsonl').read_text(encoding='utf-8')
        return runner, metrics, reason, starts, sent, events

    async def test_fallback_restarts_the_same_session_with_the_expected_model(self):
        runner, metrics, reason, starts, sent, events = await self.run_stub('once')
        self.assertEqual(reason, '测试结束')
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0][starts[0].index('--model') + 1], 'opus')
        self.assertEqual(starts[1][starts[1].index('--model') + 1], 'claude-opus-5-5')
        self.assertEqual(starts[1][starts[1].index('--resume') + 1], runner.session_id)
        self.assertIn('自动换成了 claude-opus-4-8', sent[1])
        self.assertIn('游戏一直冻结', sent[1])
        self.assertIn('服务模型被换成了 claude-opus-4-8', events)
        self.assertIn('已换回指定模型 claude-opus-5-5', events)
        self.assertIsNone(runner.model_mismatch)
        self.assertFalse(runner.paused)
        self.assertEqual(metrics.state.quota_waits, 0)
        self.assertEqual(metrics.state.net_waits, 0)

    async def test_stays_paused_when_the_model_cannot_be_switched_back(self):
        runner, metrics, reason, starts, sent, events = await self.run_stub('always', stop_when_paused=True)
        self.assertEqual(len(starts), 1 + MODEL_RESTARTS)
        self.assertIn('换不回指定模型', events)
        self.assertTrue(runner.paused)
        self.assertEqual(runner.model_mismatch['actual'], 'claude-opus-4-8')
        self.assertIn('claude-opus-4-8', reason)
        self.assertEqual(metrics.state.status, 'paused')


if __name__ == '__main__':
    unittest.main()
