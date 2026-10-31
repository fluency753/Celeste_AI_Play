"""控制台"管理提供方"：本机覆盖文件的合并与原子写、内置项覆盖与恢复默认、密钥不回显、校验、运行中拒绝修改、
本机网关地址的转换与连通测试。全部在临时目录里进行：configs/providers.json 是仓库里那份的副本，
不碰真实的 configs/local.providers.json，也不连外网（连通测试连的是测试自己起的本机服务）。"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from orchestrator import config, provider_settings as ps
from orchestrator.launch_options import build, command_line
from orchestrator.runtimes import configured_runtimes, runtime

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))   # 主控模块按仓库里的 tools/ 导入（测试把 config.REPO_ROOT 换成了临时目录）
SECRET = "sk-test-SECRET-0123456789abcdefWXYZ"      # 测试用的假密钥（末尾 4 位 WXYZ）
PASTED = "sk-pasted-SECRET-9876543210zyxwQRST"


class ProviderSandbox(unittest.TestCase):
    """每个测试一个临时仓库：configs/providers.json 是仓库里那份的副本，configs/local.providers.json 由测试自己写。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "configs").mkdir()
        shutil.copy(REPO / "configs" / "providers.json", self.root / "configs" / "providers.json")
        self.local = self.root / "configs" / "local.providers.json"
        self.real_local = REPO / "configs" / "local.providers.json"
        self.real_before = self.real_local.read_bytes() if self.real_local.exists() else None
        self.real_keys_before = sorted(p.name for p in (REPO / "configs" / "local.keys").glob("*")) \
            if (REPO / "configs" / "local.keys").is_dir() else None
        for target, value in ((config, "REPO_ROOT"), (config, "PROVIDERS"), (config, "LOCAL_PROVIDERS")):
            path = {"REPO_ROOT": self.root, "PROVIDERS": self.root / "configs" / "providers.json",
                    "LOCAL_PROVIDERS": self.local}[value]
            patcher = patch.object(target, value, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("DEEPSEEK_API_KEY", "MYGW_API_KEY", "MYPROV_API_KEY"):
            os.environ.pop(name, None)
        self.addCleanup(self.assert_real_config_untouched)

    def assert_real_config_untouched(self):
        after = self.real_local.read_bytes() if self.real_local.exists() else None
        self.assertEqual(after, self.real_before, "测试改动了真实的 configs/local.providers.json")
        keys = sorted(p.name for p in (REPO / "configs" / "local.keys").glob("*")) \
            if (REPO / "configs" / "local.keys").is_dir() else None
        self.assertEqual(keys, self.real_keys_before, "测试在真实的 configs/local.keys/ 里写了文件")

    def write_local(self, data):
        self.local.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_local(self):
        return json.loads(self.local.read_text(encoding="utf-8"))

    def provider(self, ident, data=None):
        data = data or ps.listing()
        return next(p for p in data["providers"] if p["id"] == ident)

    def form(self, ident, **changes):
        """照控制台的做法：拿列表里这一项的当前值组成表单，再改其中几项。"""
        data = ps.listing()
        p = self.provider(ident, data)
        body = {"id": ident, "version": data["version"], "label": p["label"], "base_url": p["base_url"],
                "extra_domains": p["extra_domains"], "key": {"mode": "keep"}, "key_env": p["key_env"],
                "models": [{k: v for k, v in m.items() if v} for m in p["models"]], "model_suffix": p["model_suffix"],
                "autocompact": p["autocompact"], "env": p["env"], "images": p["images"], "ready": p["ready"],
                "gateway_check": p["gateway_check"]}
        body.update(changes)
        return body

    def new_form(self, ident="myprov", **changes):
        body = {"id": ident, "create": True, "version": ps.listing()["version"], "label": "我的平台",
                "base_url": "https://api.example.com/anthropic", "extra_domains": [], "key": {"mode": "keep"},
                "models": [{"id": "model-a", "efforts": ["low", "medium", "high", "xhigh", "max"]}],
                "env": dict(ps.DEFAULT_ENV), "images": True, "ready": False}
        body.update(changes)
        return body

    def key_file(self, name="deepseek.key", text=SECRET):
        path = self.root / "secrets" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return path


class MergeTests(ProviderSandbox):
    def test_save_keeps_unknown_fields_other_providers_and_notes(self):
        key = self.key_file()
        original = {"_说明": "本机说明", "deepseek": {"key_file": str(key), "my_note": {"a": 1}},
                    "other": {"label": "别的", "base_url": "https://x.example.org/a", "custom": [1, 2]},
                    "gateway": "不是对象的顶层字段"}
        self.write_local(original)
        ps.save(self.form("deepseek", label="DeepSeek 本机", extra_domains=["cdn.deepseek.com"]))
        data = self.read_local()
        self.assertEqual(data["_说明"], "本机说明")
        self.assertEqual(data["other"], original["other"])
        self.assertEqual(data["gateway"], "不是对象的顶层字段")
        self.assertEqual(data["deepseek"]["my_note"], {"a": 1})
        self.assertEqual(data["deepseek"]["key_file"], str(key))
        self.assertEqual(data["deepseek"]["label"], "DeepSeek 本机")
        self.assertEqual(data["deepseek"]["allowed_domains"], ["api.deepseek.com", "cdn.deepseek.com"])
        # 没改的字段不写进本机覆盖（只写与入库默认值不同的）
        self.assertNotIn("base_url", data["deepseek"])
        self.assertNotIn("models", data["deepseek"])
        self.assertEqual(config.load_provider("deepseek")["allowed_domains"], ["api.deepseek.com", "cdn.deepseek.com"])
        self.assertFalse(list(self.local.parent.glob("*.tmp")), "原子写留下了 .tmp")

    def test_unknown_model_fields_are_kept(self):
        self.write_local({"custom": {"label": "C", "base_url": "https://c.example.com/anthropic",
                                     "models": [{"id": "m1", "efforts": ["low"], "description": "说明", "x": 2}]}})
        ps.save(self.form("custom", models=[{"id": "m1", "efforts": ["low", "high"], "default_effort": "high"}]))
        row = self.read_local()["custom"]["models"][0]
        self.assertEqual(row, {"id": "m1", "efforts": ["low", "high"], "default_effort": "high", "description": "说明", "x": 2})

    def test_stale_version_and_invalid_json_are_rejected_without_writing(self):
        self.write_local({"deepseek": {"key_file": "a"}})
        body = self.form("deepseek", label="新名字")
        self.write_local({"deepseek": {"key_file": "b"}})      # 打开表单之后文件被别人改过
        with self.assertRaisesRegex(ValueError, "重新读取"):
            ps.save(body)
        self.assertEqual(self.read_local(), {"deepseek": {"key_file": "b"}})
        self.local.write_text("{broken", encoding="utf-8")
        data = ps.listing()
        self.assertIn("不是有效的 JSON", data["local_error"])
        with self.assertRaisesRegex(ValueError, "不是有效的 JSON"):
            ps.save({**body, "version": data["version"]})
        self.assertEqual(self.local.read_text(encoding="utf-8"), "{broken")

    def test_runtime_dropdown_and_models_refresh_after_save(self):
        before = [r["id"] for r in configured_runtimes()]
        self.assertNotIn("claude:myprov", before)
        ps.save(self.new_form(models=[{"id": "model-a", "label": "A", "efforts": ["low", "high"], "default_effort": "high"}]))
        rows = {r["id"]: r for r in configured_runtimes()}
        self.assertEqual(rows["claude:myprov"]["label"], "Claude Code · 我的平台")
        self.assertEqual(rows["claude:myprov"]["status"], "未实测")
        self.assertEqual(rows["claude:deepseek"]["status"], "ready")
        models = runtime("claude").models("myprov")
        self.assertEqual([(m["id"], m["label"], m["efforts"], m["default_effort"]) for m in models["models"]],
                         [("model-a", "A", ["low", "high"], "high")])
        self.assertEqual(ps.listing()["runtimes"], configured_runtimes())


class BuiltinTests(ProviderSandbox):
    def test_builtin_deepseek_is_listed_with_its_settings(self):
        p = self.provider("deepseek")
        self.assertTrue(p["builtin"])
        self.assertFalse(p["overridden"])
        self.assertEqual(p["base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(p["auto_domain"], "api.deepseek.com")
        self.assertEqual(p["extra_domains"], [])
        self.assertEqual([m["id"] for m in p["models"]], ["deepseek-flash", "deepseek-v4-pro"])
        self.assertEqual(p["model_suffix"], "[1m]")
        self.assertEqual(p["autocompact"], "786432")
        self.assertEqual(p["env"], ps.DEFAULT_ENV)
        self.assertTrue(p["ready"])
        self.assertEqual(p["key"]["configured"], False)
        ids = [x["id"] for x in ps.listing()["providers"]]
        self.assertNotIn("anthropic", ids)
        self.assertNotIn("codex", ids)

    def test_override_restore_default_and_keep_key(self):
        key = self.key_file()
        repo_before = (self.root / "configs" / "providers.json").read_bytes()
        self.write_local({"deepseek": {"key_file": str(key)}})
        ps.save(self.form("deepseek", base_url="https://proxy.example.com/anthropic", model_suffix=""))
        merged = config.load_provider("deepseek")
        self.assertEqual(merged["base_url"], "https://proxy.example.com/anthropic")
        self.assertEqual(merged["allowed_domains"], ["proxy.example.com"])
        self.assertEqual(merged["model_suffix"], "")
        self.assertEqual((self.root / "configs" / "providers.json").read_bytes(), repo_before, "改了入库的 providers.json")
        # 恢复默认、保留密钥设置：本机覆盖里只剩 key_file
        ps.remove({"id": "deepseek", "version": ps.listing()["version"], "keep_key": True})
        self.assertEqual(self.read_local(), {"deepseek": {"key_file": str(key)}})
        merged = config.load_provider("deepseek")
        self.assertEqual(merged["base_url"], "https://api.deepseek.com/anthropic")
        self.assertEqual(merged["model_suffix"], "[1m]")
        self.assertTrue(self.provider("deepseek")["key"]["configured"])
        # 再恢复默认、不保留：这一项整个去掉，用户自己的密钥文件不删
        ps.remove({"id": "deepseek", "version": ps.listing()["version"], "keep_key": False})
        self.assertEqual(self.read_local(), {})
        self.assertTrue(key.is_file())
        self.assertFalse(self.provider("deepseek")["overridden"])
        with self.assertRaisesRegex(ValueError, "已经是默认设置"):
            ps.remove({"id": "deepseek", "version": ps.listing()["version"]})

    def test_setting_back_to_default_removes_the_override_field(self):
        ps.save(self.form("deepseek", autocompact="auto"))
        self.assertEqual(self.read_local()["deepseek"], {"autocompact": "auto"})
        ps.save(self.form("deepseek", autocompact="786432"))
        self.assertEqual(self.read_local(), {})

    def test_existing_user_key_file_is_kept_on_unrelated_save(self):
        key = self.key_file()
        self.write_local({"deepseek": {"key_file": str(key)}})
        ps.save(self.form("deepseek", ready=True, label="DeepSeek"))
        self.assertEqual(self.read_local(), {"deepseek": {"key_file": str(key)}})

    def test_builtin_cannot_be_renamed_or_deleted_by_create(self):
        with self.assertRaisesRegex(ValueError, "已有名为 deepseek"):
            ps.save(self.new_form("DeepSeek"))
        for name in ("anthropic", "Codex"):
            with self.assertRaisesRegex(ValueError, "anthropic 与 codex"):
                ps.save(self.new_form(name))


class SecretTests(ProviderSandbox):
    def assert_no_secret(self, value, *secrets):
        text = json.dumps(value, ensure_ascii=False)
        for secret in secrets or (SECRET, PASTED):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret[:-4], text)

    def test_listing_shows_source_and_tail_only(self):
        key = self.key_file()
        self.write_local({"deepseek": {"key_file": str(key)}})
        data = ps.listing()
        state = self.provider("deepseek", data)["key"]
        self.assertEqual((state["configured"], state["source"], state["tail"]), (True, "file", "WXYZ"))
        self.assert_no_secret(data)
        os.environ["DEEPSEEK_API_KEY"] = "env-SECRET-abcdefghijkl7777"
        state = self.provider("deepseek")["key"]
        self.assertEqual((state["source"], state["tail"]), ("env", "7777"))
        self.assertIn("DEEPSEEK_API_KEY", state["text"])
        self.assert_no_secret(ps.listing(), "env-SECRET-abcdefghijkl7777")

    def test_pasted_key_is_saved_locally_and_never_returned(self):
        ps.save(self.new_form(key={"mode": "paste", "value": "  " + PASTED + "\n"}))
        entry = self.read_local()["myprov"]
        self.assertEqual(entry["key_file"], "configs/local.keys/myprov.key")
        self.assertEqual(entry["key_env"], "MYPROV_API_KEY")
        path = self.root / "configs" / "local.keys" / "myprov.key"
        self.assertEqual(path.read_text(encoding="ascii"), PASTED + "\n")
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(PASTED, self.local.read_text(encoding="utf-8"))
        data = ps.listing()
        p = self.provider("myprov", data)
        self.assertEqual((p["key"]["source"], p["key"]["tail"], p["key_saved_by_console"]), ("console", "QRST", True))
        self.assert_no_secret(data)
        # 主控照旧用 load_api_key 读出来（相对路径按仓库根目录）
        self.assertEqual(config.load_api_key(config.load_provider("myprov")), PASTED)
        # 命令预览与命令行参数里没有密钥
        options = {"engine": "claude", "provider": "myprov", "model": "model-a", "effort": "high", "agent": "dev-x",
                   "mode": "explore", "save": {"name": "test", "slot": None, "create": False}, "sim_assist": False}
        argv = build(options)
        self.assertIn("myprov", argv)
        self.assert_no_secret(command_line(argv))
        self.assert_no_secret(argv)
        with patch.object(config, "REPO_ROOT", self.root):
            self.assertFalse(list((self.root / "configs" / "local.keys").glob("*.tmp")))

    def test_key_with_equals_sign_round_trips(self):
        value = "abc=def==ghijklmnop"
        ps.save(self.new_form(key={"mode": "paste", "value": value}))
        self.assertEqual(config.load_api_key(config.load_provider("myprov")), value)

    def test_bad_pasted_key_error_does_not_echo_it(self):
        bad = "SECRET with space 123456"
        with self.assertRaises(ValueError) as caught:
            ps.save(self.new_form(key={"mode": "paste", "value": bad}))
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertFalse(self.local.exists())
        self.assertFalse((self.root / "configs" / "local.keys").exists())

    def test_invalid_other_field_does_not_write_pasted_key(self):
        with self.assertRaisesRegex(ValueError, "接口地址"):
            ps.save(self.new_form(base_url="ftp://x", key={"mode": "paste", "value": PASTED}))
        self.assertFalse((self.root / "configs" / "local.keys").exists())

    def test_key_file_mode_checks_the_file(self):
        with self.assertRaisesRegex(ValueError, "找不到密钥文件"):
            ps.save(self.form("deepseek", key={"mode": "file", "file": str(self.root / "nope.key")}))
        empty = self.key_file("empty.key", "")
        with self.assertRaisesRegex(ValueError, "没有读到密钥"):
            ps.save(self.form("deepseek", key={"mode": "file", "file": str(empty)}))
        good = self.key_file()
        ps.save(self.form("deepseek", key={"mode": "file", "file": str(good)}))
        self.assertEqual(self.read_local()["deepseek"]["key_file"], str(good))
        # 换成粘贴：key_file 改指向控制台保存的文件，原来的文件不动
        ps.save(self.form("deepseek", key={"mode": "paste", "value": PASTED}))
        self.assertEqual(self.read_local()["deepseek"]["key_file"], "configs/local.keys/deepseek.key")
        self.assertEqual(good.read_text(encoding="utf-8"), SECRET + "\n")

    def test_secret_env_entries_are_rejected_hidden_and_preserved(self):
        with self.assertRaisesRegex(ValueError, "env 里不能写 ANTHROPIC_AUTH_TOKEN"):
            ps.save(self.new_form(env={"ANTHROPIC_AUTH_TOKEN": SECRET}))
        self.write_local({"custom": {"label": "C", "base_url": "https://c.example.com/anthropic",
                                     "models": [{"id": "m", "efforts": ["low"]}],
                                     "env": {"API_TIMEOUT_MS": "1", "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: " + SECRET}}})
        data = ps.listing()
        p = self.provider("custom", data)
        self.assertEqual(p["env"], {"API_TIMEOUT_MS": "1"})
        self.assertEqual(p["env_hidden"], ["ANTHROPIC_CUSTOM_HEADERS"])
        self.assert_no_secret(data)
        ps.save(self.form("custom", env={"API_TIMEOUT_MS": "2"}))
        self.assertEqual(self.read_local()["custom"]["env"],
                         {"API_TIMEOUT_MS": "2", "ANTHROPIC_CUSTOM_HEADERS": "x-api-key: " + SECRET})

    def test_remove_custom_provider_can_delete_console_key(self):
        ps.save(self.new_form(key={"mode": "paste", "value": PASTED}))
        path = self.root / "configs" / "local.keys" / "myprov.key"
        ps.remove({"id": "myprov", "version": ps.listing()["version"], "delete_key_file": True})
        self.assertNotIn("myprov", self.read_local())
        self.assertFalse(path.exists())
        user = self.key_file()
        self.write_local({"mine": {"label": "M", "base_url": "https://m.example.com/a", "key_file": str(user),
                                   "models": [{"id": "m", "efforts": ["low"]}]}})
        with self.assertRaisesRegex(ValueError, "控制台保存的密钥文件"):
            ps.remove({"id": "mine", "version": ps.listing()["version"], "delete_key_file": True})
        ps.remove({"id": "mine", "version": ps.listing()["version"]})
        self.assertTrue(user.is_file(), "删除提供方时删了用户自己的密钥文件")


class ValidationTests(ProviderSandbox):
    def expect(self, pattern, **changes):
        with self.assertRaisesRegex(ValueError, pattern):
            ps.save(self.new_form(**changes))
        self.assertFalse(self.local.exists())

    def test_identifier(self):
        for bad in ("", "-x", "a b", "中文", "x" * 41, "a/b", None, 3):
            with self.subTest(bad=bad):
                self.expect("标识只能用", id=bad)
        self.expect("Windows 的设备名", id="con")

    def test_base_url(self):
        self.expect("https", base_url="http://api.example.com/anthropic")
        self.expect("https", base_url="ftp://api.example.com")
        self.expect("用户名或密码", base_url="https://user:pw@api.example.com/anthropic")
        self.expect("443", base_url="https://api.example.com:8443/anthropic")
        self.expect("要写端口", base_url="http://localhost/anthropic")
        self.expect("http://", base_url="https://localhost:4000")
        self.expect("不能带 \\?", base_url="https://api.example.com/anthropic?x=1")
        self.expect("/v1 之前", base_url="https://api.example.com/anthropic/v1/messages")
        self.expect("请填写接口地址", base_url="  ")
        self.expect("不能有空格", base_url="https://api.example.com/a b")
        self.expect("主机名无效", base_url="https://-bad-.example/anthropic")

    def test_domains_models_env_and_others(self):
        self.expect("放行域名无效", extra_domains=["https://cdn.example.com"])
        self.expect("本机地址", extra_domains=["localhost"])
        self.expect("1～50 个模型", models=[])
        self.expect("模型的标识无效", models=[{"id": "a b", "efforts": ["low"]}])
        self.expect("重复", models=[{"id": "a", "efforts": ["low"]}, {"id": "a", "efforts": ["low"]}])
        self.expect("1～12 个推理强度", models=[{"id": "a", "efforts": []}])
        self.expect("推理强度无效", models=[{"id": "a", "efforts": ["auto"]}])
        self.expect("默认推理强度", models=[{"id": "a", "efforts": ["low"], "default_effort": "high"}])
        self.expect("格式无效", models=[{"id": "a", "efforts": ["low"], "token": "x"}])
        self.expect("JSON 对象", env=["x"])
        self.expect("JSON 对象", env=None)
        self.expect("环境变量名", env={"lower": "1"})
        self.expect("模型名后缀", model_suffix="1m")
        self.expect("自动压缩窗口", autocompact="5")
        self.expect("CLAUDE 或 ANTHROPIC", key_env="ANTHROPIC_API_KEY")
        self.expect("网关检查地址", gateway_check="http://example.com/health")
        self.expect("开或关", images="yes")
        self.expect("格式无效", unexpected=1)
        self.expect("密钥的设置方式", key={"mode": "steal"})
        with self.assertRaisesRegex(ValueError, "格式无效"):
            ps.save("not a dict")

    def test_new_provider_defaults(self):
        ps.save(self.new_form(env=dict(ps.DEFAULT_ENV)))
        entry = self.read_local()["myprov"]
        self.assertEqual(entry["status"], "未实测")
        self.assertEqual(entry["auth"], "api_key")
        self.assertEqual(entry["allowed_domains"], ["api.example.com"])
        self.assertEqual(entry["env"], {"ANTHROPIC_DEFAULT_OPUS_MODEL": "{model}", "ANTHROPIC_DEFAULT_SONNET_MODEL": "{model}",
                                        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "{model_base}",
                                        "CLAUDE_CODE_SUBAGENT_MODEL": "{model_base}", "API_TIMEOUT_MS": "600000"})
        self.assertNotIn("allowed_host_ports", entry)
        self.assertEqual(self.provider("myprov")["ready"], False)
        # 主控对非 ready 的提示沿用原来那套（check_provider 打印"注意：模型提供方 X 未实测"）
        from orchestrator.run import check_provider
        with patch("builtins.print") as printed:
            check_provider(config.load_provider("myprov"))
        self.assertIn("注意：模型提供方 我的平台 未实测", printed.call_args[0][0])
        ps.save(self.form("myprov", ready=True))
        self.assertEqual(self.read_local()["myprov"]["status"], "ready")
        ps.save(self.form("myprov", ready=False))
        self.assertEqual(self.read_local()["myprov"]["status"], "未实测")


class RunningTests(ProviderSandbox):
    def test_provider_in_use_cannot_be_changed(self):
        ps.save(self.new_form())
        with self.assertRaisesRegex(ValueError, "正在用 deepseek"):
            ps.save(self.form("deepseek", label="X"), in_use=frozenset({"deepseek"}))
        with self.assertRaisesRegex(ValueError, "正在用 deepseek"):
            ps.remove({"id": "deepseek", "version": ps.listing()["version"]}, in_use=frozenset({"deepseek"}))
        ps.save(self.form("myprov", label="别的照样能改"), in_use=frozenset({"deepseek"}))
        self.assertEqual(self.read_local()["myprov"]["label"], "别的照样能改")
        with self.assertRaisesRegex(ValueError, "读不到它用的提供方"):
            ps.save(self.form("myprov", label="Y"), in_use=None)
        with self.assertRaisesRegex(ValueError, "读不到它用的提供方"):
            ps.save(self.new_form("another"), in_use=None)
        data = ps.listing(running=True, in_use=frozenset({"deepseek"}))
        self.assertTrue(self.provider("deepseek", data)["in_use"])
        self.assertFalse(self.provider("myprov", data)["in_use"])

    def test_console_reads_provider_of_running_orchestrator(self):
        from overlay import console_api
        run = self.root / "runs" / "dev" / "20261030-000000"
        run.mkdir(parents=True)
        (run / "run_state.json").write_text(json.dumps({"engine": "claude", "provider": "deepseek"}), encoding="utf-8")
        status = {"running": True, "run": str(run), "launched": None, "host_state": None}
        with patch.object(console_api, "orchestrator_status", return_value=status):
            self.assertEqual(console_api._providers_in_use(), (True, frozenset({"deepseek"})))
            with self.assertRaisesRegex(ValueError, "正在用 deepseek"):
                console_api.providers_save(self.form("deepseek", label="X"))
        argv = ["--engine", "claude", "--provider", "myprov", "--agent", "x"]
        (run / "summary.json").write_text("{}", encoding="utf-8")       # 上一次运行已经结束
        with patch.object(console_api, "orchestrator_status",
                          return_value={**status, "launched": argv, "host_state": "starting"}):
            self.assertEqual(console_api._providers_in_use(), (True, frozenset({"myprov"})))
        with patch.object(console_api, "orchestrator_status",
                          return_value={**status, "launched": ["--resume", "x"], "host_state": "running"}):
            self.assertEqual(console_api._providers_in_use(), (True, None))
        with patch.object(console_api, "orchestrator_status", return_value={"running": False, "run": None}):
            self.assertEqual(console_api._providers_in_use(), (False, frozenset()))


class GatewayTests(ProviderSandbox):
    def test_local_gateway_is_rewritten_for_the_container(self):
        for given, expected in (("http://localhost:4000", "http://host.docker.internal:4000"),
                                ("http://127.0.0.1:4000/anthropic/", "http://host.docker.internal:4000/anthropic"),
                                ("HTTP://LocalHost:8080/x", "http://host.docker.internal:8080/x"),
                                ("http://host.docker.internal:4000", "http://host.docker.internal:4000")):
            with self.subTest(given=given):
                url, domain, ports = ps.parse_base_url(given)
                self.assertEqual((url, domain), (expected, None))
                self.assertEqual(ports, [int(expected.split(":")[2].split("/")[0])])
        ps.save(self.new_form("mygw", base_url="http://localhost:4000", extra_domains=[]))
        info = config.load_provider("mygw")
        self.assertEqual(info["base_url"], "http://host.docker.internal:4000")
        self.assertEqual(info["allowed_host_ports"], [4000])
        self.assertEqual(info["allowed_domains"], [])
        # 按现有机制生效：RunConfig.provider_host_ports → 容器的 PROVIDER_HOST_PORTS 与标签（防火墙放行宿主机端口）
        from orchestrator.config import RunConfig
        from orchestrator import container
        cfg = runtime("claude").configure(RunConfig(agent="dev-gw", save="test", model="model-a"), "mygw")
        self.assertEqual(cfg.provider_host_ports, [4000])
        self.assertEqual(cfg.provider_domains, [])
        self.assertEqual(container._labels(cfg)[f"{container.LABEL}.host_ports"], "4000")
        calls = []
        with patch.object(container, "docker", side_effect=lambda *a, **k: calls.append(a)), \
             patch.object(type(cfg), "workspace_dir", property(lambda s: self.root / "ws")), \
             patch.object(type(cfg), "kb_dir", property(lambda s: self.root / "kb")):
            container._create(cfg, "test-container")
        args = list(calls[0])
        self.assertIn("PROVIDER_HOST_PORTS=4000", args)
        self.assertIn("PROVIDER_DOMAINS=", args)
        self.assertIn("ALLOW_ANTHROPIC=0", args)
        firewall = (REPO / "agent-container" / "init-firewall.sh").read_text(encoding="utf-8")
        self.assertIn('for port in $HOST_PORTS; do', firewall)
        # 改回 https 接口：宿主机端口不再放行
        ps.save(self.form("mygw", base_url="https://gw.example.com/anthropic"))
        info = config.load_provider("mygw")
        self.assertNotIn("allowed_host_ports", info)
        self.assertEqual(info["allowed_domains"], ["gw.example.com"])
        self.assertEqual(ps.host_url("http://host.docker.internal:4000/a"), "http://127.0.0.1:4000/a")


class _FakeAnthropic(BaseHTTPRequestHandler):
    """本机的假接口：记下收到的请求；密钥对就回一条 message，不对就回 401 并在报错里回显密钥（看会不会被遮住）。"""
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).seen.append({"path": self.path, "auth": self.headers.get("authorization"), "body": body,
                                "version": self.headers.get("anthropic-version")})
        if self.headers.get("authorization") == "Bearer " + SECRET:
            data, code = {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]}, 200
        else:
            token = (self.headers.get("authorization") or "")[7:]
            data, code = {"type": "error", "error": {"type": "authentication_error",
                                                     "message": f"api key {token[:10]}****{token[-4:]} invalid; full {token}"}}, 401
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ConnectivityTests(ProviderSandbox):
    def setUp(self):
        super().setUp()
        _FakeAnthropic.seen = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropic)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def test_minimal_request_success_and_failure_without_echoing_key(self):
        key = self.key_file("gw.key")
        ps.save(self.new_form("mygw", base_url=f"http://localhost:{self.port}/anthropic",
                              key={"mode": "file", "file": str(key)}, models=[{"id": "m-1", "efforts": ["low"]}]))
        result = ps.connectivity({"id": "mygw"})
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], 200)
        request = _FakeAnthropic.seen[-1]
        self.assertEqual(request["path"], "/anthropic/v1/messages")
        self.assertEqual(request["body"]["model"], "m-1")
        self.assertLessEqual(request["body"]["max_tokens"], 16)
        self.assertEqual(request["version"], "2023-06-01")
        self.assertNotIn(SECRET, json.dumps(result))
        key.write_text("sk-wrong-key-0000000000000000000000\n", encoding="utf-8")
        result = ps.connectivity({"id": "mygw", "model": "m-1"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)
        self.assertIn("认证失败", result["reason"])
        self.assertNotIn("sk-wrong-key", json.dumps(result))
        self.assertNotIn("0000000000000000", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "模型列表"):
            ps.connectivity({"id": "mygw", "model": "not-listed"})

    def test_unreachable_and_missing_key(self):
        ps.save(self.new_form("mygw", base_url="http://localhost:9/anthropic"))
        with self.assertRaisesRegex(ValueError, "缺少 mygw 的 API 密钥"):
            ps.connectivity({"id": "mygw"})
        os.environ["MYGW_API_KEY"] = SECRET
        result = ps.connectivity({"id": "mygw"}, timeout=3)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["status"])
        self.assertIn("连不上接口", result["reason"])
        self.assertNotIn(SECRET, json.dumps(result))


class HTTPTests(ProviderSandbox):
    """经 HUD 服务的真实接口：只接受本机页面、不带 CORS 头、返回值里没有密钥。"""

    def setUp(self):
        super().setUp()
        from overlay.server import make_handler
        from overlay import console_api
        status = patch.object(console_api, "orchestrator_status", return_value={"running": False, "run": None})
        status.start()
        self.addCleanup(status.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, data=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        request = Request(self.base + path, data=None if data is None else json.dumps(data).encode(), headers=headers)
        try:
            with urlopen(request, timeout=5) as response:
                self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_endpoints(self):
        code, data = self.call("/api/console/providers")
        self.assertEqual(code, 200)
        self.assertIn("deepseek", [p["id"] for p in data["providers"]])
        body = self.new_form(key={"mode": "paste", "value": PASTED})
        code, error = self.call("/api/console/providers", body, origin="https://evil.example")
        self.assertEqual(code, 400)
        self.assertFalse(self.local.exists())
        code, saved = self.call("/api/console/providers", body)
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved["saved"], "myprov")
        self.assertIn("claude:myprov", [r["id"] for r in saved["runtimes"]])
        self.assertNotIn(PASTED, json.dumps(saved))
        self.assertNotIn(PASTED[:-4], json.dumps(saved))
        code, error = self.call("/api/console/providers", {**body, "version": saved["version"], "key": {"mode": "paste", "value": "a b"}})
        self.assertEqual(code, 400)
        self.assertIn("已有名为 myprov", error["error"])
        code, preview = self.call("/api/console/preview", {"options": {
            "engine": "claude", "provider": "myprov", "model": "model-a", "effort": "high", "agent": "dev-x",
            "mode": "explore", "sim_assist": False, "save": {"name": "test", "slot": None, "create": False}}})
        self.assertEqual(code, 200, preview)
        self.assertIn("--provider myprov", preview["command"])
        self.assertNotIn(PASTED, preview["command"])
        code, removed = self.call("/api/console/providers/remove",
                                  {"id": "myprov", "version": saved["version"], "delete_key_file": True})
        self.assertEqual(code, 200, removed)
        self.assertNotIn("myprov", [p["id"] for p in removed["providers"]])
        code, error = self.call("/api/console/providers/test", {"id": "myprov"})
        self.assertEqual(code, 400)
        self.assertIn("没有这个提供方", error["error"])


class ConsolePageTests(unittest.TestCase):
    def test_form_is_wired_into_run_page_and_never_renders_key(self):
        html = (REPO / "overlay" / "console.html").read_text(encoding="utf-8")
        self.assertIn('id="manageProviders"', html)
        setup = html[html.index('<fieldset id="fsAgent">'):html.index("</fieldset>", html.index('<fieldset id="fsAgent">'))]
        self.assertIn('id="runtime"', setup)
        self.assertIn('id="manageProviders"', setup)
        self.assertIn('type="password" id="provKeyValue"', html)
        self.assertIn('autocomplete="new-password"', html)
        script = html[html.index("// ---------------------------------------------------------------- 管理提供方"):]
        script = script[:script.index("// ---------------------------------------------------------------- 事件")]
        # 粘贴的密钥只进保存请求：保存后清空，快照（判断有没有改动）里不含它
        self.assertIn('if (body.key.mode === "paste") body.key.value = $("provKeyValue").value.trim();', script)
        self.assertIn('$("provKeyValue").value = "";', script)
        self.assertNotIn("key.value", script.split("function provSnapshot")[1].split("\n")[0])
        for endpoint in ("/api/console/providers", "/api/console/providers/remove", "/api/console/providers/test"):
            self.assertIn(endpoint, script)


if __name__ == "__main__":
    unittest.main()
