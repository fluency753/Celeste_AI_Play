"""Render the real console with isolated HTTP fixtures; prompt headers must not cover tabs."""
import asyncio
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse


CONSOLE = Path(__file__).resolve().parents[1] / 'overlay/console.html'


class ConsolePromptLayoutTests(unittest.TestCase):
    def test_prompt_headers_scroll_under_clickable_console_navigation(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is needed to check console layout')

        text = '# 提示词\n' + '足够长的正文，用于验证滚动后的菜单。\n' * 55
        preview = {
            'text': text, 'executor': 'fixture', 'readonly': True,
            'parts': [{'label': '系统提示词', 'source': 'system_zh.md', 'text': text}],
            'guide_parts': [{'label': 'explore_notes_zh.md', 'source': 'explore_notes_zh.md',
                             'text': text, 'sep': '\n'}],
            'first_message': text, 'overridden': [],
        }
        responses = {
            '/api/console/options': {'runtimes': [], 'defaults': {'max_minutes': 60},
                                     'saves': [], 'chapters': [], 'tunables': [], 'tunable_groups': []},
            '/api/console/kb': {'documents': [], 'deny': [], 'allow': []},
            '/api/console/prompt/preview': preview,
            '/api/console/personal-prompt': {'catalog': [{'scope': 'all', 'label': '通用'}],
                                            'text': '', 'max_chars': 1000, 'readonly': True},
            '/api/console/runs': [],
            '/api/console/status': {'game': {}, 'obs': {}, 'orchestrator': {}},
        }
        html = CONSOLE.read_text(encoding='utf-8')

        def handle(route):
            url = urlparse(route.request.url)
            if url.netloc != 'console.test':
                return route.abort()  # no fonts, live controller, or other external requests
            if url.path == '/console':
                return route.fulfill(content_type='text/html; charset=utf-8', body=html)
            return route.fulfill(json=responses.get(url.path, {}))

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                self.skipTest(f'Chromium unavailable: {error}')
            try:
                for width in (1835, 800):
                    with self.subTest(width=width):
                        page = browser.new_page(viewport={'width': width, 'height': 900})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.route('**/*', handle)
                        page.goto('http://console.test/console#prompts')
                        headers = page.locator('#promptPreview .pblock > header')
                        headers.last.wait_for()
                        self.assertEqual(headers.count(), 3)  # file, workspace guide, first message
                        for index in range(headers.count()):
                            header = headers.nth(index)
                            self.assertEqual(header.evaluate('e => getComputedStyle(e).position'), 'static')
                            self.assertEqual(header.evaluate('e => getComputedStyle(e).zIndex'), 'auto')
                            # Put this file header through the top bar, as in the reported overlap.
                            header.evaluate('e => window.scrollTo(0, e.getBoundingClientRect().top + scrollY - 10)')
                            nav = page.locator('body > header')
                            self.assertAlmostEqual(nav.bounding_box()['y'], 0, delta=1)
                            tabs = page.locator('.tabs .tab')
                            for tab in tabs.all():
                                self.assertTrue(tab.evaluate('''e => {
                                    const r = e.getBoundingClientRect();
                                    return e.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
                                }'''))
                        # Exercise the real jump consumer, then change pages without a forced click.
                        page.locator('#promptParts .prow button').nth(1).click()
                        page.locator('.tab[data-page="run"]').click()
                        self.assertTrue(page.locator('#page-prompts').is_hidden())
                        self.assertTrue(page.locator('#page-run').is_visible())
                        self.assertEqual(errors, [])
                        page.close()
            finally:
                browser.close()


class ConsoleStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_dependencies_and_live_status_preserve_saved_runtime(self):
        try:
            from playwright.async_api import async_playwright, expect
        except ImportError:
            self.skipTest('Playwright is needed to check console startup')

        from orchestrator.launch_options import DEFAULTS, normalize

        html = CONSOLE.read_text(encoding='utf-8')
        release_font, release_initial, release_stale = (asyncio.Event() for _ in range(3))
        models_seen, preview_seen = [], []
        responses = {
            '/api/console/options': {
                'runtimes': [{'id': 'codex:codex', 'label': 'Codex'},
                             {'id': 'claude:anthropic', 'label': 'Claude'}, {'id': 'claude:other', 'label': 'Other'}],
                'defaults': DEFAULTS, 'saves': [{'name': 'test', 'slot': 0}], 'chapters': [],
                'tunables': [], 'tunable_groups': [],
                # 运行方式是本机设置（1.17.0 起不随配置保存；表单里旧的 runtime 不再采用）
                'machine': {'runtime': 'host'},
                'profiles': {'stored': True, 'selected': 'saved', 'profiles': [{'id': 'saved', 'name': '保存的配置'}],
                             'current': {'id': 'saved', 'form': {'engine': 'claude', 'provider': 'other', 'model': 'saved-model',
                                                                 'effort': 'high', 'runtime': 'host', 'agent': 'saved-agent'}}},
            },
            '/api/console/kb': {'documents': [], 'deny': [], 'allow': []},
            '/api/console/runs': [],
            '/api/console/personal-prompt': {'catalog': [{'scope': 'all', 'label': '通用'}],
                                            'text': '', 'max_chars': 1000, 'readonly': True},
            '/api/console/status': {'game': {}, 'obs': {}, 'orchestrator': {'running': True}},
            '/api/control': {'control': {'connected': True}},
            '/hud/state': {'metrics': {'status': 'paused', 'play_mode': 'explore', 'hud_mod': True}},
            '/hud/history': {'items': [{'id': 'fixture', 'text': '运行中的公开消息'}]},
        }

        async def handle(route):
            url = urlparse(route.request.url)
            if url.hostname == 'fonts.googleapis.com':
                await release_font.wait()
                return await route.fulfill(body='', content_type='text/css')
            if url.netloc != 'console.test':
                return await route.abort()
            if url.path == '/console':
                return await route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if url.path == '/api/models':
                provider = parse_qs(url.query)['provider'][0]
                models_seen.append(provider)
                if provider == 'other':
                    await release_initial.wait()
                if provider == 'codex':
                    await release_stale.wait()
                ident = {'other': 'saved-model', 'codex': 'stale-model', 'anthropic': 'opus'}[provider]
                return await route.fulfill(json={'models': [{'id': ident, 'efforts': ['medium', 'high'], 'default_effort': 'medium'}]})
            if url.path in ('/api/console/preview', '/api/console/prompt/preview'):
                options = route.request.post_data_json['options']
                preview_seen.append(options)
                try:
                    await asyncio.to_thread(normalize, options)
                except ValueError as exc:
                    return await route.fulfill(status=400, json={'error': str(exc)})
                return await route.fulfill(json={'command': '预览成功', 'text': '提示词正文', 'parts': [],
                                                 'overridden': [], 'executor': 'fixture'})
            return await route.fulfill(json=responses.get(url.path, {}))

        async with async_playwright() as playwright:
            try:
                browser = await playwright.chromium.launch()
            except Exception as error:
                self.skipTest(f'Chromium unavailable: {error}')
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.route('**/*', handle)
                await page.goto('http://console.test/console#prompts', wait_until='domcontentloaded', timeout=5000)
                await expect(page.locator('#profile')).to_have_value('saved')
                await expect(page.locator('#promptWhat')).to_have_text('正在读取运行设置…')
                await page.locator('.tab[data-page="run"]').click()
                await expect(page.locator('#runModeSummary')).to_have_text('explore · 游戏内 HUD')
                await expect(page.locator('#feed')).to_contain_text('运行中的公开消息')
                await expect(page.locator('#runMode option')).to_have_count(2)
                await expect(page.locator('#runMode')).to_have_value('host')
                await expect(page.locator('#start')).to_be_disabled()
                self.assertEqual(preview_seen, [])

                release_font.set()
                release_initial.set()
                await expect(page.locator('#model')).to_have_value('saved-model')
                await expect(page.locator('#effort')).to_have_value('high')
                await expect(page.locator('#profileSave')).to_be_disabled()
                await page.locator('.tab[data-page="prompts"]').click()
                await expect(page.locator('#promptWhat')).to_contain_text('启动')
                self.assertEqual(models_seen, ['other'])
                self.assertTrue(all(options['runtime'] == 'host' for options in preview_seen))

                await page.locator('.tab[data-page="run"]').click()
                async with page.expect_request(lambda r: 'provider=codex' in r.url):
                    await page.locator('#runtime').select_option('codex:codex')
                await page.locator('#runtime').select_option('claude:anthropic')
                await expect(page.locator('#model')).to_have_value('opus')
                async with page.expect_event('requestfinished', predicate=lambda r: 'provider=codex' in r.url):
                    release_stale.set()
                async with page.expect_request(lambda r: r.url.endswith('/api/console/prompt/preview')) as final_preview:
                    await page.locator('.tab[data-page="prompts"]').click()
                self.assertEqual((await final_preview.value).post_data_json['options']['model'], 'opus')
                self.assertEqual(await page.locator('#model').input_value(), 'opus')

                await page.route('**/api/console/options', lambda r: r.fulfill(status=503, json={'error': '服务未就绪'}))
                await page.reload(wait_until='domcontentloaded')
                await expect(page.locator('#promptWhat')).to_have_text('读取设置失败：服务未就绪')
                await expect(page.locator('#start')).to_be_disabled()
                self.assertEqual(errors, [])
            finally:
                release_font.set()
                release_initial.set()
                release_stale.set()
                await browser.close()


if __name__ == '__main__':
    unittest.main()
