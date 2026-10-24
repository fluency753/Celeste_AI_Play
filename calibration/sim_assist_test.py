"""Simulation allowance contracts; no game connection, credentials or model calls."""
from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.sim_assist import SimEnergy, moving_frames, run_predictions, validate_request, write_replay


class SimEnergyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = 1000
        self.wallet = SimEnergy(self.root / "energy.json", lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def test_billing_counts_all_submitted_frames_and_candidates(self):
        self.assertEqual(validate_request({"candidates": [["60,R"]]})[1], 13)
        self.assertEqual(validate_request({"candidates": [["120,R"]] * 3})[1], 46)
        self.assertEqual(validate_request({"candidates": [["10,r+j"]]})[0], [["10,R,J"]])

    def test_oversized_or_unsafe_requests_rejected(self):
        for value in ({"candidates": [["181,R"]]}, {"candidates": [["100,R", "100,L"]]},
                      {"candidates": [["180,R"]] * 4}, {"candidates": [["1"]] * 7},
                      {"candidates": [["1,A:90"]]}, {"candidates": [["1,P"]]},
                      {"candidates": [["-1,R"]]}, {"candidates": [["1,R"]], "state": "/arbitrary"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_request(value)

    def test_restart_keeps_spent_energy_and_refund_is_idempotent(self):
        ident = self.wallet.reserve(13)
        self.wallet = SimEnergy(self.root / "energy.json")
        self.assertEqual(self.wallet.snapshot()["balance"], 27)
        self.wallet.finish(ident, refund=True)
        self.wallet.finish(ident, refund=True)
        self.assertEqual(self.wallet.snapshot()["balance"], 40)

    def test_time_alone_does_not_regenerate_and_movement_is_rate_limited(self):
        self.now += 3600
        self.wallet.reward()
        self.assertEqual(self.wallet.snapshot()["balance"], 40)
        self.wallet.reward(moving_frames=6000)
        self.assertEqual(self.wallet.snapshot()["balance"], 60)
        self.wallet.reward(moving_frames=6000)
        self.assertEqual(self.wallet.snapshot()["balance"], 60)
        self.now += 61
        self.wallet.reward()
        self.assertEqual(self.wallet.snapshot()["balance"], 60)  # no banked excess movement
        self.wallet.reward(moving_frames=60)
        self.assertEqual(self.wallet.snapshot()["balance"], 61)

    def test_room_rewards_do_not_repeat_on_backtracking_or_restart(self):
        self.wallet.reward(room="map:A:room0")
        self.wallet.reward(room="map:A:room1")
        self.assertEqual(self.wallet.snapshot()["balance"], 55)
        self.wallet = SimEnergy(self.root / "energy.json")
        self.wallet.reward(room="map:A:room0")
        self.wallet.reward(room="map:A:room1")
        self.assertEqual(self.wallet.snapshot()["balance"], 55)
        self.wallet.reward(room="map:B:room1")
        self.assertEqual(self.wallet.snapshot()["balance"], 70)

    def test_parallel_requests_cannot_overdraw(self):
        def spend(_):
            try:
                return self.wallet.reserve(13)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(spend, range(8)))
        self.assertEqual(sum(v is not None for v in accepted), 3)
        self.assertEqual(self.wallet.snapshot()["balance"], 1)

    def test_refills_never_exceed_capacity(self):
        for i in range(20):
            self.wallet.reward(room=f"map:A:{i}")
        self.assertEqual(self.wallet.snapshot()["balance"], 100)

    def test_only_real_movement_counts_not_idle_death_or_cutscene_motion(self):
        rows = [[i, x, 0, 0, 0, state, 1, 0] for i, (x, state) in enumerate([
            (1, "Normal"), (1, "Normal"), (2, "Normal"), (3, "Dead"), (4, "Dummy"), (5, "IntroRespawn")])]
        self.assertEqual(moving_frames(rows, [0, 0]), 2)
        self.assertEqual(moving_frames([], [0, 0]), 0)

    def test_worker_only_gets_private_state_and_has_no_network(self):
        replies = '\n'.join(json.dumps(r) for r in [
            {"ok": True, "info": {"frame": 1}}, {"ok": True, "results": [{"ok": True, "frames": []}]}])
        response = SimpleNamespace(returncode=0, stdout=replies, stderr="")
        cfg = RunConfig(agent="test", save="test", model="test")
        with patch("orchestrator.sim_assist.find_docker_exe", return_value="docker"), \
             patch("orchestrator.sim_assist.subprocess.run", return_value=response) as run:
            run_predictions(cfg, self.root, [["60,R"]])
        args = run.call_args_list[0].args[0]
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertFalse(any("/workspace" in a or "openai" in a for a in args))
        request = run.call_args_list[0].kwargs["input"]
        self.assertIn('"stop_on_death": true', request)
        self.assertIn('"stop_on_room": true', request)
        self.assertEqual(run.call_args_list[-1].args[0][1:3], ["rm", "-f"])

    def test_predictions_feed_existing_playback_log(self):
        from orchestrator.playback import read_logs
        log_dir = self.root / "game/simlog"
        log_dir.mkdir(parents=True)
        write_replay(self.root, [{"stop": "death", "frames": [[11, 2, 3], [12, 4, 5]]}], 10)
        logs = list(log_dir.glob("*.bin"))
        self.assertEqual(len(logs), 1)
        index = read_logs(logs)
        self.assertEqual(len(index), 1)
        self.assertEqual(int(index.start[0]), 10)
        self.assertEqual(int(index.stop[0]), 1)


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        cfg = RunConfig(agent="test", save="test", model="test", runtime="host", sim_assist=True)
        metrics = Metrics(root / "metrics.json", "test", "test", "high", 100)
        self.session = GameSession(cfg, root, metrics)
        self.calls = []
        self.session.client = SimpleNamespace(call=lambda *a, **kw: self.calls.append(a) or {"state": {"frame": 1}})

    def tearDown(self):
        self.session._frames_log.close()
        self.session._decisions_log.close()
        self.temp.cleanup()

    def test_bad_request_does_not_even_capture_state(self):
        with self.assertRaises(ValueError):
            self.session.simulate({"candidates": [["6000,R"]]})
        self.assertEqual(self.calls, [])

    def test_infrastructure_failure_refunds_but_prediction_death_costs(self):
        with patch("orchestrator.sim_assist.run_predictions", side_effect=RuntimeError("worker down")):
            with self.assertRaises(RuntimeError):
                self.session.simulate({"candidates": [["60,R"]]})
        self.assertEqual(self.session.sim_energy.snapshot()["balance"], 40)
        prediction = {"info": {"frame": 1}, "results": [{"ok": True, "stop": "death", "frames": []}]}
        with patch("orchestrator.sim_assist.run_predictions", return_value=prediction):
            result = self.session.simulate({"candidates": [["60,R"]]})
        self.assertEqual(result["cost"], 13)
        self.assertEqual(result["energy"]["balance"], 27)

    def test_insufficient_energy_never_runs_worker(self):
        self.session.sim_energy.reserve(40)
        with patch("orchestrator.sim_assist.run_predictions") as worker:
            with self.assertRaises(ValueError):
                self.session.simulate({"candidates": [["60,R"]]})
        worker.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_capture_failure_refunds(self):
        self.session.client.call = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("bridge down"))
        with self.assertRaises(RuntimeError):
            self.session.simulate({"candidates": [["60,R"]]})
        self.assertEqual(self.session.sim_energy.snapshot()["balance"], 40)


class NativeEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_metered_command_never_reaches_agent_shell_and_pause_blocks_it(self):
        from orchestrator.codex_agent import CodexRunner
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent="test", save="test", model="test", sim_assist=True)
            metrics = Metrics(root / "metrics.json", "test", "test", "high", 100)
            energy = SimEnergy(root / "energy.json")
            session = SimpleNamespace(sim_energy=energy, simulate=lambda data: {"cost": 13, "energy": energy.snapshot()})
            runner = CodexRunner(cfg, root, session, metrics, "container")
            with patch("orchestrator.codex_agent.run_program") as shell:
                result = await runner._request("item/tool/call", {"tool": "celeste_bash", "arguments": {
                    "command": 'simulate {"candidates":[["60,R"]]}', "description": "试一条短路线"}})
                self.assertTrue(result["success"])
                self.assertEqual(json.loads(result["contentItems"][0]["text"])["cost"], 13)
                runner.paused = True
                result = await runner._request("item/tool/call", {"tool": "celeste_bash", "arguments": {
                    "command": 'simulate {"candidates":[["60,R"]]}', "description": "暂停后不应预测"}})
                self.assertFalse(result["success"])
                shell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
