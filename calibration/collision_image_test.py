"""工具结果里什么时候附碰撞图（--collision-image）：默认档位、每档的附图时机、提示词与命令行。"""
import asyncio
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calibration.collision_contract_test import FakeBridge, observation
from orchestrator.config import RunConfig
from orchestrator.execution import WorkspaceCapabilities
from orchestrator.game_session import GameSession
from orchestrator.launch_options import build, config_for
from orchestrator.metrics import Metrics
from orchestrator.modes import collision_image_mode
from orchestrator.prompting import fill, play_prompt
from orchestrator.server import ACT_DESCRIPTION, OBSERVE_DESCRIPTION, build_server


def collision_images(result):
    return [b for b in result.content
            if getattr(b, 'type', '') == 'image' and ((getattr(b, 'meta', None) or {}).get('celeste') or {}).get('role') == 'collision']


class ModeTests(unittest.TestCase):
    def test_defaults_follow_the_executor(self):
        self.assertEqual(collision_image_mode(RunConfig(agent='a', save='s', model='m')), 'room')
        self.assertEqual(collision_image_mode(RunConfig(agent='a', save='s', model='m', engine='codex')), 'always')
        self.assertEqual(collision_image_mode(RunConfig(agent='a', save='s', model='m', engine='codex',
                                                        collision_image='off')), 'off')

    def test_texts_describe_the_chosen_mode(self):
        c = RunConfig(agent='a', save='s', model='m', mode='explore', knowledge_profile='explore', chapter=4)
        self.assertIn('同一房间里的碰撞图只写进工作区', fill(ACT_DESCRIPTION, c))
        self.assertIn('和完整碰撞图', fill(OBSERVE_DESCRIPTION, c))
        c.collision_image = 'always'
        self.assertIn('角色状态和完整碰撞图', fill(ACT_DESCRIPTION, c))
        c.collision_image = 'off'
        self.assertIn('碰撞图不附在结果里', fill(ACT_DESCRIPTION, c))
        self.assertIn('碰撞图只写进工作区', fill(OBSERVE_DESCRIPTION, c))
        speed = RunConfig(agent='a', save='s', model='m', sim=True, collision_image='room')
        self.assertIn('进入新房间与 observe 时返回', play_prompt(speed))
        for mode in ('always', 'room', 'off', None):
            speed.collision_image = mode
            self.assertEqual(re.findall(r'\{[a-z_]+\}', fill(ACT_DESCRIPTION, speed) + fill(OBSERVE_DESCRIPTION, speed)), [])

    def test_switch_reaches_command_line_and_config(self):
        self.assertIn('--collision-image', build({'agent': 'x', 'collision_image': 'off'}))
        self.assertEqual(config_for({'agent': 'x', 'collision_image': 'off'}).collision_image, 'off')
        self.assertNotIn('--collision-image', build({'agent': 'x'}))
        with self.assertRaises(ValueError):
            build({'agent': 'x', 'collision_image': 'sometimes'})

    def test_run_parses_the_switch(self):
        from orchestrator.run import parse_args
        with tempfile.TemporaryDirectory() as folder:
            argv = ['run', '--engine', 'codex', '--agent', 'test', '--save', 'selected', '--chapter', '0']
            with patch('orchestrator.config.REPO_ROOT', Path(folder)), patch.object(sys, 'argv', argv + ['--collision-image', 'room']):
                self.assertEqual(parse_args()[0].collision_image, 'room')
            with patch('orchestrator.config.REPO_ROOT', Path(folder)), patch.object(sys, 'argv', argv):
                self.assertIsNone(parse_args()[0].collision_image)


@patch('orchestrator.server.workspace_capabilities', lambda cfg: WorkspaceCapabilities('host', True, True, None))
class AttachmentTests(unittest.TestCase):
    def run_calls(self, mode):
        with tempfile.TemporaryDirectory() as folder, patch('orchestrator.config.REPO_ROOT', Path(folder)):
            root = Path(folder)
            cfg = RunConfig(agent='img', save='test', model='stub', engine='claude', runtime='host', sim=False,
                            chapter=3, collision_image=mode)
            cfg.min_frames_per_act = 1
            session = GameSession(cfg, root, Metrics(root/'metrics.json', 'img', 'stub', 'high', 60))
            session.client = FakeBridge()
            server = build_server(session)
            try:
                observed = asyncio.run(server.call_tool('observe', {}))
                same = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': '同房间'}))
                session.client.obs = observation(20)
                session.client.obs['level']['room'] = 's4'
                session.client.obs['collision']['room'] = 's4'
                original = session.client.call
                def to_new_room(command, **args):
                    if command == 'step':
                        obs = observation(session.client.obs['frame'] + 1)
                        obs['level']['room'] = obs['collision']['room'] = 's4'
                        session.client.obs = obs
                        return {'obs': obs, 'result': {'consumed': 1, 'planned': 1, 'stop': 'room', 'events': [], 'trajectory': []}}
                    return original(command, **args)
                session.client.call = to_new_room
                entered = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': '进新房间'}))
                return [len(collision_images(r)) for r in (observed, same, entered)]
            finally:
                session.close(False)

    def test_each_mode(self):
        self.assertEqual(self.run_calls('always'), [1, 1, 1])
        self.assertEqual(self.run_calls('room'), [1, 0, 1])
        self.assertEqual(self.run_calls('off'), [0, 0, 0])
        self.assertEqual(self.run_calls(None), [1, 0, 1])          # Claude 执行器默认：只在进房与 observe 时


if __name__ == '__main__':
    unittest.main()
