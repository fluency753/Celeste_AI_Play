"""Presentation compression preserves source geometry and cached query semantics."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibration.collision_contract_test import observation, FakeBridge
from orchestrator.geometry_context import geometry_overview, geometry_page
from orchestrator.observation import room_json, enrich_obs, describe_obs
from orchestrator.config import RunConfig
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server, ROOM_JSON_HEADER


def dense(count=200):
    obs = observation()
    for i in range(count):
        x, y = (i % 20)*12, (i // 20)*12
        obs['collision']['objects'].append({
            'id': i+1000, 'source': 'entity', 'type': 'DustStaticSpinner',
            'kind': 'hazard', 'collidable': True, 'mutable': True,
            'geometry': {'shape': 'list', 'x': x, 'y': y, 'w': 12, 'h': 12, 'parts': [
                {'shape': 'circle', 'x': x, 'y': y, 'w': 12, 'h': 12, 'r': 6},
                {'shape': 'rect', 'x': x+2, 'y': y+3, 'w': 8, 'h': 6}]}})
    return obs


class GeometryContextTests(unittest.TestCase):
    def test_dense_overview_counts_shapes_and_original_unchanged(self):
        obs = dense()
        original = copy.deepcopy(obs)
        result = geometry_overview(obs)
        self.assertFalse(result['detailsComplete'])
        self.assertEqual(result['hazards']['detailsOmitted'], 200)
        self.assertEqual(sum(g['count'] for g in result['hazards']['groups']), 200)
        self.assertEqual(len(result['objects']), 2)
        self.assertEqual(len(result['shapes']), 3)
        self.assertLess(len(json.dumps(result)), len(json.dumps(obs['collision']))/4)
        self.assertEqual(obs, original)
        self.assertEqual(json.loads(room_json(obs))['collision'], obs['collision'])

    def test_exact_relative_shapes_state_and_small_set(self):
        obs = dense(2)
        obs['entities'].append({'id': 1000, 'fields': {'speed': [10, 0]}, 'collected': True})
        page = geometry_page(obs)
        a, b = page['objects'][-2:]
        self.assertEqual(a['shape'], b['shape'])
        self.assertEqual(b['at'], [12, 0])
        shape = page['shapes'][b['shape']]
        self.assertEqual(shape['parts'][1]['x'], 2)
        self.assertEqual(a['state']['speed'], [10, 0])
        self.assertTrue(a['attributes']['collected'])
        self.assertTrue(geometry_overview(obs)['detailsComplete'])

    def test_unknown_and_nonhazards_remain_explicit_state_groups_separate(self):
        obs = dense()
        obs['collision']['objects'][-1]['collidable'] = False
        unknown = copy.deepcopy(obs['collision']['objects'][-2])
        unknown.update(id='unknown', geometry={'shape': 'unknown', 'x': 1, 'y': 1, 'w': 2, 'h': 2})
        obs['collision']['objects'].append(unknown)
        result = geometry_overview(obs)
        self.assertIn('unknown', [o['id'] for o in result['objects']])
        self.assertEqual({g['collidable'] for g in result['hazards']['groups']}, {True})
        self.assertEqual(result['hazards']['detailsReturned'], 2)

    def test_dense_hazards_preserve_unique_boss_and_direction_state(self):
        obs = dense(13)
        obs['collision']['objects'][-1]['type'] = 'AngryOshiro'
        obs['entities'].append({'id': 1012, 'fields': {'attackTimer': .3}, 'dir': 'left'})
        for i in range(6):
            obs['entities'].append({'id': 1000+i, 'dir': 'up'})
        result = geometry_overview(obs)
        boss = next(o for o in result['objects'] if o['type'] == 'AngryOshiro')
        self.assertEqual(boss['state']['attackTimer'], .3)
        self.assertEqual(boss['attributes']['dir'], 'left')
        directed = [g for g in result['hazards']['groups'] if g.get('attributes', {}).get('dir') == 'up']
        self.assertEqual(sum(g['count'] for g in directed), 6)

    def test_different_per_instance_states_are_not_discarded_or_prevent_grouping(self):
        obs = dense(14)
        obs['entities'] = [{'id': 1000+i, 'fields': {'offset': i*.01}, 'moved': [i, 0]} for i in range(14)]
        result = geometry_overview(obs)
        self.assertEqual(result['hazards']['detailsOmitted'], 14)
        instances = [s for g in result['hazards']['groups'] for s in g['instanceStates']]
        self.assertEqual(len(instances), 14)
        self.assertEqual(next(s for s in instances if s['id'] == 1003)['moved'], [3, 0])

    def test_region_uses_real_parts_circle_and_sparse_holes(self):
        obs = observation()
        obs['collision']['objects'] = [{'id': 1, 'geometry': {
            'shape': 'grid', 'x': 0, 'y': 0, 'w': 30, 'h': 30, 'parts': [
                {'shape': 'circle', 'x': 0, 'y': 0, 'w': 10, 'h': 10, 'r': 5},
                {'shape': 'rect', 'x': 20, 'y': 20, 'w': 10, 'h': 10}]}}]
        self.assertEqual(geometry_page(obs, region=[0, 0, .1, .1])['returned'], 0)
        self.assertEqual(geometry_page(obs, region=[12, 12, 2, 2])['returned'], 0)
        page = geometry_page(obs, region=[24, 24, 1, 1])
        self.assertEqual(page['returned'], 1)
        self.assertEqual(len(page['shapes']['s0']['parts']), 2)
        self.assertFalse(geometry_page(obs, region=[-200, 0, 5, 5])['regionWithinSourceBounds'])

    def test_pagination_identity_validation_partial_and_missing(self):
        obs = dense()
        page = geometry_page(obs, limit=2)
        self.assertEqual(page['next_offset'], 2)
        more = geometry_page(obs, limit=2, offset=2, expected_snapshot=page['snapshot_id'])
        self.assertEqual(more['objects'][0]['id'], 1000)
        with self.assertRaises(ValueError): geometry_page(obs, offset=2)
        obs['collision']['objects'][0]['collidable'] = False
        with self.assertRaises(ValueError): geometry_page(obs, expected_snapshot=page['snapshot_id'])
        for region in ([0, 0, 0, 2], [True, 0, 1, 1], [0, 0, float('inf'), 2]):
            with self.assertRaises(ValueError): geometry_page(obs, region=region)
        obs['collision'].update(scope='view', coverage={'terrain': False, 'entities': True}, complete=False)
        self.assertFalse(geometry_page(obs)['complete'])
        del obs['collision']
        self.assertIsNone(geometry_overview(obs))
        with self.assertRaises(ValueError): geometry_page(obs)

    def test_real_tool_query_without_workspace_does_not_touch_bridge(self):
        with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
            root = Path(folder)
            cfg = RunConfig(agent='geometry', save='test', model='stub', engine='codex', runtime='host', sim=False, chapter=3)
            metrics = Metrics(root/'metrics.json', 'geometry', 'stub', 'high', 60)
            session = GameSession(cfg, root, metrics)
            session.client = FakeBridge()
            try:
                session.client.obs = dense()
                server = build_server(session)
                observed = asyncio.run(server.call_tool('observe', {}))
                text = '\n'.join(b.text for b in observed.content if hasattr(b, 'text'))
                overview = json.JSONDecoder().raw_decode(text.split(ROOM_JSON_HEADER, 1)[1])[0]['collision']
                self.assertEqual(overview['hazards']['detailsOmitted'], 200)
                self.assertEqual(len(session.last_obs['collision']['objects']), 202)
                session.workspace = None
                with patch.object(session.client, 'call', side_effect=AssertionError('query advanced game')):
                    response = asyncio.run(server.call_tool('observe', {'detail': 'geometry', 'limit': 2}))
                self.assertFalse(response.is_error)
                page = json.loads(response.content[0].text)
                self.assertEqual(page['frame'], 12)
                self.assertEqual(page['returned'], 2)
                self.assertEqual(page['matched_total'], 202)
                self.assertEqual(session.last_obs['frame'], 12)
                session.client.obs = observation(13)
                asyncio.run(server.call_tool('observe', {}))
                stale = asyncio.run(server.call_tool('observe', {'detail': 'geometry', 'offset': 2, 'snapshot_id': page['snapshot_id']}))
                self.assertIn('快照已改变', stale.content[0].text)
            finally:
                session.close(False)

    def test_compact_room_keeps_navigation_and_eliminates_duplicate_entities(self):
        obs = enrich_obs(dense())
        obs['entities'].append({'id': 1000, 'type': 'DustStaticSpinner', 'x': 0, 'y': 0, 'w': 12, 'h': 12})
        compact = json.loads(room_json(obs, compact_context=True))
        self.assertEqual(compact['frame'], 12)
        self.assertIn('exits', compact)
        self.assertIn('player', compact)
        self.assertEqual(compact['entities'], [])
        self.assertNotIn('DustStaticSpinner#1000', describe_obs(obs, compact_context=True))
