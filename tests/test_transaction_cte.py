import os
from dataclasses import replace
from unittest import TestCase, skipUnless
from unittest.mock import patch

from sqlglot import parse_one

from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_cte import cte_catalog, physical_tables
from pqo.transaction_defaults import used_defaults
from pqo.transaction_replay import result_digest, run_transaction_replay
from pqo.transaction_replay_guard import prepare_replay


class CTEContractTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname='source', allowed_schemas=frozenset({'retail'}))
        self.columns = {('retail','stock'): ('id','account_id','batch_id','quantity','label'),
            ('retail','adjustments'): ('id','account_id','batch_id','quantity','group_id')}
        self.keys = {('retail','stock'): (('id',),), ('retail','adjustments'): (('id',),('account_id','batch_id'))}

    def program(self, command):
        return prepare_replay('BEGIN;UPDATE retail.stock SET quantity=1 WHERE id=1;'+command+
            ';SELECT id FROM retail.stock ORDER BY id;COMMIT;', self.settings)

    def catalog(self, command):
        p = self.program(command)
        return cte_catalog(parse_one(p.commands[1].sql,read='postgres'),self.columns,self.keys)

    def test_scope_chains_hints_quoted_names_and_physical_identity_are_preserved(self):
        command = '''WITH "Rows"(pk,amount) AS MATERIALIZED (SELECT id,quantity FROM retail.stock),
            selected AS NOT MATERIALIZED (SELECT * FROM "Rows" WHERE pk=1)
            SELECT * FROM selected ORDER BY pk'''
        p = self.program(command)
        self.assertEqual(p.tables, (('retail','stock'),))
        self.assertIn('AS MATERIALIZED',p.commands[1].sql)
        self.assertIn('AS NOT MATERIALIZED',p.commands[1].sql)
        tree = parse_one(p.commands[1].sql,read='postgres')
        self.assertEqual([(t.db,t.name) for t in physical_tables(tree)],[('retail','stock')])
        catalog = self.catalog(command)
        self.assertEqual(catalog['','Rows'].columns,('pk','amount'))
        self.assertEqual(catalog['','selected'].keys,(('pk',),))
        self.assertFalse(catalog['','selected'].pushdown)

    def test_exact_projection_keeps_composite_keys_but_computation_grouping_and_join_do_not(self):
        catalog = self.catalog('''WITH q AS (SELECT account_id AS a,batch_id AS b,quantity FROM retail.adjustments)
            SELECT * FROM q''')
        self.assertEqual(catalog['','q'].keys,(('a','b'),))
        for source in ('SELECT account_id,quantity FROM retail.adjustments',
                'SELECT id+0 AS id,quantity FROM retail.adjustments',
                'SELECT account_id,SUM(quantity) AS quantity FROM retail.adjustments GROUP BY account_id',
                'SELECT a.id,a.quantity FROM retail.adjustments a JOIN retail.stock s ON a.id=s.id'):
            with self.subTest(source=source):
                self.assertFalse(self.catalog('WITH q AS ('+source+') SELECT * FROM q')['','q'].keys)
        for source in ('SELECT SUM(quantity) AS n FROM retail.adjustments','SELECT 1 AS id,2 AS n'):
            self.assertEqual(self.catalog('WITH q AS ('+source+') SELECT * FROM q')['','q'].keys,((),))

    def test_materialization_reuse_and_limit_are_barriers_to_outer_index_predicates(self):
        for hint, suffix, expected in (('', '', True),('MATERIALIZED','',False),
                ('NOT MATERIALIZED','',True),('', ' ORDER BY id LIMIT 2',False)):
            q=self.catalog(f'WITH q AS {hint} (SELECT id,quantity FROM retail.stock{suffix}) SELECT * FROM q')
            self.assertEqual(q['','q'].pushdown,expected)
            self.assertEqual(q['','q'].keys,(('id',),))
        for hint, expected in (('',False),('NOT MATERIALIZED',True)):
            q=self.catalog(f'WITH q AS {hint} (SELECT id,quantity FROM retail.stock) SELECT a.id FROM q a JOIN q b ON a.id=b.id')
            self.assertEqual(q['','q'].pushdown,expected)

    def test_insert_star_uses_projected_column_order_and_only_omitted_target_defaults(self):
        for source in ('SELECT account_id,batch_id,quantity FROM retail.adjustments',
                'SELECT a.* FROM retail.adjustments a'):
            columns = ('id','account_id','batch_id','quantity','group_id') if 'a.*' in source else ('account_id','batch_id','quantity')
            command = 'WITH q AS ('+source+') INSERT INTO retail.stock('+','.join(columns)+') SELECT * FROM q'
            p=self.program(command)
            if 'a.*' not in source:
                self.assertEqual(used_defaults(p,'retail','stock',self.columns['retail','stock'],source_columns=self.columns),{'id','label'})
            else:
                with self.assertRaises(ValueError):
                    used_defaults(p,'retail','stock',self.columns['retail','stock'],source_columns=self.columns)

    def test_unsupported_cte_shapes_and_effects_are_rejected_before_any_connection(self):
        for command in (
            'WITH RECURSIVE q AS (SELECT id FROM retail.stock) SELECT * FROM q',
            'WITH q AS (DELETE FROM retail.stock WHERE id=1 RETURNING id) SELECT * FROM q',
            'WITH q AS (SELECT id FROM retail.stock UNION ALL SELECT id FROM retail.stock) SELECT * FROM q',
            'WITH q AS (SELECT id FROM q) SELECT * FROM q',
            'WITH q AS (SELECT id FROM later),later AS (SELECT id FROM retail.stock) SELECT * FROM q',
            'WITH q AS (SELECT id FROM retail.stock),q AS (SELECT id FROM retail.stock) SELECT * FROM q',
            'WITH q AS (WITH nested AS (SELECT id FROM retail.stock) SELECT * FROM nested) SELECT * FROM q',
            'WITH q AS (SELECT id FROM retail.stock) SELECT * FROM unknown',
            'WITH q AS (SELECT id FROM stock) SELECT * FROM q',
            'WITH q AS (SELECT id FROM other.stock) SELECT * FROM q',
            'WITH q AS (SELECT random() AS id FROM retail.stock) SELECT * FROM q',
            'WITH q AS (SELECT id FROM retail.stock FOR UPDATE) SELECT * FROM q',
            'WITH q AS (SELECT id FROM retail.stock LIMIT 1) SELECT * FROM q',
            'WITH q AS (SELECT id FROM retail.stock) UPDATE q SET id=2 WHERE id=1',
            'WITH q AS (SELECT id FROM retail.stock) SELECT * FROM (SELECT id FROM q) s',
        ):
            with self.subTest(command=command), patch('pqo.transaction_replay.psycopg.connect') as connect,self.assertRaises(ValueError):
                run_transaction_replay('BEGIN;UPDATE retail.stock SET quantity=1 WHERE id=1;'+command+
                    ';SELECT id FROM retail.stock;COMMIT;',self.settings,'lab',(IndexAction.create('retail','stock',('quantity',)),),
                    test_copy_confirmed=True,disposable_databases_confirmed=True)
            connect.assert_not_called()

    def test_unknown_ambiguous_duplicate_and_unnamed_cte_outputs_are_not_guessed(self):
        for source in ('SELECT quantity+1 FROM retail.stock', 'SELECT id,id FROM retail.stock',
                'SELECT missing AS id FROM retail.stock','SELECT id FROM retail.stock a JOIN retail.adjustments b ON a.id=b.id'):
            with self.subTest(source=source),self.assertRaises(ValueError):
                self.catalog('WITH q AS ('+source+') SELECT * FROM q')
        with self.assertRaises(ValueError):
            self.catalog('WITH q(a) AS (SELECT id,quantity FROM retail.stock) SELECT * FROM q')

    def test_not_materialized_option_preserves_other_commands_and_does_not_duplicate_limit(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,prepare_select_replacement
        text='''BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;
            WITH q AS MATERIALIZED (SELECT id,quantity FROM retail.stock)
            SELECT a.id,b.quantity FROM q a JOIN q b ON a.id=b.id WHERE a.id=1 ORDER BY a.id;
            SELECT id FROM retail.stock;COMMIT;'''
        option=next(x for x in generate_transaction_select_rewrites(text,self.settings) if x.rule_id=='inline-reused-cte')
        prepared=prepare_select_replacement(text,self.settings,option.number,option.variant_sql)
        self.assertIn('NOT MATERIALIZED',prepared.variant_sql)
        self.assertEqual(prepared.original.commands[0],prepared.variant.commands[0])
        self.assertEqual(prepared.original.commands[2],prepared.variant.commands[2])
        limited=text.replace('SELECT id,quantity FROM retail.stock)', 'SELECT id,quantity FROM retail.stock ORDER BY id LIMIT 2)')
        self.assertFalse(any(x.rule_id=='inline-reused-cte' for x in generate_transaction_select_rewrites(limited,self.settings)))

    def test_definition_budget_and_scope_do_not_hide_a_physical_table_of_the_same_name(self):
        chain=','.join(f'q{i} AS (SELECT id FROM '+('retail.stock' if i==0 else f'q{i-1}')+')' for i in range(8))
        self.program('WITH '+chain+' SELECT id FROM q7')
        with self.assertRaises(ValueError):
            self.program('WITH '+chain+',q8 AS (SELECT id FROM q7) SELECT id FROM q8')
        p=self.program('WITH stock AS (SELECT id FROM retail.stock) SELECT a.id FROM stock a JOIN retail.stock b ON a.id=b.id')
        self.assertEqual([(t.db,t.name) for t in physical_tables(parse_one(p.commands[1].sql,read='postgres'))],
            [('retail','stock'),('retail','stock')])


@skipUnless(os.getenv('PQO_DISPOSABLE_REPLAY_TESTS')=='1','Own CTE fixtures and disposable databases only')
class CTEPostgresTests(TestCase):
    def setUp(self):
        from test_transaction_joined_dml import JoinedDMLPostgresTests
        self.fixture=JoinedDMLPostgresTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.settings,self.reference=self.fixture.settings,self.fixture.reference

    def test_cte_chains_aggregates_reuse_and_previous_writes_at_all_isolations(self):
        with self.fixture.connect() as c:c.execute("INSERT INTO retail.groups VALUES(3,'none')")
        text='''BEGIN ISOLATION LEVEL {isolation};UPDATE retail.adjustments SET quantity=quantity+1 WHERE id=1;
            WITH raw(pk,account,batch,quantity,gid) AS MATERIALIZED
                (SELECT id,account_id,batch_id,quantity,group_id FROM retail.adjustments),
            selected AS NOT MATERIALIZED (SELECT * FROM raw WHERE quantity<100),
            totals AS (SELECT gid,COUNT(*) AS n,SUM(quantity) AS units FROM selected GROUP BY gid)
            SELECT g.id,g.label,t.n,t.units FROM retail.groups g LEFT JOIN totals t ON t.gid=g.id ORDER BY g.id;
            WITH q AS (SELECT id,quantity FROM retail.adjustments)
            SELECT a.id,b.quantity FROM q a JOIN q b ON a.id=b.id WHERE a.id=1 ORDER BY a.id,b.quantity;
            SELECT id FROM retail.stock WHERE id=1;{ending};'''
        for isolation,ending in (('READ UNCOMMITTED','COMMIT'),('READ COMMITTED','COMMIT'),
                ('REPEATABLE READ','COMMIT'),('SERIALIZABLE','ROLLBACK')):
            with self.subTest(isolation=isolation):
                r=self.fixture.replay(text.format(isolation=isolation,ending=ending))
                self.assertTrue(r.correctness_verified,r.reasons)
                for sample in r.samples:
                    self.assertEqual(sample.commands[1].result_sha256,result_digest([(1,'one',1,6),(2,'two',1,7),(3,'none',None,None)],True))
                    self.assertEqual(sample.commands[2].result_sha256,result_digest([(1,6)],True))
                    if ending=='ROLLBACK':self.assertEqual(sample.initial_state,sample.final_state)

    def test_cte_joined_writes_composite_keys_and_savepoints(self):
        r=self.fixture.replay('''BEGIN;UPDATE retail.adjustments SET quantity=quantity+1 WHERE id=1;
            WITH delta AS MATERIALIZED (SELECT account_id AS acct,batch_id AS batch,quantity AS increase FROM retail.adjustments WHERE id<=2)
            UPDATE retail.stock t SET quantity=t.quantity+d.increase FROM delta d
            WHERE t.account_id=d.acct AND t.batch_id=d.batch RETURNING t.id,t.quantity;
            SAVEPOINT discarded;
            WITH doomed AS NOT MATERIALIZED (SELECT id AS pk FROM retail.stock WHERE id=3)
            DELETE FROM retail.stock t USING doomed d WHERE t.id=d.pk RETURNING t.id,t.quantity;
            SELECT COUNT(*) FROM retail.stock;
            ROLLBACK TO discarded;RELEASE discarded;
            WITH keep AS (SELECT id,label FROM retail.groups WHERE id=2)
            DELETE FROM retail.stock t USING keep k WHERE t.id=k.id RETURNING t.id,k.label;
            SELECT id,quantity,label FROM retail.stock ORDER BY id;COMMIT;''')
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[1].result_sha256,result_digest([(1,16),(2,27)],False))
            self.assertEqual(sample.commands[3].result_sha256,result_digest([(3,30)],False))
            self.assertEqual(sample.commands[4].result_sha256,result_digest([(3,)],False))
            self.assertEqual(sample.commands[7].result_sha256,result_digest([(2,'two')],False))
            self.assertEqual(sample.commands[8].result_sha256,result_digest([(1,16,'old'),(3,30,'old'),(4,40,'old')],True))

    def test_cte_insert_projection_stars_defaults_and_sequence_state(self):
        r=self.fixture.replay('''BEGIN;
            WITH q(acct,batch,units) AS (SELECT account_id,batch_id,quantity FROM retail.adjustments WHERE id=3)
            INSERT INTO retail.stock(account_id,batch_id,quantity) SELECT q.* FROM q RETURNING id,label;
            WITH q AS (SELECT id FROM retail.stock WHERE id=5)
            UPDATE retail.stock t SET label=DEFAULT FROM q WHERE t.id=q.id RETURNING t.id,t.quantity,t.label;
            SELECT id,quantity,label FROM retail.stock WHERE id=5;COMMIT;''')
        self.assertTrue(r.correctness_verified,r.reasons)
        for sample in r.samples:
            self.assertEqual(sample.commands[0].result_sha256,result_digest([(5,'new')],False))
            self.assertEqual(sample.commands[1].result_sha256,result_digest([(5,999,'new')],False))
            self.assertNotEqual(sample.initial_sequences,sample.final_sequences)

    def test_global_aggregate_singleton_and_constant_sources_are_proven(self):
        r=self.fixture.replay('''BEGIN;
            WITH stats AS (SELECT MAX(id) AS match_id,SUM(quantity) AS amount FROM retail.adjustments)
            UPDATE retail.stock t SET quantity=s.amount FROM stats s WHERE t.id=s.match_id RETURNING t.id,t.quantity;
            WITH settings AS (SELECT 1 AS pk,2 AS factor)
            UPDATE retail.stock t SET quantity=t.quantity*s.factor FROM settings s WHERE t.id=s.pk RETURNING t.id,t.quantity;
            SELECT id,quantity FROM retail.stock WHERE id=3;COMMIT;''')
        self.assertTrue(r.correctness_verified,r.reasons)
        self.assertEqual(r.samples[0].commands[0].result_sha256,result_digest([(3,1011)],False))
        self.assertEqual(r.samples[0].commands[1].result_sha256,result_digest([(1,20)],False))

    def test_lost_keys_unknown_outputs_and_ambiguous_update_decline_before_cloning(self):
        for source in ('SELECT account_id,quantity FROM retail.adjustments',
                'SELECT id+0 AS account_id,quantity FROM retail.adjustments',
                'SELECT account_id,SUM(quantity) AS quantity FROM retail.adjustments GROUP BY account_id',
                'SELECT missing AS account_id,quantity FROM retail.adjustments'):
            with self.subTest(source=source):
                r=self.fixture.replay('BEGIN;WITH q AS ('+source+''') UPDATE retail.stock t
                    SET quantity=q.quantity FROM q WHERE t.account_id=q.account_id;
                    SELECT id FROM retail.stock;COMMIT;''')
                self.assertEqual(r.status,'declined',r.reasons)
                self.assertFalse(r.created_databases)

    def test_four_way_cte_materialization_comparison_and_wrong_variant_gate(self):
        from pqo.transaction_select_rewrite import generate_transaction_select_rewrites,run_transaction_select_rewrite
        from pqo.transaction_optimization import run_transaction_optimization
        text='''BEGIN ISOLATION LEVEL REPEATABLE READ;
            WITH delta AS (SELECT id,quantity FROM retail.adjustments)
            UPDATE retail.stock t SET quantity=d.quantity FROM delta d WHERE t.id=d.id AND t.id=1 RETURNING t.id,t.quantity;
            WITH q AS MATERIALIZED (SELECT id,quantity FROM retail.stock)
            SELECT a.id,a.quantity,b.id,b.quantity FROM q a JOIN q b ON a.id=b.id WHERE a.id=1 ORDER BY a.id;
            SELECT id,quantity FROM retail.stock WHERE id=1;COMMIT;'''
        option=next(x for x in generate_transaction_select_rewrites(text,self.settings) if x.rule_id=='inline-reused-cte')
        before=self.fixture.fingerprint()
        r=run_transaction_optimization(text,self.settings,self.reference,2,option.variant_sql,
            (IndexAction.create('retail','stock',('account_id',)),),series=2,
            test_copy_confirmed=True,disposable_databases_confirmed=True)
        self.assertTrue(r.comparable and r.cleanup_verified,r.reasons)
        self.assertEqual(len(r.observations),14)
        for observation in r.observations:
            self.assertEqual(observation.sample.commands[0].result_sha256,result_digest([(1,5)],False))
            self.assertEqual(observation.sample.commands[1].result_sha256,result_digest([(1,5,1,5)],True))
        bad=option.variant_sql.replace('a.id = 1','a.id = 2')
        self.assertNotEqual(bad,option.variant_sql)
        failure=run_transaction_select_rewrite(text,self.settings,self.reference,2,bad,series=1,
            test_copy_confirmed=True,disposable_databases_confirmed=True).measurement
        self.assertFalse(failure.correctness_verified)
        self.assertTrue(failure.cleanup_verified)
        self.assertFalse(failure.recommendation.recommended)
        self.assertEqual(before,self.fixture.fingerprint())

    def test_cte_write_plans_and_index_candidates_do_not_execute_any_write(self):
        from pqo.transaction_planning import collect_transaction_plans
        from pqo.transaction_indexes import collect_transaction_index_candidates
        text='''BEGIN;
            WITH q AS (SELECT id,quantity FROM retail.adjustments WHERE account_id=10)
            UPDATE retail.stock t SET quantity=q.quantity FROM q WHERE t.id=q.id;
            WITH q AS (SELECT id FROM retail.adjustments WHERE account_id=10)
            DELETE FROM retail.stock t USING q WHERE t.id=q.id;
            WITH q AS (SELECT account_id AS customer,quantity FROM retail.stock)
            SELECT q.quantity FROM q WHERE q.customer=20;ROLLBACK;'''
        settings=replace(self.settings,dbname=self.reference)
        before=self.fixture.fingerprint()
        plans=collect_transaction_plans(text,settings)
        self.assertEqual([p.status for p in plans.statements],['planned']*3)
        candidates=collect_transaction_index_candidates(text,settings,plans=plans)
        self.assertTrue(any(x.action.table_name=='stock' and x.action.key_columns==('account_id',) for x in candidates.candidates))
        self.assertTrue(all(x.action.schema_name=='retail' for x in candidates.candidates))
        self.assertEqual(before,self.fixture.fingerprint())

    def test_cancel_after_cte_write_and_expression_error_never_recommend_and_cleanup(self):
        from pqo.analysis_control import AnalysisControl
        text='''BEGIN;WITH q AS (SELECT id,quantity FROM retail.adjustments)
            UPDATE retail.stock t SET quantity=q.quantity FROM q WHERE t.id=q.id AND t.id=1;
            WITH q AS (SELECT id,quantity FROM retail.stock) SELECT * FROM q ORDER BY id;COMMIT;'''
        control=AnalysisControl(progress=lambda message:control.cancel() if '2 SELECT;' in message else None)
        result=self.fixture.replay(text,control=control)
        self.assertEqual(result.status,'cancelled',result.reasons)
        self.assertFalse(result.correctness_verified or result.recommendation.recommended)
        failed=text.replace('SELECT id,quantity FROM retail.stock)', 'SELECT id,quantity/0 AS quantity FROM retail.stock)')
        result=self.fixture.replay(failed)
        self.assertEqual(result.status,'error',result.reasons)
        self.assertTrue(any('22012' in reason for reason in result.reasons))
        self.assertFalse(result.recommendation.recommended)

    def test_declared_star_column_positions_cannot_turn_a_nonunique_column_into_a_key(self):
        prefix='WITH q(pk,acct,batch,units,gid) AS (SELECT * FROM retail.adjustments) '
        bad=self.fixture.replay('BEGIN;'+prefix+'''UPDATE retail.stock t SET quantity=q.units
            FROM q WHERE t.account_id=q.acct;SELECT id FROM retail.stock;COMMIT;''')
        self.assertEqual(bad.status,'declined',bad.reasons)
        self.assertFalse(bad.created_databases)
        good=self.fixture.replay('BEGIN;'+prefix+'''UPDATE retail.stock t SET quantity=q.units
            FROM q WHERE t.account_id=q.acct AND t.batch_id=q.batch RETURNING t.id,t.quantity;
            SELECT id FROM retail.stock;COMMIT;''')
        self.assertTrue(good.correctness_verified,good.reasons)
        self.assertEqual(good.samples[0].commands[0].result_sha256,result_digest([(1,5),(2,7)],False))

    def test_deferred_composite_unique_key_is_not_restored_by_a_cte_projection(self):
        with self.fixture.connect() as c:
            c.execute('ALTER TABLE retail.adjustments DROP CONSTRAINT adjustments_account_id_batch_id_key')
            c.execute('ALTER TABLE retail.adjustments ADD UNIQUE(account_id,batch_id) DEFERRABLE INITIALLY IMMEDIATE')
        r=self.fixture.replay('''BEGIN;WITH q AS (SELECT account_id,batch_id,quantity FROM retail.adjustments)
            UPDATE retail.stock t SET quantity=q.quantity FROM q
            WHERE t.account_id=q.account_id AND t.batch_id=q.batch_id;
            SELECT id FROM retail.stock;COMMIT;''')
        self.assertEqual(r.status,'declined',r.reasons)
        self.assertFalse(r.created_databases)
