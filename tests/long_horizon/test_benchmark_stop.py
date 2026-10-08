"""The upstream question-blocked fix never promotes stale or failing evidence."""
import copy
from types import SimpleNamespace as State
import unittest
from unittest.mock import patch

from jiuwenswarm.long_horizon.pact.long_horizon_benchmark import _acceptance_blocked_only_by_questions
from jiuwenswarm.long_horizon.pact.benchmark_native import NativeBenchmarkAdapter


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


class NativeSourceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_roles_receive_source_ownership_boundary(self):
        import asyncio
        import tempfile
        from pathlib import Path

        calls = []

        async def run(**kwargs):
            calls.append(kwargs)
            return State(status='completed', exit_code=0,
                         final_text='{"verdict":"continue","reason":"public evidence checked"}')

        with tempfile.TemporaryDirectory() as directory, patch(
            'jiuwenswarm.long_horizon.pact.benchmark_native.NativeExecution.from_worker',
            return_value=State(fingerprint='frozen'),
        ):
            for role in ('worker', 'reviewer'):
                adapter = NativeBenchmarkAdapter(
                    semantic_role=role, worker=State(run=run, source_commit='fixture', binary=Path('fixture')), outer_loop=None,
                    invocation_id='control', output_dir=Path(directory),
                    abort_event=asyncio.Event(), expected_fingerprint='frozen',
                )
                result = await adapter._turn(State(
                    prompt='Original public task', review_mode='acceptance', resume_session_id=None,
                    timeout_seconds=30, deadline=None, workdir=Path(directory), artifact_dir=None,
                ))
                self.assertEqual(result.status, 'completed')
                self.assertTrue(calls[-1]['prompt'].startswith('Original public task'))
                self.assertIn('Never write, copy, sync, install, or rebuild candidate changes into the original source checkout', calls[-1]['prompt'])
                self.assertIn('candidate-local isolated directory', calls[-1]['prompt'])
                self.assertIn('do not copy .git files or directories between checkouts', calls[-1]['prompt'])
                self.assertEqual(calls[-1]['read_only'], role == 'reviewer')
