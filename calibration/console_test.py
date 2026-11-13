"""控制台：运行设置 → 命令行参数、运行记录与续跑、"中断"控制。"""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.control import RunControl
from orchestrator.launch_options import build, command_line, normalize, resume_args
from orchestrator.session_controls import SessionControls


def options(**extra):
    base = {"agent": "dev-opus", "save": {"name": "test", "slot": 7, "create": False}}
    base.update(extra)
    return base


class LaunchOptionsTests(unittest.TestCase):
    def test_chapter_recording_and_independent_menu_freeze_options(self):
        default = build(options())
        self.assertEqual(default[default.index('--recording-mode') + 1], 'chapter')
        for flag in ('--freeze-chapter-select', '--no-freeze-file-select', '--no-freeze-main-menu'):
            self.assertIn(flag, default)
        custom = build(options(recording_mode='continuous', freeze_chapter_select=False,
                               freeze_file_select=True, freeze_main_menu=True))
        self.assertEqual(custom[custom.index('--recording-mode') + 1], 'continuous')
        for flag in ('--no-freeze-chapter-select', '--freeze-file-select', '--freeze-main-menu'):
            self.assertIn(flag, custom)
        with self.assertRaises(ValueError):
            build(options(recording_mode='other'))

    def test_public_defaults(self):
        # 控制台与命令行的默认值一致：现实时间上限 1000 分钟，限额短路线预测与规划回放默认关，
        # 探索模式单次规划程序最长 120 秒（提示词写"2 分钟"）
        from orchestrator.launch_options import DEFAULTS
        from orchestrator.run import parse_args
        from orchestrator.tuning import BY_KEY
        # 预置模拟器按执行器默认（None）：Claude 在容器运行方式、本机构建了模拟器时开放 /sim，Codex 默认关
        self.assertEqual((DEFAULTS["max_minutes"], DEFAULTS["sim"], DEFAULTS["sim_assist"], DEFAULTS["playback"]),
                         (1000, None, False, False))
        self.assertEqual(BY_KEY["explore_program_s"].default, 120)
        self.assertEqual(BY_KEY["thought_max_hanzi"].default, 30)
        argv = build(options(mode="explore"))
        self.assertIn("--no-playback", argv)
        self.assertIn("--no-sim-assist", argv)
        self.assertEqual(argv[argv.index("--max-minutes") + 1], "1000")
        with patch("sys.argv", ["run", "--agent", "dev-x", "--save", "test", "--mode", "explore", "--runtime", "host"]):
            launched, _ = parse_args()
        self.assertEqual((launched.max_minutes, launched.playback, launched.sim_assist), (1000.0, False, False))

    def test_default_speedrun(self):
        argv = build(options())
        from orchestrator.launch_options import simulator_installed
        simulator = simulator_installed()
        for flag in ("--mode", "speedrun", "--no-live", "--sim" if simulator else "--no-sim", "--no-playback", "--record", "--hud-mod",
                     "--chapter", "--save-slot"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--agent") + 1], "dev-opus")
        self.assertNotIn("--no-study", argv)
        self.assertTrue(command_line(argv).startswith("python -m orchestrator.run --engine claude"))

    def test_codex_preset_simulation_requires_explicit_limit(self):
        for runtime in ('host', 'container'):
            with self.subTest(runtime=runtime):
                config = options(engine='codex', runtime=runtime, sim=True)
                with self.assertRaisesRegex(ValueError, '限额使用'):
                    normalize(config)
                config['limit_planning'] = True
                normalized = normalize(config)
                self.assertTrue(normalized['sim'])
                self.assertTrue(normalized['limit_planning'])
                argv = build(config)
                self.assertIn('--sim', argv)
                self.assertIn('--limit-planning', argv)
        self.assertFalse(normalize(options(engine='codex'))['sim'])          # Codex 默认关

    def test_claude_preset_simulator_keeps_unlimited_default_and_needs_container(self):
        from orchestrator.launch_options import config_for
        for limit in (False, True):
            with self.subTest(limit=limit):
                config = options(engine='claude', runtime='container', sim=True, limit_planning=limit)
                normalized = normalize(config)
                self.assertTrue(normalized['sim'])
                argv = build(config)
                self.assertIn('--sim', argv)
                self.assertEqual('--limit-planning' in argv, limit)
                cfg = config_for(config)
                self.assertTrue(cfg.sim)
                self.assertEqual(cfg.pause_limit, limit)
        with self.assertRaisesRegex(ValueError, '没有沙箱'):
            normalize(options(engine='claude', runtime='host', sim=True))
        self.assertFalse(normalize(options(engine='claude', runtime='host'))['sim'])
        with patch('orchestrator.launch_options.simulator_installed', return_value=True):
            self.assertTrue(normalize(options(engine='claude', runtime='container'))['sim'])
            self.assertFalse(normalize(options(engine='codex'))['sim'])

    def test_explore_live_and_chapter_list(self):
        argv = build(options(mode="explore", live=True, sim_assist=False, chapters=[2, 0, 1], study=False,
                             continuous_music=True, keep_obs_open=True, start_paused=True, cutscene_mode="agent"))
        self.assertIn("--no-sim-assist", argv)
        self.assertNotIn("--sim", argv)
        self.assertIn("--live", argv)
        self.assertIn("--no-hud-mod", argv)                 # 直播模式默认网页 HUD
        self.assertEqual(argv[argv.index("--chapters") + 1], "0,1,2")
        for flag in ("--no-study", "--continuous-music", "--keep-obs-open", "--start-paused"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--cutscene-mode") + 1], "agent")
        self.assertIn("--hud-mod", build(options(live=True, hud_mod=True)))

    def test_new_save_and_validation(self):
        argv = build(options(save={"name": "新档", "create": True}))
        self.assertIn("--create-save", argv)
        self.assertNotIn("--save-slot", argv)
        bad = [options(save={"name": "x", "create": True}, chapters=[1]), options(agent="bad label"),
               options(chapters=[]), options(chapters=[0, 0]), options(chapters=[99]), options(max_minutes=0),
               options(mode="race"), options(live="yes"), options(unknown=True), options(autocompact="12"),
               options(save={"name": "", "slot": None}), options(engine="other")]
        for case in bad:
            with self.assertRaises(ValueError, msg=case):
                build(case)

    def test_codex_uses_its_own_provider(self):
        self.assertEqual(normalize(options(engine="codex", provider="anthropic"))["provider"], "codex")

    def test_resume_args(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "runs" / "opus5.5" / "20261024-140048"
            run.mkdir(parents=True)
            (run / "run_state.json").write_text(json.dumps({"agent": "opus5.5", "save": "opus5.5", "engine": "claude"}),
                                                encoding="utf-8")
            with patch("orchestrator.launch_options.REPO_ROOT", root):
                argv = resume_args(run, fresh_session=True)
                self.assertEqual(argv[:6], ["--agent", "opus5.5", "--save", "opus5.5", "--resume", str(run.resolve())])
                self.assertIn("--fresh-session", argv)
                with self.assertRaises(ValueError):
                    resume_args(Path(folder))                  # 不在 runs 目录下


class ConsoleRunsTests(unittest.TestCase):
    def test_only_latest_frozen_run_is_resumable(self):
        from overlay import console_api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            older = root / "runs" / "opus5.5" / "20261024-140048"
            latest = root / "runs" / "opus5.5" / "20261024-180000"
            chat = root / "runs" / "gpt" / "chats"
            for directory, summary in ((older, {"game_released": False, "stop_reason": "中断"}),
                                       (latest, {"game_released": False, "stop_reason": "中断", "total_time_ticks": 5})):
                directory.mkdir(parents=True)
                (directory / "run_state.json").write_text(json.dumps({"agent": "opus5.5", "save": "opus5.5"}), encoding="utf-8")
                (directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            chat.mkdir(parents=True)
            (chat / "run_state.json").write_text("{}", encoding="utf-8")
            (root / "runs" / "latest.txt").write_text(str(latest), encoding="utf-8")
            with patch.object(console_api, "REPO_ROOT", root), \
                 patch.object(console_api, "game_status", return_value={"running": True, "active": True}):
                rows = console_api.runs()
                self.assertEqual([r["stamp"] for r in rows], ["20261024-180000", "20261024-140048"])
                self.assertEqual([r["resumable"] for r in rows], [True, False])
                # 停在地图或菜单上（不冻结、照常运行）但仍保持接管：也能续跑（续跑用菜单宏进入）
                with patch.object(console_api, "game_status", return_value={"running": True, "active": True, "frozen": False}):
                    self.assertEqual([r['resumable'] for r in console_api.runs()], [True, False])
                with patch.object(console_api, "game_status", return_value={"running": True, "active": False, "frozen": False}):
                    self.assertFalse(any(r['resumable'] for r in console_api.runs()))
                with patch.object(console_api, "orchestrator_status", return_value={"running": True}):
                    with self.assertRaisesRegex(ValueError, "已有主控"):
                        console_api.start(options())
                    with self.assertRaisesRegex(ValueError, "主控还在运行"):
                        console_api.release()
                with self.assertRaisesRegex(ValueError, "只能续跑"):
                    console_api.resume(str(older))
            with patch.object(console_api, "REPO_ROOT", root), \
                 patch.object(console_api, "game_status", return_value={"running": True, "active": False}):
                self.assertFalse(console_api.runs()[0]["resumable"])     # 已经交还控制：不能续跑

    def test_running_detection_from_fresh_metrics(self):
        from overlay import console_api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "runs" / "dev" / "20261024-190000"
            run.mkdir(parents=True)
            (root / "runs" / "latest.txt").write_text(str(run), encoding="utf-8")
            (run / "metrics.json").write_text(json.dumps({"written_wall": time.time()}), encoding="utf-8")
            with patch.object(console_api, "REPO_ROOT", root), patch.object(console_api, "_port_open", return_value=False), \
                 patch.object(console_api, "LAUNCH_RECORD", root / "none.json"), \
                 patch.object(console_api, "_mutex_exists", return_value=False):
                self.assertTrue(console_api.orchestrator_status()["running"])
                (run / "summary.json").write_text("{}", encoding="utf-8")
                self.assertFalse(console_api.orchestrator_status()["running"])


class LaunchFailureTests(unittest.TestCase):
    def test_interrupted_run_with_exit_code_1_is_not_a_failure(self):
        from overlay import console_api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            host = root / "host"
            host.mkdir()
            (host / "status.json").write_text(json.dumps({"state": "exited", "exit_code": 1}), encoding="utf-8")
            (host / "output.log").write_text("Traceback ...", encoding="utf-8")
            old = root / "runs" / "dev" / "20261024-170000"
            new = root / "runs" / "dev" / "20261024-180000"
            for d in (old, new):
                d.mkdir(parents=True)
            record = root / "launch.json"
            record.write_text(json.dumps({"host": str(host), "latest_before": str(old), "wall": time.time()}), encoding="utf-8")
            with patch.object(console_api, "REPO_ROOT", root), patch.object(console_api, "LAUNCH_RECORD", record), \
                 patch.object(console_api, "_port_open", return_value=False), \
                 patch.object(console_api, "_mutex_exists", return_value=False):
                (root / "runs" / "latest.txt").write_text(str(old), encoding="utf-8")
                status = console_api.orchestrator_status()
                self.assertTrue(status["failed"])                 # 没开出新的运行就退出了
                self.assertIn("Traceback", status["log_tail"])
                (root / "runs" / "latest.txt").write_text(str(new), encoding="utf-8")
                (new / "summary.json").write_text("{}", encoding="utf-8")
                self.assertFalse(console_api.orchestrator_status()["failed"])   # 中断：写了总结，退出码 1 正常


class RunCrashTests(unittest.TestCase):
    def test_latest_run_ended_by_controller_exception_is_reported_with_its_log(self):
        from overlay import console_api
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            run = root / "runs" / "dev" / "20261111-050000"
            run.mkdir(parents=True)
            (root / "runs" / "latest.txt").write_text(str(run), encoding="utf-8")
            summary = {"stop_reason": "主控异常：ConnectionError: Codex app-server 已断开",
                       "error": {"type": "ConnectionError", "message": "Codex app-server 已断开"}}
            (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            (run / "controller.log").write_text("2026-11-11 05:00:00.000 [codex] App Server 连接意外结束\n", encoding="utf-8")
            (run / "controller_error.txt").write_text("Traceback ...", encoding="utf-8")
            with patch.object(console_api, "REPO_ROOT", root), patch.object(console_api, "LAUNCH_RECORD", root / "none.json"), \
                 patch.object(console_api, "_port_open", return_value=False), \
                 patch.object(console_api, "_mutex_exists", return_value=False):
                crash = console_api.orchestrator_status()["crash"]
                self.assertEqual(crash["error"], "ConnectionError: Codex app-server 已断开")
                self.assertIsNone(crash["cause"])
                # 游戏进程先退出：提示里先写原因，再写主控的异常。
                exited = dict(summary, stop_reason="游戏进程已退出（退出码 0xC0000005，异常退出）", stop_cause="game_exit")
                (run / "summary.json").write_text(json.dumps(exited), encoding="utf-8")
                self.assertEqual(console_api.orchestrator_status()["crash"]["cause"],
                                 "游戏进程已退出（退出码 0xC0000005，异常退出）")
                (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                self.assertEqual(crash["error_file"], str(run / "controller_error.txt"))
                self.assertIn("App Server 连接意外结束", crash["log_tail"])
                summary.pop("error")      # A normal stop (中断、预算用完) is not a crash.
                (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
                self.assertIsNone(console_api.orchestrator_status()["crash"])


class FakeRunner(SessionControls):
    supports_interrupt = True

    def __init__(self, directory):
        self.run_dir = directory
        self.chat_only = False
        self.session_id = "s"
        self.cfg = SimpleNamespace(cutscene_mode="auto", allow_chapter_reset=False)
        self.metrics = SimpleNamespace(state=SimpleNamespace(tool_calls=0, effort="high", effort_pending=None),
                                       update=lambda **kw: None)
        self.session = SimpleNamespace(done=False, goals=SimpleNamespace(check=lambda values: False, snapshot=lambda: None),
                                       goal_values=lambda: {}, cutscene_tool=True)
        self.turn_active, self.turn_id = False, None
        self.interrupted = 0
        self.stop_reason = ""
        self._init_controls()

    async def _interrupt_user(self):
        self.interrupted += 1


class InterruptControlTests(unittest.TestCase):
    def test_interrupt_stops_run_and_blocks_game_actions(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner(Path(folder))
            control = RunControl(Path(folder))
            ident = control.enqueue("interrupt")

            async def run():
                task = asyncio.create_task(runner._controls())
                await asyncio.sleep(0.3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            asyncio.run(run())
            self.assertTrue(runner.stop_requested)
            self.assertTrue(runner.interrupted_for_resume)
            self.assertIn("可续跑", runner.stop_reason)
            self.assertEqual(runner.interrupted, 1)
            row = next(a for a in control.snapshot()["actions"] if a["id"] == ident)
            self.assertEqual(row["status"], "submitted")
            self.assertIn("中断", runner._game_guard())
            self.assertTrue(control.snapshot()["interrupt_control"])

    def test_runner_without_interrupt_support_reports_error(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = FakeRunner(Path(folder))
            runner.supports_interrupt = False
            control = RunControl(Path(folder))
            ident = control.enqueue("interrupt")

            async def run():
                task = asyncio.create_task(runner._controls())
                await asyncio.sleep(0.3)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            asyncio.run(run())
            self.assertFalse(runner.stop_requested)
            self.assertFalse(runner.interrupted_for_resume)
            row = next(a for a in control.snapshot()["actions"] if a["id"] == ident)
            self.assertEqual(row["status"], "error")


class BackgroundPythonTests(unittest.TestCase):
    def test_background_hosts_use_console_python(self):
        # 从 pythonw（控制台的启动器）里启动的后台宿主与服务要用 python.exe：pythonw 的子进程（tasklist、docker 等）会弹黑色窗口
        from orchestrator import background
        with tempfile.TemporaryDirectory() as folder:
            scripts = Path(folder)
            (scripts / "python.exe").write_bytes(b"")
            (scripts / "pythonw.exe").write_bytes(b"")
            with patch.object(background.sys, "executable", str(scripts / "pythonw.exe")):
                self.assertEqual(background.console_python(), str(scripts / "python.exe"))
            with patch.object(background.sys, "executable", str(scripts / "python.exe")):
                self.assertEqual(background.console_python(), str(scripts / "python.exe"))


if __name__ == "__main__":
    unittest.main()
