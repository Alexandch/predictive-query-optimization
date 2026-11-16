from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from pqo.config import DatabaseSettings
from pqo.transaction_context import ISOLATION_LEVELS, TransactionOptions
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_parameters import ParameterValue, bind_transaction_parameters
from pqo.transaction_planning import collect_transaction_plans
from pqo.transaction_replay_guard import prepare_replay
from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,prepare_select_replacement,_render


class TransactionContextTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings(),dbname='source',allowed_schemas=frozenset({'university'}))
        self.body='UPDATE university.items SET value=7 WHERE id=1; SELECT id,value FROM university.items WHERE value=1 OR value=2 ORDER BY id;'

    def test_all_isolation_levels_and_start_alias_are_preserved(self):
        for level in ISOLATION_LEVELS:
            for prefix in ('BEGIN','BEGIN WORK','BEGIN TRANSACTION','START TRANSACTION'):
                text=prefix+' ISOLATION LEVEL '+level+', READ WRITE, NOT DEFERRABLE; '+self.body+' COMMIT;'
                with self.subTest(level=level,prefix=prefix):
                    inspection=inspect_transaction(text)
                    program=prepare_replay(text,self.settings)
                    self.assertEqual(program.options,TransactionOptions(level,False,False))
                    self.assertEqual(inspection.effective_options,program.options)
                    option=next(o for o in generate_transaction_select_rewrites(text,self.settings) if o.rule_id=='or-equality-to-in')
                    prepared=prepare_select_replacement(text,self.settings,2,option.variant_sql)
                    self.assertEqual(prepared.original.options,prepared.variant.options)
                    self.assertEqual(prepared.original.commands[0],prepared.variant.commands[0])
                    self.assertIn('ISOLATION LEVEL '+level,_render(prepared.variant))

    def test_options_no_commas_readonly_deferrable_and_set_prefix(self):
        text='BEGIN ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE; SELECT id FROM university.items; COMMIT;'
        program=prepare_replay(text,self.settings)
        self.assertEqual(program.effective_options,TransactionOptions('SERIALIZABLE',True,True))
        text='BEGIN ISOLATION LEVEL READ COMMITTED; SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ WRITE; '+self.body+' COMMIT;'
        program=prepare_replay(text,self.settings)
        self.assertEqual(program.effective_options,TransactionOptions('REPEATABLE READ',False,None))
        self.assertEqual(program.commands[0].kind,'set_transaction')

    def test_bindings_preserve_context_controls_names_and_statement_numbers(self):
        text='''START TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ WRITE;
SET TRANSACTION NOT DEFERRABLE;
SAVEPOINT "A";
UPDATE university.items SET value=$1 WHERE id=$2;
ROLLBACK TO SAVEPOINT "A";
RELEASE SAVEPOINT "A";
SELECT id FROM university.items WHERE id=$1;
ROLLBACK;'''
        values=(ParameterValue(3,1,'integer','7'),ParameterValue(3,2,'integer','1'),ParameterValue(6,1,'integer','1'))
        bound=bind_transaction_parameters(text,values)
        original=inspect_transaction(text)
        self.assertEqual(bound.inspection.options,original.options)
        self.assertEqual(bound.inspection.effective_options,original.effective_options)
        self.assertEqual(bound.inspection.ending,'rollback')
        program=prepare_replay(bound.sql_text,self.settings)
        self.assertEqual([c.kind for c in program.commands],['set_transaction','savepoint','update','rollback_to','release','select'])
        self.assertIn('"A"',program.commands[3].sql)

    def test_lexer_does_not_split_literals_dollar_quotes_comments_or_change_parameter_values(self):
        text="BEGIN /* ; BEGIN */ READ WRITE; SELECT ';', $$a;COMMIT;$$ AS x; -- ;\nROLLBACK;"
        inspection=inspect_transaction(text)
        self.assertEqual(len(inspection.statements),1)
        self.assertEqual(inspection.ending,'rollback')
        self.assertEqual(inspection.options.read_only,False)
        bound=bind_transaction_parameters('BEGIN READ ONLY; SELECT $1; COMMIT;',
            (ParameterValue(1,1,'text',"x; COMMIT; BEGIN; --"),))
        self.assertEqual(len(bound.inspection.statements),1)
        self.assertTrue(bound.inspection.options.read_only)

    def test_savepoint_lifetimes_duplicates_and_rollback_ending(self):
        text='BEGIN; SAVEPOINT a; UPDATE university.items SET value=7 WHERE id=1; SAVEPOINT a; ROLLBACK TO a; RELEASE a; ROLLBACK TO a; SELECT id FROM university.items; ROLLBACK;'
        program=prepare_replay(text,self.settings)
        self.assertEqual(program.ending,'rollback')
        self.assertEqual(len(program.commands),7)
        for tail in ('RELEASE a; ROLLBACK TO a;', 'SAVEPOINT b; ROLLBACK TO a; RELEASE b;'):
            with self.subTest(tail=tail),self.assertRaises(ValueError):
                prepare_replay('BEGIN; SAVEPOINT a; '+tail+self.body+' COMMIT;',self.settings)

    def test_unsafe_modes_readonly_writes_and_invalid_savepoints_never_connect(self):
        beginnings=(
            'BEGIN ISOLATION LEVEL UNKNOWN', 'BEGIN ISOLATION LEVEL "SERIALIZABLE"',
            "E'BEGIN'", "BEGIN ISOLATION LEVEL E'SERIALIZABLE'", "BEGIN ISOLATION LEVEL $$SERIALIZABLE$$",
            'BEGIN ISOLATION LEVEL READ COMMITTED, ISOLATION LEVEL SERIALIZABLE',
            'BEGIN READ ONLY, READ WRITE', 'BEGIN READ WRITE,',
            'BEGIN READ WRITE,, NOT DEFERRABLE', 'BEGIN DEFERRABLE NOT DEFERRABLE',
        )
        texts=[prefix+'; '+self.body+' COMMIT;' for prefix in beginnings]
        texts += ['BEGIN READ ONLY; '+self.body+' COMMIT;',
            'BEGIN; '+self.body+' SET TRANSACTION ISOLATION LEVEL SERIALIZABLE; COMMIT;',
            'BEGIN; SET TRANSACTION SNAPSHOT \'abc\'; '+self.body+' COMMIT;',
            'BEGIN; SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL SERIALIZABLE; '+self.body+' COMMIT;',
            'BEGIN; RELEASE SAVEPOINT unknown; '+self.body+' COMMIT;',
            'BEGIN; SAVEPOINT "bad;name"; '+self.body+' COMMIT;',
            'BEGIN; '+self.body+' COMMIT AND CHAIN;']
        for text in texts:
            with self.subTest(text=text),patch('psycopg.connect') as connection,self.assertRaises(ValueError):
                prepare_replay(text,self.settings)
            connection.assert_not_called()
        for type_name in ('university.int4','pg_catalog.custom_type'):
            with self.subTest(type_name=type_name),self.assertRaises(ValueError):
                prepare_replay('BEGIN; UPDATE university.items SET value=1 WHERE id=1; SELECT CAST(id AS '+type_name+') FROM university.items; COMMIT;',self.settings)

    def test_controls_have_no_explain_and_are_not_reported_as_sql_queries(self):
        text='BEGIN ISOLATION LEVEL SERIALIZABLE; SAVEPOINT a; ROLLBACK TO a; RELEASE a; COMMIT;'
        with patch('pqo.transaction_planning.psycopg.connect') as connection:
            report=collect_transaction_plans(text,self.settings)
        connection.assert_not_called()
        self.assertTrue(all(s.status=='control' and s.plan_json is None for s in report.statements))
