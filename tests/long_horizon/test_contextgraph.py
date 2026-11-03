"""Actual HTTP boundaries and paired graph diagnostics, without model calls."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

import pytest

from jiuwenswarm.long_horizon import contextgraph as reader

RECEIPT = "a" * 64


@pytest.fixture
def graph():
    records = []
    state = {"status": "complete", "corpus": "opposite", "receipt_sha256": RECEIPT,
             "rules": [{"id": "rule_1", "text": "Historical reference", "sources": []}], "episodes": []}

    class Handler(BaseHTTPRequestHandler):
        def reply(self):
            # Closing with an unread POST body can reset the client on macOS.
            if self.command == "POST":
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            records.append(self.path)
            body = state.get("raw") or json.dumps({k: v for k, v in state.items() if k != "code"}).encode()
            self.send_response(state.get("code", 200))
            if state.get("code") == 302:
                self.send_header("Location", "/replacement")
            self.end_headers()
            self.wfile.write(body)

        do_GET = reply
        do_POST = reply

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = {"JIUWENSWARM_BENCH_MEMORY_ENABLED": "1", "CONTEXTGRAPH_HTTP_URL": f"http://127.0.0.1:{server.server_port}",
               "CONTEXTGRAPH_EXPECTED_RECEIPT_SHA256": RECEIPT, "CONTEXTGRAPH_EXPECTED_CORPUS": "opposite",
               "CONTEXTGRAPH_CALL_AUDIT_PATH": str(root / "audit.jsonl"), "http_proxy": "http://127.0.0.1:1"}
        with patch.dict(os.environ, env):
            yield state, records, root, env
    server.shutdown()
    server.server_close()
    thread.join()


def journal(root):
    return [json.loads(x) for x in (root / "audit.jsonl").read_text().splitlines()]


def test_actual_read_contract_and_no_query_in_journal(graph):
    _, records, root, _ = graph
    selected = reader.admit(root / "admission.json")
    assert selected["writeback"] is False
    assert json.loads((root / "admission.json").read_text()) == {"status": "ready"}
    assert "untrusted" in reader.query("PRIVATE_QUERY_MARKER", 2)
    assert "Historical reference" in reader.evidence("rule_1", "PRIVATE_QUERY_MARKER")
    assert records == ["/health", "/query", "/evidence"]
    rows = journal(root)
    assert len(rows) == 6
    for start, end in zip(rows[::2], rows[1::2]):
        assert start["call_id"] == end["call_id"] and end["ok"] is True
    assert "PRIVATE_QUERY_MARKER" not in (root / "audit.jsonl").read_text()


@pytest.mark.parametrize("field,value", [("receipt_sha256", "b" * 64), ("corpus", "other"), ("status", "building")])
def test_wrong_release_fails_without_admission_or_fallback(graph, field, value):
    state, records, root, _ = graph
    state[field] = value
    with pytest.raises(ValueError):
        reader.admit(root / "admission.json")
    assert not (root / "admission.json").exists()
    assert records == ["/health"]
    assert journal(root)[-1]["ok"] is False


@pytest.mark.parametrize("raw", [b'{"status":"complete","status":"complete"}', b'{"x":NaN}', b"x" * (reader.MAX_RESPONSE_BYTES + 1)])
def test_malformed_or_oversized_reply_is_a_failed_call(graph, raw):
    state, _, root, _ = graph
    state["raw"] = raw
    with pytest.raises(ValueError):
        reader.request("/health")
    assert journal(root)[-1]["ok"] is False


def test_redirect_never_visits_another_release(graph):
    state, records, root, _ = graph
    state["code"] = 302
    with pytest.raises(ValueError):
        reader.request("/health")
    assert records == ["/health"] and journal(root)[-1]["ok"] is False


def test_invalid_tool_arguments_do_not_send_requests(graph):
    _, records, _, _ = graph
    for args in [("", 1), ("x", True), ("x", 11)]:
        with pytest.raises(ValueError):
            reader.query(*args)
    with pytest.raises(ValueError):
        reader.evidence("../outside", "query")
    with pytest.raises(ValueError):
        reader.request("/deposit", {})
    assert records == []


def test_audit_symlink_cannot_be_used(graph):
    _, records, root, env = graph
    outside = root / "outside"
    outside.write_text("original")
    (root / "audit.jsonl").symlink_to(outside)
    with pytest.raises(OSError):
        reader.request("/health")
    assert records == [] and outside.read_text() == "original"


def test_ambient_endpoint_does_not_enable_memory(graph):
    with patch.dict(os.environ, {"JIUWENSWARM_BENCH_MEMORY_ENABLED": "0"}):
        assert reader.selection() == {"enabled": False, "mode": "disabled", "writeback": False}
        with pytest.raises(ValueError):
            reader.request("/health")


def test_real_stdio_mcp_has_only_three_read_tools(graph):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check():
        async with stdio_client(StdioServerParameters(command=sys.executable,
            args=["-m", "jiuwenswarm.long_horizon.contextgraph_mcp"], env=dict(os.environ))) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {x.name for x in tools} == {"team_memory_health", "team_memory_query", "team_memory_evidence"}
                result = await session.call_tool("team_memory_query", {"query": "async cleanup", "top_k": 2})
                assert not result.isError and "Historical reference" in result.content[0].text
    asyncio.run(check())
