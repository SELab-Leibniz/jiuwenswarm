"""Run the real native CLI/tools against a deterministic local HTTP fixture."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/candidate")
from jiuwenswarm.long_horizon.worker import JiuwenWorker

root = Path("/tmp/native-mock-control")
source = root / "public-source"
source.mkdir(parents=True)
(source / "tests").mkdir()
(source / "tests/original.py").write_text("# original public test\n")
(source / "source.py").write_text("value = 1\n")
for args in (["init", "-q"], ["config", "user.name", "Public Control"],
             ["config", "user.email", "control@example.invalid"], ["add", "."],
             ["commit", "-qm", "public baseline"]):
    subprocess.run(["git", "-C", str(source), *args], capture_output=True,
                   stdin=subprocess.DEVNULL, check=True)
workspace = root / "role-workspace"
subprocess.run(["git", "clone", "-q", str(source), str(workspace)], capture_output=True,
               stdin=subprocess.DEVNULL, check=True)
requests = []
phase = {"label": "mutable-1", "readonly": False, "calls": 0}


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        assert self.path == "/health"
        payload = json.dumps({"status": "complete", "corpus": "public-control",
                              "receipt_sha256": "a" * 64}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = data.get("messages", [])
        requests.append({"tool_result_count": sum(message.get("role") == "tool" for message in messages),
                         "tool_results": [message.get("content") for message in messages if message.get("role") == "tool"],
                         "tools": [{"name": tool.get("function", {}).get("name"),
                                    "required": tool.get("function", {}).get("parameters", {}).get("required", []),
                                    "properties": list(tool.get("function", {}).get("parameters", {}).get("properties", {}))}
                                   for tool in data.get("tools", [])]})
        phase["calls"] += 1
        number = phase["calls"]
        if number == 1:
            calls = [("read_file", {"file_path": str(workspace / "tests/original.py")}),
                     ("read_file", {"file_path": str(workspace / "source.py")})]
            if not phase["readonly"]:
                graph_name = next(tool["function"]["name"] for tool in data["tools"]
                                  if tool["function"]["name"].endswith("team_memory_health"))
                calls.append((graph_name, {}))
        elif phase["readonly"]:
            calls = []
        elif number == 2:
            calls = [("write_file", {"file_path": str(workspace / "tests/new_case.py"), "content": "invalid"}),
                     ("bash", {"command": "mkdir -p tests/new_checker"}),
                     ("write_file", {"file_path": str(workspace / "tests/original.py"), "content": "invalid"}),
                     ("bash", {"command": "/opt/jiuwenswarm/.venv/bin/python -c \"from pathlib import Path; Path('tests/original.py').write_text('invalid')\""}),
                     ("bash", {"command": "mkdir -p .pact/checks"})]
        elif number == 3:
            calls = [("write_file", {"file_path": str(workspace / "source.py"), "content": "value = 2\n"}),
                     ("bash", {"command": "mkdir -p checks; printf 'assert True\\n' > checks/new_check.py"})]
        else:
            calls = []
        delta = {"role": "assistant"}
        if calls:
            delta["tool_calls"] = [{"index": index, "id": f"control-{number}-{index}", "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(arguments)}}
                                   for index, (name, arguments) in enumerate(calls)]
        else:
            delta["content"] = "Implemented the source change. Original public tests remain unchanged."
        common = {"id": f"mock-{number}", "object": "chat.completion.chunk", "created": 1, "model": "public-control"}
        chunks = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if calls else "stop"}]}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
spec = importlib.util.spec_from_file_location("control_configure", "/probe/configure.py")
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
profile = {"id": "bench", "name": "Bench", "provider": "openai", "api_style": "chat_completions",
           "api_key": "{{JIUWENSWARM_BENCH_API_KEY}}",
           "model_id": "public-control", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
           "max_context_tokens": 32000, "max_output_tokens": 2048, "http_read_timeout": 120,
           "stream_first_chunk_timeout": 60, "stream_idle_timeout": 45, "chat_options": ""}
graph_url = f"http://127.0.0.1:{server.server_port}"
runtime = json.loads(Path('/opt/jiuwenswarm-bench/runtime.json').read_text())
real_graph = '--real-graph' in sys.argv
graph_policy = runtime['memory_policy'] if real_graph else {'http_url': graph_url,
    'receipt_sha256': 'a' * 64, 'source_corpus': 'public-control'}
graph_url = graph_policy['http_url']
journal = root / "memory-calls.jsonl"
os.environ.update(JIUWENSWARM_MEMGRAPH_ENABLED="1", MEMGRAPH_HTTP_URL=graph_url,
                  MEMGRAPH_EXPECTED_RECEIPT_SHA256=graph_policy['receipt_sha256'],
                  MEMGRAPH_EXPECTED_CORPUS=graph_policy['source_corpus'], MEMGRAPH_CALL_AUDIT_PATH=str(journal))
from jiuwenswarm.long_horizon import memgraph
memgraph.admit(root / "memory-admission.json")
config = configure.native_config(profile, 90, {"enabled": True, "mode": "frozen-cross-corpus",
                                               "writeback": False, "http_url": graph_url,
                                               "receipt_sha256": graph_policy['receipt_sha256'],
                                               "source_corpus": graph_policy['source_corpus']})
template = root / "config.yaml"
template.write_text(json.dumps(config))
os.environ["JIUWENSWARM_BENCH_API_KEY"] = "public-protocol-control"
os.environ["JIUWENSWARM_DATA_DIR"] = str(root / "controller-state")
worker = JiuwenWorker(Path("/opt/jiuwenswarm-bench/bin/jiuwenswarm-worker"), template,
                      source_commit=runtime['jiuwenswarm_source_sha'],
                      default_timeout_seconds=90, benchmark_write_guard=True)
worker.benchmark_source_root = source
receipts=[]
workspaces=[]
for label,readonly in [("mutable-1",False),("readonly",True),("mutable-2",False)]:
    phase.update(label=label,readonly=readonly,calls=0)
    workspace=root/label
    subprocess.run(["git","clone","-q",str(source),str(workspace)],
                   capture_output=True,stdin=subprocess.DEVNULL,check=True)
    out=root/(label+"-output")
    previous_umask=os.umask(0o077)
    try:
        result=asyncio.run(worker.run(stage=label,prompt="Exercise the public source fixture.",
                                     workspace=workspace,output_dir=out,read_only=readonly))
    except Exception as error:
        metadata=json.loads((out/"metadata.json").read_text())
        print(json.dumps({"phase":label,"failure_type":type(error).__name__,
                          "failure_code":getattr(error,"code",None),"mock_provider_calls":len(requests),
                          "terminal":json.loads((out/"terminal-result.json").read_text()) if (out/"terminal-result.json").exists() else None}))
        print((out/"stderr.log").read_text()[-2000:])
        raise
    finally:
        os.umask(previous_umask)
    metadata=json.loads((out/"metadata.json").read_text())
    receipts.append({"phase":label,"native_status":result.status,
                     "cache":metadata.get("python_bytecode_cache"),
                     "cache_has_bytecode":any((out/"state/python-cache").rglob("*.pyc")),
                     "guard":metadata.get("native_write_guard"),"mock_calls":phase["calls"]})
    assert result.status=="completed"
    assert (workspace/"tests/original.py").read_text()=="# original public test\n"
    if not readonly:
        assert (workspace/"source.py").read_text()=="value = 2\n"
        assert (workspace/"checks/new_check.py").read_text()=="assert True\n"
        assert metadata["native_write_guard"]["status"]=="complete"
        assert metadata["native_write_guard"]["protected_directory_entries_unchanged"]
        assert not (workspace/"tests/new_case.py").exists()
        assert not (workspace/"tests/new_checker").exists()
    else:
        assert (workspace/"source.py").read_text()=="value = 1\n"
    assert not (workspace/".pact").exists()
    workspaces.append(workspace)
server.shutdown()
assert len(requests)==10,len(requests)
assert not Path("/opt/jiuwenswarm/jiuwenswarm/extensions/__pycache__").exists()
assert len({row["cache"] for row in receipts})==3
assert all(row["cache_has_bytecode"] for row in receipts)
print(json.dumps({"schema":"jiuwenswarm-benchmark/native-public-directory-cache-cycle/v1","status":"passed",
                  "production_model_calls":0,"mock_provider_calls":len(requests),"roles":receipts,
                  "shared_extension_cache_created":False,"source_write":True,"public_test_unchanged":True,
                  "namespace_unchanged":True,"candidate_filtering":False},sort_keys=True))
