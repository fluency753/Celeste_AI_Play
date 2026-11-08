"""模型档案（docs/开发/设置分层.md 第一步）：

- Codex 的提供方信息来自 configs/codex.json（本机 configs/local.codex.json 按顶层字段覆盖），文件缺失或写坏时用内置值；
- 压缩窗口的优先顺序：本次运行 > 模型行 > 提供方 > 节省上下文的 500000 > 不传；
- 模型列表把模型行的 autocompact、context_limit 与提供方的 autocompact 交给控制台；Codex 的显示名与上限按模型 ID 叠加；
- HUD 用模型档案的显示名与上下文上限；笔记提醒的窗口也取模型档案的上限；
- 续跑沿用原运行显式设的压缩窗口。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.config import RunConfig, autocompact_choice
from orchestrator.model_profile import CODEX_DEFAULTS, codex_settings, hud_fields, model_label
from orchestrator.runtimes import runtime
from overlay import modhud

REPO = Path(__file__).resolve().parents[1]
HUD = json.loads((REPO / "configs" / "hud.json").read_text(encoding="utf-8"))


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")


class CodexSettingsTests(unittest.TestCase):
    def test_repository_file_matches_the_built_in_values(self):
        """入库的 configs/codex.json 与原来写在 runtimes.py 里的数值相同：换成数据不改变行为。"""
        self.assertEqual(codex_settings(), CODEX_DEFAULTS)
        cfg = runtime("codex").configure(RunConfig(agent="a", save="s", model="gpt-5.5", engine="codex"))
        self.assertEqual(cfg.provider_info["allowed_domains"], ["chatgpt.com", "auth.openai.com", "api.openai.com"])
        self.assertTrue(cfg.images)
        self.assertEqual(runtime("codex").effort_default(), "medium")
        self.assertEqual(runtime("claude").effort_default(), "high")

    def test_local_override_and_broken_values(self):
        with tempfile.TemporaryDirectory() as folder, patch("orchestrator.config.REPO_ROOT", Path(folder)):
            root = Path(folder) / "configs"
            self.assertEqual(codex_settings(), CODEX_DEFAULTS)                       # 文件都没有：内置值
            write(root / "codex.json", {"label": "Codex", "default_effort": "high",
                                        "models": [{"id": "gpt-5.5", "label": "GPT-5.5", "context_limit": 250000}, "坏行"]})
            write(root / "local.codex.json", {"allowed_domains": ["chatgpt.com", "api.openai.com", "example.org"],
                                              "images": "yes", "label": ""})
            got = codex_settings()
            self.assertEqual(got["default_effort"], "high")
            self.assertEqual(got["allowed_domains"], ["chatgpt.com", "api.openai.com", "example.org"])
            self.assertTrue(got["images"])                                          # 类型不对：用内置值
            self.assertEqual(got["label"], "Codex")
            self.assertEqual(got["models"], [{"id": "gpt-5.5", "label": "GPT-5.5", "context_limit": 250000}])
            write(root / "local.codex.json", {"allowed_domains": ["https://evil/path"]})
            self.assertEqual(codex_settings()["allowed_domains"], CODEX_DEFAULTS["allowed_domains"])
            write(root / "local.codex.json", "{不是 JSON")
            self.assertEqual(codex_settings()["default_effort"], "high")            # 读不出的本机文件跳过


class AutocompactTests(unittest.TestCase):
    def cfg(self, **kw):
        cfg = RunConfig(agent="a", save="s", model="haiku", **kw)
        cfg.provider_info = {"models": [{"id": "haiku", "autocompact": "200000"}, {"id": "opus"}], "autocompact": "786432"}
        return cfg

    def test_precedence_run_model_provider_switch(self):
        cfg = self.cfg(save_context=True)
        self.assertEqual(autocompact_choice(cfg), ("200000", "model"))
        cfg.autocompact = "auto"
        self.assertEqual(autocompact_choice(cfg), ("auto", "run"))
        cfg.autocompact, cfg.model = None, "opus"                    # 这个模型行没写：用提供方的
        self.assertEqual(autocompact_choice(cfg), ("786432", "provider"))
        cfg.provider_info.pop("autocompact")
        self.assertEqual(autocompact_choice(cfg), ("500000", "save_context"))
        cfg.save_context = False
        self.assertEqual(autocompact_choice(cfg), (None, None))
        cfg.model = "haiku"
        cfg.provider_info["models"][0]["autocompact"] = "50000"       # 超出范围的写法不用
        self.assertEqual(autocompact_choice(cfg), (None, None))


class ModelListTests(unittest.TestCase):
    def test_shipped_labels_are_display_names(self):
        rows = {m["id"]: m for m in runtime("claude").models("anthropic")["models"]}
        self.assertEqual((rows["opus"]["label"], rows["fable"]["label"], rows["haiku"]["label"]),
                         ("Opus 5.5", "Fable 5.1", "Haiku 5.5"))
        deepseek = runtime("claude").models("deepseek")
        self.assertEqual(deepseek["autocompact"], "786432")
        self.assertEqual(deepseek["models"][0]["label"], "DeepSeek V4.1 Flash")

    def test_model_rows_carry_their_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            repo, local = Path(folder) / "providers.json", Path(folder) / "local.providers.json"
            write(repo, {"demo": {"label": "Demo", "autocompact": "auto", "models": [
                {"id": "big", "efforts": ["high"], "autocompact": "400000", "context_limit": 380000},
                {"id": "small", "efforts": ["low"], "autocompact": "bad", "context_limit": -1}]}})
            with patch("orchestrator.config.PROVIDERS", repo), patch("orchestrator.config.LOCAL_PROVIDERS", local):
                data = runtime("claude").models("demo")
        big, small = data["models"]
        self.assertEqual((big["autocompact"], big["context_limit"]), ("400000", 380000))
        self.assertNotIn("autocompact", small)
        self.assertNotIn("context_limit", small)
        self.assertEqual(data["autocompact"], "auto")

    def test_codex_overrides_are_layered_on_the_catalog(self):
        catalog = {"models": [{"id": "gpt-5.5", "label": "gpt-5.5", "efforts": ["low", "medium"], "default_effort": "medium"},
                              {"id": "gpt-5.5-mini", "label": "GPT-5.5 mini", "efforts": ["low"], "default_effort": "low"}],
                   "default_model": "gpt-5.5", "source": "codex_model_list"}
        settings = {**CODEX_DEFAULTS, "models": [{"id": "gpt-5.5", "label": "GPT-5.5", "context_limit": 250000},
                                                 {"id": "retired", "label": "旧模型"}]}
        with patch("orchestrator.model_catalog.load_models", return_value=json.loads(json.dumps(catalog))), \
                patch("orchestrator.model_profile.codex_settings", return_value=settings):
            rows = runtime("codex").models()["models"]
        self.assertEqual([r["id"] for r in rows], ["gpt-5.5", "gpt-5.5-mini"])   # 目录里没有的 ID 不会冒出来
        self.assertEqual((rows[0]["label"], rows[0]["context_limit"]), ("GPT-5.5", 250000))
        self.assertEqual(rows[1]["label"], "GPT-5.5 mini")
        self.assertNotIn("context_limit", rows[1])


class HudFieldTests(unittest.TestCase):
    def test_fields_for_both_executors(self):
        claude = RunConfig(agent="a", save="s", model="opus", save_context=True)
        claude.provider_info = {"models": [{"id": "opus", "label": "Opus 5.5", "context_limit": 967000}]}
        self.assertEqual(hud_fields(claude), {"model_label": "Opus 5.5", "context_limit": 967000, "compact_window": 500000})
        codex = RunConfig(agent="a", save="s", model="", engine="codex", save_context=True)
        codex.provider_info = {**CODEX_DEFAULTS, "models": [{"id": "gpt-5.5", "context_limit": 250000}]}
        self.assertEqual(hud_fields(codex, "gpt-5.5", "GPT-5.5"),            # Codex 没有 Claude 的压缩窗口
                         {"model_label": "GPT-5.5", "context_limit": 250000, "compact_window": None})
        self.assertIsNone(model_label(codex, "other"))

    def test_hud_uses_profile_label_and_limit(self):
        m = {"agent": "dev-opus", "model": "claude-opus-4-8", "model_label": "Opus 5.5"}
        self.assertEqual(modhud.display_name(m, HUD), "Opus 5.5")
        self.assertEqual(modhud.display_name({"agent": "x", "model": "deepseek-flash[1m]"}, HUD), "DeepSeek V4.1 Flash")
        self.assertEqual(modhud.context_limit({"context_window": 1000000, "context_limit": 400000}, HUD), 400000)

    def test_claude_runner_publishes_the_profile_and_uses_it_for_note_reminders(self):
        from calibration.note_reminders_test import runner_fixture
        with runner_fixture('claude') as (runner, session, path):
            self.assertIsNone(runner.metrics.state.model_label)      # 模型行没有：HUD 按原来的 hud.json 名字
            runner.cfg.provider_info["models"] = [{"id": "stub", "label": "Stub 1", "context_limit": 300000}]
            runner.metrics.update(context_window=1000000)
            self.assertEqual(runner._notes_window(), 300000)


class ResumeTests(unittest.TestCase):
    def make_run(self, root: Path, **extra) -> Path:
        run = root / "runs" / "a" / "20261108-010000"
        run.mkdir(parents=True)
        state = {"agent": "a", "save": "s", "engine": "claude", "model": "opus", "effort": "high", "max_minutes": 600,
                 "runtime": "host", "sim": False, "record": False, "mode": "explore", "knowledge_profile": "explore",
                 "all_chapters": [1], "plan_pieces": [[1, 0, None]], "piece_pending": False, "session_id": "sess",
                 "tuning": {}, **extra}
        write(run / "run_state.json", state)
        write(run / "metrics.json", {"splits": [], "effort": "high"})
        return run

    def parse(self, root: Path, *argv):
        from orchestrator.run import parse_args
        with patch("orchestrator.config.REPO_ROOT", root), patch.object(sys, "argv", ["run", "--agent", "a", "--save", "s", *argv]):
            return parse_args()

    def test_explicit_window_is_recorded_and_kept_on_resume(self):
        from orchestrator.run import write_run_state
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, args = self.parse(root, "--resume", str(self.make_run(root, autocompact="300000")))
            self.assertEqual(cfg.autocompact, "300000")
            out = root / "out"
            out.mkdir()
            write_run_state(out, cfg, "sess", args.previous)
            self.assertEqual(json.loads((out / "run_state.json").read_text(encoding="utf-8"))["autocompact"], "300000")
            cfg, _ = self.parse(root, "--resume", str(root / "runs" / "a" / "20261108-010000"), "--autocompact", "auto")
            self.assertEqual(cfg.autocompact, "auto")                           # 续跑时另外指定的优先
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg, _ = self.parse(root, "--resume", str(self.make_run(root)))    # 原运行没设过：仍按默认取
            self.assertIsNone(cfg.autocompact)


if __name__ == "__main__":
    unittest.main()
