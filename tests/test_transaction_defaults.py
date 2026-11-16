import os
import time
from dataclasses import replace
from unittest import TestCase, skipUnless
from uuid import uuid4

import psycopg
from psycopg import sql
from sqlglot import parse_one

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_defaults import _constant, used_defaults
from pqo.transaction_replay_guard import prepare_replay,replay_catalog
from pqo.transaction_replay import (StateDigest,SequenceStateDigest,assess_correctness,
    run_transaction_replay,state_digest,sequence_digest,result_digest)


class DefaultContractTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings(),allowed_schemas=frozenset({'university'}))

    def program(self,body):
        return prepare_replay(f'BEGIN; {body}; SELECT id FROM university.items; COMMIT;',self.settings)

    def test_only_reachable_defaults_are_selected_including_implicit_column_list(self):
        cases=(('INSERT INTO university.items DEFAULT VALUES',{'id','value','label'}),
            ('INSERT INTO university.items(value) VALUES(DEFAULT),(8)',{'id','value','label'}),
            ('INSERT INTO university.items VALUES(7,8)',{'label'}),
            ("INSERT INTO university.items(id,value,label) VALUES(7,8,'x')",set()),
            ('UPDATE university.items SET value=DEFAULT WHERE id=7',{'value'}),
            ('UPDATE university.items SET value=8 WHERE id=7',set()))
        for body,expected in cases:
            with self.subTest(body=body):
                self.assertEqual(used_defaults(self.program(body),'university','items',('id','value','label')),expected)

    def test_invalid_arity_and_columns_fail_before_execution(self):
        for body in ('INSERT INTO university.items(value) VALUES(1,2)',
                'INSERT INTO university.items VALUES(1,2,3,4)',
                'INSERT INTO university.items(id,id) VALUES(1,2)',
                'INSERT INTO university.items(missing) VALUES(1)',
                'INSERT INTO university.items(value) VALUES(1),(1,2)',
                'UPDATE university.items SET missing=DEFAULT WHERE id=1'):
            with self.subTest(body=body),self.assertRaises(ValueError):
                used_defaults(self.program(body),'university','items',('id','value','label'))

    def test_default_cannot_hide_in_expressions_or_tuple_assignments(self):
        for body in ('UPDATE university.items SET (value,label)=(DEFAULT,DEFAULT) WHERE id=1',
                'UPDATE university.items SET value=DEFAULT+1 WHERE id=1',
                'INSERT INTO university.items(value) VALUES(DEFAULT+1)',
                'SELECT DEFAULT FROM university.items',
                'INSERT INTO university.items(id) OVERRIDING SYSTEM VALUE VALUES(1)',
                "INSERT INTO university.items(id) VALUES(nextval('university.items_id_seq'))"):
            with self.subTest(body=body),self.assertRaises(ValueError):self.program(body)

    def test_active_constant_grammar_excludes_volatile_clock_and_user_calls(self):
        for text in ("7","-7","NULL","true","'pending'::text","'2026-01-01'::date",'NULL::timestamp'):
            with self.subTest(text=text):self.assertTrue(_constant(parse_one(text,read='postgres')))
        for text in ('now()','current_timestamp','random()',"'today'::date","'now'::timestamp",
                'university.custom_default()',"'7'::university.user_type",'(SELECT 7)'):
            with self.subTest(text=text):self.assertFalse(_constant(parse_one(text,read='postgres')))

    def test_sequence_evidence_cannot_be_missing_or_different(self):
        from pqo.transaction_replay import ReplaySample,CommandReplay
        row=StateDigest('university.items',0,'a'*64,('university.items_id_seq',))
        before=(SequenceStateDigest('university.items_id_seq','b'*64),)
        after=(SequenceStateDigest('university.items_id_seq','c'*64),)
        samples=tuple(ReplaySample(1,phase,phase,(CommandReplay(1,'select',1.,0,(),None),),2.,
            (row,),(row,),0,0.,ending='rollback',initial_sequences=before,final_sequences=after)
            for phase in ('A1','B','A2'))
        self.assertEqual(assess_correctness(samples,1),(True,True,True))
        self.assertEqual(assess_correctness((samples[0],replace(samples[1],final_sequences=before),samples[2]),1),
            (True,True,False))
        self.assertEqual(assess_correctness(tuple(replace(s,final_sequences=()) for s in samples),1),
            (False,False,False))


@skipUnless(os.environ.get('PQO_DISPOSABLE_REPLAY_TESTS')=='1','Explicit opt-in: own fixture and tracked clones only')
class DefaultsPostgresTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings.from_env(),dbname='pqo_defaults_source_not_connected',
            allowed_schemas=frozenset({'university'}))
        self.reference='pqo_defaultfixture_'+uuid4().hex
        with self.connect('postgres',autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.reference)))
            self.owner=connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()
        self.addCleanup(self.cleanup)
        with self.connect() as connection:
            connection.execute('CREATE SCHEMA university')
            connection.execute("CREATE TABLE university.items(id serial PRIMARY KEY,value integer DEFAULT 7,label text DEFAULT 'fixture')")
            connection.execute('CREATE TABLE university.identities(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,value integer DEFAULT 9)')
            connection.execute('CREATE TABLE university.by_default(id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,value integer DEFAULT 11)')

    def connect(self,database=None,**kwargs):
        return psycopg.connect(**replace(self.settings,dbname=database or self.reference).connection_kwargs(),**kwargs)

    def cleanup(self):
        with self.connect('postgres',autocommit=True) as connection:
            if connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()==self.owner:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.reference)))

    def fingerprint(self,text):
        with self.connect(autocommit=True) as connection:
            connection.execute("SET timezone='UTC'")
            tables=replay_catalog(connection,self.settings,prepare_replay(text,self.settings))
            deadline=time.monotonic()+30
            return (state_digest(connection,tables,self.settings,deadline,AnalysisControl()),
                sequence_digest(connection,tables,self.settings,deadline,AnalysisControl()))

    def run_replay(self,text):
        before=self.fingerprint(text)
        report=run_transaction_replay(text,self.settings,self.reference,
            (IndexAction.create('university','items',('value',)),),test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertTrue(report.cleanup_verified,report.reasons)
        self.assertEqual(before,self.fingerprint(text),'Reference rows AND sequences must remain unchanged')
        return report

    def test_serial_both_identities_defaults_and_savepoint_preserve_generated_ids(self):
        text='''BEGIN ISOLATION LEVEL REPEATABLE READ;
            INSERT INTO university.items(value) VALUES(DEFAULT),(4) RETURNING id,value,label;
            SAVEPOINT discarded;
            INSERT INTO university.items DEFAULT VALUES RETURNING id;
            ROLLBACK TO discarded;
            RELEASE discarded;
            UPDATE university.items SET id=DEFAULT,value=DEFAULT WHERE id=1 RETURNING id,value;
            INSERT INTO university.identities DEFAULT VALUES RETURNING id,value;
            INSERT INTO university.by_default(id,value) VALUES(90,8),(DEFAULT,DEFAULT) RETURNING id,value;
            SELECT id,value,label FROM university.items WHERE id=2 OR id=4 ORDER BY id;
            COMMIT;'''
        report=self.run_replay(text)
        self.assertTrue(report.correctness_verified,report.reasons)
        for sample in report.samples:
            self.assertEqual(len(sample.initial_state),3)
            self.assertEqual(len(sample.initial_sequences),3)
            self.assertEqual(sample.commands[0].result_sha256,result_digest([(1,7,'fixture'),(2,4,'fixture')],False))
            self.assertEqual(sample.commands[2].result_sha256,result_digest([(3,)],False))
            self.assertEqual(sample.commands[5].result_sha256,result_digest([(4,7)],False))
            self.assertEqual(sample.commands[6].result_sha256,result_digest([(1,9)],False))
            self.assertEqual(sample.commands[7].result_sha256,result_digest([(90,8),(1,11)],False))
            self.assertEqual(sample.commands[8].result_sha256,result_digest([(2,4,'fixture'),(4,7,'fixture')],True))
            self.assertNotEqual(sample.initial_sequences,sample.final_sequences)

    def test_full_rollback_restores_rows_but_not_consumed_sequence_values(self):
        text='''BEGIN; INSERT INTO university.items VALUES(DEFAULT,DEFAULT) RETURNING id,value,label;
            SELECT id,value FROM university.items ORDER BY id; ROLLBACK;'''
        report=self.run_replay(text)
        self.assertTrue(report.correctness_verified,report.reasons)
        for sample in report.samples:
            self.assertEqual(sample.initial_state,sample.final_state)
            self.assertNotEqual(sample.initial_sequences,sample.final_sequences)
            self.assertEqual(sample.commands[0].result_sha256,result_digest([(1,7,'fixture')],False))

    def test_only_active_defaults_are_audited_and_custom_function_is_never_called(self):
        with self.connect() as connection:
            connection.execute('CREATE TABLE university.calls(id integer PRIMARY KEY)')
            connection.execute('''CREATE FUNCTION university.custom_default() RETURNS integer LANGUAGE plpgsql AS $$
                BEGIN INSERT INTO university.calls VALUES(1); RETURN 7; END $$''')
            connection.execute('ALTER TABLE university.items ALTER COLUMN value SET DEFAULT university.custom_default()')
        inactive='BEGIN; UPDATE university.items SET value=2 WHERE id=1; SELECT id FROM university.items; COMMIT;'
        report=self.run_replay(inactive)
        self.assertTrue(report.correctness_verified,report.reasons)
        with self.connect() as connection:
            connection.execute('ALTER TABLE university.items ALTER COLUMN label SET DEFAULT now()::text')
        explicit="BEGIN; INSERT INTO university.items(value,label) VALUES(2,'explicit'); SELECT id FROM university.items; COMMIT;"
        self.assertTrue(self.run_replay(explicit).correctness_verified)
        for body in ('INSERT INTO university.items DEFAULT VALUES','UPDATE university.items SET value=DEFAULT WHERE id=1'):
            text=f'BEGIN; {body}; SELECT id FROM university.items; COMMIT;'
            report=run_transaction_replay(text,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
                test_copy_confirmed=True,disposable_databases_confirmed=True)
            self.assertEqual(report.status,'declined',report.reasons)
            self.assertFalse(report.created_databases)
        with self.connect() as connection:self.assertEqual(connection.execute('SELECT count(*) FROM university.calls').fetchone()[0],0)

    def test_sequence_cache_cycle_sharing_and_ownership_are_refused(self):
        cases=(('ALTER SEQUENCE university.items_id_seq CACHE 2','ALTER SEQUENCE university.items_id_seq CACHE 1'),
            ('ALTER SEQUENCE university.items_id_seq CYCLE','ALTER SEQUENCE university.items_id_seq NO CYCLE'),
            ("ALTER TABLE university.items ALTER COLUMN value SET DEFAULT nextval('university.items_id_seq')",
                'ALTER TABLE university.items ALTER COLUMN value SET DEFAULT 7'),
            ('ALTER SEQUENCE university.items_id_seq OWNED BY NONE','ALTER SEQUENCE university.items_id_seq OWNED BY university.items.id'))
        text='BEGIN; INSERT INTO university.items DEFAULT VALUES; SELECT id FROM university.items; COMMIT;'
        for change,undo in cases:
            with self.subTest(change=change):
                with self.connect() as connection:connection.execute(change)
                try:
                    report=run_transaction_replay(text,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
                        test_copy_confirmed=True,disposable_databases_confirmed=True)
                    self.assertEqual(report.status,'declined',report.reasons)
                    self.assertTrue(report.cleanup_verified)
                    self.assertFalse(report.created_databases)
                finally:
                    with self.connect() as connection:connection.execute(undo)

    def test_identity_always_explicit_value_is_not_silently_overridden(self):
        text='''BEGIN; INSERT INTO university.identities(id) VALUES(50);
            SELECT id FROM university.items; COMMIT;'''
        report=self.run_replay(text)
        self.assertEqual(report.status,'error',report.reasons)
        self.assertFalse(report.correctness_verified)
        self.assertEqual(report.recommendation.status,'inconclusive')

    def test_nonstandard_sequence_increment_and_cancel_after_consumption(self):
        with self.connect() as connection:
            connection.execute('ALTER SEQUENCE university.items_id_seq INCREMENT -3 MINVALUE -100 MAXVALUE 100 START 10 RESTART 10')
        text='''BEGIN; INSERT INTO university.items DEFAULT VALUES RETURNING id;
            INSERT INTO university.items DEFAULT VALUES RETURNING id;
            SELECT id FROM university.items ORDER BY id; COMMIT;'''
        report=self.run_replay(text)
        self.assertTrue(report.correctness_verified,report.reasons)
        self.assertEqual(report.samples[0].commands[0].result_sha256,result_digest([(10,)],False))
        self.assertEqual(report.samples[0].commands[1].result_sha256,result_digest([(7,)],False))
        before=self.fingerprint(text)
        control=AnalysisControl()
        control.progress=lambda message: control.cancel() if message.startswith('Последовательное выполнение: 2') else None
        cancelled=run_transaction_replay(text,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
            test_copy_confirmed=True,disposable_databases_confirmed=True,control=control)
        self.assertEqual(cancelled.status,'cancelled',cancelled.reasons)
        self.assertTrue(cancelled.cleanup_verified)
        self.assertFalse(cancelled.correctness_verified)
        self.assertEqual(before,self.fingerprint(text))

    def test_select_rewrite_preserves_generated_ids_and_rejects_result_mismatch(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        text='''BEGIN; INSERT INTO university.items(value) VALUES(7),(9) RETURNING id;
            SELECT id,value FROM university.items WHERE value=7 OR value=9 ORDER BY id; COMMIT;'''
        option=next(item for item in generate_transaction_select_rewrites(text,self.settings) if item.rule_id=='or-equality-to-in')
        before=self.fingerprint(text)
        for variant,correct in ((option.variant_sql,True),
                ('SELECT id,value FROM university.items WHERE value=8 ORDER BY id',False)):
            report=run_transaction_select_rewrite(text,self.settings,self.reference,2,variant,series=1,
                test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
            self.assertEqual(report.correctness_verified,correct,report.reasons)
            self.assertTrue(report.cleanup_verified)
            self.assertTrue(report.final_states_equal,'Both programs make identical writes and consume identical IDs')
            self.assertEqual(report.results_equal,correct)
            self.assertFalse(report.recommendation.recommended)
        self.assertEqual(before,self.fingerprint(text))
