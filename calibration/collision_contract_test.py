"""Real observation/tool/workspace consumers; only the game bridge is simulated."""
import asyncio
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from calibration.formal_run_test import _observation
from orchestrator.collision_snapshot import current_collision
from orchestrator.config import RunConfig
from orchestrator.execution import WorkspaceCapabilities
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.observation import enrich_obs, room_json
from orchestrator.server import build_server, ROOM_JSON_HEADER


def observation(frame=12, x=0, present=True):
    obs = _observation()
    obs.update(frame=frame, controllable=True, frozen=True)
    obs['level'].update(sid='Celeste/3-CelestialResort', room='s3')
    brick = {'id': 171, 'type': 'Solid', 'cat': 'solid', 'x': x, 'y': -8, 'w': 8, 'h': 8, 'collidable': True}
    obs['entities'] = [brick] if present else []
    terrain = {'id': 'terrain:0', 'source': 'terrain', 'type': 'SolidTiles', 'kind': 'solid',
               'collidable': True, 'mutable': False,
               'geometry': {'shape': 'rect', 'x': 16, 'y': 0, 'w': 8, 'h': 24}}
    entity = {'id': 171, 'source': 'entity', 'type': 'Solid', 'kind': 'solid', 'collidable': True,
              'mutable': True, 'geometry': {'shape': 'rect', 'x': x, 'y': -8, 'w': 8, 'h': 8}}
    obs['collision'] = {'version': 1, 'frame': frame, 'sid': obs['level']['sid'], 'room': 's3',
                        'scope': 'room', 'bounds': [0, -16, 40, 40],
                        'coverage': {'terrain': True, 'entities': True}, 'complete': True,
                        'geometryComplete': True, 'objects': [terrain] + ([entity] if present else []),
                        'player': {'hitbox': obs['player']['hitbox']}}
    return obs


class FakeBridge:
    def __init__(self):
        self.obs = observation()

    def call(self, command, **args):
        if command == 'observe':
            return {'obs': copy.deepcopy(self.obs)}
        if command == 'step':
            self.obs = observation(self.obs['frame'] + 1, x=8)
            return {'obs': copy.deepcopy(self.obs), 'result': {
                'consumed': 1, 'planned': 1, 'stop': 'done', 'events': [], 'trajectory': []}}
        if command == 'hello':
            return {}
        raise AssertionError('Unexpected external operation: ' + command)

    def close(self):
        pass


class CollisionContractTests(unittest.TestCase):
    def test_authoritative_terrain_survives_ascii_entity_overlays(self):
        obs = observation()
        obs['tiles']['rows'] = ['..=..'] * 3  # Presentation overlay obscures the underlying #.
        data = json.loads(room_json(enrich_obs(obs)))
        self.assertEqual(data['solids'], [[16, 0, 8, 24]])
        self.assertEqual([o['id'] for o in data['collision']['objects']], ['terrain:0', 171])
        self.assertIn('不能只用 solids', data['solidsNote'])

    def test_stale_partial_missing_and_malformed_snapshots_are_not_fabricated(self):
        for change in ({'frame': 11}, {'sid': 'another'}, {'room': 'elsewhere'},
                       {'bounds': [10**1000, 0, 1, 1]}, {'bounds': [1e308, 0, 1e308, 1]},
                       {'complete': False}):
            obs = observation()
            obs['collision'].update(change)
            self.assertIsNone(json.loads(room_json(enrich_obs(obs)))['collision'])
        obs = observation()
        obs['collision'].update(scope='view', coverage={'terrain': False, 'entities': True}, complete=False)
        obs['collision']['objects'] = [o for o in obs['collision']['objects'] if o['source'] == 'entity']
        self.assertIsNotNone(current_collision(obs))
        del obs['collision']
        self.assertIsNone(json.loads(room_json(enrich_obs(obs)))['collision'])

    # Keep the in-context baseline/delta fallback consumer active for this contract test.
    @patch('orchestrator.server.workspace_capabilities',
           lambda cfg: WorkspaceCapabilities('host', True, True, None))
    def test_real_session_and_tool_dispatch_refresh_same_room_geometry_and_images(self):
        with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
            root = Path(folder)
            run = root/'run'
            run.mkdir()
            cfg = RunConfig(agent='isolated', save='test', model='stub', engine='codex', runtime='host',
                            sim=False, chapter=3, cutscene_mode='agent')
            cfg.min_frames_per_act = 1  # Isolate geometry updates; action-minimum policy has its own regressions.
            metrics = Metrics(run/'metrics.json', 'isolated', 'stub', 'high', 60)
            session = GameSession(cfg, run, metrics)
            session.client = FakeBridge()
            # Close handles before TemporaryDirectory cleanup on Windows.
            try:
                server = build_server(session)
                observed = asyncio.run(server.call_tool('observe', {}))
                images = [b for b in observed.content if getattr(b, 'type', '') == 'image']
                self.assertEqual(len(images), 1)  # No screenshot fixture: this is the collision image.
                self.assertEqual(images[0].meta['celeste'], {'role': 'collision', 'frame': 12,
                    'sid': 'Celeste/3-CelestialResort', 'room': 's3'})
                text = '\n'.join(b.text for b in observed.content if hasattr(b, 'text'))
                data = json.JSONDecoder().raw_decode(text.split(ROOM_JSON_HEADER, 1)[1])[0]
                self.assertEqual(data['collision']['frame'], 12)
                manifest = session.workspace/'game/collision.json'
                before = json.loads(manifest.read_text(encoding='utf-8'))
                first_image = session.workspace/before['image']
                self.assertEqual(Image.open(io.BytesIO(first_image.read_bytes())).format, 'PNG')
                acted = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': '核对移动后的碰撞体'}))
                self.assertFalse(acted.is_error)
                text = '\n'.join(b.text for b in acted.content if hasattr(b, 'text'))
                self.assertIn('当前帧的统一碰撞数据', text)
                self.assertEqual(sum(getattr(b, 'type', '') == 'image' for b in acted.content), 1)
                after = json.loads(manifest.read_text(encoding='utf-8'))
                self.assertEqual(after['collision']['frame'], 13)
                self.assertEqual(after['collision']['objects'][1]['geometry']['x'], 8)
                self.assertNotEqual(after['image'], before['image'])
                self.assertTrue(first_image.exists())
                history = next((session.workspace/'game/rooms').glob('*.json'))
                historical = json.loads(history.read_text(encoding='utf-8'))
                self.assertTrue(historical['snapshot']['historical'])
                self.assertEqual(historical['obs']['collision']['frame'], 12)
                session.client.obs = observation(14, present=False)
                session.observe()
                removed = json.loads(manifest.read_text(encoding='utf-8'))
                self.assertEqual(len(removed['collision']['objects']), 1)
                self.assertFalse(first_image.exists())
                session.client.obs['storyInput'] = 'dialogue'
                session.client.obs['level'].update(inCutscene=True, dialogOpen=True, dialogWaiting=True, dialogChoices=[])
                advanced = asyncio.run(server.call_tool('cutscene', {'action': 'advance'}))
                self.assertFalse(advanced.is_error)
                self.assertEqual(sum(getattr(b, 'type', '') == 'image' for b in advanced.content), 1)
                current = json.loads(manifest.read_text(encoding='utf-8'))
                self.assertEqual(current['collision']['frame'], session.client.obs['frame'])
                del session.client.obs['collision']  # Older bridge: don't keep the last good map as current.
                legacy = asyncio.run(server.call_tool('observe', {}))
                self.assertFalse(legacy.is_error)
                self.assertEqual(sum(getattr(b, 'type', '') == 'image' for b in legacy.content), 0)
                missing = json.loads(manifest.read_text(encoding='utf-8'))
                self.assertIsNone(missing['collision'])
                self.assertIsNone(missing['image'])
            finally:
                session.close(False)
