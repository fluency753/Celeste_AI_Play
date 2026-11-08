"""本机设置与提供方账户（docs/开发/设置分层.md 第二步）：

- 本机设置存在 configs/local.console.json 的 machine，按开跑同样的规则校验；第一次读取时依次从选中配置的表单、
  本机最近一次运行的记录取值（升级后行为不变）；手改坏了按默认值；
- 配置的表单不再保存本机设置与用量设置；
- 用量设置存在提供方（configs/local.providers.json 的 usage），保留这个提供方原有的字段，Codex 不适用；
- 主控：命令行没写用量参数时按提供方的设置，写了按命令行；
- 控制台的选项接口带上本机设置与各提供方的用量，迁移选中配置里不是默认值的用量设置。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))     # 主控 import bridge_client

from orchestrator import machine_settings as ms
from orchestrator import profiles


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "configs").mkdir()
        real = Path(__file__).resolve().parents[1] / "configs"
        for name in ("providers.json", "codex.json", "hud.json"):
            (self.root / "configs" / name).write_bytes((real / name).read_bytes())
        for target, value in (("orchestrator.config.REPO_ROOT", self.root),
                              ("orchestrator.config.PROVIDERS", self.root / "configs" / "providers.json"),
                              ("orchestrator.config.LOCAL_PROVIDERS", self.root / "configs" / "local.providers.json")):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(profiles.activate, profiles.LEGACY)

    def prefs(self):
        return json.loads((self.root / "configs" / "local.console.json").read_text(encoding="utf-8"))

    def local_providers(self):
        return json.loads((self.root / "configs" / "local.providers.json").read_text(encoding="utf-8"))

    def run_state(self, name, **state):
        path = self.root / "runs" / "a" / name / "run_state.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"agent": "a", **state}), encoding="utf-8")
        return path


class MachineTests(Fixture):
    def test_first_load_takes_the_selected_profile_then_the_latest_run(self):
        old = self.run_state("20261101-000000", recording_mode="chapter", bridge_port=47000)
        new = self.run_state("20261107-000000", recording_mode="continuous", freeze_chapter_select=False, bridge_port=48000)
        os.utime(old, (time.time() - 100, time.time() - 100))
        pid = profiles.create("旧配置", base=profiles.DEFAULT)
        meta_path = self.root / "local" / "profiles" / pid / "profile.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["form"] = {"mode": "explore", "recording_mode": "continuous", "freeze_chapter_select": False}   # 旧版存的表单
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with patch.object(profiles, "selected", return_value=pid):
            settings = ms.load()
        # 选中配置的表单写了连续录制、不冻结选章；端口它没写：按最近一次运行
        self.assertEqual((settings["recording_mode"], settings["freeze_chapter_select"], settings["bridge_port"]),
                         ("continuous", False, 48000))
        self.assertEqual(settings["hud_port"], 47790)                                   # 两处都没有：默认值
        self.assertEqual(self.prefs()["machine"], settings)
        with patch.object(profiles, "selected", return_value="default"):
            self.assertEqual(ms.load(), settings)                                       # 写下来以后不再迁移

    def test_without_profile_or_runs_defaults_apply(self):
        with patch.object(profiles, "selected", return_value="default"):
            self.assertEqual(ms.load(), ms.defaults())

    def test_save_validates_like_a_launch(self):
        with patch.object(profiles, "selected", return_value="default"):
            saved = ms.save({"recording_mode": "continuous", "ffmpeg": "D:\\ffmpeg\\bin\\ffmpeg.exe", "audio_offset_ms": -40})
        self.assertEqual((saved["recording_mode"], saved["audio_offset_ms"]), ("continuous", -40))
        self.assertEqual(self.prefs()["machine"]["ffmpeg"], "D:\\ffmpeg\\bin\\ffmpeg.exe")
        for bad in ({"bridge_port": 80}, {"recording_mode": "weekly"}, {"mode": "explore"}, {}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ms.save(bad)
        prefs = self.prefs()
        prefs["machine"]["hud_port"] = "坏"
        (self.root / "configs" / "local.console.json").write_text(json.dumps(prefs), encoding="utf-8")
        self.assertEqual(ms.load(), ms.defaults())                                       # 手改坏了：默认值

    def test_profiles_no_longer_store_machine_or_usage_settings(self):
        form = {"mode": "explore", "recording_mode": "continuous", "bridge_port": 48000, "max_usage": 0.9,
                "quota_wait": False, "chapters": [1], "tune": {"explore_act_frames": 300}}
        self.assertEqual(profiles.clean_form(form), {"mode": "explore", "chapters": [1], "tune": {"explore_act_frames": 300},
                                                     "pins": ["chapters"]})


class LastTests(Fixture):
    def test_remembered_values_merge_and_seed_from_the_selected_profile(self):
        pid = profiles.create("旧配置", base=profiles.DEFAULT)
        meta_path = self.root / "local" / "profiles" / pid / "profile.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["form"] = {"mode": "explore", "agent": "series", "live": True, "recording_mode": "continuous"}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        with patch.object(profiles, "selected", return_value=pid):
            self.assertEqual(ms.load_last(), {"agent": "series", "live": True})   # 还没记过：按选中配置，不写文件
        self.assertFalse((self.root / "configs" / "local.console.json").exists())
        ms.save_last({"model": "opus", "agent": "a", "max_minutes": 60})
        # 换到固定了模型与时限的配置：只送来没固定的组，原来记住的模型与时限留着
        self.assertEqual(ms.save_last({"agent": "b", "live": False}),
                         {"model": "opus", "agent": "b", "max_minutes": 60, "live": False})
        self.assertEqual(ms.load_last()["agent"], "b")
        for bad in ({"mode": "explore"}, {"recording_mode": "chapter"}, "x", {"agent": "x" * 60000}):
            with self.subTest(bad=str(bad)[:30]), self.assertRaises(ValueError):
                ms.save_last(bad)

    def test_debug_independent_and_unpinned_groups_are_not_stored_in_profiles(self):
        form = {"mode": "explore", "pins": ["chapters"], "chapters": [2], "agent": "x", "live": True, "record": False,
                "play_speed": 3, "skip_enter": True}
        self.assertEqual(ms.strip(form), {"mode": "explore", "chapters": [2], "pins": ["chapters"]})
        self.assertEqual(ms.pins_of({"pins": ["save", "nope"]}), ["save"])


class UsageTests(Fixture):
    def test_usage_is_written_to_the_provider_and_keeps_its_other_fields(self):
        (self.root / "configs" / "local.providers.json").write_text(json.dumps({"deepseek": {"key_file": "k.key"}}),
                                                                    encoding="utf-8")
        self.assertEqual(ms.usage_for("anthropic"), ms.USAGE_DEFAULTS)
        got = ms.save_usage("anthropic", {"max_usage": 0.9, "quota_wait": False, "quota_wait_max_hours": None})
        self.assertEqual(got, {"max_usage": 0.9, "quota_wait": False, "quota_wait_max_hours": 24})
        ms.save_usage("deepseek", {"quota_wait_max_hours": 6})
        local = self.local_providers()
        self.assertEqual(local["anthropic"], {"usage": {"max_usage": 0.9, "quota_wait": False}})
        self.assertEqual(local["deepseek"], {"key_file": "k.key", "usage": {"quota_wait_max_hours": 6}})
        ms.save_usage("anthropic", {})                                                    # 全部回到默认：不留空项
        self.assertNotIn("anthropic", self.local_providers())
        for provider, values in (("codex", {}), ("anthropic", {"max_usage": 2}), ("anthropic", {"quota_wait": "yes"}),
                                 ("anthropic", {"other": 1}), ("nobody", {})):
            with self.subTest(provider=provider, values=values), self.assertRaises(ValueError):
                ms.save_usage(provider, values)
        self.assertEqual(ms.provider_usage({"usage": {"max_usage": "坏"}}), ms.USAGE_DEFAULTS)

    def test_controller_uses_the_provider_unless_the_command_line_says_otherwise(self):
        from orchestrator.run import parse_args
        ms.save_usage("anthropic", {"max_usage": 0.8, "quota_wait": False, "quota_wait_max_hours": 3})

        def parse(*argv, runtime="host"):
            with patch.object(sys, "argv", ["run", "--agent", "a", "--save", "s", "--runtime", runtime, *argv]):
                return parse_args()[0]
        cfg = parse()
        self.assertEqual((cfg.max_usage, cfg.quota_wait, cfg.quota_wait_max_h), (0.8, False, 3))
        cfg = parse("--max-usage", "0.5", "--quota-wait", "--quota-wait-max-hours", "12")
        self.assertEqual((cfg.max_usage, cfg.quota_wait, cfg.quota_wait_max_h), (0.5, True, 12))
        cfg = parse("--provider", "deepseek", runtime="container")                       # 另一个提供方：它自己的设置
        self.assertEqual((cfg.max_usage, cfg.quota_wait, cfg.quota_wait_max_h), (None, True, 24))
        self.assertFalse(parse("--no-quota-wait").quota_wait)

    def test_console_options_carry_machine_and_usage_and_migrate_profile_usage(self):
        from overlay import console_api
        root = self.root / "local" / "profiles"
        pid = profiles.create("我的", base=profiles.DEFAULT)
        meta = json.loads((root / pid / "profile.json").read_text(encoding="utf-8"))
        meta["form"] = {"engine": "claude", "provider": "deepseek", "quota_wait": False, "recording_mode": "continuous"}
        (root / pid / "profile.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")   # 旧版存的表单
        profiles.select(pid)
        payload = console_api.machine_payload()
        self.assertEqual(payload["machine"]["recording_mode"], "continuous")
        self.assertEqual(payload["provider_usage"]["deepseek"]["quota_wait"], False)
        self.assertEqual(payload["provider_usage"]["anthropic"], ms.USAGE_DEFAULTS)
        self.assertEqual(self.local_providers()["deepseek"]["usage"], {"quota_wait": False})
        self.assertEqual(console_api.save_machine({"keep_obs_open": True})["machine"]["keep_obs_open"], True)
        self.assertEqual(console_api.save_provider_usage({"provider": "anthropic", "usage": {"max_usage": 0.7}})["usage"]["max_usage"], 0.7)
        with self.assertRaises(ValueError):
            console_api.save_provider_usage({"provider": "anthropic", "usage": {}, "x": 1})


if __name__ == "__main__":
    unittest.main()
