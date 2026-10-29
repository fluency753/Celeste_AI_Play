"""续跑、重接同一个 Claude Code 会话时的规则送达与操作指南重新发布：不连接游戏或模型。
用一个假的 Claude Code 子进程（stdio 流式 JSON）代替外部进程，主控内部的启动、等待网络与重接流程保持原样。"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import personal_prompts as personal
from orchestrator.agent import ClaudeRunner
from orchestrator.config import RunConfig
from orchestrator.metrics import Metrics
from orchestrator.segments import Piece

# argv[1] 是记录文件：不存在时（第一个进程）报告连接错误后挂起，像断网那样；之后的进程正常回复。
# argv[2] 为 "ok" 时第一个进程也正常回复（只测续跑首条消息）。
STUB = r'''import sys, json, time, os, uuid
def emit(v): print(json.dumps(v, ensure_ascii=False), flush=True)
mark = sys.argv[1]
first = not os.path.exists(mark) and sys.argv[2] != "ok"
open(mark, "a").close()
for line in sys.stdin:
    p = json.loads(line)
    if p.get("type") == "control_request":
        emit({"type": "control_response", "response": {"request_id": p["request_id"], "subtype": "success"}}); continue
    with open(mark, "a", encoding="utf-8") as f:
        f.write(json.dumps(p["message"]["content"], ensure_ascii=False) + "\n")
    emit({"type": "system", "subtype": "init", "model": "claude-stub", "tools": [], "mcp_servers": []})
    if first:
        for attempt in range(1, 7):
            emit({"type": "system", "subtype": "api_retry", "attempt": attempt, "max_retries": 10,
                  "retry_delay_ms": 10, "error_status": None, "error": "Connection error."})
            time.sleep(0.05)
        time.sleep(30)
        continue
    emit({"type": "assistant", "message": {"id": str(uuid.uuid4()), "model": "claude-stub",
          "content": [{"type": "text", "text": "收到。"}]}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "收到", "usage": {"output_tokens": 2}})
'''
RULES_NOTE = '（系统）续跑配置与指南如下'
HOST_NOTE = '（系统）续跑的会话沿用之前保存的提示词快照'


class ResumeRulesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name, value in (('orchestrator.config.REPO_ROOT', self.root),
                            ('orchestrator.personal_prompts.STORE', self.root / 'local/personal_prompts.json'),
                            ('orchestrator.agent.find_docker_exe', lambda: 'docker'),
                            ('orchestrator.agent.find_claude_exe', lambda: sys.executable)):
            context = patch(name, value)
            context.start()
            self.addCleanup(context.stop)
        (self.root / 'stub.py').write_text(STUB, encoding='utf-8')
        self.runs = 0

    def config(self, runtime='container'):
        cfg = RunConfig(agent='rules-test', save='test', model='opus', runtime=runtime)
        cfg.net_retry_attempts, cfg.net_probe_s, cfg.net_wait_cap_s = 4, 0.05, 0.3
        return cfg

    def run_dir(self, session_id, extra=None):
        self.runs += 1
        directory = self.root / 'runs' / str(self.runs)
        directory.mkdir(parents=True)
        (directory / 'run_state.json').write_text(json.dumps({'session_id': session_id, **(extra or {})}), encoding='utf-8')
        return directory

    async def drive(self, cfg, directory, *, session_id, resume, note='第一条消息', replies=1, during_wait=None, healthy=False):
        """跑一个 ClaudeRunner 直到收到 replies 次回复；返回 (发出的各条消息, 各次启动参数, runner)。"""
        metrics = Metrics(directory / 'metrics.json', 'rules-test', 'opus', 'high', 600)
        metrics.start_play()
        runner = ClaudeRunner(cfg, directory, SimpleNamespace(done=False, done_reason=''), metrics, '',
                              session_id=session_id, resume=resume, resume_note=note)
        mark, stub = directory / 'sent.jsonl', self.root / 'stub.py'
        expected = replies + (0 if healthy else 1)      # 断网时多一条重接消息
        commands, original_command, original_turn = [], ClaudeRunner._command, ClaudeRunner._next_user_turn

        def command(agent):
            original_command(agent)     # 保留真实的指南发布与提示词生成，只替换外部进程
            commands.append(agent._common_args())
            return [sys.executable, '-X', 'utf8', '-u', str(stub), str(mark), 'ok' if healthy else 'net']

        async def probe(agent):
            if during_wait is not None:
                during_wait()
            return True

        async def next_turn(agent, fallback, **kwargs):
            if mark.exists() and len(sent_messages(mark)) >= expected and agent.turns >= expected:
                agent.stop_reason = '测试结束'
                return False
            return await original_turn(agent, fallback, **kwargs)

        with patch.object(ClaudeRunner, '_command', command), \
             patch.object(ClaudeRunner, '_probe_network', probe), \
             patch.object(ClaudeRunner, '_next_user_turn', next_turn):
            reason = await asyncio.wait_for(runner.run(), 30)
        self.assertEqual(reason, '测试结束')
        return sent_messages(mark), commands, runner

    def guide_dir(self, cfg):
        base = cfg.workspace_dir / 'game' / 'instructions'
        return next(p for p in base.iterdir() if not p.name.startswith('.'))

    def delivered(self, directory):
        return json.loads((directory / 'run_state.json').read_text(encoding='utf-8')).get('rules_delivered')

    async def test_reconnect_republishes_tampered_guide_without_repeating_unchanged_rules(self):
        cfg = self.config()
        directory = self.run_dir('session-a')
        touched = []

        def agent_edits_guide():
            # 断网前智能体在指南目录里跑过命令（Claude Code 留下 .claude/），还改了一节
            if touched:
                return
            guide = self.guide_dir(cfg)
            (guide / '.claude').mkdir()
            (guide / '.claude' / '.cc-writes').write_text('{}', encoding='utf-8')
            section = next(p for p in guide.iterdir() if p.suffix == '.md' and p.name != 'index.md')
            touched.append((section.name, section.read_bytes()))
            section.write_text('agent edit', encoding='utf-8')

        sent, commands, runner = await self.drive(cfg, directory, session_id='session-a', resume=False,
                                                  during_wait=agent_edits_guide)
        self.assertEqual(len(commands), 2, '断网后应重启一次 Claude Code')
        self.assertIn('--session-id', commands[0])
        self.assertEqual(commands[1][commands[1].index('--resume') + 1], 'session-a')
        self.assertEqual(sent[0], '第一条消息')
        self.assertIn('模型连不上', sent[1])
        for text in sent:
            self.assertNotIn(RULES_NOTE, text)       # 系统提示词里已有，规则没变就不再附
        guide = self.guide_dir(cfg)
        name, original = touched[0]
        self.assertEqual((guide / name).read_bytes(), original)
        stale, = [p for p in guide.parent.iterdir() if p.name.startswith('.stale-')]
        self.assertEqual((stale / name).read_text(encoding='utf-8'), 'agent edit')
        self.assertTrue((stale / '.claude' / '.cc-writes').is_file())
        record = self.delivered(directory)
        self.assertEqual(record, {'session_id': 'session-a', 'sha256': runner._startup_rules_digest})

    async def test_reconnect_after_personal_prompt_change_sends_new_rules_once(self):
        for runtime, note in (('container', RULES_NOTE), ('host', HOST_NOTE)):
            with self.subTest(runtime=runtime):
                cfg = self.config(runtime)
                directory = self.run_dir('session-b-' + runtime)
                personal.STORE.unlink(missing_ok=True)

                def user_edits_personal_prompt():
                    if not personal.read('all')['text']:
                        personal.save('all', 'NEW-PERSONAL-MARKER', personal.read('all')['version'])

                sent, commands, runner = await self.drive(cfg, directory, session_id='session-b-' + runtime, resume=False,
                                                          during_wait=user_edits_personal_prompt)
                self.assertEqual(len(commands), 2)
                self.assertNotIn(note, sent[0])
                self.assertIn('模型连不上', sent[1])
                self.assertEqual(sent[1].count(note), 1)
                self.assertEqual(sent[1].count('NEW-PERSONAL-MARKER'), 1)
                self.assertEqual(self.delivered(directory)['sha256'], runner._startup_rules_digest)

    async def test_corrupt_personal_prompt_file_during_wait_keeps_delivered_snapshot(self):
        cfg = self.config()
        personal.save('all', 'KEPT-MARKER', personal.read('all')['version'])
        directory = self.run_dir('session-c')

        def user_breaks_file():
            personal.STORE.write_text('{broken', encoding='utf-8')

        with patch('builtins.print') as log:
            sent, commands, runner = await self.drive(cfg, directory, session_id='session-c', resume=False,
                                                      during_wait=user_breaks_file)
        self.assertEqual(len(commands), 2, '重接不应因个人提示词文件损坏而结束运行')
        self.assertIn('模型连不上', sent[1])
        self.assertNotIn(RULES_NOTE, sent[1])
        self.assertIn('KEPT-MARKER', runner._startup_guide['personal']['text'])
        self.assertTrue(any('个人追加要求读取失败' in str(call.args[0]) for call in log.call_args_list if call.args))

    async def test_resumed_session_sends_rules_only_when_changed_or_unknown(self):
        for runtime, note in (('container', RULES_NOTE), ('host', HOST_NOTE)):
            with self.subTest(runtime=runtime):
                cfg = self.config(runtime)
                fresh = self.run_dir('session-d')
                sent, _, runner = await self.drive(cfg, fresh, session_id='session-d', resume=False, healthy=True)
                self.assertNotIn(note, sent[0])
                record = self.delivered(fresh)
                self.assertEqual(record['session_id'], 'session-d')
                # 主控续跑（新运行目录，run.py 带上同一会话的记录）：规则没变就不附
                same = self.run_dir('session-d', {'rules_delivered': record})
                sent, _, _ = await self.drive(cfg, same, session_id='session-d', resume=True, note='续跑消息', healthy=True)
                self.assertEqual(sent[0], '续跑消息')
                # 记录属于别的会话或没有记录：不知道会话里是哪一版，照常附上
                for extra in ({'rules_delivered': {**record, 'session_id': 'other'}}, {}):
                    unknown = self.run_dir('session-d', extra)
                    sent, _, _ = await self.drive(cfg, unknown, session_id='session-d', resume=True, note='续跑消息', healthy=True)
                    self.assertEqual(sent[0].count(note), 1)
                    self.assertEqual(self.delivered(unknown), record)
                # 设置变了：附上新规则并更新记录
                changed_cfg = self.config(runtime)
                changed_cfg.min_frames_per_act = 55
                changed = self.run_dir('session-d', {'rules_delivered': record})
                sent, _, runner = await self.drive(changed_cfg, changed, session_id='session-d', resume=True,
                                                   note='续跑消息', healthy=True)
                self.assertEqual(sent[0].count(note), 1)
                self.assertNotEqual(self.delivered(changed)['sha256'], record['sha256'])
                # 追加章节的开场消息写明"以后面的规则为准"：即使没变也附上
                extended_cfg = self.config(runtime)
                extended_cfg._extended_plan = [Piece(1, 0, None)]
                current = {'session_id': 'session-d', 'sha256': runner_digest(extended_cfg)}
                extended = self.run_dir('session-d', {'rules_delivered': current})
                sent, _, _ = await self.drive(extended_cfg, extended, session_id='session-d', resume=True,
                                              note='开始后续章节', healthy=True)
                self.assertEqual(sent[0].count(note), 1)
                self.assertEqual(self.delivered(extended), current)

    def test_controller_resume_carries_record_only_for_the_same_session(self):
        from orchestrator.run import write_run_state
        cfg = self.config()
        record = {'session_id': 'kept', 'sha256': 'abc'}
        previous = {'dir': self.root, 'state': {'rules_delivered': record}, 'metrics': {}}
        for session_id, expected in (('kept', record), ('fresh', None), (None, None)):
            with self.subTest(session_id=session_id):
                directory = self.root / f'state-{session_id}'
                directory.mkdir()
                write_run_state(directory, cfg, session_id, previous)
                state = json.loads((directory / 'run_state.json').read_text(encoding='utf-8'))
                self.assertEqual(state.get('rules_delivered'), expected)


def runner_digest(cfg):
    """与主控相同的方式算出 cfg 当前规则的摘要（用来构造"会话里已有同一版规则"的记录）。"""
    import hashlib
    from orchestrator.prompt_guide import startup_context
    from orchestrator.startup_prompt import runtime_prompt
    return hashlib.sha256(runtime_prompt(cfg, guide=startup_context(cfg)).encode('utf-8')).hexdigest()


def sent_messages(mark):
    return [json.loads(line) for line in mark.read_text(encoding='utf-8').splitlines() if line.startswith('"')]


if __name__ == '__main__':
    unittest.main()
