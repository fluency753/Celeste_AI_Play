"""Session-owned pause context; token debt itself belongs to TokenDrift."""
from pathlib import Path
import math
import json

from .session_store import load_json, write_json


def paused_menu(obs):
    return obs.get('scene') == 'Level' and bool(obs.get('pauseMenu') or (obs.get('level') or {}).get('paused'))


class TokenMenuBoundary(RuntimeError):
    """An acknowledged recovery precedes a helper's still-unsent next step."""


class TokenMenu:
    def __init__(self, session):
        self.session = session
        self.path = session.run_dir / 'token_menu.json'
        self.state = None
        self.latest = None

    def identity(self, obs):
        level = obs.get('level') or {}
        return [getattr(self.session, 'game_process_id', None), (obs.get('save') or {}).get('slot', self.session.cfg.save_slot),
                level.get('sid'), level.get('side', 'A')]

    @staticmethod
    def validate(state):
        from .game_session import PLAY_LETTERS, TAS_HELD_LETTERS, expand_inputs
        from .workspace_files import tas
        if not isinstance(state, dict) or state.get('stage') not in {'menu', 'recovery', 'ready', 'navigation'}:
            raise ValueError('暂停上下文阶段无效')
        if type(state.get('frame')) is not int or state['frame'] < 0:
            raise ValueError('暂停上下文帧号无效')
        if not isinstance(state.get('identity'), list) or len(state['identity']) != 4:
            raise ValueError('暂停上下文身份无效')
        pid, slot, sid, side = state['identity']
        if (type(pid) is not int or pid <= 0 or (slot is not None and (type(slot) is not int or slot < 0))
                or not isinstance(sid, str) or not sid or side not in {'A', 'B', 'C'}):
            raise ValueError('暂停上下文游戏或存档身份无效')
        keys, fmt = state.get('keys'), state.get('input_format')
        if not isinstance(keys, str) or len(keys) > 128 or fmt not in {'celeste_ai', 'celeste_tas'}:
            raise ValueError('暂停前按键或输入格式无效')
        if type(state.get('navigation_seen', False)) is not bool:
            raise ValueError('暂停上下文导航凭据无效')
        if fmt == 'celeste_ai':
            expand_inputs(['1,' + keys if keys else '1'], 1, PLAY_LETTERS)
            if any(token.upper().startswith('A:') and not math.isfinite(float(token[2:])) for token in keys.split(',')):
                raise ValueError('暂停前模拟方向不是有限角度')
        else:
            if any(not token or not set(token) <= TAS_HELD_LETTERS for token in keys.split(',') if keys):
                raise ValueError('暂停前快照包含菜单按键')
            tas.expand(['1,' + keys if keys else '1'], 1)

    def restore(self, obs):
        previous = self.session.metrics.state.resumed_from
        source = self.path if self.path.exists() else Path(previous) / self.path.name if previous else None
        if source is not None and source.exists():
            saved = load_json(source)
            self.state = saved.get('context')
            if self.state:
                try:
                    if saved.get('schema') != 1:
                        raise ValueError('暂停上下文版本无效')
                    self.validate(self.state)
                except (ValueError, TypeError) as error:
                    self.state = dict(self.state) if isinstance(self.state, dict) else {}
                    self.state['invalid'] = str(error)
            if self.state and source != self.path:
                summary = load_json(source.parent / 'summary.json')
                if summary.get('game_released') is not False:
                    self.state['invalid'] = '上次已交还游戏或缺少冻结交接凭据'
            if self.state and (self.state.get('current_identity', self.state.get('identity')) != self.identity(obs)
                               or self.state.get('frame') != obs.get('frame')):
                self.state['invalid'] = '暂停前输入的游戏身份或帧号与当前状态不符'
            self.save()
        elif previous and paused_menu(obs):
            self.state = self.legacy_context(Path(previous), obs)
            if self.state is not None:
                self.save()
        self.latest = obs

    def legacy_context(self, previous, obs):
        """Upgrade an already-open 1.23.0 menu only from matching frozen evidence."""
        if (load_json(previous / 'summary.json').get('game_released') is not False
                or load_json(previous / 'held_input.json').get('frame') != obs.get('frame')):
            return None
        epoch = load_json(previous / 'token_drift.json').get('epoch')
        source = load_json(previous / 'run_state.json')
        identity = self.identity(obs)
        if (not isinstance(epoch, list) or len(epoch) != 5 or epoch[0] != identity[0]
                or epoch[3] != identity[2] or epoch[4] != (obs.get('level') or {}).get('room')
                or source.get('save_slot') != identity[1]):
            return None
        def rows(name):
            path = previous / name
            if not path.exists():
                return []
            with path.open('rb') as stream:
                offset = max(0, path.stat().st_size - 2 * 1024 * 1024)
                stream.seek(offset)
                if offset:
                    stream.readline()
                lines = stream.read().splitlines()
            result = []
            for line in reversed(lines):
                try:
                    result.append(json.loads(line))
                except (ValueError, UnicodeError):
                    continue
            return result
        for decision in rows('decisions.jsonl'):
            if not any('游戏暂停菜单已打开' in str(note) for note in decision.get('auto_notes') or []):
                if any(e.get('type') in {'controllable', 'room', 'death', 'complete'} for e in decision.get('events') or []):
                    return None
                continue
            frames = [e.get('frame') for e in decision.get('events') or []
                      if e.get('type') == 'uncontrollable' and type(e.get('frame')) is int]
            frame = max(frames) if frames else None
            if type(frame) is not int or frame > obs.get('frame', -1):
                return None
            if any(e.get('type') in {'controllable', 'room', 'death', 'complete'} and
                   type(e.get('frame')) is int and e['frame'] > frame for e in decision.get('events') or []):
                return None
            row = next((r for r in rows('frames.jsonl') if r.get('f') == frame - 1), None)
            if row is None:
                return None
            fmt = row.get('input_format', 'celeste_ai')
            held = self.session._input_held({'inputs': ['1,' + row.get('keys', '')], 'input_format': fmt}, 1, obs)
            state = {'identity': self.identity(obs), 'keys': held[0], 'input_format': held[1],
                     'frame': obs['frame'], 'stage': 'menu', 'held_frame': frame - 1,
                     'source': 'legacy_pause_decision_and_frame_log'}
            self.validate(state)
            return state
        return None

    def save(self):
        write_json(self.path, {'schema': 1, 'context': self.state})

    def note(self, obs, held):
        """Called for acknowledged steps only; held is the preceding real frame."""
        boundary = False
        preceding = self.latest or self.session.last_obs or {}
        if self.state is None and paused_menu(obs) and not paused_menu(preceding):
            self.state = {'identity': self.identity(obs), 'keys': held[0], 'input_format': held[1],
                          'frame': obs.get('frame'), 'stage': 'menu'}
            self.session.token_motion_epoch += 1
            boundary = True
        if self.state is not None:
            previous = self.state['stage']
            stage = ('navigation' if obs.get('scene') != 'Level' else 'menu' if paused_menu(obs)
                     else 'ready' if self.session.token_playable(obs) else 'recovery')
            self.state.update(stage=stage, frame=obs.get('frame'), current_identity=self.identity(obs))
            if obs.get('scene') == 'Overworld':
                self.state['navigation_seen'] = True
            if stage == 'ready' and previous != 'ready':
                self.session.token_motion_epoch += 1
                boundary = True
            self.save()
        self.latest = obs
        if boundary:
            wake = getattr(self.session, 'token_boundary_wake', None)
            if wake is not None:
                wake()

    def held(self, obs):
        if self.state:
            self.validate(self.state)
        current = self.identity(obs)
        matches = bool(self.state and (self.state['identity'] == current or
                       self.state.get('navigation_seen') and self.state['identity'][:2] == current[:2]))
        if not matches or self.state.get('invalid'):
            raise ValueError('暂停待补推进的输入来源未确认：' + (self.state or {}).get('invalid', '游戏或存档身份与暂停时不同'))
        if self.state.get('navigation_seen'):
            return '', self.state['input_format']   # Left the level: the old level's keys no longer apply.
        return self.state['keys'], self.state['input_format']

    def clear(self):
        self.state = None
        self.save()
