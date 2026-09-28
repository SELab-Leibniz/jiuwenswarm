import copy
from pathlib import Path
from types import SimpleNamespace as State
import unittest

from jiuwenswarm.long_horizon.pact.long_horizon_progress import ReviewProgressGuard


def state(tree="a", feedback="Need parser integration"):
    candidate = State(tree=tree)
    return State(candidate=candidate, review=State(candidate=candidate, artifact=Path("review"),
                 reasons=["feedback_recorded"], feedback=feedback), acceptance=None, delivery=None,
                 verification=State(status="failed", candidate=candidate, check_results=[{"name":"public", "version":1,
                                    "status":"failed", "exit_code":1}]), contract_revision=1,
                 contract={"goal":"fixture"}, constraints=[], checks=[],
                 policy=State(verify_command=None),
                 unresolved_assumptions=[], user_decisions=[])


class ProgressGuardTests(unittest.TestCase):
    def test_unverified_registered_checks_do_not_consume_a_round(self):
        guard=ReviewProgressGuard();current=state()
        current.verification=None;current.checks=[{'name':'public'}]
        for count in range(1,4):self.assertFalse(guard.observe(current,count))
        self.assertEqual(guard.evidence()['reviewed_inputs'],[])
        verified=state();verified.checks=current.checks
        self.assertFalse(guard.observe(verified,3))
        self.assertFalse(guard.observe(verified,4))
        self.assertTrue(guard.observe(verified,5))

    def test_only_distinct_completed_workers_count_even_when_review_is_deduplicated(self):
        guard = ReviewProgressGuard(); current = state()
        self.assertFalse(guard.observe(current, 0)); self.assertFalse(guard.observe(current, 1))
        for _ in range(20): self.assertFalse(guard.observe(current, 1))
        self.assertFalse(guard.observe(current, 2)); self.assertTrue(guard.observe(current, 3))
        self.assertFalse(guard.evidence()["acceptance_waived"])
        self.assertEqual(len(guard.evidence()["reviewed_inputs"]), 3)

    def test_alternating_candidates_and_substantive_progress_do_not_stop(self):
        guard = ReviewProgressGuard()
        for count in range(1,5): self.assertFalse(guard.observe(state(str(count % 2)), count))
        self.assertFalse(guard.observe(state("1"), 5))
        guard = ReviewProgressGuard()
        for count in range(1,10): self.assertFalse(guard.observe(state(feedback=str(count)), count))

    def test_changed_checks_contract_and_decisions_are_progress(self):
        guard = ReviewProgressGuard(); current = state()
        self.assertFalse(guard.observe(current, 1))
        for count, key, value in [(2,"checks",[{"name":"new"}]),(3,"contract_revision",2),
                                 (4,"user_decisions",["decided"]),(5,"unresolved_assumptions",["new"])]:
            changed=copy.copy(current);setattr(changed,key,value)
            self.assertFalse(guard.observe(changed,count))

    def test_stale_missing_accepted_and_delivered_reviews_never_stop(self):
        for key,value in [("review",None),("candidate",State(tree="changed")),
                          ("acceptance",{}),("delivery",{})]:
            guard=ReviewProgressGuard();changed=copy.copy(state());setattr(changed,key,value)
            for count in range(1,5):self.assertFalse(guard.observe(changed,count))

    def test_waits_for_exact_candidate_verification_and_does_not_consume_round(self):
        guard=ReviewProgressGuard();current=state()
        self.assertFalse(guard.observe(current,1))
        current.verification.candidate=State(tree='stale')
        self.assertFalse(guard.observe(current,2))
        current.verification.candidate=current.candidate
        self.assertFalse(guard.observe(current,2))
        self.assertTrue(guard.observe(current,3))

    def test_review_paths_and_verifier_timestamps_are_not_progress(self):
        guard=ReviewProgressGuard()
        for count in range(1,4):
            current=state();current.review.artifact=Path(f"review-{count}")
            current.verification.check_results[0]["elapsed_seconds"]=count
            self.assertEqual(guard.observe(current,count),count==3)
