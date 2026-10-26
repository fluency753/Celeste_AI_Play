"""Presentation compression preserves source geometry and cached query semantics."""
import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from calibration.collision_contract_test import observation, FakeBridge
from orchestrator.geometry_context import geometry_overview, geometry_page, GeometryContext
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
    def test_baseline_group_columns_restore_all_precise_geometry_instances(self):
        obs = dense(14)
        obs['collision']['objects'][2]['geometry']['parts'][0]['r'] = 5.123456789
        layout = geometry_overview(obs)
        restored = {}
        def translate(shape, x, y):
            result = copy.deepcopy(shape)
            if 'x' in result: result['x'] += x
            if 'y' in result: result['y'] += y
            if 'parts' in result: result['parts'] = [translate(p, x, y) for p in result['parts']]
            return result
        for group in layout['groups']:
            for row in group['instances']:
                values = dict(zip(group.get('instanceColumns', layout['instanceColumns']), row))
                shape = values.get('shape', group.get('shape'))
                restored[(group.get('source'), values['id'])] = translate(layout['shapes'][shape], values['x'], values['y'])
        self.assertEqual(restored, {(o.get('source'), o['id']): o['geometry'] for o in obs['collision']['objects']})

    def test_delta_reconstructs_spawn_remove_move_disable_and_shape_state_changes(self):
        ctx = GeometryContext()
        obs = dense(2)
        baseline, candidate = ctx.prepare(obs)
        self.assertIsNone(ctx.last_emitted)
        ctx.commit(candidate)
        before = copy.deepcopy(ctx.last_emitted['records'])
        changed = copy.deepcopy(obs)
        changed['frame'] = changed['collision']['frame'] = 13
        changed['collision']['objects'].pop(0)
        changed['collision']['objects'][0]['collidable'] = False
        changed['collision']['objects'][1]['geometry']['x'] = 3
        changed['collision']['objects'].append({'id': 'spawn', 'source': 'entity', 'type': 'Boss',
                                                'kind': 'hazard', 'geometry': {'shape': 'rect', 'x': 1, 'y': 2, 'w': 3, 'h': 4}})
        changed['entities'].append({'id': 1001, 'fields': {'active': False, 'attackTimer': 0}})
        changed['respawnPoint'] = [10, 20]
        delta, candidate = ctx.prepare(changed)
        self.assertEqual(delta['mode'], 'delta')
        self.assertEqual(delta['base_snapshot_id'], baseline['snapshot_id'])
        self.assertEqual(delta['roomChanges']['respawnPoint'], [10, 20])
        for source, ident in delta['removed']:
            before.pop((source, ident))
        for group in delta['added'] + delta['changed']:
            for row in group['instances']:
                obj = {k: v for k, v in group.items() if k not in {'instances', 'instanceColumns'}}
                values = dict(zip(group.get('instanceColumns', ['id', 'x', 'y']), row))
                obj.update(id=values['id'], at=[values['x'], values['y']])
                if 'shape' in values:
                    obj['shape'] = values['shape']
                if 'state' in values:
                    if values['state']: obj['state'] = values['state']
                    else: obj.pop('state', None)
                before[(obj.get('source'), obj['id'])] = obj
                self.assertIn(obj['shape'], delta['shapes'])
        self.assertEqual(before, candidate['records'])
        # Failed emission does not advance last_emitted; prepare remains reproducible.
        self.assertEqual(ctx.prepare(changed)[0], delta)
        ctx.commit(candidate)
        empty, _ = ctx.prepare(changed)
        self.assertEqual(empty['changed'], [])
        self.assertEqual(empty['added'], [])

    def test_reset_scope_invalid_room_regression_and_force(self):
        ctx = GeometryContext()
        obs = observation()
        _, candidate = ctx.prepare(obs)
        ctx.commit(candidate)
        for field, value in [('scope', 'view'), ('bounds', [-8, -16, 48, 40]),
                             ('geometryComplete', False)]:
            changed = copy.deepcopy(obs)
            changed['collision'][field] = value
            self.assertEqual(ctx.prepare(changed)[0]['mode'], 'baseline')
        self.assertEqual(ctx.prepare(obs, force=True)[0]['mode'], 'baseline')
        changed = copy.deepcopy(obs)
        changed['frame'] = changed['collision']['frame'] = 1
        self.assertEqual(ctx.prepare(changed)[0]['mode'], 'baseline')
        invalid = copy.deepcopy(obs)
        del invalid['collision']
        payload, candidate = ctx.prepare(invalid)
        self.assertEqual(payload['mode'], 'unavailable')
        ctx.commit(candidate)
        self.assertEqual(ctx.prepare(obs)[0]['mode'], 'baseline')

    def test_state_only_changes_invalidate_cached_snapshot_and_do_not_depend_on_field_order(self):
        ctx = GeometryContext()
        obs = observation()
        obs['entities'][0]['fields'] = {'active': False, 'attackTimer': .12345678}
        baseline, candidate = ctx.prepare(obs)
        ctx.commit(candidate)
        obs['entities'][0]['fields'] = {'attackTimer': .12345678, 'active': False}
        self.assertEqual(ctx.prepare(obs)[0]['changed'], [])
        obs['entities'][0]['fields']['active'] = True
        delta, _ = ctx.prepare(obs)
        self.assertNotEqual(delta['base_snapshot_id'], delta['snapshot_id'])
        self.assertEqual(len(delta['changed']), 1)
        self.assertEqual(geometry_page(obs)['snapshot_id'], delta['snapshot_id'])
        with self.assertRaises(ValueError):
            geometry_page(obs, expected_snapshot=baseline['snapshot_id'])

    def test_dense_overview_counts_shapes_and_original_unchanged(self):
        obs = dense()
        original = copy.deepcopy(obs)
        result = geometry_overview(obs)
        self.assertTrue(result['detailsComplete'])
        self.assertEqual(result['object_count'], 202)
        self.assertEqual(sum(len(g['instances']) for g in result['groups']), 202)
        self.assertEqual(len(result['groups']), 3)
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
        self.assertIn('unknown', [row[0] for g in result['groups'] for row in g['instances']])
        self.assertEqual({g['collidable'] for g in result['groups'] if g.get('kind') == 'hazard'}, {True, False})
        self.assertEqual(result['object_count'], 203)

    def test_dense_hazards_preserve_unique_boss_and_direction_state(self):
        obs = dense(13)
        obs['collision']['objects'][-1]['type'] = 'AngryOshiro'
        obs['entities'].append({'id': 1012, 'fields': {'attackTimer': .3}, 'dir': 'left'})
        for i in range(6):
            obs['entities'].append({'id': 1000+i, 'dir': 'up'})
        result = geometry_overview(obs)
        boss = next(o for o in result['groups'] if o['type'] == 'AngryOshiro')
        self.assertEqual(boss['state']['attackTimer'], .3)
        self.assertEqual(boss['attributes']['dir'], 'left')
        directed = [g for g in result['groups'] if g.get('attributes', {}).get('dir') == 'up']
        self.assertEqual(sum(len(g['instances']) for g in directed), 6)

    def test_selected_state_keeps_false_zero_precise_values_and_raw_remains_queryable(self):
        obs = dense(14)
        obs['entities'] = [{'id': 1000+i, 'fields': {'offset': i*.01, 'attackTimer': i*.00001,
                            'active': False, 'imageOffset': [0, 0]}, 'moved': [i, 0]} for i in range(14)]
        result = geometry_overview(obs)
        group = next(g for g in result['groups'] if g.get('kind') == 'hazard')
        self.assertEqual(len(group['instances']), 14)
        self.assertEqual(group['instances'][3][3]['attackTimer'], obs['entities'][3]['fields']['attackTimer'])
        self.assertFalse(group['instances'][0][3]['active'])
        self.assertNotIn('imageOffset', group['instances'][0][3])
        raw = geometry_page(obs)
        self.assertEqual(raw['objects'][5]['state']['offset'], .03)
        self.assertEqual(raw['objects'][5]['moved'], [3, 0])

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
                self.assertEqual(overview['object_count'], 202)
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

    def test_real_server_delta_queries_render_failure_and_cutscene_share_emitted_baseline(self):
        with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
            root = Path(folder)
            cfg = RunConfig(agent='geometry', save='test', model='stub', engine='codex', runtime='host',
                            sim=False, chapter=3, cutscene_mode='agent')
            cfg.min_frames_per_act = 1
            metrics = Metrics(root/'metrics.json', 'geometry', 'stub', 'high', 60)
            session = GameSession(cfg, root, metrics)
            session.client = FakeBridge()
            server = build_server(session)
            def call(name, args):
                result = asyncio.run(server.call_tool(name, args))
                text = '\n'.join(b.text for b in result.content if hasattr(b, 'text'))
                return result, text
            def delta_from(text):
                return json.JSONDecoder().raw_decode(text.split('当前帧的统一碰撞数据（变化）：\n', 1)[1])[0]
            try:
                _, text = call('observe', {})
                baseline = json.JSONDecoder().raw_decode(text.split(ROOM_JSON_HEADER, 1)[1])[0]['collision']
                with patch.object(session.client, 'call', side_effect=AssertionError('cache query touched game')):
                    _, detail = call('observe', {'detail': 'geometry', 'region': [0, -16, 40, 40]})
                    self.assertEqual(json.loads(detail)['frame'], 12)
                    _, player = call('observe', {'detail': 'player'})
                    self.assertEqual(json.loads(player)['frame'], 12)
                # 碰撞图由 GameSession 按帧画一次、写工作区与工具结果共用：两处都替换才是真实的画图失败
                with patch('orchestrator.server.render_collision_view', side_effect=ValueError('render failed')), \
                        patch('orchestrator.game_session.render_collision_view', side_effect=ValueError('render failed')):
                    with self.assertRaises(UnexpectedToolError):
                        call('act', {'inputs': ['1,R'], 'thought': '测试输出失败'})
                # The first act advanced the external game, but no response was emitted.
                result, text = call('act', {'inputs': ['1,R'], 'thought': '核对下一动作'})
                self.assertFalse(result.is_error)
                delta = delta_from(text)
                self.assertEqual(delta['base_snapshot_id'], baseline['snapshot_id'])
                self.assertEqual(delta['frame'], 14)
                self.assertLess(len(text), 2500)
                self.assertEqual(len(delta['changed']), 1)
                session.client.obs['storyInput'] = 'dialogue'
                session.client.obs['level'].update(inCutscene=True, dialogOpen=True, dialogWaiting=True, dialogChoices=[])
                result, text = call('cutscene', {'action': 'advance'})
                self.assertFalse(result.is_error)
                self.assertIn('"mode":"delta"', text)
                self.assertIn(delta['snapshot_id'], text)
                _, text = call('observe', {})
                refreshed = json.JSONDecoder().raw_decode(text.split(ROOM_JSON_HEADER, 1)[1])[0]['collision']
                self.assertEqual(refreshed['mode'], 'baseline')
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
