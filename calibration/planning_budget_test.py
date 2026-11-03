"""Host planning allowance transitions; no live configuration, game or containers."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.metrics import Metrics
from orchestrator.planning_budget import PlanningBudget, room_rounds


class PlanningBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / 'run' / 'planning_budget.json'
        self.projection = self.root / 'workspace' / 'game' / 'rounds.json'
        self.metrics = Metrics(self.root / 'metrics.json', 'test', 'fixture', 'medium', 600)
        self.budget = self.open_budget()

    def tearDown(self):
        self.temp.cleanup()

    def open_budget(self, **options):
        return PlanningBudget(self.metrics, self.private, self.projection, **options)

    def credit(self, count=3):
        self.budget.start()
        self.budget.on_room('Celeste/1-ForsakenCity:A', 'start', [0, 0, 320, 180])
        self.assertEqual(self.budget.bank, count)

    def test_room_size_and_repeat_rewards_survive_restart(self):
        self.credit()
        self.assertFalse(self.budget.on_room('Celeste/1-ForsakenCity:A', 'start'))
        self.assertTrue(self.budget.on_room('Celeste/1-ForsakenCity:A', 'wide', [0, 0, 640, 180]))
        self.assertEqual(self.budget.bank, 9)
        self.budget.reserve('request-1', 'fingerprint')
        restarted = self.open_budget()
        self.assertEqual(restarted.bank, 8)
        self.assertEqual(restarted.rounds_used, 1)
        self.assertTrue(restarted.started)
        self.assertFalse(restarted.on_room('Celeste/1-ForsakenCity:A', 'wide'))
        self.assertEqual(restarted.bank, 8)
        self.assertEqual(self.metrics.state.plan_rounds_used, 1)

    def test_sides_and_chapters_reset_balance_and_room_visits(self):
        self.credit()
        self.budget.on_room('Celeste/1-ForsakenCity:B', 'start')
        self.assertEqual(self.budget.bank, 3)
        self.budget.reserve('side-b', 'input')
        self.budget.on_room('Celeste/2-OldSite:A', 'start')
        self.assertEqual(self.budget.bank, 3)
        self.assertEqual(self.budget.rounds_used, 1)
        self.assertEqual(self.budget.snapshot()['rooms_seen'], 1)

    def test_deaths_add_budget_without_resetting_used_count(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        self.budget.on_death(2)
        self.assertEqual(self.budget.bank, 4)
        self.assertEqual(self.budget.rounds_used, 1)

    def test_workspace_projection_cannot_grant_or_restore_budget(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        self.projection.write_text('{"limited":false,"left":999999,"used":0}', encoding='utf-8')
        restarted = self.open_budget()
        self.assertEqual(restarted.bank, 2)
        self.assertEqual(json.loads(self.projection.read_text(encoding='utf-8'))['left'], 2)
        # The API returns copies; mutating a caller's snapshot cannot change the ledger.
        snapshot = restarted.snapshot()
        snapshot['left'] = 100
        self.assertEqual(restarted.bank, 2)

    def test_resume_copies_private_ledger_and_does_not_touch_source(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        source = self.private.read_bytes()
        next_private = self.root / 'next-run' / 'planning_budget.json'
        next_budget = PlanningBudget(self.metrics, next_private, self.projection, previous_path=self.private)
        self.assertEqual(next_budget.bank, 2)
        self.assertFalse(next_budget.on_room('Celeste/1-ForsakenCity:A', 'start'))
        next_budget.on_death()
        self.assertEqual(next_budget.bank, 3)
        self.assertEqual(self.private.read_bytes(), source)

    def test_legacy_resume_does_not_use_workspace_or_gift_current_room(self):
        self.projection.write_text('{"left":100000,"rooms_seen":0}', encoding='utf-8')
        old = self.root / 'legacy-run' / 'planning_budget.json'
        migrated = PlanningBudget(self.metrics, old, self.projection, legacy=True)
        self.assertEqual(migrated.bank, 0)
        self.assertTrue(migrated.snapshot()['migration_required'])
        self.assertFalse(migrated.on_room('Celeste/1-ForsakenCity:A', 'current'))
        self.assertEqual(migrated.bank, 0)
        self.assertFalse(migrated.snapshot()['migration_required'])
        restarted = PlanningBudget(self.metrics, old, self.projection)
        self.assertFalse(restarted.on_room('Celeste/1-ForsakenCity:A', 'current'))
        restarted.on_room('Celeste/1-ForsakenCity:A', 'next')
        self.assertEqual(restarted.bank, 3)

    def test_private_save_failure_does_not_spend_or_cache_authorization(self):
        self.credit()
        before = self.private.read_bytes()
        with patch.object(Path, 'replace', side_effect=PermissionError('private ledger locked')):
            with self.assertRaises(PermissionError):
                self.budget.reserve('request-1', 'input')
        self.assertEqual(self.budget.bank, 3)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.private.read_bytes(), before)
        receipt, fresh = self.budget.reserve('request-1', 'input')
        self.assertTrue(fresh)
        self.assertEqual(receipt['status'], 'reserved')
        self.assertEqual(self.budget.bank, 2)

    def test_cleanup_failure_after_replace_does_not_hide_successful_commit(self):
        self.credit()
        with patch.object(Path, 'unlink', side_effect=PermissionError('temporary cleanup locked')):
            receipt, fresh = self.budget.reserve('request-1', 'input')
        self.assertTrue(fresh)
        self.assertEqual(receipt['status'], 'reserved')
        self.assertEqual(self.budget.bank, 2)
        persisted = json.loads(self.private.read_text(encoding='utf-8'))
        self.assertEqual(persisted['bank'], 2)
        restarted = self.open_budget()
        receipt, fresh = restarted.reserve('request-1', 'input')
        self.assertFalse(fresh)
        self.assertEqual(receipt['status'], 'reserved')
        self.assertEqual(restarted.rounds_used, 1)

    def test_cleanup_failure_cannot_replace_original_commit_error(self):
        self.credit()
        with patch.object(Path, 'replace', side_effect=PermissionError('private replace locked')), \
             patch.object(Path, 'unlink', side_effect=PermissionError('temporary cleanup locked')):
            with self.assertRaisesRegex(PermissionError, 'private replace locked'):
                self.budget.reserve('request-1', 'input')
        self.assertEqual(self.budget.bank, 3)
        self.assertEqual(self.budget.rounds_used, 0)

    def test_deposit_failure_preserves_unvisited_room_for_retry(self):
        before = self.private.read_bytes()
        with patch.object(Path, 'replace', side_effect=PermissionError('private ledger locked')):
            with self.assertRaises(PermissionError):
                self.budget.on_room('chapter:A', 'start')
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.snapshot()['rooms_seen'], 0)
        self.assertEqual(self.private.read_bytes(), before)
        self.assertTrue(self.budget.on_room('chapter:A', 'start'))
        self.assertEqual(self.budget.bank, 3)

    def test_projection_failure_keeps_spend_and_repeated_request_repairs_publication(self):
        self.credit()
        original_replace = Path.replace

        def replace(path, target):
            if Path(target) == self.projection:
                raise PermissionError('workspace projection locked')
            return original_replace(path, target)

        with patch.object(Path, 'replace', replace):
            receipt, fresh = self.budget.reserve('request-1', 'input')
        self.assertTrue(fresh)
        self.assertEqual(receipt['status'], 'reserved')
        self.assertEqual(self.budget.bank, 2)
        self.assertEqual(json.loads(self.projection.read_text(encoding='utf-8'))['left'], 3)
        repeated, fresh = self.budget.reserve('request-1', 'input')
        self.assertFalse(fresh)
        self.assertEqual(repeated['status'], 'reserved')
        self.assertEqual(json.loads(self.projection.read_text(encoding='utf-8'))['left'], 2)
        self.assertEqual(self.budget.rounds_used, 1)

    def test_pending_failed_and_completed_receipts_never_authorize_reexecution(self):
        self.credit()
        for index, status in enumerate(('reserved', 'failed', 'completed')):
            ident = f'request-{index}'
            receipt, fresh = self.budget.reserve(ident, 'input')
            self.assertTrue(fresh)
            if status != 'reserved':
                self.budget.finish(ident, status=status)
            restarted = self.open_budget()
            repeated, fresh = restarted.reserve(ident, 'input')
            self.assertFalse(fresh)
            self.assertEqual(repeated['status'], status)
            self.assertEqual(restarted.bank, 2 - index)
        self.assertEqual(self.budget.bank, 0)
        with self.assertRaisesRegex(ValueError, '本次未执行模拟'):
            self.budget.reserve('another-request', 'input')
        self.assertIn('普通程序仍可使用', self.budget.deny_reason())
        with self.assertRaisesRegex(ValueError, '不同内容'):
            self.budget.reserve('request-0', 'different-input')

    def test_failed_completion_write_leaves_conservative_reservation_and_can_finish_later(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        with patch.object(Path, 'replace', side_effect=PermissionError('completion locked')):
            with self.assertRaises(PermissionError):
                self.budget.finish('request-1')
        restarted = self.open_budget()
        repeated, fresh = restarted.reserve('request-1', 'input')
        self.assertFalse(fresh)
        self.assertEqual(repeated['status'], 'reserved')
        self.assertEqual(restarted.bank, 2)
        self.assertEqual(restarted.finish('request-1')['status'], 'completed')
        restarted.finish('request-1', status='failed')
        self.assertEqual(restarted.reserve('request-1', 'input')[0]['status'], 'completed')

    def test_receipt_lookup_is_readonly_checks_identity_and_returns_a_copy(self):
        self.credit()
        self.assertIsNone(self.budget.receipt('request-1', 'input'))
        self.budget.reserve('request-1', 'input')
        before = self.private.read_bytes()
        receipt = self.budget.receipt('request-1', 'input')
        self.assertEqual(receipt['status'], 'reserved')
        receipt['status'] = 'completed'
        self.assertEqual(self.budget.receipt('request-1', 'input')['status'], 'reserved')
        self.assertEqual(self.private.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, '不同内容'):
            self.budget.receipt('request-1', 'changed-input')
        for ident, digest in ((None, 'input'), ('', 'input'), ('new', '')):
            with self.subTest(ident=ident, digest=digest), self.assertRaises(ValueError):
                self.budget.receipt(ident, digest)

    def test_confirmed_unsubmitted_request_refund_is_idempotent_and_cannot_reexecute(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        self.assertEqual(self.budget.refund('request-1')['status'], 'refunded')
        self.assertEqual(self.budget.bank, 3)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.metrics.state.plan_rounds_used, 0)
        restarted = self.open_budget()
        restarted.refund('request-1')
        receipt, fresh = restarted.reserve('request-1', 'input')
        self.assertFalse(fresh)
        self.assertEqual(receipt['status'], 'refunded')
        restarted.finish('request-1')
        self.assertEqual(restarted.receipt('request-1', 'input')['status'], 'refunded')
        self.assertEqual(restarted.bank, 3)
        self.assertEqual(restarted.rounds_used, 0)

    def test_refund_write_failure_keeps_reservation_and_does_not_refund_memory(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        before = self.private.read_bytes()
        with patch.object(Path, 'replace', side_effect=PermissionError('refund ledger locked')):
            with self.assertRaisesRegex(PermissionError, 'refund ledger locked'):
                self.budget.refund('request-1')
        self.assertEqual(self.budget.bank, 2)
        self.assertEqual(self.budget.rounds_used, 1)
        self.assertEqual(self.budget.receipt('request-1', 'input')['status'], 'reserved')
        self.assertEqual(self.private.read_bytes(), before)
        self.budget.refund('request-1')
        self.assertEqual(self.budget.bank, 3)
        self.assertEqual(self.budget.rounds_used, 0)

    def test_refund_does_not_reverse_failed_or_completed_execution(self):
        self.credit()
        for status in ('failed', 'completed'):
            self.budget.reserve(status, 'input')
            self.budget.finish(status, status=status)
            receipt = self.budget.refund(status)
            self.assertEqual(receipt['status'], status)
        self.assertEqual(self.budget.bank, 1)
        self.assertEqual(self.budget.rounds_used, 2)

    def test_late_refund_does_not_move_old_allowance_into_a_new_chapter(self):
        self.credit()
        self.budget.reserve('request-1', 'input')
        self.budget.on_room('Celeste/2-OldSite:A', 'start')
        self.assertEqual(self.budget.bank, 3)
        self.budget.refund('request-1')
        self.assertEqual(self.budget.bank, 3)
        self.assertEqual(self.budget.rounds_used, 0)
        self.assertEqual(self.open_budget().receipt('request-1', 'input')['status'], 'refunded')

    def test_concurrent_requests_cannot_overspend_and_duplicate_ids_authorize_once(self):
        self.credit()

        def reserve(number):
            try:
                return self.budget.reserve(f'parallel-{number}', 'input')[1]
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(outcomes), 3)
        self.assertEqual(self.budget.bank, 0)
        self.assertEqual(self.budget.rounds_used, 3)
        self.budget.on_death()
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(lambda _: self.budget.reserve('same-request', 'input')[1], range(12)))
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.budget.rounds_used, 4)
        self.assertEqual(self.open_budget().bank, 0)

    def test_corrupt_private_ledger_fails_closed_without_reset(self):
        self.private.write_text('{"version":1,"bank":100}', encoding='utf-8')
        before = self.private.read_bytes()
        with self.assertRaisesRegex(ValueError, '未重置额度'):
            self.open_budget()
        self.assertEqual(self.private.read_bytes(), before)

    def test_room_size_falls_back_for_missing_or_nonfinite_bounds(self):
        self.assertEqual(room_rounds(3, [0, 0, 640, 180]), 6)
        for bounds in (None, [], [0, 0, float('inf'), 180], [0, 0, float('nan'), 180], [0, 0, -1, 180]):
            with self.subTest(bounds=bounds):
                self.assertEqual(room_rounds(3, bounds), 3)


if __name__ == '__main__':
    unittest.main()
