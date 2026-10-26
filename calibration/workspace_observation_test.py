"""End-to-end file-first observation consumers; only the external game bridge is fake."""
import asyncio
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calibration.collision_contract_test import FakeBridge, observation
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server, FILE_JSON_HEADER
from orchestrator.workspace_files.collision_matrix import Matrix


@contextmanager
def game_session(engine='codex', runtime='host'):
    with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
        root = Path(folder)
        run = root / 'run'
        run.mkdir()
        cfg = RunConfig(agent='file-observation', save='test', model='stub', engine=engine,
                        runtime=runtime, sim=False, chapter=3, cutscene_mode='agent')
        cfg.min_frames_per_act = 1
        session = GameSession(cfg, run, Metrics(run / 'metrics.json', 'isolated', 'stub', 'high', 60))
        session.client = FakeBridge()
        try:
            yield session, build_server(session)
        finally:
            session.close(False)


def call(server, name='observe', args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    if result.is_error:
        raise AssertionError(str(result))
    return '\n'.join(block.text for block in result.content if hasattr(block, 'text'))


def arguments(name):
    if name == 'act':
        return {'inputs': ['1,R'], 'thought': '验证碰撞文件交接'}
    if name == 'cutscene':
        return {'action': 'advance'}
    return {}


def prepare_story(session, name):
    if name == 'cutscene':
        session.client.obs['storyInput'] = 'dialogue'
        session.client.obs['level'].update(inCutscene=True, dialogOpen=True, dialogWaiting=True, dialogChoices=[])


class WorkspaceObservationTests(unittest.TestCase):
    def assert_file_result(self, text, session):
        self.assertIn(FILE_JSON_HEADER, text)
        metadata = json.JSONDecoder().raw_decode(text.split(FILE_JSON_HEADER, 1)[1])[0]
        self.assertEqual(metadata['encoding'], 'workspace-files-v1')
        self.assertEqual(metadata['frame'], session.last_obs['frame'])
        self.assertEqual(metadata['sid'], session.last_obs['level']['sid'])
        self.assertEqual(metadata['room'], session.last_obs['level']['room'])
        self.assertTrue(metadata['snapshot_id'])
        self.assertEqual(metadata['files']['observation'], '/workspace/game/latest_obs.json')
        for field in ('objects', 'groups', 'instances', 'solids'):
            self.assertNotIn('"' + field + '":', text)
        path = session.workspace / 'game/latest_obs.json'
        published = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(published['obs'], session.last_obs)
        matrix = Matrix.from_file(path)
        self.assertEqual(matrix.cell, 8)
        self.assertEqual(matrix.identity['frame'], metadata['frame'])
        self.assertEqual(matrix.sources[0]['geometry'], session.last_obs['collision']['objects'][0]['geometry'])
        return metadata

    def assert_fallback(self, text):
        self.assertNotIn(FILE_JSON_HEADER, text)
        self.assertIn('room-context-v2', text)
        self.assertTrue('"groups"' in text or '"changed"' in text)

    def test_default_observe_act_cutscene_publish_files_consumed_by_matrix(self):
        with game_session() as (session, server):
            for name in ('observe', 'act', 'cutscene'):
                with self.subTest(tool=name):
                    prepare_story(session, name)
                    metadata = self.assert_file_result(call(server, name, arguments(name)), session)
                    self.assertEqual(metadata['collision']['object_count'], len(session.last_obs['collision']['objects']))
                    self.assertFalse((session.workspace / 'game/collision_matrix.json').exists())

    def test_actual_runtime_capabilities_select_file_first_or_fallback(self):
        for engine, runtime in (('codex', 'host'), ('codex', 'container'),
                                ('claude', 'container'), ('claude', 'host')):
            with self.subTest(engine=engine, runtime=runtime), game_session(engine, runtime) as (session, server):
                text = call(server)
                if engine == 'claude' and runtime == 'host':
                    self.assertIsNone(session.workspace)
                    self.assert_fallback(text)
                else:
                    self.assert_file_result(text, session)

    def test_explicit_geometry_and_player_queries_stay_cached_in_file_first_mode(self):
        with game_session() as (session, server):
            self.assert_file_result(call(server), session)
            frame = session.last_obs['frame']
            with patch.object(session.client, 'call', side_effect=AssertionError('detail query advanced game')):
                geometry = json.loads(call(server, 'observe', {'detail': 'geometry', 'limit': 1}))
                player = json.loads(call(server, 'observe', {'detail': 'player'}))
            self.assertEqual(geometry['encoding'], 'shared-shapes-v1')
            self.assertEqual(geometry['frame'], frame)
            self.assertEqual(geometry['returned'], 1)
            self.assertEqual(player['frame'], frame)
            self.assertEqual(player['player'], session.last_obs['player'])
            self.assertEqual(session.last_obs['frame'], frame)

    def test_file_snapshot_id_can_be_used_for_cached_geometry_pagination(self):
        with game_session() as (session, server):
            metadata = self.assert_file_result(call(server), session)
            with patch.object(session.client, 'call', side_effect=AssertionError('detail query advanced game')):
                page = json.loads(call(server, 'observe', {'detail': 'geometry', 'offset': 1,
                    'limit': 1, 'snapshot_id': metadata['snapshot_id']}))
            self.assertEqual(page['snapshot_id'], metadata['snapshot_id'])
            self.assertEqual(page['returned'], 1)

    def test_return_from_file_mode_replaces_prior_inline_baseline(self):
        with game_session() as (session, server):
            original = session._write_json
            def fail_observation(rel, value):
                if rel == 'game/latest_obs.json':
                    raise PermissionError('observation file locked')
                original(rel, value)
            with patch.object(session, '_write_json', fail_observation):
                self.assert_fallback(call(server))
            self.assert_file_result(call(server, 'act', arguments('act')), session)
            with patch.object(session, '_write_json', fail_observation):
                text = call(server, 'act', arguments('act'))
            self.assert_fallback(text)
            self.assertIn('"mode":"baseline"', text)
            self.assertNotIn('"base_snapshot_id":', text)

    def test_missing_file_falls_back_then_real_publication_recovers(self):
        with game_session() as (session, server):
            self.assert_file_result(call(server), session)
            path = session.workspace / 'game/latest_obs.json'
            path.unlink()
            original = session._write_json
            def omit_observation(rel, value):
                if rel != 'game/latest_obs.json':
                    original(rel, value)
            with patch.object(session, '_write_json', omit_observation):
                self.assert_fallback(call(server))
            self.assertFalse(path.exists())
            self.assert_file_result(call(server), session)

    def test_oversized_and_deeply_nested_file_fall_back_without_losing_action_result(self):
        for kind in ('oversized', 'nested'):
            with self.subTest(kind=kind), game_session() as (session, server):
                self.assert_file_result(call(server), session)
                path = session.workspace / 'game/latest_obs.json'
                if kind == 'oversized':
                    raw = json.dumps({'obs': session.last_obs}).encode('utf-8') + b' ' * (16 * 1024 * 1024)
                else:
                    raw = b'[' * 2000 + b'0' + b']' * 2000
                path.write_bytes(raw)
                original = session._write_json
                def omit_observation(rel, value):
                    if rel != 'game/latest_obs.json':
                        original(rel, value)
                frame = session.last_obs['frame']
                with patch.object(session, '_write_json', omit_observation):
                    text = call(server, 'act', arguments('act'))
                self.assert_fallback(text)
                self.assertGreater(session.last_obs['frame'], frame)
                self.assertIn('"mode":"baseline"', text)

    def test_later_collision_image_failure_does_not_invalidate_published_observation(self):
        with game_session() as (session, server):
            with patch.object(session, '_write_collision_view', side_effect=OSError('fixture png publication failed')):
                self.assert_file_result(call(server), session)

    def test_malformed_file_is_not_advertised_as_usable(self):
        with game_session() as (session, server):
            self.assert_file_result(call(server), session)
            path = session.workspace / 'game/latest_obs.json'
            path.write_bytes(b'{"obs":')
            original = session._write_json
            def omit_observation(rel, value):
                if rel != 'game/latest_obs.json':
                    original(rel, value)
            with patch.object(session, '_write_json', omit_observation):
                self.assert_fallback(call(server))
            self.assert_file_result(call(server), session)

    def test_failed_publish_on_all_three_entries_falls_back_without_fabricated_reference(self):
        for name in ('observe', 'act', 'cutscene'):
            with self.subTest(tool=name), game_session() as (session, server):
                self.assert_file_result(call(server), session)
                original = session._write_json
                def fail_observation(rel, value):
                    if rel == 'game/latest_obs.json':
                        raise PermissionError('observation file locked')
                    original(rel, value)
                session.client.obs = observation(20, x=4)
                prepare_story(session, name)
                with patch.object(session, '_write_json', fail_observation):
                    self.assert_fallback(call(server, name, arguments(name)))
                self.assertNotEqual(json.loads((session.workspace / 'game/latest_obs.json').read_text())['obs']['frame'],
                                    session.last_obs['frame'])
                self.assert_file_result(call(server), session)

    def test_planning_gate_keeps_geometry_inline_even_when_files_exist(self):
        from orchestrator.pausegate import PauseGate
        with game_session() as (session, server):
            self.assert_file_result(call(server), session)
            gate = PauseGate(session.metrics, enabled=True, sim_only=True,
                             state_path=session.workspace / 'game/rounds.json', log=lambda message: None)
            restricted = build_server(session, gate=gate)
            self.assert_fallback(call(restricted))
            gate.bank = 0
            gate._publish()
            self.assert_fallback(call(restricted, 'act', arguments('act')))
            gate.enabled = False
            self.assert_file_result(call(restricted), session)

    def test_file_context_omits_collectible_listing_but_file_keeps_it(self):
        with game_session() as (session, server):
            session.client.obs['entities'].append({'id': 99123, 'type': 'Strawberry',
                'cat': 'collectible', 'x': 7.125, 'y': 8.375, 'w': 12, 'h': 12})
            text = call(server)
            self.assert_file_result(text, session)
            self.assertNotIn('Strawberry#99123', text)
            self.assertNotIn('7.125', text)
            published = json.loads((session.workspace / 'game/latest_obs.json').read_text(encoding='utf-8'))
            self.assertEqual(published['obs']['entities'][-1]['id'], 99123)

    def test_same_frame_same_room_but_different_file_contents_are_not_trusted(self):
        with game_session() as (session, server):
            original = session._write_json
            def stale_collision(rel, value):
                if rel == 'game/latest_obs.json':
                    value = copy.deepcopy(value)
                    value['obs']['collision']['objects'][0]['geometry']['x'] = 999
                original(rel, value)
            with patch.object(session, '_write_json', stale_collision):
                self.assert_fallback(call(server))
            self.assert_file_result(call(server), session)

    def test_existing_files_do_not_enable_file_first_when_session_has_no_workspace(self):
        with game_session() as (session, server):
            self.assert_file_result(call(server), session)
            old_workspace = session.workspace
            session.workspace = None
            self.assertTrue((old_workspace / 'game/latest_obs.json').is_file())
            self.assert_fallback(call(server))

    def test_existing_files_do_not_enable_programs_for_host_claude(self):
        with game_session() as (session, server):
            self.assert_file_result(call(server), session)
            session.cfg.engine = 'claude'
            session.cfg.runtime = 'host'
            self.assert_fallback(call(server))


if __name__ == '__main__':
    unittest.main()
