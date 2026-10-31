"""运行配置（orchestrator/profiles.py 与控制台的"配置"）：

- 不写 --profile 时（命令行开跑、没记配置的旧运行续跑）用本机原有设置：路径、命令与提示词都与没有配置功能时相同；
- 新建、复制、保存、改名、删除、选中；默认配置只读；
- 配置里的提示词修改、个人追加要求与知识库修改只在用这份配置时生效，知识库修改不改仓库里的文件；
- 控制台按选中的配置读写，不影响主控进程用着的那份。
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))     # 主控 import bridge_client

from orchestrator import kb_access, personal_prompts, profiles, prompting
from orchestrator.config import RunConfig
from orchestrator.launch_options import build
from orchestrator.startup_prompt import runtime_prompt

SAVE = {"name": "test", "slot": 7, "create": False}


def cfg(**kw):
    c = RunConfig(agent="a", save="s", model="opus")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


class ProfileFixture(unittest.TestCase):
    """临时的仓库根目录：local/、configs/ 与一个小知识库都在里面；提示词仍用仓库里的 orchestrator/prompts/。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        kb = self.root / "kb"
        (kb / "explore").mkdir(parents=True)
        for rel, text in {"README.md": "a\n", "techniques.md": "b\n", "explore/README.md": "c\n",
                          "explore/tools.md": "d\n"}.items():
            (kb / rel).write_text(text, encoding="utf-8")
        self.kb = kb
        from overlay import console_api
        self.console = console_api
        for p in (patch("orchestrator.config.REPO_ROOT", self.root), patch.object(console_api, "REPO_ROOT", self.root),
                  patch.object(prompting, "OVERRIDES", self.root / "local" / "prompts"),
                  patch.object(personal_prompts, "STORE", self.root / "local" / "personal_prompts.json"),
                  patch.object(console_api, "PREFS", self.root / "configs" / "local.console.json"),
                  patch.object(console_api, "orchestrator_status", return_value={"running": False, "run": None})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(profiles.activate, profiles.LEGACY)

    def legacy_override(self, name="runtime_zh.md", line="本机原有设置的修改行。"):
        path = prompting.OVERRIDES / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((prompting.PROMPTS / name).read_text(encoding="utf-8") + "\n" + line + "\n", encoding="utf-8")
        return line


class LegacyTests(ProfileFixture):
    def test_without_profile_everything_is_the_old_local_files(self):
        self.assertEqual(profiles.active(), profiles.LEGACY)
        self.assertEqual(profiles.prompts_dir(), prompting.OVERRIDES)
        self.assertEqual(prompting.overrides_dir(), prompting.OVERRIDES)
        self.assertEqual(profiles.personal_store(), personal_prompts.STORE)
        self.assertEqual(personal_prompts.store_path(), personal_prompts.STORE)
        self.assertEqual(profiles.kb_edits_dir(), self.root / "local" / "kb_edits")
        self.assertEqual(profiles.kb_overlay_for(), "")
        self.assertEqual(profiles.state_profile(None), profiles.LEGACY)
        self.assertEqual(profiles.state_profile({"agent": "old"}), profiles.LEGACY)   # 没记配置的旧运行
        c = cfg()
        self.assertEqual((c.profile, c.kb_overlay, c.kb_view), ("local", "", False))
        self.assertEqual(c.kb_dir, c.kb_source)                                          # 仍直接挂知识库本身

    def test_command_without_profile_is_unchanged(self):
        plain = build({"agent": "dev", "save": SAVE})
        self.assertNotIn("--profile", plain)
        self.assertEqual(build({"agent": "dev", "save": SAVE}, profile=profiles.LEGACY), plain)
        self.assertEqual(build({"agent": "dev", "save": SAVE}, profile=profiles.DEFAULT), plain + ["--profile", "default"])
        pid = profiles.create("系列", profiles.DEFAULT)
        self.assertEqual(build({"agent": "dev", "save": SAVE}, profile=pid)[-2:], ["--profile", pid])
        with self.assertRaises(ValueError):
            build({"agent": "dev", "save": SAVE}, profile="nope")

    def test_run_uses_legacy_unless_told(self):
        from orchestrator.run import use_profile
        c = cfg()
        use_profile(c, None, None)
        self.assertEqual((c.profile, c.kb_overlay, profiles.active()), ("local", "", "local"))
        pid = profiles.create("系列", profiles.DEFAULT)
        use_profile(c, {"state": {"profile": pid}}, None)                 # 续跑沿用原运行
        self.assertEqual((c.profile, profiles.active()), (pid, pid))
        use_profile(c, {"state": {"profile": pid}}, "default")            # 命令行写了就按命令行
        self.assertEqual(c.profile, "default")
        use_profile(c, {"state": {}}, None)                               # 没记配置的旧运行
        self.assertEqual(c.profile, "local")
        with self.assertRaises(SystemExit):
            use_profile(c, None, "missing")

    def test_legacy_prompt_text_matches_old_lookup(self):
        """本机原有设置下生成的启动提示词，与直接按 local/prompts/、local/personal_prompts.json 生成的完全相同。"""
        line = self.legacy_override()
        personal_prompts.STORE.write_text(json.dumps({"version": 1, "scopes": {"all": "原有的个人追加要求"}},
                                                     ensure_ascii=False), encoding="utf-8")
        c = cfg(mode="explore", knowledge_profile="explore")
        text = runtime_prompt(c)
        self.assertIn(line, text)
        self.assertIn("原有的个人追加要求", text)
        with profiles.using(profiles.LEGACY):
            self.assertEqual(runtime_prompt(c), text)
        # 换了配置不带这些；换回来又一样
        with profiles.using(profiles.DEFAULT):
            other = runtime_prompt(c)
        self.assertNotIn(line, other)
        self.assertNotIn("原有的个人追加要求", other)
        self.assertEqual(runtime_prompt(c), text)


class ProfileStoreTests(ProfileFixture):
    def test_create_copy_save_rename_delete(self):
        line = self.legacy_override()
        personal_prompts.STORE.write_text(json.dumps({"version": 1, "scopes": {"all": "原有"}}, ensure_ascii=False),
                                          encoding="utf-8")
        (self.root / "local" / "kb_edits" / "explore").mkdir(parents=True)
        (self.root / "local" / "kb_edits" / "explore" / "tools.md").write_text("改过\n", encoding="utf-8")
        self.assertTrue(profiles.legacy_customized())
        self.assertEqual(profiles.selected(), profiles.LEGACY)        # 没选过：原有设置里有东西就用它

        pid = profiles.create("复制", profiles.LEGACY, {"agent": "x", "kb_deny": ["忽略"]},
                              {"kb_deny": {"explore": ["tools.md"]}})
        self.assertTrue((profiles.root() / pid / "prompts" / "runtime_zh.md").is_file())
        self.assertTrue((profiles.root() / pid / "personal_prompts.json").is_file())
        self.assertTrue((profiles.root() / pid / "kb" / "explore" / "tools.md").is_file())
        m = profiles.meta(pid)
        self.assertEqual((m["name"], m["form"], m["kb_deny"]["explore"]), ("复制", {"agent": "x"}, ["tools.md"]))
        s = profiles.summary(pid)
        self.assertEqual((s["prompts"], s["personal"], s["kb"], s["form"]), (["runtime_zh.md"], 1, 1, True))

        fresh = profiles.create("从默认", profiles.DEFAULT)
        self.assertEqual(profiles.summary(fresh), {"prompts": [], "personal": 0, "kb": 0, "form": False})
        self.assertEqual([r["id"] for r in profiles.listing()["profiles"]], ["default", "local", *sorted([pid, fresh])])

        profiles.save_form(pid, {"agent": "y"})
        self.assertEqual(profiles.meta(pid)["form"], {"agent": "y"})
        profiles.rename(pid, "  新名字 ")
        self.assertEqual(profiles.meta(pid)["name"], "新名字")
        for bad in ("", " ", "x" * 41, "a\nb"):
            with self.assertRaises(ValueError):
                profiles.rename(pid, bad)

        profiles.select(pid)
        self.assertEqual(profiles.selected(), pid)
        profiles.delete(pid)
        self.assertFalse((profiles.root() / pid).exists())
        self.assertEqual(profiles.selected(), profiles.LEGACY)     # 选中的被删了：回到按情况挑的那份
        with self.assertRaises(ValueError):
            profiles.check(pid)

    def test_builtin_rules(self):
        self.assertEqual(profiles.selected(), profiles.DEFAULT)    # 本机原有设置是空的：默认配置
        with self.assertRaisesRegex(ValueError, "默认配置"):
            profiles.save_form(profiles.DEFAULT, {})
        for pid in (profiles.DEFAULT, profiles.LEGACY):
            with self.assertRaises(ValueError):
                profiles.delete(pid)
            with self.assertRaises(ValueError):
                profiles.rename(pid, "x")
        profiles.save_form(profiles.LEGACY, {"agent": "a"})        # 本机原有设置可以存表单设置
        self.assertEqual(profiles.meta(profiles.LEGACY)["form"], {"agent": "a"})
        for bad in ("../x", "a/b", "", None, 3, "x" * 41):
            with self.assertRaises(ValueError):
                profiles.check(bad)
        with self.assertRaises(ValueError):
            profiles.save_form(profiles.LEGACY, {"big": "x" * 300_000})

    def test_using_is_per_thread(self):
        pid = profiles.create("系列", profiles.DEFAULT)
        profiles.activate(pid)                                     # 主控进程用这一份
        seen = {}

        def console_request():
            with profiles.using(profiles.DEFAULT):
                seen["inside"] = profiles.active()
            seen["after"] = profiles.active()

        with profiles.using(profiles.LEGACY):
            worker = threading.Thread(target=console_request)
            worker.start(); worker.join()
            self.assertEqual(profiles.active(), profiles.LEGACY)
        self.assertEqual(seen, {"inside": "default", "after": pid})
        self.assertEqual(profiles.active(), pid)


class OverlayTests(ProfileFixture):
    def setUp(self):
        super().setUp()
        p = patch.object(kb_access, "KB_ROOT", self.kb)
        p.start()
        self.addCleanup(p.stop)

    def test_kb_edits_live_in_the_profile_and_reach_the_view(self):
        pid = profiles.create("系列", profiles.DEFAULT)
        with profiles.using(pid):
            doc = kb_access.read_document("explore", "tools.md")
            kb_access.write_document("explore", "tools.md", "配置里的版本\n", doc["mtime"])
            kb_access.write_document("explore", "speedrun/techniques.md", "速通改版\n")
        self.assertEqual((self.kb / "explore" / "tools.md").read_text(encoding="utf-8"), "d\n")   # 仓库不变
        self.assertEqual(kb_access.read_document("explore", "tools.md")["text"], "d\n")          # 别的配置看不到
        overlay = profiles.kb_overlay_for(pid)
        self.assertEqual(overlay, str(profiles.root() / pid / "kb"))
        c = cfg(mode="explore", knowledge_profile="explore", profile=pid, kb_overlay=overlay,
                kb_allow=["speedrun/techniques.md"])
        self.assertTrue(c.kb_view)
        logs = []
        view = kb_access.prepare_view(c, log=logs.append)
        self.assertEqual((view / "tools.md").read_text(encoding="utf-8"), "配置里的版本\n")
        self.assertEqual((view / "README.md").read_text(encoding="utf-8"), "c\n")
        self.assertEqual((view / "speedrun" / "techniques.md").read_text(encoding="utf-8"), "速通改版\n")
        self.assertTrue(any("配置里的知识库修改" in line for line in logs))
        # 速通模式（整个 kb/）同样换成修改后的版本
        s = cfg(profile=pid, kb_overlay=overlay)
        self.assertEqual((kb_access.prepare_view(s, log=lambda *_: None) / "explore" / "tools.md").read_text(encoding="utf-8"),
                         "配置里的版本\n")
        # 恢复仓库版本后不再有修改
        with profiles.using(pid):
            kb_access.reset_document("explore", "tools.md")
            kb_access.reset_document("explore", "speedrun/techniques.md")
        self.assertEqual(profiles.kb_overlay_for(pid), "")

    def test_state_and_session_use_the_runs_profile(self):
        from orchestrator.runtimes import config_from_state
        pid = profiles.create("系列", profiles.DEFAULT)
        state = {"agent": "a", "model": "opus", "effort": "high", "engine": "claude", "provider": "anthropic"}
        self.assertEqual(config_from_state(state).profile, "local")
        c = config_from_state({**state, "profile": pid})
        self.assertEqual((c.profile, c.kb_overlay), (pid, ""))
        profiles.delete(pid)
        c = config_from_state({**state, "profile": pid})            # 配置被删了：唤起时 activate 才报错
        self.assertEqual(c.kb_overlay, "")
        with self.assertRaises(ValueError):
            profiles.activate(c.profile)


class ConsoleTests(ProfileFixture):
    def test_default_is_read_only(self):
        api = self.console
        self.assertEqual(api.profiles_payload()["current"]["id"], "default")
        for call in (lambda: api.save_prefs({"kb_deny": {"explore": []}}),
                     lambda: api.prompt_write("runtime_zh.md", "x", "v"),
                     lambda: api.prompt_reset("runtime_zh.md"),
                     lambda: api.personal_prompt_write("all", "x", "v"),
                     lambda: api.profile_save({"form": {}})):
            with self.assertRaisesRegex(ValueError, "默认配置"):
                call()
        self.assertTrue(api.personal_prompt_read("all")["readonly"])
        self.assertTrue(api.prompt_read("runtime_zh.md")["readonly"])

    def test_create_from_current_and_switch(self):
        api = self.console
        line = self.legacy_override()
        api.profile_select("local")
        doc = api.personal_prompt_read("all")
        api.personal_prompt_write("all", "原有的要求", doc["version"])
        payload = api.profile_create({"name": "系列", "base": "current", "form": {"agent": "series", "mode": "explore"}})
        pid = payload["selected"]
        self.assertEqual(payload["current"], {"id": pid, "name": "系列", "readonly": False, "builtin": False,
                                              "form": {"agent": "series", "mode": "explore"}})
        # 新配置复制了提示词修改与个人追加要求；改它不影响本机原有设置
        self.assertIn(line, api.prompt_read("runtime_zh.md")["text"])
        doc = api.personal_prompt_read("all")
        self.assertEqual(doc["text"], "原有的要求")
        api.personal_prompt_write("all", "系列的要求", doc["version"])
        read = api.prompt_read("runtime_zh.md")
        api.prompt_write("runtime_zh.md", read["text"] + "系列专用。\n", read["version"])
        self.assertIn("系列专用。", (profiles.root() / pid / "prompts" / "runtime_zh.md").read_text(encoding="utf-8"))
        self.assertNotIn("系列专用。", (prompting.OVERRIDES / "runtime_zh.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(personal_prompts.STORE.read_text(encoding="utf-8"))["scopes"]["all"], "原有的要求")
        # 预览与命令跟着选中的配置
        options = {"agent": "series", "mode": "explore", "save": SAVE}
        preview = api.prompt_preview(options)
        self.assertEqual(preview["profile"], pid)
        self.assertIn("系列的要求", preview["text"] + "".join(p["text"] for p in preview["guide_parts"]))
        self.assertIn(f"--profile {pid}", api.preview(options)["command"])
        api.profile_select("default")
        self.assertNotIn("--profile", api.preview(options)["command"].replace("--profile default", ""))
        self.assertNotIn("系列的要求", api.prompt_preview(options)["text"])
        api.profile_select("local")
        self.assertNotIn("--profile", api.preview(options)["command"])

    def test_kb_permissions_follow_the_profile(self):
        api = self.console
        with patch.object(kb_access, "KB_ROOT", self.kb):
            api.profile_select("local")
            api.save_prefs({"kb_deny": {"explore": ["tools.md"]}})
            self.assertEqual(json.loads(api.PREFS.read_text(encoding="utf-8"))["kb_deny"]["explore"], ["tools.md"])
            pid = api.profile_create({"name": "系列", "base": "current", "form": None})["selected"]
            self.assertEqual(api.load_prefs()["kb_deny"]["explore"], ["tools.md"])     # 复制了原有设置的权限
            api.save_prefs({"kb_deny": {"explore": []}, "kb_allow": {"explore": ["speedrun/techniques.md"]}})
            self.assertEqual(profiles.meta(pid)["kb_deny"]["explore"], [])
            self.assertEqual(profiles.meta(pid)["kb_allow"]["explore"], ["speedrun/techniques.md"])
            self.assertEqual(json.loads(api.PREFS.read_text(encoding="utf-8"))["kb_deny"]["explore"], ["tools.md"])
            listing = api.kb_list("explore")
            self.assertEqual((listing["profile"], listing["deny"], listing["allow"]), (pid, [], ["speedrun/techniques.md"]))
            api.kb_write("explore", "tools.md", "系列\n")
            self.assertTrue(next(d for d in api.kb_list("explore")["documents"] if d["path"] == "tools.md")["edited"])
            self.assertEqual(api.kb_reset("explore", "tools.md")["text"], "d\n")
            api.profile_select("default")
            self.assertEqual(api.load_prefs()["kb_deny"], {"speedrun": [], "explore": []})

    def test_delete_guards(self):
        api = self.console
        pid = api.profile_create({"name": "系列", "base": "default"})["selected"]
        run = self.root / "runs" / "a" / "1"
        run.mkdir(parents=True)
        (run / "run_state.json").write_text(json.dumps({"profile": pid}), encoding="utf-8")
        self.assertEqual(next(r for r in api.profiles_payload()["profiles"] if r["id"] == pid)["runs"], 1)
        with patch.object(api, "orchestrator_status", return_value={"running": True, "run": str(run)}):
            with self.assertRaisesRegex(ValueError, "正在运行"):
                api.profile_delete({"id": pid})
        self.assertEqual(api.profile_delete({"id": pid})["selected"], "default")
        for bad in ({"id": "local"}, {"id": "default"}, {"id": pid}, {"other": 1}):
            with self.assertRaises(ValueError):
                api.profile_delete(bad)


if __name__ == "__main__":
    unittest.main()
