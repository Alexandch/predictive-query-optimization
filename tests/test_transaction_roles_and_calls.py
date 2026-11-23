import os
from unittest import TestCase, skipUnless
from uuid import uuid4

from psycopg import sql

from pqo.transaction_planning import collect_transaction_plans
from pqo.transaction_replay import result_digest
from pqo.transaction_resources import run_transaction_resources


@skipUnless(os.getenv('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'requires a disposable PostgreSQL lab')
class TransactionRolesAndCallsTests(TestCase):
    def setUp(self):
        from test_transaction_joined_dml import JoinedDMLPostgresTests
        self.fixture = JoinedDMLPostgresTests(methodName='runTest')
        self.role = 'pqo_rolefixture_' + uuid4().hex
        self.addCleanup(self.drop_role)
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        with self.fixture.connect('postgres', autocommit=True) as c:
            c.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(self.role)))

    def drop_role(self):
        with self.fixture.connect('postgres', autocommit=True) as c:
            c.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(self.role)))

    def profile(self, text):
        before = self.fixture.fingerprint()
        report = run_transaction_resources(text, self.fixture.settings, self.fixture.reference,
            test_copy_confirmed=True, disposable_databases_confirmed=True)
        self.assertTrue(report.cleanup_verified, report.reasons)
        self.assertEqual(self.fixture.fingerprint(), before)
        return report

    def set_up_policy(self):
        with self.fixture.connect() as c:
            ident = sql.Identifier(self.role)
            c.execute(sql.SQL('GRANT USAGE ON SCHEMA retail TO {}').format(ident))
            c.execute(sql.SQL('GRANT SELECT, UPDATE ON retail.stock TO {}').format(ident))
            c.execute('ALTER TABLE retail.stock ENABLE ROW LEVEL SECURITY')
            c.execute(sql.SQL('CREATE POLICY tenant ON retail.stock TO {} USING (account_id=10 AND CURRENT_USER={}) '
                              'WITH CHECK (account_id=10)').format(ident, sql.Literal(self.role)))

    def test_calls_execute_under_rls_and_snapshots_include_hidden_rows(self):
        self.set_up_policy()
        with self.fixture.connect() as c:
            c.execute('''CREATE PROCEDURE retail.bump() LANGUAGE SQL BEGIN ATOMIC
                UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;
                END''')
        text = f'''BEGIN ISOLATION LEVEL REPEATABLE READ;
        SET LOCAL ROLE {self.role};
        CALL retail.bump();
        UPDATE retail.stock SET quantity=quantity+100 WHERE id=2 RETURNING id;
        SELECT id,quantity FROM retail.stock ORDER BY id;
        COMMIT;'''
        report = self.profile(text)
        self.assertEqual(report.status, 'completed', report.reasons)
        item, = report.variants
        self.assertEqual(item.control.commands[3].result_sha256, result_digest([(1, 11)], True))
        self.assertEqual(item.control.commands[2].row_count, 0)
        self.assertEqual(next(t for t in item.control.final_state if t.table == 'retail.stock').rows, 4)
        self.assertTrue(report.profiles_match_controls)
        self.assertIsNone(item.profile.commands[1].resources)

        from dataclasses import replace
        plans = collect_transaction_plans(text, replace(self.fixture.settings, dbname=self.fixture.reference))
        self.assertEqual([statement.status for statement in plans.statements], ['control', 'blocked', 'planned', 'planned'])

    def test_unknown_policy_and_procedure_are_declined_before_cloning(self):
        with self.fixture.connect() as c:
            c.execute('CREATE PROCEDURE retail.opaque() LANGUAGE plpgsql AS $$ BEGIN NULL; END $$')
            c.execute('''CREATE PROCEDURE retail.wide() LANGUAGE SQL BEGIN ATOMIC
                UPDATE retail.stock SET quantity=0 WHERE account_id=10;
                END''')
        for name in ('opaque', 'wide'):
            with self.subTest(name=name):
                report = self.profile(f'BEGIN; CALL retail.{name}(); SELECT id FROM retail.stock; COMMIT;')
                self.assertEqual(report.status, 'declined', report.reasons)
                self.assertFalse(report.created_databases)
        self.set_up_policy()
        with self.fixture.connect() as c:
            c.execute('ALTER POLICY tenant ON retail.stock USING (current_database() IS NOT NULL)')
        report = self.profile(f'''BEGIN; SET LOCAL ROLE {self.role};
            UPDATE retail.stock SET quantity=1 WHERE id=1; SELECT id FROM retail.stock; COMMIT;''')
        self.assertEqual(report.status, 'declined', report.reasons)
        self.assertFalse(report.created_databases)
