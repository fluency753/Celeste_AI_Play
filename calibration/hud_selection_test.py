"""Select and copy real HUD messages without contacting a running controller."""
from pathlib import Path
import time
import unittest
from urllib.parse import urlparse


class HudSelectionTests(unittest.TestCase):
    def test_selected_message_survives_history_refresh_and_copies_to_clipboard(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is needed to run the real HUD page')
        html = (Path(__file__).resolve().parents[1] / 'overlay/hud.html').read_text(encoding='utf-8')
        text = '这是一段可以复制的会话文本。'
        items = [{'id': 'first', 'text': text, 'kind': '解说'}]

        def handle(route):
            url = urlparse(route.request.url)
            if url.netloc != 'hud.test':
                return route.abort()
            if url.path == '/hud':
                return route.fulfill(content_type='text/html; charset=utf-8', body=html)
            if url.path == '/hud/state':
                return route.fulfill(json={'now': time.time(), 'config': {},
                                           'metrics': {'status': 'paused', 'model': 'fixture'}})
            if url.path == '/hud/history':
                return route.fulfill(json={'run': 'fixture', 'items': items})
            if url.path == '/api/control':
                return route.fulfill(json={'run': 'fixture', 'control': {'connected': False}})
            return route.fulfill(status=404, json={})

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                self.skipTest(f'Chromium unavailable: {error}')
            try:
                context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'])
                page = context.new_page()
                page.route('**/*', handle)
                page.goto('https://hud.test/hud')
                entry = page.locator('#thought .thought-entry').first
                entry.wait_for()
                page.evaluate("document.documentElement.classList.add('hud-desktop')")
                entry.scroll_into_view_if_needed()
                self.assertEqual(page.locator('.head').evaluate('e => getComputedStyle(e).userSelect'), 'none')
                # Use mouse selection, so CSS and pointer handlers participate.
                bounds = entry.evaluate('''e => {
                    const range = document.createRange(); range.selectNodeContents(e.lastChild);
                    const r = range.getBoundingClientRect();
                    return {x: r.x, y: r.y, width: r.width, height: r.height};
                }''')
                y = bounds['y'] + bounds['height'] / 2
                page.mouse.move(bounds['x'], y)
                page.mouse.down()
                page.mouse.move(bounds['x'] + bounds['width'], y, steps=10)
                page.mouse.up()
                selected = page.evaluate('getSelection().toString()')
                self.assertIn(text, selected, str(bounds))
                items.append({'id': 'second', 'text': '新消息不会清除已有选区。', 'kind': '解说'})
                page.wait_for_function("document.querySelectorAll('#thought .thought-entry').length === 2")
                self.assertEqual(page.evaluate('getSelection().toString()'), selected)
                page.keyboard.press('Control+c')
                self.assertEqual(page.evaluate('navigator.clipboard.readText()'), selected)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
