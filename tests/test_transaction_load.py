import os
from unittest import TestCase, skipUnless

from pqo.analysis_control import AnalysisControl
from pqo.transaction_load import LoadOptions, run_transaction_load


SQL = '''BEGIN;
SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ WRITE;
SELECT id, quantity FROM retail.stock WHERE id = 1;
UPDATE retail.stock SET quantity = quantity + 1 WHERE id = 1 RETURNING quantity;
COMMIT;'''


@skipUnless(os.getenv('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'requires disposable PostgreSQL laboratory')
class TransactionLoadPostgresTests(TestCase):
    def setUp(self):
        from test_transaction_joined_dml import JoinedDMLPostgresTests

        self.fixture = JoinedDMLPostgresTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def run_load(self, text=SQL, **kwargs):
        before = self.fixture.fingerprint()
        report = run_transaction_load(text, self.fixture.settings, self.fixture.reference,
            test_copy_confirmed=True, disposable_databases_confirmed=True, **kwargs)
        self.assertTrue(report.cleanup_verified, report.reasons)
        self.assertEqual(before, self.fixture.fingerprint())
        with self.fixture.connect('postgres') as c:
            self.assertFalse(c.execute('SELECT datname FROM pg_database WHERE datname=ANY(%s)',
                                       (list(report.created_databases),)).fetchall())
        return report

    def test_clients_share_copy_and_override_isolation_in_set_transaction(self):
        report = self.run_load(options=LoadOptions(4, 3, compare_isolation='SERIALIZABLE'))
        self.assertEqual(report.status, 'completed', report.reasons)
        self.assertEqual(len(report.created_databases), 3)
        first, second = report.phases
        self.assertEqual((first.isolation, second.isolation), ('READ COMMITTED', 'SERIALIZABLE'))
        self.assertEqual((first.committed, first.failed), (12, 0))
        self.assertEqual(second.committed + second.failed, 12)
        self.assertEqual(first.initial_state, second.initial_state)
        self.assertNotEqual(first.initial_state, first.final_state)
        self.assertGreater(first.completed_per_second, 0)
        self.assertGreaterEqual(first.p95_ms, first.p50_ms)
        self.assertEqual({attempt.client for attempt in second.attempts}, {1, 2, 3, 4})
        self.assertTrue(all(a.sqlstate == '40001' for a in second.attempts if a.outcome == 'failed'))

    def test_rollback_and_cancellation_clean_the_shared_copy(self):
        report = self.run_load(SQL.replace('COMMIT;', 'ROLLBACK;'), options=LoadOptions(3, 2))
        phase, = report.phases
        self.assertEqual(report.status, 'completed', report.reasons)
        self.assertEqual((phase.committed, phase.rolled_back), (0, 6))
        self.assertEqual(phase.initial_state, phase.final_state)

        control = AnalysisControl()
        def cancel_at_start(message):
            if 'клиентов на общей копии' in message:
                control.cancel()
        control.progress = cancel_at_start
        cancelled = self.run_load(control=control)
        self.assertEqual(cancelled.status, 'cancelled', cancelled.reasons)
        self.assertEqual(len(cancelled.created_databases), 2)
        self.assertEqual(cancelled.phases, ())
