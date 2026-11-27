"""Standalone stdlib deterministic runner for a sealed collection of checks.

Executed with Python -I. No agent, prompt or model dependency belongs here.
Children inherit the verifier process group so its existing timeout kills all of
them. Each completed item is persisted before the next command starts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


def main() -> int:
    sealed = Path(sys.argv[1]).read_bytes()
    if hashlib.sha256(sealed).hexdigest() != sys.argv[2]:
        print('check_manifest_changed', file=sys.stderr)
        return 1
    manifest = json.loads(sealed)
    destination = Path(os.environ['PACT_LOOP_DIR']) / 'check-results.json'
    results: list[dict[str, Any]] = []
    failed = False
    for index, item in enumerate(manifest['checks'], start=1):
        error = ''
        for filename, expected in item.get('files', {}).items():
            path = Path(filename)
            if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(Path.cwd())
                    or hashlib.sha256(path.read_bytes()).hexdigest() != expected):
                error = f'definition_changed: {filename}; register the revised check with its reason'
                break
        deadline = manifest.get('deadline')
        if deadline is not None and time.time() >= deadline:
            error = 'budget_exhausted'
        started = time.monotonic()
        if error:
            status, code, stdout, stderr = 'failed', None, '', error
        else:
            process = subprocess.run(item['command'], shell=True, stdin=subprocess.DEVNULL,
                                     capture_output=True, text=True)
            code, stdout, stderr = process.returncode, process.stdout, process.stderr
            status = 'passed' if code == 0 else 'failed'
        # Keep original completed output independently of bounded prompt tails.
        for stream, output in (('stdout', stdout), ('stderr', stderr)):
            (destination.parent / f'check-{index:03d}-{stream}.log').write_text(output, encoding='utf-8')
        failed |= status != 'passed'
        results.append({'name': item['name'], 'source': item['source'], 'command': item['command'],
            'fingerprint': item.get('fingerprint'), 'status': status, 'exit_code': code,
            'stdout_tail': stdout[-12000:], 'stderr_tail': stderr[-12000:],
            'duration_seconds': time.monotonic() - started})
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(json.dumps({'schema': 'pact-runtime/check-results/v1',
            'candidate': manifest['candidate'], 'checks_fingerprint': manifest['checks_fingerprint'],
            'results': results}, ensure_ascii=False))
        os.replace(temporary, destination)
        print(f"[{item['name']}] {status}", flush=True)
        print(stdout[-12000:], end='', flush=True)
        print(stderr[-12000:], end='', file=sys.stderr, flush=True)
        if error == 'budget_exhausted':
            break
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
