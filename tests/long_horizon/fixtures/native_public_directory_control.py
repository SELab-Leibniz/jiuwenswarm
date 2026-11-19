"""Public-source directory admission control in the real fixed Go image."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

mode = sys.argv[1]
if mode == 'candidate':
    sys.path.insert(0, '/candidate')
from jiuwenswarm.long_horizon.benchmark_write_guard import BenchmarkWriteGuard, public_test_paths

source = Path('/app')
inventory = public_test_paths(source)
assert any(path.startswith('checkers/testdata/') for path in inventory)
assert not (source / 'checkers/testdata/publicGuardNew').exists()
root = Path('/tmp/public-directory-control')
root.mkdir()
workspace = root / 'workspace'
subprocess.run(['git', 'clone', '-q', '--no-hardlinks', str(source), str(workspace)],
               stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=True)
state = root / 'state'
state.mkdir(mode=0o700)
guard = BenchmarkWriteGuard(workspace, state, inventory)
directory = workspace / 'checkers/testdata'
before = (directory.stat().st_uid, directory.stat().st_gid, directory.stat().st_mode)
guard.prepare()
script = '''from pathlib import Path
import json
r=Path.cwd();blocked=[]
for action in (lambda:(r/'checkers/testdata/publicGuardNew').mkdir(),
               lambda:(r/'checkers/testdata/direct_new.go').write_text('new public fixture'),
               lambda:(r/'checkers/testdata').chmod(0o777)):
 try:action()
 except PermissionError:blocked.append(True)
 else:blocked.append(False)
(r/'checks').mkdir()
(r/'checks/new_case_test.go').write_text('new separate regression')
(r/'implementation.go').write_text('new implementation')
print(json.dumps(blocked))
'''
try:
    result = subprocess.run([*guard.command_prefix, sys.executable, '-c', script], cwd=workspace,
                            env=guard.environment(dict(os.environ)), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=30, check=True)
    blocked = json.loads(result.stdout)
finally:
    receipt = guard.finish()
assert receipt['status'] == 'complete', receipt
assert before == (directory.stat().st_uid, directory.stat().st_gid, directory.stat().st_mode)
assert (workspace / 'checks/new_case_test.go').is_file()
assert (workspace / 'implementation.go').is_file()
if mode == 'baseline':
    assert blocked == [False, False, True], blocked
    assert (directory / 'direct_new.go').exists()
else:
    assert blocked == [True, True, True], blocked
    assert not (directory / 'publicGuardNew').exists()
    assert not (directory / 'direct_new.go').exists()
    assert receipt['protected_directory_entries_unchanged']
print(json.dumps({'schema':'jiuwen-public-directory-control/v1','status':'passed','mode':mode,
                  'public_inventory_count':len(inventory),'blocked':blocked,'receipt':receipt,
                  'production_model_calls':0,'source_sha':subprocess.run(
                      ['git','-C',str(source),'rev-parse','HEAD'],stdin=subprocess.DEVNULL,
                      capture_output=True,text=True,check=True).stdout.strip()}))
