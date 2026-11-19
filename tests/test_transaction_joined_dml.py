import os
from dataclasses import replace
from unittest import TestCase, skipUnless
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlglot import parse_one

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_joined_dml import validate_joined_dml
from pqo.transaction_replay import run_transaction_replay, result_digest
from pqo.transaction_replay_guard import prepare_replay


class JoinedDMLContractTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname='source', allowed_schemas=frozenset({'retail'}))
        self.columns = {('retail', 'stock'): {'id', 'account_id', 'batch_id', 'quantity', 'label'},
            ('retail', 'adjustments'): {'id', 'account_id', 'batch_id', 'quantity', 'group_id'},
            ('retail', 'groups'): {'id', 'label'}}
        self.keys = {('retail', 'stock'): [('id',)],
            ('retail', 'adjustments'): [('id',), ('account_id', 'batch_id')], ('retail', 'groups'): [('id',)]}

    def program(self, body):
        return prepare_replay('BEGIN;'+body+';SELECT id FROM retail.stock ORDER BY id;COMMIT;', self.settings)

    def validate(self, body, keys=None):
        program = self.program(body)
        tree = parse_one(program.commands[0].sql, read='postgres')
        validate_joined_dml(tree, self.columns, self.keys if keys is None else keys)
        return program

    def test_qualified_aliases_self_join_composite_keys_left_join_and_comma_sources(self):
        for body in (
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t.id=a.id RETURNING t.*,a.quantity',
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t.account_id=a.account_id AND t.batch_id=a.batch_id',
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.stock a WHERE t.id=a.id',
            'UPDATE retail.stock t SET label=g.label FROM retail.adjustments a LEFT JOIN retail.groups g ON g.id=a.group_id WHERE t.id=a.id',
            'UPDATE retail.stock t SET label=g.label FROM retail.adjustments a,retail.groups g WHERE t.id=a.id AND a.group_id=g.id',
            'UPDATE retail.stock AS "T" SET QUANTITY=A.quantity FROM retail.adjustments A WHERE "T".id=a.id',
            'DELETE FROM retail.stock t USING retail.adjustments a WHERE t.id=a.id RETURNING t.id,a.quantity',
            'DELETE FROM retail.stock t USING retail.adjustments a JOIN retail.groups g ON g.id=a.group_id WHERE t.id=a.id RETURNING t.*,g.label',
        ):
            with self.subTest(body=body):
                self.validate(body)

    def test_multiple_matches_delete_only_returns_target_but_update_is_declined(self):
        body = 'DELETE FROM retail.stock t USING retail.adjustments a WHERE t.account_id=a.account_id RETURNING t.*'
        self.validate(body)
        for body in (body.replace('t.*', 'a.quantity'),
                'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t.account_id=a.account_id',
                'UPDATE retail.stock t SET quantity=0 FROM retail.adjustments a WHERE t.account_id=a.account_id'):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, 'единственное совпадение'):
                self.validate(body)

    def test_no_inference_from_or_expressions_partial_keys_cycles_or_left_failure(self):
        for condition in ('t.id=a.id OR t.account_id=a.account_id',
                't.account_id=a.account_id', 't.id=a.id+0'):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                self.validate('UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE '+condition)
        # An ON equality on the nullable side cannot constrain the left side.
        body = '''UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a
            LEFT JOIN retail.groups g ON g.id=a.group_id WHERE t.batch_id=g.id'''
        with self.assertRaisesRegex(ValueError, 'единственное совпадение'):
            self.validate(body)
        # UNIQUE INCLUDE is not a wider unique key; no key also represents
        # deferred, invalid, partial/expression indexes omitted by catalog audit.
        with self.assertRaisesRegex(ValueError, 'единственное совпадение'):
            self.validate('UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t.id=a.id', {})

    def test_unknown_ambiguous_aliased_and_quoted_column_bindings_fail_closed(self):
        for body in (
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE id=a.id',
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE stock.id=a.id',
            'UPDATE retail.stock t SET quantity=a.missing FROM retail.adjustments a WHERE t.id=a.id',
            'UPDATE retail.stock t SET t.quantity=a.quantity FROM retail.adjustments a WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t."ID"=a.id',
            'DELETE FROM retail.stock t USING retail.adjustments a WHERE t.id=a.id RETURNING missing.*',
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.validate(body)

    def test_unsupported_shapes_never_connect(self):
        for body in (
            'UPDATE retail.stock t SET quantity=a.quantity FROM (SELECT * FROM retail.adjustments) a WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments t WHERE t.id=1',
            'UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a RIGHT JOIN retail.groups g ON g.id=a.id WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=1 FROM retail.adjustments a CROSS JOIN retail.groups g WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=1 FROM retail.adjustments a JOIN retail.groups g USING(id) WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=1 FROM retail.adjustments a JOIN retail.groups g ON true WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=1 FROM retail.adjustments a JOIN retail.groups g ON g.id=t.id WHERE t.id=a.id',
            'UPDATE retail.stock t SET quantity=1 FROM LATERAL (SELECT * FROM retail.adjustments) a WHERE t.id=a.id',
            'DELETE FROM retail.stock t USING retail.adjustments a',
            'DELETE FROM retail.stock t USING other.adjustments a WHERE t.id=a.id',
            'DELETE FROM retail.stock t USING retail.adjustments a WHERE t.id=a.id RETURNING random()',
        ):
            with self.subTest(body=body), patch('pqo.transaction_replay.psycopg.connect') as connect, self.assertRaises(ValueError):
                run_transaction_replay('BEGIN;'+body+';SELECT id FROM retail.stock;COMMIT;', self.settings, 'lab',
                    (IndexAction.create('retail', 'stock', ('account_id',)),),
                    test_copy_confirmed=True, disposable_databases_confirmed=True)
            connect.assert_not_called()

    def test_inspection_lists_physical_relations_without_join_fragments(self):
        body = '''UPDATE retail.stock t SET label=g.label FROM retail.adjustments a
            JOIN retail.groups g ON g.id=a.group_id WHERE t.id=a.id'''
        inspection = inspect_transaction('BEGIN;'+body+';COMMIT;')
        self.assertEqual(inspection.statements[0].relation_names, ('retail.adjustments', 'retail.groups', 'retail.stock'))


@skipUnless(os.environ.get('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'Own joined-DML fixture and disposable databases only')
class JoinedDMLPostgresTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings.from_env(), dbname='pqo_joined_source_not_connected', allowed_schemas=frozenset({'retail'}))
        self.reference = 'pqo_joinedfixture_'+uuid4().hex
        with self.connect('postgres', autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.reference)))
            self.owner = c.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (self.reference,)).fetchone()
        self.addCleanup(self.cleanup)
        with self.connect() as c:
            c.execute('CREATE SCHEMA retail')
            c.execute("CREATE TABLE retail.stock(id serial PRIMARY KEY,account_id integer,batch_id integer,quantity integer DEFAULT 0,label text DEFAULT 'new')")
            c.execute("INSERT INTO retail.stock(account_id,batch_id,quantity,label) VALUES(10,1,10,'old'),(20,1,20,'old'),(30,1,30,'old'),(40,2,40,'old')")
            c.execute('CREATE TABLE retail.adjustments(id integer PRIMARY KEY,account_id integer,batch_id integer,quantity integer,group_id integer,UNIQUE(account_id,batch_id))')
            c.execute('INSERT INTO retail.adjustments VALUES(1,10,1,5,1),(2,20,1,7,2),(3,10,2,999,99)')
            c.execute('CREATE TABLE retail.groups(id integer PRIMARY KEY,label text)')
            c.execute("INSERT INTO retail.groups VALUES(1,'one'),(2,'two')")
            c.execute('CREATE TABLE retail.events(id integer PRIMARY KEY,stock_id integer,label text)')
            c.execute("INSERT INTO retail.events VALUES(1,3,'x'),(2,3,'y'),(3,4,'z')")

    def connect(self, database=None, **kwargs):
        return psycopg.connect(**replace(self.settings, dbname=database or self.reference).connection_kwargs(), **kwargs)

    def cleanup(self):
        with self.connect('postgres', autocommit=True) as c:
            if c.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s', (self.reference,)).fetchone() == self.owner:
                c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.reference)))

    def fingerprint(self):
        with self.connect() as c:
            return tuple(tuple(c.execute(sql.SQL('SELECT * FROM retail.{} ORDER BY id').format(sql.Identifier(t))).fetchall())
                for t in ('stock', 'adjustments', 'groups', 'events')) + (c.execute('SELECT last_value,is_called FROM retail.stock_id_seq').fetchone(),)

    def replay(self, text, **kwargs):
        before = self.fingerprint()
        result = run_transaction_replay(text, self.settings, self.reference,
            (IndexAction.create('retail', 'stock', ('account_id', 'batch_id')),),
            test_copy_confirmed=True, disposable_databases_confirmed=True, **kwargs)
        self.assertTrue(result.cleanup_verified, result.reasons)
        self.assertEqual(before, self.fingerprint())
        with self.connect('postgres') as c:
            self.assertFalse(c.execute('SELECT datname FROM pg_catalog.pg_database WHERE datname=ANY(%s)',
                (list(result.created_databases),)).fetchall())
        return result

    def test_joined_commands_observe_prior_writes_and_savepoints_at_all_isolations(self):
        text = '''BEGIN ISOLATION LEVEL {isolation};
            UPDATE retail.adjustments SET quantity=quantity+1 WHERE id=1;
            UPDATE retail.stock t SET quantity=t.quantity+a.quantity,label=g.label
            FROM retail.adjustments a LEFT JOIN retail.groups g ON g.id=a.group_id
            WHERE t.account_id=a.account_id AND t.batch_id=a.batch_id RETURNING t.id,t.quantity,g.label;
            SAVEPOINT gone;
            DELETE FROM retail.stock t USING retail.events e WHERE t.id=e.stock_id AND t.id=3 RETURNING t.id,t.quantity;
            ROLLBACK TO gone; RELEASE gone;
            DELETE FROM retail.stock t USING retail.adjustments a,retail.groups g
            WHERE t.account_id=a.account_id AND t.batch_id=a.batch_id AND g.id=a.group_id AND t.id=2 RETURNING t.id,g.label;
            SELECT id,quantity,label FROM retail.stock ORDER BY id;{ending};'''
        for isolation, ending in (('READ UNCOMMITTED', 'COMMIT'), ('READ COMMITTED', 'COMMIT'),
                ('REPEATABLE READ', 'COMMIT'), ('SERIALIZABLE', 'ROLLBACK')):
            with self.subTest(isolation=isolation):
                r = self.replay(text.format(isolation=isolation, ending=ending))
                self.assertTrue(r.correctness_verified, r.reasons)
                for sample in r.samples:
                    self.assertEqual(sample.commands[1].result_sha256, result_digest([(1,16,'one'),(2,27,'two')], False))
                    self.assertEqual(sample.commands[3].row_count, 1)
                    self.assertEqual(sample.commands[3].result_sha256, result_digest([(3,30)], False))
                    self.assertEqual(sample.commands[6].result_sha256, result_digest([(2,'two')], False))
                    self.assertEqual(sample.commands[7].result_sha256, result_digest([(1,16,'one'),(3,30,'old'),(4,40,'old')], True))
                    self.assertEqual(sample.initial_sequences, sample.final_sequences)
                    if ending == 'ROLLBACK':
                        self.assertEqual(sample.initial_state, sample.final_state)

    def test_composite_unique_required_and_deferred_unique_is_not_a_proof(self):
        body = '''BEGIN;UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a
            WHERE t.account_id=a.account_id{extra};SELECT id,quantity FROM retail.stock ORDER BY id;COMMIT;'''
        r = self.replay(body.format(extra=''))
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)
        self.assertTrue(any('единственное совпадение' in reason for reason in r.reasons))
        with self.connect() as c:
            c.execute('ALTER TABLE retail.adjustments DROP CONSTRAINT adjustments_account_id_batch_id_key')
            c.execute('ALTER TABLE retail.adjustments ADD UNIQUE(account_id,batch_id) DEFERRABLE INITIALLY IMMEDIATE')
        r = self.replay(body.format(extra=' AND t.batch_id=a.batch_id'))
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)

    def test_delete_many_matches_is_allowed_only_with_target_returning(self):
        text = '''BEGIN;DELETE FROM retail.stock t USING retail.events e WHERE t.id=e.stock_id AND t.id=3
            RETURNING {output};SELECT id FROM retail.stock ORDER BY id;COMMIT;'''
        r = self.replay(text.format(output='t.*'))
        self.assertTrue(r.correctness_verified, r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].row_count, 1)
            self.assertEqual(sample.commands[0].result_sha256, result_digest([(3,30,1,30,'old')], False))
        for output in ('t.id,e.label', '*'):
            r = self.replay(text.format(output=output))
            self.assertEqual(r.status, 'declined', r.reasons)
            self.assertFalse(r.created_databases)

    def test_source_unique_include_does_not_silently_prove_a_prefix(self):
        with self.connect() as c:
            c.execute('ALTER TABLE retail.adjustments DROP CONSTRAINT adjustments_account_id_batch_id_key')
            c.execute('CREATE UNIQUE INDEX account_batches ON retail.adjustments(account_id,batch_id) INCLUDE(quantity)')
        r = self.replay('''BEGIN;UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a
            WHERE t.account_id=a.account_id;SELECT id FROM retail.stock;COMMIT;''')
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)

    def test_self_join_defaults_and_sequence_consumption_are_compared(self):
        r = self.replay('''BEGIN;UPDATE retail.stock t SET quantity=a.quantity+1,label=DEFAULT
            FROM retail.stock a WHERE t.id=a.id AND t.id=1 RETURNING t.id,t.quantity,t.label;
            UPDATE retail.stock t SET id=DEFAULT FROM retail.groups g WHERE t.id=g.id AND t.id=1 RETURNING t.id;
            SELECT id,quantity,label FROM retail.stock ORDER BY id;COMMIT;''')
        self.assertTrue(r.correctness_verified, r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].result_sha256, result_digest([(1,11,'new')], False))
            self.assertEqual(sample.commands[1].result_sha256, result_digest([(5,)], False))
            self.assertNotEqual(sample.initial_sequences, sample.final_sequences)

    def test_select_rewrite_and_four_way_comparison_preserve_joined_writes(self):
        from pqo.transaction_optimization import run_transaction_optimization
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites, prepare_select_replacement
        text = '''BEGIN ISOLATION LEVEL REPEATABLE READ;
            UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t.id=a.id AND t.id=1 RETURNING t.id,t.quantity;
            DELETE FROM retail.stock t USING retail.events e WHERE t.id=e.stock_id AND t.id=3 RETURNING t.id;
            SELECT q.label,COUNT(*) AS n,SUM(q.quantity) AS units FROM
                (SELECT label,quantity FROM retail.stock ORDER BY quantity DESC,id) q
            GROUP BY q.label ORDER BY q.label; COMMIT;'''
        rewrite = next(option for option in generate_transaction_select_rewrites(text, self.settings)
            if option.number == 3 and option.rule_id == 'subquery-order-without-limit')
        prepared = prepare_select_replacement(text, self.settings, 3, rewrite.variant_sql)
        self.assertEqual(prepared.original.commands[:2], prepared.variant.commands[:2])
        before = self.fingerprint()
        r = run_transaction_optimization(text, self.settings, self.reference, 3, rewrite.variant_sql,
            (IndexAction.create('retail', 'stock', ('account_id', 'batch_id')),), series=2,
            test_copy_confirmed=True, disposable_databases_confirmed=True)
        self.assertTrue(r.cleanup_verified and r.comparable, r.reasons)
        self.assertEqual(len(r.observations), 14)
        for observation in r.observations:
            sample = observation.sample
            self.assertEqual([command.kind for command in sample.commands], ['update', 'delete', 'select'])
            self.assertEqual(sample.commands[0].result_sha256, result_digest([(1,5)], False))
            self.assertEqual(sample.commands[1].result_sha256, result_digest([(3,)], False))
            self.assertEqual(sample.commands[2].result_sha256, result_digest([('old',3,65)], True))
            self.assertEqual(next(s.rows for s in sample.final_state if s.table == 'retail.stock'), 3)
        self.assertEqual(before, self.fingerprint())

    def test_cancel_after_joined_write_and_sql_error_clean_all_copies(self):
        text = '''BEGIN;UPDATE retail.stock t SET quantity=a.quantity FROM retail.adjustments a WHERE t.id=a.id;
            SELECT id FROM retail.stock ORDER BY id;COMMIT;'''
        control = AnalysisControl(progress=lambda message: control.cancel() if '2 SELECT;' in message else None)
        r = self.replay(text, control=control)
        self.assertEqual(r.status, 'cancelled', r.reasons)
        self.assertFalse(r.correctness_verified)
        self.assertFalse(r.recommendation.recommended)
        text = text.replace('quantity=a.quantity', 'id=1').replace('t.id=a.id;', 't.id=a.id AND t.id=2;')
        r = self.replay(text)
        self.assertEqual(r.status, 'error', r.reasons)
        self.assertTrue(any('23505' in reason for reason in r.reasons))
        self.assertFalse(r.recommendation.recommended)
