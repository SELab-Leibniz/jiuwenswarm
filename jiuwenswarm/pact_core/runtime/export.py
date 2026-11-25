"""Seal complete candidate files for an external evaluator, without delivery."""
from __future__ import annotations

import errno
import hashlib
import os
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterator, cast

from ..artifacts import read_json, write_json_atomic
from .._runtime_serialization import decode
from ..kernel import ExecutionOutcome, WorkCandidate, default_worktree_root, read_work_candidate
from .boundary import assert_source_basis, campaign_lock, executor_lock
from .schemas import RuntimeContractError, RuntimeStateError
from .store import CampaignReader, digest
from .types import CandidateRef

if TYPE_CHECKING:
    from .control_plane import WorkRuntime

EXPORT_SCHEMA = 'pact-runtime/candidate-export/v3'


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    artifact: Path
    artifact_sha256: str
    work_id: str
    candidate: CandidateRef
    source_workspace: Path
    source_basis: dict[str, object]
    accepted: bool
    snapshot: Path
    patch: Path
    artifact_hashes: dict[str, str]
    protected_roots: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CandidateExportDiagnostic:
    """One canonical candidate skipped because its content cannot be exported."""
    candidate: CandidateRef
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class LatestCandidateExport:
    """The selected export, or no receipt when every candidate is ineligible."""
    receipt: ExportReceipt | None
    diagnostics: tuple[CandidateExportDiagnostic, ...]


class _CandidateNotExportable(RuntimeStateError):
    """Only explicit candidate-content limitations may trigger history fallback."""
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def safe_relative(value: str) -> PurePosixPath:
    """Reject escape paths and repository/control namespaces on every platform."""
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or str(path) != value
            or any(part in ('', '.', '..') or part.casefold() in ('.git', '.pact', '.pact-io')
                   or '\\' in part or ':' in part for part in path.parts)):
        raise RuntimeStateError('export path must be a safe relative candidate path')
    return path


def output_directory(destination: Path, protected: tuple[Path, ...]) -> Path:
    path = destination.expanduser().absolute()
    if path != path.resolve() or path.exists():
        raise RuntimeStateError('output must be a new exact directory, without symlink parents')
    for root in protected:
        root = root.resolve()
        if path.is_relative_to(root) or root.is_relative_to(path):
            raise RuntimeStateError('output overlaps protected source or evidence')
    if any(p.casefold() in ('.git', '.pact', '.pact-io') for p in path.parts):
        raise RuntimeStateError('output cannot use a control namespace')
    return path


def _git(workspace: Path, *args: str) -> bytes:
    # Export must not honor caller Git redirects or invoke checkout filters.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    result = subprocess.run(['git', *args], cwd=workspace, env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeStateError('cannot read sealed checkpoint Git objects')
    return result.stdout


def _snapshot(workspace: Path, checkpoint: str, destination: Path, baseline: str
              ) -> tuple[dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    hashes: dict[str, str] = {}
    gitlinks: dict[str, str] = {}
    opaque_symlinks: dict[str, dict[str, str]] = {}
    baseline_links: dict[bytes, str] = {}
    for row in _git(workspace, 'ls-tree', '-rz', '--full-tree', baseline).split(b'\0'):
        if row:
            header, baseline_name = row.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split()
            if mode == '120000' and kind == 'blob':
                baseline_links[baseline_name] = oid
    rows = _git(workspace, 'ls-tree', '-rz', '--full-tree', checkpoint).split(b'\0')
    source_links: dict[str, str] = {}
    def link_key(name: str) -> str:
        return unicodedata.normalize('NFC', name).casefold()

    def absolute(link: str) -> bool:
        # A C:/... fixture is a relative literal on POSIX. Preserve its
        # platform semantics; POSIX-rooted links also stay absolute on Windows.
        return PurePosixPath(link).is_absolute() or bool(Path(link).anchor)

    for row in rows:
        if row:
            header, link_name = row.split(b'\t', 1)
            mode, kind, oid = header.decode('ascii').split()
            if mode == '120000' and kind == 'blob':
                try:
                    key = link_key(link_name.decode('utf-8'))
                    if key in source_links:
                        raise _CandidateNotExportable('ambiguous_symlink', 'candidate symlink paths are ambiguous')
                    source_links[key] = _git(workspace, 'cat-file', 'blob', oid).decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise _CandidateNotExportable('unsupported_encoding', 'candidate symlink is not UTF-8') from exc

    def escapes(name: str, link: str) -> bool:
        # Resolve components against the Git symlink graph, not the sparse
        # export filesystem. In particular fixture/.. must expand fixture
        # BEFORE '..'; missing opaque paths cannot hide a chain escape.
        stack = list(safe_relative(name).parts[:-1])
        pending = list(PurePosixPath(link).parts)
        if absolute(link):
            return True
        expansions = 0
        while pending:
            part = pending.pop(0)
            if part == '..':
                if not stack:
                    return True
                stack.pop()
            elif part != '.':
                stack.append(part)
                chained = source_links.get(link_key('/'.join(stack)))
                if chained is not None:
                    expansions += 1
                    if expansions > 40:
                        raise _CandidateNotExportable('symlink_cycle', 'candidate symlink cycle is unsupported')
                    if absolute(chained):
                        return True
                    stack.pop()
                    pending = list(PurePosixPath(chained).parts) + pending
        return False
    destination.mkdir()
    links: list[tuple[Path, str]] = []
    for row in rows:
        if not row:
            continue
        header, raw_name = row.split(b'\t', 1)
        mode, kind, oid = header.decode('ascii').split()
        try:
            name = raw_name.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise _CandidateNotExportable('unsupported_encoding', 'candidate path is not UTF-8') from exc
        try:
            relative = safe_relative(name)
        except RuntimeStateError as exc:
            raise _CandidateNotExportable('unsupported_path', str(exc)) from exc
        if kind == 'commit' and mode == '160000':
            # A Git tree records a submodule commit, not its working files.
            # Preserve that opaque identity without fetching or following it.
            destination.joinpath(*relative.parts).mkdir(parents=True)
            gitlinks[name] = oid
            continue
        if kind != 'blob' or mode not in ('100644', '100755', '120000'):
            raise _CandidateNotExportable('unsupported_file_type',
                'export requires regular files, internal symlinks or Git links')
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = _git(workspace, 'cat-file', 'blob', oid)
        if mode == '120000':
            try:
                link = data.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise _CandidateNotExportable('unsupported_encoding', 'candidate symlink target is not UTF-8') from exc
            if escapes(name, link):
                if baseline_links.get(raw_name) != oid:
                    raise _CandidateNotExportable('external_symlink', 'candidate symlink escapes exported snapshot')
                # Original fixture links are data, never executable filesystem
                # links in an evaluator's sealed tree. Preserve exact blob bytes
                # and identity; newly introduced or changed escapes still fail.
                opaque_symlinks[name] = {'target': link, 'blob_oid': oid}
                continue
            links.append((target, link))
        else:
            target.write_bytes(data)
            target.chmod(0o755 if mode == '100755' else 0o644)
        hashes[f'snapshot/{name}'] = hashlib.sha256(data).hexdigest()
    # Create links last, so no file writes can traverse a candidate symlink.
    for target, link in links:
        target.symlink_to(link)
    for target, _ in links:
        try:
            try:
                # Python >=3.13 tolerates cycles in non-strict resolve().
                resolved = target.resolve(strict=True)
            except (FileNotFoundError, NotADirectoryError):
                # An internal dangling link is still valid candidate content.
                resolved = target.resolve()
        except RuntimeError as exc:
            raise _CandidateNotExportable('symlink_cycle', 'candidate symlink cycle is unsupported') from exc
        except OSError as exc:
            if exc.errno != errno.ELOOP:
                raise
            raise _CandidateNotExportable('symlink_cycle', 'candidate symlink cycle is unsupported') from exc
        if not resolved.is_relative_to(destination.resolve()):
            raise _CandidateNotExportable('external_symlink', 'candidate symlink chain escapes snapshot')
    return hashes, gitlinks, opaque_symlinks


def export_candidate(runtime: WorkRuntime, candidate: CandidateRef, destination: Path) -> ExportReceipt:
    """Export a canonical historical/current candidate; acceptance is not required."""
    reader = CampaignReader(runtime.workspace, candidate.work_id)
    protected = (runtime.workspace, runtime.worktree_root or default_worktree_root())
    output = output_directory(destination, protected)
    with executor_lock(reader), campaign_lock(reader):
        state = reader.read_current_state()
        for ref in cast(list[str], state['evidence_refs']):
            raw = read_json(reader.resolve_ref(ref))
            value = raw.get('candidate')
            if isinstance(value, dict) and value.get('artifact') == str(candidate.artifact):
                record = reader.read_record(ref)
                break
        else:
            raise RuntimeStateError('candidate is not recorded by this work')
        return _export_locked(runtime, reader, state, candidate, ref, record, output, protected)


def _candidate_history(reader: CampaignReader, state: dict[str, object]) -> Iterator[tuple[CandidateRef, str, dict[str, object]]]:
    history = cast(list[str], state['evidence_refs'])
    current_ref = state['candidate_ref']
    if not isinstance(current_ref, str) or current_ref not in history:
        raise RuntimeStateError('current candidate is not recorded in work history')
    seen: dict[Path, WorkCandidate] = {}
    for ref in [current_ref, *reversed(history)]:
        record = reader.read_record(ref)
        if record.get('kind') != 'candidate':
            if ref == current_ref:
                raise RuntimeStateError('current candidate record has the wrong kind')
            continue
        candidate = decode(WorkCandidate, record.get('candidate'))
        if candidate.source_workspace != reader.workspace:
            raise RuntimeStateError('candidate source differs from work')
        if candidate.artifact in seen:
            if seen[candidate.artifact] != candidate:
                raise RuntimeStateError('candidate history contains conflicting identities')
            continue
        seen[candidate.artifact] = candidate
        yield CandidateRef(reader.campaign_id, candidate.artifact, candidate.checkpoint, candidate.tree), ref, record


def export_latest_candidate(runtime: WorkRuntime, work_id: str, destination: Path, *,
                            expected_revision: int | None = None) -> LatestCandidateExport:
    """Export current, then newest eligible history, to one exact fresh directory.

    Selection and export share the work ownership lock. Only explicit candidate
    content limitations produce diagnostics; source, canonical, receipt,
    destination and I/O faults raise. No receipt means every retained candidate
    was ineligible, and the destination is absent. Export never delivers work.
    """
    if expected_revision is not None and (isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int) or expected_revision < 0):
        raise RuntimeContractError('expected_revision must be a nonnegative integer')
    reader = CampaignReader(runtime.workspace, work_id)
    protected = (runtime.workspace, runtime.worktree_root or default_worktree_root())
    with executor_lock(reader), campaign_lock(reader):
        state = (reader.read_current_state() if expected_revision is None
                 else reader.assert_expected_revision(expected_revision))
        output = output_directory(destination, protected)
        # Validate current authority before considering any historical fallback.
        runtime.read_work(work_id)
        assert_source_basis(reader, state)
        diagnostics: list[CandidateExportDiagnostic] = []
        for candidate, ref, record in _candidate_history(reader, state):
            try:
                receipt = _export_locked(runtime, reader, state, candidate, ref, record, output, protected)
            except _CandidateNotExportable as exc:
                diagnostics.append(CandidateExportDiagnostic(candidate, exc.code, str(exc)))
            else:
                return LatestCandidateExport(receipt, tuple(diagnostics))
        if not diagnostics:
            raise RuntimeStateError('work has no canonical candidate to export')
        return LatestCandidateExport(None, tuple(diagnostics))


def _export_locked(runtime: WorkRuntime, reader: CampaignReader, state: dict[str, object],
                   candidate: CandidateRef, record_ref: str, record: dict[str, object], output: Path,
                   protected: tuple[Path, ...]) -> ExportReceipt:
    """Shared sealed export; the caller already holds both ownership locks."""
    bound = decode(WorkCandidate, record.get('candidate'))
    if (record.get('kind') != 'candidate'
            or CandidateRef(candidate.work_id, bound.artifact, bound.checkpoint, bound.tree) != candidate):
        raise RuntimeStateError('candidate does not match its canonical record')
    accepted = False
    promotion_outcome: ExecutionOutcome | None = None
    if state['acceptance_ref'] is not None:
        current = reader.read_record(state['candidate_ref'])
        if current.get('candidate') == record.get('candidate'):
            snapshot = runtime.read_work(candidate.work_id)
            accepted = snapshot.acceptance is not None and snapshot.acceptance.candidate == candidate
            if state['status'] == 'completed' and accepted:
                delivery = reader.read_record(state['delivery_ref'])
                if delivery.get('acceptance_ref') != state['acceptance_ref'] or snapshot.delivery is None:
                    raise RuntimeStateError('delivered candidate lacks an exact acceptance binding')
                acceptance_record = reader.read_record(state['acceptance_ref'])
                if acceptance_record.get('kind') != 'accept' or acceptance_record.get('complete') is not True:
                    raise RuntimeStateError('delivered export is not bound to full acceptance')
                promotion_outcome = decode(ExecutionOutcome, acceptance_record['outcome'])
    sealed = read_work_candidate(candidate.artifact, promotion_outcome=promotion_outcome)
    if sealed != bound or sealed.source_workspace != runtime.workspace:
        raise RuntimeStateError('candidate source or canonical identity is invalid')

    def validate_unchanged() -> None:
        if reader.read_current_state() != state or reader.read_record(record_ref) != record:
            raise RuntimeStateError('canonical work changed during export')
        if read_work_candidate(candidate.artifact, promotion_outcome=promotion_outcome) != sealed:
            raise RuntimeStateError('candidate changed during export')
        assert_source_basis(reader, state)

    stage: Path | None = None
    try:
        try:
            if sealed.namespace_violations:
                raise _CandidateNotExportable('namespace_violation', 'candidate contains a reserved namespace violation')
            output.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix='.pact-export-', dir=output.parent))
            initialization = read_json(sealed.initialization)
            baseline = initialization.get('baseline_commit')
            if (not isinstance(baseline, str) or len(baseline) not in (40, 64)
                    or any(c not in '0123456789abcdef' for c in baseline)):
                raise RuntimeStateError('export baseline identity is missing')
            hashes, gitlinks, opaque_symlinks = _snapshot(
                runtime.workspace, sealed.checkpoint, stage / 'snapshot', baseline)
        except _CandidateNotExportable:
            # Every content limitation, including namespaces, must preserve the
            # same canonical/source/evidence checks as successful publication.
            validate_unchanged()
            raise
        (stage / 'candidate.patch').write_bytes(sealed.patch.read_bytes())
        hashes['candidate.patch'] = digest(stage / 'candidate.patch')
        initialization = read_json(sealed.initialization)
        payload: dict[str, object] = {
            'schema': EXPORT_SCHEMA, 'work_id': candidate.work_id,
            'candidate_artifact': str(candidate.artifact), 'candidate_sha256': digest(candidate.artifact),
            'checkpoint': candidate.checkpoint, 'tree': candidate.tree,
            'source_workspace': str(runtime.workspace), 'source_basis': sealed.source_basis,
            'protected_roots': [str(path) for path in protected],
            'baseline_commit': initialization.get('baseline_commit'),
            'accepted': accepted, 'delivery_authorized': False,
            'artifact_hashes': hashes, 'gitlinks': gitlinks, 'opaque_symlinks': opaque_symlinks,
            'artifact_modes': {name: (stage / name).lstat().st_mode & 0o777 for name in hashes},
        }
        write_json_atomic(stage / 'export.json', payload)
        # Catch concurrent source/evidence changes before publishing the bundle.
        validate_unchanged()
        output_directory(output, protected)
        stage.rename(output)
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage)
        raise
    return ExportReceipt(output / 'export.json', digest(output / 'export.json'), candidate.work_id, candidate,
                         runtime.workspace, sealed.source_basis, accepted, output / 'snapshot',
                         output / 'candidate.patch', hashes, protected)


def verify_export(receipt: ExportReceipt) -> dict[str, object]:
    """Validate the sealed bundle without reading its originating work/source."""
    root = receipt.artifact.parent
    if (receipt.artifact != root / 'export.json' or receipt.artifact != receipt.artifact.resolve()
            or digest(receipt.artifact) != receipt.artifact_sha256):
        raise RuntimeStateError('export receipt changed')
    payload = read_json(receipt.artifact)
    if (payload.get('schema') != EXPORT_SCHEMA or payload.get('work_id') != receipt.work_id
            or payload.get('checkpoint') != receipt.candidate.checkpoint
            or payload.get('tree') != receipt.candidate.tree
            or payload.get('source_workspace') != str(receipt.source_workspace)
            or payload.get('source_basis') != receipt.source_basis
            or payload.get('protected_roots') != [str(path) for path in receipt.protected_roots]
            or payload.get('accepted') is not receipt.accepted
            or payload.get('artifact_hashes') != receipt.artifact_hashes
            or receipt.snapshot != root / 'snapshot' or receipt.patch != root / 'candidate.patch'):
        raise RuntimeStateError('export identity does not match receipt')
    gitlinks = payload.get('gitlinks')
    if not isinstance(gitlinks, dict):
        raise RuntimeStateError('export Git links changed')
    for name, oid in gitlinks.items():
        if not isinstance(name, str) or not isinstance(oid, str):
            raise RuntimeStateError('export Git link identity changed')
        relative = safe_relative(name)
        if len(oid) not in (40, 64) or any(c not in '0123456789abcdef' for c in oid):
            raise RuntimeStateError('export Git link commit is invalid')
        path = receipt.snapshot.joinpath(*relative.parts)
        if path != path.resolve() or not path.is_dir() or any(path.iterdir()):
            raise RuntimeStateError('export Git link must remain an empty opaque directory')
    opaque_symlinks = payload.get('opaque_symlinks')
    if not isinstance(opaque_symlinks, dict):
        raise RuntimeStateError('export opaque symlink metadata changed')
    for name, item in opaque_symlinks.items():
        if not isinstance(name, str) or not isinstance(item, dict) or set(item) != {'target', 'blob_oid'}:
            raise RuntimeStateError('export opaque symlink identity changed')
        relative = safe_relative(name)
        path = receipt.snapshot.joinpath(*relative.parts)
        if path != path.resolve() or path.exists() or path.is_symlink():
            raise RuntimeStateError('export opaque symlink must remain metadata only')
        target, oid = item['target'], item['blob_oid']
        if not isinstance(target, str) or not isinstance(oid, str) or len(oid) not in (40, 64):
            raise RuntimeStateError('export opaque symlink blob identity changed')
        data = target.encode('utf-8')
        blob = b'blob ' + str(len(data)).encode('ascii') + b'\0' + data
        observed_oid = (hashlib.sha1(blob) if len(oid) == 40 else hashlib.sha256(blob)).hexdigest()
        if observed_oid != oid:
            raise RuntimeStateError('export opaque symlink blob hash mismatch')
    observed: dict[str, str] = {}
    modes = payload.get('artifact_modes')
    if not isinstance(modes, dict) or set(modes) != set(receipt.artifact_hashes):
        raise RuntimeStateError('export artifact modes changed')
    for name, expected in receipt.artifact_hashes.items():
        relative = safe_relative(name)
        path = root.joinpath(*relative.parts)
        if path.is_symlink():
            if not path.resolve().is_relative_to(receipt.snapshot.resolve()):
                raise RuntimeStateError('export symlink escapes snapshot')
            actual = hashlib.sha256(os.readlink(path).encode()).hexdigest()
        else:
            if path != path.resolve() or not path.is_file():
                raise RuntimeStateError('export artifact path changed')
            actual = digest(path)
        if actual != expected:
            raise RuntimeStateError('export artifact hash mismatch')
        if path.lstat().st_mode & 0o777 != modes[name]:
            raise RuntimeStateError('export artifact mode changed')
        observed[name] = actual
    actual_paths = {str(p.relative_to(root)) for p in receipt.snapshot.rglob('*') if p.is_file() or p.is_symlink()}
    if actual_paths != {name for name in observed if name.startswith('snapshot/')} or 'candidate.patch' not in observed:
        raise RuntimeStateError('export snapshot contains undeclared files')
    return payload
