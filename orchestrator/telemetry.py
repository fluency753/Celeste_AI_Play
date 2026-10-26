"""Portable public HUD events. Protocol traces remain runtime-specific."""
import json
import time


def publish(directory, ident, text, *, role='assistant', kind='解说'):
    event = {'id': str(ident), 'text': text, 'role': role, 'kind': kind, 'wall': time.time()}
    with (directory / 'public_events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def public_event(value):
    if isinstance(value, str) and value.startswith('data:image/'):
        return '<image omitted>'
    if isinstance(value, dict):
        if str(value.get('type', '')).lower() in {'reasoning', 'thinking', 'redacted_thinking'}:
            return {k: value[k] for k in ('type', 'id') if k in value}
        return {k: ('<image omitted>' if k == 'imageUrl' and str(v).startswith('data:') else public_event(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [public_event(v) for v in value]
    return value
