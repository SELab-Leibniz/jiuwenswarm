"""Check the LH artifacts used for offline installation."""

import hashlib
import json
from importlib.resources import files
from pathlib import PurePosixPath


def test_bundled_wheels_match_provenance_and_offline_requirements() -> None:
    package = files("jiuwenswarm.lh")
    source = json.loads(package.joinpath("SOURCE.json").read_text(encoding="utf-8"))
    requirements = package.joinpath("requirements.txt").read_text(encoding="utf-8").splitlines()
    modern = source["pact_core"]
    pathspec = source["companion_wheels"]["pathspec"]
    legacy = source["companion_wheels"]["pact-core-legacy-campaign"]

    assert set(requirements) == {
        "--no-index",
        "--find-links ./wheels",
        "--require-hashes",
        f"pact-core=={modern['version']} --hash=sha256:{modern['sha256']}",
        f"pathspec==1.1.1 --hash=sha256:{pathspec['sha256']}",
    }
    assert PurePosixPath(legacy["bundled_wheel"]).parent.name == "legacy"

    for artifact in (modern, pathspec, legacy):
        relative_path = PurePosixPath(artifact["bundled_wheel"]).relative_to("jiuwenswarm/lh")
        wheel = package.joinpath(*relative_path.parts)
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == artifact["sha256"]
