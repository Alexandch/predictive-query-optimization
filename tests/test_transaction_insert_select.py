import os
from dataclasses import replace
from unittest import TestCase,skipUnless
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_defaults import used_defaults
from pqo.transaction_parameters import bind_transaction_parameters,ParameterValue
from pqo.transaction_replay_guard import prepare_replay
from pqo.transaction_replay import run_transaction_replay,result_digest


class InsertSelectContractTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings(),dbname='source',allowed_schemas=frozenset({'retail'}))

    def program(self,body):
        return prepare_replay('BEGIN;'+body+';SELECT id FROM retail.stock;COMMIT;',self.settings)

    def test_flat_sources_aggregates_constants_self_reads_and_source_index_candidates(self):
        for body in (
            'INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming',
            'INSERT INTO retail.stock(sku,quantity) SELECT i.sku,i.quantity FROM retail.incoming i JOIN retail.groups g ON g.id=i.group_id ORDER BY i.id',
            'INSERT INTO retail.stock(sku,quantity) SELECT i.sku,SUM(i.quantity) FROM retail.incoming i LEFT JOIN retail.groups g ON g.id=i.group_id GROUP BY i.sku HAVING COUNT(*)>0 ORDER BY i.sku',
            "INSERT INTO retail.stock(sku,quantity) SELECT 'fixed',3",
            'INSERT INTO retail.stock(id,sku,quantity,label) SELECT * FROM retail.stock ON CONFLICT(id) DO NOTHING',
            'INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming ORDER BY id LIMIT 10',
        ):
            with self.subTest(body=body):self.assertEqual(self.program(body).commands[0].kind,'insert')
        from pqo.transaction_indexes import _accesses,_writes
        from sqlglot import parse_one
        from types import SimpleNamespace
        tree=parse_one('INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming WHERE quantity=7',read='postgres')
        accesses=_accesses(tree,1,{('retail','incoming'):SimpleNamespace(columns=('id','sku','quantity'))},set())
        self.assertEqual([(a.relation,a.equalities) for a in accesses],[(('retail','incoming'),['quantity'])])
        self.assertEqual([item[2] for item in _writes({1:tree})],[('retail','stock')])

    def test_unsupported_sources_and_effects_are_rejected_offline(self):
        head='INSERT INTO retail.stock(sku,quantity) '
        for source in (
            'SELECT sku,quantity FROM incoming',
            'SELECT sku,quantity FROM other.incoming',
            'SELECT sku,quantity FROM (SELECT sku,quantity FROM retail.incoming) q',
            'WITH q AS (SELECT sku,quantity FROM retail.incoming) SELECT * FROM q',
            'SELECT sku,quantity FROM retail.incoming UNION ALL SELECT sku,quantity FROM retail.incoming',
            'SELECT sku,(SELECT quantity FROM retail.incoming) FROM retail.incoming',
            'SELECT i.sku,i.quantity FROM retail.incoming i CROSS JOIN retail.groups g',
            'SELECT i.sku,i.quantity FROM retail.incoming i RIGHT JOIN retail.groups g ON g.id=i.group_id',
            'SELECT sku,quantity FROM retail.incoming FOR UPDATE',
            'SELECT sku,quantity FROM retail.incoming LIMIT 2',
            'SELECT sku,DEFAULT FROM retail.incoming',
            'SELECT sku,random() FROM retail.incoming',
            "SELECT sku,nextval('retail.stock_id_seq') FROM retail.incoming",
            'SELECT sku,quantity FROM retail.incoming i(id,sku,quantity)',
            'SELECT sku,quantity FROM retail.incoming TABLESAMPLE SYSTEM(1)',
        ):
            with self.subTest(source=source),patch('pqo.transaction_replay.psycopg.connect') as connect,self.assertRaises(ValueError):
                run_transaction_replay('BEGIN;'+head+source+';SELECT id FROM retail.stock;COMMIT;',self.settings,'lab',
                    (IndexAction.create('retail','incoming',('quantity',)),),test_copy_confirmed=True,disposable_databases_confirmed=True)
            connect.assert_not_called()

    def test_projection_width_expands_stars_and_keeps_default_null_distinction(self):
        columns=('id','sku','quantity','label')
        metadata={('retail','incoming'):('sku','quantity'),('retail','groups'):('label',)}
        cases=(('INSERT INTO retail.stock(sku,quantity) SELECT i.* FROM retail.incoming i',{'id','label'}),
            ('INSERT INTO retail.stock(sku,quantity,label) SELECT * FROM retail.incoming i JOIN retail.groups g ON i.quantity=g.label',{'id'}),
            ('INSERT INTO retail.stock(sku,quantity,label) SELECT sku,quantity,NULL FROM retail.incoming',{'id'}),
            ('INSERT INTO retail.stock SELECT 1,sku,quantity FROM retail.incoming',{'label'}),
            ('INSERT INTO retail.stock AS t(sku,quantity) SELECT sku,quantity FROM retail.incoming ON CONFLICT(sku) DO UPDATE SET label=DEFAULT',{'id','label'}))
        for body,expected in cases:
            with self.subTest(body=body):self.assertEqual(used_defaults(self.program(body),'retail','stock',columns,source_columns=metadata),expected)
        for body in ('INSERT INTO retail.stock(sku) SELECT * FROM retail.incoming',
                'INSERT INTO retail.stock(sku,quantity) SELECT sku FROM retail.incoming',
                'INSERT INTO retail.stock(missing) SELECT sku FROM retail.incoming',
                'INSERT INTO retail.stock(sku) SELECT unknown.* FROM retail.incoming'):
            with self.subTest(body=body),self.assertRaises(ValueError):
                used_defaults(self.program(body),'retail','stock',columns,source_columns=metadata)

    def test_parameter_binding_and_select_replacement_preserve_insert_source(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,prepare_select_replacement
        text='''BEGIN ISOLATION LEVEL REPEATABLE READ;SAVEPOINT before_insert;
            INSERT INTO retail.stock AS t(sku,quantity) SELECT sku,quantity FROM retail.incoming
            WHERE quantity>$1 ORDER BY id ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity;
            SELECT id FROM retail.stock WHERE quantity=5 OR quantity=7 ORDER BY id;COMMIT;'''
        bound=bind_transaction_parameters(text,(ParameterValue(2,1,'integer','3'),))
        option=next(x for x in generate_transaction_select_rewrites(bound.sql_text,self.settings) if x.rule_id=='or-equality-to-in')
        replacement=prepare_select_replacement(bound.sql_text,self.settings,3,option.variant_sql)
        self.assertEqual(replacement.original.commands[:2],replacement.variant.commands[:2])
        self.assertEqual(replacement.original.options,replacement.variant.options)
        self.assertEqual(replacement.original.tables,(('retail','incoming'),('retail','stock')))


@skipUnless(os.environ.get('PQO_DISPOSABLE_REPLAY_TESTS')=='1','Explicit opt-in: own fixture and tracked disposable databases only')
class InsertSelectPostgresTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings.from_env(),dbname='pqo_insert_select_source_not_connected',allowed_schemas=frozenset({'retail'}))
        self.reference='pqo_insertfixture_'+uuid4().hex
        with self.connect('postgres',autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.reference)))
            self.owner=c.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()
        self.addCleanup(self.cleanup)
        with self.connect() as c:
            c.execute('CREATE SCHEMA retail')
            c.execute('CREATE TABLE retail.incoming(id integer PRIMARY KEY,sku text,quantity integer,group_id integer)')
            c.execute("INSERT INTO retail.incoming VALUES(1,'A',5,1),(2,'B',7,2),(3,'C',9,99)")
            c.execute('CREATE TABLE retail.groups(id integer PRIMARY KEY,label text)')
            c.execute("INSERT INTO retail.groups VALUES(1,'one'),(2,'two')")
            c.execute("CREATE TABLE retail.stock(id serial PRIMARY KEY,sku text UNIQUE,quantity integer DEFAULT 0,label text DEFAULT 'new')")
            c.execute("INSERT INTO retail.stock(sku,quantity,label) VALUES('A',10,'old')")

    def connect(self,database=None,**kwargs):
        return psycopg.connect(**replace(self.settings,dbname=database or self.reference).connection_kwargs(),**kwargs)

    def cleanup(self):
        with self.connect('postgres',autocommit=True) as c:
            if c.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()==self.owner:
                c.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.reference)))

    def fingerprint(self):
        with self.connect() as c:
            return (tuple(c.execute('SELECT * FROM retail.incoming ORDER BY id').fetchall()),
                tuple(c.execute('SELECT * FROM retail.groups ORDER BY id').fetchall()),
                tuple(c.execute('SELECT * FROM retail.stock ORDER BY id').fetchall()),
                c.execute('SELECT last_value,is_called FROM retail.stock_id_seq').fetchone())

    def replay(self,text,**kwargs):
        before=self.fingerprint()
        tables=prepare_replay(text,self.settings).tables
        table='incoming' if ('retail','incoming') in tables else 'stock'
        r=run_transaction_replay(text,self.settings,self.reference,(IndexAction.create('retail',table,('quantity',)),),
            test_copy_confirmed=True,disposable_databases_confirmed=True,**kwargs)
        self.assertTrue(r.cleanup_verified,r.reasons)
        self.assertEqual(before,self.fingerprint())
        return r

    def test_join_source_sees_prior_updates_and_savepoint_commit_rollback_match(self):
        text='''BEGIN ISOLATION LEVEL REPEATABLE READ;
            UPDATE retail.incoming SET quantity=quantity+1 WHERE id=1;
            INSERT INTO retail.stock AS target(sku,quantity,label)
            SELECT i.sku,i.quantity,g.label FROM retail.incoming i LEFT JOIN retail.groups g ON g.id=i.group_id
            WHERE i.id BETWEEN 1 AND 3 ORDER BY i.id
            ON CONFLICT(sku) DO UPDATE SET quantity=target.quantity+excluded.quantity,label=excluded.label RETURNING id,sku,quantity,label;
            SAVEPOINT discarded;
            INSERT INTO retail.stock(sku,quantity) SELECT 'discarded',SUM(quantity) FROM retail.incoming RETURNING id,quantity;
            ROLLBACK TO discarded;RELEASE discarded;
            SELECT id,sku,quantity,label FROM retail.stock ORDER BY id;COMMIT;'''
        for ending in ('COMMIT','ROLLBACK'):
            with self.subTest(ending=ending):
                r=self.replay(text.replace('COMMIT;',ending+';'))
                self.assertTrue(r.correctness_verified,r.reasons)
                for sample in r.samples:
                    expected=[(1,'A',16,'one'),(3,'B',7,'two'),(4,'C',9,None)]
                    self.assertEqual(sample.commands[1].result_sha256,result_digest(expected,False))
                    self.assertEqual(sample.commands[3].result_sha256,result_digest([(5,22)],False))
                    self.assertEqual(sample.commands[6].result_sha256,result_digest(expected,True))
                    if ending=='ROLLBACK':self.assertEqual(sample.initial_state,sample.final_state)
                    self.assertNotEqual(sample.initial_sequences,sample.final_sequences)

    def test_stars_implicit_columns_and_self_insert_use_catalog_column_order(self):
        text='''BEGIN;INSERT INTO retail.stock SELECT id+100,sku,quantity,'copied' FROM retail.incoming WHERE id=2;
            INSERT INTO retail.stock(sku,quantity,label) SELECT 'summary',COUNT(*),NULL FROM retail.stock;
            INSERT INTO retail.stock SELECT s.* FROM retail.stock s ORDER BY s.id ON CONFLICT(id) DO NOTHING;
            SELECT id,sku,quantity,label FROM retail.stock ORDER BY id;COMMIT;'''
        r=self.replay(text)
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[2].row_count,0)
            self.assertEqual(sample.commands[3].result_sha256,result_digest([(1,'A',10,'old'),(2,'summary',2,None),(102,'B',7,'copied')],True))

    def test_explicit_null_bypasses_target_default_but_omission_does_not(self):
        with self.connect() as c:c.execute('ALTER TABLE retail.stock ALTER COLUMN label SET DEFAULT now()::text')
        good='''BEGIN;INSERT INTO retail.stock(sku,quantity,label) SELECT sku,quantity,NULL FROM retail.incoming
            WHERE id=2 ORDER BY id;SELECT label FROM retail.stock WHERE sku='B';COMMIT;'''
        r=self.replay(good)
        self.assertTrue(r.correctness_verified,r.reasons)
        self.assertEqual(r.samples[0].commands[1].result_sha256,result_digest([(None,)],False))
        bad="BEGIN;INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming WHERE false;SELECT id FROM retail.stock;COMMIT;"
        r=self.replay(bad)
        self.assertEqual(r.status,'declined',r.reasons)
        self.assertFalse(r.created_databases)

    def test_catalog_expansion_of_source_star_and_self_insert_append_only_snapshot_rows(self):
        text='''BEGIN;INSERT INTO retail.stock(quantity,label) SELECT g.* FROM retail.groups g ORDER BY g.id;
            INSERT INTO retail.stock(sku,quantity) SELECT CAST(id+100 AS TEXT),quantity FROM retail.stock WHERE id=1;
            SELECT id,sku,quantity,label FROM retail.stock ORDER BY id;COMMIT;'''
        r=self.replay(text)
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].row_count,2)
            self.assertEqual(sample.commands[1].row_count,1)
            self.assertEqual(sample.commands[2].result_sha256,
                result_digest([(1,'A',10,'old'),(2,None,1,'one'),(3,None,2,'two'),(4,'101',10,'new')],True))

    def test_empty_source_does_not_consume_default_sequence(self):
        r=self.replay('''BEGIN;INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming WHERE false RETURNING id;
            SELECT COUNT(*) FROM retail.stock;COMMIT;''')
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].row_count,0)
            self.assertEqual(sample.initial_sequences,sample.final_sequences)
            self.assertEqual(sample.initial_state,sample.final_state)

    def test_invalid_projection_or_star_fails_catalog_audit_before_copy_creation(self):
        for source in ('SELECT sku FROM retail.incoming','SELECT missing.* FROM retail.incoming',
                'SELECT * FROM retail.incoming'):
            with self.subTest(source=source):
                r=self.replay('BEGIN;INSERT INTO retail.stock(sku,quantity) '+source+';SELECT id FROM retail.stock;COMMIT;')
                self.assertEqual(r.status,'declined',r.reasons)
                self.assertFalse(r.created_databases)

    def test_source_unique_violation_is_not_split_or_silently_deduplicated(self):
        with self.connect() as c:c.execute("INSERT INTO retail.incoming VALUES(4,'A',2,1)")
        text='''BEGIN;INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming WHERE sku='A' ORDER BY id
            ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity;SELECT id FROM retail.stock;COMMIT;'''
        r=self.replay(text)
        self.assertEqual(r.status,'error',r.reasons)
        self.assertTrue(any('21000' in reason for reason in r.reasons))
        self.assertFalse(r.recommendation.recommended)

    def test_source_order_change_is_detected_in_final_id_to_data_mapping(self):
        with self.connect() as c:
            c.execute('DELETE FROM retail.incoming')
            c.execute("INSERT INTO retail.incoming SELECT i,'item-'||i,5001-i,1 FROM generate_series(1,5000) i")
            c.execute('ANALYZE retail.incoming')
        from pqo.transaction_replay import _kwargs,PREFIX
        def forced(settings,database):
            args=_kwargs(settings,database)
            if database.startswith(PREFIX):args['options']+=' -c enable_seqscan=off -c enable_bitmapscan=off'
            return args
        text='''BEGIN;INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming
            WHERE quantity BETWEEN 1 AND 5 RETURNING sku,quantity;
            SELECT sku,quantity FROM retail.stock ORDER BY sku;COMMIT;'''
        with patch('pqo.transaction_replay._kwargs',side_effect=forced):r=self.replay(text)
        self.assertEqual(r.status,'completed',r.reasons)
        self.assertTrue(r.results_equal,'Business projections match while generated IDs attach to different rows')
        self.assertFalse(r.final_states_equal)
        self.assertFalse(r.correctness_verified)
        self.assertFalse(r.recommendation.recommended)

    def test_four_isolation_levels_and_cancel_after_bulk_write(self):
        for level in ('READ UNCOMMITTED','READ COMMITTED','REPEATABLE READ','SERIALIZABLE'):
            with self.subTest(level=level):
                text=f'''BEGIN ISOLATION LEVEL {level};INSERT INTO retail.stock(sku,quantity)
                    SELECT sku,quantity FROM retail.incoming WHERE id>1 ORDER BY id;
                    SELECT id FROM retail.stock ORDER BY id;COMMIT;'''
                r=self.replay(text)
                self.assertTrue(r.correctness_verified,r.reasons)
                self.assertTrue(all(s.transaction_options.isolation_level==level for s in r.samples))
        control=AnalysisControl()
        control.progress=lambda message:control.cancel() if message.startswith('Последовательное выполнение: 2') else None
        r=self.replay(text,control=control)
        self.assertEqual(r.status,'cancelled',r.reasons)
        self.assertFalse(r.recommendation.recommended)

    def test_affected_row_limit_does_not_silently_truncate_source_query(self):
        with self.connect() as c:
            c.execute('DELETE FROM retail.incoming')
            c.execute("INSERT INTO retail.incoming SELECT i,'item-'||i,i,1 FROM generate_series(1,10001) i")
        r=self.replay('''BEGIN;INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming ORDER BY id;
            SELECT id FROM retail.stock;COMMIT;''')
        self.assertEqual(r.status,'declined',r.reasons)
        self.assertTrue(any('10000' in reason for reason in r.reasons))
        self.assertFalse(r.correctness_verified)

    def test_standalone_select_rewrite_keeps_insert_select_and_effects(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        text='''BEGIN;INSERT INTO retail.stock(sku,quantity) SELECT sku,quantity FROM retail.incoming WHERE id>1 ORDER BY id;
            SELECT id,sku,quantity FROM retail.stock WHERE quantity=7 OR quantity=9 ORDER BY id;COMMIT;'''
        option=next(x for x in generate_transaction_select_rewrites(text,self.settings) if x.rule_id=='or-equality-to-in')
        before=self.fingerprint()
        r=run_transaction_select_rewrite(text,self.settings,self.reference,2,option.variant_sql,series=1,
            test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
        self.assertTrue(r.correctness_verified,r.reasons)
        self.assertTrue(r.cleanup_verified)
        self.assertEqual(r.samples[0].commands[1].result_sha256,result_digest([(2,'B',7),(3,'C',9)],True))
        self.assertEqual(before,self.fingerprint())
