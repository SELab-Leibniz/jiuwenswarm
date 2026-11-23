# Copyright (c) 2026 Chrys. All rights reserved.

"""Public reference comparison and receipt provenance regression controls."""

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pact_core.runtime import AcceptancePolicy
from test_control_regressions import repository

from jiuwenswarm.lh.early_acceptance_audit import parse_audit_report
from jiuwenswarm.lh.harbor_unified.candidate_artifacts import make_evidence_runtime
from jiuwenswarm.lh.method_routing import method_diagnosis
from jiuwenswarm.lh.public_reference import SCHEMA, check
from jiuwenswarm.lh.public_reference_evidence import recorded_reference_checks
from jiuwenswarm.lh.reference_client_spec import PUBLIC_REFERENCE_COMMAND
from jiuwenswarm.lh.suite import original_task_pair

COUNTER = """
import json, sys
from pathlib import Path

request = json.load(sys.stdin)
value = request["input"]["initial"]
rows = []
for step, delta in enumerate(request["input"]["actions"], 1):
    value += delta
    if step in request["checkpoints"]:
        observed = value
        if sys.argv[1] == "candidate" and Path("module.py").read_text() == "wrong" and step == 2:
            observed = value + 1
        rows.append({"step": step, "state": {"stock/total~": [observed]}})
print(json.dumps(rows))
"""


@pytest.fixture
def fixture(tmp_path):
    request = tmp_path / "request.json"
    request.write_text('{"input":{"initial":0,"actions":[2,-1,4]},"checkpoints":[1,2,3]}\n')
    return request


def adapter(body):
    return [sys.executable, "-I", "-c", body]


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("pass", "invalid_json"),
        ("print('[]')", "incomplete_trace"),
        ("print('forbidden')", "invalid_json"),
        ("""print('{"error":"access_denied"}')""", "access_denied"),
        ("raise SystemExit(23)", "nonzero_exit"),
        ("print('x' * 1048577)", "output_too_large"),
        ("""print('[{"step":1,"state":1}]' * 3)""", "invalid_json"),
    ],
)
def test_probe_failure_skips_candidate(tmp_path, fixture, code, reason):
    marker = tmp_path / "called"
    candidate = adapter(f"open({str(marker)!r}, 'x').close()")
    result = check(fixture, adapter(code), candidate)
    assert (result["status"], result["reason"]) == ("reference_unavailable", reason)
    assert not marker.exists()


@pytest.mark.parametrize(
    "rows",
    [
        [{"step": i, "state": 0} for i in (1, 3, 2)],
        [{"step": i, "state": 0} for i in (1, 2, 2)],
        [{"step": i, "state": 0} for i in (True, 2, 3)],
        [{"step": i, "error": "denied"} for i in (1, 2, 3)],
    ],
)
def test_malformed_candidate_trace_is_unverified(fixture, rows):
    result = check(fixture, [*adapter(COUNTER), "reference"], adapter("print(" + repr(json.dumps(rows)) + ")"))
    assert result["status"] == "candidate_unavailable"
    assert result["reason"] == "invalid_observation"


@pytest.mark.parametrize("steps", [[], [1, 1], [2, 1], [True], [-1], [1]])
def test_compare_requires_multiple_ordered_checkpoints(fixture, steps):
    fixture.write_text(json.dumps({"input": {}, "checkpoints": steps}))
    assert check(fixture, ["unused"], ["unused"])["status"] == "invalid_request"


def test_probe_accepts_single_step_and_reports_deadline(fixture):
    fixture.write_text('{"input":null,"checkpoints":[0]}')
    assert check(fixture, adapter('print(\'[{"step":0,"state":null}]\')'))["status"] == "available"
    assert check(fixture, adapter("import time; time.sleep(30)"), timeout=0.2)["reason"] == "timeout"


@pytest.mark.parametrize(
    ("left", "right", "pointer"),
    [
        ("true", "1", ""),
        ("0.10000000000000001", "0.1", ""),
        ("0.10", "0.1", None),
        ("1.0", "1", None),
        ('{"a":[]}', '{"a":[null]}', "/a/0"),
        ('{"x":null}', "{}", "/x"),
        ('{"b":0,"a":1}', '{"a":1,"b":0}', None),
    ],
)
def test_public_state_json_semantics(fixture, left, right, pointer):
    fixture.write_text('{"input":null,"checkpoints":[0,1]}')

    def emit(value):
        return adapter("print(" + repr('[{"step":0,"state":' + value + '},{"step":1,"state":null}]') + ")")

    result = check(fixture, emit(left), emit(right))
    if pointer is None:
        assert result["status"] == "match"
    else:
        assert (result["status"], result["step"], result["field"]) == ("mismatch", 0, pointer)


@pytest.mark.parametrize(
    ("status", "count", "clean"),
    [
        ("reference_unavailable", 0, False),
        ("candidate_unavailable", 0, False),
        ("available", 2, False),
        ("match", 1, False),
        ("mismatch", 2, False),
        ("match", 2, True),
    ],
)
def test_stateful_audit_consumes_comparison_receipt(status, count, clean):
    receipt = {
        "schema": SCHEMA,
        "status": status,
        "checkpoints": count,
        "request_sha256": "a" * 64,
        "reference_argv_sha256": "b" * 64,
        "candidate_argv_sha256": "c" * 64,
    }
    row = {"requirement": "counter sequence", "verified": True, "public_reference_check": receipt}
    assert (
        parse_audit_report(
            json.dumps({"verdict": "clean", "checked": [row], "gaps": []}), reference_receipts=(receipt,)
        ).clean
        is clean
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("success", "match"), ("nonzero", "nonzero_exit"), ("hold_pipe", "timeout"), ("overflow", "output_too_large")],
)
def test_compare_cleans_adapter_process_group(tmp_path, fixture, mode, expected):
    marker = tmp_path / "survived"
    descendant = f"import time; time.sleep(1); open({str(marker)!r}, 'x').close()"
    rows = '[{"step":1,"state":0},{"step":2,"state":0},{"step":3,"state":0}]'
    code = (
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable, '-I', '-c', {descendant!r}], "
        f"stdout={'None' if mode == 'hold_pipe' else 'subprocess.DEVNULL'})\n"
        + (
            "raise SystemExit(17)"
            if mode == "nonzero"
            else "print('x'*1048577)"
            if mode == "overflow"
            else f"print({rows!r})"
        )
    )
    if mode == "hold_pipe":
        code += "\nimport time; time.sleep(10)"
    receipt = check(fixture, adapter(f"print({rows!r})"), adapter(code), timeout=0.7 if mode == "hold_pipe" else 5)
    if expected == "match":
        assert receipt["status"] == expected
    else:
        assert (receipt["status"], receipt["reason"]) == ("candidate_unavailable", expected)
        if mode == "nonzero":
            assert receipt["exit_code"] == 17
    time.sleep(1.1)
    assert not marker.exists()


def test_identical_adapters_cannot_validate_a_candidate(fixture):
    assert check(fixture, ["adapter"], ["adapter"])["reason"] == "identical_adapters"


def test_candidate_output_alone_does_not_classify_a_check_failure():
    from jiuwenswarm.lh.public_reference_evidence import direct_reference_failure

    row = {
        "exit_code": 2,
        "stdout_tail": json.dumps({"schema": SCHEMA, "status": "reference_unavailable", "reason": "access_denied"}),
    }
    for command in ("./candidate", PUBLIC_REFERENCE_COMMAND + " compare request | ./candidate"):
        assert direct_reference_failure({**row, "command": command}) is None


def test_self_reported_match_cannot_certify_coverage(tmp_path):
    receipt = {
        "schema": SCHEMA,
        "status": "match",
        "checkpoints": 2,
        "request_sha256": "a" * 64,
        "candidate_argv_sha256": "b" * 64,
        "reference_argv_sha256": "c" * 64,
    }
    command = shlex.join(adapter(f"print({json.dumps(receipt)!r})"))
    repo = repository(tmp_path)
    runtime = make_evidence_runtime(
        repo,
        worktree_root=tmp_path / "trees",
        archive_root=tmp_path / "archive",
        deadline_unix=None,
        max_total_bytes=100000,
    )
    state = runtime.create_work(
        request_id="untrusted",
        work_id="untrusted",
        contract=original_task_pair("stock")["goal_contract"],
        policy=AcceptancePolicy(verify_command="true", allow_unverified=False),
    )
    state = runtime.register_checks(
        state.work_id,
        expected_revision=state.revision,
        reason="ordinary check",
        checks=[{"name": "stock", "command": command, "requirement": "stock"}],
    )
    state = runtime.verify_candidate(state.work_id, expected_revision=state.revision)
    assert state.verification.status == "passed" and not recorded_reference_checks(state)
    report = parse_audit_report(
        json.dumps(
            {
                "verdict": "clean",
                "checked": [{"requirement": "stock", "verified": True, "public_reference_check": receipt}],
                "gaps": [],
            }
        ),
        reference_receipts=recorded_reference_checks(state),
    )
    assert report.unverified and not report.clean and not report.gaps


def test_report_without_reproduction_is_unverified():
    row = {
        "requirement": "stock",
        "verified": False,
        "public_reference_check": {"schema": SCHEMA, "status": "mismatch", "step": 2, "field": "/stock"},
    }
    report = parse_audit_report(json.dumps({"verdict": "gaps", "checked": [row], "gaps": []}))
    assert report.unverified and not report.gaps


