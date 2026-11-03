
import subprocess
from pathlib import Path
import pytest

@pytest.fixture
def git_repo_factory():
    def make(path: Path) -> Path:
        path.mkdir()
        for args in [['init', '-q'], ['config','user.name','Fixture'], ['config','user.email','fixture@example.invalid']]:
            subprocess.run(['git', *args],cwd=path,check=True,capture_output=True)
        (path / 'README.md').write_text('Public fixture\n')
        subprocess.run(['git','add','.'],cwd=path,check=True,capture_output=True)
        subprocess.run(['git','commit','-qm','Baseline'],cwd=path,check=True,capture_output=True)
        return path
    return make
