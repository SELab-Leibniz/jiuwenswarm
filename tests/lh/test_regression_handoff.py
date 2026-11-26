# Copyright (c) 2026 Chrys. All rights reserved.

from __future__ import annotations

import hashlib
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from jiuwenswarm.pact_core.runtime import AcceptancePolicy
from jiuwenswarm.pact_core.runtime.types import CandidateRef, VerificationEvidence
from jiuwenswarm.pact_core.schemas import AdapterProbe, TurnResult

from jiuwenswarm.lh.harbor_unified.candidate_artifacts import make_evidence_runtime
from jiuwenswarm.lh.public_reference import SCHEMA as REFERENCE_SCHEMA
from jiuwenswarm.lh.reference_client_spec import PUBLIC_REFERENCE_COMMAND
from jiuwenswarm.lh.regression_handoff import (
    BEHAVIOR_SCHEMA,
    MAX_NATIVE_REGRESSIONS,
    merge_regressions,
    regression_case_key,
    worker_regression_context,
)
from jiuwenswarm.lh.suite import original_task_pair

FIXTURE = "a" * 64
METHOD = "b" * 64
CASE = "tests/test_public.py::test_case[param-1]"


def snapshot(work="old", *, checks=(), rows=(), candidate_letter="a"):
    candidate = CandidateRef(work, Path("candidate"), candidate_letter * 40, "c" * 40)
    return SimpleNamespace(
        work_id=work,
        revision=7,
        candidate=candidate,
        policy=AcceptancePolicy(verify_command="true"),
        checks=checks,
        verification=VerificationEvidence(
            Path("verification"), candidate, "failed" if any(row["exit_code"] for row in rows) else "passed",
            "true", 0, "", "", rows,
        ),
    )


def row_for(definition, *, kind, fields, method_failure=None):
    exit_code = {"match": 0, "mismatch": 1, "unavailable": 2}[kind]
    return {
        "name": definition["name"],
        "source": "derived",
        "fingerprint": definition["fingerprint"],
        "command": definition["command"],
        "status": "passed" if kind == "match" else "failed",
        "exit_code": exit_code,
        "stdout_tail": json.dumps(fields),
        "stderr_tail": "",
        **({"method_prerequisite": method_failure} if method_failure else {}),
    }


def oracle(*, work="old", kind="mismatch", fixture=FIXTURE, method=METHOD, name="state-compare", literal=True):
    command = shlex.join([
        *shlex.split(PUBLIC_REFERENCE_COMMAND), "compare", "public.json",
        "--reference", '["reference"]', "--candidate", '["candidate"]',
    ])
    definition = {
        "name": name, "command": command if literal else "./candidate",
        "fingerprint": method, "files": {}, "disposition": "pending",
    }
    receipt = {
        "schema": REFERENCE_SCHEMA,
        "status": "reference_unavailable" if kind == "unavailable" else kind,
        "request_sha256": fixture,
        "reference_argv_sha256": "d" * 64,
        "candidate_argv_sha256": "e" * 64,
    }
    if kind == "mismatch":
        receipt.update(step=5, field="/stock")
    if kind == "match":
        receipt.update(checkpoints=3)
    if kind == "unavailable":
        receipt.update(reason="access_denied")
    return snapshot(work, checks=(definition,), rows=(row_for(definition, kind=kind, fields=receipt),))


def behavior(
    *, work="new", kind="match", fixture=FIXTURE, method=METHOD, case=CASE,
    disposition="admitted", bound=True, checked_fixture=True, method_failure=None,
):
    key = regression_case_key(case, "5")
    definition = {
        "name": f"regression-{key}", "command": "verified-public-case", "fingerprint": method,
        "disposition": disposition, "files": {"public.json": fixture} if checked_fixture else {},
        "method_review": {
            "review_ref": "review-result",
            "reason": f"public-regression-v1 {key} {fixture} Same public scenario." if bound else "Looks fine.",
        },
    }
    receipt = {
        "schema": BEHAVIOR_SCHEMA,
        "key": key,
        "fixture_sha256": fixture,
        "outcome": kind,
        "case": case,
        "checkpoint": "5",
    }
    row = row_for(definition, kind=kind, fields=receipt, method_failure=method_failure)
    row["stdout_tail"] = "case output\n" + row["stdout_tail"] + "\n"
    return snapshot(work, checks=(definition,), rows=(row,))


def hint(case=CASE, checkpoint="5", source="jiuwen:request-1:25", **extra):
    return {"case": case, "checkpoint": checkpoint, "source": source, **extra}


def test_native_identity_survives_restart_and_discards_log_text():
    original = snapshot()
    earlier = merge_regressions(state=original, native_hints=[hint(command="UNTRUSTED_COMMAND")])
    (item,) = earlier["items"]
    assert item["key"] == regression_case_key(CASE, "5")
    assert item["origin"]["work_id"] == original.work_id
    assert item["origin"]["candidate_tree"] == original.candidate.tree
    assert item["origin"]["candidate_checkpoint"] == original.candidate.checkpoint
    persisted = json.loads(json.dumps(earlier))
    replayed = merge_regressions(
        persisted, state=snapshot("different-work"),
        native_hints=[hint(checkpoint="005", source="jiuwen:request-2:26", detail="different stderr")],
    )
    assert replayed == earlier == persisted
    assert CASE in worker_regression_context(replayed)
    assert "UNTRUSTED_COMMAND" not in json.dumps(replayed) + worker_regression_context(replayed)


def test_native_hints_require_a_bound_candidate_and_safe_labels():
    assert not merge_regressions(native_hints=[hint()])["items"]
    state = snapshot()
    assert not merge_regressions(state=state, native_hints=[hint("case\nuntrusted"), hint(checkpoint="5; shell")])["items"]
    (entry,) = merge_regressions(state=state, native_hints=[hint("case", None)])["items"]
    assert entry["checkpoint"] is None


def test_generic_checkpoints_can_name_a_public_observation_without_accepting_traversal():
    ledger = merge_regressions(
        native_work_id="active-work",
        native_hints=[hint("scenario", "after-release"), hint("tests/../private/test_case", "5")],
    )
    (entry,) = ledger["items"]
    assert (entry["case"], entry["checkpoint"]) == ("scenario", "after-release")


def test_live_observations_can_persist_at_known_work_without_claiming_checkpoint():
    previous = merge_regressions(native_work_id="known-running-work", native_hints=[hint()])
    (item,) = previous["items"]
    assert item["origin"] == {"work_id": "known-running-work"}
    assert item["source"] == hint()["source"]
    assert item["evidence"] == {"kind": "native"}
    assert not merge_regressions(native_work_id="invalid\nwork", native_hints=[hint()])["items"]
    assert not merge_regressions(state=snapshot("wrong"), native_work_id="known-running-work", native_hints=[hint()])[
        "items"
    ]
    persisted = json.loads(json.dumps(previous))
    assert merge_regressions(persisted, state=behavior(kind="match"))["items"] == []


def test_context_distinguishes_unverified_observation_from_core_and_explains_binding():
    previous = merge_regressions(state=oracle(), native_hints=[hint()])
    context = worker_regression_context(previous)
    assert "unverified native clue" in context and "Core mismatch" in context
    assert "regression-<id>" in context and "public-regression-v1 <id> <fixture_sha256>" in context
    assert BEHAVIOR_SCHEMA in context and "files" in context and "Exit 0" in context and "1 for mismatch" in context
    assert "origin Work old" in context and "source jiuwen:request-1:25" in context
    assert "first divergence" in context and "repair and rerun" in context and "keep it unresolved" in context


def test_old_pending_items_survive_pressure_and_verified_evidence_has_room():
    earlier = merge_regressions(
        state=snapshot(), native_hints=[hint(f"old_case_{i}") for i in range(MAX_NATIVE_REGRESSIONS)]
    )
    later = merge_regressions(earlier, state=oracle(), native_hints=[hint("cannot-displace-old")])
    assert later["items"][:MAX_NATIVE_REGRESSIONS] == earlier["items"]
    assert len(later["items"]) == MAX_NATIVE_REGRESSIONS + 1
    assert later["omitted"] == 1
    assert merge_regressions(later, state=oracle(), native_hints=[hint("cannot-displace-old")]) == later
    assert "omitted" in worker_regression_context(later)
    assert "old_case_0" in worker_regression_context(later, limit=1)
    assert "old_case_1" in worker_regression_context(later, limit=1, offset=1)


@pytest.mark.parametrize("kind", ["unavailable", "match"])
def test_comparison_unavailable_and_success_do_not_create_product_debt(kind):
    assert merge_regressions(state=oracle(kind=kind))["items"] == []


def test_oracle_requires_the_real_literal_helper_current_candidate_and_active_method():
    assert merge_regressions(state=oracle(literal=False))["items"] == []
    stale = oracle()
    stale.candidate = replace(stale.candidate, checkpoint="f" * 40)
    assert merge_regressions(state=stale)["items"] == []
    suspended = oracle()
    suspended.checks = ({**suspended.checks[0], "disposition": "suspended"},)
    assert merge_regressions(state=suspended)["items"] == []


def test_oracle_retest_requires_same_fixture_and_method_even_when_other_checks_pass():
    before = merge_regressions(state=oracle())
    (entry,) = before["items"]
    assert entry["case"] == "state-compare"
    assert entry["checkpoint"] == "5"
    assert merge_regressions(before, state=snapshot("other")) == before
    assert merge_regressions(before, state=oracle(work="other", kind="match", fixture="f" * 64)) == before
    assert merge_regressions(before, state=oracle(work="other", kind="match", method="f" * 64)) == before
    assert merge_regressions(before, state=oracle(work="other", kind="unavailable")) == before
    assert merge_regressions(before, state=oracle(work="other", kind="match"))["items"] == []


def test_capture_mismatches_without_closing_from_an_unqualified_candidate():
    before = merge_regressions(state=oracle())
    unrelated = oracle(work="changed", kind="mismatch", fixture="f" * 64)
    retained = merge_regressions(before, state=unrelated, allow_resolution=False)
    assert len(retained["items"]) == 2
    stale_pass = oracle(work="previous", kind="match")
    assert merge_regressions(retained, state=stale_pass, allow_resolution=False) == retained
    native_pass = merge_regressions(
        state=behavior(), native_hints=[hint()], allow_resolution=False,
    )
    assert native_pass["items"][0]["evidence"] == {"kind": "native"}


@pytest.mark.parametrize(
    "retest",
    [
        {"case": "different-case"},
        {"disposition": "pending"},
        {"bound": False},
        {"checked_fixture": False},
        {"method_failure": {"kind": "tool_lookup", "value": "tool"}},
    ],
)
def test_native_hint_cannot_be_closed_by_unbound_retest(retest):
    previous = merge_regressions(state=snapshot(), native_hints=[hint()])
    assert merge_regressions(previous, state=behavior(**retest)) == previous


def test_independently_reviewed_behavior_can_close_a_native_hint_only_by_true_retest():
    previous = merge_regressions(state=snapshot(), native_hints=[hint()])
    assert merge_regressions(previous, state=snapshot("new"), native_hints=[hint()]) == previous
    proper = behavior()
    spoiled = proper.verification.check_results[0] | {"stdout_tail": "same case now passes"}
    proper.verification = replace(proper.verification, check_results=(spoiled,))
    assert merge_regressions(previous, state=proper) == previous
    assert merge_regressions(previous, state=behavior(), native_hints=[hint()])["items"] == []


def test_reviewed_behavior_mismatch_freezes_the_fixture_and_method():
    previous = merge_regressions(state=snapshot(), native_hints=[hint()])
    failed = merge_regressions(previous, state=behavior(kind="mismatch"))
    (entry,) = failed["items"]
    assert entry["origin"] == previous["items"][0]["origin"]
    assert entry["evidence"]["kind"] == "behavior"
    assert merge_regressions(failed, state=behavior(fixture="f" * 64)) == failed
    assert merge_regressions(failed, state=behavior(method="f" * 64)) == failed
    assert merge_regressions(failed, state=behavior())["items"] == []
    assert merge_regressions(state=behavior(kind="mismatch"))["items"][0]["case"] == CASE
    assert not merge_regressions(state=behavior(kind="mismatch", bound=False))["items"]


def test_bound_behavior_can_verify_a_same_invocation_hint_without_repeating_its_label():
    state = behavior(kind="mismatch")
    row = state.verification.check_results[0]
    receipt = json.loads(row["stdout_tail"].strip().splitlines()[-1])
    del receipt["case"], receipt["checkpoint"]
    state.verification = replace(state.verification, check_results=({**row, "stdout_tail": json.dumps(receipt)},))
    (entry,) = merge_regressions(state=state, native_hints=[hint()])["items"]
    assert entry["case"] == CASE
    assert entry["evidence"]["kind"] == "behavior"
    assert entry["evidence"]["fixture_sha256"] == FIXTURE


def test_public_oracle_mismatch_and_match_are_read_from_real_different_core_works(tmp_path, git_repo_factory):
    fixture = tmp_path / "public.json"
    fixture.write_text('{"input":{"stock":3},"checkpoints":[1,2]}\n')
    expected = 'print(\'[{"step":1,"state":3},{"step":2,"state":3}]\')'
    actual = (
        'from pathlib import Path; import json; '
        'stock = int(Path("src/README.md").read_text()); '
        'print(json.dumps([{"step": 1, "state": 3}, {"step": 2, "state": stock}]))'
    )
    command = shlex.join([
        *shlex.split(PUBLIC_REFERENCE_COMMAND), "compare", str(fixture),
        "--reference", json.dumps([sys.executable, "-I", "-c", expected]),
        "--candidate", json.dumps([sys.executable, "-I", "-c", actual]),
    ])
    repo = git_repo_factory(tmp_path / "repo")
    runtime = make_evidence_runtime(
        repo, worktree_root=tmp_path / "worktrees", archive_root=tmp_path / "archive",
        deadline_unix=None, max_total_bytes=100_000,
    )
    contract = original_task_pair("Preserve continuous stock state.")["goal_contract"]

    def run(work_id, stock):
        state = runtime.create_work(
            request_id=work_id, work_id=work_id, contract=contract,
            policy=AcceptancePolicy(verify_command=command),
        )
        handle = runtime.begin_execution(state.work_id, expected_revision=state.revision, draft=True)
        (handle.workdir / "src/README.md").write_text(str(stock))
        state = runtime.save_candidate(handle, summary="Save test candidate")
        return runtime.verify_candidate(state.work_id, expected_revision=state.revision)

    failed = run("work-01", 4)
    ledger = merge_regressions(state=failed)
    (entry,) = ledger["items"]
    assert entry["checkpoint"] == "2"
    assert entry["evidence"]["fixture_sha256"] == hashlib.sha256(fixture.read_bytes()).hexdigest()
    assert failed.verification.status == "failed"
    passed = run("work-02", 3)
    assert passed.candidate.tree != failed.candidate.tree
    assert merge_regressions(json.loads(json.dumps(ledger)), state=passed)["items"] == []


def test_real_core_admitted_case_check_resolves_native_observation(tmp_path, git_repo_factory):
    case = "public-stock-trigger"
    key = regression_case_key(case)
    fixture = b'{"expected_stock":3}\n'
    fixture_sha = hashlib.sha256(fixture).hexdigest()
    previous = merge_regressions(native_work_id="work-01", native_hints=[hint(case, None)])
    repo = git_repo_factory(tmp_path / "repo")
    runtime = make_evidence_runtime(
        repo, worktree_root=tmp_path / "worktrees", archive_root=tmp_path / "archive",
        deadline_unix=None, max_total_bytes=100_000,
    )
    requirement = "Report the stock after the public trigger."
    state = runtime.create_work(
        request_id="work-02", work_id="work-02", contract=original_task_pair(requirement)["goal_contract"],
        policy=AcceptancePolicy(verify_command="true"),
    )
    handle = runtime.begin_execution(state.work_id, expected_revision=state.revision, draft=True)
    (handle.workdir / "public-stock.json").write_bytes(fixture)
    (handle.workdir / "src/README.md").write_text("3\n")
    state = runtime.save_candidate(handle, summary="Save the public candidate")
    name = f"regression-{key}"
    receipt = {"schema": BEHAVIOR_SCHEMA, "key": key, "fixture_sha256": fixture_sha}
    executable = (
        "import json; from pathlib import Path; "
        "expected = json.loads(Path('public-stock.json').read_text())['expected_stock']; "
        "actual = int(Path('src/README.md').read_text()); "
        f"receipt = {receipt!r}; "
        "receipt['outcome'] = 'match' if actual == expected else 'mismatch'; "
        "print(json.dumps(receipt)); raise SystemExit(actual != expected)"
    )
    state = runtime.register_checks(
        state.work_id, expected_revision=state.revision, reason="Reproduce the named public stock trigger",
        checks=[{
            "name": name, "command": shlex.join([sys.executable, "-I", "-c", executable]),
            "requirement": requirement, "files": ["public-stock.json"],
        }],
    )
    state = runtime.verify_candidate(state.work_id, expected_revision=state.revision)
    assert state.verification.status == "passed"
    assert merge_regressions(previous, state=state) == previous

    class Reviewer:
        id = "independent-reviewer"

        def probe(self):
            return AdapterProbe(adapter_id=self.id, available=True)

        def run_turn(self, request):
            return TurnResult("completed", "The public fixture binds the expected stock.", "fixture", 0)

    state = runtime.review_candidate(state.work_id, expected_revision=state.revision, reviewer=Reviewer())
    state = runtime.review_checks(
        state.work_id, expected_revision=state.revision, review=state.review,
        decisions=[{
            "name": name, "version": 1, "action": "admit",
            "reason": f"public-regression-v1 {key} {fixture_sha} The check reads and applies the pinned fixture.",
        }],
    )
    state = runtime.verify_candidate(state.work_id, expected_revision=state.revision)
    assert state.checks[0]["disposition"] == "admitted"
    assert state.verification.check_results[-1]["fingerprint"] == state.checks[0]["fingerprint"]
    assert merge_regressions(previous, state=state)["items"] == []
