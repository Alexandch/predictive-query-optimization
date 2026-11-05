from dataclasses import replace
from unittest import TestCase
from unittest.mock import MagicMock,patch

from pqo.analysis_control import AnalysisControl
from pqo.config import DatabaseSettings
from pqo.transaction_replay_guard import prepare_replay
from pqo.transaction_select_rewrite import (generate_transaction_select_rewrites,prepare_select_replacement,
    run_transaction_select_rewrite,select_rewrite_request_digest)
from pqo.transaction_mixed_recommendation import MixedReplayPolicy,assess_mixed_replay


TEXT = '''BEGIN;
UPDATE university.items SET value=700 WHERE id=1 RETURNING id,value;
SELECT q.value,COUNT(*) AS n,SUM(q.id) AS total
FROM (SELECT id,value FROM university.items ORDER BY label,id DESC) AS q
GROUP BY q.value ORDER BY q.value;
SELECT id,value FROM university.items WHERE id=1 ORDER BY id;
COMMIT;'''


class TransactionSelectRewriteTests(TestCase):
    def setUp(self):
        self.settings=replace(DatabaseSettings(),dbname='source',allowed_schemas=frozenset({'university'}))

    def option(self):
        return next(item for item in generate_transaction_select_rewrites(TEXT,self.settings) if item.rule_id=='subquery-order-without-limit')

    def test_local_generation_and_replacement_preserve_every_other_command(self):
        with patch('pqo.transaction_replay.psycopg.connect') as connect:
            option=self.option()
            prepared=prepare_select_replacement(TEXT,self.settings,option.number,option.variant_sql)
        connect.assert_not_called()
        self.assertEqual(option.number,2)
        self.assertNotIn('ORDER BY label',prepared.variant_sql)
        self.assertIn('ORDER BY q.value',prepared.variant_sql)
        self.assertEqual(prepared.original.tables,prepared.variant.tables)
        self.assertEqual(prepared.original.commands[0],prepared.variant.commands[0])
        self.assertEqual(prepared.original.commands[2],prepared.variant.commands[2])
        self.assertNotEqual(prepared.original.commands[1],prepared.variant.commands[1])

    def test_existing_index_replay_contract_is_not_silently_widened(self):
        with self.assertRaises(ValueError): prepare_replay(TEXT,self.settings)
        self.assertEqual(len(prepare_replay(TEXT,self.settings,allow_select_wrapper=True).commands),3)

    def test_only_single_aggregate_from_wrapper_is_allowed(self):
        for select in (
            'SELECT * FROM (SELECT id FROM university.items) q',
            'SELECT (SELECT count(*) FROM university.items) FROM university.items',
            'SELECT count(*) FROM (SELECT * FROM (SELECT id FROM university.items) x) q',
            'WITH q AS (SELECT id FROM university.items) SELECT count(*) FROM q',
            'SELECT count(*) FROM (SELECT id FROM university.items FOR UPDATE) q',
            'SELECT count(*) FROM (SELECT id FROM university.items OFFSET 2) q',
            'SELECT count(*) FROM (SELECT custom_fn(id) FROM university.items) q',
            'SELECT count(*) FROM (SELECT id FROM university.items) q(x)',
            'SELECT count(*) FROM (SELECT id FROM university.items) q JOIN university.items z ON true',
        ):
            text='BEGIN; UPDATE university.items SET value=2 WHERE id=1; '+select+'; COMMIT;'
            with self.subTest(select=select),self.assertRaises(ValueError):
                generate_transaction_select_rewrites(text,self.settings)

    def test_replacements_reject_dml_multiple_commands_new_tables_and_output_order_changes(self):
        option=self.option()
        for number,variant in ((1,option.variant_sql),(True,option.variant_sql),(99,option.variant_sql),
                (2,'DELETE FROM university.items WHERE id=1'),(2,option.variant_sql+'; SELECT * FROM university.items'),
                (2,option.variant_sql.replace('university.items','university.other')),
                (2,option.variant_sql.replace('ORDER BY q.value','ORDER BY q.value DESC')),
                (2,option.variant_sql+' LIMIT 2'),(2,option.original_sql)):
            with self.subTest(number=number,variant=variant),patch('pqo.transaction_replay.psycopg.connect') as connect,self.assertRaises(ValueError):
                run_transaction_select_rewrite(TEXT,self.settings,'lab',number,variant,test_copy_confirmed=True,disposable_databases_confirmed=True)
            connect.assert_not_called()

    def test_manual_variant_may_change_filter_but_must_be_measured_not_presumed_equivalent(self):
        option=self.option()
        prepared=prepare_select_replacement(TEXT,self.settings,2,option.variant_sql.replace('GROUP BY','WHERE q.value<>700 GROUP BY'))
        self.assertEqual(prepared.number,2)
        self.assertNotEqual(prepared.original_sql,prepared.variant_sql)

    def test_flat_supported_rules_and_honest_manual_fallback(self):
        for predicate,rule in (('value=1 OR value=2','or-equality-to-in'),('value>=1 AND value<=2','range-to-between')):
            text=f'BEGIN; UPDATE university.items SET value=3 WHERE id=1; SELECT id FROM university.items WHERE {predicate}; COMMIT;'
            options=generate_transaction_select_rewrites(text,self.settings)
            self.assertIn(rule,{option.rule_id for option in options})
        options=generate_transaction_select_rewrites('BEGIN; SELECT id FROM university.items; UPDATE university.items SET value=1 WHERE id=1; COMMIT;',self.settings)
        self.assertEqual([option.rule_id for option in options],['manual'])

    def test_unconfirmed_unsupported_cancelled_and_invalid_policy_never_connect(self):
        variant=self.option().variant_sql
        for kwargs in ({},{'test_copy_confirmed':True}, {'test_copy_confirmed':True,'disposable_databases_confirmed':True,'series':True},
                {'test_copy_confirmed':True,'disposable_databases_confirmed':True,'policy':'invalid'}):
            with patch('pqo.transaction_replay.psycopg.connect') as connect,self.assertRaises(ValueError):
                run_transaction_select_rewrite(TEXT,self.settings,'lab',2,variant,**kwargs)
            connect.assert_not_called()
        control=AnalysisControl(); control.cancel()
        with patch('pqo.transaction_replay.psycopg.connect') as connect,self.assertRaises(RuntimeError):
            run_transaction_select_rewrite(TEXT,self.settings,'lab',2,variant,control=control)
        connect.assert_not_called()

    def test_provenance_binds_selected_number_variant_policy_and_context_not_password(self):
        variant=self.option().variant_sql; policy=MixedReplayPolicy()
        digest=lambda text,settings,reference,number,variant,series,policy: select_rewrite_request_digest(text,settings,reference,number,variant,series,policy)
        base=(TEXT,self.settings,'lab',2,variant,2,policy)
        original=digest(*base)
        for index,value in ((0,TEXT+' --changed'),(1,replace(self.settings,dbname='other')),(2,'otherlab'),(3,3),
                (4,variant.replace('SUM(q.id)','MAX(q.id)')),(5,3),(6,replace(policy,minimum_gain_ms=6.))):
            changed=list(base); changed[index]=value
            self.assertNotEqual(original,digest(*changed))
        self.assertEqual(original,digest(TEXT,replace(self.settings,password='SECRET'),'lab',2,variant,2,policy))

    def test_rewrite_assessment_requires_no_indexes_and_does_not_weaken_index_assessment(self):
        from test_transaction_mixed_recommendation import MixedRecommendationTests
        report=MixedRecommendationTests().fixture()
        report=replace(report,actions=(),samples=tuple(replace(sample,index_size_bytes=0,index_final_size_bytes=0,index_build_ms=0.) for sample in report.samples))
        self.assertTrue(assess_mixed_replay(report,report.policy,expected_request_digest=report.request_digest,mode='rewrite').recommended)
        self.assertFalse(assess_mixed_replay(report,report.policy,expected_request_digest=report.request_digest).recommended)
        samples=list(report.samples); samples[1]=replace(samples[1],index_size_bytes=1024)
        self.assertFalse(assess_mixed_replay(replace(report,samples=tuple(samples)),report.policy,expected_request_digest=report.request_digest,mode='rewrite').recommended)

    def test_shared_lifecycle_uses_variant_only_in_b_and_no_index_actions(self):
        from test_transaction_replay import TransactionReplayTests
        original=TransactionReplayTests()
        scratch=MagicMock(); scratch.created={'pqo_replay_seed':(1,10)}
        scratch.create.side_effect=['pqo_replay_seed','pqo_replay_A1','pqo_replay_B','pqo_replay_A2']
        scratch.remove.return_value=True; scratch.finish.return_value=(True,())
        connection=MagicMock(); connection.__enter__.return_value=connection
        connection.execute.return_value.fetchone.return_value=(1024,)
        programs=[]
        def sample(settings,database,program,actions,series,phase,deadline,control):
            programs.append((program,actions,phase))
            return replace(original.sample(phase),database=database)
        with patch('pqo.transaction_replay.ScratchDatabases',return_value=scratch),patch('pqo.transaction_replay.psycopg.connect',return_value=connection), \
                patch('pqo.transaction_replay.replay_catalog'),patch('pqo.transaction_replay._sample',side_effect=sample):
            result=run_transaction_select_rewrite(TEXT,self.settings,'lab',2,self.option().variant_sql,series=1,
                test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertEqual(programs[0][0],programs[2][0]); self.assertNotEqual(programs[0][0],programs[1][0])
        self.assertTrue(all(not actions for _,actions,_ in programs))
        self.assertTrue(result.measurement.cleanup_verified)
        self.assertFalse(result.measurement.recommendation.recommended)
        scratch.finish.assert_called_once()
