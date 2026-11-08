"""Durable token-to-frame credit; game input remains owned by GameSession."""
import json
import time

from .output_usage import token_count
from .session_store import load_json, write_json


class TokenDrift:
    def __init__(self, path, metrics, tokens_per_frame, previous=None):
        self.path, self.metrics, self.tokens_per_frame = path, metrics, tokens_per_frame
        self.state = {'schema': 1, 'source': None, 'last_total': None, 'remainder_tokens': 0,
                      'pending_frames': 0, 'eligible_tokens': 0, 'excluded_tokens': 0,
                      'consumed_frames': 0, 'dropped_frames': 0, 'batch_id': 0, 'batch': None, 'epoch': None,
                      'excluded_gap': False}
        source = path if path.exists() else previous
        if source is not None and source.exists():
            saved = load_json(source)
            if saved.get('schema') != 1:
                raise ValueError('Token 推进账本格式无效，未发送游戏输入')
            self.state.update(saved)
            for key in ('remainder_tokens', 'pending_frames', 'batch_id', 'consumed_frames', 'dropped_frames'):
                if token_count(self.state.get(key)) is None:
                    raise ValueError('Token 推进账本计数无效，未发送游戏输入')
            # A resumed owner never replays previously queued movement.
            self.state['dropped_frames'] += self.state['pending_frames']
            self.state['pending_frames'] = 0
            self.state['excluded_gap'] = True
            if (self.state.get('batch') or {}).get('status') == 'pending':
                self.state['batch']['status'] = 'unknown'
        self.baseline = metrics.state.output_tokens
        self._save()

    @property
    def unknown(self):
        return (self.state.get('batch') or {}).get('status') == 'unknown'

    @property
    def pending(self):
        return self.state['pending_frames']

    def _save(self):
        self.state['tokens_per_frame'] = self.tokens_per_frame
        write_json(self.path, self.state)
        self.metrics.update(token_drift={key: self.state.get(key) for key in (
            'tokens_per_frame', 'remainder_tokens', 'pending_frames', 'eligible_tokens',
            'excluded_tokens', 'consumed_frames', 'dropped_frames', 'batch')})

    def event(self, kind, **data):
        with self.path.with_name('token_advances.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'event': kind, 'wall': time.time(), **data}, ensure_ascii=False) + '\n')

    def receive(self, total, latest, source, eligible, epoch):
        if token_count(total) is None:
            return
        same_source = self.state['source'] == source
        previous = self.state['last_total'] if same_source else (
            max(0, total - latest) if token_count(latest) is not None else total)
        if previous is None:
            previous = total
        if not same_source and self.state['source'] is None:
            previous = max(previous, self.baseline)
        delta = max(0, total - previous)
        if token_count(latest) is not None:
            delta = min(delta, latest)  # Never charge an unobserved historical gap.
        self.state['last_total'] = max(previous, total)
        self.state['source'] = source
        if not delta:
            self._save()
            return
        allowed_now = bool(eligible and not self.unknown)
        eligible = bool(allowed_now and not self.state['excluded_gap'])
        # Samples inside an excluded phase cannot prove where its ending lies.
        # Only the first positive sample after returning to eligible closes it.
        self.state['excluded_gap'] = not allowed_now
        if not eligible:
            self.state['excluded_tokens'] += delta
            self.discard('usage_excluded', save=False)
        else:
            if self.state['epoch'] is not None and self.state['epoch'] != list(epoch):
                self.discard('game_epoch_changed', save=False)
            self.state['epoch'] = list(epoch)
            self.state['eligible_tokens'] += delta
            frames, remainder = divmod(self.state['remainder_tokens'] + delta, self.tokens_per_frame)
            self.state['pending_frames'] += frames
            self.state['remainder_tokens'] = remainder
        self.event('usage', source=source, output_delta=delta, eligible=bool(eligible),
                   pending_frames=self.pending, remainder_tokens=self.state['remainder_tokens'])
        self._save()

    def exclude_window(self, reason):
        # Without an intermediate usage sample, the next positive delta may
        # straddle excluded time. Drop it rather than charging an unknown span.
        self.state['excluded_gap'] = True
        self.discard(reason)

    def discard(self, reason, *, save=True):
        if self.pending:
            self.event('discarded', reason=reason, frames=self.pending)
            self.state['dropped_frames'] += self.pending
            self.state['pending_frames'] = 0
        if save:
            self._save()

    def begin(self, epoch, before):
        if self.unknown or not self.pending:
            return None
        if self.state['epoch'] != list(epoch):
            self.discard('game_epoch_changed')
            return None
        # No artificial pacing. The bound is only a pause/cancel acknowledgement
        # boundary, and batches are sent consecutively while credit remains.
        frames = min(self.pending, 60)
        self.state['pending_frames'] -= frames
        self.state['batch_id'] += 1
        batch = {'id': self.state['batch_id'], 'status': 'pending', 'requested': frames, 'before': before}
        self.state['batch'] = batch
        self._save()  # Must succeed before any input can be submitted.
        self.event('requested', batch=batch)
        return frames

    def commit(self, result):
        batch = self.state['batch']
        consumed = result.get('consumed')
        if token_count(consumed) is None or consumed > batch['requested']:
            raise ValueError('Token 推进返回的实际帧数无效，执行范围未确认')
        batch.update(status='committed', **result)
        self.state['consumed_frames'] += consumed
        self.state['dropped_frames'] += batch['requested'] - consumed
        if result.get('stop') != 'done':
            self.state['excluded_gap'] = True
            self.discard('step_' + str(result.get('stop')), save=False)
        self._save()
        self.event('committed', batch=batch)

    def fail(self, error):
        self.state['batch'].update(status='unknown', error=str(error)[:1000])
        self.discard('unknown_result', save=False)
        self._save()
        self.event('unknown', batch=self.state['batch'])
