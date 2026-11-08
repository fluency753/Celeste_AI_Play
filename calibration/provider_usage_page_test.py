"""Exercise account switches in the real console with isolated HTTP boundaries."""
import asyncio
from pathlib import Path
from urllib.parse import urlparse
import unittest

from orchestrator.launch_options import DEFAULTS


class ProviderUsagePageTests(unittest.TestCase):
    def test_pending_account_edits_keep_owner_and_order_across_switches(self):
        asyncio.run(self.check())

    async def check(self):
        try:
            from playwright.async_api import async_playwright, expect
        except ImportError:
            self.skipTest('Playwright is needed to check the console')
        posted = []
        fail_next = False
        options = {
            'runtimes': [{'id': 'claude:anthropic', 'engine': 'claude', 'provider': 'anthropic', 'label': 'A'},
                         {'id': 'claude:other', 'engine': 'claude', 'provider': 'other', 'label': 'B'}],
            'defaults': DEFAULTS, 'saves': [{'name': 'test', 'slot': 7}],
            'chapters': [{'id': 0, 'label': '序章'}], 'sides': [], 'checkpoints': {},
            'tunables': [], 'tunable_groups': [], 'maps': [{'id': 'celeste', 'label': '蔚蓝', 'enabled': True}],
            'machine': {}, 'provider_usage': {
                'anthropic': {'quota_wait': True, 'quota_wait_max_hours': 24, 'max_usage': .9},
                'other': {'quota_wait': False, 'quota_wait_max_hours': 12, 'max_usage': .5}},
            'last': {}, 'profiles': {'stored': True, 'selected': 'mine',
                'profiles': [{'id': 'mine', 'name': '我的'}],
                'current': {'id': 'mine', 'name': '我的', 'readonly': False,
                            'form': {'pins': [], 'mode': 'explore'}}}}

        async def handle(route):
            nonlocal fail_next
            url = urlparse(route.request.url)
            if url.netloc != 'console.test':
                return await route.abort()
            if url.path == '/console':
                return await route.fulfill(content_type='text/html; charset=utf-8',
                    body=(Path(__file__).resolve().parents[1] / 'overlay/console.html').read_text(encoding='utf-8'))
            if url.path == '/api/console/options':
                return await route.fulfill(json=options)
            if url.path == '/api/models':
                return await route.fulfill(json={'models': [{'id': 'opus', 'efforts': ['high'], 'default_effort': 'high'}]})
            if url.path == '/api/console/provider-usage':
                body = route.request.post_data_json
                posted.append(body)
                if fail_next:
                    fail_next = False
                    return await route.fulfill(status=503, json={'error': 'temporary write failure'})
                await asyncio.sleep(.15)
                return await route.fulfill(json={'usage': body['usage']})
            if url.path in ('/api/console/preview', '/api/console/prompt/preview'):
                return await route.fulfill(json={'command': '预览', 'text': '', 'parts': [], 'overridden': []})
            responses = {'/api/console/kb': {'documents': [], 'deny': [], 'allow': []},
                         '/api/console/runs': [], '/api/console/status': {'game': {}, 'obs': {}, 'orchestrator': {'running': False}},
                         '/api/console/last': {'last': {}}}
            return await route.fulfill(json=responses.get(url.path, {}))

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch()
            except Exception as error:
                self.skipTest(f'Chromium unavailable: {error}')
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                await page.route('**/*', handle)
                await page.goto('http://console.test/console#run', wait_until='domcontentloaded')
                await expect(page.locator('#model')).to_have_value('opus')
                await page.fill('#maxUsage', '.8')
                await page.dispatch_event('#maxUsage', 'change')
                await page.select_option('#runtime', 'claude:other')
                await page.fill('#maxUsage', '.4')
                await page.dispatch_event('#maxUsage', 'change')
                await page.select_option('#runtime', 'claude:anthropic')
                await expect(page.locator('#maxUsage')).to_have_value('0.8')
                await page.evaluate('flushUsageSaves()')
                by_provider = {item['provider']: item['usage'] for item in posted}
                self.assertEqual(by_provider['anthropic'], {'quota_wait': True, 'quota_wait_max_hours': None, 'max_usage': .8})
                self.assertEqual(by_provider['other'], {'quota_wait': False, 'quota_wait_max_hours': 12, 'max_usage': .4})
                posted.clear()
                await page.fill('#maxUsage', '.7')
                await page.dispatch_event('#maxUsage', 'change')
                await page.evaluate('() => { flushUsageSaves(); }')
                await page.fill('#maxUsage', '.6')
                await page.dispatch_event('#maxUsage', 'change')
                await page.evaluate('flushUsageSaves()')
                self.assertEqual([item['usage']['max_usage'] for item in posted], [.7, .6])
                fail_next = True
                await page.fill('#maxUsage', '.55')
                await page.dispatch_event('#maxUsage', 'change')
                with self.assertRaisesRegex(Exception, '尚未保存'):
                    await page.evaluate('flushUsageSaves()')
                await page.evaluate('flushUsageSaves()')
                self.assertEqual([item['usage']['max_usage'] for item in posted][-2:], [.55, .55])
                self.assertEqual(errors, [])
            finally:
                await browser.close()
