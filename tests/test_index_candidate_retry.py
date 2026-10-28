from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import Mock

from pqo.analysis_control import AnalysisControl, AnalysisCancelled
from pqo.index_actions import IndexAction
from pqo.sequential_environment import SequentialIndexEpisode, SequentialIndexState, SequentialIndexTransition
from pqo.sequential_recommendation import _try_ranked_candidates, recommend_sequential_indexes


class IndexCandidateRetryTests(unittest.TestCase):
    def setUp(self):
        self.actions = [IndexAction.stop(), *[IndexAction.create("retail", "orders", (key,)) for key in ("a", "b", "c")]]
        self.initial = SequentialIndexState(0, 2, 100., 100., 10000, 0, (), (), 0., False)
        self.connection = Mock()
        self.episode = SequentialIndexEpisode(environment=Mock(), connection=self.connection,
            query="SELECT 1", candidates=tuple(self.actions), state=self.initial)
        self.checks = []

    def transition(self, action, *, used=True, reward=.5, measured=50.):
        previous = self.episode.state
        state = replace(previous, step=previous.step+1, current_time_ms=measured if used else previous.current_time_ms,
            used_budget_bytes=previous.used_budget_bytes+100, selected_actions=(*previous.selected_actions, action),
            selected_index_bytes=(*previous.selected_index_bytes, 100))
        self.episode.state = state
        self.episode._created_index_names.append(action.key_columns[0])
        return SequentialIndexTransition(previous, action, state, reward, .5, 100, 10., used, used, True, None, measured)

    def run_candidates(self, limit=3, values=None, accepted=0):
        return _try_ranked_candidates(self.episode, self.actions, values or [10., .9, .8, .7],
            threshold=.5, accepted_count=accepted, max_candidates=limit,
            minimum_absolute_ms=5., minimum_ratio=.05, control=AnalysisControl(), checks=self.checks)

    def test_second_ranked_candidate_succeeds_after_rollback_of_first(self):
        def step(action):
            if action == self.actions[1]:
                return self.transition(action, used=False, reward=-.1, measured=130.)
            self.assertEqual(self.episode.state, self.initial)
            self.assertEqual(self.episode._created_index_names, [])
            return self.transition(action)
        self.episode.step = Mock(side_effect=step)
        index, transition, reason = self.run_candidates()
        self.assertEqual(index, 2)
        self.assertIsNone(reason)
        self.assertEqual(self.episode.state.selected_actions, (self.actions[2],))
        self.assertEqual(self.episode.state.used_budget_bytes, 100)
        self.assertEqual(self.episode._created_index_names, ["b"])
        self.assertEqual(self.checks[0].after_time_ms, 130.)
        self.assertEqual(self.checks[0].rejection_reason, "index_not_used")
        self.assertTrue(self.checks[1].accepted)

    def test_limit_stops_search_and_all_rejections_restore_original_state(self):
        self.episode.step = Mock(side_effect=lambda action: self.transition(action, reward=-.1))
        index, _, reason = self.run_candidates(limit=2)
        self.assertIsNone(index)
        self.assertEqual(reason, "non_positive_reward")
        self.assertEqual(self.episode.step.call_count, 2)
        self.assertEqual(self.episode.state, self.initial)
        self.assertEqual(self.episode._created_index_names, [])

    def test_timeout_is_recovered_before_trying_next_candidate(self):
        error = RuntimeError("statement timeout")
        error.sqlstate = "57014"
        def step(action):
            if action == self.actions[1]:
                self.transition(action)
                raise error
            self.assertEqual(self.episode.state, self.initial)
            return self.transition(action)
        self.episode.step = Mock(side_effect=step)
        self.assertEqual(self.run_candidates()[0], 2)
        self.assertEqual(self.checks[0].rejection_reason, "statement_timeout")
        self.assertIsNone(self.checks[0].after_time_ms)

    def test_cancellation_and_unexpected_error_do_not_trigger_more_attempts(self):
        for error in (AnalysisCancelled("cancelled"), ValueError("bad action")):
            def step(action):
                self.transition(action)
                raise error
            self.episode.step = Mock(side_effect=step)
            with self.assertRaises(type(error)):
                self.run_candidates()
            self.assertEqual(self.episode.step.call_count, 1)
            self.assertEqual(self.episode.state, self.initial)
            self.assertEqual(self.episode._created_index_names, [])

    def test_rejected_trial_preserves_previously_accepted_index_and_budget(self):
        self.transition(self.actions[1])
        accepted = self.episode.state
        with self.episode.candidate_trial():
            self.transition(self.actions[2])
        self.assertEqual(self.episode.state, accepted)
        self.assertEqual(self.episode._created_index_names, ["a"])

    def test_exception_after_accept_still_rolls_trial_back(self):
        with self.assertRaises(ValueError):
            with self.episode.candidate_trial() as trial:
                self.transition(self.actions[1])
                trial.accept()
                raise ValueError("failed before leaving trial")
        self.assertEqual(self.episode.state, self.initial)

    def test_nested_trials_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Nested"):
            with self.episode.candidate_trial():
                with self.episode.candidate_trial():
                    pass
        self.assertFalse(self.episode._candidate_trial_active)

    def test_model_threshold_is_retained_after_a_successful_step(self):
        self.episode.step = Mock()
        index, _, reason = self.run_candidates(values=[10., .2, .1, .05], accepted=1)
        self.assertIsNone(index)
        self.assertEqual(reason, "model_stop")
        self.episode.step.assert_not_called()

    def test_rejects_invalid_attempt_limits_before_loading_model(self):
        for limit in (0, 9, 1.5, "3", True):
            with self.assertRaisesRegex(ValueError, "max_candidates_per_step"):
                recommend_sequential_indexes("SELECT 1", Path("missing.pt"), max_candidates_per_step=limit)
