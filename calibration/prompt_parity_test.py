"""控制台的提示词预览与实际开跑一致，提示词里没有冗余的哈希与重复路径：

- 同一组运行设置，launch_options.config_for（控制台预览）与 build → run.parse_args（控制台开跑的命令）得到的设置与提示词逐字相同；
  控制台能选的组合都能通过主控的启动校验（例如开着计划外地图时不再带上旧的 --no-allow-chapter-reset）；
- 现实时间上限不超过一天才写进启动提示词，9999999 这类"不限时"不写，命令行里也不写成 1e+07；
- 工作区操作指南的目录名是短哈希、只出现一次，索引不列每节的内容哈希；
- 探索模式开放的速通资料按目录归并，不把长路径逐个列出。
"""
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))     # 主控 import bridge_client

from orchestrator import launch_options, profiles
from orchestrator.prompting import prompt_parts, speedrun_allow_rule
from orchestrator.prompt_guide import VERSION_CHARS, guide_document
from orchestrator.startup_prompt import TIME_LIMIT_SHOWN_MAX, runtime_prompt, task_brief

SAVE = {"name": "test", "slot": 7, "create": False}
FIELDS = ("engine", "provider", "model", "effort", "mode", "max_minutes", "save", "save_slot", "chapters", "plan",
          "live", "playback", "record", "hud_mod", "formal_thought", "stiffness", "save_context", "sim", "sim_assist",
          "cutscene_mode", "allow_detour", "action_permissions", "kb_allow", "kb_deny", "study", "tuning",
          "recording_mode", "play_scope", "completion_policy", "provider_info")

CASES = {
    "探索 · Claude": {"mode": "explore"},
    "探索 · Codex": {"mode": "explore", "engine": "codex", "provider": "codex", "model": "gpt-5.5"},
    "探索 · 计划外地图与剧情交给智能体": {"mode": "explore", "allow_detour": True, "cutscene_mode": "agent",
                                   "action_permissions": {"restart_chapter": False, "return_to_map": False, "save_quit": False}},
    "探索 · 仅正式思考、思考僵直、直播": {"mode": "explore", "formal_thought": True, "stiffness": True, "live": True,
                                  "tune": {"explore_program_s": None, "explore_act_frames": 300}},
    "探索 · Codex 计划外地图": {"mode": "explore", "engine": "codex", "provider": "codex", "model": "gpt-5.5",
                            "allow_detour": True, "formal_thought": True},
    "速通 · 关模拟器": {"mode": "speedrun", "sim": False, "chapters": [1, 2], "playback": True},
    "速通 · B 面": {"mode": "speedrun", "sim": False, "chapters": [4], "sides": [[4, "B"]]},
    "不限时": {"mode": "explore", "max_minutes": 9999999},
}


class PreviewMatchesLaunchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(profiles.activate, profiles.LEGACY)

    def launch(self, options):
        from orchestrator.run import parse_args
        argv = ["run", *launch_options.build(options, profiles.DEFAULT)]
        with patch.object(sys, "argv", argv):
            cfg, _ = parse_args()
        return cfg, argv

    def test_preview_and_launch_give_the_same_settings_and_prompt(self):
        for name, extra in CASES.items():
            options = {"agent": "parity", "save": SAVE, **extra}
            with self.subTest(name):
                launched, argv = self.launch(options)
                preview = launch_options.config_for(options, profiles.DEFAULT)
                for field in FIELDS:
                    if field == "chapters":      # 单章时命令行写 --chapter N，主控在开局后才补成列表
                        self.assertEqual(preview.chapters or [preview.chapter], launched.chapters or [launched.chapter])
                        continue
                    self.assertEqual(getattr(preview, field, None), getattr(launched, field, None), field)
                with profiles.using(profiles.DEFAULT):
                    texts = []
                    for cfg in (preview, launched):
                        parts = "".join(p.sep + p.text for p in prompt_parts(cfg))
                        texts.append(parts + runtime_prompt(cfg, guide=guide_document(cfg)))
                self.assertEqual(texts[0], texts[1])
                if extra.get("allow_detour"):
                    self.assertNotIn("--no-allow-chapter-reset", argv)
                    self.assertTrue(launched.allow_detour and launched.action_permissions["return_to_map"])

    def test_minutes_are_written_exactly(self):
        argv = launch_options.build({"agent": "parity", "save": SAVE, "max_minutes": 9999999})
        self.assertEqual(argv[argv.index("--max-minutes") + 1], "9999999")
        self.assertEqual(launch_options.minutes_text(90.5), "90.5")
        with self.assertRaises(ValueError):
            launch_options.normalize({"agent": "parity", "max_minutes": 10_000_000})


class PromptRedundancyTests(unittest.TestCase):
    def cfg(self, **kw):
        options = {"agent": "parity", "save": SAVE, "mode": "explore", **kw}
        return launch_options.config_for(options, profiles.DEFAULT)

    def test_time_limit_is_told_only_when_it_matters(self):
        self.assertIn(f"本次现实时间上限：{TIME_LIMIT_SHOWN_MAX} 分钟。", task_brief(self.cfg(max_minutes=TIME_LIMIT_SHOWN_MAX)))
        self.assertIn("本次现实时间上限：30 分钟。", task_brief(self.cfg(max_minutes=30)))
        for minutes in (TIME_LIMIT_SHOWN_MAX + 1, 9999999):
            self.assertNotIn("现实时间上限", task_brief(self.cfg(max_minutes=minutes)))

    def test_guide_path_is_short_and_written_once(self):
        cfg = self.cfg()
        with profiles.using(profiles.DEFAULT):
            guide = guide_document(cfg)
            text = runtime_prompt(cfg, guide=guide)
        self.assertEqual(len(guide["version"]), VERSION_CHARS)
        self.assertIsNone(re.search(r"[0-9a-f]{16,}", text), text)
        self.assertEqual(text.count("/workspace/game/instructions/"), 1)
        self.assertIn("同目录的 index.md", text)
        index = guide["documents"]["index.md"]
        self.assertIsNone(re.search(r"[0-9a-f]{16,}", index), index)
        self.assertIn(guide["version"], guide["path"])

    def test_program_reference_is_not_repeated(self):
        cfg = self.cfg()
        with profiles.using(profiles.DEFAULT):
            body = guide_document(cfg)["documents"]["guide.md"]
        self.assertEqual(body.count("具体优化方法与文件／展示接口按需查阅"), 1)

    def test_opened_speedrun_files_are_grouped_by_folder(self):
        allow = (["speedrun/techniques.md", "speedrun/mechanics.md", "speedrun/search_methods.md"]
                 + [f"speedrun/reference/celeste_wiki/page{i}.md" for i in range(17)]
                 + [f"speedrun/reference/strawberry_jam/{n}.md" for n in ("gyms_en", "gyms_zh", "library_en", "library_zh")])
        text = speedrun_allow_rule(SimpleNamespace(kb_allow=allow, mode="explore"))
        self.assertIn("开放了速通知识库的 24 个文件", text)
        for expected in ("techniques.md", "search_methods.md", "reference/celeste_wiki/ 下的 17 个文件",
                         "reference/strawberry_jam/ 下的 4 个文件"):
            self.assertIn(expected, text)
        self.assertNotIn("page3", text)
        small = speedrun_allow_rule(SimpleNamespace(kb_allow=allow[:3] + allow[3:5], mode="explore"))
        self.assertIn("reference/celeste_wiki/page0.md", small)
        self.assertEqual(speedrun_allow_rule(SimpleNamespace(kb_allow=allow, mode="speedrun")), "")


if __name__ == "__main__":
    unittest.main()
