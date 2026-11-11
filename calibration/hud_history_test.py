"""公开解说历史：完整文本、增量读取、隐私过滤和保留范围。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from overlay.history import JsonTail, PublicHistory, TAIL_BYTES, read_history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = self.root / "codex_events.jsonl"
        self.reader = PublicHistory(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def emit(self, item, stamp=10000):
        with self.events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"method": "item/completed", "params": {
                "completedAtMs": stamp, "item": item}}, ensure_ascii=False) + "\n")

    def test_full_public_message_without_reasoning_or_stream_fragments(self):
        self.emit({"type": "reasoning", "id": "secret", "text": "PRIVATE"})
        self.emit({"type": "agentMessage", "id": "public", "text": "完整解说" * 100})
        data = self.reader.snapshot({})
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["text"], "完整解说" * 100)
        self.assertNotIn("PRIVATE", json.dumps(data))
        self.assertEqual(self.reader.snapshot({}), data)

    def test_partial_line_waits_until_complete_and_decisions_are_ordered(self):
        raw = json.dumps({"method": "item/completed", "params": {"completedAtMs": 12000,
                         "item": {"type": "agentMessage", "id": "p", "text": "完整一句"}}}).encode()
        self.events.write_bytes(raw[:20])
        self.assertEqual(self.reader.snapshot({}), [])
        with self.events.open("ab") as stream:
            stream.write(raw[20:] + b"\n")
        (self.root / "decisions.jsonl").write_text(json.dumps({"i": 1, "t": 1, "thought": "先跳上台阶"}) + "\n", encoding="utf-8")
        result = self.reader.snapshot({"started_wall": 10})
        self.assertEqual([item["kind"] for item in result], ["尝试", "解说"])

    def test_only_recent_twelve_messages_are_retained(self):
        for n in range(80):
            self.emit({"type": "agentMessage", "id": str(n), "text": f"消息 {n}"}, stamp=n*1000)
        data = self.reader.snapshot({})
        self.assertEqual(len(data), 12)
        self.assertEqual(data[0]["text"], "消息 68")
        self.assertEqual(data[-1]["text"], "消息 79")

    def test_initial_history_read_skips_old_bytes_and_waits_for_complete_tail(self):
        path = self.root / "tail.jsonl"
        path.write_bytes(b'{"old":true}\n' + b'x' * TAIL_BYTES + b'\n{"recent":true}\n{"partial":')
        reader = JsonTail(path)
        self.assertEqual(reader.read(), [{"recent": True}])
        with path.open("ab") as stream:
            stream.write(b'true}\n')
        self.assertEqual(reader.read(), [{"partial": True}])

    def test_hud_http_backfills_same_session_after_restart_and_goal_change(self):
        import threading
        from urllib.request import Request, urlopen
        from orchestrator.control import RunControl
        from orchestrator.telemetry import publish
        from overlay.server import HudServer, make_handler

        old, current = self.root / "old", self.root / "current"
        state = {"engine": "codex", "session_id": "same-session", "agent": "fixture"}
        for directory, data in ((old, state), (current, {**state, "resumed_from": str(old)})):
            directory.mkdir()
            (directory / "run_state.json").write_text(json.dumps(data), encoding="utf-8")
        for n in range(15):
            publish(old, f"act:{n}:0", f"上一段消息 {n}", kind="尝试")
        old_control = RunControl(old)
        old_control.enqueue("message", "上一段用户消息")
        control = RunControl(current)
        control.heartbeat(goals_enabled=True)

        server = HudServer(("127.0.0.1", 0), make_handler(current))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f"http://127.0.0.1:{server.server_address[1]}"

        def history():
            with urlopen(url + "/hud/history", timeout=5) as response:
                return json.load(response)

        try:
            initial = history()
            self.assertEqual(initial["run"], str(current))
            self.assertEqual(len(initial["items"]), 12)
            self.assertEqual(initial["items"][0]["text"], "上一段消息 4")
            request = Request(url + "/api/control", json.dumps({
                "run": str(current), "kind": "goal",
                "text": json.dumps({"text": "新目标", "conditions": [], "match": "all"}),
            }).encode("utf-8"), {"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(history(), initial)

            publish(current, "act:14:0", "当前段消息", kind="尝试")
            items = history()["items"]
            self.assertEqual(len(items), 12)
            self.assertEqual([item["text"] for item in items[-3:]],
                             ["上一段消息 14", "上一段用户消息", "当前段消息"])
            self.assertEqual(len({item["id"] for item in items}), 12)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_backfill_stops_at_different_session_or_outside_run_group(self):
        group = self.root / "group"
        current, foreign = group / "current", group / "foreign"
        outside = self.root / "outside"
        state = {"engine": "codex", "session_id": "same-session", "agent": "fixture"}
        for directory, data in ((current, state), (foreign, {**state, "session_id": "other"}), (outside, state)):
            directory.mkdir(parents=True)
            (directory / "run_state.json").write_text(json.dumps(data), encoding="utf-8")
            (directory / "public_events.jsonl").write_text(
                json.dumps({"id": "msg", "wall": 1, "text": directory.name}) + "\n", encoding="utf-8")
        for previous in (foreign, outside):
            (current / "run_state.json").write_text(json.dumps({**state, "resumed_from": str(previous)}), encoding="utf-8")
            self.assertEqual([item["text"] for item in read_history(current, {})], ["current"])

    def test_compaction_system_message_updates_without_private_summary(self):
        started = {"method": "item/started", "params": {"startedAtMs": 123000,
                   "item": {"type": "contextCompaction", "id": "compact", "summary": "PRIVATE"}}}
        self.events.write_text(json.dumps(started) + "\n", encoding="utf-8")
        first = self.reader.snapshot({})
        self.assertEqual(first[0]["role"], "system")
        self.assertIn("正在压缩", first[0]["text"])
        self.emit({"type": "contextCompaction", "id": "compact", "summary": "PRIVATE"})
        final = self.reader.snapshot({})
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["text"], "上下文压缩完成。")
        self.assertEqual(final[0]["wall"], 123)
        self.assertNotIn("PRIVATE", json.dumps(final))

    def test_retry_public_events_and_decision_fallback_stay_hidden_across_polls(self):
        from orchestrator.telemetry import publish
        publish(self.root, 'act:1:0', '同一条说明', kind='尝试')
        publish(self.root, 'act:1:1', '同一条说明（自动重试 1）', kind='尝试')
        self.assertEqual([r['id'] for r in self.reader.snapshot({})], ['act:1:0'])
        (self.root/'decisions.jsonl').write_text('\n'.join(json.dumps({'i': 1, 'attempt': n,
            'thought': '同一条说明' + (f'（自动重试 {n}）' if n else '')}) for n in range(3))+'\n', encoding='utf-8')
        publish(self.root, 'act:2:0', '新说明', kind='尝试')
        for _ in range(2):
            self.assertEqual([r['id'] for r in self.reader.snapshot({})], ['act:1:0', 'act:2:0'])

    def test_retry_filter_precedes_retention_and_preserves_roles_and_real_public_text(self):
        from orchestrator.telemetry import publish
        publish(self.root, 'act:1:0', '原始说明')
        for n in range(1, 101):
            publish(self.root, f'act:1:{n}', f'原始说明（自动重试 {n}）')
        publish(self.root, 'public:normal', '原始说明（自动重试 1）')
        publish(self.root, 'act:7:1', '用户输入', role='user')
        publish(self.root, 'act:8:1', '系统通知', role='system')
        self.assertEqual([r['id'] for r in self.reader.snapshot({}, 80)],
                         ['act:1:0', 'public:normal', 'act:7:1', 'act:8:1'])

    def test_old_decision_only_records_with_unknown_attempt_are_not_guessed(self):
        values = [{'i': 1, 'thought': '旧首轮'}, {'i': 1, 'attempt': 1, 'thought': '旧重试'},
                  {'i': 2, 'attempt': -1, 'thought': '负数未知'},
                  {'i': 3, 'attempt': 'unknown', 'thought': '格式未知'}]
        (self.root/'decisions.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in values), encoding='utf-8')
        self.assertEqual({r['id'] for r in self.reader.snapshot({})}, {'act:1:0', 'act:2:-1', 'act:3:unknown'})

    def test_real_http_history_and_web_page_keep_only_first_retry_bubble(self):
        import threading
        from urllib.request import urlopen
        from overlay.server import HudServer, make_handler
        from orchestrator.telemetry import publish
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright is needed to check the HUD consumer')
        publish(self.root, 'act:1:0', '首条说明', kind='尝试')
        publish(self.root, 'act:1:1', '首条说明（自动重试 1）', kind='尝试')
        server = HudServer(('127.0.0.1', 0), make_handler(self.root))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with urlopen(url+'/hud/history', timeout=5) as response:
                self.assertEqual([r['id'] for r in json.load(response)['items']], ['act:1:0'])
            with sync_playwright() as playwright:
                try:
                    browser = playwright.chromium.launch()
                except Exception as exc:
                    self.skipTest(f'Chromium unavailable: {exc}')
                try:
                    page = browser.new_page(viewport={'width': 600, 'height': 1100})
                    pending = set()
                    page.on('request', lambda request: pending.add(request))
                    page.on('requestfinished', lambda request: pending.discard(request))
                    page.on('requestfailed', lambda request: pending.discard(request))
                    page.route('**/api/models*', lambda route: route.fulfill(json={'models': []}))
                    page.route('**/api/control', lambda route: route.fulfill(json={'run': str(self.root), 'control': {'connected': False}}))
                    page.route('**/hud/state', lambda route: route.fulfill(json={'config': {}, 'metrics': {'status': 'paused'}}))
                    page.goto(url+'/hud')
                    page.locator('#thought .thought-entry').first.wait_for()
                    self.assertEqual(page.locator('#thought .thought-entry').count(), 1)
                    publish(self.root, 'act:1:2', '首条说明（自动重试 2）', kind='尝试')
                    publish(self.root, 'act:2:0', '首条说明', kind='尝试')
                    page.evaluate('pollHistory()')
                    self.assertEqual(page.locator('#thought .thought-entry').count(), 1)
                    publish(self.root, 'act:3:0', '新说明', kind='尝试')
                    page.evaluate('pollHistory()')
                    page.wait_for_function("document.querySelectorAll('#thought .thought-entry').length === 2")
                    self.assertEqual(page.locator('#thought .thought-entry').evaluate_all('(nodes) => nodes.map(n => n.dataset.id)'),
                                     ['act:1:0', 'act:3:0'])
                    # Stop new polls and drain real requests before closing the
                    # browser, rather than disconnecting during a server write.
                    import time
                    page.route('**/*', lambda route: route.abort())
                    deadline = time.monotonic()+3
                    while pending and time.monotonic() < deadline:
                        page.wait_for_timeout(20)
                    self.assertFalse([request.url for request in pending])
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

    def test_consecutive_new_calls_collapse_and_intervening_messages_start_new_bubbles(self):
        from orchestrator.telemetry import publish
        for i in range(1, 24):
            publish(self.root, f'act:{i}:0', '相同说明', kind='尝试')
            self.assertEqual([r['id'] for r in self.reader.snapshot({})], ['act:1:0'])
        publish(self.root, 'narrate:1', '中间解说')
        publish(self.root, 'act:24:0', '相同说明', kind='尝试')
        publish(self.root, 'user:1', '用户介入', role='user')
        publish(self.root, 'act:25:0', '相同说明', kind='尝试')
        self.assertEqual([r['id'] for r in self.reader.snapshot({})],
                         ['act:1:0', 'narrate:1', 'act:24:0', 'user:1', 'act:25:0'])

    def test_hidden_action_completion_does_not_reappear_after_intervening_narration(self):
        path = self.root/'public_events.jsonl'
        values = [{'id': 'act:1:0', 'text': '相同', 'wall': 10},
                  {'id': 'act:2:0', 'text': '相同', 'wall': 11}]
        path.write_text(''.join(json.dumps(r)+'\n' for r in values), encoding='utf-8')
        first = self.reader.snapshot({})
        self.assertEqual([r['id'] for r in first], ['act:1:0'])
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'id': 'narrate:1', 'text': '新解说', 'wall': 12})+'\n')
        (self.root/'decisions.jsonl').write_text(json.dumps({'i': 2, 'attempt': 0,
            'thought': '相同', 't': 30})+'\n', encoding='utf-8')
        final = self.reader.snapshot({'started_wall': 0})
        self.assertEqual([r['id'] for r in final], ['act:1:0', 'narrate:1'])
        self.assertEqual(final[0]['wall'], first[0]['wall'])


if __name__ == "__main__":
    unittest.main()
