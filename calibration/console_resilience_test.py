"""控制台的自我恢复与排查：

- 运行设置读不出来时（例如窗口打开时 HUD 服务刚好在重启）自动重试，配置栏写明在重试，不再一直停在“读取中…”；
- 页面里的脚本错误报给 HUD 服务，追加到本机 local/console-client.log（超过上限只留后一半）；
- 启动器只把属于 Python 进程的同名窗口当作已经开着的控制台（资源管理器留下的同名代理窗口不算）。
"""
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from overlay import console_api
from overlay.server import ThreadingHTTPServer, make_handler

REPO = Path(__file__).resolve().parents[1]
CONSOLE = (REPO / "overlay" / "console.html").read_text(encoding="utf-8")


def function_source(name: str) -> str:
    start = CONSOLE.index(f"async function {name}(")
    end = CONSOLE.index("\n}\n", start) + 3
    return CONSOLE[start:end]


class ClientErrorLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "local" / "console-client.log"
        for target, value in ((console_api, "CLIENT_LOG"), ):
            p = patch.object(target, value, self.log)
            p.start()
            self.addCleanup(p.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(lambda: (self.server.shutdown(), worker.join(3), self.server.server_close()))

    def post(self, body):
        request = Request(f"http://127.0.0.1:{self.server.server_port}/api/console/client-error",
                          data=json.dumps(body, ensure_ascii=False).encode(), headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_errors_are_appended_and_the_log_is_bounded(self):
        self.assertEqual(self.post({"where": "loadOptions#1", "message": "Failed to fetch", "stack": "x" * 9000,
                                    "page": "run", "extra": 1}), {"logged": True})
        row = json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["where"], row["message"], row["page"]), ("loadOptions#1", "Failed to fetch", "run"))
        self.assertEqual(len(row["stack"]), 4000)
        self.assertEqual(row["version"], console_api.CONSOLE_VERSION)
        self.post({"where": 7, "message": None})                      # 不是文字的字段记成空
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8").splitlines()[1])["where"], "")
        with patch.object(console_api, "CLIENT_LOG_MAX", 2000):
            for i in range(30):
                self.post({"where": f"e{i}", "message": "m" * 100})
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertLess(self.log.stat().st_size, 2000 + 400)
        self.assertEqual(json.loads(lines[-1])["where"], "e29")
        for line in lines:
            json.loads(line)                                          # 截断后每行仍是完整的 JSON


@unittest.skipUnless(shutil.which("node"), "需要 Node.js 运行页面函数")
class LoadOptionsRetryTests(unittest.TestCase):
    def test_failures_are_retried_and_shown_in_the_profile_bar(self):
        harness = r"""
const source = require("fs").readFileSync(0, "utf8");
const els = {profile: {options: [], replaceChildren(...o) { this.options = o; }}, profileHint: {textContent: ""}};
const $ = id => els[id];
function Option(text, value) { this.text = text; this.value = value; }
const ui = {settingsError: null, page: "run"};
const shown = [], toasts = [], reported = [];
let calls = 0, previews = 0;
async function loadOptions() {
  calls++;
  shown.push(els.profile.options.map(o => o.text).join("|"));
  if (calls < 3) throw new Error("Failed to fetch");
  els.profile.replaceChildren(new Option("预置配置 1", "preset1"));
}
function toast(text, error) { toasts.push([text, !!error]); }
function reportClientError(where, e) { reported.push(where + ":" + e.message); }
function schedulePreview() { previews++; }
function loadPromptPreview() {}
global.setTimeout = (fn) => fn();
eval(source);
loadOptionsUntilReady().then(() => {
  console.log(JSON.stringify({calls, shown, toasts, reported, error: ui.settingsError, previews,
                              profile: els.profile.options.map(o => o.text)}));
});
"""
        result = subprocess.run(["node", "-e", harness], input=function_source("loadOptionsUntilReady"),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertEqual(out["calls"], 3)
        self.assertEqual(out["shown"][1:], ["读取失败，正在重试…", "读取失败，正在重试…"])
        self.assertEqual(out["reported"], ["loadOptions#1:Failed to fetch", "loadOptions#2:Failed to fetch"])
        self.assertIsNone(out["error"])
        self.assertEqual(out["profile"], ["预置配置 1"])
        self.assertEqual(out["toasts"], [["读取设置失败：Failed to fetch", True], ["运行设置已读取。", False]])
        self.assertEqual(out["previews"], 1)

    def test_boot_uses_the_retrying_loader(self):
        boot = CONSOLE[CONSOLE.index("(function boot() {"):]
        self.assertIn("loadOptionsUntilReady();", boot[:400])
        self.assertNotIn("loadOptions().catch", CONSOLE)


class LauncherTests(unittest.TestCase):
    def test_only_python_owned_windows_count_as_an_open_console(self):
        source = (REPO / "tools" / "launcher" / "launcher.c").read_text(encoding="utf-8")
        main = source[source.index("int WINAPI WinMain"):]
        self.assertIn("HWND window = console_window();", main)
        self.assertNotRegex(main, re.compile(r"FindWindowW\(NULL, TITLE\)"))
        helper = source[source.index("static int owned_by_python"):source.index("int WINAPI WinMain")]
        for needle in ("FindWindowExW(NULL, window, NULL, TITLE)", "QueryFullProcessImageNameW",
                       'L"pythonw.exe"', 'L"python.exe"'):
            self.assertIn(needle, helper)


if __name__ == "__main__":
    unittest.main()
