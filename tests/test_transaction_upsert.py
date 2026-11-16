import os
from dataclasses import replace
from unittest import TestCase,skipUnless
from uuid import uuid4

import psycopg
from psycopg import sql

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_defaults import used_defaults
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_parameters import bind_transaction_parameters,ParameterValue
from pqo.transaction_replay_guard import prepare_replay
from pqo.transaction_replay import run_transaction_replay,result_digest


class UpsertContractTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings(),allowed_schemas=frozenset({'retail'}))

    def prepare(self,body):
        return prepare_replay('BEGIN;'+body+';SELECT id FROM retail.stock;COMMIT;',self.settings)

    def test_supported_arbiters_aliases_excluded_default_and_nothing(self):
        for body in (
            'INSERT INTO retail.stock(sku) VALUES(\'A\') ON CONFLICT DO NOTHING',
            'INSERT INTO retail.stock(sku) VALUES(\'A\') ON CONFLICT(sku) DO NOTHING',
            'INSERT INTO retail.stock(sku) VALUES(\'A\') ON CONFLICT ON CONSTRAINT stock_sku_key DO NOTHING',
            'INSERT INTO retail.stock AS t(sku,quantity) VALUES(\'A\',2) ON CONFLICT(sku) DO UPDATE SET quantity=t.quantity+EXCLUDED.quantity WHERE t.quantity<EXCLUDED.quantity',
            'INSERT INTO retail.stock(sku) VALUES(\'A\') ON CONFLICT(sku) DO UPDATE SET label=DEFAULT',
            'INSERT INTO retail.stock(sku) VALUES(\'A\') ON CONFLICT(sku,active) DO UPDATE SET quantity=excluded.quantity',
        ):
            with self.subTest(body=body):self.assertEqual(self.prepare(body).commands[0].kind,'insert')

    def test_unsupported_conflict_forms_fail_before_connecting(self):
        head="INSERT INTO retail.stock(sku,quantity) VALUES('A',2) "
        for suffix in (
            'ON CONFLICT DO UPDATE SET quantity=3',
            'ON CONFLICT((quantity+1)) DO NOTHING',
            'ON CONFLICT(sku COLLATE "C") DO NOTHING',
            'ON CONFLICT(sku text_ops) DO NOTHING',
            'ON CONFLICT(sku) WHERE active DO NOTHING',
            'ON CONFLICT(sku,sku) DO NOTHING',
            'ON CONFLICT(sku DESC) DO NOTHING',
            'ON CONFLICT(sku) DO UPDATE SET (quantity,label)=(2,\'x\')',
            'ON CONFLICT(sku) DO UPDATE SET stock.quantity=2',
            'ON CONFLICT(sku) DO UPDATE SET quantity=1,quantity=2',
            'ON CONFLICT(sku) DO UPDATE SET quantity=(SELECT quantity FROM retail.stock)',
            'ON CONFLICT(sku) DO UPDATE SET quantity=other.quantity',
            'ON CONFLICT(sku) DO UPDATE SET quantity=retail.stock.quantity',
            'ON CONFLICT(sku) DO UPDATE SET quantity=random()',
            'ON CONFLICT(sku) DO UPDATE SET quantity=DEFAULT+1',
            'ON CONFLICT(sku) DO UPDATE SET quantity=nextval(\'retail.stock_id_seq\')',
        ):
            with self.subTest(suffix=suffix),self.assertRaises(ValueError):self.prepare(head+suffix)
        with self.assertRaises(ValueError):
            self.prepare("INSERT INTO retail.stock AS excluded(sku) VALUES('A') ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity")

    def test_alias_column_list_and_conflict_defaults_are_audited_together(self):
        p=self.prepare("INSERT INTO retail.stock AS t(sku,label) VALUES('A','explicit') ON CONFLICT(sku) DO UPDATE SET label=DEFAULT")
        self.assertEqual(used_defaults(p,'retail','stock',('id','sku','quantity','label')),{'id','quantity','label'})
        with self.assertRaises(ValueError):
            used_defaults(self.prepare("INSERT INTO retail.stock AS t(sku,missing) VALUES('A',1) ON CONFLICT DO NOTHING"),
                'retail','stock',('id','sku','quantity','label'))

    def test_default_values_rendering_survives_inspection_binding_and_rewrite(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,prepare_select_replacement
        text='''BEGIN; INSERT INTO retail.stock DEFAULT VALUES ON CONFLICT(sku)
            DO UPDATE SET quantity=stock.quantity+1 RETURNING id,quantity;
            SELECT id FROM retail.stock WHERE id=$1 OR id=$2 ORDER BY id;COMMIT;'''
        bound=bind_transaction_parameters(text,(ParameterValue(2,1,'integer','1'),ParameterValue(2,2,'integer','2')))
        option=next(item for item in generate_transaction_select_rewrites(bound.sql_text,self.settings) if item.rule_id=='or-equality-to-in')
        replacement=prepare_select_replacement(bound.sql_text,self.settings,2,option.variant_sql)
        for p in (self.prepare('INSERT INTO retail.stock DEFAULT VALUES ON CONFLICT DO NOTHING RETURNING id'),
                replacement.original,replacement.variant):
            sql_text=p.commands[0].sql
            self.assertLess(sql_text.index('DEFAULT VALUES'),sql_text.index('ON CONFLICT'))
            self.assertEqual(inspect_transaction('BEGIN;'+sql_text+';COMMIT;').statements[0].kind,'insert')

    def test_structural_write_inventory_includes_conditional_update_columns(self):
        from sqlglot import parse_one
        from pqo.transaction_indexes import _writes
        tree=parse_one("INSERT INTO retail.stock(sku) VALUES('A') ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity,label=DEFAULT",read='postgres')
        writes=_writes({1:tree})
        self.assertEqual([(kind,columns) for _,kind,_,columns in writes],[('insert',set()),('update',{'quantity','label'})])


@skipUnless(os.environ.get('PQO_DISPOSABLE_REPLAY_TESTS')=='1','Explicit opt-in: own fixture and tracked disposable copies only')
class UpsertPostgresTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings.from_env(),dbname='pqo_upsert_source_not_connected',allowed_schemas=frozenset({'retail'}))
        self.reference='pqo_upsertfixture_'+uuid4().hex
        with self.connect('postgres',autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.reference)))
            self.owner=connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()
        self.addCleanup(self.cleanup)
        with self.connect() as connection:
            connection.execute('CREATE SCHEMA retail')
            connection.execute("""CREATE TABLE retail.stock(id serial PRIMARY KEY,sku text NOT NULL UNIQUE DEFAULT 'A',
                quantity integer DEFAULT 0,label text DEFAULT 'new',active boolean DEFAULT true,nullable_key integer UNIQUE)""")
            connection.execute("INSERT INTO retail.stock(sku,quantity,label) VALUES('A',10,'old'),('B',20,'old')")

    def connect(self,database=None,**kwargs):
        return psycopg.connect(**replace(self.settings,dbname=database or self.reference).connection_kwargs(),**kwargs)

    def cleanup(self):
        with self.connect('postgres',autocommit=True) as connection:
            if connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()==self.owner:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.reference)))

    def fingerprint(self):
        with self.connect(autocommit=True) as connection:
            # Own fixed fixture, including intentionally unsupported catalogs:
            # fingerprinting must not depend on the guard accepting the schema.
            rows=tuple(connection.execute('SELECT * FROM retail.stock ORDER BY id').fetchall())
            sequence=connection.execute('SELECT last_value,is_called FROM retail.stock_id_seq').fetchone()
            return rows,sequence

    def replay(self,text,**kwargs):
        before=self.fingerprint()
        r=run_transaction_replay(text,self.settings,self.reference,(IndexAction.create('retail','stock',('quantity',)),),
            test_copy_confirmed=True,disposable_databases_confirmed=True,**kwargs)
        self.assertTrue(r.cleanup_verified,r.reasons)
        self.assertEqual(before,self.fingerprint(),'Reference rows and sequence must not change')
        return r

    def test_insert_skip_update_filtered_update_and_savepoint_in_every_phase(self):
        text='''BEGIN ISOLATION LEVEL REPEATABLE READ;
            INSERT INTO retail.stock(sku,quantity) VALUES('C',5) RETURNING id,sku,quantity;
            INSERT INTO retail.stock(sku,quantity) VALUES('C',99) ON CONFLICT(sku) DO NOTHING RETURNING id;
            INSERT INTO retail.stock AS t(sku,quantity) VALUES('C',2) ON CONFLICT(sku)
                DO UPDATE SET quantity=t.quantity+EXCLUDED.quantity RETURNING id,quantity;
            INSERT INTO retail.stock AS t(sku,quantity) VALUES('C',0) ON CONFLICT(sku)
                DO UPDATE SET quantity=EXCLUDED.quantity WHERE EXCLUDED.quantity>0 RETURNING id;
            SAVEPOINT discarded;
            INSERT INTO retail.stock AS t(sku,quantity) VALUES('A',4) ON CONFLICT ON CONSTRAINT stock_sku_key
                DO UPDATE SET quantity=t.quantity+EXCLUDED.quantity RETURNING id,quantity;
            ROLLBACK TO discarded; RELEASE discarded;
            SELECT id,sku,quantity,label FROM retail.stock ORDER BY id;
            COMMIT;'''
        for ending in ('COMMIT','ROLLBACK'):
            with self.subTest(ending=ending):
                r=self.replay(text.replace('COMMIT;',ending+';'),series=2)
                self.assertTrue(r.correctness_verified,r.reasons)
                for sample in r.samples:
                    commands=sample.commands
                    self.assertEqual(commands[0].result_sha256,result_digest([(3,'C',5)],False))
                    self.assertEqual((commands[1].row_count,commands[1].result_sha256),(0,result_digest([],False)))
                    self.assertEqual(commands[2].result_sha256,result_digest([(3,7)],False))
                    self.assertEqual((commands[3].row_count,commands[3].result_sha256),(0,result_digest([],False)))
                    self.assertEqual(commands[5].result_sha256,result_digest([(1,14)],False))
                    self.assertEqual(commands[8].result_sha256,result_digest([(1,'A',10,'old'),(2,'B',20,'old'),(3,'C',7,'new')],True))
                    self.assertNotEqual(sample.initial_sequences,sample.final_sequences)
                    if ending=='ROLLBACK':self.assertEqual(sample.initial_state,sample.final_state)

    def test_default_values_and_update_default_allocate_both_required_numbers(self):
        text='''BEGIN;INSERT INTO retail.stock DEFAULT VALUES ON CONFLICT DO NOTHING RETURNING id;
            INSERT INTO retail.stock DEFAULT VALUES ON CONFLICT(sku) DO UPDATE SET id=DEFAULT,label=DEFAULT RETURNING id,label;
            SELECT id,sku FROM retail.stock ORDER BY id;COMMIT;'''
        r=self.replay(text)
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].row_count,0) # attempted ID 3 consumed
            self.assertEqual(sample.commands[1].result_sha256,result_digest([(5,'new')],False)) # attempted 4, UPDATE DEFAULT 5
            self.assertEqual(sample.commands[2].result_sha256,result_digest([(2,'B'),(5,'A')],True))

    def test_both_default_branches_are_audited_before_creating_copies(self):
        with self.connect() as connection:
            connection.execute('ALTER TABLE retail.stock ALTER COLUMN label SET DEFAULT now()::text')
        texts=("INSERT INTO retail.stock(sku,label) VALUES('A','explicit') ON CONFLICT(sku) DO UPDATE SET label=DEFAULT WHERE false",
            "INSERT INTO retail.stock(sku) VALUES('A') ON CONFLICT(sku) DO NOTHING")
        for body in texts:
            with self.subTest(body=body):
                r=self.replay('BEGIN;'+body+';SELECT id FROM retail.stock;COMMIT;')
                self.assertEqual(r.status,'declined',r.reasons)
                self.assertFalse(r.created_databases)

    def test_missing_and_deferrable_arbiters_are_refused_before_clone_creation(self):
        with self.connect() as connection:
            connection.execute('ALTER TABLE retail.stock ADD CONSTRAINT quantity_deferred UNIQUE(quantity) DEFERRABLE INITIALLY DEFERRED')
        for clause in ('ON CONFLICT(label) DO NOTHING',
                'ON CONFLICT ON CONSTRAINT missing DO NOTHING',
                'ON CONFLICT(quantity) DO UPDATE SET label=excluded.label',
                'ON CONFLICT ON CONSTRAINT quantity_deferred DO NOTHING','ON CONFLICT DO NOTHING'):
            with self.subTest(clause=clause):
                r=self.replay("BEGIN;INSERT INTO retail.stock(sku,quantity) VALUES('C',7) "+clause+';SELECT id FROM retail.stock;COMMIT;')
                self.assertEqual(r.status,'declined',r.reasons)
                self.assertFalse(r.created_databases)

    def test_composite_keys_and_nullable_unique_key_keep_postgres_semantics(self):
        with self.connect() as connection:
            connection.execute('ALTER TABLE retail.stock ADD UNIQUE(sku,active)')
        text='''BEGIN;INSERT INTO retail.stock(sku,quantity) VALUES('A',2)
            ON CONFLICT(active,sku) DO UPDATE SET quantity=stock.quantity+EXCLUDED.quantity RETURNING id,quantity;
            INSERT INTO retail.stock(sku) VALUES('C'),('D') ON CONFLICT(nullable_key) DO NOTHING RETURNING id;
            SELECT id,sku,quantity FROM retail.stock ORDER BY id;COMMIT;'''
        r=self.replay(text)
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].result_sha256,result_digest([(1,12)],False))
            self.assertEqual(sample.commands[1].result_sha256,result_digest([(4,),(5,)],False))

    def test_duplicate_multirow_update_is_an_error_and_does_not_recommend(self):
        text="""BEGIN;INSERT INTO retail.stock(sku,quantity) VALUES('A',1),('A',2)
            ON CONFLICT(sku) DO UPDATE SET quantity=excluded.quantity;SELECT id FROM retail.stock;COMMIT;"""
        r=self.replay(text)
        self.assertEqual(r.status,'error',r.reasons)
        self.assertFalse(r.correctness_verified)
        self.assertTrue(any('21000' in reason for reason in r.reasons),r.reasons)
        self.assertFalse(r.recommendation.recommended)

    def test_nulls_not_distinct_unique_index_is_respected(self):
        with self.connect() as connection:
            connection.execute('UPDATE retail.stock SET nullable_key=id')
            connection.execute('ALTER TABLE retail.stock DROP CONSTRAINT stock_nullable_key_key')
            connection.execute('CREATE UNIQUE INDEX nullable_key_unique ON retail.stock(nullable_key) NULLS NOT DISTINCT')
        text="""BEGIN;INSERT INTO retail.stock(sku,quantity) VALUES('C',3) ON CONFLICT(nullable_key) DO NOTHING RETURNING id;
            INSERT INTO retail.stock(sku,quantity) VALUES('D',4) ON CONFLICT(nullable_key)
                DO UPDATE SET quantity=stock.quantity+excluded.quantity RETURNING id,sku,quantity;
            SELECT id FROM retail.stock ORDER BY id;COMMIT;"""
        r=self.replay(text)
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].result_sha256,result_digest([(3,)],False))
            self.assertEqual(sample.commands[1].result_sha256,result_digest([(3,'C',7)],False))

    def test_upsert_runs_in_each_requested_isolation_level(self):
        for level in ('READ UNCOMMITTED','READ COMMITTED','REPEATABLE READ','SERIALIZABLE'):
            with self.subTest(level=level):
                text=f'''BEGIN ISOLATION LEVEL {level};INSERT INTO retail.stock(sku,quantity) VALUES('A',2)
                    ON CONFLICT(sku) DO UPDATE SET quantity=stock.quantity+excluded.quantity RETURNING id,quantity;
                    SELECT id FROM retail.stock;COMMIT;'''
                r=self.replay(text)
                self.assertTrue(r.correctness_verified,r.reasons)
                for sample in r.samples:
                    self.assertEqual(sample.transaction_options.isolation_level,level)
                    self.assertEqual(sample.commands[0].result_sha256,result_digest([(1,12)],False))

    def test_cancellation_after_conflict_consumes_only_disposable_sequence(self):
        control=AnalysisControl()
        control.progress=lambda message: control.cancel() if message.startswith('Последовательное выполнение: 2') else None
        r=self.replay("BEGIN;INSERT INTO retail.stock(sku) VALUES('A') ON CONFLICT(sku) DO NOTHING;SELECT id FROM retail.stock;COMMIT;",control=control)
        self.assertEqual(r.status,'cancelled',r.reasons)
        self.assertFalse(r.correctness_verified)
        self.assertFalse(r.recommendation.recommended)

    def test_rewrite_inside_upsert_transaction_preserves_all_writes_and_ids(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        text="""BEGIN ISOLATION LEVEL SERIALIZABLE;INSERT INTO retail.stock(sku,quantity) VALUES('A',2)
            ON CONFLICT(sku) DO UPDATE SET quantity=stock.quantity+excluded.quantity RETURNING id;
            SELECT id,quantity FROM retail.stock WHERE sku='A' OR sku='B' ORDER BY id;COMMIT;"""
        option=next(item for item in generate_transaction_select_rewrites(text,self.settings) if item.rule_id=='or-equality-to-in')
        before=self.fingerprint()
        r=run_transaction_select_rewrite(text,self.settings,self.reference,2,option.variant_sql,series=1,
            test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
        self.assertTrue(r.correctness_verified,r.reasons)
        self.assertTrue(r.cleanup_verified)
        for sample in r.samples:
            self.assertEqual(sample.commands[1].result_sha256,result_digest([(1,12),(2,20)],True))
            self.assertEqual(sample.transaction_options.isolation_level,'SERIALIZABLE')
        self.assertEqual(before,self.fingerprint())
