"""Story waiting boundaries with real session/tool consumers and only a bridge fixture."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mcp.server.mcpserver.exceptions import ToolError

from orchestrator.config import RunConfig
from orchestrator.game_session import AGENT_STORY_WAIT_FRAMES, TRANSITION_WAIT_FRAMES, ActError, GameSession
from orchestrator.metrics import Metrics
from orchestrator.server import build_server
from orchestrator.navigation import ChapterNavigation
from orchestrator.segments import Piece
from tools.bridge_client import BridgeClient


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
            if self.obs.get('level') is not None:
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


class RespawnBridge(StoryBridge):
    death_frame = 97790

    def __init__(self, *, ready=97921, pause_at=None, death_event=True, screen_wipe=True, confirm=False):
        super().__init__(observation(controllable=True, cutscene=False))
        self.obs.update(frame=self.death_frame - 1, save={'slot': 15})
        self.ready, self.pause_at = ready, pause_at
        self.paused = False
        self.stop_lists = []
        self.death_event, self.screen_wipe, self.confirm = death_event, screen_wipe, confirm

    def call(self, command, **args):
        if command != 'step':
            return super().call(command, **args)
        inputs, stops = args['inputs'], args['stop_on']
        self.steps.append(inputs)
        self.stop_lists.append(stops)
        planned = sum(int(part.split(',')[0]) for part in inputs)
        frame = self.obs['frame']
        events = []
        if frame < self.death_frame:
            count, stop = (1, 'death') if self.death_event else (planned, 'done')
            events = ([{'type': 'death', 'frame': self.death_frame, 'detail': self.obs['level']['room']}]
                      if self.death_event else [])
            self.obs.update(controllable=False, player=None)
            self.obs.pop('pauseMenu', None)
            self.obs['level'].update(screenWipe=self.screen_wipe, deaths=1, paused=False)
        elif self.confirm and any(',C' in part for part in inputs):
            count, stop = 1, 'controllable'
            self.obs.update(controllable=True, player=observation(controllable=True)['player'])
            self.obs['level']['screenWipe'] = False
        elif self.ready is not None and frame + planned >= self.ready and 'controllable' in stops:
            count, stop = self.ready - frame, 'controllable'
            self.obs.update(controllable=True, player=observation(controllable=True)['player'])
            self.obs['level']['screenWipe'] = False
        else:
            count, stop = planned, 'done'
        self.frames += count
        self.obs['frame'] += count
        if self.pause_at is not None and self.obs['frame'] >= self.pause_at:
            self.paused = True
        return {'obs': copy.deepcopy(self.obs), 'result': {'consumed': count, 'planned': planned,
                'events': events, 'trajectory': [], 'stop': stop}}


class BridgeWire:
    """Only replace the socket transport; retain BridgeClient hooks and failure tracking."""
    def __init__(self, bridge):
        self.bridge = bridge

    def write(self, data):
        self.request = json.loads(data)

    def readline(self):
        request = self.request.copy()
        ident, command = request.pop('id'), request.pop('cmd')
        return (json.dumps({'id': ident, 'ok': True, **self.bridge.call(command, **request)}) + '\n').encode()

    def makefile(self, mode):
        return self

    def flush(self):
        pass

    def close(self):
        pass

    def setsockopt(self, *args):
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

    def pending_next_chapter(self):
        self.session.cfg.chapter = 4
        self.session._navigation = ChapterNavigation.start([Piece(4)], pending=True,
                                                          previous_sid='Celeste/3-CelestialResort')

    def test_pending_navigation_watch_reads_current_story_without_consuming_target(self):
        self.pending_next_chapter()
        def after(client, inputs):
            client.obs.update(storyInput='dialogue', storyDecision='dialogue',
                              story=[{'kind':'dialogue','text':'仍是当前章节的一句对白'}])
        start=observation(playing=True)
        start['story']=[{'kind':'dialogue','text':'播放途中刚出现的前一句'}]
        client=self.bridge(start,after)
        result=asyncio.run(build_server(self.session).call_tool('cutscene',{'action':'watch'}))
        self.assertFalse(result.is_error)
        self.assertEqual(client.steps,[['30']])
        self.assertEqual(self.session.last_obs['storyInput'],'dialogue')
        self.assertEqual(self.session.last_obs['frame'],130)
        self.assertEqual(self.session.last_obs['storyPlayback'],[{'kind':'dialogue','text':'播放途中刚出现的前一句'}])
        self.assertEqual(self.session.chapter_queue,[Piece(4)])
        self.assertTrue(self.session.chapter_finished)

    def test_pending_navigation_advance_waits_for_next_line_without_extra_confirmation(self):
        self.pending_next_chapter()
        def after(client, inputs):
            if inputs==['1,C','1']:
                client.obs.update(storyInput=None,storyDecision=None,
                                  storyPlaybackState={'state':'playing','source':'dialogue_resume'})
            else:
                client.obs.update(storyInput='dialogue',storyDecision='dialogue')
        client=self.bridge(observation(node='dialogue'),after)
        _,_,info=self.session.cutscene('advance')
        self.assertEqual(client.steps,[['1,C','1'],['30']])
        self.assertEqual((info['frames'],info['auto_frames']),(2,30))
        self.assertEqual(self.session.chapter_queue,[Piece(4)])

    def test_pending_navigation_unknown_and_input_nodes_do_not_guess_inputs(self):
        self.pending_next_chapter()
        for node in (None,'choice','tutorial','confirm'):
            with self.subTest(node=node):
                client=self.bridge(observation(node=node))
                _,_,info=self.session.cutscene('watch')
                self.assertEqual(client.steps,[])
                self.assertEqual(info['auto_frames'],0)
                self.assertEqual(self.session.chapter_queue,[Piece(4)])
                if node is None:self.assertIn('未知界面','；'.join(info['auto_notes']))

    def test_pending_navigation_watch_preserves_pause_stop_and_wait_budget(self):
        self.pending_next_chapter()
        client=self.bridge(observation(playing=True))
        self.session.control_guard=lambda:True
        _,_,info=self.session.cutscene('watch')
        self.assertEqual(client.steps,[])
        self.assertIn('用户已暂停','；'.join(info['auto_notes']))
        self.session.control_guard=None
        _,_,info=self.session.cutscene('watch')
        self.assertEqual(info['auto_frames'],AGENT_STORY_WAIT_FRAMES)
        self.assertTrue(all(segment==['30'] for segment in client.steps))
        self.session.goals.stop_now()
        with self.assertRaisesRegex(ActError,'本次试玩已结束'):self.session.cutscene('watch')

    def test_pending_navigation_observe_and_act_keep_current_story_playable(self):
        for entry in ('observe','act'):
            with self.subTest(entry=entry):
                self.pending_next_chapter()
                def after(client,inputs):
                    if inputs==['40,R']:
                        client.obs=observation(playing=True)
                    else:
                        client.obs.update(storyInput='dialogue',storyDecision='dialogue')
                client=self.bridge(observation(playing=True) if entry=='observe' else
                                   observation(controllable=True,cutscene=False),after)
                if entry=='observe':self.session.observe()
                else:self.session.act(['40,R'],'走到当前剧情节点',None)
                self.assertIn(['30'],client.steps)
                self.assertEqual(self.session.last_obs['storyInput'],'dialogue')
                self.assertTrue(self.session.chapter_finished)

    def test_auto_mode_still_stops_at_navigation_boundary(self):
        self.pending_next_chapter()
        self.session.cfg.cutscene_mode='auto'
        client=self.bridge(observation(playing=True))
        _,_,frames,_=self.session._auto_advance(client.obs,None)
        self.assertEqual(frames,0)
        self.assertEqual(client.steps,[])

    def test_initial_state_write_preserves_pending_entry_on_controller_restart(self):
        from orchestrator.run import write_run_state, parse_args
        from unittest.mock import patch
        import sys
        self.pending_next_chapter()
        self.session.cfg.resume_pending=True
        self.session.cfg.plan=[Piece(4)]
        self.session.cfg.chapters=[4]
        write_run_state(self.root,self.session.cfg,'same-session',None)
        state=json.loads((self.root/'run_state.json').read_text(encoding='utf-8'))
        self.assertTrue(state['piece_pending'])
        self.assertEqual(state['navigation_remaining'],[[4,0,None]])
        self.session.metrics.update(splits=[])
        argv=['run','--agent',self.session.cfg.agent,'--save','test','--resume',str(self.root),
              '--engine','claude','--runtime','host','--no-sim','--no-sim-assist']
        with patch.object(sys,'argv',argv):cfg,_=parse_args()
        self.assertTrue(cfg.resume_pending)
        self.assertEqual(cfg.plan,[Piece(4)])
        # Automatic startup already enters its target before session creation;
        # a consumed request flag must not make the next restart enter it again.
        cfg.cutscene_mode='auto'
        auto_root=self.root/'auto';auto_root.mkdir()
        write_run_state(auto_root,cfg,'same-session',None)
        auto_state=json.loads((auto_root/'run_state.json').read_text(encoding='utf-8'))
        self.assertFalse(auto_state['piece_pending'])

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
        text = '\n'.join(item.text for item in result.content if item.type == 'text')
        self.assertIn('未知界面', text)
        self.assertNotIn('显式推进一小段空输入', text)
        self.assertEqual(client.steps, [])

    def test_entry_handover_guides_explicit_steps_and_recognized_postcard(self):
        start = observation()
        start.update(scene='LevelEnter', storyPlaybackState={'state': 'unknown', 'source': 'unrecognized_story_scene'})

        def after(client, inputs):
            client.obs.update(storyInput='postcard', storyDecision='postcard')

        client = self.bridge(start, after)
        server = build_server(self.session)
        result = asyncio.run(server.call_tool('cutscene', {'action': 'watch'}))
        self.assertFalse(result.is_error)
        text = '\n'.join(item.text for item in result.content if item.type == 'text')
        self.assertIn('刚进入获准章节', text)
        self.assertIn('显式推进一小段空输入', text)
        self.assertEqual(client.steps, [])
        result = asyncio.run(server.call_tool('act', {'inputs': ['30'], 'thought': '推进已知入场过程',
            'stop_on': ['controllable', 'story_input', 'room']}))
        self.assertFalse(result.is_error)
        self.assertIn('明信片已显示完毕', '\n'.join(item.text for item in result.content if item.type == 'text'))
        self.assertEqual(client.steps, [['30']])

    def test_verified_level_enter_automatically_waits_to_postcard_without_confirmation(self):
        start = {'scene': 'LevelEnter', 'frame': 51945, 'save': {'slot': 15}, 'controllable': False,
                 'reload_boundary': {'session': {'sid': 'Celeste/3-CelestialResort', 'side': 'A', 'room': 'start'}}}

        def after(client, inputs):
            if client.frames == 60:
                client.obs.update(storyInput='postcard', storyDecision='postcard')

        client = self.bridge(start, after)
        server = build_server(self.session)
        self.session.cfg.save_slot = 14
        result = asyncio.run(server.call_tool('cutscene', {'action': 'watch'}))
        self.assertFalse(result.is_error)
        self.assertEqual(client.steps, [])
        self.session.cfg.save_slot = 15
        result = asyncio.run(server.call_tool('cutscene', {'action': 'watch'}))
        self.assertFalse(result.is_error)
        self.assertEqual(client.steps, [['30'], ['30']])
        self.assertEqual(self.session.last_obs['storyInput'], 'postcard')
        self.session.observe()
        self.assertEqual(client.frames, 60)

    def _death_fixture(self, *, wait=True, **kwargs):
        client = self.session.client = RespawnBridge(**kwargs)
        self.session.last_obs = copy.deepcopy(client.obs)
        self.session.control_guard = lambda: client.paused
        server = build_server(self.session)
        result = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': '进入尖刺'}))
        self.assertFalse(result.is_error)
        self.assertEqual(client.steps, [['1,R']])  # No forced animation wait after the death.
        if wait:
            asyncio.run(server.call_tool('observe', {}))
        return client, server

    def _stop_report(self, server, reason='角色没有恢复'):
        result = asyncio.run(server.call_tool('stop_run', {'reason': reason}))
        self.assertFalse(result.is_error)
        return json.loads(result.content[0].text.splitlines()[0])

    def _wire_bridge(self, bridge):
        with patch('tools.bridge_client.socket.create_connection', return_value=BridgeWire(bridge)):
            client = BridgeClient(port=1)
        client.after_step = self.session._after_step
        self.session.client = client
        self.session.last_obs = copy.deepcopy(bridge.obs)
        self.session.game_process_id = 778
        return build_server(self.session)

    def test_menu_retry_without_death_event_or_wipe_can_choose_waiting(self):
        bridge = RespawnBridge(ready=3067, death_event=False, screen_wipe=False)
        bridge.death_frame = 2937
        bridge.obs.update(frame=2934, controllable=False,
                          pauseMenu={'focused': True, 'selection': 1, 'items': []})
        bridge.obs['level']['paused'] = True
        server = self._wire_bridge(bridge)
        result = asyncio.run(server.call_tool('act', {'inputs': ['1,D', '1', '1,C'], 'thought': '选择重试'}))
        self.assertFalse(result.is_error)
        self.assertEqual(bridge.steps, [['1,D', '1', '1,C']])
        self.assertEqual(self.session.last_obs['frame'], 2937)
        self.assertIsNone(self.session.last_obs['player'])
        self.assertFalse(self.session.last_obs['level']['screenWipe'])
        self.assertEqual(self.session._transition_wait.evidence, 'death_counter')
        self.assertEqual(self.session._transition_wait.start_frame, 2937)
        report = self._stop_report(server)
        self.assertEqual(report['status'], 'recovery_available')
        self.assertFalse(report['paused'])
        self.assertEqual(len(bridge.steps), 1)
        result = asyncio.run(server.call_tool('cutscene', {'action': 'watch'}))
        self.assertFalse(result.is_error)
        self.assertTrue(self.session.last_obs['controllable'])
        self.assertEqual(bridge.obs['frame'], 3067)
        self.assertTrue(all(part.split(',')[0] == part for step in bridge.steps[1:] for part in step))
        self.assertIsNone(json.loads((self.root/'transition_wait.json').read_text())['wait'])

    def test_natural_death_confirmation_executes_before_any_wait_or_retry_program(self):
        bridge = RespawnBridge(confirm=True, screen_wipe=False)
        server = self._wire_bridge(bridge)
        self.session.container = 'fixture-only'
        plan = self.session.workspace/'plan.json'
        plan.write_text(json.dumps({'inputs': ['60,R'], 'retry': {'command': 'python planner.py', 'max': 2}}))
        with patch('orchestrator.game_session.run_program') as program:
            result = asyncio.run(server.call_tool('act', {'plan_file': 'plan.json', 'thought': '执行路线'}))
            self.assertFalse(result.is_error)
            program.assert_not_called()
        self.assertEqual(bridge.steps, [['60,R']])
        self.assertEqual(bridge.obs['frame'], bridge.death_frame)
        self.assertEqual(self.session.last_obs['level']['deaths'], 1)
        result = asyncio.run(server.call_tool('act', {
            'inputs': ['1,C'], 'thought': '确认缩短死亡动画', 'stop_on': ['controllable']}))
        self.assertFalse(result.is_error)
        self.assertEqual(bridge.steps, [['60,R'], ['1,C']])
        self.assertTrue(self.session.last_obs['controllable'])
        self.assertEqual(bridge.obs['frame'], bridge.death_frame + 1)

    def test_room_change_before_a_death_in_the_same_step_keeps_the_respawn_wait(self):
        before = observation(controllable=True, cutscene=False, deaths=2, room='10-z')
        self.session._transition_wait = self.session._transition_observed = None
        self.session.last_obs = copy.deepcopy(before)
        self.session._ingest_transition(before)
        after = copy.deepcopy(before)
        after.update(frame=130, controllable=False)
        after['player'].update(dead=True)
        after['level'].update(room='11-a', deaths=3)
        result = {'events': [{'type': 'room', 'frame': 110, 'detail': '11-a'},
                             {'type': 'death', 'frame': 125, 'detail': '11-a'}]}
        self.session._handle_events(result, after)
        wait = self.session._transition_wait
        self.assertIsNotNone(wait)                      # 更早的换房事件不清掉新房间里的死亡等待
        self.assertEqual((wait.kind, wait.start_frame), ('respawn', 125))
        self.session._handle_events({'events': [{'type': 'room', 'frame': 140, 'detail': '12'}]})
        self.assertIsNone(self.session._transition_wait)   # 死亡之后再换房照常清掉

    def test_missing_player_alone_and_counter_from_another_identity_do_not_authorize_wait(self):
        for changed in ('none', 'room', 'sid', 'slot', 'epoch', 'counter_reset'):
            with self.subTest(changed=changed):
                before = observation(controllable=True, cutscene=False, deaths=2)
                before['save'] = {'slot': 15}
                self.session._transition_wait = self.session._transition_observed = None
                self.session.last_obs = copy.deepcopy(before)
                self.session._ingest_transition(before)
                after = copy.deepcopy(before)
                after.update(frame=101, player=None, controllable=False)
                after['level']['deaths'] = 2 if changed == 'none' else 3
                if changed in ('room', 'sid'):
                    after['level'][changed] = 'other'
                elif changed == 'slot':
                    after['save']['slot'] = 16
                elif changed == 'epoch':
                    self.session.reload_epoch = getattr(self.session, 'reload_epoch', 0) + 1
                elif changed == 'counter_reset':
                    after['level']['deaths'] = 0
                client = StoryBridge(after)
                self.session.client = client
                self.session.observe()
                self.assertEqual(client.steps, [])
                self.assertIsNone(self.session._transition_wait)

    def test_respawn_restart_preserves_budget_and_stall_only_at_identical_frozen_state(self):
        bridge, server = self._death_fixture(wait=False, screen_wipe=False)
        self.session.game_process_id = 778
        # Record the new process baseline before establishing evidence again.
        self.session._transition_wait = self.session._transition_observed = None
        self.session._handle_events({'events': [{'type': 'death', 'frame': bridge.death_frame}]}, bridge.obs)
        bridge.obs['frame'] += 90
        self.session._write_workspace(copy.deepcopy(bridge.obs))
        saved = json.loads((self.root/'transition_wait.json').read_text())
        self.assertEqual(saved['wait']['start_frame'], bridge.death_frame)
        for variant in ('same', 'stalled', 'exhausted', 'generation', 'reload_pending', 'process', 'frame', 'identity', 'deaths'):
            with self.subTest(variant=variant):
                directory = self.root/variant
                directory.mkdir()
                metrics = Metrics(directory/'metrics.json', 'test', 'test', 'high', 600)
                metrics.state.resumed_from = str(self.root)
                restored = GameSession(copy.copy(self.session.cfg), directory, metrics)
                restored.game_process_id = 779 if variant == 'process' else 778
                obs = copy.deepcopy(bridge.obs)
                data = copy.deepcopy(saved)
                if variant == 'stalled':
                    data['wait']['stalled_at'] = obs['frame']
                elif variant == 'generation':
                    data['wait']['epoch'][1] = 2
                elif variant == 'reload_pending':
                    restored.reload_observation_required = True
                elif variant == 'exhausted':
                    data['wait']['start_frame'] = obs['frame'] - TRANSITION_WAIT_FRAMES
                elif variant == 'frame':
                    obs['frame'] += 1
                elif variant == 'identity':
                    obs['level']['room'] = 'other'
                elif variant == 'deaths':
                    obs['level']['deaths'] += 1
                (self.root/'transition_wait.json').write_text(json.dumps(data))
                try:
                    restored._restore_transition_wait(obs)
                    restored.last_obs = obs
                    if variant in ('same', 'stalled', 'exhausted', 'generation'):
                        wait = restored._transition_for(obs)
                        self.assertIsNotNone(wait)
                        self.assertEqual(wait.start_frame, data['wait']['start_frame'])
                        remaining = TRANSITION_WAIT_FRAMES - 90 if variant in ('same', 'generation') else 0
                        self.assertEqual(wait.progress(obs['frame'])['remaining_frames'], remaining)
                        if variant == 'generation':
                            self.assertEqual(restored.reload_epoch, 2)
                    else:
                        self.assertIsNone(restored._transition_for(obs))
                finally:
                    restored.close(release=False)

    def test_entry_restart_keeps_the_original_budget_and_reload_persists_invalidation(self):
        self.session.cfg.save_slot = 15
        self.session.game_process_id = 778
        start = {'scene': 'LevelEnter', 'frame': 500, 'save': {'slot': 15}, 'controllable': False,
                 'reload_boundary': {'session': {'sid': 'Celeste/3-CelestialResort', 'side': 'A', 'room': 'start'}}}
        self.session._write_workspace(start)
        later = copy.deepcopy(start)
        later['frame'] = 560
        self.session._write_workspace(later)
        directory = self.root/'entry-restart'
        directory.mkdir()
        metrics = Metrics(directory/'metrics.json', 'test', 'test', 'high', 600)
        metrics.state.resumed_from = str(self.root)
        restored = GameSession(copy.copy(self.session.cfg), directory, metrics)
        restored.game_process_id = 778
        try:
            restored._restore_transition_wait(later)
            self.assertEqual(restored._transition_wait.start_frame, 500)
            self.assertEqual(restored._transition_wait.progress(560)['advanced_frames'], 60)
            restored.after_bridge_reload(observation(controllable=True, cutscene=False))
            self.assertTrue(restored.reload_observation_required)
            self.assertIsNone(json.loads((directory/'transition_wait.json').read_text())['wait'])
        finally:
            restored.close(release=False)

    def test_new_death_during_story_auto_play_hands_back_before_respawn(self):
        def after(client, inputs):
            if client.frames == 1:
                client.obs['controllable'] = False
                client.obs['storyPlaybackState'] = {'state': 'playing', 'source': 'fixture'}
            else:
                client.obs['player'] = None
                client.obs['level'].update(deaths=1, inCutscene=False)
                client.obs['storyPlaybackState'] = {'state': 'unknown', 'source': 'level_blocked'}
        start = observation(controllable=True, cutscene=False)
        bridge = StoryBridge(start, after)
        self._wire_bridge(bridge)
        outcome = self.session.act(['1,R'], '经过剧情入口', None)
        self.assertEqual(bridge.steps, [['1,R'], ['30']])
        self.assertEqual(outcome.auto_frames, 30)
        self.assertIsNone(outcome.obs['player'])
        self.assertEqual(self.session._transition_wait.evidence, 'death_counter')

    def test_respawn_can_resume_bounded_wait_after_stop_report_without_extra_input(self):
        client, server = self._death_fixture(pause_at=97880)
        self.assertEqual(self.session.last_obs['frame'], 97880)
        self.assertEqual(client.steps, [['1,R'], ['30'], ['30'], ['30']])
        client.paused, client.pause_at = False, None
        report = self._stop_report(server)
        self.assertEqual(report['status'], 'recovery_available')
        self.assertEqual(report['transition']['advanced_frames'], 90)
        self.assertFalse(report['paused'])
        self.assertIsNone(self.session.agent_stop)
        self.assertEqual(client.obs['frame'], 97880)
        result = asyncio.run(server.call_tool('observe', {}))
        self.assertFalse(result.is_error)
        self.assertTrue(self.session.last_obs['controllable'])
        self.assertEqual(client.obs['frame'], 97921)
        self.assertEqual(client.steps[1:], [['30']] * 5)
        self.assertTrue(all({'controllable', 'story_input', 'room'} <= set(stops) for stops in client.stop_lists[1:]))

    def test_respawn_budget_includes_explicit_frames_and_duplicate_reports_do_not_pause(self):
        client, server = self._death_fixture(ready=None, pause_at=97880)
        client.paused, client.pause_at = False, None
        pauses = []
        self.session.request_pause = lambda reason: pauses.append(reason) or True
        result = asyncio.run(server.call_tool('act', {'inputs': ['60'], 'thought': '补充中性帧'}))
        self.assertFalse(result.is_error)
        self.assertIn(['60'], client.steps)
        self.assertEqual(client.obs['frame'], 97940)
        self.session.observe()  # Explicitly choose waiting; act did not add an automatic suffix.
        self.assertEqual(client.obs['frame'], client.death_frame + TRANSITION_WAIT_FRAMES)
        steps = client.steps.copy()
        self.session.observe()
        self.assertEqual(client.steps, steps)
        self.assertEqual(self._stop_report(server)['status'], 'reported')
        self.assertEqual(self._stop_report(server, '同一帧仍然无角色')['status'], 'duplicate')
        self.assertEqual(len(pauses), 1)
        self.assertEqual(len(self.session.agent_stop['reports']), 1)
        path = self.root / 'stop_run_diagnostics.jsonl'
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['status'], 'duplicate')

    def test_transport_failure_allows_stop_report_despite_cached_respawn_evidence(self):
        _, server = self._death_fixture(pause_at=97880)
        self.session.control_guard = None
        client = BridgeClient.__new__(BridgeClient)
        client.next_id = 8
        replies = iter(({}, ConnectionError('桥接已断开'), {'obs': self.session.last_obs}))

        def broken(*args, **kwargs):
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply

        client._call = broken
        client.call('step', inputs=['30'])
        with self.assertRaises(ConnectionError):
            client.call('observe')
        client.call('observe')
        self.session.client = client
        try:
            self.assertEqual(self._stop_report(server, '桥接没有返回可核验的推进结果')['status'], 'reported')
        finally:
            self.session.client = None

    def test_respawn_wait_stops_on_zero_progress_and_preserves_report_path(self):
        client, server = self._death_fixture(pause_at=97880)
        client.paused, client.pause_at = False, None
        original = client.call

        def stalled(command, **args):
            if command != 'step':
                return original(command, **args)
            client.steps.append(args['inputs'])
            return {'obs': copy.deepcopy(client.obs), 'result': {'consumed': 0, 'events': [], 'stop': 'done'}}

        client.call = stalled
        count = len(client.steps)
        self.session.observe()
        self.assertEqual(len(client.steps), count + 1)
        self.session.observe()
        self.assertEqual(len(client.steps), count + 1)
        self.assertEqual(self._stop_report(server)['status'], 'reported')

    def test_respawn_observe_does_not_add_frames_to_tas(self):
        client, _ = self._death_fixture(pause_at=97880)
        client.paused, client.pause_at = False, None
        client.obs['input_format'] = 'celeste_tas'
        steps = client.steps.copy()
        obs, _ = self.session.observe()
        self.assertEqual(obs['frame'], 97880)
        self.assertEqual(client.steps, steps)

    def test_death_wipe_handover_exposes_next_explicit_step_without_auto_input(self):
        start = observation(cutscene=False, screenWipe=True)
        start['player'] = None
        client = self.bridge(start)
        result = asyncio.run(build_server(self.session).call_tool('act', {
            'inputs': ['60'], 'thought': '等待重生', 'stop_on': ['controllable', 'story_input', 'room']}))
        self.assertFalse(result.is_error)
        text = '\n'.join(item.text for item in result.content if item.type == 'text')
        self.assertIn('刚死亡、换房且仍处于过渡', text)
        self.assertIn('核对新帧与状态', text)
        self.assertEqual(client.steps, [['60']])
        obs, _ = self.session.observe()
        self.assertEqual(obs['frame'], 160)
        self.assertIn('显式推进一小段空输入', '\n'.join(self.session.observe_notes))
        self.assertEqual(client.steps, [['60']])

    def test_final_chapter_recording_boundary_settles_at_verified_panel_without_extra_step(self):
        self.session.cfg.save_slot = 15
        sid = 'Celeste/3-CelestialResort'
        start = observation(controllable=True, cutscene=False)
        start['save'] = {'slot': 15}
        panel = {'scene': 'Overworld', 'frame': 134, 'saving': False, 'save': {'slot': 15},
                 'menu': {'current': 'OuiChapterPanel', 'next': None, 'panelSelectingMode': True,
                          'panelArea': 3, 'panelSide': 'Normal'}}

        class EndingBridge(StoryBridge):
            def call(bridge, command, **args):
                if command != 'step':
                    return super().call(command, **args)
                bridge.steps.append(args['inputs'])
                if len(bridge.steps) == 1:
                    bridge.obs = {'scene': 'AreaComplete', 'frame': 101, 'save': {'slot': 15},
                                  'completion': {'sid': sid, 'side': 'A'}}
                    used, reason, event = 1, 'complete', 'complete'
                else:
                    if len(bridge.steps) != 2:
                        raise AssertionError('Unexpected input after recording boundary')
                    bridge.obs = panel
                    used, reason, event = 33, 'recording_boundary', 'recording_boundary'
                planned = sum(int(part.split(',')[0]) for part in args['inputs'])
                return {'obs': copy.deepcopy(bridge.obs), 'result': {'consumed': used, 'planned': planned, 'stop': reason,
                    'trajectory': [], 'events': [{'frame': bridge.obs['frame'], 'type': event}]}}

        client = self.session.client = EndingBridge(start)
        self.session.last_obs = start
        server = build_server(self.session)
        self.session.observe()
        result = asyncio.run(server.call_tool('act', {'inputs': ['1,R'], 'thought': '走到章末'}))
        self.assertFalse(result.is_error)
        self.assertTrue(self.session._pending_complete)
        self.assertFalse(self.session.done)
        result = asyncio.run(server.call_tool('act', {'inputs': ['1,C', '180'], 'thought': '确认结算'}))
        self.assertFalse(result.is_error)
        text = '\n'.join(item.text for item in result.content if item.type == 'text')
        self.assertIn('执行了 33/181 帧', text)
        self.assertIn('本次试玩已结束', text)
        self.assertEqual(client.steps, [['1,R'], ['1,C', '180']])
        self.assertFalse(self.session._pending_complete)
        self.assertEqual(self.session.done_reason, '章节完成')
        self.assertEqual([split['chapter'] for split in self.session.metrics.state.splits], [sid])
        self.session.finish_at_results()
        self.assertEqual(len(client.steps), 2)

    def test_pending_panel_preserves_navigation_when_identity_or_finality_is_unconfirmed(self):
        self.session.cfg.save_slot = 15
        self.session._handle_events({'events': [{'type': 'complete'}]})
        panel = {'scene': 'Overworld', 'frame': 134, 'saving': False, 'save': {'slot': 15},
                 'menu': {'current': 'OuiChapterPanel', 'next': None, 'panelSelectingMode': True,
                          'panelArea': 3, 'panelSide': 'Normal'}}
        variants = (
            ('保存未完成', {'saving': True}, [], '仍在保存'),
            ('不同存档', {'save': {'slot': 14}}, [], '短按 B'),
            ('不同面', {'menu': {**panel['menu'], 'panelSide': 'BSide'}}, [], '短按 B'),
            ('菜单还在切换', {'menu': {**panel['menu'], 'next': 'OuiChapterSelect'}}, [], None),
            ('后续章节', {}, [Piece(4)], '短按 B'),
        )
        for name, change, queue, hint in variants:
            with self.subTest(name=name):
                self.session.chapter_queue = queue
                client = self.bridge({**panel, **change})
                self.session.observe()
                self.assertFalse(self.session.done)
                self.assertTrue(self.session._pending_complete)
                self.assertFalse(self.session.metrics.state.splits)
                self.assertEqual(client.steps, [])
                if hint:
                    self.assertIn(hint, '\n'.join(self.session.observe_notes))

        self.session.chapter_queue = []
        client = self.bridge(panel)
        result = asyncio.run(build_server(self.session).call_tool('observe', {}))
        self.assertFalse(result.is_error)
        self.assertIn('本次试玩已结束', '\n'.join(item.text for item in result.content if item.type == 'text'))
        self.assertEqual(client.steps, [])
        self.assertEqual(self.session.done_reason, '章节完成')

    def test_goal_stop_can_settle_pending_final_panel_from_latest_observation(self):
        self.session.cfg.save_slot = 15
        state_path = self.root/'run_state.json'
        state_path.write_text('{}', encoding='utf-8')
        self.session._handle_events({'events': [{'type': 'complete'}]})
        client = self.bridge({'scene': 'Overworld', 'saving': False, 'save': {'slot': 15},
            'menu': {'current': 'OuiChapterPanel', 'next': None, 'panelSelectingMode': True,
                     'panelArea': 3, 'panelSide': 'Normal'}})
        self.session.finish_user_stop()
        self.session.finish_at_results()
        self.assertEqual(self.session.done_reason, '章节完成')
        self.assertEqual(self.session.end_scene, 'Overworld')
        self.assertFalse(json.loads(state_path.read_text(encoding='utf-8'))['completion_pending'])
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
        # 剧情里游戏这一刻不让暂停（canPause 为假）：不算跳过尝试，每 10 帧看一次能不能暂停跳过
        self.assertEqual(client.steps, [['10']])
        self.assertEqual(count, 10)


if __name__ == '__main__':
    unittest.main()
