"""可调参数（--tune）、知识库阅读权限（--kb-deny）与控制台的设置接口。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import kb_access, tuning
from orchestrator.config import RunConfig
from orchestrator.game_session import ActError, parse_plan
from orchestrator.launch_options import build
from orchestrator.prompting import play_prompt
from orchestrator.sim_assist import AssistRules, SimEnergy, validate_request


def cfg(**kw):
    c = RunConfig(agent="a", save="s", model="opus")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class TunableTests(unittest.TestCase):
    def test_defaults_match_run_config(self):
        c = cfg()
        for t in tuning.TUNABLES:
            self.assertEqual(getattr(c, t.key), t.default, t.key)

    def test_parse_and_validate(self):
        self.assertEqual(tuning.parse_pairs(["playback_speed=4", "explore_program_runs="]),
                         {"playback_speed": 4.0, "explore_program_runs": None})
        for bad in (["playback_speed"], ["nope=1"], ["max_frames_per_act=1.5"], ["playback_speed=999"],
                    ["remind_s=abc"]):
            with self.assertRaises(ValueError, msg=bad):
                tuning.parse_pairs(bad)
        with self.assertRaises(ValueError):
            tuning.check_relations({"bash_default_s": 700})
        c = cfg()
        tuning.apply(c, {"playback_speed": 5.0, "max_frames_per_act": 600})
        self.assertEqual(c.playback_speed, 5.0)
        self.assertEqual(c.tuning, {"playback_speed": 5.0})          # 只记与默认值不同的
        self.assertEqual(tuning.argv({"playback_speed": 5.0, "remind_s": 30}), ["--tune", "playback_speed=5"])

    def test_prompt_numbers_follow_tunables(self):
        speed = cfg(mode="speedrun", knowledge_profile="full", sim=True, playback=True, live=True, live_remind_s=10.0)
        base = play_prompt(speed)
        self.assertIn("一次最多 600 帧", base)
        self.assertIn("默认最多 5 分钟（用 Bash 的 timeout 参数最多加到 10 分钟）", base)
        self.assertIn("大约每 100 条画 1 条，至少 40 条", base)
        self.assertIn("解说计时缩短为 10 秒", base)
        tuning.apply(speed, {"max_frames_per_act": 900, "bash_default_s": 90, "bash_max_s": 1200,
                             "playback_ratio": 50, "playback_min_tracks": 20})
        speed.live_remind_s = 15.0
        changed = play_prompt(speed)
        for text in ("一次最多 900 帧", "默认最多 90 秒（用 Bash 的 timeout 参数最多加到 20 分钟）",
                     "大约每 50 条画 1 条，至少 20 条", "解说计时缩短为 15 秒"):
            self.assertIn(text, changed)
        explore = cfg(mode="explore", knowledge_profile="explore", chapter=1)
        self.assertIn("单次规划程序最多 15 秒", play_prompt(explore))
        self.assertNotIn("两次 act 之间最多运行", play_prompt(explore))
        tuning.apply(explore, {"explore_program_s": 40, "explore_program_runs": 2})
        text = play_prompt(explore)
        self.assertIn("单次规划程序最多 40 秒", text)
        self.assertIn("两次 act 之间最多运行 2 次规划程序", text)
        tuning.apply(explore, {"explore_program_s": None})
        self.assertIn("单次规划程序不限时长；", play_prompt(explore))
        # 探索模式：act 单次上限与自写模拟器、搜索程序的推演帧数（默认都是 150 帧）
        from orchestrator.modes import act_frame_cap
        from orchestrator.prompting import fill
        self.assertEqual((act_frame_cap(explore), act_frame_cap(speed)), (150, 900))
        self.assertEqual(fill("一次最多 {act_max_frames} 帧", explore), "一次最多 150 帧")
        self.assertIn("自写的模拟器或搜索程序每次推演最多 150 帧", play_prompt(explore))
        tuning.apply(explore, {"explore_act_frames": 240, "explore_search_frames": None})
        self.assertEqual(act_frame_cap(explore), 240)
        self.assertNotIn("自写的模拟器或搜索程序每次推演", play_prompt(explore))
        self.assertNotIn("{explore_search_rule}", play_prompt(explore))
        # 最多可用的 CPU 核：不限时写本机核数；设了就写设的数；容器实际能用的更少时写实际数
        self.assertIn(f"本次最多可以用 {tuning.HOST_CPUS} 个核", play_prompt(explore))
        tuning.apply(explore, {"agent_cpus": 1})
        self.assertIn("可用多核缩短计算时间，本次最多可以用 1 个核", play_prompt(explore))
        with self.assertRaises(ValueError):
            tuning.coerce("agent_cpus", tuning.HOST_CPUS + 1)      # 不超过本机的核数
        explore.agent_cpus_effective = 1
        self.assertNotIn("{cpu_rule}", play_prompt(explore))
        self.assertIn("实在拿不到才放弃", play_prompt(explore))          # 鼓励拿草莓
        self.assertIn("模拟器对象尽量只构造一次再复用", play_prompt(explore))   # /workspace 是主机挂载，读写慢
        # 多核那句后面的并行要点：Claude Code 执行器是详细版本，Codex 执行器是简短的一句
        self.assertIn("个核，用几个按实际环境决定；并行时注意，主进程单核做的汇总", play_prompt(explore))
        self.assertIn("再由子进程从父节点重放生成。并行版本要用同样的输入与单核对比，确认结果完全一致。/workspace", play_prompt(explore))
        self.assertNotIn("避免大量进程通信", play_prompt(explore))
        explore.engine = "codex"
        self.assertIn("个核，用几个按实际环境决定；避免大量进程通信，用相同输入核对并行与单核结果。/workspace", play_prompt(explore))
        self.assertNotIn("去重只在主进程做", play_prompt(explore))
        self.assertNotIn("{parallel_rule}", play_prompt(explore))
        explore.engine = "claude"
        self.assertNotIn("不要求全收集", play_prompt(explore))
        for placeholder in ("{act_max_frames}", "{bash_default}", "{explore_program}", "{explore_program_rule}", "{remind}",
                            "{playback_ratio}"):
            self.assertNotIn(placeholder, changed + text)

    def test_thought_brief_per_engine(self):
        """Claude Code 执行器的提示词与工具说明都要求思考语句汉字不超过 30 个（英文术语不计），超过就退回；
        Codex 执行器不加，工具说明照旧。"""
        import asyncio
        from types import SimpleNamespace
        from orchestrator.metrics import Metrics
        from orchestrator.server import build_server
        from orchestrator.thought import count_hanzi, thought_problem
        rule = "思考语句尽量简短，汉字不超过 30 个（英文术语不计）。"

        def descriptions(c):
            with tempfile.TemporaryDirectory() as d:
                metrics = Metrics(Path(d) / "metrics.json", "a", "opus", "high", 600)
                server = build_server(SimpleNamespace(cfg=c, metrics=metrics, workspace=None))
                return {t.name: t.description for t in asyncio.run(server.list_tools())}

        for kw in (dict(mode="explore", knowledge_profile="explore", chapter=1),
                   dict(mode="speedrun", knowledge_profile="full", sim=True, runtime="container"),
                   dict(mode="speedrun", knowledge_profile="full", sim=False, runtime="container")):
            claude, codex = cfg(**kw), cfg(engine="codex", **kw)
            self.assertEqual(play_prompt(claude).count(rule), 1, kw)
            self.assertNotIn("汉字不超过", play_prompt(codex), kw)
            for c in (claude, codex):
                self.assertNotIn("{thought_brief", play_prompt(c))
                self.assertEqual(play_prompt(c).count("思考语句的语气不必一直冷静理智，可以自然流露真实的情绪"), 1, kw)
                self.assertNotIn("{thought_mood", play_prompt(c))
        tools = descriptions(cfg(mode="explore", knowledge_profile="explore", chapter=1))
        self.assertIn("thought：一两句中文（尽量简短，汉字不超过 30 个，英文术语不计），会显示给观众", tools["act"])
        self.assertIn("thought：一两句中文（尽量简短，汉字不超过 30 个，英文术语不计），只写", tools["narrate"])
        tools = descriptions(cfg(mode="explore", knowledge_profile="explore", chapter=1, engine="codex"))
        self.assertIn("thought：一两句中文，会显示给观众", tools["act"])
        self.assertIn("thought：一两句中文（120 字以内），只写", tools["narrate"])
        for desc in (tools["act"], tools["narrate"]):
            self.assertIn("语气不必一直冷静理智，可以自然带点情绪，但不要夸张。", desc)
            self.assertNotIn("{thought_mood", desc)
        # 只数汉字：英文术语、数字与标点不计
        self.assertEqual(count_hanzi("先 climbjump 上墙，再 wallbounce 越过尖刺。"), 8)
        claude, codex = cfg(), cfg(engine="codex")
        ok = "借" * 30 + " cornerboost hyper wavedash ultra"
        self.assertIsNone(thought_problem(claude, ok))
        self.assertIn("有 31 个汉字", thought_problem(claude, ok + "冲"))
        self.assertIsNone(thought_problem(codex, ok + "冲"))                     # Codex 执行器不限汉字数
        self.assertIn("120 字以内", thought_problem(codex, "冲" * 121))
        tuning.apply(claude, {"thought_max_hanzi": 12})
        self.assertIn("汉字不超过 12 个", play_prompt(cfg(mode="explore", knowledge_profile="explore", chapter=1,
                                                       thought_max_hanzi=12)))
        self.assertIn("压缩到 12 个以内", thought_problem(claude, "冲" * 13))
        # act 与 narrate 都按同一规则退回
        tools_server = build_server(SimpleNamespace(cfg=cfg(mode="explore", knowledge_profile="explore", chapter=1),
                                                    metrics=None, workspace=None))
        reply = asyncio.run(tools_server.call_tool("narrate", {"thought": "冲" * 31}))
        self.assertIn("有 31 个汉字", json.dumps(reply, ensure_ascii=False, default=str))

    def test_retry_limits(self):
        plan = {"inputs": ["10,R"], "retry": {"command": "python3 p.py", "max": 7, "timeout_s": 90}}
        with self.assertRaises(ActError):
            parse_plan(plan)                                          # 默认上限 5 次、60 秒
        self.assertEqual(parse_plan(plan, max_retries=8, max_timeout_s=120).retry.max, 7)

    def test_assist_rules(self):
        self.assertEqual(validate_request({"candidates": [["60,R"]]})[1], 3)
        rules = AssistRules.of(cfg(assist_cost_base=2, assist_cost_frames=12, assist_max_candidates=2, assist_initial=10))
        self.assertEqual(validate_request({"candidates": [["60,R"]]}, rules)[1], 7)
        with self.assertRaises(ValueError):
            validate_request({"candidates": [["10,R"]] * 3}, rules)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(SimEnergy(Path(folder) / "e.json", rules=rules).snapshot()["balance"], 10)
        self.assertIn("体力上限100，初始60", AssistRules().text())

    def test_explore_program_runs_between_acts(self):
        from orchestrator.game_session import GameSession
        session = GameSession.__new__(GameSession)
        session.cfg = cfg(mode="explore", explore_program_runs=2)
        session.programs_since_act = 0
        self.assertIsNone(session.program_run_denied())
        self.assertIsNone(session.program_run_denied())
        self.assertIn("最多运行 2 次", session.program_run_denied())
        session.cfg.mode = "speedrun"
        self.assertIsNone(session.program_run_denied())              # 速通模式不限

    def test_bash_limits(self):
        from orchestrator.agent import bash_limits_ms
        from orchestrator.codex_agent import program_cap
        self.assertEqual(bash_limits_ms(cfg()), (300000, 600000))
        self.assertEqual(bash_limits_ms(cfg(mode="explore")), (15000, 15000))
        self.assertEqual(program_cap(cfg(mode="explore", explore_program_s=30)), 30)
        # 不限时：上限放到 24 小时，实际由整次运行的现实时间上限约束
        self.assertEqual(bash_limits_ms(cfg(mode="explore", explore_program_s=None)), (86400000, 86400000))
        self.assertEqual(program_cap(cfg(mode="explore", explore_program_s=None)), 86400)
        self.assertIsNone(tuning.parse_pairs(["explore_program_s=none"])["explore_program_s"])

    def test_run_args(self):
        from orchestrator.run import parse_args
        with tempfile.TemporaryDirectory() as folder:
            argv = ["run", "--engine", "codex", "--agent", "t", "--save", "s", "--chapter", "0",
                    "--tune", "playback_speed=4", "--kb-deny", "tools.md", "--mode", "explore", "--no-sim-assist"]
            with patch("orchestrator.config.REPO_ROOT", Path(folder)), patch.object(sys, "argv", argv):
                c = parse_args()[0]
            self.assertEqual(c.playback_speed, 4.0)
            self.assertEqual(c.tuning, {"playback_speed": 4.0})
            self.assertEqual(c.kb_deny, ["tools.md"])
            self.assertEqual(c.live_remind_s, 30.0)
            with patch.object(sys, "argv", argv[:-7] + ["--tune", "nope=1"]):
                with self.assertRaises(SystemExit):
                    parse_args()

    def test_launch_options_pass_tunables_and_kb(self):
        save = {"name": "test", "slot": 7, "create": False}
        argv = build({"agent": "dev", "save": save, "tune": {"playback_speed": 4, "remind_s": ""},
                      "kb_deny": ["techniques.md"], "study_minutes": 30})
        self.assertEqual(argv[argv.index("--tune") + 1], "playback_speed=4")
        self.assertEqual(argv[argv.index("--kb-deny") + 1], "techniques.md")
        self.assertEqual(argv[argv.index("--study-minutes") + 1], "30")
        for bad in ({"tune": {"nope": 1}}, {"kb_deny": ["missing.md"]}, {"map": "mod"}, {"bridge_port": 80},
                    {"runtime": "host"}, {"ffmpeg": "a\nb"}):
            with self.assertRaises(ValueError, msg=bad):
                build({"agent": "dev", "save": save, **bad})


class KbAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        kb = self.root / "kb"
        (kb / "explore" / "help").mkdir(parents=True)
        (kb / "reference" / "celeste_wiki").mkdir(parents=True)
        for rel, text in {"README.md": "a\n", "techniques.md": "b\n", "explore/README.md": "c\n",
                          "explore/tools.md": "d\n", "explore/help/controls.md": "e\n",
                          "reference/README.md": "f\n", "reference/celeste_wiki/Tech.md": "g\n"}.items():
            (kb / rel).write_text(text, encoding="utf-8")
        self.patches = [patch.object(kb_access, "KB_ROOT", kb), patch("orchestrator.config.REPO_ROOT", self.root)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.temp.cleanup()

    def test_documents_per_mode(self):
        speed = {d["path"]: d for d in kb_access.documents("speedrun")}
        self.assertIn("reference/celeste_wiki/Tech.md", speed)
        self.assertTrue(speed["techniques.md"]["required"])
        self.assertFalse(speed["explore/tools.md"]["required"])
        explore = {d["path"]: d for d in kb_access.documents("explore")}
        self.assertEqual(set(explore), {"README.md", "tools.md", "help/controls.md"})
        self.assertEqual(explore["help/controls.md"]["group"], "操作提示（选读）")
        with self.assertRaises(ValueError):
            kb_access.normalize_deny("explore", ["techniques.md"])

    def test_read_write_stays_inside_kb(self):
        (self.root / "kb/explore/tools.md").write_bytes(b"d\r\n")
        doc = kb_access.read_document("explore", "tools.md")
        self.assertEqual(doc["text"], "d\n")
        kb_access.write_document("explore", "tools.md", "new\r\n", doc["mtime"])
        self.assertEqual((self.root / "kb/explore/tools.md").read_text(encoding="utf-8"), "new\n")
        with self.assertRaises(ValueError):
            kb_access.write_document("explore", "tools.md", "x", doc["mtime"] - 100)   # 打开之后被改过
        for bad in ("../README.md", "/etc/passwd", "..\\x.md", ""):
            with self.assertRaises(ValueError, msg=bad):
                kb_access.read_document("explore", bad)
        with self.assertRaises(ValueError):
            kb_access.write_document("speedrun", "new.md", "x")                        # 只编辑已有文件

    def test_view_holds_only_allowed_files(self):
        from orchestrator.kb_study import required_files
        c = cfg()
        self.assertEqual(kb_access.prepare_view(c, log=lambda *_: None), self.root / "kb")
        c.kb_deny = ["techniques.md", "reference/celeste_wiki/Tech.md"]
        view = kb_access.prepare_view(c, log=lambda *_: None)
        self.assertEqual(view, c.kb_dir)
        self.assertTrue((view / "README.md").is_file())
        self.assertFalse((view / "techniques.md").exists())
        self.assertFalse((view / "reference" / "celeste_wiki").exists())
        self.assertNotIn("techniques.md", required_files(view))
        c.kb_deny = ["README.md"]                                     # 换一组：之前不给看的回来，新的被删掉
        kb_access.prepare_view(c, log=lambda *_: None)
        self.assertTrue((view / "techniques.md").is_file())
        self.assertFalse((view / "README.md").exists())

    def test_container_kb_view_label(self):
        from orchestrator.container import LABEL, LABEL_DEFAULTS, _labels
        c = cfg()
        self.assertEqual(_labels(c)[f"{LABEL}.kb_view"], "0")
        self.assertEqual(LABEL_DEFAULTS[f"{LABEL}.kb_view"], "0")     # 旧容器没有这个标签，不会因此重建
        c.kb_deny = ["README.md"]
        self.assertEqual(_labels(c)[f"{LABEL}.kb_view"], "1")


class ConsolePrefsTests(unittest.TestCase):
    def test_game_dir_and_kb_prefs(self):
        from orchestrator import gamepaths
        from overlay import console_api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            game = root / "Celeste"
            (game / "Saves").mkdir(parents=True)
            (game / "Celeste.exe").write_bytes(b"")
            prefs = root / "configs" / "local.console.json"
            with patch.object(console_api, "PREFS", prefs), patch.object(gamepaths, "CONSOLE_PREFS", prefs), \
                 patch.object(gamepaths, "DEPLOY_ANSWERS", root / "none.json"), patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("CELESTE_DIR", None)
                with self.assertRaisesRegex(ValueError, "Celeste.exe"):
                    console_api.save_prefs({"celeste_dir": str(root)})
                saved = console_api.save_prefs({"celeste_dir": f'"{game}"'})
                self.assertEqual(saved["celeste_dir"], str(game))
                self.assertEqual(gamepaths.celeste_dir_info()["source"], "console")
                self.assertEqual(gamepaths.celeste_dir(), game)
                saved = console_api.save_prefs({"kb_deny": {"explore": ["help/controls.md"]}})
                self.assertEqual(saved["kb_deny"]["explore"], ["help/controls.md"])
                self.assertEqual(json.loads(prefs.read_text(encoding="utf-8"))["celeste_dir"], str(game))
                with self.assertRaises(ValueError):
                    console_api.save_prefs({"other": 1})


if __name__ == "__main__":
    unittest.main()
