"""Make controller-owned Git evidence readable to a file-only Reviewer.

The pinned pact-core Reviewer prompt carries three controller checkpoint IDs
before any task content. No shell or outside-workspace read permission is added
by this transport. The temporary candidate files are removed before PACT checks
for Reviewer mutations; byte-identical copies remain in the role artifacts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SHA = r'[0-9a-f]{40}(?:[0-9a-f]{24})?'
_HEADER = re.compile(
    r'\A# PACT Reviewer Round [0-9]+\n\n'
    r'This is a fresh session in a disposable checkout at `(?P<output>' + _SHA + r')`\.\n'
    r'Treat this checkout as the final candidate\. Inspect the actual files, not\n'
    r'just the worker summary: it is an untrusted claim, not evidence\. Use\n'
    r'`git diff (?P<input>' + _SHA + r')\.\.(?P=output)`\n'
    r"for this round's delta and `git diff (?P<baseline>" + _SHA + r')\.\.(?P=output)`\n'
)


@dataclass(frozen=True)
class ReviewContext:
    directory: Path
    created_parent: bool
    prompt: str
    identity: tuple[int, int]


def _git(workspace: Path, *arguments: str) -> bytes:
    git = shutil.which('git')
    if git is None:
        raise RuntimeError('Reviewer checkpoint evidence requires Git')
    # Resolve this checkout, never an inherited alternate GIT_DIR/worktree.
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment['GIT_OPTIONAL_LOCKS'] = '0'
    return subprocess.run(
        [git, '-C', str(workspace), '--no-pager', *arguments],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
        env=environment, timeout=60,
    ).stdout


def prepare_review_context(workspace: Path, prompt: str, artifact_dir: Path) -> ReviewContext | None:
    """Capture real round/cumulative diffs from the pinned machine-generated header."""
    if not prompt.startswith(('# PACT Reviewer Round ', '# PACT Work Review\n')):
        # PACT's later decision format-repair turn explicitly forbids reopening
        # review and contains its own inline artifacts, not checkpoint IDs.
        return None
    # Everything after <plan> may contain untrusted task/model text. Never
    # search those sections for a convenient hash or Git command.
    header = prompt.split('<plan>', 1)[0]
    if prompt.startswith('# PACT Work Review\n'):
        # The current Core header supplies whole-work evidence only. There is
        # no claim that an invented per-round baseline is the previous Worker.
        pattern = (r"\A# PACT Work Review\n\n[^\n]+\nInspect the actual checkout at `(?P<output>" + _SHA
            + r")` and the cumulative changes\nwith `git diff (?P<baseline>" + _SHA + r")\.\.(?P=output)`\.")
        current = re.match(pattern, prompt)
        match = ({**current.groupdict(), 'input': current['baseline']} if current else None)
    else:
        match = _HEADER.match(header)
    if match is None:
        raise ValueError('Pinned Reviewer checkpoint header is malformed')
    workspace = workspace.resolve(strict=True)
    output = match['output']
    if _git(workspace, 'rev-parse', '--verify', 'HEAD').decode().strip() != output:
        raise ValueError('Reviewer checkout HEAD differs from the controller output checkpoint')
    for checkpoint in {match['input'], match['baseline'], output}:
        if _git(workspace, 'cat-file', '-t', checkpoint).strip() != b'commit':
            raise ValueError('Reviewer evidence checkpoint is not a Git commit')
    artifacts = artifact_dir / 'read-only-review-evidence'
    artifacts.mkdir(parents=True, exist_ok=False)
    records = {}
    whole_work = prompt.startswith('# PACT Work Review\n')
    diffs = [('cumulative.diff', match['baseline'])]
    if not whole_work:
        diffs.insert(0, ('round.diff', match['input']))
    for name, base in diffs:
        data = _git(workspace, 'diff', '--no-ext-diff', '--no-textconv', '--binary',
                    '--full-index', f'{base}..{output}', '--')
        (artifacts / name).write_bytes(data)
        records[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    manifest = {
        'schema': 'jiuwenswarm/pact-read-only-review-evidence/v1',
        'input_checkpoint': None if whole_work else match['input'], 'baseline_checkpoint': match['baseline'],
        'scope': 'whole_work' if whole_work else 'round',
        'output_checkpoint': output, 'files': records,
    }
    (artifacts / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    parent = workspace / '.pact-io'
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValueError('Reviewer evidence parent must be a real directory inside the candidate')
    created_parent = not parent.exists()
    parent.mkdir(mode=0o700, exist_ok=True)
    if parent.resolve() != workspace / '.pact-io':
        raise ValueError('Reviewer evidence parent escaped the assigned candidate')
    directory = Path(tempfile.mkdtemp(prefix='jiuwen-readonly-review-', dir=parent))
    info = directory.stat()
    identity = (info.st_dev, info.st_ino)
    try:
        for name in [*records, 'manifest.json']:
            source = artifacts / name
            destination = directory / name
            destination.write_bytes(source.read_bytes())
            destination.chmod(0o400)
        relative = directory.relative_to(workspace).as_posix()
        round_briefing = '' if whole_work else f'- Round delta: `{relative}/round.diff`\n'
        briefing = (
            '\n\n## Controller-captured review evidence for file-only tools\n\n'
            'This Reviewer has read-only file tools. It cannot execute the Git or test commands '
            'mentioned by the generic PACT prompt, or write to the external scratch directory. '
            'Use read_file on the following files inside the assigned candidate checkout. The host '
            'captured these exact Git diffs after checking HEAD against the output checkpoint:\n'
            f'{round_briefing}'
            f'- Cumulative delta: `{relative}/cumulative.diff`\n'
            f'- Checkpoint IDs and byte hashes: `{relative}/manifest.json`\n\n'
            'An empty diff is valid evidence of no changes between those commits. Inspect candidate '
            'source files as needed. Plan, goal tracker, original requirement and public verification '
            'receipts are already inline in this prompt; do not try to read their external artifact '
            'paths. Distinguish host-executed public verification from independent checks; no independent '
            'commands can be run with this role tool set. These temporary files are host-owned and '
            'are removed after your turn before the Reviewer mutation gate runs.'
        )
        return ReviewContext(directory=directory, created_parent=created_parent, prompt=briefing, identity=identity)
    except BaseException:
        cleanup_review_context(ReviewContext(directory, created_parent, '', identity))
        raise


def cleanup_review_context(context: ReviewContext | None) -> None:
    """Remove only the unique directory created by this role invocation."""
    if context is None:
        return
    directory = context.directory
    if directory.parent.is_symlink():
        raise ValueError('Refusing Reviewer evidence cleanup through a replaced parent symlink')
    if directory.is_symlink():
        directory.unlink()
    elif directory.exists():
        info = directory.stat()
        if (info.st_dev, info.st_ino) != context.identity:
            raise ValueError('Refusing to remove a replaced Reviewer evidence directory')
        shutil.rmtree(directory)
    if context.created_parent:
        try:
            directory.parent.rmdir()
        except OSError:
            # Keep any pre-existing or independently-created sibling content.
            pass
