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
        self.assertEqual(validate_request({"candidates": [["60,R"]]})[1], 3)
        self.assertEqual(validate_request({"candidates": [["120,R"]] * 3})[1], 12)
        self.assertEqual(validate_request({"candidates": [["10,r+j"]]})[0], [["10,R,J"]])

    def test_oversized_or_unsafe_requests_rejected(self):
        for value in ({"candidates": [["361,R"]]}, {"candidates": [["200,R", "200,L"]]},
                      {"candidates": [["180,R"]] * 4}, {"candidates": [["1"]] * 7},
                      {"candidates": [["1,A:90"]]}, {"candidates": [["1,P"]]},
                      {"candidates": [["-1,R"]]}, {"candidates": [["1,R"]], "state": "/arbitrary"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_request(value)

    def test_longer_predictions_and_comparisons_fit_initial_allowance(self):
        self.assertEqual(validate_request({'candidates': [['360,R']]})[1], 8)
        self.assertEqual(validate_request({'candidates': [['120,R'], ['120,L']]})[1], 8)
        cost = validate_request({'candidates': [['180,R']] * 3})[1]
        self.assertEqual(cost, 15)
        for _ in range(4):
            self.wallet.reserve(cost)
        self.assertEqual(self.wallet.snapshot()['balance'], 0)
        with self.assertRaises(ValueError):
            self.wallet.reserve(cost)

    def test_restart_keeps_spent_energy_and_refund_is_idempotent(self):
        ident = self.wallet.reserve(13)
        self.wallet = SimEnergy(self.root / "energy.json")
        self.assertEqual(self.wallet.snapshot()["balance"], 47)
        self.wallet.finish(ident, refund=True)
        self.wallet.finish(ident, refund=True)
        self.assertEqual(self.wallet.snapshot()["balance"], 60)

    def test_time_alone_does_not_regenerate_and_movement_is_rate_limited(self):
        self.now += 3600
        self.wallet.reward()
        self.assertEqual(self.wallet.snapshot()["balance"], 60)
        self.wallet.reward(moving_frames=6000)
        self.assertEqual(self.wallet.snapshot()["balance"], 80)
        self.wallet.reward(moving_frames=6000)
        self.assertEqual(self.wallet.snapshot()["balance"], 80)
        self.now += 61
        self.wallet.reward()
        self.assertEqual(self.wallet.snapshot()["balance"], 80)  # no banked excess movement
        self.wallet.reward(moving_frames=60)
        self.assertEqual(self.wallet.snapshot()["balance"], 81)

    def test_room_rewards_do_not_repeat_on_backtracking_or_restart(self):
        self.wallet.reward(room="map:A:room0")
        self.wallet.reward(room="map:A:room1")
        self.assertEqual(self.wallet.snapshot()["balance"], 75)
        self.wallet = SimEnergy(self.root / "energy.json")
        self.wallet.reward(room="map:A:room0")
        self.wallet.reward(room="map:A:room1")
        self.assertEqual(self.wallet.snapshot()["balance"], 75)
        self.wallet.reward(room="map:B:room1")
        self.assertEqual(self.wallet.snapshot()["balance"], 90)

    def test_parallel_requests_cannot_overdraw(self):
        def spend(_):
            try:
                return self.wallet.reserve(13)
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(spend, range(8)))
        self.assertEqual(sum(v is not None for v in accepted), 4)
        self.assertEqual(self.wallet.snapshot()["balance"], 8)

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

    def test_partial_replay_failure_cannot_be_recounted_as_legacy(self):
        with self.assertRaises(ValueError):
            write_replay(self.root, [{"frames": [[1, 2, 3]]}, {"frames": [[2, "bad", 3]]}], 1)
        self.assertEqual(list((self.root / "game/simlog").glob("*.bin")), [])


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

    def test_tas_conversion_precedes_billing_and_cannot_silently_drop_menu_actions(self):
        before=self.session.sim_energy.snapshot()
        for inputs in (['1,S'],['1,Q'],['1,H'],['9999,J']):
            with self.assertRaises(ValueError):self.session.simulate({'input_format':'celeste_tas','candidates':[inputs]})
        self.assertEqual(self.calls,[])
        self.assertEqual(self.session.sim_energy.snapshot(),before)
        prediction={'info':{'frame':1},'results':[{'stop':'done','frames':[]} ]}
        with patch('orchestrator.sim_assist.run_predictions',return_value=prediction) as run:
            result=self.session.simulate({'input_format':'celeste_tas','candidates':[['1,J','1,K','1,C']]})
        self.assertEqual(run.call_args.args[2],[['1,J','1,K','1,Y']])
        self.assertEqual(result['requested_input_format'],'celeste_tas')
        self.assertEqual(result['input_format'],'celeste_ai')

    def test_mcp_predictions_count_once_and_replay_without_next_action(self):
        import asyncio
        from orchestrator.playback import Playback
        from orchestrator.server import build_server
        for enabled in (True, False):
            with self.subTest(playback=enabled):
                session = self.session
                session.cfg.playback = enabled
                session.workspace = session.run_dir / str(enabled)
                session.metrics.update(sim_calls=0, sim_rollouts=0)
                session.sim_energy = SimEnergy(session.run_dir / (str(enabled) + "-energy.json"))
                playback = Playback(session.cfg, session, session.metrics, None, log=lambda _: None)
                session.playback = playback
                prediction = {"info": {"frame": 1}, "results": [
                    {"stop": "done", "frames": [[2, 2, 3], [3, 4, 5]]},
                    {"stop": "death", "frames": [[2, 2, 3], [3, 5, 5]]}]}
                server = build_server(session)
                try:
                    with patch("orchestrator.sim_assist.run_predictions", return_value=prediction), \
                         patch.object(playback, "_call", return_value={}) as replay:
                        asyncio.run(server.call_tool("simulate", {"candidates": [["20,R"], ["20,L"]]}))
                        # drain waits for the collector; no act or Bash result needed to submit it.
                        playback.drain()
                        self.assertEqual(session.metrics.state.sim_calls, 1)
                        self.assertEqual(session.metrics.state.sim_rollouts, 2)
                        self.assertEqual(replay.call_count, 1 if enabled else 0)
                        self.assertEqual(session.metrics.state.sim_round_rollouts, 0)
                        asyncio.run(server.call_tool("simulate", {}))
                        playback.drain()
                        self.assertEqual(session.metrics.state.sim_calls, 1)
                        self.assertEqual(session.metrics.state.sim_rollouts, 2)
                finally:
                    playback.close()
                    session.playback = None

    def test_visualization_failure_keeps_success_count_and_spent_energy(self):
        self.session.workspace = self.session.run_dir / "workspace"
        self.session.playback = SimpleNamespace(collect_prediction=lambda *a: (_ for _ in ()).throw(OSError("disk")))
        prediction = {"info": {"frame": 1}, "results": [{"stop": "done", "frames": []}]}
        with patch("orchestrator.sim_assist.run_predictions", return_value=prediction):
            result = self.session.simulate({"candidates": [["60,R"]]})
        self.assertIn("replay_warning", result)
        self.assertEqual(self.session.metrics.state.sim_calls, 1)
        self.assertEqual(self.session.metrics.state.sim_rollouts, 1)
        self.assertEqual(result["energy"]["balance"], 57)

    def test_legacy_logs_are_counted_with_playback_disabled(self):
        from orchestrator.playback import Playback
        session = self.session
        session.cfg.playback = False
        session.workspace = session.run_dir / "workspace"
        playback = Playback(session.cfg, session, session.metrics, None, log=lambda _: None)
        try:
            write_replay(session.workspace, [{"stop": "done", "frames": [[1, 2, 3], [2, 3, 3]]}], 0)
            playback.drain()
            playback.drain()
            self.assertEqual(session.metrics.state.sim_calls, 1)
            self.assertEqual(session.metrics.state.sim_rollouts, 1)
            self.assertEqual(playback.rounds, [])
        finally:
            playback.close()

    def test_infrastructure_failure_refunds_but_prediction_death_costs(self):
        with patch("orchestrator.sim_assist.run_predictions", side_effect=RuntimeError("worker down")):
            with self.assertRaises(RuntimeError):
                self.session.simulate({"candidates": [["60,R"]]})
        self.assertEqual(self.session.sim_energy.snapshot()["balance"], 60)
        prediction = {"info": {"frame": 1}, "results": [{"ok": True, "stop": "death", "frames": []}]}
        with patch("orchestrator.sim_assist.run_predictions", return_value=prediction):
            result = self.session.simulate({"candidates": [["60,R"]]})
        self.assertEqual(result["cost"], 3)
        self.assertEqual(result["energy"]["balance"], 57)

    def test_insufficient_energy_never_runs_worker(self):
        self.session.sim_energy.reserve(60)
        with patch("orchestrator.sim_assist.run_predictions") as worker:
            with self.assertRaises(ValueError):
                self.session.simulate({"candidates": [["60,R"]]})
        worker.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_capture_failure_refunds(self):
        self.session.client.call = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("bridge down"))
        with self.assertRaises(RuntimeError):
            self.session.simulate({"candidates": [["60,R"]]})
        self.assertEqual(self.session.sim_energy.snapshot()["balance"], 60)


class NativeEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_metered_tool_never_reaches_agent_shell_and_pause_blocks_it(self):
        from orchestrator.codex_agent import CodexRunner
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = RunConfig(agent="test", save="test", model="test", sim_assist=True)
            metrics = Metrics(root / "metrics.json", "test", "test", "high", 100)
            energy = SimEnergy(root / "energy.json")
            session = SimpleNamespace(cfg=cfg, metrics=metrics, workspace=None, done=False, sim_energy=energy, simulate=lambda data: {"cost": 13, "energy": energy.snapshot()})
            from orchestrator.server import build_server
            runner = CodexRunner(cfg, root, session, metrics, "container", game_server=build_server(session))
            runner.turn_id, runner.turn_active = 'test-turn', True
            with patch("orchestrator.sandbox.run_program") as shell:
                result = await runner._request("item/tool/call", {"tool": "celeste_simulate", "arguments": {"candidates": [["60,R"]]}})
                self.assertTrue(result["success"])
                self.assertEqual(json.loads(result["contentItems"][0]["text"])["cost"], 13)
                runner.paused = True
                result = await runner._request("item/tool/call", {"tool": "celeste_simulate", "arguments": {"candidates": [["60,R"]]}})
                self.assertFalse(result["success"])
                shell.assert_not_called()


if __name__ == "__main__":
    unittest.main()
