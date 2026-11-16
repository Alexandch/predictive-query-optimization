import os
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import json
import tempfile
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg
from psycopg import sql

from pqo.analysis_control import AnalysisCancelled, AnalysisControl
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_replay_guard import prepare_replay, replay_catalog
from pqo.transaction_context import TransactionOptions
from pqo.transaction_replay import (CommandReplay, ReplaySample, StateDigest, ScratchDatabases,
    assess_correctness, result_digest, run_transaction_replay, _execute_program)

ROOT = Path(__file__).resolve().parents[1]
TEXT = "BEGIN; SELECT id,value FROM university.items WHERE id=1 ORDER BY id; UPDATE university.items SET value=2 WHERE id=1 RETURNING id,value; SELECT id,value FROM university.items WHERE id=1; COMMIT;"


class TransactionReplayTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname='source', allowed_schemas=frozenset({'university'}))
        self.action = IndexAction.create('university', 'items', ('value',))

    def test_closed_sql_contract_and_explicit_example(self):
        program = prepare_replay(TEXT, self.settings)
        self.assertEqual(program.tables, (('university', 'items'),))
        self.assertEqual(tuple(item.kind for item in program.commands), ('select','update','select'))
        self.assertTrue(program.commands[0].ordered)
        example = prepare_replay((ROOT / 'examples/transactions/university-disposable-replay.sql').read_text(encoding='utf-8'), self.settings)
        self.assertEqual(len(example.commands), 6)
        joined = prepare_replay((ROOT / 'examples/transactions/university-join-replay.sql').read_text(encoding='utf-8'), self.settings)
        self.assertEqual(len(joined.commands), 3)
        self.assertEqual(len(joined.tables), 5)

    def test_unsupported_sql_is_rejected_offline_before_any_connection(self):
        for body in (
            "SELECT custom_fn(id) FROM university.items", "SELECT nextval('s') FROM university.items",
            "SELECT now() FROM university.items", "SELECT random() FROM university.items",
            "SELECT * FROM university.items FOR UPDATE", "SELECT * FROM university.items WHERE id=$1",
            "SELECT * FROM items", "SELECT * FROM other.items", "SELECT * INTO newtable FROM university.items",
            "SELECT * FROM university.items LIMIT 1", "SELECT * FROM university.items a JOIN university.items b ON true",
            "WITH x AS (DELETE FROM university.items RETURNING *) SELECT * FROM x",
            "UPDATE university.items SET value=3", "DELETE FROM university.items",
            "UPDATE university.items SET (value,label)=(2,'a') WHERE id=1", "INSERT INTO university.items(id,value) SELECT id,value FROM university.items UNION ALL SELECT id,value FROM university.items",
            "INSERT INTO university.items(id,value) VALUES(1,2) ON CONFLICT((id+1)) DO UPDATE SET value=3",
            "SELECT CAST('now' AS timestamp) FROM university.items", "SET work_mem='1MB'",
            "UPDATE university.items SET label='today' WHERE id=1", "SELECT xmin FROM university.items",
        ):
            text = TEXT.replace('SELECT id,value FROM university.items WHERE id=1 ORDER BY id', body, 1)
            with self.subTest(body=body), patch('pqo.transaction_replay.psycopg.connect') as connect, self.assertRaises(ValueError):
                run_transaction_replay(text, self.settings, 'lab', (self.action,), test_copy_confirmed=True, disposable_databases_confirmed=True)
            connect.assert_not_called()
        for text in (TEXT.replace('BEGIN;', 'BEGIN ISOLATION LEVEL UNKNOWN;'), 'BEGIN; SELECT id FROM university.items; COMMIT;'):
            with self.assertRaises(ValueError):
                prepare_replay(text, self.settings)

    def test_flat_inner_left_and_self_joins_preserve_all_sources_and_keys(self):
        selects = (
            'SELECT a.id,b.value FROM university.items a INNER JOIN university.other b ON a.id=b.id',
            'SELECT a.id,b.value FROM university.items a LEFT OUTER JOIN university.other b ON (a.id=b.id AND b.value=a.value)',
            'SELECT a.id,b.id FROM university.items a JOIN university.items b ON a.value=b.value',
            'SELECT items.id,other.value FROM university.items JOIN university.other ON items.id=other.id',
            'SELECT a.id,c.value FROM university.items a JOIN university.other b ON a.id=b.id LEFT JOIN university.third c ON c.id=b.id AND c.value=a.value',
            'SELECT "A".id,b.value FROM university.items AS "A" JOIN university.other b ON "A".id=b.id',
            'SELECT A.id,B.value FROM university.items A JOIN university.other B ON a.id=b.id',
        )
        for select in selects:
            with self.subTest(select=select), patch('pqo.transaction_replay.psycopg.connect') as connect:
                program = prepare_replay('BEGIN; UPDATE university.items SET value=7 WHERE id=1; '+select+'; COMMIT;', self.settings)
            connect.assert_not_called()
            self.assertEqual(program.commands[1].kind, 'select')
            expected = {('university', 'items')}
            for table in ('other', 'third'):
                if 'university.'+table in select:
                    expected.add(('university', table))
            self.assertEqual(set(program.tables), expected)
        composite = prepare_replay('BEGIN; UPDATE university.items SET value=7 WHERE id=1; '+selects[1]+'; COMMIT;', self.settings)
        self.assertIn('b.value = a.value', composite.commands[1].sql)

    def test_unsupported_join_forms_and_every_source_schema_are_rejected_offline(self):
        selects = (
            'SELECT * FROM university.items a CROSS JOIN university.other b',
            'SELECT * FROM university.items a, university.other b',
            'SELECT * FROM university.items a RIGHT JOIN university.other b ON a.id=b.id',
            'SELECT * FROM university.items a FULL JOIN university.other b ON a.id=b.id',
            'SELECT * FROM university.items a NATURAL JOIN university.other b',
            'SELECT * FROM university.items a JOIN university.other b USING(id)',
            'SELECT * FROM university.items a JOIN university.other b ON a.id=b.id OR true',
            'SELECT * FROM university.items a JOIN university.other b ON a.id>b.id',
            'SELECT * FROM university.items a JOIN university.other b ON b.id=b.value',
            'SELECT * FROM university.items a JOIN university.other b ON a.id=unknown.id',
            'SELECT * FROM university.items a JOIN university.other b ON a.id=c.id JOIN university.third c ON b.id=c.id',
            'SELECT * FROM university.items a JOIN university.other a ON a.id=a.id',
            'SELECT * FROM university.items A JOIN university.other a ON A.id=a.id',
            'SELECT * FROM university.items "A" JOIN university.other b ON a.id=b.id',
            'SELECT * FROM university.items a JOIN university.other b(x,y) ON a.id=b.x',
            'SELECT * FROM university.items a JOIN other_schema.items b ON a.id=b.id',
            'SELECT * FROM university.items a JOIN items b ON a.id=b.id',
            'SELECT * FROM university.items a JOIN (SELECT id FROM university.other) b ON a.id=b.id',
            'SELECT * FROM university.items a JOIN LATERAL (SELECT id FROM university.other) b ON a.id=b.id',
            'UPDATE university.items a SET value=b.value FROM university.other b WHERE a.id=b.id',
            'DELETE FROM university.items a USING university.other b WHERE a.id=b.id',
        )
        for select in selects:
            text = 'BEGIN; UPDATE university.items SET value=7 WHERE id=1; '+select+'; COMMIT;'
            with self.subTest(select=select), patch('pqo.transaction_replay.psycopg.connect') as connect, self.assertRaises(ValueError):
                run_transaction_replay(text, self.settings, 'lab', (self.action,),
                    test_copy_confirmed=True, disposable_databases_confirmed=True)
            connect.assert_not_called()
        for count in (8, 9):
            joins = ' '.join(f'JOIN university.t{i} t{i} ON a.id=t{i}.id' for i in range(1, count))
            text = f'BEGIN; UPDATE university.items SET value=7 WHERE id=1; SELECT a.id FROM university.items a {joins}; COMMIT;'
            if count == 8:
                self.assertEqual(len(prepare_replay(text,self.settings).tables), 8)
            else:
                with self.assertRaises(ValueError): prepare_replay(text,self.settings)

    def test_reference_and_permission_guards_never_connect(self):
        for reference, confirmed, disposable in (('source', True,True), ('SOURCE',True,True), ('postgres',True,True),
                ('template1',True,True), ('pqo_replay_other',True,True), ('lab;DROP DATABASE source',True,True), ('lab',False,True), ('lab',True,False)):
            with self.subTest(reference=reference), patch('pqo.transaction_replay.psycopg.connect') as connect, self.assertRaises(ValueError):
                run_transaction_replay(TEXT, self.settings, reference, (self.action,), test_copy_confirmed=confirmed, disposable_databases_confirmed=disposable)
            connect.assert_not_called()

    def test_result_fingerprint_retains_duplicates_nulls_types_and_order(self):
        rows = [(1,None,Decimal('2.5')), (1,None,Decimal('2.5')), (2,'x',Decimal('3'))]
        self.assertEqual(result_digest(rows, False), result_digest(list(reversed(rows)), False))
        self.assertNotEqual(result_digest(rows, True), result_digest(list(reversed(rows)), True))
        self.assertNotEqual(result_digest(rows, False), result_digest(rows[1:], False))
        self.assertNotEqual(result_digest([(1,)], False), result_digest([('1',)], False))

    def test_reserved_source_and_invalid_series_reject_without_connect(self):
        with patch('pqo.transaction_replay.psycopg.connect') as connect:
            for settings,series in ((replace(self.settings,dbname='postgres'),1),(self.settings,True),(self.settings,0),(self.settings,4)):
                with self.assertRaises(ValueError):
                    run_transaction_replay(TEXT,settings,'lab',(self.action,),test_copy_confirmed=True,disposable_databases_confirmed=True,series=series)
        connect.assert_not_called()

    def test_recovery_metadata_has_no_sql_rows_or_password_and_is_written_before_ddl(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = ScratchDatabases(replace(self.settings,password='SENSITIVE_TEST_PASSWORD'),directory)
            scratch.created['pqo_replay_test'] = (100,10)
            scratch._record()
            data = scratch.manifest.read_text(encoding='utf-8')
            self.assertNotIn('SENSITIVE_TEST_PASSWORD',data)
            self.assertNotIn('UPDATE',data)
            self.assertEqual(json.loads(data)['created'], {'pqo_replay_test':[100,10]})

    def test_clone_lifecycle_uses_private_seed_and_new_database_for_every_phase(self):
        scratch = MagicMock()
        scratch.create.side_effect = ['pqo_replay_seed','pqo_replay_a1','pqo_replay_b','pqo_replay_a2']
        scratch.created = {'pqo_replay_seed':(1,10)}
        scratch.remove.return_value = True
        scratch.finish.return_value = (True,())
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = (1024,)
        def sample(settings,database,program,actions,series,phase,deadline,control):
            return replace(self.sample(phase),database=database)
        with patch('pqo.transaction_replay.ScratchDatabases',return_value=scratch), patch('pqo.transaction_replay.psycopg.connect',return_value=connection) as connect, \
                patch('pqo.transaction_replay.replay_catalog'), patch('pqo.transaction_replay.read_transaction_options',return_value=TransactionOptions('READ COMMITTED',False,False)), \
                patch('pqo.transaction_replay._sample',side_effect=sample):
            report = run_transaction_replay(TEXT,self.settings,'lab',(self.action,),test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertTrue(report.correctness_verified)
        self.assertTrue(report.cleanup_verified)
        self.assertEqual(scratch.create.call_args_list[0].args,('lab',))
        self.assertFalse(scratch.create.call_args_list[0].kwargs['enabled'])
        self.assertTrue(all(call.args==('pqo_replay_seed',) and call.kwargs['enabled'] for call in scratch.create.call_args_list[1:]))
        self.assertEqual([call.args[0] for call in scratch.remove.call_args_list],['pqo_replay_a1','pqo_replay_b','pqo_replay_a2'])
        self.assertEqual(connect.call_args.kwargs['dbname'],'lab')

    def test_cancel_error_and_cleanup_failure_cannot_produce_correctness(self):
        for outcome,cleanup in ((RuntimeError('fixture error'),True),(AnalysisCancelled('fixture cancel'),True),(None,False)):
            scratch = MagicMock()
            scratch.create.side_effect = ['pqo_replay_seed','pqo_replay_run']
            scratch.created = {'pqo_replay_run':(1,10)}
            scratch.remove.return_value = cleanup
            scratch.finish.return_value = (cleanup,() if cleanup else ('pqo_replay_run',))
            connection = MagicMock(); connection.__enter__.return_value=connection
            connection.execute.return_value.fetchone.return_value=(1024,)
            with patch('pqo.transaction_replay.ScratchDatabases',return_value=scratch), patch('pqo.transaction_replay.psycopg.connect',return_value=connection), \
                    patch('pqo.transaction_replay.replay_catalog'), patch('pqo.transaction_replay.read_transaction_options',return_value=TransactionOptions('READ COMMITTED',False,False)), \
                    patch('pqo.transaction_replay._sample',side_effect=outcome,return_value=self.sample()):
                report = run_transaction_replay(TEXT,self.settings,'lab',(self.action,),test_copy_confirmed=True,disposable_databases_confirmed=True)
            self.assertFalse(report.correctness_verified)
            self.assertEqual(report.cleanup_verified,cleanup)
            if isinstance(outcome,AnalysisCancelled):
                self.assertEqual(report.status,'cancelled')
            scratch.finish.assert_called_once()

    def sample(self, phase='A1', *, value='end', initial='start', result='result'):
        return ReplaySample(1, phase, 'pqo_replay_x', (CommandReplay(1,'select',1.,1,(('id',23),),result),), 2.,
            (StateDigest('university.items',1,initial),), (StateDigest('university.items',1,value),),0,0.)

    def test_completeness_initial_outputs_and_final_state_are_independent_checks(self):
        samples = (self.sample(), self.sample('B'), self.sample('A2'))
        self.assertEqual(assess_correctness(samples,1), (True,True,True))
        self.assertEqual(assess_correctness(samples[:2],1), (False,False,False))
        for field, expected in (('initial',(False,True,True)), ('result',(True,False,True)), ('value',(True,True,False))):
            self.assertEqual(assess_correctness((samples[0],self.sample('B',**{field:'different'}),samples[2]),1), expected)

    def test_execute_runs_each_statement_once_in_order_and_commits_only_copy_connection(self):
        program = prepare_replay(TEXT,self.settings)
        connection = MagicMock()
        connection.execute.return_value.description = None
        connection.execute.return_value.rowcount = 1
        with patch('pqo.transaction_replay._timeout'),patch('pqo.transaction_replay.read_transaction_options',return_value=TransactionOptions('READ COMMITTED',False,False)):
            readings, duration, observed = _execute_program(connection,program,self.settings,999999999,AnalysisControl())
        commands = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(commands, ['BEGIN',*(item.sql for item in program.commands),'COMMIT'])
        self.assertEqual(len(readings),3)
        self.assertGreaterEqual(duration,0)

    def test_execution_error_does_not_commit_and_rolls_back(self):
        connection = MagicMock()
        connection.execute.side_effect = [None, RuntimeError('fixture failure'), None]
        with patch('pqo.transaction_replay._timeout'), self.assertRaises(RuntimeError):
            _execute_program(connection, prepare_replay(TEXT,self.settings), self.settings,99999999,AnalysisControl())
        self.assertEqual(connection.execute.call_args.args[0], 'ROLLBACK')

    def test_mismatching_server_isolation_aborts_before_any_user_command(self):
        program=prepare_replay(TEXT.replace('BEGIN;','BEGIN ISOLATION LEVEL SERIALIZABLE;'),self.settings)
        connection=MagicMock()
        with patch('pqo.transaction_replay._timeout'), \
                patch('pqo.transaction_replay.read_transaction_options',return_value=TransactionOptions('READ COMMITTED',False,False)),self.assertRaises(RuntimeError):
            _execute_program(connection,program,self.settings,999999999,AnalysisControl())
        self.assertEqual([call.args[0] for call in connection.execute.call_args_list],
            ['BEGIN ISOLATION LEVEL SERIALIZABLE','ROLLBACK'])

    def test_different_runtime_contexts_or_committed_data_after_rollback_are_not_equivalent(self):
        samples=(self.sample(),self.sample('B'),self.sample('A2'))
        self.assertEqual(assess_correctness((samples[0],replace(samples[1],transaction_options=TransactionOptions('SERIALIZABLE')),samples[2]),1),
            (False,False,False))
        rolled=tuple(replace(sample,ending='rollback') for sample in samples)
        self.assertFalse(assess_correctness(rolled,1)[2])

    def test_cleanup_never_drops_unknown_reused_or_ambiguous_database(self):
        scratch = ScratchDatabases(self.settings)
        with patch('pqo.transaction_replay.psycopg.connect') as connect, self.assertRaises(ValueError):
            scratch.remove('source')
        connect.assert_not_called()
        for recorded, actual in ((None,(100,10)), ((100,10),(101,10)), ((100,10),(100,11))):
            scratch.created = {'pqo_replay_test': recorded}
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.execute.return_value.fetchone.return_value = actual
            with patch('pqo.transaction_replay.psycopg.connect',return_value=connection):
                self.assertFalse(scratch.remove('pqo_replay_test'))
            self.assertTrue(all(not isinstance(call.args[0],sql.Composed) for call in connection.execute.call_args_list))

    def test_empty_cleanup_does_not_connect_and_cancellation_preflight_does_not_connect(self):
        with patch('pqo.transaction_replay.psycopg.connect') as connect:
            self.assertEqual(ScratchDatabases(self.settings).finish(), (True,()))
            control = AnalysisControl(); control.cancel()
            with self.assertRaises(RuntimeError):
                run_transaction_replay(TEXT,self.settings,'lab',(self.action,),test_copy_confirmed=True,disposable_databases_confirmed=True,control=control)
        connect.assert_not_called()


@skipUnless(os.environ.get('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'Explicit opt-in: creates/drops only its own fixture and tracked scratch DBs')
class DisposableReplayPostgresTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings.from_env(), dbname='pqo_university_unseen', allowed_schemas=frozenset({'university'}))
        self.reference = 'pqo_replayfixture_' + uuid4().hex
        self.owner = None
        admin = replace(self.settings,dbname='postgres')
        with psycopg.connect(**admin.connection_kwargs(),autocommit=True) as connection:
            connection.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(self.reference)))
            self.owner = connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()
        self.addCleanup(self.cleanup_reference)
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            connection.execute('CREATE SCHEMA university')
            connection.execute('CREATE TABLE university.items(id integer PRIMARY KEY,value integer NOT NULL,label text)')
            connection.execute("INSERT INTO university.items SELECT i,i%100,'fixture' FROM generate_series(1,5000) i")
            connection.execute('ANALYZE university.items')

    def cleanup_reference(self):
        with psycopg.connect(**replace(self.settings,dbname='postgres').connection_kwargs(),autocommit=True) as connection:
            identity = connection.execute('SELECT oid,datdba FROM pg_catalog.pg_database WHERE datname=%s',(self.reference,)).fetchone()
            if identity == self.owner:
                connection.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.reference)))

    def test_real_isolation_visibility_and_timeouts_do_not_take_an_early_snapshot(self):
        from pqo.transaction_replay import _timeout
        # This database belongs to this test; neither connection targets a user DB.
        for level in ('READ UNCOMMITTED','READ COMMITTED','REPEATABLE READ','SERIALIZABLE'):
            with self.subTest(level=level):
                target=replace(self.settings,dbname=self.reference)
                with psycopg.connect(**target.connection_kwargs(),autocommit=True) as reader, \
                        psycopg.connect(**target.connection_kwargs(),autocommit=True) as writer:
                    writer.execute('UPDATE university.items SET value=1 WHERE id=1')
                    text=f'BEGIN ISOLATION LEVEL {level}, READ ONLY; SELECT value FROM university.items WHERE id=1; SELECT value FROM university.items WHERE id=1; COMMIT;'
                    program=prepare_replay(text,self.settings)
                    stage={'number':0,'written':set()}
                    def progress(message):
                        stage['number']=int(message.split(': ')[1].split()[0])
                    def timeout(connection,deadline,settings):
                        _timeout(connection,deadline,settings)
                        number=stage['number']
                        if number not in stage['written']:
                            # If timeout used SELECT set_config here, the first
                            # repeatable-read snapshot would precede this write.
                            writer.execute('UPDATE university.items SET value=%s WHERE id=1',(17 if number==1 else 23,))
                            stage['written'].add(number)
                    with patch('pqo.transaction_replay._timeout',side_effect=timeout):
                        commands,_,observed=_execute_program(reader,program,self.settings,
                            __import__('time').monotonic()+30,AnalysisControl(progress=progress))
                    self.assertEqual(observed,TransactionOptions(level,True,False))
                    self.assertEqual(commands[0].result_sha256,result_digest([(17,)],False))
                    expected=23 if level in {'READ COMMITTED','READ UNCOMMITTED'} else 17
                    self.assertEqual(commands[1].result_sha256,result_digest([(expected,)],False))

    def test_savepoint_stack_and_final_rollback_preserve_every_observed_result(self):
        body='''SET TRANSACTION NOT DEFERRABLE;
UPDATE university.items SET value=100 WHERE id=1;
SAVEPOINT s;
UPDATE university.items SET value=200 WHERE id=1;
SELECT value FROM university.items WHERE id=1;
SAVEPOINT s;
UPDATE university.items SET value=300 WHERE id=1;
ROLLBACK TO s;
SELECT value FROM university.items WHERE id=1;
RELEASE s;
ROLLBACK TO s;
SELECT value FROM university.items WHERE id=1;
RELEASE s;
INSERT INTO university.items(id,value) VALUES(5001,10);
DELETE FROM university.items WHERE id=2;
SELECT COUNT(*) FROM university.items;'''
        for ending in ('COMMIT','ROLLBACK'):
            with self.subTest(ending=ending):
                text='BEGIN ISOLATION LEVEL SERIALIZABLE, READ WRITE; '+body+ending+';'
                report=run_transaction_replay(text,self.settings,self.reference,
                    (IndexAction.create('university','items',('value',)),),series=1,
                    test_copy_confirmed=True,disposable_databases_confirmed=True)
                self.assertTrue(report.correctness_verified,report.reasons)
                self.assertTrue(report.cleanup_verified)
                for sample in report.samples:
                    self.assertEqual(sample.transaction_options,TransactionOptions('SERIALIZABLE',False,False))
                    self.assertEqual(sample.ending,ending.lower())
                    for number,value in ((5,200),(9,200),(12,100),(16,5000)):
                        self.assertEqual(sample.commands[number-1].result_sha256,result_digest([(value,)],False))
                    if ending=='ROLLBACK':self.assertEqual(sample.initial_state,sample.final_state)
                    else:self.assertNotEqual(sample.initial_state,sample.final_state)
                with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as c:
                    self.assertEqual(c.execute('SELECT value FROM university.items WHERE id=1').fetchone()[0],1)

    def test_readonly_serializable_deferrable_rewrite_keeps_context(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        text='BEGIN READ ONLY; SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, DEFERRABLE; SELECT id,value FROM university.items WHERE value=1 OR value=2 ORDER BY id; ROLLBACK;'
        option=next(o for o in generate_transaction_select_rewrites(text,self.settings) if o.rule_id=='or-equality-to-in')
        report=run_transaction_select_rewrite(text,self.settings,self.reference,2,option.variant_sql,series=1,
            test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
        self.assertTrue(report.correctness_verified,report.reasons)
        self.assertTrue(report.cleanup_verified)
        self.assertNotIn('Нет полного смешанного',str(report.recommendation.reasons))
        for sample in report.samples:
            self.assertEqual(sample.transaction_options,TransactionOptions('SERIALIZABLE',True,True))
            self.assertEqual(sample.initial_state,sample.final_state)
            self.assertEqual(sample.commands[0].kind,'set_transaction')

    def test_reference_isolation_defaults_are_explicitly_applied_to_every_clone(self):
        with psycopg.connect(**replace(self.settings,dbname='postgres').connection_kwargs(),autocommit=True) as c:
            c.execute(sql.SQL("ALTER DATABASE {} SET default_transaction_isolation='repeatable read'").format(sql.Identifier(self.reference)))
        report=run_transaction_replay(TEXT,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
            test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertTrue(report.correctness_verified,report.reasons)
        self.assertTrue(report.cleanup_verified)
        self.assertTrue(all(sample.transaction_options.isolation_level=='REPEATABLE READ' for sample in report.samples))

    def test_real_serialization_failure_is_not_retried_or_recommended_and_cleans_clones(self):
        from pqo.transaction_replay import _execute_program as execute
        text=TEXT.replace('BEGIN;','BEGIN ISOLATION LEVEL REPEATABLE READ;')
        def conflicting_run(connection,program,settings,deadline,control):
            def progress(message):
                control.report(message)
                if message.startswith('Последовательное выполнение: 2 '):
                    self.assertTrue(connection.info.dbname.startswith('pqo_replay_'))
                    with psycopg.connect(**replace(settings,dbname=connection.info.dbname).connection_kwargs(),autocommit=True) as writer:
                        writer.execute('UPDATE university.items SET value=999 WHERE id=1')
            return execute(connection,program,settings,deadline,AnalysisControl(progress=progress))
        with patch('pqo.transaction_replay._execute_program',side_effect=conflicting_run) as run:
            report=run_transaction_replay(text,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
                test_copy_confirmed=True,disposable_databases_confirmed=True)
        run.assert_called_once()
        self.assertEqual(report.status,'error',report.reasons)
        self.assertTrue(any('40001' in reason for reason in report.reasons))
        self.assertFalse(report.correctness_verified)
        self.assertFalse(report.recommendation.recommended)
        self.assertTrue(report.cleanup_verified)

    def join_fixture(self):
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            connection.execute('CREATE TABLE university.labels(id integer PRIMARY KEY,label text)')
            connection.execute('CREATE TABLE university.links(id integer PRIMARY KEY,item_id integer REFERENCES university.items(id),label_id integer REFERENCES university.labels(id))')
            connection.execute("INSERT INTO university.labels VALUES(1,'alpha'),(2,NULL)")
            connection.execute('INSERT INTO university.links VALUES(1,1,1),(2,1,1),(3,2,NULL),(4,3,2)')
        return '''BEGIN;
UPDATE university.items SET value=700 WHERE id=1 RETURNING id,value;
SELECT i.id,i.value,g.label FROM university.items i
JOIN university.links l ON l.item_id=i.id
LEFT JOIN university.labels g ON g.id=l.label_id
WHERE i.id=1 OR i.id=2 OR i.id=3 OR i.id=4 ORDER BY i.id,l.id;
INSERT INTO university.links(id,item_id,label_id) VALUES(5,4,NULL);
DELETE FROM university.links WHERE id=4;
SELECT i.id,i.value,g.label FROM university.items i
JOIN university.links l ON l.item_id=i.id
LEFT JOIN university.labels g ON g.id=l.label_id
WHERE i.id<=4 ORDER BY i.id,l.id;
COMMIT;'''

    def assert_join_results(self, report):
        self.assertEqual(report.status,'completed',report.reasons)
        self.assertTrue(report.correctness_verified,report.reasons)
        self.assertTrue(report.cleanup_verified,report.reasons)
        for sample in report.samples:
            self.assertEqual(len(sample.initial_state),3)
            self.assertNotEqual(sample.initial_state,sample.final_state)
            self.assertEqual(sample.commands[1].row_count,4)
            self.assertEqual(sample.commands[1].result_sha256,
                result_digest([(1,700,'alpha'),(1,700,'alpha'),(2,2,None),(3,3,None)],True))
            self.assertEqual(sample.commands[4].result_sha256,
                result_digest([(1,700,'alpha'),(1,700,'alpha'),(2,2,None),(4,4,None)],True))
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            self.assertEqual(connection.execute('SELECT value FROM university.items WHERE id=1').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT id FROM university.links ORDER BY id').fetchall(),[(1,),(2,),(3,),(4,)])

    def test_join_replay_checks_duplicates_nulls_prior_writes_and_all_tables(self):
        text = self.join_fixture()
        report = run_transaction_replay(text,self.settings,self.reference,
            (IndexAction.create('university','links',('item_id',)),),series=2,
            test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assert_join_results(report)
        self.assertEqual(len(report.samples),6)
        self.assertEqual(len(report.created_databases),7)

    def test_join_select_rewrite_matches_context_and_rejects_changed_null_semantics(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        text = self.join_fixture()
        option = next(o for o in generate_transaction_select_rewrites(text,self.settings) if o.rule_id=='or-equality-to-in')
        self.assertEqual(option.number,2)
        result = run_transaction_select_rewrite(text,self.settings,self.reference,2,option.variant_sql,
            test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assert_join_results(result.measurement)
        self.assertFalse(result.measurement.actions)
        wrong = option.variant_sql.replace('LEFT JOIN','INNER JOIN')
        report = run_transaction_select_rewrite(text,self.settings,self.reference,2,wrong,series=1,
            test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
        self.assertTrue(report.initial_states_equal)
        self.assertTrue(report.final_states_equal)
        self.assertFalse(report.results_equal)
        self.assertFalse(report.correctness_verified)
        self.assertFalse(report.recommendation.recommended)
        self.assertTrue(report.cleanup_verified)

    def test_real_sequential_replay_uses_same_state_and_removes_copies(self):
        action = IndexAction.create('university','items',('value',))
        second = IndexAction.create('university','items',('value','id'))
        with patch('pqo.transaction_replay.psycopg.connect',wraps=psycopg.connect) as connect:
            report = run_transaction_replay(TEXT,self.settings,self.reference,(action,second),series=2,
                test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertTrue(all(call.kwargs['dbname']!=self.settings.dbname for call in connect.call_args_list))
        self.assertEqual(report.status,'completed',report.reasons)
        self.assertTrue(report.correctness_verified,report.reasons)
        self.assertTrue(report.cleanup_verified,report.reasons)
        self.assertEqual(len(report.created_databases),7)
        self.assertEqual(len(report.samples),6)
        self.assertTrue(all(name.startswith('pqo_replay_') and name not in (self.reference,self.settings.dbname) for name in report.created_databases))
        self.assertNotEqual(report.samples[0].initial_state,report.samples[0].final_state)
        self.assertGreater(report.samples[1].index_size_bytes,0)
        self.assertGreater(report.samples[1].index_final_size_bytes,0)
        self.assertIsNotNone(report.recommendation)
        self.assertEqual(len(report.recommendation.series),2)
        self.assertEqual(len(report.recommendation.summary),6)
        self.assertEqual(report.recommendation.request_digest,report.request_digest)
        if report.recommendation.recommended:
            self.assertTrue(all(item.gain_passed and not item.regression_reasons and not item.drift_reasons
                for item in report.recommendation.series))
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            self.assertEqual(connection.execute('SELECT value FROM university.items WHERE id=1').fetchone()[0],1)

    def test_select_rewrite_sees_previous_write_and_matches_every_output_and_final_state(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        from test_transaction_select_rewrite import TEXT as rewrite_text
        option=next(o for o in generate_transaction_select_rewrites(rewrite_text,self.settings) if o.rule_id=='subquery-order-without-limit')
        with patch('pqo.transaction_replay.psycopg.connect',wraps=psycopg.connect) as connect:
            result=run_transaction_select_rewrite(rewrite_text,self.settings,self.reference,2,option.variant_sql,
                test_copy_confirmed=True,disposable_databases_confirmed=True)
        report=result.measurement
        self.assertTrue(report.correctness_verified,report.reasons)
        self.assertTrue(report.cleanup_verified)
        self.assertEqual(len(report.created_databases),7)
        self.assertTrue(all(call.kwargs['dbname']!=self.settings.dbname for call in connect.call_args_list))
        self.assertFalse(report.actions)
        self.assertTrue(all(sample.index_size_bytes==sample.index_final_size_bytes==sample.index_build_ms==0 for sample in report.samples))
        expected=[]
        for value in range(100):
            ids=[i for i in range(1,5001) if i%100==value and i!=1]
            expected.append((value,len(ids),sum(ids)))
        expected.append((700,1,1))
        self.assertEqual(report.samples[0].commands[1].result_sha256,result_digest(expected,True))
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            self.assertEqual(connection.execute('SELECT value FROM university.items WHERE id=1').fetchone()[0],1)

    def test_inequivalent_manual_select_is_rejected_even_when_final_data_match(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        from test_transaction_select_rewrite import TEXT as rewrite_text
        option=next(o for o in generate_transaction_select_rewrites(rewrite_text,self.settings) if o.rule_id=='subquery-order-without-limit')
        variant=option.variant_sql.replace('GROUP BY','WHERE q.value<>700 GROUP BY')
        report=run_transaction_select_rewrite(rewrite_text,self.settings,self.reference,2,variant,series=1,
            test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
        self.assertTrue(report.initial_states_equal)
        self.assertTrue(report.final_states_equal)
        self.assertFalse(report.results_equal)
        self.assertFalse(report.correctness_verified)
        self.assertFalse(report.recommendation.recommended)
        self.assertTrue(report.cleanup_verified)

    def test_select_rewrite_cancel_after_write_cleans_every_private_database(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        from test_transaction_select_rewrite import TEXT as rewrite_text
        option=next(o for o in generate_transaction_select_rewrites(rewrite_text,self.settings) if o.rule_id=='subquery-order-without-limit')
        control=AnalysisControl()
        control.progress=lambda message: control.cancel() if message.startswith('Последовательное выполнение: 2') else None
        report=run_transaction_select_rewrite(rewrite_text,self.settings,self.reference,2,option.variant_sql,
            test_copy_confirmed=True,disposable_databases_confirmed=True,control=control).measurement
        self.assertEqual(report.status,'cancelled')
        self.assertTrue(report.cleanup_verified)
        self.assertFalse(report.recommendation.recommended)

    def test_sql_error_and_catalog_refusal_cleanup_without_touching_reference(self):
        action = IndexAction.create('university','items',('value',))
        failing = TEXT.replace('UPDATE university.items SET value=2 WHERE id=1 RETURNING id,value',
            'INSERT INTO university.items(id,value) VALUES(1,5)')
        report = run_transaction_replay(failing,self.settings,self.reference,(action,),test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertEqual(report.status,'error',report.reasons)
        self.assertTrue(report.cleanup_verified)
        self.assertEqual(report.recommendation.status,'inconclusive')
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            connection.execute('ALTER TABLE university.items ALTER COLUMN value SET DEFAULT (random()*100)::integer')
        active = TEXT.replace('UPDATE university.items SET value=2 WHERE id=1 RETURNING id,value',
            'INSERT INTO university.items(id) VALUES(5001)')
        declined = run_transaction_replay(active,self.settings,self.reference,(action,),test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertEqual(declined.status,'declined',declined.reasons)
        self.assertFalse(declined.created_databases)
        self.assertTrue(declined.cleanup_verified)

    def test_cancellation_after_write_still_cleans_all_copies_and_keeps_reference(self):
        control = AnalysisControl()
        def progress(message):
            if message.startswith('Последовательное выполнение: 3'):
                control.cancel()
        control.progress = progress
        report = run_transaction_replay(TEXT,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
            test_copy_confirmed=True,disposable_databases_confirmed=True,control=control)
        self.assertEqual(report.status,'cancelled',report.reasons)
        self.assertTrue(report.cleanup_verified,report.reasons)
        self.assertFalse(report.correctness_verified)
        self.assertFalse(report.recommendation.recommended)
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            self.assertEqual(connection.execute('SELECT value FROM university.items WHERE id=1').fetchone()[0],1)

    def test_real_statement_timeout_after_write_refuses_recommendation_and_cleans_copies(self):
        from pqo.transaction_replay import _timeout
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            connection.execute("INSERT INTO university.items SELECT i,i%100,'fixture' FROM generate_series(5001,100000) i")
            connection.execute('ANALYZE university.items')
        text = ('BEGIN; UPDATE university.items SET value=700 WHERE id=1 RETURNING id,value; '
                'SELECT SUM(CAST(value AS numeric)/3) FROM university.items; COMMIT;')
        control = AnalysisControl()
        running_select = False
        def progress(message):
            nonlocal running_select
            if message.startswith('Последовательное выполнение: 2'):
                running_select = True
        control.progress = progress
        def timeout(connection, deadline, settings):
            _timeout(connection, deadline, settings)
            if running_select:
                connection.execute("SET statement_timeout='1ms'")
        # Actual PostgreSQL cancellation, not a mocked QueryCanceled exception.
        # Limit only the tested SELECT so catalog/state checks remain independent.
        with patch('pqo.transaction_replay._timeout', side_effect=timeout):
            report = run_transaction_replay(text,self.settings,self.reference,
                (IndexAction.create('university','items',('value',)),), series=2,
                test_copy_confirmed=True,disposable_databases_confirmed=True,control=control)
        self.assertTrue(running_select)
        self.assertEqual(report.status,'error',report.reasons)
        self.assertTrue(any('57014' in reason for reason in report.reasons),report.reasons)
        self.assertFalse(report.correctness_verified)
        self.assertFalse(report.recommendation.recommended)
        self.assertTrue(report.cleanup_verified)
        self.assertFalse(report.leftover_databases)
        self.assertEqual(len(report.created_databases),2)
        with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
            self.assertEqual(connection.execute('SELECT value FROM university.items WHERE id=1').fetchone()[0],1)
        with psycopg.connect(**replace(self.settings,dbname='postgres').connection_kwargs()) as connection:
            self.assertFalse(connection.execute('SELECT datname FROM pg_database WHERE datname=ANY(%s)',
                                               (list(report.created_databases),)).fetchall())

    def test_catalog_features_are_refused_before_creating_any_scratch_database(self):
        cases=(
            ('ALTER TABLE university.items ENABLE ROW LEVEL SECURITY','ALTER TABLE university.items DISABLE ROW LEVEL SECURITY'),
            ('CREATE SEQUENCE university.unsupported_sequence','DROP SEQUENCE university.unsupported_sequence'),
            ('CREATE INDEX unsupported_partial ON university.items(value) WHERE value>1','DROP INDEX university.unsupported_partial'),
            ('ALTER TABLE university.items ADD CONSTRAINT unsupported_check CHECK(value>=0)','ALTER TABLE university.items DROP CONSTRAINT unsupported_check'),
            ("CREATE FUNCTION university.replay_trigger() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; CREATE TRIGGER unsupported_trigger BEFORE UPDATE ON university.items FOR EACH ROW EXECUTE FUNCTION university.replay_trigger()",
             'DROP TRIGGER unsupported_trigger ON university.items; DROP FUNCTION university.replay_trigger()'),
        )
        for setup,restore in cases:
            with self.subTest(setup=setup):
                with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
                    connection.execute(setup)
                try:
                    report=run_transaction_replay(TEXT,self.settings,self.reference,(IndexAction.create('university','items',('value',)),),
                        test_copy_confirmed=True,disposable_databases_confirmed=True)
                    self.assertEqual(report.status,'declined',report.reasons)
                    self.assertFalse(report.created_databases)
                    self.assertTrue(report.cleanup_verified)
                finally:
                    with psycopg.connect(**replace(self.settings,dbname=self.reference).connection_kwargs()) as connection:
                        connection.execute(restore)
