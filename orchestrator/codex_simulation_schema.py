"""Update a rollout's simulator schema without moving any history bytes."""
from __future__ import annotations

import json


DESCRIPTION = ('Predictions require an explicit request_id; reuse the same ID to retrieve results. '
               'Omit candidates for balance.')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate metadata key: {key}')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f'Invalid JSON constant: {value}')


def patch_metadata(line: bytes, session_id: str, request_id_schema: dict) -> bytes:
    """Return a same-length first line, or reject incompatible/truncated metadata.

    The caller owns file locking and publication. This function never reads files
    or changes existing arguments, required fields, or any other tool.
    """
    if not isinstance(line, bytes) or not isinstance(session_id, str) or not session_id:
        raise ValueError('Expected metadata bytes and a nonempty session ID')
    if not line.endswith(b'\n'):
        raise ValueError('Metadata must be a complete newline-terminated first line')
    ending = b'\r\n' if line.endswith(b'\r\n') else b'\n'
    body = line[:-len(ending)]
    if b'\n' in body or b'\r' in body:
        raise ValueError('Expected exactly one metadata line')
    try:
        metadata = json.loads(body.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
        json.dumps(metadata, allow_nan=False)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('Invalid UTF-8 session metadata JSON') from error
    if not isinstance(metadata, dict) or metadata.get('type') != 'session_meta':
        raise ValueError('Expected session_meta')
    payload = metadata.get('payload')
    if not isinstance(payload, dict) or payload.get('id') != session_id:
        raise ValueError('Session metadata ID does not match the selected session')
    tools = payload.get('dynamic_tools')
    if (not isinstance(tools, list)
            or any(not isinstance(tool, dict) or not isinstance(tool.get('name'), str) for tool in tools)):
        raise ValueError('Invalid dynamic_tools metadata')
    targets = [tool for tool in tools if tool['name'] == 'celeste_simulate']
    if len(targets) != 1:
        raise ValueError('Expected exactly one celeste_simulate tool')
    tool = targets[0]
    schema = tool.get('inputSchema')
    if (not isinstance(tool.get('description'), str) or not isinstance(schema, dict)
            or schema.get('type') != 'object' or not isinstance(schema.get('properties'), dict)):
        raise ValueError('Invalid celeste_simulate inputSchema')
    required = schema.get('required', [])
    if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
            or 'request_id' in required):
        raise ValueError('Simulator balance queries must not require request_id')
    if not isinstance(request_id_schema, dict) or not request_id_schema:
        raise ValueError('Expected a request_id schema object')
    try:
        current_schema_json = json.dumps(request_id_schema, ensure_ascii=False, allow_nan=False, sort_keys=True)
        current_schema = json.loads(current_schema_json)
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid request_id schema JSON') from error
    properties = schema['properties']
    if 'request_id' in properties:
        existing = json.dumps(properties['request_id'], ensure_ascii=False, allow_nan=False, sort_keys=True)
        if existing != current_schema_json:
            raise ValueError('Existing request_id schema conflicts with the current tool')
        return line
    properties['request_id'] = current_schema
    tool['description'] = DESCRIPTION
    encoded = json.dumps(metadata, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(encoded) > len(body):
        raise ValueError('Updated metadata does not fit the original first-line byte length')
    return encoded + b' ' * (len(body) - len(encoded)) + ending
