# Copyright (c) 2026 Chrys. All rights reserved.

# Ported from Chrys 6fcfb3321892735631b49f33236b1e2a988ded2d: src/chrys/service/requirement_clarification/verification.py; solver execution is JiuwenSwarm.
"""Public checks synthesized from clarified requirements, separate from PACT goals."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
VERIFICATION_FILE = "public-verification.json"


class PublicCheck(BaseModel):
    """One repeatable command and the acceptance criteria it is intended to exercise."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=120)
    command: NonBlank = Field(max_length=16000)
    target_ac_ids: list[NonBlank] = Field(min_length=1, max_length=1)
    rationale: NonBlank = Field(max_length=4000)


class PublicVerification(BaseModel):
    """A generated public verifier is a proposal, never the independent grading oracle."""

    model_config = ConfigDict(extra="forbid")

    schema_id: Literal["jiuwenswarm/pact-public-verification/v1"] = Field(alias="schema")
    status: Literal["ready", "unavailable"]
    reason: NonBlank = Field(max_length=4000)
    checks: list[PublicCheck] = Field(max_length=100)

    def validate_coverage(self, acceptance_ids: set[str]) -> None:
        """Refuse empty/unavailable proposals and foreign or missing criterion references."""
        if self.status != "ready" or not self.checks:
            raise ValueError("public verification unavailable: " + self.reason)
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("public verification check IDs must be unique")
        covered: set[str] = set()
        for check in self.checks:
            if "\x00" in check.command:
                raise ValueError("public verification commands cannot contain NUL")
            if len(check.target_ac_ids) != len(set(check.target_ac_ids)):
                raise ValueError("public verification criterion references must be unique")
            covered.update(check.target_ac_ids)
        if covered != acceptance_ids:
            raise ValueError(
                "public verification must reference exactly the Goal Contract's acceptance criteria: "
                f"missing={sorted(acceptance_ids - covered)}, foreign={sorted(covered - acceptance_ids)}"
            )


def json_digest(value: object) -> str:
    """Hash semantic JSON independently of artifact indentation."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


_PYTHON_HEREDOC = re.compile(
    r"(?m)^(?P<header>python(?:3(?:\.\d+)?)?\b[^\n]*<<(?P<quote>['\"]?)"
    r"(?P<tag>[A-Za-z_][A-Za-z_0-9]*)(?P=quote)[ \t]*\n)(?P<body>[\s\S]*?)^(?P=tag)[ \t]*$"
)
_DATA_PROBE_MODULES = frozenset({"os", "re", "sys", "json", "pathlib", "shlex", "hashlib", "typing", "collections"})
_DYNAMIC_EXECUTION_NAMES = frozenset(
    {"eval", "exec", "compile", "__import__", "__builtins__", "getattr", "globals", "locals", "vars"}
)
_EXECUTION_ATTRIBUTES = frozenset(
    {"system", "popen", "Popen", "run", "call", "check_call", "check_output", "run_path", "__getattribute__"}
)


def referenced_public_tests(command: str) -> set[str]:
    """Keep test dispatch references, without treating inline data fixtures as test invocations.

    This is an evidence guard, not a shell/Python security analyzer. Unknown heredoc
    shapes, parse failures and scripts using dispatch/reflection retain the previous
    conservative whole-script check. A simple artifact parser may legitimately use
    nonexistent test filenames in synthetic diff fixtures without executing them.
    """

    def exclude_data_program(match: re.Match[str]) -> str:
        try:
            tree = ast.parse(match["body"])
        except (SyntaxError, ValueError, RecursionError):
            return match[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name.split(".", 1)[0] not in _DATA_PROBE_MODULES for alias in node.names
            ):
                return match[0]
            if isinstance(node, ast.ImportFrom):
                if node.level or (node.module or "").split(".", 1)[0] not in _DATA_PROBE_MODULES:
                    return match[0]
                if any(
                    alias.name == "*" or alias.name in _EXECUTION_ATTRIBUTES or alias.name.startswith(("exec", "spawn"))
                    for alias in node.names
                ):
                    return match[0]
            if isinstance(node, ast.Name) and node.id in _DYNAMIC_EXECUTION_NAMES:
                return match[0]
            if isinstance(node, ast.Attribute) and (
                node.attr in _EXECUTION_ATTRIBUTES or node.attr.startswith(("exec", "spawn"))
            ):
                return match[0]
        return match["header"] + match["tag"]

    inspected_command = _PYTHON_HEREDOC.sub(exclude_data_program, command)
    return set(re.findall(r"\btests?/[A-Za-z0-9_./-]+\.py\b", inspected_command))


PUBLIC_VERIFICATION_INSTRUCTIONS = """Generate repeatable public verification commands from the original requirement,
validated clarification, immutable Goal Contract and Initial Plan. Inspect public tests, build configuration and
documentation in the frozen repository with the read-only tools. The commands will run from a fresh checkout of a
candidate revision, not this snapshot: never embed snapshot paths, session paths or implementation-specific answers.
The candidate is a CLEAN COMMITTED checkout: git diff HEAD and git diff are normally empty even for a correct change.
Run from the supplied current directory; do not cd to an original workspace path or inherited REPO/WORKSPACE variable.
Python probes must import this candidate's package (use an inline heredoc or explicitly prepend cwd to sys.path),
not an installed or primary-checkout copy. Verify the actual import source when the package could be ambiguous.
Compare filesystem identities after resolving both paths: a relative sys.path entry does NOT imply a relative
module.__file__. For example, compare Path(package.__file__).resolve().parent with (Path.cwd() / 'package').resolve();
never require __file__ to start with './', and do not compare unresolved '/cwd/./package' with '/cwd/package'.
Only invoke existing public test files that you actually inspected with read_file. Verify package exports and
import patterns against this frozen revision; do not invent re-exports or use knowledge of a later library version.
Prefer existing relevant public tests. When they do not cover the requested behavior, write a self-contained regression
probe as a command (for example a Python heredoc with actual assertions against the public API). Do not modify source
or test files, install dependencies, fetch external tests, or access private benchmark/grader/gold assets.
Map each check to exactly ONE existing acceptance criterion ID; cover EVERY behavior in it, not just one example.
This call targets one criterion. List its distinct obligations internally before writing checks: every named variant,
positive and negative boundary, and observable outcome must have an actual assertion. Do not promise coverage in a
rationale that the command does not implement. Do not emit placeholder checks or source-keyword searches in place of
runtime behavior. Process/history obligations need actual provenance: a clean detached HEAD alone does not prove
that work was done on a new branch or that the branch started from a specified ref. At execution, the verifier shim
sets CHRYS_PACT_SOURCE_WORKSPACE to the controller-owned primary checkout. You may READ its Git branch/ref/reflog
metadata for provenance, while all code imports, behavior checks and edits remain confined to the candidate cwd.
The frozen snapshot deliberately contains only reachable history, not the primary checkout's branch refs/reflogs;
their absence here does not establish their absence at verification time. Check the requested base and feature branch
against the frozen initial commit, real branch-creation evidence and candidate ancestry. Check that all candidate
work is committed. PACT promotes the verified patch afterward; the primary agent must then fulfill final primary
workspace delivery obligations. Never modify primary Git state from a verifier, accept detached HEAD as a feature
branch, or invent a successful provenance check. Report unavailable if no meaningful runtime check can be written.
A wrapper/type annotation must preserve the valid use of the underlying type: select a supported underlying API from
public repository evidence and compare equivalent ordinary declarations. Do not demand an unrelated feature such as
a new parameter location or new supported type. Registration alone does not prove request-time generic behavior.
For each preserved behavior, include an equivalent ordinary declaration as a control, with the same fixture and
inputs. Establish expected defaults from that control: test clients can add headers, retain cookies or transform
request data. An input omitted in the test call is not necessarily absent from the request received by the app.
Prefer explicit isolated inputs, and assert the control response before comparing the requested new declaration.
For a preservation-only criterion, compare with the ordinary control's observed result; do not invent rejection
behavior merely because an input looks unusual. Only demand a particular negative status when public evidence
establishes that boundary. A type that already infers a parameter kind does not prove its explicit marker was read;
also exercise a non-default marker option whose omission would change an asserted response or schema field.
Every fixture must obey ALL other acceptance criteria: a test of one feature cannot depend on a declaration that
another criterion explicitly forbids. Existing language/type-system equality may compare metadata by object identity;
reuse the same metadata objects when testing flattening rather than comparing separately constructed instances.
Trace helper calls and argument types as executable Python. Construct the valid ordinary control through the SAME
helper before an invalid declaration: an AttributeError/TypeError caused by looking up a route path as an attribute,
calling a string, or other fixture setup errors is not evidence that the API rejected the declaration. Keep fixture
setup outside rejection-catching blocks; catch only the actual API registration/validation call being tested.
Follow the repository's actual schema/documentation references before inspecting a field: an inline definition and
a referenced definition can describe the same behavior. Do not require one representation unless the user asked for it.
For source-tree implementation/provenance criteria, check the actual import location and source diff against the
provided baseline. Do not invent an internal function that must unwrap, store or return a particular representation.
For an explicit artifact-delivery criterion, execute the public submission command the user supplied from this
candidate and assert the actual requested artifact exists with the required contents. Its documented side effect is
allowed. A dirty diff, changed filename or existing stale artifact is not a substitute for executing delivery.
A command explicitly supplied by the user can be installed on the runtime PATH outside the source repository.
Its absence from the frozen source or pyproject does not prove it is unavailable. Invoke that exact command and
let a missing executable fail the runtime check; do not reject it merely because its implementation is not in Git.
If the provided delivery mechanism cannot work from a committed candidate, report unavailable rather than fabricate
a proxy criterion. Never modify the candidate Git state merely to make a dirty-diff assertion pass.
Keep checks independent: an early mission must not require a future mission to be complete. The controller runs
checks for the current mission plus all previously satisfied criteria, so regressions are also checked. The rationale
must identify the public evidence and the observable assertion, not just repeat the criterion. A nonzero command exit
must mean failure. Never suppress failures, return success after errors, use always-success commands, accept zero tests,
or replace behavioral checks with syntax/import-only checks. Tests may legitimately fail before the feature is fixed;
do not weaken them to make a baseline pass. Keep commands bounded and noninteractive.
Missing dependencies must fail a required check, never print SKIP and exit successfully without exercising it.
If a meaningful executable public verifier cannot be written from the available evidence, return status unavailable,
an explanation, and no checks. Do not fabricate a test path or pretend coverage. Otherwise return status ready.
Return only the closed jiuwenswarm/pact-public-verification/v1 JSON schema. These are public development checks; only the
independent benchmark verifier decides the benchmark score.
"""

PUBLIC_VERIFICATION_REVIEW_INSTRUCTIONS = (
    PUBLIC_VERIFICATION_INSTRUCTIONS
    + """

You are reviewing an untrusted draft verifier written by another side agent. Re-read the public source and tests
needed to audit its assumptions. The draft's claims and expected outputs are NOT authority. Trace every assertion to
the original requirement or established public API behavior; check fixtures against the ENTIRE Goal Contract.
Look especially for unsupported control declarations, contradictory defaults, identity-dependent comparisons,
unresolved schema references, implementation-detail assertions and checks that pass without exercising the target.
Preserve all requested behavior and meaningful negative cases; do not weaken a check just because new behavior is
absent from the frozen baseline. Correct verifier defects only. Return a complete replacement public-verification
object for this criterion, with its reason explaining the substantive corrections or why the inspected draft is sound.
Never return an approval sentence in place of the complete executable checks.
"""
)
