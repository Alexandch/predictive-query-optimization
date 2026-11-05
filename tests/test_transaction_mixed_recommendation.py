from dataclasses import replace, asdict
import json
from unittest import TestCase
from unittest.mock import patch

from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_mixed_recommendation import MixedReplayPolicy, assess_mixed_replay, replay_request_digest
from pqo.transaction_replay import CommandReplay, ReplaySample, StateDigest, TransactionReplay, run_transaction_replay


class MixedRecommendationTests(TestCase):
    def fixture(self, series=2):
        settings = replace(DatabaseSettings(),dbname='source',allowed_schemas=frozenset({'university'}))
        actions = (IndexAction.create('university','items',('value',)),)
        policy = MixedReplayPolicy()
        digest = replay_request_digest('SQL fixture',settings,'lab',actions,series,policy)
        samples = []
        for number in range(1,series+1):
            for phase in ('A1','B','A2'):
                command_times = (30.,10.) if phase == 'B' else (90.,10.)
                commands = tuple(CommandReplay(index,kind,ms,1,(('id',23),),'a'*64)
                    for index,(kind,ms) in enumerate(zip(('select','update'),command_times),1))
                samples.append(ReplaySample(number,phase,f'pqo_replay_{number}_{phase}',commands,
                    50. if phase=='B' else 110., (StateDigest('university.items',1,'b'*64),),
                    (StateDigest('university.items',1,'c'*64),),1024 if phase=='B' else 0,
                    20. if phase=='B' else 0.,2048 if phase=='B' else 0))
        report = TransactionReplay('completed','lab',actions,tuple(samples),series,True,True,True,True,True,
            ('pqo_replay_seed',*(item.database for item in samples)),(),(),None,digest,policy)
        return report

    def assess(self,report,**kwargs):
        return assess_mixed_replay(report,kwargs.get('policy',report.policy),expected_request_digest=kwargs.get('digest',report.request_digest))

    def mutate(self,report,index,**kwargs):
        samples=list(report.samples); samples[index]=replace(samples[index],**kwargs)
        return replace(report,samples=tuple(samples))

    def test_stable_total_gain_recommends_fixed_set_and_separates_costs(self):
        result=self.assess(self.fixture())
        self.assertTrue(result.recommended)
        self.assertEqual(result.status,'recommended')
        self.assertEqual(result.maximum_index_bytes,2048)
        self.assertEqual(result.median_build_ms,20.)
        self.assertEqual(result.summary[0].gain_ms,60.)
        self.assertEqual(result.summary[1].gain_ms,60.)
        self.assertEqual(result.summary[2].gain_ms,0.)
        self.assertTrue(all(item.gain_passed for item in result.series))

    def test_one_series_is_diagnostic_not_recommendation(self):
        result=self.assess(self.fixture(1))
        self.assertEqual(result.status,'inconclusive')
        self.assertEqual(len(result.series),1)

    def test_read_speedup_cannot_hide_write_regression(self):
        report=self.fixture()
        samples=[]
        for item in report.samples:
            if item.phase=='B':
                commands=(item.commands[0],replace(item.commands[1],client_ms=15.))
                item=replace(item,commands=commands,transaction_client_ms=55.)
            samples.append(item)
        result=self.assess(replace(report,samples=tuple(samples)))
        self.assertTrue(all(item.gain_passed for item in result.series))
        self.assertEqual(result.status,'not_recommended')
        self.assertTrue(any('UPDATE' in reason for reason in result.reasons))

    def test_one_failed_series_cannot_be_averaged_away(self):
        report=self.mutate(self.fixture(),4,transaction_client_ms=108.)
        result=self.assess(report)
        self.assertFalse(result.recommended)
        self.assertFalse(result.series[1].gain_passed)

    def test_individual_write_regression_is_not_hidden_by_another_faster_write(self):
        report=self.fixture()
        samples=[]
        for sample in report.samples:
            first=replace(sample.commands[1],client_ms=10. if sample.phase=='B' else 5.)
            second=replace(first,number=3,client_ms=0. if sample.phase=='B' else 5.)
            samples.append(replace(sample,commands=(sample.commands[0],first,second)))
        result=self.assess(replace(report,samples=tuple(samples)))
        self.assertEqual(result.summary[2].gain_ms,0.)
        self.assertEqual(result.status,'not_recommended')
        self.assertTrue(any('Команда 2 UPDATE' in reason for reason in result.reasons))

    def test_first_release_large_baseline_drift_is_not_mistaken_for_recommendation(self):
        report=self.fixture()
        # Match the diagnostic 0.8.35 pattern without pretending it is new evidence.
        samples=[]
        for sample in report.samples:
            times={'A1':165.253,'B':44.374,'A2':73.140}
            commands=tuple(replace(item,client_ms=1.) for item in sample.commands)
            samples.append(replace(sample,commands=commands,transaction_client_ms=times[sample.phase]))
        self.assertEqual(self.assess(replace(report,samples=tuple(samples))).status,'inconclusive')

    def test_noop_or_slow_total_is_not_recommended_even_with_fast_select(self):
        for total in (110.,120.):
            report=self.fixture()
            report=replace(report,samples=tuple(replace(s,transaction_client_ms=total) if s.phase=='B' else s for s in report.samples))
            self.assertEqual(self.assess(report).status,'not_recommended')

    def test_drift_within_series_blocks_apparent_gain(self):
        report=self.mutate(self.fixture(),0,transaction_client_ms=170.)
        result=self.assess(report)
        self.assertEqual(result.status,'inconclusive')
        self.assertTrue(any('A1/A2' in reason for reason in result.reasons))

    def test_command_drift_cannot_hide_in_stable_total(self):
        report=self.fixture()
        commands=(replace(report.samples[0].commands[0],client_ms=85.),replace(report.samples[0].commands[1],client_ms=15.))
        result=self.assess(self.mutate(report,0,commands=commands))
        self.assertEqual(result.status,'inconclusive')
        self.assertTrue(any('UPDATE' in reason for reason in result.reasons))

    def test_baseline_and_indexed_instability_across_series_both_block(self):
        for phases in (('A1','A2'),('B',)):
            report=self.fixture()
            report=replace(report,samples=tuple(replace(s,transaction_client_ms=s.transaction_client_ms+50.)
                if s.series==2 and s.phase in phases else s for s in report.samples))
            self.assertEqual(self.assess(report).status,'inconclusive')

    def test_size_budget_includes_growth_after_commit(self):
        report=self.fixture()
        report=replace(report,policy=replace(report.policy,maximum_index_bytes=1500))
        self.assertEqual(self.assess(report).status,'not_recommended')
        self.assertTrue(any('бюджет' in reason for reason in self.assess(report).reasons))

    def test_both_absolute_and_relative_regression_thresholds_required(self):
        for baseline,slower,expected in ((10.,11.,True),(10.,12.,True),(10.,12.1,False),
                (100.,109.,True),(100.,110.,True),(100.,110.1,False)):
            report=self.fixture()
            report=replace(report,samples=tuple(replace(s,
                commands=(s.commands[0],replace(s.commands[1],client_ms=slower if s.phase=='B' else baseline)),
                transaction_client_ms=160. if s.phase=='B' else 210.) for s in report.samples))
            self.assertEqual(self.assess(report).recommended,expected)

    def test_corrupt_incomplete_error_and_cleanup_evidence_never_accepts(self):
        report=self.fixture()
        for broken in (replace(report,status='cancelled'),replace(report,status='error'),replace(report,cleanup_verified=False),
                replace(report,leftover_databases=('pqo_replay_owned',)),replace(report,results_equal=False),
                replace(report,samples=report.samples[:-1]),replace(report,created_databases=()),replace(report,requested_series=True),
                self.mutate(report,1,final_state=(StateDigest('university.items',1,'d'*64),)),
                self.mutate(report,1,database=report.samples[0].database),self.mutate(report,1,initial_state=()),
                self.mutate(report,1,index_final_size_bytes=0)):
            with self.subTest(broken=broken.status):
                assessment=self.assess(broken)
                self.assertEqual(assessment.status,'inconclusive')
                self.assertIsNone(assessment.maximum_index_bytes)
                self.assertIsNone(assessment.median_build_ms)

    def test_nonfinite_negative_bool_and_impossible_timing_rejects(self):
        report=self.fixture()
        for value in (float('nan'),float('inf'),-1.,True):
            self.assertFalse(self.assess(self.mutate(report,1,transaction_client_ms=value)).recommended)
            self.assertFalse(self.assess(self.mutate(report,1,index_build_ms=value)).recommended)
        self.assertFalse(self.assess(self.mutate(report,1,transaction_client_ms=1.)).recommended)

    def test_provenance_policy_and_context_changes_require_fresh_replay(self):
        report=self.fixture()
        self.assertFalse(self.assess(report,digest='e'*64).recommended)
        self.assertFalse(self.assess(replace(report,request_digest='')).recommended)
        self.assertFalse(self.assess(report,policy=replace(report.policy,minimum_gain_ms=0.)).recommended)
        settings=replace(DatabaseSettings(),dbname='source',allowed_schemas=frozenset({'university'}))
        base=replay_request_digest('text',settings,'lab',report.actions,2,report.policy)
        for text,setting,reference,actions,series,policy in (
                ('text2',settings,'lab',report.actions,2,report.policy),
                ('text',replace(settings,host='elsewhere'),'lab',report.actions,2,report.policy),
                ('text',settings,'otherlab',report.actions,2,report.policy),
                ('text',settings,'lab',(IndexAction.create('university','items',('id',)),),2,report.policy),
                ('text',settings,'lab',report.actions,3,report.policy),
                ('text',settings,'lab',report.actions,2,replace(report.policy,minimum_gain_ms=6.))):
            self.assertNotEqual(base,replay_request_digest(text,setting,reference,actions,series,policy))
        self.assertEqual(base,replay_request_digest('text',replace(settings,password='SECRET'),'lab',report.actions,2,report.policy))
        self.assertNotIn('SECRET',json.dumps(asdict(self.assess(report))))

    def test_invalid_policy_declines_before_any_database_connection(self):
        for kwargs in ({'minimum_gain_ms':float('nan')},{'minimum_gain_ratio':2},{'drift_ms':True},
                {'maximum_index_bytes':0},{'maximum_index_bytes':65*1024**2}):
            with self.assertRaises(ValueError): MixedReplayPolicy(**kwargs)
        with patch('pqo.transaction_replay.psycopg.connect') as connect,self.assertRaises(ValueError):
            run_transaction_replay('BEGIN; SELECT id FROM university.items; UPDATE university.items SET value=1 WHERE id=1; COMMIT;',
                replace(DatabaseSettings(),dbname='source',allowed_schemas=frozenset({'university'})),'lab',self.fixture().actions,
                test_copy_confirmed=True,disposable_databases_confirmed=True,policy='invalid')
        connect.assert_not_called()
