"""Real native CLI/model-call boundary with local SSE connection resets only."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, '/candidate')
from jiuwenswarm.long_horizon.worker import JiuwenWorker

mode = sys.argv[1]
assert mode in {'disabled', 'recovery', 'persistent', 'auth', 'deadline', 'tool-free'}
root = Path('/tmp/native-transport-control')
workspace = root / 'public-source'
workspace.mkdir(parents=True)
(workspace / 'tests').mkdir()
(workspace / 'tests/original.py').write_text('# public test unchanged\n')
for args in (['init', '-q'], ['config', 'user.name', 'Control'],
             ['config', 'user.email', 'control@example.invalid'], ['add', '.'], ['commit', '-qm', 'public baseline']):
    subprocess.run(['git', '-C', str(workspace), *args], stdin=subprocess.DEVNULL, capture_output=True, check=True)
requests = []

class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests.append(data)
        number = len(requests)
        tool_free = mode == 'tool-free'
        if mode == 'auth' and number == 2:
            body = json.dumps({'error': {'message': 'control unauthorized', 'type': 'authentication_error', 'code': 'invalid_api_key'}}).encode()
            self.send_response(401)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        reset = (tool_free and number == 1) or (not tool_free and number >= 2 and (mode != 'recovery' or number == 2))
        delta = {'role': 'assistant'}
        if reset:
            delta['content'] = 'FAILED_PARTIAL_STREAM_MUST_NOT_COMMIT'
            if not tool_free:
                delta['tool_calls'] = [{'index': 0, 'id': 'failed-partial-tool', 'type': 'function',
                    'function': {'name': 'bash', 'arguments': json.dumps({'command': "printf bad >> failed-tool.txt"})}}]
        elif number == 1 and not tool_free:
            delta['tool_calls'] = [{'index': 0, 'id': 'completed-once', 'type': 'function',
                'function': {'name': 'bash', 'arguments': json.dumps({'command': "printf once >> completed-tool.txt"})}}]
        else:
            delta['content'] = 'RECOVERED_FINAL_ONLY'
        common = {'id': f'fixture-{number}', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'transport-control'}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Connection', 'close')
        if reset:
            self.send_header('Content-Length', '999999')
        self.end_headers()
        self.wfile.write(('data: ' + json.dumps({**common, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]}) + '\n\n').encode())
        self.wfile.flush()
        if reset:
            # Deliberate TCP RST after a delivered partial message: this must
            # traverse the real HTTPX -> native FrameworkError cause chain.
            time.sleep(0.1)
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            os.close(self.connection.detach())
            self.close_connection = True
            return
        finish = 'tool_calls' if delta.get('tool_calls') else 'stop'
        self.wfile.write(('data: ' + json.dumps({**common, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}) + '\n\n').encode())
        self.wfile.write(b'data: [DONE]\n\n')
        self.wfile.flush()

server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
threading.Thread(target=server.serve_forever, daemon=True).start()
spec = importlib.util.spec_from_file_location('control_configure', '/probe/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)
profile = {'id': 'control', 'name': 'Control', 'provider': 'openai', 'api_style': 'chat_completions',
    'api_key': '{{JIUWENSWARM_BENCH_API_KEY}}', 'model_id': 'transport-control',
    'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'max_context_tokens': 32000,
    'max_output_tokens': 2048, 'http_read_timeout': 120, 'stream_first_chunk_timeout': 60,
    'stream_idle_timeout': 45, 'chat_options': ''}
config = configure.native_config(profile, 90)
config['execution_guard'] = {'model_transport_retry': {'enabled': False} if mode == 'disabled' else
    {'enabled': True, 'backoff_seconds': [60] if mode == 'deadline' else [0, 0, 0]}}
# Deadline control's longer wire backoff must remain bounded by the original role deadline.
template = root / 'config.yaml'
template.write_text(json.dumps(config))
os.environ['JIUWENSWARM_BENCH_API_KEY'] = 'local-control-no-network'
os.environ['JIUWENSWARM_DATA_DIR'] = str(root / 'controller-state')
worker = JiuwenWorker(Path('/opt/jiuwenswarm-bench/bin/jiuwenswarm-worker'), template,
    source_commit='candidate-control', default_timeout_seconds=20 if mode == 'deadline' else 90,
    benchmark_write_guard=True)
worker.benchmark_source_root = workspace
out = root / 'output'
started = time.monotonic()
error = None
result = None
try:
    result = asyncio.run(worker.run(stage='transport-control', prompt='Exercise the public transport fixture.',
        workspace=workspace, output_dir=out, read_only=mode == 'tool-free',
        tool_policy='none' if mode == 'tool-free' else 'configured'))
except Exception as exc:
    error = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
elapsed = time.monotonic() - started
server.shutdown()
metadata = json.loads((out / 'metadata.json').read_text())
terminal_path = out / 'terminal-result.json'
terminal = json.loads(terminal_path.read_text()) if terminal_path.exists() else None
records = []
for path in [out / 'events.jsonl']:
    for line in path.read_text().splitlines():
        try: records.append(json.loads(line))
        except ValueError: pass
retries = [row for row in records if row.get('event_type') == 'chat.model_transport_retry']
stderr = (out / 'stderr.log').read_text()
# Check the subsequent wire request, which is the authoritative conversation,
# not raw observed deltas (those are retained deliberately for failure audit).
failed_committed = any('FAILED_PARTIAL_STREAM_MUST_NOT_COMMIT' in json.dumps(row['messages']) for row in requests)
completed = (workspace / 'completed-tool.txt').read_text() if (workspace / 'completed-tool.txt').exists() else None
bad_executed = (workspace / 'failed-tool.txt').exists()
assert not failed_committed
assert not bad_executed
assert (workspace / 'tests/original.py').read_text() == '# public test unchanged\n'
if mode != 'tool-free': assert completed == 'once', completed
if mode in {'recovery', 'tool-free'}:
    assert error is None and result.status == 'completed', (error, terminal)
    assert result.final_text == 'RECOVERED_FINAL_ONLY', result.final_text
    assert len(requests) == (2 if mode == 'tool-free' else 3), len(requests)
    assert len(retries) == 1, retries
elif mode == 'persistent':
    assert error and len(requests) == 5, (error, len(requests))
    assert len(retries) == 3, len(retries)
    assert 'read retry exhausted' in stderr
elif mode == 'disabled':
    assert error and len(requests) == 2 and not retries, (error, len(requests), retries)
    assert 'ReadError' in stderr
elif mode == 'auth':
    assert error and len(requests) == 2 and not retries, (error, len(requests), retries)
else:
    assert error and error['code'] == 'TIMEOUT', (error, terminal)
    assert len(requests) == 2 and len(retries) == 1, (len(requests), retries)
    assert elapsed < 28, elapsed
summary = {'schema': 'jiuwenswarm-benchmark/native-transport-control/v1', 'status': 'passed', 'mode': mode,
    'production_model_calls': 0, 'mock_provider_calls': len(requests), 'native_retry_events': len(retries),
    'typed_read_error_observed': 'ReadError' in stderr, 'elapsed_seconds': elapsed,
    'completed_tool_executions': 0 if completed is None else 1, 'failed_tool_executed': bad_executed,
    'failed_partial_in_wire_history': failed_committed, 'native_status': result.status if result else None,
    'native_final': result.final_text if result else None, 'error': error,
    'candidate_filtering': False, 'public_test_unchanged': True,
    'native_source_identity': metadata['source']}
print(json.dumps(summary, sort_keys=True))
