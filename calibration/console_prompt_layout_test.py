"""Render the real console with isolated HTTP fixtures; prompt headers must not cover tabs."""
from pathlib import Path
import unittest


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
                                     'saves': [], 'chapters': [], 'tunables': []},
            '/api/console/prompt/preview': preview,
            '/api/console/personal-prompt': {'catalog': [{'scope': 'all', 'label': '通用'}],
                                            'text': '', 'max_chars': 1000, 'readonly': True},
            '/api/console/runs': [],
            '/api/console/status': {'game': {}, 'obs': {}, 'orchestrator': {}},
        }
        html = CONSOLE.read_text(encoding='utf-8')

        def handle(route):
            from urllib.parse import urlparse
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


if __name__ == '__main__':
    unittest.main()
