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
        text = '第一行会话文本。\n第二行文本。\n\n代码：\n    print("保留缩进")\n最后一行。'
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
            if url.path == '/hud/tools':
                return route.fulfill(json={'run': 'fixture', 'items': [
                    {'id': 'tool', 'tool': 'fixture', 'text': '工具复制测试', 'status': 'returned',
                     'parameters': {'a': 1}, 'result': {'b': 2}}]})
            return route.fulfill(status=404, json={})

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as error:
                self.skipTest(f'Chromium unavailable: {error}')
            try:
                context = browser.new_context(permissions=['clipboard-read', 'clipboard-write'],
                                              viewport={'width': 600, 'height': 1100})
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
                    const node = e.lastChild, range = document.createRange();
                    range.setStart(node, 0); range.setEnd(node, 1);
                    const first = range.getBoundingClientRect();
                    range.setStart(node, node.length - 1); range.setEnd(node, node.length);
                    const last = range.getBoundingClientRect();
                    return {x: first.x, y: first.y + first.height / 2,
                            endX: last.right, endY: last.y + last.height / 2};
                }''')
                page.mouse.move(bounds['x'], bounds['y'])
                page.mouse.down()
                page.mouse.move(bounds['endX'], bounds['endY'], steps=10)
                page.mouse.up()
                selected = page.evaluate('getSelection().toString()')
                self.assertEqual(text, selected, str(bounds))
                items.append({'id': 'second', 'text': '新消息不会清除已有选区。', 'kind': '解说'})
                page.wait_for_function("document.querySelectorAll('#thought .thought-entry').length === 2")
                self.assertEqual(page.evaluate('getSelection().toString()'), selected)
                page.keyboard.press('Control+c')
                self.assertEqual(page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n'), selected)
                formats = page.evaluate('''async () =>
                    (await navigator.clipboard.read()).flatMap(item => item.types)''')
                self.assertEqual(formats, ['text/plain'])
                page.evaluate('''() => {
                    const target = document.createElement('textarea');
                    target.id = 'pasteTarget';
                    target.style.cssText = 'position:fixed;inset:0;background:white;color:black;z-index:1000';
                    document.body.append(target); target.focus();
                }''')
                page.keyboard.press('Control+v')
                self.assertEqual(page.locator('#pasteTarget').input_value(), text)
                page.locator('#pasteTarget').evaluate('e => e.remove()')
                page.fill('#agentInput', '输入框第一行\n第二行')
                page.press('#agentInput', 'Control+a')
                page.press('#agentInput', 'Control+c')
                self.assertEqual(page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n'),
                                 '输入框第一行\n第二行')
                page.click('#showTools')
                page.click('#toolRows summary')
                tool_text = page.locator('#toolRows pre').text_content()
                self.assertIn('\n', tool_text)
                page.locator('#toolRows pre').select_text()
                page.keyboard.press('Control+c')
                self.assertEqual(page.evaluate('navigator.clipboard.readText()').replace('\r\n', '\n'), tool_text)
                self.assertEqual(page.evaluate('''async () =>
                    (await navigator.clipboard.read()).flatMap(item => item.types)'''), ['text/plain'])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
