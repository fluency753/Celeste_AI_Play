"""Stopped-thread migration with real temporary rollout, journal, backup and SQLite."""
from contextlib import closing, contextmanager
import asyncio
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator import codex_simulation_migration as migration
from orchestrator import codex_container, codex_session_environment, config, container
from orchestrator.codex_simulation_schema import patch_metadata


IDENT = 'aaaaaaaa-1234-5678-90ab-cccccccccccc'
SCHEMA = {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'default': None, 'title': 'Request Id'}


@contextmanager
def unlocked(*args):
    yield


class SimulationMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-migration-')
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.rollout = self.home / 'sessions/2026/11' / f'rollout-{IDENT}.jsonl'
        self.rollout.parent.mkdir(parents=True)
        metadata = {'timestamp': '2026-11-03T00:00:00Z', 'type': 'session_meta', 'payload': {
            'id': IDENT, 'cwd': '/workspace', 'dynamic_tools': [
                {'name': 'celeste_simulate', 'description': '旧模拟器说明' * 80, 'inputSchema': {
                    'type': 'object', 'properties': {'candidates': {'type': 'array'},
                                                    'input_format': {'type': 'string'}}, 'required': []}},
                {'name': 'celeste_show', 'description': '观察', 'inputSchema': {
                    'type': 'object', 'properties': {}}}]}}
        self.header = json.dumps(metadata, ensure_ascii=False, separators=(',', ':')).encode() + b'\r\n'
        self.history = ('{"type":"event_msg","payload":{"text":"历史🎮"}}\r\n'
                        '{"type":"response_item","payload":{"text":"unchanged"}}\r\n').encode()
        self.original = self.header + self.history
        self.rollout.write_bytes(self.original)
        self.journal = self.home / migration.JOURNALS / IDENT / 'journal.json'
        self.backup = self.journal.parent / 'original.jsonl'
        lock_patch = patch.object(migration, 'writer_lock', side_effect=unlocked)
        self.lock = lock_patch.start()
        self.addCleanup(lock_patch.stop)
        guard_patch = patch.object(migration, 'active_app_servers', return_value=[])
        self.guard = guard_patch.start()
        self.addCleanup(guard_patch.stop)

    def migrate(self, apply=True):
        return migration.migrate(self.home, IDENT, SCHEMA, patch_metadata, apply=apply)

    def prepare(self):
        with patch.object(migration, '_replace_header', side_effect=OSError('replacement boundary failed')):
            with self.assertRaisesRegex(OSError, 'replacement boundary'):
                self.migrate()
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'prepared')
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_dry_check_is_read_only(self):
        before = {str(path.relative_to(self.home)): path.read_bytes()
                  for path in self.home.rglob('*') if path.is_file()}
        result = self.migrate(apply=False)
        self.assertEqual(result['status'], 'outdated')
        self.assertEqual({str(path.relative_to(self.home)): path.read_bytes()
                          for path in self.home.rglob('*') if path.is_file()}, before)
        self.assertFalse(self.journal.parent.exists())
        self.lock.assert_not_called()

    def test_apply_is_fixed_length_with_complete_history_backup_and_journal(self):
        result = self.migrate()
        expected = patch_metadata(self.header, IDENT, SCHEMA)
        self.assertEqual(self.rollout.read_bytes(), expected + self.history)
        self.assertEqual(self.rollout.stat().st_size, len(self.original))
        self.assertEqual(self.backup.read_bytes(), self.original)
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'complete')
        self.assertEqual(result['status'], 'updated')
        self.assertTrue(result['history_unchanged'])
        self.assertEqual(result['header_bytes'], len(self.header))
        self.assertEqual(result['history_hash'], migration.digest(self.history))

    def test_completed_migration_is_idempotent_without_replacing_or_rebacking_up(self):
        self.migrate()
        migrated = self.rollout.read_bytes()
        journal = self.journal.read_bytes()
        with patch.object(migration, '_replace_header') as replace:
            result = self.migrate()
        replace.assert_not_called()
        self.assertEqual(result['status'], 'current')
        self.assertEqual(self.rollout.read_bytes(), migrated)
        self.assertEqual(self.journal.read_bytes(), journal)
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_prepared_before_replace_failure_can_retry(self):
        self.prepare()
        self.assertEqual(self.rollout.read_bytes(), self.original)
        self.assertEqual(self.migrate(apply=False)['status'], 'pending')
        self.assertEqual(self.migrate()['status'], 'updated')
        self.assertEqual(self.rollout.read_bytes(), patch_metadata(self.header, IDENT, SCHEMA) + self.history)

    def test_complete_journal_failure_retries_without_replacing_updated_rollout(self):
        write_json = migration._json

        def fail_complete(path, value):
            if value['state'] == 'complete':
                raise OSError('completion persistence failed')
            write_json(path, value)

        with patch.object(migration, '_json', side_effect=fail_complete):
            with self.assertRaisesRegex(OSError, 'completion persistence'):
                self.migrate()
        expected = patch_metadata(self.header, IDENT, SCHEMA) + self.history
        self.assertEqual(self.rollout.read_bytes(), expected)
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'prepared')
        with patch.object(migration, '_replace_header') as replace:
            self.assertEqual(self.migrate()['status'], 'updated')
        replace.assert_not_called()
        self.assertEqual(self.rollout.read_bytes(), expected)
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'complete')

    def test_partial_backup_write_failure_is_retryable(self):
        def partial_copy(source, output, *args):
            output.write(source.read(17))
            raise OSError('backup write failed')

        with patch.object(migration.shutil, 'copyfileobj', side_effect=partial_copy):
            with self.assertRaisesRegex(OSError, 'backup write'):
                self.migrate()
        self.assertEqual(self.rollout.read_bytes(), self.original)
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.migrate()['status'], 'updated')
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_pending_retry_keeps_newly_appended_history(self):
        self.prepare()
        appended = b'{"type":"event_msg","payload":{"text":"new event"}}\n'
        with self.rollout.open('ab') as output:
            output.write(appended)
        self.migrate()
        self.assertEqual(self.rollout.read_bytes(), patch_metadata(self.header, IDENT, SCHEMA) + self.history + appended)
        self.assertEqual(self.backup.read_bytes(), self.original)

    def test_pending_changed_header_is_rejected_without_overwriting(self):
        self.prepare()
        changed = self.original.replace(b'2026-11-03', b'2026-11-04', 1)
        self.rollout.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, '首行'):
            self.migrate()
        self.assertEqual(self.rollout.read_bytes(), changed)
        self.assertEqual(json.loads(self.journal.read_text())['state'], 'prepared')

    def test_pending_changed_or_truncated_original_history_is_rejected(self):
        self.prepare()
        for changed in (self.header + self.history.replace(b'unchanged', b'corrupted', 1), self.original[:-7]):
            with self.subTest(length=len(changed)):
                self.rollout.write_bytes(changed)
                with self.assertRaisesRegex(ValueError, '历史'):
                    self.migrate()
                self.assertEqual(self.rollout.read_bytes(), changed)
                self.assertEqual(json.loads(self.journal.read_text())['state'], 'prepared')

    def create_cache(self, ident):
        path = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(path)) as database, database:
            database.execute('CREATE TABLE thread_dynamic_tools (thread_id TEXT, name TEXT)')
            database.execute('INSERT INTO thread_dynamic_tools VALUES (?, ?)', (ident, 'celeste_simulate'))
        return path

    def test_target_thread_tool_cache_blocks_without_changing_database_or_rollout(self):
        cache = self.create_cache(IDENT)
        database_bytes = cache.read_bytes()
        with self.assertRaisesRegex(ValueError, '工具缓存'):
            self.migrate()
        self.assertEqual(cache.read_bytes(), database_bytes)
        self.assertEqual(self.rollout.read_bytes(), self.original)
        self.assertFalse(self.journal.parent.exists())

    def test_other_thread_tool_cache_does_not_block_or_change_database(self):
        cache = self.create_cache('another-thread')
        database_bytes = cache.read_bytes()
        self.assertEqual(self.migrate()['status'], 'updated')
        self.assertEqual(cache.read_bytes(), database_bytes)

    def test_busy_writer_lock_rejects_before_backup_or_write(self):
        @contextmanager
        def busy(*args):
            raise RuntimeError('writer busy')
            yield

        with patch.object(migration, 'writer_lock', side_effect=busy):
            with self.assertRaisesRegex(RuntimeError, 'writer busy'):
                self.migrate()
        self.assertEqual(self.rollout.read_bytes(), self.original)
        self.assertFalse(self.journal.parent.exists())

    def test_active_app_server_blocks_before_backup_or_write(self):
        self.guard.return_value = [12345]
        with self.assertRaisesRegex((ValueError, RuntimeError), 'app-server|app.server|宿主|进程'):
            self.migrate()
        self.assertEqual(self.rollout.read_bytes(), self.original)
        self.assertFalse(self.journal.parent.exists())

    def test_session_id_mismatch_and_unsafe_id_are_rejected(self):
        self.rollout.write_bytes(self.original.replace(IDENT.encode(), b'bbbbbbbb-1234-5678-90ab-cccccccccccc', 1))
        changed = self.rollout.read_bytes()
        with self.assertRaisesRegex(ValueError, 'ID'):
            self.migrate()
        self.assertEqual(self.rollout.read_bytes(), changed)
        with self.assertRaisesRegex(ValueError, '编号'):
            migration.migrate(self.home, '../escape', SCHEMA, patch_metadata, apply=True)
        self.assertFalse(self.journal.parent.exists())

    def test_duplicate_rollout_path_is_rejected(self):
        duplicate = self.rollout.with_name(f'another-{IDENT}.jsonl')
        duplicate.write_bytes(self.original)
        with self.assertRaisesRegex(ValueError, '唯一'):
            self.migrate()
        self.assertEqual(self.rollout.read_bytes(), self.original)
        self.assertEqual(duplicate.read_bytes(), self.original)
        self.assertFalse(self.journal.parent.exists())

    def test_symlinked_rollout_is_rejected_without_changing_target(self):
        target = self.home / 'outside.jsonl'
        self.rollout.replace(target)
        try:
            self.rollout.symlink_to(target)
        except OSError as error:
            self.skipTest(f'Host cannot create symlink: {error}')
        with self.assertRaisesRegex(ValueError, '符号链接'):
            self.migrate()
        self.assertEqual(target.read_bytes(), self.original)
        self.assertFalse(self.journal.parent.exists())


class HostMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='codex-host-migration-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'runs/fixture/20261103-000000'
        self.directory.mkdir(parents=True)
        self.output = self.root / 'maintenance-output'
        self.state = dict(agent='fixture', save='fixture', engine='codex', provider='codex',
                          model='fixture-model', effort='high', knowledge_profile='explore',
                          session_id=IDENT, session_storage='container', runtime='container',
                          profile='local', mode='explore', sim=False, live_mode=False)
        self.write_state()
        self.labels = {'celeste-ai-play.workspace_tag': 'fixture-explore'}
        self.mounts = [{'Destination': '/home/agent/.codex', 'Type': 'volume',
                        'Name': 'celeste-codex-fixture-explore'}]
        self.running = True
        self.response_id = IDENT
        self.calls = []
        self.lease_events = []
        self.patch(config, 'REPO_ROOT', self.root)
        self.docker = self.patch(container, 'docker', side_effect=self.external_docker)
        self.process = self.patch(codex_container, '_run', side_effect=self.external_process)
        self.create = self.patch(container, '_create', side_effect=AssertionError('must not create a container'))
        self.ensure = self.patch(container, 'ensure_container', side_effect=AssertionError('must not ensure a container'))
        from orchestrator import game_process
        self.lease = self.patch(game_process, 'controller_lease', side_effect=self.external_lease)

    def patch(self, target, name, *args, **kwargs):
        item = patch.object(target, name, *args, **kwargs)
        result = item.start()
        self.addCleanup(item.stop)
        return result

    def write_state(self):
        (self.directory / 'run_state.json').write_text(json.dumps(self.state), encoding='utf-8')

    def external_docker(self, *args, **kwargs):
        self.assertEqual(args[0], 'inspect')
        self.assertEqual(args[-1], 'celeste-agent-fixture-explore')
        return subprocess.CompletedProcess(args, 0, json.dumps(self.running) + '|' +
                                           json.dumps(self.labels) + '|' + json.dumps(self.mounts), '')

    @contextmanager
    def external_lease(self):
        self.lease_events.append('acquired')
        try:
            yield
        finally:
            self.lease_events.append('released')

    def external_process(self, name, *command, **kwargs):
        self.assertEqual(name, 'celeste-agent-fixture-explore')
        self.calls.append((command, kwargs))
        if command == ('codex', '--version'):
            return subprocess.CompletedProcess(command, 0, f'codex-cli {codex_container.CODEX_VERSION}\n', '')
        self.assertEqual(command[:3], ('python3', '-I', '-c'))
        return subprocess.CompletedProcess(command, 0, json.dumps({'session_id': self.response_id, 'status': 'outdated'}), '')

    def migrate(self, apply=False, directory=None):
        return asyncio.run(migration.migrate_run(directory or self.directory, self.output, apply=apply))

    def assert_no_creation(self):
        self.create.assert_not_called()
        self.ensure.assert_not_called()

    def test_host_uses_actual_current_schema_and_trusted_python_without_model_or_container_creation(self):
        expected_tools = asyncio.run(codex_session_environment.tool_specs(
            codex_session_environment.configuration(self.state), self.root / 'expected-descriptor'))
        expected_schema = next(tool for tool in expected_tools if tool['name'] == 'celeste_simulate')['inputSchema']['properties']['request_id']
        original_state = (self.directory / 'run_state.json').read_bytes()
        for apply in (False, True):
            with self.subTest(apply=apply):
                self.calls.clear()
                result = self.migrate(apply=apply)
                self.assertEqual(result['session_id'], IDENT)
                self.assertEqual(result['container'], 'celeste-agent-fixture-explore')
                self.assertEqual(len(self.calls), 2)  # Runtime version, then isolated maintenance Python.
                command, kwargs = self.calls[-1]
                payload = json.loads(kwargs['input'])
                self.assertEqual(payload['schema'], expected_schema)
                self.assertEqual(payload['session_id'], IDENT)
                self.assertEqual(payload['apply'], apply)
                self.assertIn('def patch_metadata(', payload['patch_source'])
                self.assertIn('def migrate(', payload['maintenance_source'])
                self.assertIn("maintenance['migrate']", command[-1])
                self.assertEqual(kwargs['timeout'], 180)
                self.assertEqual((self.directory / 'run_state.json').read_bytes(), original_state)
                self.assert_no_creation()
        self.assertEqual(self.lease_events, ['acquired', 'released'])
        self.lease.assert_called_once()

    def test_host_rejects_wrong_run_engine_and_storage_before_docker_or_mutex(self):
        with self.assertRaisesRegex(ValueError, 'runs'):
            self.migrate(directory=self.root / 'outside', apply=True)
        for field, value in (('engine', 'claude'), ('session_storage', 'host')):
            with self.subTest(field=field):
                original = self.state[field]
                self.state[field] = value
                self.write_state()
                with self.assertRaisesRegex(ValueError, '容器 Codex'):
                    self.migrate(apply=True)
                self.state[field] = original
        self.docker.assert_not_called()
        self.process.assert_not_called()
        self.lease.assert_not_called()
        self.assert_no_creation()

    def test_host_rejects_stopped_foreign_owner_or_session_volume_before_process(self):
        for damage in ('stopped', 'owner', 'volume', 'bind'):
            with self.subTest(damage=damage):
                self.running = damage != 'stopped'
                self.labels = {'celeste-ai-play.workspace_tag': 'other' if damage == 'owner' else 'fixture-explore'}
                self.mounts = [{'Destination': '/home/agent/.codex',
                                'Type': 'bind' if damage == 'bind' else 'volume',
                                'Name': 'other' if damage == 'volume' else 'celeste-codex-fixture-explore'}]
                with self.assertRaisesRegex(ValueError, '归属|未运行'):
                    self.migrate()
        self.process.assert_not_called()
        self.lease.assert_not_called()
        self.assert_no_creation()

    def test_host_rejects_maintenance_response_for_another_session_and_releases_lease(self):
        self.response_id = 'another-session'
        with self.assertRaisesRegex(RuntimeError, '身份不一致'):
            self.migrate(apply=True)
        self.assertEqual(self.lease_events, ['acquired', 'released'])
        self.assert_no_creation()


if __name__ == '__main__':
    unittest.main()
