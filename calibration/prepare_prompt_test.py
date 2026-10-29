"""准备阶段的第一条消息按运行类型给出下一步：仅准备不开始试玩，准备后试玩读完即 observe 开始，
开了预置模拟器的速通另要求先想清技巧与程序并记进笔记；同一条规则不重复。不连接游戏或模型。"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orchestrator import prompting
from orchestrator.agent import ClaudeRunner
from orchestrator.codex_agent import CodexRunner
from orchestrator.config import RunConfig
from orchestrator.kb_study import KbStudy
from orchestrator.metrics import Metrics
from orchestrator.prompting import study_prompt

PLAY = '读完后调用 observe 开始游玩'
SIM_PLAN = '哪些技巧能在本章用来缩短游戏内计时'
PREPARE_ONLY = '本次仅准备：按要求完整读取知识库、整理工作区 notes.md 后结束本轮；不调用游戏工具，不开始试玩。'


class PreparePromptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.kb = self.root / 'kb'
        self.kb.mkdir()
        for name in ('README.md', 'tools.md'):
            (self.kb / name).write_text('one\n', encoding='utf-8')
        for name, value in (('orchestrator.config.REPO_ROOT', self.root),
                            ('orchestrator.personal_prompts.STORE', self.root / 'local/personal.json')):
            context = patch(name, value)
            context.start()
            self.addCleanup(context.stop)
        overrides = patch.object(prompting, 'OVERRIDES', self.root / 'prompts')
        overrides.start()
        self.addCleanup(overrides.stop)

    def test_each_run_type_gets_its_own_next_step(self):
        study = KbStudy(self.kb, 30)
        play = study_prompt(study)
        self.assertIn(PLAY, play)
        self.assertIn('到时游戏工具自动开放', play)
        self.assertNotIn(SIM_PLAN, play)
        speedrun = study_prompt(study, sim=True)
        self.assertIn(SIM_PLAN, speedrun)
        self.assertIn('把要点整理进 /workspace/notes.md', speedrun)
        self.assertIn('然后调用 observe 开始游玩', speedrun)
        self.assertIn('/sim', speedrun)
        for sim in (False, True):
            prepare = study_prompt(study, sim=sim, prepare_only=True)
            self.assertNotIn('observe 开始游玩', prepare)
            self.assertNotIn('游戏工具自动开放', prepare)
            self.assertNotIn(SIM_PLAN, prepare)          # 仅准备没有"本章"
            self.assertEqual('/sim' in prepare, sim)
        for text in (play, speedrun, study_prompt(study, prepare_only=True)):
            self.assertNotIn('仅准备阶段不开始试玩', text)
            self.assertIn('/kb/README.md', text)
            self.assertIn('不用 Bash 扫描或搜索代替阅读', text)

    async def claude_first_message(self, *, prepare_only, sim=False):
        from calibration.lifecycle_test import STUB
        run = self.root / ('prepare' if prepare_only else 'play') / ('sim' if sim else 'nosim')
        run.mkdir(parents=True)
        captured, stub = run / 'sent.jsonl', run / 'stub.py'
        stub.write_text(STUB.replace(" text=p['message']['content']",
            " text=p['message']['content']\n with open(sys.argv[1],'a',encoding='utf-8') as f: f.write(json.dumps(text,ensure_ascii=False)+'\\n')"),
            encoding='utf-8')
        cfg = RunConfig(agent='prepare-test', save='test', model='test', sim=sim)
        runner = ClaudeRunner(cfg, run, SimpleNamespace(done=False, done_reason=''),
                              Metrics(run / 'metrics.json', 'prepare-test', 'test', 'high', 60), 'stub',
                              container='unused', study=KbStudy(self.kb, 10), prepare_only=prepare_only)
        original = runner._command

        def command():
            original()      # 保留真实的提示词与指南发布，只替换外部进程
            return [sys.executable, '-X', 'utf8', '-u', str(stub), str(captured)]

        with patch.object(runner, '_command', side_effect=command), \
             patch('orchestrator.agent.find_docker_exe', return_value='docker'):
            await asyncio.wait_for(runner.run(), 10)
        return json.loads(captured.read_text(encoding='utf-8').splitlines()[0])

    async def test_claude_prepare_only_and_play_runs_send_matching_first_message(self):
        prepare = await self.claude_first_message(prepare_only=True)
        self.assertEqual(prepare.count(PREPARE_ONLY), 1)
        self.assertEqual(prepare.count('不开始试玩'), 1)
        self.assertNotIn('observe 开始游玩', prepare)
        for sim in (False, True):
            with self.subTest(sim=sim):
                play = await self.claude_first_message(prepare_only=False, sim=sim)
                self.assertNotIn('不开始试玩', play)
                self.assertEqual(play.count('observe 开始游玩'), 1)
                self.assertEqual(SIM_PLAN in play, sim)

    def test_codex_preparation_message_no_longer_says_play_is_off_in_play_runs(self):
        for prepare_only in (False, True):
            with self.subTest(prepare_only=prepare_only):
                cfg = RunConfig(agent='prepare-test', save='test', model='test', engine='codex')
                runner = CodexRunner(cfg, self.root, None, Metrics(self.root / 'metrics.json', 'prepare-test', 'test', 'high', 60),
                                     'unused', KbStudy(self.kb, 10), prepare_only=prepare_only)
                try:
                    text = runner._first_prompt()
                finally:
                    runner.close_events()
                self.assertNotIn('仅准备阶段不开始试玩', text)
                self.assertEqual('本次只准备' in text, prepare_only)


if __name__ == '__main__':
    unittest.main()
