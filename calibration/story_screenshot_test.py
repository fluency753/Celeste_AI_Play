import asyncio
import base64
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from calibration.collision_contract_test import FakeBridge
from orchestrator.config import RunConfig
from orchestrator.execution import WorkspaceCapabilities
from orchestrator.game_session import GameSession
from orchestrator.metrics import Metrics
from orchestrator.observation import describe_obs, upscale_png
from orchestrator.server import build_server
from tools.bridge_client import BridgeClient


def png(size):
    output = io.BytesIO()
    Image.new('RGB', size, 'navy').save(output, format='PNG')
    return base64.b64encode(output.getvalue()).decode()


class StoryScreenshotTests(unittest.TestCase):
    def test_native_hud_resolution_is_preserved_and_world_is_upscaled(self):
        for before, after in [((320, 180), (640, 360)), ((1280, 720), (1280, 720))]:
            with self.subTest(size=before):
                self.assertEqual(Image.open(io.BytesIO(upscale_png(png(before)))).size, after)

    def test_transport_preserves_image_failure_and_scope_for_tool_consumer(self):
        for error in (None, '原生剧情界面截图失败，请重新观测。'):
            reply = {'id': 1, 'ok': True, 'obs': {}, 'imageScope': 'story_hud',
                     'imageFormat': 'png-base64-1280x720', 'imageError': error}
            client = BridgeClient.__new__(BridgeClient)
            client.cancel_check = None
            client.next_id = 1
            client._send = Mock()
            client.file = io.BytesIO((json.dumps(reply) + '\n').encode())
            received = client.call('observe')
            self.assertEqual(received['obs']['screenshot']['scope'], 'story_hud')
            text = describe_obs(received['obs'])
            self.assertIn(error or '截图包含当前原生剧情界面', text)

    @patch('orchestrator.server.workspace_capabilities',
           lambda cfg: WorkspaceCapabilities('host', True, True, None))
    def test_same_room_act_keeps_story_hud_image_in_real_tool_dispatch(self):
        image = png((640, 360))

        class HudBridge(FakeBridge):
            def call(self, command, **args):
                reply = super().call(command, **args)
                if 'obs' in reply:
                    reply['obs']['screenshot'] = {'scope': 'story_hud'}
                    reply['image'] = image
                return copy.deepcopy(reply)

        with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
            root = Path(folder)
            cfg = RunConfig(agent='hud-image', save='test', model='stub', runtime='host',
                            sim=False, cutscene_mode='agent', collision_image='off')
            cfg.min_frames_per_act = 1
            session = GameSession(cfg, root, Metrics(root/'metrics.json', 'hud-image', 'stub', 'high', 60))
            session.client = HudBridge()
            try:
                server = build_server(session)
                asyncio.run(server.call_tool('observe', {}))
                result = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': '操作当前界面'}))
                pictures = [item for item in result.content if getattr(item, 'type', '') == 'image']
                self.assertEqual(len(pictures), 1)
                self.assertEqual(pictures[0].meta['celeste']['role'], 'screenshot')
                self.assertEqual(Image.open(io.BytesIO(base64.b64decode(pictures[0].data))).size, (640, 360))
            finally:
                session.close(False)
