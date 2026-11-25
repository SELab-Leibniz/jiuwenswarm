"""Local evaluator mirrors; no benchmark service, container or model invocation."""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..artifacts import write_json_atomic
from ..runtime.export import ExportReceipt, output_directory, safe_relative, verify_export
from ..runtime.schemas import RuntimeStateError
from ..runtime.store import digest

BRIDGE_SCHEMA = 'pact-benchmark/local-bridge/v1'


@dataclass(frozen=True, slots=True)
class ArtifactMapping:
    candidate_path: str
    output_path: str
    logical_path: str | None = None


@dataclass(frozen=True, slots=True)
class BridgeReceipt:
    artifact: Path
    workspace: Path
    patch: Path
    benchmark: str
    export: ExportReceipt


def publish_candidate(
    receipt: ExportReceipt, destination: Path, *, benchmark: str = 'generic',
    mappings: tuple[ArtifactMapping, ...] = (), absolute_output_roots: tuple[str, ...] = (),
    windows: tuple[ExportReceipt, ...] = (),
) -> BridgeReceipt:
    """Publish a fresh mirror and explicit candidate-file mappings for local tests.

    DeepSWE gets the complete candidate, Marathon additionally gets declared file
    outputs, and LoopsBench may get ordered window candidates/patches. These are
    PACT bundle conventions, not claims of official harness compatibility.
    Absolute logical outputs are manifest aliases only: they never grant reads or
    writes at an absolute filesystem location.
    """
    if benchmark not in ('generic', 'deepswe', 'swe-marathon', 'loopsbench'):
        raise RuntimeStateError('unknown local bridge convention')
    if windows and benchmark != 'loopsbench':
        raise RuntimeStateError('window exports require the loopsbench convention')
    for item in (receipt, *windows):
        verify_export(item)
        if item.work_id != receipt.work_id or item.source_basis != receipt.source_basis:
            raise RuntimeStateError('window exports must share one work and original source basis')
    protected = tuple(root for item in (receipt, *windows)
                      for root in (*item.protected_roots, item.source_workspace, item.artifact.parent))
    output = output_directory(destination, protected)
    declared_roots: list[PurePosixPath] = []
    for value in absolute_output_roots:
        root = PurePosixPath(value)
        if not root.is_absolute() or root == PurePosixPath('/') or '..' in root.parts or str(root) != value:
            raise RuntimeStateError('absolute output declarations require specific normalized logical roots')
        safe_relative(str(root).lstrip('/'))
        declared_roots.append(root)
    exports: list[tuple[Path, str, str | None]] = []
    targets: set[str] = set()
    for mapping in mappings:
        relative = safe_relative(mapping.candidate_path)
        target = str(safe_relative(mapping.output_path))
        source = receipt.snapshot.joinpath(*relative.parts)
        if (f'snapshot/{relative}' not in receipt.artifact_hashes or source != source.resolve()
                or not source.is_file()):
            raise RuntimeStateError('mapped input must be a declared regular candidate file')
        if any(target == old or target.startswith(old + '/') or old.startswith(target + '/') for old in targets):
            raise RuntimeStateError('output mappings overlap')
        targets.add(target)
        logical = mapping.logical_path
        if logical is not None:
            alias = PurePosixPath(logical)
            if alias.is_absolute():
                if '..' in alias.parts or str(alias) != logical or not any(alias.is_relative_to(root) for root in declared_roots):
                    raise RuntimeStateError('absolute logical output has not been explicitly declared')
                safe_relative(logical.lstrip('/'))
            else:
                safe_relative(logical)
        exports.append((source, target, logical))
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.pact-bridge-', dir=output.parent))
    try:
        shutil.copytree(receipt.snapshot, stage / 'workspace', symlinks=True)
        shutil.copyfile(receipt.patch, stage / 'candidate.patch')
        artifacts: list[dict[str, object]] = []
        for source, target, logical in exports:
            mapped = stage / 'artifacts' / target
            mapped.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, mapped)
            artifacts.append({'candidate_path': str(source.relative_to(receipt.snapshot)),
                              'output_path': f'artifacts/{target}', 'logical_path': logical,
                              'sha256': digest(mapped)})
        window_entries: list[dict[str, object]] = []
        for index, window in enumerate(windows, 1):
            prefix = f'windows/{index:04d}'
            folder = stage / prefix
            folder.mkdir(parents=True)
            shutil.copytree(window.snapshot, folder / 'workspace', symlinks=True)
            shutil.copyfile(window.patch, folder / 'candidate.patch')
            window_entries.append({'index': index, 'checkpoint': window.candidate.checkpoint,
                'tree': window.candidate.tree, 'accepted': window.accepted,
                'workspace': f'{prefix}/workspace', 'patch': f'{prefix}/candidate.patch',
                'patch_sha256': digest(folder / 'candidate.patch'), 'export_sha256': window.artifact_sha256})
        for item in (receipt, *windows):
            verify_export(item)
        write_json_atomic(stage / 'bridge.json', {
            'schema': BRIDGE_SCHEMA, 'benchmark': benchmark, 'official_harness_integration': False,
            'work_id': receipt.work_id, 'checkpoint': receipt.candidate.checkpoint,
            'tree': receipt.candidate.tree, 'accepted': receipt.accepted,
            'export_sha256': receipt.artifact_sha256, 'source_basis': receipt.source_basis,
            'workspace': 'workspace', 'patch': 'candidate.patch',
            'patch_sha256': digest(stage / 'candidate.patch'), 'artifacts': artifacts,
            'absolute_output_roots': list(absolute_output_roots), 'windows': window_entries,
        })
        stage.rename(output)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return BridgeReceipt(output / 'bridge.json', output / 'workspace', output / 'candidate.patch', benchmark, receipt)
