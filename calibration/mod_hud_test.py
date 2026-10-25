"""游戏内 HUD（--hud-mod）：显示内容、画图、共享内存协议与开关默认值。"""
import json
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.run import parse_args
from overlay import modhud
from overlay.modhud import (HEADER_SIZE, IMAGE_MAX, MAPPING_SIZE, OFF_ENABLED, OFF_GLYPHS_N, OFF_HEARTBEAT, OFF_IMG_W,
                            OFF_SEQ, OFF_TIMER_BASE, OFF_TIMER_LIVE, OFF_TIMER_X, OFF_VIEW_W, ModHud, SharedHud,
                            make_atlas, model_key, premultiplied, render, view_model)

CONFIG = json.loads((Path(__file__).resolve().parent.parent / "configs" / "hud.json").read_text(encoding="utf-8"))


def metrics(now, **extra):
    m = {"agent": "dev-ds", "model": "deepseek-flash[1m]", "effort": "xhigh", "effort_pending": "max",
         "status": "thinking", "started_wall": now - 600, "thinking_s": 100.0, "thinking_since_wall": now - 20,
         "tool_s": 50.0, "context_tokens": 950000, "compactions": 2, "deaths": 3, "sim_calls": 4,
         "sim_round_rollouts": 25000, "sim_best_frames": 120, "chapter": "Celeste/1-ForsakenCity", "room": "2",
         "chapter_time_ticks": 123_456_789, "completed_ticks": 500_000_000, "dashes": 0, "max_dashes": 1,
         "live": True, "thought": "贴墙跳上去再冲刺。", "thought_updated_wall": now - 1,
         "splits": [{"chapter": "Celeste/0-Intro", "ticks": 500_000_000}]}
    m.update(extra)
    return m


class ViewModelTests(unittest.TestCase):
    def test_old_design_fields(self):
        now = time.time()
        vm = view_model(metrics(now), CONFIG, [{"id": "a", "text": "进入房间", "kind": "解说", "wall": now - 30}], now)
        self.assertEqual(vm.name, "DeepSeek V4.1 Flash")              # 按模型别名查名字，不显示原始 ID
        self.assertEqual((vm.status, vm.room, vm.room_id), ("思考中", "1A", "2"))
        self.assertEqual(vm.theme, CONFIG["colors"]["dash0"])          # 冲刺用完：蓝发
        self.assertEqual((vm.effort, vm.effort_level, vm.effort_pending), ("xhigh", 3, "→ max"))
        stats = {k: (v, tone) for k, v, tone in vm.stats}
        self.assertEqual(stats["思考"][0], "0:02:00")                  # 100 秒 + 正在思考的 20 秒
        self.assertEqual(stats["上下文"], ("950k", "warn"))
        self.assertEqual(stats["推演路径"][0], "2.5万 条")
        self.assertEqual(stats["推演最速"][0], "2.000s")
        self.assertEqual(stats["知识库读取"], ("—", "dim"))
        self.assertEqual([e.text for e in vm.history], ["进入房间", "贴墙跳上去再冲刺。"])   # 最新一句补在末尾
        self.assertTrue(vm.history[-1].latest and not vm.history[0].latest)
        self.assertEqual((vm.timer_base, vm.timer_extra, vm.timer_live), (500_000_000, 123_456_789, True))

    def test_sim_energy_shows_balance_only(self):
        now = time.time()
        energy = {"balance": 37, "capacity": 100, "rules": "体力上限100……4×额外候选数；最多6条、每条180帧"}
        stats = {k: v for k, v, _ in view_model(metrics(now, sim_energy=energy), CONFIG, [], now).stats}
        self.assertEqual(stats["推演额度"], "37 / 100")                 # 规则说明只在网页 HUD 的悬停提示里
        hidden = {k for k, _, _ in view_model(metrics(now, sim_stats=False), CONFIG, [], now).stats}
        self.assertFalse(hidden & {"模拟器调用", "推演路径", "推演最速", "推演额度"})   # 不能推演时这几栏不显示
        self.assertIn("死亡", hidden)

    def test_chapter_done_stops_live_timer_and_replay_meta(self):
        now = time.time()
        m = metrics(now, chapter="Celeste/0-Intro", replaying=True, replay_round=2, replay_runs=1234,
                    replay_label="试三种起跳")
        vm = view_model(m, CONFIG, [], now)
        self.assertFalse(vm.timer_live)
        self.assertEqual(vm.status, "规划回放")
        self.assertEqual(vm.meta, "第 2 轮规划 · 推演 1,234 条路线 · 试三种起跳")

    def test_timers_do_not_change_the_view_model_identity(self):
        now = time.time()
        a = view_model(metrics(now), CONFIG, [], now)
        b = view_model(metrics(now, chapter_time_ticks=999), CONFIG, [], now)
        self.assertEqual(a, b)            # 章节计时由桥接逐帧画，计时变化不重画卡片

    def test_model_key(self):
        self.assertEqual(model_key("claude-opus-5-5[1m]"), "opus")
        self.assertEqual(model_key("deepseek-flash[1m]"), "deepseek-flash")


class RenderTests(unittest.TestCase):
    def test_card_and_atlas(self):
        now = time.time()
        vm = view_model(metrics(now), CONFIG, [{"id": str(i), "text": "很长的一句解说" * 8, "kind": "解说", "wall": now}
                                               for i in range(8)], now)
        for width in (350, 700):
            card = render(vm, width, CONFIG["colors"])
            self.assertEqual(card.image.width, width)
            self.assertLess(card.image.height, width * 2.6)             # 历史一栏有上限（放得下三条长消息），卡片不会无限长
            short = render(vm, width, CONFIG["colors"], max_height=int(width * 1.5))
            self.assertLessEqual(short.image.height, int(width * 1.5))   # 显示区矮时思考语句一栏让出高度
            self.assertTrue(0 < card.timer_x < width / 3 and 0 < card.timer_y < card.image.height / 3)
            self.assertEqual(len(premultiplied(card.image)), width * card.image.height * 4)
        atlas = make_atlas(1.0, CONFIG["colors"])
        chars = {(chr(g[0]), g[1]) for g in atlas.glyphs}
        self.assertTrue(all((c, s) in chars for c in "0123456789:." for s in (0, 1)))
        digits = {g[8] for g in atlas.glyphs if chr(g[0]).isdigit() and g[1] == 0}
        self.assertEqual(len(digits), 1)                               # 数字等宽，计时跳动时不抖

    def test_premultiplied_alpha(self):
        from PIL import Image
        image = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
        self.assertEqual(premultiplied(image), bytes([100, 50, 25, 128]))


class SharedMemoryTests(unittest.TestCase):
    def test_publish_protocol(self):
        shared = SharedHud(buffer=bytearray(MAPPING_SIZE))
        shared.publish(b"\x01" * 16, (2, 2), b"\x02" * 8, (2, 1), [(48, 0, 0, 0, 1, 1, 0, -1, 1)], (5, 6), (7, 8))
        buf = shared.buf
        self.assertEqual(struct.unpack_from("<II", buf, 0), (modhud.MAGIC, modhud.LAYOUT_VERSION))
        seq = struct.unpack_from("<q", buf, OFF_SEQ)[0]
        self.assertTrue(seq > 0 and seq % 2 == 0)                       # 偶数：写完了
        self.assertEqual(struct.unpack_from("<iiii", buf, OFF_IMG_W), (2, 2, 2, 1))
        self.assertEqual(struct.unpack_from("<ii", buf, OFF_TIMER_X), (5, 6))
        self.assertEqual(struct.unpack_from("<iii", buf, OFF_GLYPHS_N), (1, 7, 8))
        self.assertEqual(bytes(buf[HEADER_SIZE:HEADER_SIZE + 16]), b"\x01" * 16)
        self.assertEqual(bytes(buf[HEADER_SIZE + IMAGE_MAX:HEADER_SIZE + IMAGE_MAX + 8]), b"\x02" * 8)
        self.assertEqual(struct.unpack_from("<i", buf, OFF_ENABLED)[0], 1)
        shared.timer(10, 20, False)
        self.assertEqual(struct.unpack_from("<qq", buf, OFF_TIMER_BASE), (10, 20))
        self.assertEqual(struct.unpack_from("<i", buf, OFF_TIMER_LIVE)[0], 0)
        shared.disable()
        self.assertEqual(struct.unpack_from("<i", buf, OFF_ENABLED)[0], 0)
        self.assertEqual(struct.unpack_from("<q", buf, OFF_HEARTBEAT)[0], 0)

    def test_redraws_only_when_content_or_size_changes(self):
        now = time.time()
        m = metrics(now, status="executing")
        with tempfile.TemporaryDirectory() as folder:
            shared = SharedHud(buffer=bytearray(MAPPING_SIZE))
            hud = ModHud(Path(folder), shared=shared, metrics=m)
            with patch("overlay.history.read_history", return_value=[]):
                self.assertTrue(hud.tick())
                width = struct.unpack_from("<i", shared.buf, OFF_IMG_W)[0]
                self.assertEqual(width, 350)                            # 显示区大小未知时按 1920 宽
                self.assertFalse(hud.tick())
                m["chapter_time_ticks"] += 10_000_000                   # 只有计时变了：不重画，计时字段照写
                self.assertFalse(hud.tick())
                self.assertEqual(struct.unpack_from("<q", shared.buf, OFF_TIMER_BASE + 8)[0], m["chapter_time_ticks"])
                m["thought"] = "换一条路线"
                self.assertTrue(hud.tick())
                struct.pack_into("<ii", shared.buf, OFF_VIEW_W, 2560, 1440)   # 桥接报告显示区变大
                self.assertTrue(hud.tick())
                self.assertEqual(struct.unpack_from("<i", shared.buf, OFF_IMG_W)[0], round(350 * 2560 / 1920))
            hud.close()
            self.assertEqual(struct.unpack_from("<i", shared.buf, OFF_ENABLED)[0], 0)

    def test_reads_metrics_file_and_hides_until_there_is_one(self):
        with tempfile.TemporaryDirectory() as folder:
            shared = SharedHud(buffer=bytearray(MAPPING_SIZE))
            hud = ModHud(Path(folder), shared=shared)
            with patch("overlay.history.read_history", return_value=[]):
                self.assertFalse(hud.tick())
                self.assertEqual(struct.unpack_from("<i", shared.buf, OFF_ENABLED)[0], 0)
                (Path(folder) / "metrics.json").write_text(json.dumps(metrics(time.time())), encoding="utf-8")
                self.assertTrue(hud.tick())
                self.assertEqual(struct.unpack_from("<i", shared.buf, OFF_ENABLED)[0], 1)


class FlagTests(unittest.TestCase):
    def parse(self, root, *extra):
        argv = ['run', '--engine', 'codex', '--agent', 'test', '--save', 'selected', '--chapter', '0', *extra]
        with patch('orchestrator.config.REPO_ROOT', root), patch.object(sys, 'argv', argv):
            return parse_args()[0]

    def test_mod_hud_is_default_without_live(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertTrue(self.parse(root).hud_mod)
            self.assertFalse(self.parse(root, '--live').hud_mod)
            self.assertTrue(self.parse(root, '--live', '--hud-mod').hud_mod)
            self.assertFalse(self.parse(root, '--no-hud-mod').hud_mod)


class PageVersionTests(unittest.TestCase):
    def test_web_hud_page_version_matches_config(self):
        # 不一致时网页 HUD 每次轮询都会以为自己过期、一直刷新
        import re
        root = Path(__file__).resolve().parent.parent
        page = re.search(r"const PAGE_VERSION = (\d+);", (root / "overlay" / "hud.html").read_text(encoding="utf-8"))
        config = json.loads((root / "configs" / "hud.json").read_text(encoding="utf-8"))
        self.assertEqual(int(page.group(1)), config["page_version"])


if __name__ == "__main__":
    unittest.main()
