# Copyright (c) 2026 Chrys. All rights reserved.

"""Verify real call edges, model traversal, evidence consumption and recovery."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_suite_flow import plugin, trial

from jiuwenswarm.lh import LongHorizonBenchmark
from jiuwenswarm.lh.localization import localize_suite
from jiuwenswarm.lh.semantic_graph import RepositoryGraph


def graph_files(root: Path):
    files = {
        "app/__init__.py": "",
        "app/storage.py": "def commit_record(value):\n    return value.strip()\n",
        "app/other.py": "def commit_record(value):\n    return 'unrelated'\n",
        "app/queue.py": "from .storage import commit_record as persist\n\ndef flush_batch(value):\n    return persist(value)\n",
        "tests/test_queue.py": "from app.queue import flush_batch as execute\n\ndef test_empty_batch():\n    assert execute(' x ') == 'x'\n",
    }
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return files


def test_import_qualified_graph_reaches_test_without_keyword_and_rejects_same_name(tmp_path):
    files = graph_files(tmp_path)
    graph = RepositoryGraph(tmp_path)
    assert ("app/queue.py:flush_batch", "app/storage.py:commit_record", "cross_file_call") in graph.edges
    assert not any(
        source == "app/queue.py:flush_batch" and target.startswith("app/other.py") for source, target, _ in graph.edges
    )
    first = graph.query("find_code_definition", {"definition_name": "commit_record", "file_path": "app/storage.py"})
    assert first["units"][0]["relationships"] == [
        {"kind": "caller", "target": "app/queue.py:flush_batch", "file_path": "app/queue.py", "symbol": "flush_batch"}
    ]
    second = graph.query("find_child_unit", {"definition_name": "flush_batch", "file_path": "app/queue.py"})
    assert any(
        relation["target"] == "tests/test_queue.py:test_empty_batch" for relation in second["units"][0]["relationships"]
    )
    assert "commit_record" not in files["tests/test_queue.py"]
    graph.adjacent.clear()
    without = graph.query("find_code_definition", {"definition_name": "commit_record", "file_path": "app/storage.py"})
    assert first["units"][0]["preview"] == without["units"][0]["preview"]
    assert not without["units"][0]["relationships"]


def test_scopes_reexports_known_methods_inheritance_and_callbacks(tmp_path):
    (tmp_path / "base.py").write_text("class Base:\n    def process(self):\n        return 1\n")
    (tmp_path / "export.py").write_text("from base import Base as Processor\n")
    (tmp_path / "client.py").write_text(
        "from export import Processor\nfrom base import Base as Imported\nimport asyncio\n"
        "class Child(Processor):\n    def process(self):\n        return super().process()\n"
        "def run(value: Child):\n    return value.process()\n"
        "def local():\n    def hidden():\n        return Imported()\n    return 0\n"
        "def shadowed(Imported):\n    return Imported()\n"
        "def schedule():\n    asyncio.create_task(run(None))\n"
    )
    graph = RepositoryGraph(tmp_path)
    assert ("client.py:Child", "base.py:Base", "inherits") in graph.edges
    assert ("client.py:Child.process", "base.py:Base.process", "override") in graph.edges
    assert ("client.py:run", "client.py:Child.process", "call") in graph.edges
    assert ("client.py:schedule", "client.py:run", "async_call") in graph.edges
    assert ("client.py:local.hidden", "base.py:Base", "cross_file_call") in graph.edges
    assert not any(
        source in {"client.py:local", "client.py:shadowed"} and target == "base.py:Base"
        for source, target, _ in graph.edges
    )


def test_graph_never_reads_symlinks_or_private_grading_paths(tmp_path):
    root = tmp_path / "view"
    root.mkdir()
    outside = tmp_path / "private.py"
    outside.write_text("def hidden(): pass\n")
    (root / "leak.py").symlink_to(outside)
    (root / "patches").mkdir()
    (root / "patches" / "answer.py").write_text("def answer(): pass\n")
    (root / "public.py").write_text("def public(): pass\n")
    graph = RepositoryGraph(root)
    assert set(graph.files) == {"public.py"}
    assert not graph.query("find_code_content", {"content": "hidden", "file_path": "../private.py"})["matches"]
    with pytest.raises(ValueError, match="indexed file"):
        graph.normalize([{"file_path": "../private.py", "symbol": "hidden"}])


@pytest.mark.parametrize(
    "name,text,needle",
    [
        ("main.go", 'package main\nfunc Greet() string { return "hello" }\n', "Greet"),
        ("run.sh", "#!/bin/sh\nprintf 'public-greeting\\n'\n", "public-greeting"),
    ],
)
def test_go_and_shell_sources_support_file_and_content_localization(tmp_path, name, text, needle):
    (tmp_path / name).write_text(text)
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / name).write_text("private-gold")
    graph = RepositoryGraph(tmp_path)
    assert set(graph.files) == {name}
    assert graph.query("find_file", {"file_name": name})["files"]
    matches = graph.query("find_code_content", {"content": needle})["matches"]
    assert matches and all(row["file_path"] == name for row in matches)
    assert not graph.query("find_code_content", {"content": "private-gold"})["matches"]
    assert not graph.edges  # File/content support does not invent non-Python call edges.


def test_go_vendor_dependencies_cannot_crowd_out_public_source_file_budget(tmp_path):
    (tmp_path / "main.go").write_text("package main\n")
    vendor = tmp_path / "vendor" / "thirdparty"
    vendor.mkdir(parents=True)
    for n in range(5):
        (vendor / f"dep{n}.go").write_text("package dependency\n")
    public = tmp_path / "zpublic"
    public.mkdir()
    (public / "feature.go").write_text("package feature\nfunc PublicFeature() {}\n")
    graph = RepositoryGraph(tmp_path, max_files=2)
    assert set(graph.files) == {"main.go", "zpublic/feature.go"}
    assert not graph.truncated
    assert graph.query("find_code_content", {"content": "PublicFeature"})["matches"]


def test_conditional_class_function_name_collision_keeps_base_dispatch_unresolved(tmp_path):
    (tmp_path / "conditional.py").write_text(
        "class Base:\n    def process(self):\n        return 1\n"
        "if enabled:\n"
        "    class Optional(Base):\n"
        "        def process(self):\n            return super().process()\n"
        "else:\n"
        "    def Optional():\n        return Base()\n"
        "def unrelated():\n    return Base()\n"
    )
    graph = RepositoryGraph(tmp_path)
    assert ("conditional.py:unrelated", "conditional.py:Base", "call") in graph.edges
    assert not any(
        source == "conditional.py:Optional.process" and target == "conditional.py:Base.process"
        for source, target, _ in graph.edges
    )
    assert graph.query("find_code_definition", {"definition_name": "unrelated"})["units"]


@pytest.mark.parametrize(
    "invalid", ["not json", '{"locations":[{"file_path":"app/other.py","symbol":"commit_record"}]}']
)
@pytest.mark.asyncio
async def test_failed_agent_search_is_explicit_graph_fallback_not_fake_success(tmp_path, invalid):
    root = tmp_path / "view"
    graph_files(root)
    context = SimpleNamespace(
        snapshot=SimpleNamespace(roots=[SimpleNamespace(view_root=str(root))]), session_dir=tmp_path / "output"
    )

    async def investigate(*args, **kwargs):
        return invalid

    value = SimpleNamespace(investigate=investigate, artifacts=SimpleNamespace(exhausted=TimeoutError))
    result = await localize_suite(value, context, "Fix commit_record callers", "frozen-fixture")
    assert result["status"] == "degraded" and result["mode"] == "deterministic-graph-fallback"
    assert result["query_count"] == 0 and result["graph_summary"]["edge_count"] > 0
    assert any(row["file_path"] == "tests/test_queue.py" for row in result["locations"])


@pytest.mark.asyncio
async def test_cancelled_graph_search_keeps_partial_trace_and_does_not_claim_completion(tmp_path):
    root = tmp_path / "view"
    graph_files(root)
    context = SimpleNamespace(
        snapshot=SimpleNamespace(roots=[SimpleNamespace(view_root=str(root))]), session_dir=tmp_path / "output"
    )
    entered = asyncio.Event()

    async def investigate(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    value = SimpleNamespace(investigate=investigate, artifacts=SimpleNamespace(exhausted=TimeoutError))
    task = asyncio.create_task(localize_suite(value, context, "commit_record", "frozen-fixture"))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    trace = next((tmp_path / "output").rglob("localization-trace.jsonl")).read_text()
    assert '"reason": "cancelled"' in trace
    assert not list((tmp_path / "output").rglob("code-localization.json"))


@pytest.mark.asyncio
async def test_unexposed_location_is_requeried_before_selection_instead_of_whole_search_fallback(tmp_path):
    root = tmp_path / "view"
    graph_files(root)
    context = SimpleNamespace(
        snapshot=SimpleNamespace(roots=[SimpleNamespace(view_root=str(root))]), session_dir=tmp_path / "output"
    )
    replies = [
        {
            "queries": [
                {
                    "name": "find_child_unit",
                    "arguments": {"definition_name": "commit_record", "file_path": "app/storage.py"},
                }
            ]
        },
        {"locations": [{"file_path": "app/other.py", "symbol": "commit_record"}]},
        {
            "queries": [
                {
                    "name": "find_child_unit",
                    "arguments": {"definition_name": "commit_record", "file_path": "app/other.py"},
                }
            ]
        },
        {"locations": [{"file_path": "app/other.py", "symbol": "commit_record"}]},
    ]
    prompts = []

    async def investigate(context, prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps(replies.pop(0))

    value = SimpleNamespace(investigate=investigate, artifacts=SimpleNamespace(exhausted=TimeoutError))
    result = await localize_suite(value, context, "Compare both public definitions", "frozen-fixture")
    assert result["status"] == "available" and result["model_calls"] == 4
    assert result["query_count"] == 2 and result["locations"][0]["file_path"] == "app/other.py"
    assert "location was not exposed" in prompts[2]
    trace = next((tmp_path / "output").rglob("localization-trace.jsonl")).read_text()
    assert '"event": "model-reply-rejected"' in trace
    assert '"event": "deterministic-graph-fallback"' not in trace


@pytest.mark.asyncio
async def test_actual_shared_suite_graph_reaches_plan_p1_and_seals_resume_evidence(tmp_path, git_repo_factory):
    args, calls = trial(tmp_path, git_repo_factory), []
    graph_files(args.workdir)
    subprocess.run(["git", "add", "."], cwd=args.workdir, stdin=subprocess.DEVNULL, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Graph fixture"],
        cwd=args.workdir,
        stdin=subprocess.DEVNULL,
        check=True,
        capture_output=True,
    )
    result = await LongHorizonBenchmark(args, plugin=plugin(calls)).run()
    report = json.loads(result.selection.read_text())
    assert not result.internal_failure, report["internal_failure"]
    assert report["internal_accepted"] and report["delivered"]
    suite = report["long_horizon"]["suite"]
    localization = suite["localization"]
    assert localization["status"] == "available", localization
    assert localization["query_count"] == 3 and localization["model_calls"] == 4
    assert localization["relationship_exposure_count"] >= 3
    assert "tests/test_queue.py:test_empty_batch" in localization["selected_graph_exposed_units"]
    assert not any(row["file_path"] == "app/other.py" for row in localization["locations"])
    assert suite["localization_consumption"]["initial_plan_hints_include_localization"]
    receipt = json.loads((args.output / suite["p1_graph_consumption"][0]["path"]).read_text())
    assert receipt["prompt_includes_localization"] and receipt["stage"] == "p1-worker-adapter"
    assert report["budget"]["role_calls"] == len(calls)
    before = len(calls)
    resume = replace(args, work_id=report["work_id"], expected_revision=report["revision"])
    replay = await LongHorizonBenchmark(resume, plugin=plugin(calls)).run()
    assert not replay.internal_failure and len(calls) == before
    trace = next(row for row in suite["graph_artifacts"] if row["path"].endswith("localization-trace.jsonl"))
    (args.output / trace["path"]).write_text("{}\n")
    changed = await LongHorizonBenchmark(resume, plugin=plugin(calls)).run()
    assert changed.internal_failure and len(calls) == before
    assert (
        "bound graph localization evidence changed"
        in json.loads(changed.selection.read_text())["internal_failure"]["detail"]
    )
