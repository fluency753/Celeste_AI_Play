"""本次运行的设置与固定项（docs/开发/设置分层.md 第三步），用真实的控制台页面与隔离的接口核对：

- 旧版配置没有 pins：存过的本次运行设置全部算固定，值取配置里的；没固定的取记住的上次填写；
  直播与录屏取记住的值，配置里旧的值不再采用；
- 改没固定的项：记成上次填写的，不算配置的未保存改动；改固定的项、勾选或取消固定：算配置的改动；
- 页面里的分组与主控（machine_settings.RUN_GROUPS）一致。
"""
import asyncio
import json
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from orchestrator.launch_options import DEFAULTS
from orchestrator.machine_settings import DEBUG_KEYS, INDEPENDENT_KEYS, RUN_GROUPS

CONSOLE = Path(__file__).resolve().parents[1] / "overlay" / "console.html"


class GroupTests(unittest.TestCase):
    def test_page_groups_match_the_controller(self):
        html = CONSOLE.read_text(encoding="utf-8")
        block = re.search(r"const RUN_GROUPS = (\{.*?\});", html, re.S).group(1)
        groups = json.loads(re.sub(r"(\w+):", r'"\1":', block))
        self.assertEqual(groups, {k: list(v) for k, v in RUN_GROUPS.items()})
        self.assertIn(f"const INDEPENDENT_KEYS = {json.dumps(list(INDEPENDENT_KEYS))};", html)
        self.assertIn(f"const DEBUG_KEYS = {json.dumps(list(DEBUG_KEYS))};".replace('", "', '", "'), html)
        for group in RUN_GROUPS:
            self.assertIn(f'data-pin="{group}"', html)


class PinPageTests(unittest.TestCase):
    def test_pinned_values_come_from_the_profile_and_the_rest_is_remembered(self):
        asyncio.run(self.check())

    async def check(self):
        try:
            from playwright.async_api import async_playwright, expect
        except ImportError:
            self.skipTest("Playwright is needed to check the console")
        html = CONSOLE.read_text(encoding="utf-8")
        legacy_form = {"engine": "claude", "provider": "anthropic", "model": "opus", "effort": "high", "mode": "explore",
                       "chapters": [1], "max_minutes": 600, "live": True, "record": True, "recording_mode": "continuous"}
        last = {"agent": "dev-last", "max_minutes": 30, "live": False, "record": False,
                "save": {"name": "test", "slot": 7, "create": False}}
        posted_last = []
        responses = {
            "/api/console/options": {
                "runtimes": [{"id": "claude:anthropic", "engine": "claude", "provider": "anthropic", "label": "Claude"}],
                "defaults": DEFAULTS, "saves": [{"name": "test", "slot": 7}],
                "chapters": [{"id": 0, "label": "序章"}, {"id": 1, "label": "1A"}], "sides": [], "checkpoints": {},
                "tunables": [], "tunable_groups": [], "maps": [{"id": "celeste", "label": "蔚蓝", "enabled": True}],
                "machine": {}, "provider_usage": {}, "last": last,
                "profiles": {"stored": True, "selected": "mine", "profiles": [{"id": "mine", "name": "我的"}],
                             "current": {"id": "mine", "name": "我的", "readonly": False, "form": legacy_form}},
            },
            "/api/console/kb": {"documents": [], "deny": [], "allow": []},
            "/api/console/runs": [],
            "/api/console/status": {"game": {}, "obs": {}, "orchestrator": {"running": False}},
        }

        async def handle(route):
            url = urlparse(route.request.url)
            if url.netloc != "console.test":
                return await route.abort()
            if url.path == "/console":
                return await route.fulfill(content_type="text/html; charset=utf-8", body=html)
            if url.path == "/api/models":
                self.assertEqual(parse_qs(url.query)["provider"], ["anthropic"])
                return await route.fulfill(json={"models": [{"id": "opus", "label": "Opus 5.5", "efforts": ["high", "max"],
                                                             "default_effort": "high"}]})
            if url.path == "/api/console/last":
                values = route.request.post_data_json["values"]
                posted_last.append(values)
                return await route.fulfill(json={"last": values})
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
                pins = {g: await page.locator(f'[data-pin="{g}"]').is_checked() for g in RUN_GROUPS}
                self.assertEqual({g for g, on in pins.items() if on}, {"model", "chapters", "max_minutes"})
                await expect(page.locator("#maxMinutes")).to_have_value("600")        # 固定：配置里的
                await expect(page.locator("#agent")).to_have_value("dev-last")         # 没固定：上次填写的
                self.assertFalse(await page.locator("#live").is_checked())             # 直播与录屏：记住的，不按旧配置
                self.assertFalse(await page.locator("#record").is_checked())
                await expect(page.locator("#profileSave")).to_be_disabled()

                await page.fill("#agent", "dev-new")
                await page.dispatch_event("#agent", "change")
                await page.wait_for_function("() => document.querySelector('#profileSave').disabled")
                await page.wait_for_timeout(1200)
                self.assertTrue(posted_last)
                self.assertEqual(posted_last[-1]["agent"], "dev-new")
                self.assertNotIn("max_minutes", posted_last[-1])                       # 固定的项不记成上次填写
                self.assertNotIn("model", posted_last[-1])
                self.assertEqual((posted_last[-1]["live"], posted_last[-1]["record"]), (False, False))
                await expect(page.locator("#profileSave")).to_be_disabled()            # 不算配置的改动

                await page.fill("#maxMinutes", "700")
                await page.dispatch_event("#maxMinutes", "change")
                await expect(page.locator("#profileSave")).to_be_enabled()             # 固定的项改了：配置的改动
                await page.fill("#maxMinutes", "600")
                await page.dispatch_event("#maxMinutes", "change")
                await expect(page.locator("#profileSave")).to_be_disabled()

                await page.locator('#pinBox summary').click()
                await page.check('[data-pin="agent"]')
                await expect(page.locator("#profileSave")).to_be_enabled()
                form = await page.evaluate("profileForm()")
                self.assertEqual(form["pins"], ["agent", "chapters", "max_minutes", "model"])
                self.assertEqual(form["agent"], "dev-new")
                for key in ("live", "record", "recording_mode", "save", "start_paused", *DEBUG_KEYS):
                    self.assertNotIn(key, form)
                self.assertEqual(errors, [])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
