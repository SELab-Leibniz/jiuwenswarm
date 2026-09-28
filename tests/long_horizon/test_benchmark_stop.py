"""The upstream question-blocked fix never promotes stale or failing evidence."""
import copy
from types import SimpleNamespace as State
import unittest

from jiuwenswarm.long_horizon.pact.long_horizon_benchmark import _acceptance_blocked_only_by_questions


class BenchmarkStopTests(unittest.TestCase):
    def test_exact_passed_gate_with_question_stops_without_accepting(self):
        candidate = State(tree='exact')
        state = State(status='blocked',unresolved_assumptions=['question'],acceptance=None,delivery=None,
                      candidate=candidate,review=State(candidate=candidate,complete=False,
                          reasons=['gate_complete','unresolved_user_questions']),
                      verification=State(status='passed'),policy=State(allow_unverified=False))
        self.assertTrue(_acceptance_blocked_only_by_questions(state))
        self.assertIsNone(state.acceptance);self.assertIsNone(state.delivery)
        for changes in ({'status':'active'},{'unresolved_assumptions':[]},{'review':None},
                        {'review':State(candidate=State(tree='stale'),complete=False,reasons=state.review.reasons)},
                        {'review':State(candidate=candidate,complete=False,reasons=['feedback_recorded','unresolved_user_questions'])},
                        {'review':State(candidate=candidate,complete=True,reasons=state.review.reasons)},
                        {'verification':State(status='failed')},{'verification':None}):
            changed = copy.copy(state)
            for key,value in changes.items():setattr(changed,key,value)
            with self.subTest(changes=changes):
                self.assertFalse(_acceptance_blocked_only_by_questions(changed))
