"""Pure fixed-length rollout migration; all fixtures and history are in memory."""
import copy
import json
import unittest

from orchestrator.codex_simulation_schema import patch_metadata


REQUEST_ID = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,80}$',
              'description': 'Explicit request ID; reuse it for retries.'}
SESSION = 'aaaaaaaa-1234-5678-90ab-cccccccccccc'


def metadata():
    return {'timestamp': '2026-11-03T00:00:00.000Z', 'type': 'session_meta', 'payload': {
        'id': SESSION, 'cwd': '/workspace', 'originator': 'codex_exec',
        'cli_version': '0.165.0-alpha.1.2', 'instructions': '中文与🎮的会话指令',
        'dynamic_tools': [
            {'name': 'celeste_act', 'description': '执行动作', 'inputSchema': {
                'type': 'object', 'properties': {'inputs': {'type': 'string'}}, 'required': ['inputs']}},
            {'name': 'celeste_simulate', 'description': '旧模拟器说明，保留旧候选格式的语义。' * 24,
             'inputSchema': {'type': 'object', 'properties': {
                 'candidates': {'type': 'array', 'items': {'type': 'string'}},
                 'input_format': {'type': 'string', 'enum': ['compact', 'tas']}},
                 'required': [], 'additionalProperties': False}},
            {'name': 'celeste_show', 'description': '观察', 'inputSchema': {
                'type': 'object', 'properties': {}, 'additionalProperties': False}}],
        'extra_future_metadata': {'preserve': ['原样结构', {'enabled': True}]}}}


def encode(value, ending=b'\n', padding=0):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b' ' * padding + ending


class SimulationSchemaTests(unittest.TestCase):
    def test_utf8_and_history_offsets_are_preserved_for_lf_and_crlf(self):
        for ending in (b'\n', b'\r\n'):
            with self.subTest(ending=ending):
                before = metadata()
                line = encode(before, ending, padding=13)
                history_lines = [b'{"type":"response_item","payload":{"text":"history"}}' + ending,
                                 '{"type":"event_msg","payload":{"text":"后续🎮"}}'.encode('utf-8') + ending]
                original = line + b''.join(history_lines)
                patched = patch_metadata(line, SESSION, REQUEST_ID)
                migrated = patched + b''.join(history_lines)
                self.assertEqual(len(patched), len(line))
                self.assertTrue(patched.endswith(ending))
                self.assertEqual(migrated[len(line):], original[len(line):])
                offset = len(line)
                for history in history_lines:
                    self.assertEqual(migrated[offset:offset + len(history)], history)
                    offset += len(history)
                after = json.loads(patched)
                target = after['payload']['dynamic_tools'][1]
                self.assertEqual(target['inputSchema']['properties']['request_id'], REQUEST_ID)
                self.assertNotIn('request_file', target['inputSchema']['properties'])
                self.assertNotIn('request_id', target['inputSchema']['required'])
                self.assertIn('explicit', target['description'])
                self.assertIn('reuse the same ID', target['description'])
                target['inputSchema']['properties'].pop('request_id')
                target['description'] = before['payload']['dynamic_tools'][1]['description']
                self.assertEqual(after, before)  # Includes every non-target tool and metadata field.

    def test_repeated_migration_returns_the_exact_same_bytes(self):
        line = encode(metadata(), b'\r\n')
        patched = patch_metadata(line, SESSION, REQUEST_ID)
        self.assertEqual(patch_metadata(patched, SESSION, REQUEST_ID), patched)

    def test_real_288_byte_old_description_has_room_for_current_nullable_id_field(self):
        value = metadata()
        value['payload']['dynamic_tools'][1]['description'] = '旧' * 96
        request_id = {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'default': None, 'title': 'Request Id'}
        line = encode(value)
        result = patch_metadata(line, SESSION, request_id)
        self.assertEqual(len(result), len(line))
        self.assertEqual(json.loads(result)['payload']['dynamic_tools'][1]['inputSchema']['properties']['request_id'], request_id)

    def test_existing_compatible_schema_retains_original_spacing_description_and_key_order(self):
        value = metadata()
        value['payload']['dynamic_tools'][1]['inputSchema']['properties']['request_id'] = dict(reversed(list(REQUEST_ID.items())))
        line = json.dumps(value, ensure_ascii=True, indent=None).encode() + b' \t  \r\n'
        self.assertEqual(patch_metadata(line, SESSION, REQUEST_ID), line)

    def test_caller_schema_is_not_mutated_and_is_copied(self):
        schema = copy.deepcopy(REQUEST_ID)
        before = copy.deepcopy(schema)
        result = patch_metadata(encode(metadata()), SESSION, schema)
        self.assertEqual(schema, before)
        schema['description'] = 'changed later'
        self.assertEqual(json.loads(result)['payload']['dynamic_tools'][1]['inputSchema']['properties']['request_id'], before)

    def test_query_schema_can_omit_required_entirely(self):
        value = metadata()
        value['payload']['dynamic_tools'][1]['inputSchema'].pop('required')
        result = json.loads(patch_metadata(encode(value), SESSION, REQUEST_ID))
        self.assertNotIn('required', result['payload']['dynamic_tools'][1]['inputSchema'])

    def test_mismatched_session_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'ID'):
            patch_metadata(encode(metadata()), 'different-session', REQUEST_ID)

    def test_missing_or_duplicate_target_is_rejected(self):
        for duplicate in (False, True):
            value = metadata()
            tools = value['payload']['dynamic_tools']
            tools.append(copy.deepcopy(tools[1])) if duplicate else tools.pop(1)
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, 'exactly one'):
                patch_metadata(encode(value), SESSION, REQUEST_ID)

    def test_conflicting_existing_schema_is_rejected(self):
        value = metadata()
        value['payload']['dynamic_tools'][1]['inputSchema']['properties']['request_id'] = {'type': 'number'}
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            patch_metadata(encode(value), SESSION, REQUEST_ID)

    def test_required_request_id_is_incompatible_even_with_current_field(self):
        value = metadata()
        schema = value['payload']['dynamic_tools'][1]['inputSchema']
        schema['properties']['request_id'] = REQUEST_ID
        schema['required'] = ['request_id']
        with self.assertRaisesRegex(ValueError, 'must not require'):
            patch_metadata(encode(value), SESSION, REQUEST_ID)

    def test_insufficient_byte_capacity_does_not_truncate_any_data(self):
        value = metadata()
        value['payload']['dynamic_tools'][1]['description'] = 'x'
        line = encode(value)
        unchanged = bytes(line)
        with self.assertRaisesRegex(ValueError, 'does not fit'):
            patch_metadata(line, SESSION, REQUEST_ID)
        self.assertEqual(line, unchanged)

    def test_truncated_invalid_or_multiline_json_is_rejected(self):
        line = encode(metadata())
        for bad in (line[:-1], line[:-12] + b'\n', b'\xff\n', b'{}\n{}\n', b'{}\r',
                    line.replace(b'"session_meta"', b'NaN', 1), b'[]\n', b'null\n'):
            with self.subTest(bad=bad[:30]), self.assertRaises(ValueError):
                patch_metadata(bad, SESSION, REQUEST_ID)

    def test_duplicate_json_keys_are_rejected_instead_of_silently_dropped(self):
        line = encode(metadata()).replace(b'"cwd":"/workspace"', b'"cwd":"/workspace","cwd":"/outside"')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            patch_metadata(line, SESSION, REQUEST_ID)

    def test_unexpected_metadata_shapes_are_rejected(self):
        for kind in ('event', 'payload', 'tools', 'tool', 'schema', 'properties', 'description', 'required'):
            value = metadata()
            target = value['payload']['dynamic_tools'][1]
            if kind == 'event': value['type'] = 'event_msg'
            elif kind == 'payload': value['payload'] = []
            elif kind == 'tools': value['payload']['dynamic_tools'] = {}
            elif kind == 'tool': value['payload']['dynamic_tools'][0] = None
            elif kind == 'schema': target['inputSchema'] = []
            elif kind == 'properties': target['inputSchema']['properties'] = []
            elif kind == 'description': target['description'] = None
            elif kind == 'required': target['inputSchema']['required'] = 'request_id'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                patch_metadata(encode(value), SESSION, REQUEST_ID)

    def test_invalid_current_schema_is_rejected(self):
        for schema in (None, {}, [], {'minLength': float('nan')}, {'value': object()}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                patch_metadata(encode(metadata()), SESSION, schema)


if __name__ == '__main__':
    unittest.main()
