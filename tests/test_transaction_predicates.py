import os
from dataclasses import replace
from unittest import TestCase, skipUnless

from sqlglot import parse_one

from pqo.config import DatabaseSettings
from pqo.transaction_cte import cte_catalog
from pqo.transaction_defaults import select_projection_width
from pqo.transaction_predicates import validate_predicate_columns
from pqo.transaction_replay_guard import prepare_replay
from pqo.transaction_select_rewrite import generate_transaction_select_rewrites, prepare_select_replacement


class PredicateContractTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), dbname='source', allowed_schemas=frozenset({'retail'}))
        self.columns = {('retail', 'stock'): ('id', 'account_id', 'batch_id', 'quantity', 'label'),
            ('retail', 'adjustments'): ('id', 'account_id', 'batch_id', 'quantity', 'group_id'),
            ('retail', 'groups'): ('id', 'label')}

    def program(self, command):
        return prepare_replay('BEGIN;UPDATE retail.stock SET quantity=1 WHERE id=1;' + command +
            ';SELECT id FROM retail.stock ORDER BY id;COMMIT;', self.settings)

    def test_nested_correlations_null_sensitive_not_in_and_cte_references(self):
        for command in (
            '''SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.adjustments a
                WHERE a.account_id=t.account_id AND NOT EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=a.group_id))''',
            'SELECT t.id FROM retail.stock t WHERE t.id NOT IN (SELECT a.group_id FROM retail.adjustments a)',
            '''WITH q AS (SELECT id,account_id FROM retail.stock WHERE EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=stock.id))
                SELECT q.id FROM q WHERE q.id IN(SELECT a.id FROM retail.adjustments a WHERE a.account_id=q.account_id)''',
            'UPDATE retail.stock t SET quantity=2 WHERE EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=t.id LIMIT 1)',
            'DELETE FROM retail.stock t WHERE t.id IN(SELECT COUNT(*) FROM retail.groups)',
        ):
            with self.subTest(command=command):
                p = self.program(command)
                validate_predicate_columns(parse_one(p.commands[1].sql, read='postgres'), self.columns)
        from sqlglot import exp
        tree = parse_one(self.program('SELECT id FROM retail.stock WHERE id NOT IN(SELECT id FROM retail.groups)').commands[1].sql, read='postgres')
        self.assertIs(type(tree.args['where'].this), exp.Not)
        self.assertIs(type(tree.args['where'].this.this), exp.In)

    def test_shadowed_alias_missing_local_column_ambiguity_and_cte_boundary_decline(self):
        for command in (
            'SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.groups t WHERE t.quantity=1)',
            '''SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.adjustments a
                JOIN retail.groups g ON a.group_id=g.id WHERE id=t.id)''',
            'WITH q AS(SELECT id FROM retail.stock WHERE EXISTS(SELECT 1 WHERE t.id=1)) SELECT t.id FROM q t',
            'SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.groups g WHERE unknown.id=t.id)',
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                p = self.program(command)
                validate_predicate_columns(parse_one(p.commands[1].sql, read='postgres'), self.columns)

    def test_inner_alias_shadowing_and_unqualified_correlation_have_local_precedence(self):
        for command in (
            'SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.groups t WHERE t.id=1)',
            'SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=account_id)',
            'SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT g.id AS x FROM retail.groups g ORDER BY x LIMIT 1)',
        ):
            p = self.program(command)
            validate_predicate_columns(parse_one(p.commands[1].sql, read='postgres'), self.columns)

    def test_effects_scalar_row_union_and_nonfilter_subqueries_remain_closed(self):
        for command in (
            'SELECT (SELECT id FROM retail.groups) FROM retail.stock',
            'SELECT EXISTS(SELECT 1 FROM retail.groups) FROM retail.stock',
            'UPDATE retail.stock SET quantity=(SELECT id FROM retail.groups) WHERE id=1',
            'DELETE FROM retail.stock WHERE id=1 RETURNING EXISTS(SELECT 1 FROM retail.groups)',
            'SELECT id FROM retail.stock WHERE (id,quantity) IN(SELECT id,id FROM retail.groups)',
            'SELECT id FROM retail.stock WHERE id IN(SELECT * FROM retail.groups)',
            'SELECT id FROM retail.stock WHERE EXISTS(SELECT 1 FROM retail.groups UNION SELECT 2)',
            'SELECT id FROM retail.stock WHERE EXISTS(SELECT random() FROM retail.groups)',
            'SELECT id FROM retail.stock WHERE EXISTS(SELECT 1 FROM retail.groups FOR UPDATE)',
            'SELECT id FROM retail.stock WHERE EXISTS(SELECT 1 FROM (SELECT id FROM retail.groups) q)',
            'SELECT id FROM retail.stock WHERE id IN(SELECT id FROM retail.groups LIMIT 1)',
        ):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.program(command)

    def test_depth_and_count_limits(self):
        query = 'SELECT 1 FROM retail.groups'
        for _ in range(4):
            query = 'SELECT 1 FROM retail.groups WHERE EXISTS(' + query + ')'
        with self.assertRaises(ValueError):
            self.program('SELECT id FROM retail.stock WHERE EXISTS(' + query + ')')
        with self.assertRaises(ValueError):
            self.program('SELECT id FROM retail.stock WHERE ' + ' AND '.join(['EXISTS(SELECT 1 FROM retail.groups)'] * 17))
        joins = ' '.join(f'JOIN retail.groups g{i} ON g{i}.id=t.id' for i in range(8))
        with self.assertRaises(ValueError):
            self.program('SELECT t.id FROM retail.stock t ' + joins + ' WHERE EXISTS(SELECT 1 FROM retail.groups)')

    def test_star_width_and_cte_key_proof_ignore_nested_tables_and_aggregates(self):
        tree = parse_one('SELECT s.* FROM retail.stock s WHERE EXISTS(SELECT COUNT(*) FROM retail.groups)', read='postgres')
        self.assertEqual(select_projection_width(tree, self.columns), 5)
        tree = parse_one('WITH q AS(SELECT account_id FROM retail.stock s WHERE EXISTS(SELECT COUNT(*) FROM retail.groups)) SELECT * FROM q', read='postgres')
        self.assertEqual(cte_catalog(tree, self.columns, {('retail', 'stock'): (('id',),)})['', 'q'].keys, ())

    def test_positive_in_and_distinct_join_options_preserve_other_commands(self):
        for sql, rule in (
            ('SELECT t.id FROM retail.stock t WHERE t.account_id IN(SELECT a.account_id FROM retail.adjustments a) ORDER BY t.id', 'in-filter-to-exists'),
            ('SELECT DISTINCT t.id FROM retail.stock t JOIN retail.adjustments a ON a.account_id=t.account_id ORDER BY t.id', 'distinct-inner-join-to-exists'),
        ):
            text = 'BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;' + sql + ';COMMIT;'
            option = next(x for x in generate_transaction_select_rewrites(text, self.settings) if x.rule_id == rule)
            prepared = prepare_select_replacement(text, self.settings, 2, option.variant_sql)
            self.assertEqual(prepared.original.commands[0], prepared.variant.commands[0])
            self.assertIn('EXISTS', prepared.variant_sql)
            validate_predicate_columns(parse_one(prepared.variant_sql, read='postgres'), self.columns)

    def test_in_rewrite_does_not_change_negative_unknown_projection_or_shadowed_scopes(self):
        from pqo.sql_rewrite import generate_sql_rewrites
        for sql in (
            'SELECT t.id FROM retail.stock t WHERE t.id NOT IN(SELECT a.id FROM retail.groups a)',
            'SELECT t.id FROM retail.stock t WHERE (t.id IN(SELECT a.id FROM retail.groups a)) IS NULL',
            'SELECT t.id IN(SELECT a.id FROM retail.groups a) FROM retail.stock t',
            'SELECT t.id FROM retail.stock t WHERE t.id IN(SELECT t.id FROM retail.groups t)',
            'SELECT t.id FROM retail.stock t WHERE t.id IN(SELECT a.id FROM retail.groups a ORDER BY a.id LIMIT 1)',
        ):
            with self.subTest(sql=sql):
                self.assertFalse(any(x.rule_id == 'in-filter-to-exists' for x in generate_sql_rewrites(sql)))


@skipUnless(os.getenv('PQO_DISPOSABLE_REPLAY_TESTS') == '1', 'requires disposable PostgreSQL laboratory')
class PredicatePostgresTests(TestCase):
    def setUp(self):
        from test_transaction_joined_dml import JoinedDMLPostgresTests
        self.fixture = JoinedDMLPostgresTests(methodName='runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.settings, self.reference = self.fixture.settings, self.fixture.reference

    def test_predicates_observe_writes_savepoints_and_all_isolations(self):
        from pqo.transaction_replay import result_digest
        text = '''BEGIN ISOLATION LEVEL {isolation};
            UPDATE retail.stock t SET quantity=t.quantity+1 WHERE EXISTS(SELECT 1 FROM retail.adjustments a
                WHERE a.account_id=t.account_id AND a.batch_id=t.batch_id AND a.group_id IN(SELECT g.id FROM retail.groups g));
            SAVEPOINT retained;
            DELETE FROM retail.stock t WHERE NOT EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=t.id) RETURNING t.id;
            SELECT id FROM retail.stock ORDER BY id;
            ROLLBACK TO retained;RELEASE retained;
            SELECT t.id,t.quantity FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.adjustments a
                WHERE a.account_id=t.account_id AND NOT EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=a.group_id)) ORDER BY t.id;
            SELECT id,quantity FROM retail.stock ORDER BY id;{ending};'''
        for isolation, ending in (('READ UNCOMMITTED', 'COMMIT'), ('READ COMMITTED', 'COMMIT'),
                ('REPEATABLE READ', 'COMMIT'), ('SERIALIZABLE', 'ROLLBACK')):
            with self.subTest(isolation=isolation):
                r = self.fixture.replay(text.format(isolation=isolation, ending=ending))
                self.assertTrue(r.correctness_verified, r.reasons)
                for sample in r.samples:
                    self.assertEqual(sample.commands[2].result_sha256, result_digest([(3,), (4,)], False))
                    self.assertEqual(sample.commands[3].result_sha256, result_digest([(1,), (2,)], True))
                    self.assertEqual(sample.commands[6].result_sha256, result_digest([(1, 11)], True))
                    self.assertEqual(sample.commands[7].result_sha256, result_digest([(1,11),(2,21),(3,30),(4,40)], True))
                    if ending == 'ROLLBACK':
                        self.assertEqual(sample.initial_state, sample.final_state)

    def test_null_duplicates_positive_in_rewrite_and_wrong_not_in_are_detected(self):
        from pqo.transaction_select_rewrite import run_transaction_select_rewrite
        with self.fixture.connect() as c:
            c.execute('CREATE TABLE retail.null_values(id int PRIMARY KEY, account_id int)')
            c.execute('INSERT INTO retail.null_values VALUES(1,10),(2,10),(3,NULL)')
            c.execute('UPDATE retail.stock SET account_id=NULL WHERE id=4')
        text = '''BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;
            SELECT t.id FROM retail.stock t WHERE t.account_id IN(SELECT a.account_id FROM retail.null_values a) OR t.id=2 ORDER BY t.id;
            SELECT t.id FROM retail.stock t WHERE t.account_id NOT IN(SELECT a.account_id FROM retail.null_values a) ORDER BY t.id;COMMIT;'''
        option = next(x for x in generate_transaction_select_rewrites(text, self.settings) if x.number == 2 and x.rule_id == 'in-filter-to-exists')
        report = run_transaction_select_rewrite(text, self.settings, self.reference, 2, option.variant_sql, series=1,
            test_copy_confirmed=True, disposable_databases_confirmed=True).measurement
        self.assertTrue(report.correctness_verified and report.cleanup_verified, report.reasons)
        from pqo.transaction_replay import result_digest
        self.assertEqual(report.samples[0].commands[1].result_sha256, result_digest([(1,), (2,)], True))
        self.assertEqual(report.samples[0].commands[2].row_count, 0)
        bad = '''SELECT t.id FROM retail.stock t WHERE NOT EXISTS(SELECT a.account_id FROM retail.null_values a
            WHERE a.account_id=t.account_id) ORDER BY t.id'''
        report = run_transaction_select_rewrite(text, self.settings, self.reference, 3, bad, series=1,
            test_copy_confirmed=True, disposable_databases_confirmed=True).measurement
        self.assertFalse(report.correctness_verified or report.recommendation.recommended)
        self.assertTrue(report.cleanup_verified)

    def test_cte_joined_writes_and_insert_star_with_predicates(self):
        from pqo.transaction_replay import result_digest
        r = self.fixture.replay('''BEGIN;
            WITH delta AS(SELECT id,quantity FROM retail.adjustments a WHERE EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=a.group_id))
            UPDATE retail.stock t SET quantity=d.quantity FROM delta d WHERE t.id=d.id AND EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=t.id);
            INSERT INTO retail.stock(id,account_id,batch_id,quantity,label)
            SELECT a.* FROM retail.stock a WHERE a.id=4 AND NOT EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=a.id) AND a.id=9;
            DELETE FROM retail.stock t USING retail.events e WHERE t.id=e.stock_id AND EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=t.id) RETURNING t.id;
            SELECT id,quantity FROM retail.stock ORDER BY id;COMMIT;''')
        self.assertTrue(r.correctness_verified, r.reasons)
        self.assertEqual(r.samples[0].commands[3].result_sha256, result_digest([(1,5),(2,7),(3,30),(4,40)], True))

    def test_empty_sets_null_left_values_and_exists_limit_zero(self):
        from pqo.transaction_replay import result_digest
        with self.fixture.connect() as c:
            c.execute('UPDATE retail.stock SET account_id=NULL WHERE id=4')
        r = self.fixture.replay('''BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;
            SELECT t.id FROM retail.stock t WHERE t.account_id NOT IN(SELECT g.id FROM retail.groups g WHERE g.id<0) ORDER BY t.id;
            SELECT t.id FROM retail.stock t WHERE t.account_id IN(SELECT g.id FROM retail.groups g WHERE g.id<0) ORDER BY t.id;
            SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT COUNT(*) FROM retail.groups g WHERE g.id<0) ORDER BY t.id;
            SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT COUNT(*) FROM retail.groups g LIMIT 0) ORDER BY t.id;
            SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 WHERE account_id=t.account_id) ORDER BY t.id;
            COMMIT;''')
        self.assertTrue(r.correctness_verified, r.reasons)
        sample = r.samples[0]
        self.assertEqual(sample.commands[1].result_sha256, result_digest([(1,),(2,),(3,),(4,)], True))
        self.assertEqual(sample.commands[2].row_count, 0)
        self.assertEqual(sample.commands[3].row_count, 4)
        self.assertEqual(sample.commands[4].row_count, 0)
        self.assertEqual(sample.commands[5].result_sha256, result_digest([(1,),(2,),(3,)], True))

    def test_distinct_join_four_way_comparison(self):
        from pqo.index_actions import IndexAction
        from pqo.transaction_optimization import run_transaction_optimization
        from pqo.transaction_replay import result_digest
        text = '''BEGIN;UPDATE retail.stock SET quantity=quantity+1 WHERE id=1;
            SELECT DISTINCT t.id,t.quantity FROM retail.stock t JOIN retail.adjustments a ON a.account_id=t.account_id
            ORDER BY t.id;COMMIT;'''
        option = next(x for x in generate_transaction_select_rewrites(text, self.settings) if x.rule_id == 'distinct-inner-join-to-exists')
        before = self.fixture.fingerprint()
        r = run_transaction_optimization(text, self.settings, self.reference, 2, option.variant_sql,
            (IndexAction.create('retail','stock',('account_id',)),), series=2,
            test_copy_confirmed=True, disposable_databases_confirmed=True)
        self.assertTrue(r.comparable and r.cleanup_verified, r.reasons)
        self.assertEqual(len(r.observations), 14)
        for observation in r.observations:
            self.assertEqual(observation.sample.commands[1].result_sha256, result_digest([(1,11),(2,20)], True))
        self.assertEqual(before, self.fixture.fingerprint())

    def test_invalid_scope_declines_before_cloning_and_errors_and_cancel_cleanup(self):
        text = '''BEGIN;UPDATE retail.stock t SET quantity=quantity+1 WHERE id=1;
            SELECT t.id FROM retail.stock t WHERE EXISTS(SELECT 1 FROM retail.groups g WHERE g.id=t.id);COMMIT;'''
        r = self.fixture.replay(text.replace('g.id=t.id', 'g.quantity=t.id'))
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)
        from pqo.analysis_control import AnalysisControl
        control = AnalysisControl(progress=lambda message: control.cancel() if '2 SELECT;' in message else None)
        r = self.fixture.replay(text, control=control)
        self.assertEqual(r.status, 'cancelled', r.reasons)
        self.assertFalse(r.correctness_verified or r.recommendation.recommended)
        r = self.fixture.replay(text.replace('g.id=t.id', 'g.id=t.id/0'))
        self.assertEqual(r.status, 'error', r.reasons)
        self.assertTrue(any('22012' in reason for reason in r.reasons))
        self.assertFalse(r.recommendation.recommended)

    def test_inner_aggregate_is_not_a_uniqueness_proof_for_joined_update(self):
        r = self.fixture.replay('''BEGIN;
            WITH q AS(SELECT account_id,quantity FROM retail.adjustments a
                WHERE EXISTS(SELECT COUNT(*) FROM retail.groups g WHERE g.id=a.group_id))
            UPDATE retail.stock t SET quantity=q.quantity FROM q WHERE t.account_id=q.account_id;
            SELECT id FROM retail.stock;COMMIT;''')
        self.assertEqual(r.status, 'declined', r.reasons)
        self.assertFalse(r.created_databases)
        self.assertTrue(any('единственное совпадение' in reason for reason in r.reasons))

    def test_plans_and_inner_index_candidates_keep_correlated_scopes_and_do_not_write(self):
        from pqo.transaction_planning import collect_transaction_plans
        from pqo.transaction_indexes import collect_transaction_index_candidates
        text = '''BEGIN;UPDATE retail.stock t SET quantity=quantity+1 WHERE t.id=1
            AND EXISTS(SELECT 1 FROM retail.adjustments a WHERE a.account_id=t.account_id AND a.batch_id=1);
            SELECT t.id FROM retail.stock t WHERE t.account_id=20 AND EXISTS(
                SELECT 1 FROM retail.events e WHERE e.stock_id=t.id AND e.label='x');ROLLBACK;'''
        settings = replace(self.settings, dbname=self.reference)
        before = self.fixture.fingerprint()
        plans = collect_transaction_plans(text, settings)
        self.assertEqual([p.status for p in plans.statements], ['planned', 'planned'])
        candidates = collect_transaction_index_candidates(text, settings, plans=plans)
        self.assertTrue(any(c.action.table_name == 'events' and 'stock_id' in c.action.key_columns for c in candidates.candidates))
        self.assertTrue(any(c.action.table_name == 'stock' and 'account_id' in c.action.key_columns for c in candidates.candidates))
        self.assertEqual(before, self.fixture.fingerprint())
