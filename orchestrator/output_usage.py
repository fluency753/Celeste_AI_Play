"""Claude response-level usage accounting; never consumes streamed content bodies."""
from collections import OrderedDict
from pathlib import Path

from .session_store import load_json, write_json


def token_count(value):
    return value if type(value) is int and value >= 0 else None


def usage_fields(value):
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
              'cache_creation_input_tokens') if token_count(value.get(key)) is not None}
    details = value.get('output_tokens_details')
    if isinstance(details, dict) and token_count(details.get('thinking_tokens')) is not None:
        result['output_tokens_details'] = {'thinking_tokens': details['thinking_tokens']}
    return result


def partial_usage_event(event):
    """A strict allowlist applied before partial events are written to disk."""
    if event.get('type') != 'stream_event' or event.get('parent_tool_use_id'):
        return None
    raw = event.get('event') or {}
    kind = raw.get('type')
    if kind not in ('message_start', 'message_delta', 'message_stop'):
        return None
    body = {'type': kind}
    if kind == 'message_start':
        message = raw.get('message') or {}
        body['message'] = {key: message[key] for key in ('id', 'model') if isinstance(message.get(key), str)}
        body['message']['usage'] = usage_fields(message.get('usage'))
    elif kind == 'message_delta':
        body['usage'] = usage_fields(raw.get('usage'))
    return {'type': 'stream_event', 'event': body}


class ClaudeUsage:
    def __init__(self, metrics, path, session_id, on_output):
        self.metrics, self.path, self.session_id, self.on_output = metrics, path, session_id, on_output
        self.output, self.reasoning = metrics.state.output_tokens, metrics.state.thinking_tokens
        self.responses, self.results = OrderedDict(), []
        try:
            previous = getattr(metrics.state, 'resumed_from', None)
            source = path if path.exists() else Path(previous) / path.name if previous else path
            saved = load_json(source)
        except (OSError, ValueError):
            saved = {}
        if saved.get('session_id') == session_id:
            self.output = max(self.output, token_count(saved.get('output_tokens')) or 0)
            self.reasoning = max(self.reasoning, token_count(saved.get('thinking_tokens')) or 0)
            self.responses.update(saved.get('responses') or {})
            self.results = saved.get('results') or []
        turn = saved.get('turn') if saved.get('session_id') == session_id else None
        self.current = None
        self.turn_output = token_count((turn or {}).get('output')) or 0
        self.turn_reasoning = token_count((turn or {}).get('reasoning')) or 0
        self.finalized = bool((turn or {}).get('finalized', False))
        self.turn_responses = (turn or {}).get('responses') or {}

    def begin_turn(self):
        self.current = None
        # A newly sent user message starts a new CLI result scope. Replayed
        # response IDs still cover that result, without adding to global usage.
        self.turn_output, self.turn_reasoning, self.finalized = 0, 0, False
        self.turn_responses = {}

    def _add(self, output, reasoning):
        self.output += output
        self.reasoning += reasoning
        while len(self.responses) > 512:
            self.responses.popitem(last=False)
        self.results = self.results[-128:]
        write_json(self.path, {'schema': 1, 'session_id': self.session_id,
                   'output_tokens': self.output, 'thinking_tokens': self.reasoning,
                   'responses': self.responses, 'results': self.results,
                   'turn': {'output': self.turn_output, 'reasoning': self.turn_reasoning,
                            'finalized': self.finalized, 'responses': self.turn_responses}})
        self.metrics.update(output_tokens=self.output, thinking_tokens=self.reasoning)
        # Accounting is authoritative. If movement persistence fails afterwards,
        # recovery never compensates missed movement from historical totals.
        self.on_output(self.output, output)

    def partial(self, event):
        value = partial_usage_event(event)
        if value is None:
            return
        raw = value['event']
        if raw['type'] == 'message_start':
            message = raw['message']
            self.current = message.get('id') if message.get('model') != '<synthetic>' else None
            return  # message_start.output_tokens is a placeholder, not generation.
        if raw['type'] != 'message_delta' or not self.current:
            return
        usage = raw['usage']
        output = token_count(usage.get('output_tokens'))
        reasoning = token_count((usage.get('output_tokens_details') or {}).get('thinking_tokens'))
        previous = self.responses.get(self.current, [0, 0])
        if len(previous) > 2 and previous[2]:
            return  # Result reconciliation already closed this historical response.
        latest = [max(previous[0], output or 0), max(previous[1], reasoning or 0)]
        self.responses[self.current] = latest
        covered = self.turn_responses.get(self.current, [0, 0])
        self.turn_output += latest[0] - covered[0]
        self.turn_reasoning += latest[1] - covered[1]
        self.turn_responses[self.current] = latest
        self._add(latest[0] - previous[0], latest[1] - previous[1])

    def result(self, event):
        ident = event.get('uuid')
        if self.finalized or ident and ident in self.results:
            return
        usage = usage_fields(event.get('usage'))
        output = token_count(usage.get('output_tokens'))
        reasoning = token_count((usage.get('output_tokens_details') or {}).get('thinking_tokens'))
        if ident:
            self.results.append(ident)
        self.finalized = True
        delta = max(0, output - self.turn_output) if output is not None else 0
        reasoning_delta = max(0, reasoning - self.turn_reasoning) if reasoning is not None else 0
        self.turn_output += delta
        self.turn_reasoning += reasoning_delta
        if output is not None:
            for response in self.turn_responses:
                if response in self.responses:
                    self.responses[response] = [*self.responses[response][:2], True]
        self._add(delta, reasoning_delta)
