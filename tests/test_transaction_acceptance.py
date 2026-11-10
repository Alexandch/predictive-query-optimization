from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from pqo.config import DatabaseSettings
from pqo.transaction_acceptance import (
    AcceptanceFixture, DOMAINS, acceptance_checks, run_acceptance,
)
from pqo.transaction_replay_guard import prepare_replay
from pqo.transaction_replay import TransactionReplay
from pqo.transaction_mixed_recommendation import MixedReplayPolicy, MixedRecommendation
from pqo.transaction_select_rewrite import generate_transaction_select_rewrites, prepare_select_replacement


class TransactionAcceptanceTests(TestCase):
    def test_all_domain_programs_fit_closed_contract_and_have_real_write_context(self):
        for domain in DOMAINS:
            settings = replace(DatabaseSettings(), allowed_schemas=frozenset({domain.schema}))
            with self.subTest(domain=domain.schema):
                program = prepare_replay(domain.index_sql(), settings)
                self.assertEqual([c.kind for c in program.commands], ['update'] + ['select']*8)
                self.assertEqual(program.tables, ((domain.schema, domain.table),))
                prepare_replay(domain.negative_sql(), settings)
                option = next(o for o in generate_transaction_select_rewrites(domain.rewrite_sql(), settings)
                              if o.rule_id == 'subquery-order-without-limit')
                prepared = prepare_select_replacement(domain.rewrite_sql(), settings, 2, option.variant_sql)
                self.assertEqual(prepared.original.commands[0], prepared.variant.commands[0])
                self.assertEqual(prepared.original.commands[2], prepared.variant.commands[2])
                wrong = option.variant_sql.replace('GROUP BY', "WHERE q.status <> 'reviewed' GROUP BY", 1)
                # It is syntactically supported: only replay can catch the data difference.
                prepare_select_replacement(domain.rewrite_sql(), settings, 2, wrong)

    def test_explicit_confirmation_before_output_creation_or_connection(self):
        with TemporaryDirectory() as directory, patch('pqo.transaction_acceptance.psycopg.connect') as connect:
            target = Path(directory) / 'not-created'
            with self.assertRaises(ValueError):
                run_acceptance(target, DatabaseSettings())
            self.assertFalse(target.exists())
            connect.assert_not_called()

    def test_existing_output_is_not_overwritten(self):
        with TemporaryDirectory() as directory, patch('pqo.transaction_acceptance.psycopg.connect') as connect:
            with self.assertRaises(FileExistsError):
                run_acceptance(Path(directory), DatabaseSettings(), laboratory_confirmed=True)
            connect.assert_not_called()

    def test_cleanup_never_drops_an_unknown_or_reused_identity(self):
        with TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(DatabaseSettings(), DOMAINS[0], Path(directory))
            fixture.create_attempted = True
            connection = MagicMock()
            connection.__enter__.return_value = connection
            for recorded, actual in ((None, (11, 12)), ((11, 12), (13, 12)), ((11, 12), (11, 13))):
                fixture.identity = recorded
                connection.execute.return_value.fetchone.return_value = actual
                connection.reset_mock()
                with patch('pqo.transaction_acceptance._connect', return_value=connection):
                    self.assertFalse(fixture.remove())
                self.assertEqual(connection.execute.call_count, 1)  # read-only identity check, no DROP

    def test_cleanup_independently_checks_absence_and_manifest_excludes_password(self):
        with TemporaryDirectory() as directory:
            fixture = AcceptanceFixture(replace(DatabaseSettings(), password='SECRET_NEVER_RECORD'), DOMAINS[0], Path(directory))
            fixture.create_attempted = True
            fixture.identity = (11, 12)
            connection = MagicMock()
            connection.__enter__.return_value = connection
            connection.execute.return_value.fetchone.side_effect = [(11, 12), (True,)]
            with patch('pqo.transaction_acceptance._connect', return_value=connection) as connect:
                self.assertTrue(fixture.remove())
            self.assertEqual(connect.call_count, 2)
            self.assertNotIn('SECRET_NEVER_RECORD', fixture.manifest.read_text(encoding='utf-8'))
            self.assertIn('"removed": true', fixture.manifest.read_text(encoding='utf-8'))

    @staticmethod
    def report(**changes):
        values = dict(status='completed', cleanup_verified=True, leftover_databases=(),
                      recommendation=SimpleNamespace(recommended=False), initial_states_equal=True,
                      results_equal=True, final_states_equal=True, correctness_verified=True,
                      samples=tuple(range(6)), created_databases=tuple(range(7)))
        values.update(changes)
        return SimpleNamespace(**values)

    def test_semantic_and_timing_acceptance_are_separate(self):
        self.assertTrue(all(acceptance_checks(self.report(), 'equivalent').values()))
        positive = self.report(recommendation=SimpleNamespace(recommended=True))
        self.assertTrue(all(acceptance_checks(positive, 'equivalent').values()))
        self.assertFalse(all(acceptance_checks(positive, 'no_benefit').values()))
        self.assertFalse(all(acceptance_checks(self.report(samples=(1,)), 'equivalent').values()))
        wrong = self.report(results_equal=False, correctness_verified=False)
        self.assertTrue(all(acceptance_checks(wrong, 'different_output').values()))
        self.assertFalse(all(acceptance_checks(self.report(), 'different_output').values()))

    def test_cancel_error_decline_must_not_recommend_and_always_need_cleanup(self):
        for status in ('cancelled', 'error', 'declined'):
            item = self.report(status=status, correctness_verified=False, created_databases=())
            self.assertTrue(all(acceptance_checks(item, status).values()))
            item.cleanup_verified = False
            self.assertFalse(all(acceptance_checks(item, status).values()))
        with self.assertRaises(ValueError):
            acceptance_checks(self.report(), 'unknown')

    def test_unverified_clone_removal_stops_matrix_and_keeps_failure_report(self):
        policy = MixedReplayPolicy()
        decision = MixedRecommendation('inconclusive', policy, '', (), (), None, None, ('incomplete',))
        measured = TransactionReplay('error', 'fixture', (), (), 2, False, False, False, False,
            False, ('pqo_replay_owned',), ('pqo_replay_owned',), ('cleanup unverified',), recommendation=decision)
        fixture = MagicMock()
        fixture.name = 'pqo_mvpfixture_owned'
        fixture.domain = DOMAINS[0]
        fixture.fingerprint.return_value = {'state': 'immutable'}
        fixture.remove.return_value = True
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = ('17',)
        connection.execute.return_value.fetchall.return_value = [('pqo_replay_owned',)]
        with TemporaryDirectory() as directory, patch('pqo.transaction_acceptance._connect', return_value=connection), \
                patch('pqo.transaction_acceptance.AcceptanceFixture', return_value=fixture), \
                patch('pqo.transaction_acceptance.run_transaction_replay', return_value=measured) as run:
            target = Path(directory) / 'report'
            with self.assertRaises(RuntimeError):
                run_acceptance(target, DatabaseSettings(), laboratory_confirmed=True, progress=lambda message: None)
            run.assert_called_once()
            fixture.remove.assert_called_once()
            import json
            result = json.loads((target / 'report.json').read_text(encoding='utf-8'))
            self.assertEqual(result['status'], 'error')
            self.assertEqual(result['cases'][0]['status'], 'failed')
            self.assertFalse(result['cases'][0]['checks']['independent_absence'])
