from dataclasses import asdict, replace
import json
import os
from unittest import TestCase, skipUnless
from unittest.mock import patch

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_resources import COUNTERS, parse_resources, prepare_resource_request, resource_request_digest, run_transaction_resources


TEXT = '''BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1 RETURNING id,quantity;
    SELECT id,quantity FROM retail.stock WHERE account_id=10 OR account_id=20 ORDER BY id;COMMIT;'''


def document(**root):
    return [{'Plan': {'Node Type': 'Sort', 'Actual Rows': 2, 'Actual Loops': 1,
        **{key: 0 for key in COUNTERS.values()}, **root}, 'Execution Time': 1.25}]


class ResourceContractTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname='source', allowed_schemas=frozenset({'retail'}))

    def test_root_counters_are_inclusive_and_operator_memory_is_a_maximum(self):
        plan = document(**{'Shared Hit Blocks': 12, 'WAL Bytes': 99, 'Sort Space Type': 'Memory', 'Sort Space Used': 64,
            'Plans': [{'Node Type': 'Hash', 'Shared Hit Blocks': 12, 'WAL Bytes': 99, 'Peak Memory Usage': 96,
                'Workers': [{'Peak Memory Usage': 128}]}, {'Node Type': 'Sort', 'Sort Space Type': 'Disk', 'Sort Space Used': 512}]})
        plan[0]['Planning'] = {'Shared Hit Blocks': 1000}
        result = parse_resources(plan)
        self.assertEqual(result.counters['shared_hit_blocks'], 12)
        self.assertEqual(result.counters['wal_bytes'], 99)
        self.assertEqual(result.max_operator_memory_kb, 128)
        self.assertEqual(result.max_sort_disk_kb, 512)
        self.assertEqual(result.output_rows, 2)

    def test_missing_counters_and_memory_are_unknown_not_zero(self):
        r = parse_resources([{'Plan': {'Actual Rows': 0, 'Actual Loops': 1}}])
        self.assertIsNone(r.execution_ms)
        self.assertTrue(all(v is None for v in r.counters.values()))
        self.assertIsNone(r.max_operator_memory_kb)
        self.assertIsNone(r.max_sort_disk_kb)

    def test_malformed_or_negative_metrics_are_never_accepted(self):
        for data in ([], {}, [{'Plan': {}}], document(**{'Actual Loops': 2}), document(**{'Actual Loops': True}), document(**{'Actual Rows': True}),
                document(**{'Shared Hit Blocks': -1}), document(**{'WAL Bytes': 1.5}),
                document(**{'Peak Memory Usage': -1}), document(**{'Plans': {}}),
                [{'Plan': {'Actual Rows': 1,'Actual Loops':1},'Execution Time':float('nan')} ]):
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_resources(data)

    def test_contract_and_confirmation_reject_before_connecting(self):
        for text, ref, confirmed in ((TEXT, 'source', True), (TEXT, 'baseline', False),
                (TEXT.replace('quantity+1','random()'), 'baseline', True)):
            with self.subTest(text=text), patch('pqo.transaction_resources.psycopg.connect') as connect:
                with self.assertRaises(ValueError):
                    run_transaction_resources(text, self.settings, ref, test_copy_confirmed=confirmed, disposable_databases_confirmed=confirmed)
                connect.assert_not_called()
        for work_mem in (True, 0, 63, 65537, 1.5):
            with self.assertRaises(ValueError):
                prepare_resource_request(TEXT, self.settings, work_mem_kb=work_mem)
        with self.assertRaises(ValueError):
            prepare_resource_request(TEXT, self.settings, number=2)

    def test_provenance_and_prepared_variant_keep_other_commands(self):
        variant = 'SELECT id,quantity FROM retail.stock WHERE account_id IN(10,20) ORDER BY id'
        action = IndexAction.create('retail','stock',('account_id',))
        prepared = prepare_resource_request(TEXT, self.settings, (action,), 2, variant)
        self.assertEqual([p[0] for p in prepared], ['original','combined'])
        self.assertEqual(prepared[0][1].commands[0], prepared[1][1].commands[0])
        base = resource_request_digest(TEXT, self.settings, 'baseline')
        for value in (resource_request_digest(TEXT,self.settings,'other'),
                resource_request_digest(TEXT,self.settings,'baseline',work_mem_kb=64),
                resource_request_digest(TEXT,self.settings,'baseline',(action,)),
                resource_request_digest(TEXT,self.settings,'baseline',number=2,variant_sql=variant)):
            self.assertNotEqual(base, value)


@skipUnless(os.getenv('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'requires disposable PostgreSQL laboratory')
class ResourcePostgresTests(TestCase):
    def setUp(self):
        from test_transaction_joined_dml import JoinedDMLPostgresTests
        self.fixture = JoinedDMLPostgresTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.settings, self.reference = self.fixture.settings, self.fixture.reference

    def run_profile(self, text=TEXT, **kwargs):
        before = self.fixture.fingerprint()
        r = run_transaction_resources(text, self.settings, self.reference,
            test_copy_confirmed=True, disposable_databases_confirmed=True, **kwargs)
        self.assertTrue(r.cleanup_verified, r.reasons)
        self.assertEqual(before, self.fixture.fingerprint())
        with self.fixture.connect('postgres') as c:
            self.assertFalse(c.execute('SELECT datname FROM pg_catalog.pg_database WHERE datname=ANY(%s)',(list(r.created_databases),)).fetchall())
        return r

    def test_profile_runs_each_write_once_and_matches_control_data(self):
        r = self.run_profile(work_mem_kb=64)
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual(len(r.created_databases), 3)
        self.assertTrue(r.profiles_match_controls)
        self.assertIsNone(r.normal_variants_equivalent)
        v = r.variants[0]
        self.assertEqual(v.profile.block_size, 8192)
        self.assertEqual(v.profile.work_mem_kb, 64)
        self.assertGreater(v.profile.counters['wal_bytes'], 0)
        self.assertGreater(v.profile.counters['shared_hit_blocks'], 0)
        self.assertTrue(all(c.output_count_checked for c in v.profile.commands))
        self.assertFalse(v.profile.result_values_verified)
        self.assertEqual(v.profile.final_state, v.control.final_state)
        encoded = json.dumps(asdict(r))
        self.assertNotIn('UPDATE retail.stock', encoded)
        self.assertNotIn('Index Cond', encoded)

    def test_index_and_rewrite_comparison_preserves_normal_result_verification(self):
        variant = 'SELECT id,quantity FROM retail.stock WHERE account_id IN(10,20) ORDER BY id'
        r = self.run_profile(actions=(IndexAction.create('retail','stock',('account_id',)),), number=2, variant_sql=variant)
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertEqual(len(r.created_databases), 5)
        self.assertTrue(r.normal_variants_equivalent and r.profiles_match_controls)
        self.assertEqual([v.kind for v in r.variants], ['original','combined'])
        self.assertEqual(r.variants[0].profile.index_size_bytes, 0)
        self.assertGreater(r.variants[1].profile.index_size_bytes, 0)

    def test_wrong_manual_variant_stops_before_any_instrumented_execution(self):
        r = self.run_profile(number=2, variant_sql='SELECT id,quantity FROM retail.stock WHERE account_id=30 ORDER BY id')
        self.assertEqual(r.status, 'mismatch', r.reasons)
        self.assertFalse(r.normal_variants_equivalent)
        self.assertFalse(r.profiles_match_controls)
        self.assertTrue(all(v.profile is None for v in r.variants))
        self.assertEqual(len(r.created_databases), 3)

    def test_profile_row_count_mismatch_is_not_reported_as_verified(self):
        def altered(raw):
            result = parse_resources(raw)
            return replace(result, output_rows=result.output_rows + 1)
        with patch('pqo.transaction_resources.parse_resources', side_effect=altered):
            r = self.run_profile()
        self.assertEqual(r.status, 'mismatch', r.reasons)
        self.assertFalse(r.profiles_match_controls)
        self.assertTrue(r.variants[0].profile.states_match_control)
        self.assertFalse(r.variants[0].profile.output_counts_match_control)

    def test_savepoints_rollback_serial_defaults_and_all_isolations_match(self):
        text = '''BEGIN ISOLATION LEVEL {level};
            INSERT INTO retail.stock(account_id,batch_id,quantity) VALUES(90,3,9) RETURNING id,quantity;
            SAVEPOINT discarded;
            UPDATE retail.stock SET quantity=quantity+10 WHERE id=1;
            SELECT id,quantity FROM retail.stock ORDER BY id;
            ROLLBACK TO discarded;RELEASE discarded;
            SELECT id,quantity FROM retail.stock ORDER BY id;{ending};'''
        for level, ending in (('READ UNCOMMITTED','COMMIT'),('READ COMMITTED','COMMIT'),
                ('REPEATABLE READ','COMMIT'),('SERIALIZABLE','ROLLBACK')):
            with self.subTest(level=level):
                r = self.run_profile(text.format(level=level, ending=ending))
                self.assertEqual(r.status, 'completed', r.reasons)
                p = r.variants[0].profile
                self.assertEqual(p.transaction_options.isolation_level, level)
                self.assertGreater(p.counters['wal_bytes'], 0)
                self.assertIsNone(p.commands[1].resources)
                self.assertFalse(p.commands[2].output_count_checked)
                self.assertNotEqual(p.initial_sequences, p.final_sequences)
                if ending == 'ROLLBACK':
                    self.assertEqual(p.initial_state, p.final_state)

    def test_cte_insert_select_upsert_returning_has_matching_output_counts(self):
        r = self.run_profile('''BEGIN;
            WITH q AS(SELECT id,account_id,batch_id,quantity FROM retail.adjustments WHERE id<=2)
            INSERT INTO retail.stock AS target(id,account_id,batch_id,quantity)
            SELECT id,account_id,batch_id,quantity FROM q ORDER BY id
            ON CONFLICT(id) DO UPDATE SET quantity=EXCLUDED.quantity RETURNING id,quantity;
            SELECT id,quantity FROM retail.stock ORDER BY id;COMMIT;''')
        self.assertEqual(r.status, 'completed', r.reasons)
        v = r.variants[0]
        self.assertEqual(v.control.commands[0].row_count, 2)
        self.assertEqual(v.profile.commands[0].resources.output_rows, 2)
        self.assertTrue(v.profile.output_counts_match_control)
        self.assertEqual(v.profile.initial_sequences, v.profile.final_sequences)

    def test_disk_spill_and_operator_memory_are_collected_without_global_counters(self):
        with self.fixture.connect() as c:
            c.execute('CREATE TABLE retail.resource_rows(id integer PRIMARY KEY, grouping integer, payload text)')
            c.execute("INSERT INTO retail.resource_rows SELECT i,i%700,repeat(md5(i::text),4) FROM generate_series(1,35000) i")
        text = '''BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;
            SELECT grouping,COUNT(*) FROM retail.resource_rows GROUP BY grouping ORDER BY grouping;
            SELECT id,payload FROM retail.resource_rows ORDER BY payload,id LIMIT 8000;COMMIT;'''
        r = self.run_profile(text, work_mem_kb=64)
        self.assertEqual(r.status, 'completed', r.reasons)
        p = r.variants[0].profile
        self.assertGreater(p.counters['temp_written_blocks'], 0)
        self.assertGreater(p.counters['temp_read_blocks'], 0)
        self.assertGreater(p.max_sort_disk_kb or 0, 0)
        self.assertTrue(p.states_match_control)
        # A larger limit is a separate run with the same setting in control/profile.
        r = self.run_profile(text, work_mem_kb=65536)
        self.assertEqual(r.status, 'completed', r.reasons)
        self.assertGreater(r.variants[0].profile.max_operator_memory_kb or 0, 0)

    def test_cancel_profile_and_invalid_catalog_never_leave_owned_databases(self):
        control = AnalysisControl(progress=lambda msg: control.cancel() if 'Профиль ресурсов: 2 ' in msg else None)
        r = self.run_profile(control=control)
        self.assertEqual(r.status, 'cancelled', r.reasons)
        self.assertFalse(r.profiles_match_controls)
        bad = TEXT.replace('quantity+1','quantity/0')
        r = self.run_profile(bad)
        self.assertEqual(r.status, 'error', r.reasons)
        self.assertTrue(any('22012' in reason for reason in r.reasons))
        with self.fixture.connect() as c:
            c.execute('CREATE FUNCTION retail.blocked_trigger() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW;END$$')
            c.execute('CREATE TRIGGER user_trigger BEFORE UPDATE ON retail.stock FOR EACH ROW EXECUTE FUNCTION retail.blocked_trigger()')
        r = self.run_profile()
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)
