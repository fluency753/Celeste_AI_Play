"""控制台的提示词预览与实际开跑一致，提示词里没有冗余的哈希与重复路径：

- 同一组运行设置，launch_options.config_for（控制台预览）与 build → run.parse_args（控制台开跑的命令）得到的设置与提示词逐字相同；
  控制台能选的组合都能通过主控的启动校验（例如开着计划外地图时不再带上旧的 --no-allow-chapter-reset）；
- 现实时间上限不超过一天才写进启动提示词，9999999 这类"不限时"不写，命令行里也不写成 1e+07；
- 工作区操作指南的目录名是短哈希、只出现一次，索引不列每节的内容哈希；
- 探索模式开放的速通资料按目录归并，不把长路径逐个列出；
- 分层合并（docs/开发/设置分层.md）：控制台页面按配置固定的项、记住的上次填写、本机设置、提供方用量拼出运行设置，
  拼出的值各取对的那一层，预览与开跑得到的设置与提示词仍逐字相同；模型档案的思考字数在两边一致（两种执行器）。
"""
import asyncio
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse
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


class LayeredPreviewTests(unittest.TestCase):
    """第五步：同一组分层输入，预览与开跑得到的设置与提示词逐字相同。"""

    def setUp(self):
        self.addCleanup(profiles.activate, profiles.LEGACY)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        real = json.loads((Path(__file__).resolve().parents[1] / "configs" / "providers.json").read_text(encoding="utf-8"))
        models = [dict(row, **({"thought": {"max_hanzi": 40, "temp_max_hanzi": 90}} if row["id"] == "opus" else {}))
                  for row in real["anthropic"]["models"]]
        (root / "local.providers.json").write_text(json.dumps({"anthropic": {"models": models}}), encoding="utf-8")
        (root / "codex.json").write_text(json.dumps({"models": [{"id": "gpt-5.5", "thought": {"max_hanzi": 50}}]}),
                                         encoding="utf-8")
        for target, value in (("orchestrator.config.LOCAL_PROVIDERS", root / "local.providers.json"),
                              ("orchestrator.model_profile.codex_paths",
                               lambda: (root / "codex.json", root / "local.codex.json"))):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def same(self, options):
        """开跑（build → run.parse_args）与预览（config_for）的设置与提示词逐字相同；返回提示词。"""
        launched, argv = PreviewMatchesLaunchTests.launch(self, options)
        preview = launch_options.config_for(options, profiles.DEFAULT)
        for field in FIELDS:
            if field == "chapters":
                self.assertEqual(preview.chapters or [preview.chapter], launched.chapters or [launched.chapter])
                continue
            self.assertEqual(getattr(preview, field, None), getattr(launched, field, None), field)
        with profiles.using(profiles.DEFAULT):
            texts = ["".join(p.sep + p.text for p in prompt_parts(cfg)) + runtime_prompt(cfg, guide=guide_document(cfg))
                     for cfg in (preview, launched)]
        self.assertEqual(texts[0], texts[1])
        return texts[0], argv

    def test_model_profile_thought_limits_reach_both(self):
        text, _ = self.same({"agent": "parity", "save": SAVE, "mode": "explore", "formal_thought": True})
        self.assertIn("汉字不超过 40 个", text)                                    # 模型档案
        self.assertIn("汉字不超过 90 个", text)
        text, _ = self.same({"agent": "parity", "save": SAVE, "mode": "explore", "formal_thought": True,
                             "tune": {"thought_max_hanzi": 25}})
        self.assertIn("汉字不超过 25 个", text)                                    # 可调参数改过的优先
        self.assertNotIn("汉字不超过 40 个", text)
        text, _ = self.same({"agent": "parity", "save": SAVE, "mode": "explore", "engine": "codex", "provider": "codex",
                             "model": "gpt-5.5"})
        self.assertIn("汉字不超过 50 个", text)                                    # Codex：写了才限

    def test_console_page_layers_preview_like_launch(self):
        options = asyncio.run(self.page_options())
        if options is None:
            return
        self.assertEqual((options["engine"], options["model"], options["effort"], options["chapters"]),
                         ("claude", "opus", "high", [1]))                          # 配置固定的
        self.assertEqual((options["agent"], options["max_minutes"], options["save"]),
                         ("dev-last", 30, {"name": "test", "slot": 7, "create": False}))   # 没固定：上次填写的
        self.assertEqual((options["live"], options["record"]), (False, True))      # 直播与录屏：各自记住的
        self.assertEqual((options["bridge_port"], options["recording_mode"]), (47775, "continuous"))   # 本机设置
        self.assertEqual(options["tune"], {"explore_act_frames": 300, "agent_cpus": 1})   # 本机的核数，不按旧配置的 2
        self.assertEqual((options["max_usage"], options["quota_wait"]), (0.9, False))      # 提供方账户
        self.assertFalse(options["no_agent"] or options["skip_enter"] or options["record_marker"])   # 调试项：默认
        text, argv = self.same(options)
        self.assertIn("汉字不超过 40 个", text)
        for expected in (["--max-usage", "0.9"], ["--no-quota-wait"], ["--bridge-port", "47775"],
                         ["--tune", "agent_cpus=1"]):
            joined = " ".join(argv)
            self.assertIn(" ".join(expected), joined)

    async def page_options(self):
        try:
            from playwright.async_api import async_playwright, expect
        except ImportError:
            self.skipTest("Playwright is needed to check the console")
        from orchestrator.tuning import catalog
        html = (Path(__file__).resolve().parents[1] / "overlay" / "console.html").read_text(encoding="utf-8")
        tunables = catalog()
        form = {"engine": "claude", "provider": "anthropic", "model": "opus", "effort": "high", "mode": "explore",
                "chapters": [1], "formal_thought": True, "tune": {"explore_act_frames": 300, "agent_cpus": 2},
                "pins": ["model", "chapters"]}
        options = {
            "runtimes": [{"id": "claude:anthropic", "engine": "claude", "provider": "anthropic", "label": "Claude"}],
            "defaults": launch_options.DEFAULTS, "saves": [{"name": "test", "slot": 7}],
            "chapters": [{"id": 0, "label": "序章"}, {"id": 1, "label": "1A"}], "sides": [], "checkpoints": {},
            "tunables": tunables, "tunable_groups": list(dict.fromkeys(t["group"] for t in tunables)),
            "maps": [{"id": "celeste", "label": "蔚蓝", "enabled": True}],
            "machine": {"bridge_port": 47775, "recording_mode": "continuous", "agent_cpus": 1},
            "provider_usage": {"anthropic": {"max_usage": 0.9, "quota_wait": False, "quota_wait_max_hours": 24}},
            "last": {"agent": "dev-last", "max_minutes": 30, "live": False, "record": True,
                     "save": {"name": "test", "slot": 7, "create": False}, "model": "haiku", "chapters": [0]},
            "profiles": {"stored": True, "selected": "mine", "profiles": [{"id": "mine", "name": "我的"}],
                         "current": {"id": "mine", "name": "我的", "readonly": False, "form": form}},
        }
        responses = {"/api/console/options": options, "/api/console/kb": {"documents": [], "deny": [], "allow": []},
                     "/api/console/runs": [], "/api/console/last": {"last": {}},
                     "/api/console/status": {"game": {}, "obs": {}, "orchestrator": {"running": False}},
                     "/api/models": {"models": [{"id": m, "efforts": ["high", "max"], "default_effort": "high"}
                                                for m in ("opus", "haiku")]}}
        previews = []

        async def handle(route):
            url = urlparse(route.request.url)
            if url.netloc != "console.test":
                return await route.abort()
            if url.path == "/console":
                return await route.fulfill(content_type="text/html; charset=utf-8", body=html)
            if url.path == "/api/console/preview":
                previews.append(route.request.post_data_json["options"])
            if url.path in ("/api/console/preview", "/api/console/prompt/preview"):
                return await route.fulfill(json={"command": "预览", "text": "", "parts": [], "overridden": []})
            return await route.fulfill(json=responses.get(url.path, {}))

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as error:  # noqa: BLE001
                self.skipTest(f"Chromium unavailable: {error}")
            try:
                page = await browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.route("**/*", handle)
                await page.goto("http://console.test/console#run", wait_until="domcontentloaded")
                await expect(page.locator("#model")).to_have_value("opus")
                await expect(page.locator("#agent")).to_have_value("dev-last")
                await page.wait_for_function("() => !document.querySelector('#command').textContent.includes('读取')")
                await page.wait_for_timeout(600)
                result = await page.evaluate("formOptions()")
                self.assertTrue(previews)
                self.assertEqual(previews[-1], result)                             # 命令预览用的就是开跑会发的
                self.assertEqual(errors, [])
                return result
            finally:
                await browser.close()


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
