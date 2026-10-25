"""控制台"提示词"页：分块预览与全文一致、本机修改（local/prompts/）的读写与恢复。"""
import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import prompting
from orchestrator.config import RunConfig
from orchestrator.prompting import play_prompt, prompt_parts


def cfg(**kw):
    c = RunConfig(agent="a", save="s", model="opus")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class PromptPartsTests(unittest.TestCase):
    def test_local_target_instruction_respects_available_simulation(self):
        for enabled in (False, True):
            c = cfg(mode='explore',engine='codex',sim=False,sim_assist=enabled)
            rule = prompting.local_target_rule(c)
            self.assertTrue(rule)
            self.assertEqual('限额模拟' in rule,enabled)
            self.assertIn(rule,play_prompt(c))
        self.assertEqual(prompting.local_target_rule(cfg(engine='claude',sim_assist=True)),'')

    def test_parts_join_to_full_prompt(self):
        for mode, sim, live, assist in itertools.product(("speedrun", "explore"), (True, False), (True, False),
                                                         (True, False)):
            c = cfg(mode=mode, sim=sim, live=live, sim_assist=assist and mode == "explore",
                    runtime="container", chapters=[1, 2] if mode == "explore" else [])
            parts = prompt_parts(c)
            self.assertEqual("".join(p.sep + p.text for p in parts), play_prompt(c))
            for p in parts:
                self.assertNotIn("{interface}", p.text)
                if p.source:
                    self.assertTrue((prompting.PROMPTS / p.source).is_file(), p.source)

    def test_tool_list_matches_registered_tools(self):
        from orchestrator.modes import game_tools
        for kw in (dict(sim=True), dict(sim=False), dict(sim=True, live=True), dict(cutscene_mode="agent"),
                   dict(sim=False, runtime="host")):
            c = cfg(mode="speedrun", runtime=kw.pop("runtime", "container"), **kw)
            text = play_prompt(c)
            line = next(l for l in text.splitlines() if l.startswith("游戏工具有"))
            names = game_tools(c)
            self.assertEqual(line.count("`") // 2, len(names), line)
            for name in names:
                self.assertIn(f"`{name}`", line)
            auto = "act` 返回时 Madeline 已经可以操作" in text
            self.assertEqual(auto, "cutscene" not in names)      # 有剧情工具时不再说"剧情一律自动跳过"

    def test_interface_is_spliced_in(self):
        parts = prompt_parts(cfg(mode="speedrun", sim=True, runtime="container"))
        sources = [p.source for p in parts]
        k = sources.index("interface_zh.md")
        self.assertEqual(sources[k - 1], sources[k + 1])          # 系统提示词在接口前后各一段
        self.assertTrue(parts[k + 1].label.endswith("（接上）"))


class PromptOverrideTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.overrides = Path(self.folder.name) / "prompts"
        self.patch = patch.object(prompting, "OVERRIDES", self.overrides)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.folder.cleanup()

    def test_override_changes_prompt_and_run_state(self):
        from orchestrator.run import prompt_override_hashes
        c = cfg(mode="explore", runtime="container")
        before = play_prompt(c)
        self.assertEqual(prompting.overridden_parts(), [])
        self.overrides.mkdir()
        repo = (prompting.PROMPTS / "explore_zh.md").read_text(encoding="utf-8")
        (self.overrides / "explore_zh.md").write_text(repo + "\n本机加的一行。", encoding="utf-8")
        (self.overrides / "unknown.md").write_text("不是提示词文件", encoding="utf-8")
        self.assertIn("本机加的一行。", play_prompt(c))
        self.assertNotEqual(play_prompt(c), before)
        self.assertEqual(prompting.overridden_parts(), ["explore_zh.md"])
        self.assertEqual(list(prompt_override_hashes()), ["explore_zh.md"])
        part = next(p for p in prompt_parts(c) if p.source == "explore_zh.md")
        self.assertIn("本机加的一行。", part.text)

    def test_console_write_reset_and_guards(self):
        from overlay import console_api
        with patch.object(console_api, "orchestrator_status", return_value={"running": False}):
            doc = console_api.prompt_read("hud_zh.md")
            self.assertFalse(doc["overridden"])
            self.assertEqual(doc["text"], doc["repo_text"])
            with self.assertRaisesRegex(ValueError, "没有这个提示词文件"):
                console_api.prompt_read("../config.py")
            with self.assertRaisesRegex(ValueError, "没有这个提示词文件"):
                console_api.prompt_write("nope.md", "x", doc["version"])
            with self.assertRaisesRegex(ValueError, "被改过"):
                console_api.prompt_write("hud_zh.md", "x", "0" * 16)

            saved = console_api.prompt_write("hud_zh.md", doc["text"].replace("\n", "\r\n") + "\r\n加一行", doc["version"])
            self.assertTrue(saved["overridden"])
            stored = (self.overrides / "hud_zh.md").read_text(encoding="utf-8")
            self.assertNotIn("\r", stored)
            self.assertTrue(stored.endswith("加一行"))
            self.assertEqual(prompting.read_part("hud_zh.md"), stored)

            # 改回与仓库相同 = 删掉本机修改
            back = console_api.prompt_write("hud_zh.md", doc["repo_text"], saved["version"])
            self.assertFalse(back["overridden"])
            self.assertFalse((self.overrides / "hud_zh.md").exists())

            # 删掉占位符会提示
            sim = console_api.prompt_read("system_sim_zh.md")
            names = [p["name"] for p in sim["placeholders"]]
            self.assertIn("{interface}", names)
            result = console_api.prompt_write("system_sim_zh.md", sim["text"].replace("{interface}", ""), sim["version"])
            self.assertIn("{interface}", result["missing_placeholders"])
            self.assertEqual(console_api.prompt_reset("system_sim_zh.md")["overridden"], False)
            self.assertFalse((self.overrides / "system_sim_zh.md").exists())

        with patch.object(console_api, "orchestrator_status", return_value={"running": True}):
            doc = console_api.prompt_read("hud_zh.md")
            with self.assertRaisesRegex(ValueError, "主控在运行"):
                console_api.prompt_write("hud_zh.md", doc["text"] + "x", doc["version"])
            with self.assertRaisesRegex(ValueError, "主控在运行"):
                console_api.prompt_reset("hud_zh.md")


class PromptPreviewTests(unittest.TestCase):
    def test_preview_matches_what_executors_send(self):
        from orchestrator.codex_agent import codex_prompt
        from orchestrator.launch_options import config_for
        from overlay import console_api
        with patch.object(prompting, "OVERRIDES", Path(tempfile.gettempdir()) / "no-such-prompts-dir"):
            claude = console_api.prompt_preview({"mode": "explore"})       # 名字与存档没填也能预览
            self.assertTrue(claude["executor"].startswith("Claude Code"))
            self.assertEqual(claude["text"], "".join(p["sep"] + p["text"] for p in claude["parts"]))
            self.assertEqual(claude["overridden"], [])
            if claude["first_message"]:
                self.assertIn("/kb/README.md", claude["first_message"])

            options = {"mode": "explore", "engine": "codex", "agent": "a", "save": {"name": "s"}}
            codex = console_api.prompt_preview(options)
            self.assertEqual(codex["parts"][-1]["label"], "原生 Codex 工具映射（自动加）")
            self.assertEqual(codex["text"], codex_prompt(config_for(options)))

            speed = console_api.prompt_preview({"mode": "speedrun", "agent": "a", "save": {"name": "s"}})
            self.assertTrue(any(p["source"] == "hud_zh.md" for p in speed["parts"]))


if __name__ == "__main__":
    unittest.main()
