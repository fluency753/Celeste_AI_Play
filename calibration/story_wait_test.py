"""Story waiting boundaries with real session/tool consumers and only a bridge fixture."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from mcp.server.mcpserver.exceptions import ToolError

from orchestrator.config import RunConfig
from orchestrator.game_session import AGENT_STORY_WAIT_FRAMES, ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server


def observation(*, node=None, playing=False, controllable=False, cutscene=True, **level):
    result = {'frame': 100, 'scene': 'Level', 'frozen': True, 'storyInput': node,
              'storyDecision': node, 'story': [], 'controllable': controllable,
              'level': {'sid': 'Celeste/3-CelestialResort', 'side': 'A', 'room': '11-a',
                        'inCutscene': cutscene, 'skippingCutscene': False, 'paused': False,
                        'canPause': False, 'transitioning': False, 'frozen': False,
                        'completed': False, 'dialogOpen': node == 'dialogue',
                        'dialogWaiting': node == 'dialogue', 'dialogChoices': [],
                        'deaths': 0, 'deathsInRoom': 0, 'chapterTimeTicks': 0,
                        'camera': [0, 0], 'bounds': [0, 0, 320, 180]},
              'player': {'pos': [10, 20], 'speed': [0, 0], 'dead': False,
                         'state': 'Normal' if controllable else 'Dummy', 'inControl': controllable,
                         'dashes': 1, 'maxDashes': 1, 'stamina': 110, 'onGround': True,
                         'facing': 'R', 'ducking': False, 'wallLeft': False, 'wallRight': False,
                         'hitbox': [6, 9, 8, 11],
                         'timers': {'jumpGrace': 0, 'varJump': 0, 'dashCooldown': 0}}}
    result['level'].update(level)
    if playing:
        result['storyPlaybackState'] = {'state': 'playing', 'source': 'Textbox'}
    return result


class StoryBridge:
    def __init__(self, obs, after_step=None):
        self.obs = copy.deepcopy(obs)
        self.after_step = after_step
        self.steps = []
        self.frames = 0

    def call(self, command, **args):
        if command == 'observe':
            return {'obs': copy.deepcopy(self.obs)}
        if command == 'step':
            inputs = args['inputs']
            self.steps.append(inputs)
            count = sum(int(part.split(',')[0]) for part in inputs)
            self.frames += count
            self.obs['frame'] += count
            self.obs['level']['chapterTimeTicks'] += count * 166667
            if self.after_step:
                self.after_step(self, inputs)
            return {'obs': copy.deepcopy(self.obs),
                    'result': {'consumed': count, 'planned': count, 'events': [],
                               'trajectory': [], 'stop': 'done', 'deviation': None}}
        if command == 'deactivate':
            return {}
        raise AssertionError(f'Unexpected external bridge operation: {command}')

    def close(self):
        pass


class StoryWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        cfg = RunConfig(agent='story-wait-test', save='test', model='test', runtime='host',
                        cutscene_mode='agent', knowledge_profile='explore', chapter=3)
        cfg.thought_lead_s = 0
        self.session = GameSession(cfg, self.root, Metrics(self.root/'metrics.json', 'test', 'test', 'high', 600))
        self.session.workspace = self.root/'workspace'
        (self.session.workspace/'game/rooms').mkdir(parents=True)

    def tearDown(self):
        self.session.close(release=False)
        self.temp.cleanup()

    def bridge(self, obs, after_step=None):
        self.session.client = StoryBridge(obs, after_step)
        self.session.last_obs = copy.deepcopy(obs)
        return self.session.client

    def test_observe_unknown_cutscene_is_read_only_and_saves_observation(self):
        client = self.bridge(observation())
        obs, _ = self.session.observe()
        self.assertEqual(client.steps, [])
        self.assertEqual(obs['frame'], 100)
        self.assertIn('未知界面', '\n'.join(self.session.observe_notes))
        saved = json.loads((self.session.workspace/'game/latest_obs.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['obs']['frame'], 100)

    def test_watch_unknown_through_real_mcp_returns_observation_without_steps(self):
        client = self.bridge(observation())
        result = asyncio.run(build_server(self.session).call_tool('cutscene', {'action': 'watch'}))
        self.assertFalse(result.is_error)
        self.assertIn('未知界面', '\n'.join(item.text for item in result.content if item.type == 'text'))
        self.assertEqual(client.steps, [])

    def test_advance_unknown_preserves_current_frame_and_reports_no_confirmation(self):
        client = self.bridge(observation())
        with self.assertRaisesRegex(ActError, '输入未发送.*已自动等待 0 帧'):
            self.session.cutscene('advance')
        self.assertEqual(client.steps, [])
        self.assertEqual(self.session.last_obs['frame'], 100)

    def test_mcp_advance_unknown_preserves_failure_and_the_actual_reason(self):
        client = self.bridge(observation())
        with self.assertRaises(ToolError) as caught:
            asyncio.run(build_server(self.session).call_tool('cutscene', {'action': 'advance'}))
        text = str(caught.exception)
        self.assertIn('输入未发送', text)
        self.assertIn('已自动等待 0 帧', text)
        self.assertIn('新观测已保存', text)
        self.assertNotEqual(text, 'Error executing tool cutscene')
        self.assertEqual(client.steps, [])

    def test_invalid_choice_after_wait_reports_range_and_saves_the_new_node(self):
        def after(client, inputs):
            client.obs['storyInput'] = 'choice'
            client.obs['level']['dialogChoices'] = [{'index':0,'text':'甲'}]
            client.obs['level']['dialogChoiceReady'] = True
        client = self.bridge(observation(playing=True), after)
        with self.assertRaisesRegex(ActError, 'choice.*输入未发送.*已自动等待 30 帧'):
            self.session.cutscene('choose', choice=99)
        self.assertEqual(client.steps, [['30']])
        self.assertEqual(self.session.last_obs['frame'], 130)
        self.assertEqual(self.session.last_obs['storyInput'], 'choice')

    def test_advance_dialogue_then_unknown_does_not_guess_next_node(self):
        def after(client, inputs):
            client.obs.update(storyInput=None, storyDecision=None)
            client.obs['level'].update(dialogOpen=False, dialogWaiting=False)
        client = self.bridge(observation(node='dialogue'), after)
        obs, _, info = self.session.cutscene('advance')
        self.assertEqual(client.steps, [['1,C', '1']])
        self.assertEqual((info['frames'], info['auto_frames'], obs['frame']), (2, 0, 102))
        self.assertIn('未知界面', '\n'.join(info['auto_notes']))

    def test_choose_wait_then_unknown_preserves_frame_and_reports_actual_wait(self):
        def after(client, inputs):
            client.obs.pop('storyPlaybackState', None)
        client = self.bridge(observation(playing=True), after)
        with self.assertRaisesRegex(ActError, '输入未发送.*已自动等待 30 帧'):
            self.session.cutscene('choose', choice=0)
        self.assertEqual(client.steps, [['30']])
        saved = json.loads((self.session.workspace/'game/latest_obs.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['obs']['frame'], 130)

    def test_memo_confirmation_is_explicit_and_never_auto_repeated(self):
        client = self.bridge(observation(node='memo'))
        _, _, info = self.session.cutscene('advance')
        self.assertEqual(client.steps, [['1,C', '1']])
        self.assertEqual(info['auto_frames'], 0)
        self.assertEqual(info['story_input'], 'memo')

    def test_confirmed_animation_budget_does_not_reset_when_source_changes(self):
        def after(client, inputs):
            client.obs['storyPlaybackState']['source'] = 'MemoPage' if len(client.steps) % 2 else 'Textbox'
        client = self.bridge(observation(playing=True), after)
        _, _, info = self.session.cutscene('watch')
        self.assertEqual(client.frames, AGENT_STORY_WAIT_FRAMES)
        self.assertEqual(info['auto_frames'], AGENT_STORY_WAIT_FRAMES)
        self.assertTrue(all(inputs == ['30'] for inputs in client.steps))
        self.assertIn('等待预算', '\n'.join(info['auto_notes']))

    def test_advance_shares_one_budget_before_and_after_confirmation(self):
        def after(client, inputs):
            if inputs == ['1,C', '1']:
                client.obs.update(storyInput=None, storyDecision=None)
                client.obs['level'].update(dialogOpen=False, dialogWaiting=False)
            elif client.frames == 300:
                client.obs.update(storyInput='dialogue', storyDecision='dialogue')
                client.obs['level'].update(dialogOpen=True, dialogWaiting=True)
        client = self.bridge(observation(playing=True), after)
        _, _, info = self.session.cutscene('advance')
        self.assertEqual(client.frames, AGENT_STORY_WAIT_FRAMES + 2)
        self.assertEqual(info['frames'], 2)
        self.assertEqual(info['auto_frames'], AGENT_STORY_WAIT_FRAMES)
        self.assertEqual(sum(inputs == ['1,C', '1'] for inputs in client.steps), 1)

    def test_paused_control_guard_prevents_known_animation_wait(self):
        client = self.bridge(observation(playing=True))
        self.session.control_guard = lambda: '用户已暂停'
        _, _, info = self.session.cutscene('watch')
        self.assertEqual(client.steps, [])
        self.assertEqual(info['auto_frames'], 0)
        self.assertIn('用户已暂停', '\n'.join(info['auto_notes']))

    def test_known_death_and_transition_can_advance_to_control(self):
        for change in ('death', 'transition'):
            with self.subTest(change=change):
                start = observation(cutscene=False)
                if change == 'death':
                    start['player']['dead'] = True
                else:
                    start['level']['transitioning'] = True
                def after(client, inputs):
                    frame = client.obs['frame']
                    client.obs = observation(controllable=True, cutscene=False)
                    client.obs['frame'] = frame
                client = self.bridge(start, after)
                _, _, count, _ = self.session._auto_advance(start, None)
                self.assertEqual(client.steps, [['30']])
                self.assertEqual(count, 30)

    def test_unknown_dummy_handover_allows_explicit_act_without_pre_wait(self):
        start = observation(cutscene=False)
        client = self.bridge(start)
        _, _, count, _ = self.session._auto_advance(start, None)
        self.assertEqual(count, 0)
        self.assertTrue(self.session._idle_handover)
        result = self.session.act(['40,C'], '尝试确认未知界面', None)
        self.assertEqual(client.steps, [['40,C']])
        self.assertEqual(result.auto_frames, 0)

    def test_act_ending_in_unknown_cutscene_does_not_auto_wait(self):
        def after(client, inputs):
            frame = client.obs['frame']
            client.obs = observation()
            client.obs['frame'] = frame
        client = self.bridge(observation(controllable=True, cutscene=False), after)
        result = self.session.act(['40,R'], '靠近交互处', None)
        self.assertEqual(client.steps, [['40,R']])
        self.assertEqual(result.auto_frames, 0)
        self.assertIn('未知界面', '\n'.join(result.auto_notes))

    def test_auto_mode_retains_its_ordinary_cutscene_wait(self):
        self.session.cfg.cutscene_mode = 'auto'
        def after(client, inputs):
            client.obs = observation(controllable=True, cutscene=False)
        start = observation()
        client = self.bridge(start, after)
        _, _, count, _ = self.session._auto_advance(start, None)
        self.assertEqual(client.steps, [['30']])
        self.assertEqual(count, 30)


if __name__ == '__main__':
    unittest.main()
