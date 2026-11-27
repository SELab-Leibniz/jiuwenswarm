"""Git-backed workspace isolation and checkpoints for PACT loops.

Ported from chrys ``git_checkpoints.py``. Checkpoints are always built from
the actual filesystem via a temporary index; the agent's own git state
(commits, staging, branches) is never trusted (AGENTS.md invariant 5).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from .artifacts import atomic_write_text
from .schemas import (
    InitializationCheckpoint,
    PromotionResult,
    ReviewerMutation,
    RoundCheckpoint,
    WorkspaceSnapshot,
)

_LOOP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PACT_PATHSPECS = (
    ".",
    ":(top,exclude,icase).pact",
    ":(top,exclude,icase).pact/**",
    ":(top,exclude,icase).pact-io",
    ":(top,exclude,icase).pact-io/**",
)
_PACT_RESERVED_PATHSPECS = (
    ":(top,icase).pact",
    ":(top,icase).pact/**",
    ":(top,icase).pact-io",
    ":(top,icase).pact-io/**",
)
_VOLATILE_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "dist",
        "build",
        "target",
        ".venv",
        "venv",
    }
)
_PACT_IDENTITY = {
    "GIT_AUTHOR_NAME": "PACT",
    "GIT_AUTHOR_EMAIL": "pact@pact-core.local",
    "GIT_COMMITTER_NAME": "PACT",
    "GIT_COMMITTER_EMAIL": "pact@pact-core.local",
}


def default_worktree_root() -> Path:
    return Path.home() / ".pact-core" / "worktrees"


class PactGitError(RuntimeError):
    """Raised when a Git checkpoint invariant cannot be established."""


class CheckpointStore:
    """Own one PACT loop's private checkpoint ref and role worktrees."""

    def __init__(self, workspace: Path, loop_id: str, *, worktree_root: Path | None = None) -> None:
        if not _LOOP_ID_RE.fullmatch(loop_id):
            raise ValueError(f"Invalid PACT loop id: {loop_id}")
        git = shutil.which("git")
        if git is None:
            raise PactGitError("PACT Git checkpoints require git on PATH")
        self._git = git
        self.workspace = workspace.expanduser().resolve()
        self.loop_id = loop_id
        repo_key = hashlib.sha256(str(self.workspace).encode()).hexdigest()[:16]
        root = worktree_root or default_worktree_root()
        self.loop_root = root.expanduser().resolve() / repo_key / loop_id
        self.worker_worktree = self.loop_root / "worker"
        self.checkpoint_ref = f"refs/pact/{loop_id}"
        self.initialization: InitializationCheckpoint | None = None
        self.current_commit = ""

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        command_env["GIT_OPTIONAL_LOCKS"] = "0"
        if env:
            command_env.update(env)
        result = subprocess.run(
            [self._git, *args],
            cwd=cwd or self.workspace,
            env=command_env,
            input=input_text.encode('utf-8', errors='surrogateescape') if input_text is not None else None,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            text=False,
            check=False,
        )
        # Decode without universal-newline translation: Git patches carry exact source bytes.
        result = subprocess.CompletedProcess(
            result.args, result.returncode, result.stdout.decode('utf-8', errors='surrogateescape'), result.stderr.decode('utf-8', errors='surrogateescape')
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
            raise PactGitError(f"git {' '.join(args)} failed: {detail}")
        return result

    def _output(self, args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
        return self._run(args, cwd=cwd, env=env).stdout.strip()

    def _branch(self, workspace: Path) -> str | None:
        result = self._run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=workspace, check=False)
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit code {result.returncode}"
            raise PactGitError(f"git symbolic-ref failed: {detail}")
        return result.stdout.strip()

    @contextmanager
    def _temporary_index(self) -> Iterator[dict[str, str]]:
        with tempfile.TemporaryDirectory(prefix="pact-index-") as directory:
            yield {"GIT_INDEX_FILE": str(Path(directory) / "index")}

    def _workspace_tree(self, workspace: Path, base_commit: str) -> str:
        with self._temporary_index() as env:
            self._run(["read-tree", base_commit], cwd=workspace, env=env)
            self._run(["add", "-A", "--", "."], cwd=workspace, env=env)
            self._run(
                [
                    "rm",
                    "-r",
                    "--force",
                    "--cached",
                    "--ignore-unmatch",
                    "--",
                    *_PACT_RESERVED_PATHSPECS,
                ],
                cwd=workspace,
                env=env,
            )
            return self._output(["write-tree"], cwd=workspace, env=env)

    def _index_tree(self, workspace: Path) -> str:
        index_path = Path(self._output(["rev-parse", "--git-path", "index"], cwd=workspace))
        if not index_path.is_absolute():
            index_path = workspace / index_path
        with tempfile.TemporaryDirectory(prefix="pact-index-snapshot-") as directory:
            snapshot_path = Path(directory) / "index"
            shutil.copyfile(index_path, snapshot_path)
            return self._output(["write-tree"], cwd=workspace, env={"GIT_INDEX_FILE": str(snapshot_path)})

    def _status(self, workspace: Path, *, include_reserved: bool = False) -> str:
        args = ["status", "--porcelain=v2", "--branch", "--untracked-files=all"]
        if not include_reserved:
            args.extend(["--", *_PACT_PATHSPECS])
        return self._run(args, cwd=workspace).stdout.rstrip()

    def _namespace_violations(self, workspace: Path) -> list[str]:
        violations: list[str] = []
        for pact_root in workspace.iterdir():
            if pact_root.name.casefold() != ".pact":
                continue
            if pact_root.is_symlink() or not pact_root.is_dir():
                violations.append(pact_root.relative_to(workspace).as_posix())
                continue
            descendants = [
                path.relative_to(workspace).as_posix()
                for path in pact_root.rglob("*")
                if path.is_file() or path.is_symlink()
            ]
            violations.extend(descendants or [pact_root.relative_to(workspace).as_posix()])
        return sorted(violations)

    def _tracked_reserved_paths(self, head_commit: str) -> list[str]:
        index_output = self._run(["ls-files", "-z"]).stdout
        head_output = self._run(["ls-tree", "-rz", "--name-only", head_commit]).stdout
        paths = {
            path
            for path in (*index_output.split("\0"), *head_output.split("\0"))
            if path and path.split("/", 1)[0].casefold() in {".pact", ".pact-io"}
        }
        return sorted(paths)

    def _untracked_volatile_directories(self) -> list[str]:
        output = self._run(["ls-files", "--others", "--exclude-standard", "-z"]).stdout
        directories: set[str] = set()
        for path in (entry for entry in output.split("\0") if entry):
            parts = path.split("/")
            for index, part in enumerate(parts[:-1] if len(parts) > 1 else parts):
                if part.casefold() in _VOLATILE_DIRECTORY_NAMES:
                    relative = "/".join(parts[: index + 1])
                    if (self.workspace / relative).is_dir():
                        directories.add(relative)
                    break
        return sorted(directories)

    def _snapshot(
        self,
        workspace: Path,
        base_commit: str,
        *,
        include_reserved_status: bool = False,
    ) -> WorkspaceSnapshot:
        return WorkspaceSnapshot(
            head_commit=self._output(["rev-parse", "HEAD"], cwd=workspace),
            branch=self._branch(workspace),
            index_tree=self._index_tree(workspace),
            workspace_tree=self._workspace_tree(workspace, base_commit),
            status=self._status(workspace, include_reserved=include_reserved_status),
        )

    def workspace_identity(self) -> WorkspaceSnapshot:
        """Snapshot source identity without creating checkpoints or worktrees."""
        head = self._output(["rev-parse", "HEAD"])
        return self._snapshot(self.workspace, head)

    def _commit_tree(self, tree: str, parent: str, message: str) -> str:
        return self._run(
            ["commit-tree", tree, "-p", parent],
            env=_PACT_IDENTITY,
            input_text=f"{message}\n",
        ).stdout.strip()

    def _diff(self, start: str, end: str) -> str:
        return self._run(
            ["diff", "--binary", "--full-index", "--no-ext-diff", start, end, "--", *_PACT_PATHSPECS]
        ).stdout

    def _changed_paths(self, start: str, end: str) -> list[str]:
        output = self._run(["diff", "--name-only", "-z", start, end, "--", *_PACT_PATHSPECS]).stdout
        return [path for path in output.split("\0") if path]

    def _ref_commit(self) -> str:
        return self._output(["rev-parse", "--verify", self.checkpoint_ref])

    def initialize(self) -> InitializationCheckpoint:
        """Create C0 from the source workspace and a detached worker worktree."""
        if not self.workspace.is_dir():
            raise PactGitError(f"PACT workspace does not exist: {self.workspace}")
        inside = self._output(["rev-parse", "--is-inside-work-tree"])
        if inside != "true":
            raise PactGitError("PACT workspace must be a non-bare Git worktree")
        repo_root = Path(self._output(["rev-parse", "--show-toplevel"])).resolve()
        if repo_root != self.workspace:
            raise PactGitError(f"PACT workspace must be the Git repository root: {repo_root}")
        head_commit = self._output(["rev-parse", "--verify", "HEAD"])
        tracked_reserved = self._tracked_reserved_paths(head_commit)
        if tracked_reserved:
            raise PactGitError(
                "PACT reserves .pact/** and .pact-io/**; tracked files exist in a reserved namespace"
            )
        volatile_directories = self._untracked_volatile_directories()
        if volatile_directories:
            joined = ", ".join(volatile_directories)
            raise PactGitError(
                "PACT found untracked, unignored volatile artifact directories: "
                f"{joined}. Add them to .gitignore or .git/info/exclude before running PACT"
            )
        existing_ref = self._run(["show-ref", "--verify", "--quiet", self.checkpoint_ref], check=False)
        if existing_ref.returncode == 0:
            raise PactGitError(f"PACT checkpoint ref already exists: {self.checkpoint_ref}")
        if existing_ref.returncode not in {0, 1}:
            raise PactGitError(f"Unable to inspect PACT checkpoint ref: {self.checkpoint_ref}")
        if self.loop_root.exists():
            raise PactGitError(f"PACT worktree directory already exists: {self.loop_root}")

        source = self._snapshot(self.workspace, head_commit, include_reserved_status=True)
        baseline_commit = self._commit_tree(
            source.workspace_tree,
            head_commit,
            f"PACT {self.loop_id} initialization baseline",
        )
        self._run(["update-ref", self.checkpoint_ref, baseline_commit, ""])
        try:
            self.worker_worktree.parent.mkdir(parents=True, exist_ok=False)
            self._run(["worktree", "add", "--detach", str(self.worker_worktree), baseline_commit])
        except Exception:
            self._run(["update-ref", "-d", self.checkpoint_ref, baseline_commit], check=False)
            raise

        initialization = InitializationCheckpoint(
            source_workspace=self.workspace,
            head_commit=source.head_commit,
            branch=source.branch,
            index_tree=source.index_tree,
            workspace_tree=source.workspace_tree,
            status=source.status,
            baseline_commit=baseline_commit,
            baseline_tree=source.workspace_tree,
            checkpoint_ref=self.checkpoint_ref,
            worker_worktree=self.worker_worktree,
        )
        self.initialization = initialization
        self.current_commit = baseline_commit
        return initialization

    def _require_initialized(self) -> InitializationCheckpoint:
        if self.initialization is None:
            raise PactGitError("PACT Git checkpoint store is not initialized")
        return self.initialization

    def restore_promotion_candidate(
        self,
        initialization: InitializationCheckpoint,
        checkpoint: str,
    ) -> None:
        """Restore only the identity needed to promote a retained candidate.

        Deferred promotion deliberately does not recreate a worker worktree or
        resume execution. It binds the immutable initialization snapshot to the
        still-retained private checkpoint ref, then lets ``promote`` repeat all
        source-workspace drift checks before applying the patch.
        """
        if initialization.source_workspace.expanduser().resolve() != self.workspace:
            raise PactGitError("promotion source workspace does not match initialization")
        if initialization.checkpoint_ref != self.checkpoint_ref:
            raise PactGitError("promotion checkpoint ref does not match loop identity")
        if self._ref_commit() != checkpoint:
            raise PactGitError("promotion candidate does not match retained checkpoint ref")
        ancestor = self._run(
            ["merge-base", "--is-ancestor", initialization.baseline_commit, checkpoint],
            check=False,
        )
        if ancestor.returncode != 0:
            raise PactGitError("promotion candidate is not descended from its baseline")
        self.initialization = initialization
        self.current_commit = checkpoint

    def assert_round_start(self) -> None:
        """Require the worker checkout and shared refs to match the durable checkpoint."""
        self._require_initialized()
        if self._output(["rev-parse", "HEAD"], cwd=self.worker_worktree) != self.current_commit:
            raise PactGitError("PACT worker HEAD does not match the durable checkpoint")
        if self._workspace_tree(self.worker_worktree, self.current_commit) != self._output(
            ["rev-parse", f"{self.current_commit}^{{tree}}"]
        ):
            raise PactGitError("PACT worker worktree is not clean at round start")
        self.assert_control_ownership(self.current_commit)

    def assert_source_unchanged(self) -> None:
        """Require the complete source basis, including uncommitted files/index."""
        initialization = self._require_initialized()
        current = self._snapshot(self.workspace, initialization.head_commit)
        if (
            current.head_commit != initialization.head_commit
            or current.branch != initialization.branch
            or current.index_tree != initialization.index_tree
            or current.workspace_tree != initialization.workspace_tree
        ):
            raise PactGitError("source workspace changed since draft initialization")

    def initialize_from_checkpoint(
        self,
        initialization: InitializationCheckpoint,
        checkpoint: str,
        *,
        expected_tree: str | None = None,
        expected_parent: str | None = None,
    ) -> InitializationCheckpoint:
        """Fork an exact retained checkpoint into a fresh, controller-owned checkout.

        The original source/baseline remain the promotion basis. This does not
        resume an old worktree or inspect any agent-created commit/ref.
        """
        if initialization.source_workspace.expanduser().resolve() != self.workspace:
            raise PactGitError("draft source workspace does not match initialization")
        identities = (
            checkpoint, initialization.head_commit, initialization.index_tree,
            initialization.workspace_tree, initialization.baseline_commit, initialization.baseline_tree,
        )
        if any(not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) for value in identities):
            raise PactGitError("draft requires full Git object identities")
        old_ref = initialization.checkpoint_ref
        old_id = old_ref.removeprefix("refs/pact/")
        if old_ref != f"refs/pact/{old_id}" or not _LOOP_ID_RE.fullmatch(old_id):
            raise PactGitError("invalid retained draft checkpoint ref")
        if self._output(["rev-parse", "--verify", old_ref]) != checkpoint:
            raise PactGitError("draft does not match retained checkpoint ref")
        if expected_tree is not None and self._output(["rev-parse", f"{checkpoint}^{{tree}}"] ) != expected_tree:
            raise PactGitError("draft checkpoint tree does not match evidence")
        if expected_parent is not None and self._output(["rev-list", "--parents", "-n", "1", checkpoint]).split() != [checkpoint, expected_parent]:
            raise PactGitError("draft checkpoint parent does not match evidence")
        if self._run(
            ["merge-base", "--is-ancestor", initialization.baseline_commit, checkpoint],
            check=False,
        ).returncode != 0:
            raise PactGitError("draft checkpoint is not descended from its baseline")
        if self._output(["rev-list", "--parents", "-n", "1", initialization.baseline_commit]).split() != [initialization.baseline_commit, initialization.head_commit]:
            raise PactGitError("draft baseline is not anchored to its source HEAD")
        if self._output(["rev-parse", f"{initialization.baseline_commit}^{{tree}}"] ) != initialization.baseline_tree:
            raise PactGitError("draft baseline tree does not match initialization")
        if initialization.baseline_tree != initialization.workspace_tree:
            raise PactGitError("draft baseline does not match source workspace tree")
        existing = self._run(["show-ref", "--verify", "--quiet", self.checkpoint_ref], check=False)
        if existing.returncode != 1 or self.loop_root.exists():
            raise PactGitError("draft destination checkpoint ref or worktree already exists")
        self.initialization = replace(
            initialization, checkpoint_ref=self.checkpoint_ref,
            worker_worktree=self.worker_worktree,
        )
        self.assert_source_unchanged()
        self._run(["update-ref", self.checkpoint_ref, checkpoint, ""])
        try:
            self.worker_worktree.parent.mkdir(parents=True, exist_ok=False)
            self._run(["worktree", "add", "--detach", str(self.worker_worktree), checkpoint])
        except BaseException:
            self._run(["update-ref", "-d", self.checkpoint_ref, checkpoint], check=False)
            raise
        self.current_commit = checkpoint
        self.assert_round_start()
        return self.initialization

    def retained_checkpoint(self, *, namespace_violations: tuple[str, ...] = ()) -> RoundCheckpoint:
        """Describe the current immutable candidate without creating a Worker turn."""
        initialization = self._require_initialized()
        self.assert_round_start()
        checkpoint = self.current_commit
        ancestry = self._output(["rev-list", "--parents", "-n", "1", checkpoint]).split()
        if len(ancestry) != 2 or ancestry[0] != checkpoint:
            raise PactGitError("retained checkpoint must have exactly one parent")
        parent_commit = ancestry[1]
        return RoundCheckpoint(
            round_number=1, parent_commit=parent_commit,
            commit=checkpoint, tree=self._output(["rev-parse", f"{checkpoint}^{{tree}}"]),
            status=self._status(self.worker_worktree),
            changed_paths=self._changed_paths(parent_commit, checkpoint),
            namespace_violations=list(namespace_violations),
            round_diff=self._diff(parent_commit, checkpoint),
            cumulative_diff=self._diff(initialization.baseline_commit, checkpoint),
        )

    def assert_control_ownership(self, checkpoint: str) -> None:
        """Fail closed when a role changed source HEAD/branch or the private ref."""
        initialization = self._require_initialized()
        if self._ref_commit() != checkpoint:
            raise PactGitError("PACT checkpoint ref changed outside the controller")
        if (
            self._output(["rev-parse", "HEAD"]) != initialization.head_commit
            or self._branch(self.workspace) != initialization.branch
        ):
            raise PactGitError("source workspace HEAD or branch changed during PACT")

    def checkpoint_worker(self, *, round_number: int) -> RoundCheckpoint:
        """Create a controller-owned worker checkpoint from the final filesystem."""
        initialization = self._require_initialized()
        parent_commit = self.current_commit
        self.assert_control_ownership(parent_commit)

        status = self._status(self.worker_worktree)
        namespace_violations = self._namespace_violations(self.worker_worktree)
        tree = self._workspace_tree(self.worker_worktree, parent_commit)
        commit = self._commit_tree(tree, parent_commit, f"PACT {self.loop_id} round {round_number:02d} checkpoint")
        round_diff = self._diff(parent_commit, commit)
        cumulative_diff = self._diff(initialization.baseline_commit, commit)
        changed_paths = self._changed_paths(parent_commit, commit)
        self._run(["reset", "--hard", commit], cwd=self.worker_worktree)
        self._run(["clean", "-ffdx", "--", "."], cwd=self.worker_worktree)
        self._run(["update-ref", self.checkpoint_ref, commit, parent_commit])
        self.current_commit = commit
        return RoundCheckpoint(
            round_number=round_number,
            parent_commit=parent_commit,
            commit=commit,
            tree=tree,
            status=status,
            changed_paths=changed_paths,
            namespace_violations=namespace_violations,
            round_diff=round_diff,
            cumulative_diff=cumulative_diff,
        )

    @contextmanager
    def reviewer_worktree(self, *, round_number: int, checkpoint: str) -> Iterator[Path]:
        """Yield a disposable detached reviewer worktree at a durable checkpoint."""
        reviewer = self.loop_root / f"reviewer-round-{round_number:02d}"
        if reviewer.exists():
            raise PactGitError(f"PACT reviewer worktree already exists: {reviewer}")
        self._run(["worktree", "add", "--detach", str(reviewer), checkpoint])
        try:
            yield reviewer
        finally:
            self._run(["worktree", "remove", "--force", str(reviewer)], check=False)

    @contextmanager
    def verification_worktree(self, *, round_number: int, checkpoint: str) -> Iterator[Path]:
        """Yield a disposable detached checkout for deterministic verification."""
        verification = self.loop_root / f"verification-round-{round_number:02d}"
        if verification.exists():
            raise PactGitError(f"PACT verification worktree already exists: {verification}")
        self._run(["worktree", "add", "--detach", str(verification), checkpoint])
        try:
            yield verification
        finally:
            self._run(["worktree", "remove", "--force", str(verification)], check=False)

    @contextmanager
    def final_validation_worktree(self, *, checkpoint: str) -> Iterator[Path]:
        """Yield the one disposable checkout used for terminal validation."""
        validation = self.loop_root / "final-validation"
        if validation.exists():
            raise PactGitError(f"PACT final validation worktree already exists: {validation}")
        self._run(["worktree", "add", "--detach", str(validation), checkpoint])
        try:
            yield validation
        finally:
            self._run(["worktree", "remove", "--force", str(validation)], check=False)

    @contextmanager
    def reviewer_scratch(self, *, round_number: int) -> Iterator[Path]:
        """Yield an empty, checkout-external scratch directory for a reviewer."""
        scratch = self.loop_root / f"reviewer-scratch-round-{round_number:02d}"
        if scratch.exists():
            raise PactGitError(f"PACT reviewer scratch already exists: {scratch}")
        scratch.mkdir(parents=True)
        try:
            yield scratch
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def capture_reviewer_mutation(self, reviewer: Path, checkpoint: str) -> ReviewerMutation:
        """Capture net reviewer changes without trusting reviewer Git state."""
        expected_tree = self._output(["rev-parse", f"{checkpoint}^{{tree}}"])
        snapshot = self._snapshot(reviewer, checkpoint)
        namespace_violations = self._namespace_violations(reviewer)
        changed = (
            snapshot.head_commit != checkpoint
            or snapshot.index_tree != expected_tree
            or snapshot.workspace_tree != expected_tree
            or bool(namespace_violations)
        )
        return ReviewerMutation(
            changed=changed,
            head_commit=snapshot.head_commit,
            index_tree=snapshot.index_tree,
            workspace_tree=snapshot.workspace_tree,
            status=snapshot.status,
            diff=self._diff(checkpoint, snapshot.workspace_tree),
            namespace_violations=namespace_violations,
        )

    def export_patch(self, checkpoint: str, patch_path: Path) -> None:
        """Write the C0..checkpoint patch for humans without applying it."""
        initialization = self._require_initialized()
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(patch_path, self._diff(initialization.baseline_commit, checkpoint))

    def promote(self, checkpoint: str, patch_path: Path) -> PromotionResult:
        """Apply C0..checkpoint to the untouched source working tree."""
        initialization = self._require_initialized()
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch = self._diff(initialization.baseline_commit, checkpoint)
        atomic_write_text(patch_path, patch)

        current_head = self._output(["rev-parse", "HEAD"])
        current_branch = self._branch(self.workspace)
        if current_head != initialization.head_commit or current_branch != initialization.branch:
            return PromotionResult("blocked", checkpoint, patch_path, "source workspace HEAD or branch changed")
        current_index = self._index_tree(self.workspace)
        if current_index != initialization.index_tree:
            return PromotionResult("blocked", checkpoint, patch_path, "source workspace index changed")
        current_tree = self._workspace_tree(self.workspace, initialization.head_commit)
        if current_tree != initialization.workspace_tree:
            return PromotionResult("blocked", checkpoint, patch_path, "source workspace tree changed")
        if self._ref_commit() != checkpoint:
            return PromotionResult("blocked", checkpoint, patch_path, "PACT checkpoint ref changed")
        if not patch:
            return PromotionResult("no_changes", checkpoint, patch_path)

        # Deliver the accepted bytes; host formatting/fuzzy-context policies
        # cannot rewrite them or weaken either applicability check.
        apply_args = ["-c", "apply.ignoreWhitespace=no", "apply", "--whitespace=nowarn", "--binary"]
        check = self._run([*apply_args, "--check", str(patch_path)], check=False)
        if check.returncode != 0:
            reason = check.stderr.strip() or check.stdout.strip() or "git apply --check failed"
            return PromotionResult("blocked", checkpoint, patch_path, reason)
        applied = self._run([*apply_args, str(patch_path)], check=False)
        if applied.returncode != 0:
            reason = applied.stderr.strip() or applied.stdout.strip() or "git apply failed"
            return PromotionResult("failed", checkpoint, patch_path, reason)
        return PromotionResult("applied", checkpoint, patch_path)

    def cleanup(self) -> None:
        """Remove role worktrees while retaining the internal checkpoint ref."""
        if self.worker_worktree.exists():
            self._run(["worktree", "remove", "--force", str(self.worker_worktree)], check=False)
