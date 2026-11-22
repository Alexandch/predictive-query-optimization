from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.transaction_concurrency import concurrency_request_digest, prepare_concurrent, run_concurrent_transactions
from pqo.transaction_replay import ScratchDatabases, result_digest


EXAMPLES = json.loads((Path(__file__).resolve().parents[1] / 'examples/transactions/university-concurrency.json').read_text(encoding='utf-8'))


def fixture_case(name):
    return {k: v.replace('university.students', 'retail.stock').replace('student_id', 'id').replace('campus_id', 'quantity')
        for k, v in EXAMPLES[name].items()}


class ConcurrencyContractTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname='source', allowed_schemas=frozenset({'retail'}))
        self.case = fixture_case('lock')

    def test_examples_preserve_each_session_command_order(self):
        for name in EXAMPLES:
            case = fixture_case(name)
            programs, steps = prepare_concurrent(case['sql_a'], case['sql_b'], case['schedule'], self.settings)
            for label in ('A', 'B'):
                self.assertEqual([s.command for s in steps if s.session == label and type(s.command) is int],
                    list(range(1, len(programs[label].commands) + 1)))
                self.assertEqual(sum(s.command == 'END' and s.session == label for s in steps), 1)
            self.assertNotIn('?', case['description'])

    def test_bad_schedules_sql_or_missing_confirmation_fail_before_connection(self):
        a, b, schedule = self.case['sql_a'], self.case['sql_b'], self.case['schedule']
        for invalid in ('A 2', 'A 1\nA 1', 'A END', 'WAIT B', 'WAIT B LOCK', 'A 1 &\nA 2',
                schedule.replace('WAIT B\n', ''), schedule + '\nB 2', schedule + '\nDROP DATABASE source',
                'A 1\nSLEEP 10', 'x' * 4001):
            with self.subTest(schedule=invalid), patch('pqo.transaction_concurrency.psycopg.connect') as connect:
                with self.assertRaises(ValueError):
                    run_concurrent_transactions(a, b, invalid, self.settings, 'baseline',
                        test_copy_confirmed=True, disposable_databases_confirmed=True)
                connect.assert_not_called()
        for text, ref, confirmed in ((a, 'source', True), (a, 'baseline', False),
                (a.replace('quantity+1', 'random()'), 'baseline', True),
                (a.replace('READ WRITE', 'READ ONLY'), 'baseline', True)):
            with self.subTest(text=text), patch('pqo.transaction_concurrency.psycopg.connect') as connect:
                with self.assertRaises(ValueError):
                    run_concurrent_transactions(text, b, schedule, self.settings, ref,
                        test_copy_confirmed=confirmed, disposable_databases_confirmed=confirmed)
                connect.assert_not_called()

    def test_provenance_includes_sql_schedule_endpoint_reference_and_limits(self):
        a, b, steps = self.case['sql_a'], self.case['sql_b'], self.case['schedule']
        base = concurrency_request_digest(a, b, steps, self.settings, 'baseline', 5000)
        for args in ((a.replace('READ COMMITTED', 'REPEATABLE READ'), b, steps, self.settings, 'baseline', 5000),
                (a, b, steps.replace('A 1\nB 1', 'B 1\nA 1'), self.settings, 'baseline', 5000),
                (a, b, steps, replace(self.settings, port=5433), 'baseline', 5000),
                (a, b, steps, self.settings, 'other', 5000), (a, b, steps, self.settings, 'baseline', 6000)):
            self.assertNotEqual(base, concurrency_request_digest(*args))
        for value in (True, 499, 10001, 1.5):
            with self.assertRaises(ValueError):
                concurrency_request_digest(a, b, steps, self.settings, 'baseline', value)

    def test_malformed_sql_and_missing_password_are_safe_diagnostics(self):
        import psycopg
        with self.assertRaises(ValueError):
            prepare_concurrent("BEGIN; SELECT 'unfinished", self.case['sql_b'], self.case['schedule'], self.settings)
        with patch('pqo.transaction_concurrency.psycopg.connect', side_effect=psycopg.OperationalError('fe_sendauth: no password supplied')):
            r = run_concurrent_transactions(self.case['sql_a'], self.case['sql_b'], self.case['schedule'], self.settings,
                'baseline', test_copy_confirmed=True, disposable_databases_confirmed=True)
        self.assertEqual(r.status, 'error')
        self.assertTrue(r.cleanup_verified)
        self.assertFalse(r.created_databases)
        self.assertIn('Пароль не введён', r.reasons[0])


@skipUnless(os.getenv('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'requires disposable PostgreSQL laboratory')
class ConcurrencyPostgresTests(TestCase):
    def setUp(self):
        from test_transaction_joined_dml import JoinedDMLPostgresTests
        self.fixture = JoinedDMLPostgresTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.settings, self.reference = self.fixture.settings, self.fixture.reference

    def run_case(self, name, *, case=None, **kwargs):
        case = case or fixture_case(name)
        before, self.captured = self.fixture.fingerprint(), None

        def factory(*args, **kw):
            scratch = ScratchDatabases(*args, **kw)
            finish = scratch.finish

            def capture_and_finish():
                try:
                    if len(scratch.created) == 2:
                        with self.fixture.connect(list(scratch.created)[-1]) as c:
                            self.captured = c.execute('SELECT id,quantity FROM retail.stock ORDER BY id').fetchall()
                finally:
                    cleanup = finish()
                return cleanup
            scratch.finish = capture_and_finish
            return scratch

        with patch('pqo.transaction_concurrency.ScratchDatabases', side_effect=factory):
            r = run_concurrent_transactions(case['sql_a'], case['sql_b'], case['schedule'], self.settings, self.reference,
                test_copy_confirmed=True, disposable_databases_confirmed=True, **kwargs)
        self.assertTrue(r.cleanup_verified, r.reasons)
        self.assertEqual(before, self.fixture.fingerprint())
        with self.fixture.connect('postgres') as c:
            self.assertFalse(c.execute('SELECT datname FROM pg_catalog.pg_database WHERE datname=ANY(%s)', (list(r.created_databases),)).fetchall())
        return r

    def test_lock_wait_has_real_blocking_pid_and_both_writes_commit(self):
        r = self.run_case('lock')
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual([s.outcome for s in r.sessions], ['committed', 'committed'])
        self.assertTrue(any(o.session == 'B' and o.command == 2 and o.blockers == ('A',) for o in r.locks))
        self.assertEqual(self.captured, [(1,12),(2,20),(3,30),(4,40)])
        self.assertNotEqual(r.initial_state, r.final_state)
        self.assertEqual(r.initial_sequences, r.final_sequences)
        self.assertEqual(len(r.created_databases), 2)

    def test_deadlock_reports_one_victim_without_retry_and_survivor_commits(self):
        r = self.run_case('deadlock')
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual(sorted(s.outcome for s in r.sessions), ['aborted', 'committed'])
        self.assertEqual([c.sqlstate for s in r.sessions for c in s.commands if c.sqlstate], ['40P01'])
        self.assertTrue(r.locks)
        self.assertEqual(self.captured, [(1,11),(2,21),(3,30),(4,40)])
        self.assertTrue(any(c.status == 'skipped' and c.number == 'END' for s in r.sessions for c in s.commands))

    def test_repeat_read_visibility_at_all_four_isolations(self):
        initial, changed = result_digest([(1,10),(2,20)], True), result_digest([(1,11),(2,20)], True)
        for level in ('READ UNCOMMITTED', 'READ COMMITTED', 'REPEATABLE READ', 'SERIALIZABLE'):
            case = fixture_case('snapshot')
            case['sql_a'] = case['sql_a'].replace('REPEATABLE READ', level)
            with self.subTest(level=level):
                r = self.run_case('snapshot', case=case)
                self.assertEqual(r.status, 'completed', r.reasons)
                a = r.sessions[0]
                self.assertEqual(a.transaction_options.isolation_level, level)
                self.assertEqual(a.commands[0].result_sha256, initial)
                self.assertEqual(a.commands[1].result_sha256, changed if level in ('READ COMMITTED', 'READ UNCOMMITTED') else initial)
                self.assertEqual(self.captured, [(1,11),(2,20),(3,30),(4,40)])

    def test_same_row_updates_abort_with_40001_at_strict_isolations(self):
        for level in ('REPEATABLE READ', 'SERIALIZABLE'):
            case = fixture_case('lock')
            case['sql_a'], case['sql_b'] = (case[k].replace('READ COMMITTED', level) for k in ('sql_a', 'sql_b'))
            with self.subTest(level=level):
                r = self.run_case('lock', case=case)
                self.assertEqual(r.status, 'completed', r.reasons)
                self.assertEqual([s.outcome for s in r.sessions], ['committed', 'aborted'])
                self.assertEqual([c.sqlstate for s in r.sessions for c in s.commands if c.sqlstate], ['40001'])
                self.assertEqual(self.captured, [(1,11),(2,20),(3,30),(4,40)])

    def test_serializable_write_skew_is_reported_as_conflict(self):
        r = self.run_case('serialization')
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual(sorted(s.outcome for s in r.sessions), ['aborted', 'committed'])
        self.assertEqual([c.sqlstate for s in r.sessions for c in s.commands if c.sqlstate], ['40001'])
        self.assertIn(self.captured, ([(1,11),(2,20),(3,30),(4,40)], [(1,10),(2,21),(3,30),(4,40)]))
        self.assertTrue(r.final_state)

    def test_savepoint_rollback_releases_lock_to_peer_and_terminal_rollback_is_preserved(self):
        case = {
            'sql_a': '''BEGIN ISOLATION LEVEL REPEATABLE READ;
                SELECT id,quantity FROM retail.stock WHERE id=1;
                SAVEPOINT discarded;
                UPDATE retail.stock SET quantity=quantity+10 WHERE id=1 RETURNING id,quantity;
                ROLLBACK TO discarded;RELEASE discarded;
                SELECT id,quantity FROM retail.stock WHERE id=1;ROLLBACK;''',
            'sql_b': '''BEGIN;SELECT id,quantity FROM retail.stock WHERE id=1;
                UPDATE retail.stock SET quantity=quantity+1 WHERE id=1 RETURNING id,quantity;COMMIT;''',
            'schedule': 'A 1\nB 1\nA 2\nA 3\nB 2 &\nWAIT B LOCK\nA 4\nWAIT B\nA 5\nB END\nA 6\nA END',
        }
        r = self.run_case('lock', case=case)
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual([s.outcome for s in r.sessions], ['rolled_back', 'committed'])
        self.assertEqual(r.sessions[0].commands[2].result_sha256, result_digest([(1,20)], False))
        self.assertEqual(r.sessions[0].commands[5].result_sha256, result_digest([(1,10)], False))
        self.assertEqual(r.sessions[1].commands[1].result_sha256, result_digest([(1,11)], False))
        self.assertTrue(r.locks)
        self.assertEqual(self.captured, [(1,11),(2,20),(3,30),(4,40)])

    def test_statement_timeout_and_midflight_cancel_cleanup(self):
        case = fixture_case('lock')
        case['schedule'] = case['schedule'].replace('A END\nWAIT B', 'WAIT B\nA END')
        r = self.run_case('lock', case=case, statement_ms=500)
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual([c.sqlstate for s in r.sessions for c in s.commands if c.sqlstate], ['57014'])
        control = AnalysisControl(progress=lambda msg: control.cancel() if 'B LOCK' in msg else None)
        r = self.run_case('lock', control=control)
        self.assertEqual(r.status, 'cancelled', r.reasons)
        self.assertFalse(r.final_state)
        self.assertTrue(r.sessions)
        self.assertEqual(self.captured, [(1,10),(2,20),(3,30),(4,40)])

    def test_bad_expression_and_missing_expected_lock_cannot_look_like_success(self):
        case = fixture_case('lock')
        case['sql_a'] = case['sql_a'].replace('quantity+1', 'quantity/0')
        r = self.run_case('lock', case=case)
        self.assertEqual(r.status, 'error', r.reasons)
        self.assertTrue(any(c.sqlstate == '22012' for s in r.sessions for c in s.commands))
        case = fixture_case('lock')
        case['sql_b'] = case['sql_b'].replace('WHERE id=1', 'WHERE id=2')
        r = self.run_case('lock', case=case)
        self.assertEqual(r.status, 'error', r.reasons)
        self.assertTrue(any('блокировка не наблюдалась' in reason for reason in r.reasons))
        self.assertEqual(self.captured, [(1,10),(2,20),(3,30),(4,40)])

    def test_trigger_declines_before_cloning_and_report_contains_no_sql_or_password(self):
        with self.fixture.connect() as c:
            c.execute('CREATE FUNCTION retail.blocked_trigger() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW;END$$')
            c.execute('CREATE TRIGGER user_trigger BEFORE UPDATE ON retail.stock FOR EACH ROW EXECUTE FUNCTION retail.blocked_trigger()')
        r = self.run_case('lock')
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)
        encoded = json.dumps(asdict(r))
        self.assertNotIn('UPDATE retail.stock', encoded)
        if self.settings.password:
            self.assertNotIn(self.settings.password, encoded)
