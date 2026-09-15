import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from devin_tracing import log
from devin_tracing.config import Config
from devin_tracing.devin_db import LlmCall, ToolCall, Turn

try:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    HAVE_PROTO = True
except ImportError:  # pragma: no cover
    HAVE_PROTO = False

if HAVE_PROTO:
    from devin_tracing.export import export
    from devin_tracing.spans import build_request


class StubCollector:
    """Local OTLP/HTTP endpoint that replies with scripted status codes."""

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.requests = []
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                collector.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status = collector.statuses.pop(0) if collector.statuses else 200
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"stub-body")

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d/v1/traces" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def sample_request(config):
    tool = ToolCall(id="c1", name="exec", arguments={"command": "ls"}, output="a", success=True,
                    started_at_ns=3, ended_at_ns=4)
    call = LlmCall(request_id="r", model="m", started_at_ns=1, ended_at_ns=2, output_text="ok", reasoning=None,
                   finish_reason="stop", input_tokens=1, output_tokens=1, cache_read_tokens=0,
                   cache_write_tokens=0, ttft_ms=None, input_messages=[], tool_calls=[tool])
    turn = Turn(session_id="s", turn_number=1, user_prompt="secret prompt", started_at_ns=1, ended_at_ns=4,
                head_node_id=1, working_directory="/w", agent_mode="normal", llm_calls=[call])
    return build_request(turn, {"prompt_id": "p"}, config)


@unittest.skipUnless(HAVE_PROTO, "opentelemetry-proto not installed")
class TestExport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self.tmp.name, "t.log")
        log.configure(self.log_path, verbose=False)
        self.collector = None

    def tearDown(self):
        if self.collector:
            self.collector.close()
        log.configure(None, False)
        self.tmp.cleanup()

    def log_text(self):
        if not os.path.exists(self.log_path):
            return ""
        with open(self.log_path) as f:
            return f.read()

    def config(self, **kw):
        base = dict(api_key="key-123", space_id="space-456", project_name="proj")
        base.update(kw)
        return Config(**base)

    def test_posts_protobuf_with_arize_headers(self):
        self.collector = StubCollector([200])
        config = self.config(endpoint=self.collector.url)
        request = sample_request(config)

        self.assertTrue(export(request, config))

        self.assertEqual(len(self.collector.requests), 1)
        sent = self.collector.requests[0]
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        self.assertEqual(sent["path"], "/v1/traces")
        self.assertEqual(headers["content-type"], "application/x-protobuf")
        self.assertEqual(headers["space_id"], "space-456")
        self.assertEqual(headers["api_key"], "key-123")
        decoded = ExportTraceServiceRequest()
        decoded.ParseFromString(sent["body"])
        self.assertEqual(decoded, request)

    def test_retries_once_on_server_error(self):
        self.collector = StubCollector([503, 200])
        config = self.config(endpoint=self.collector.url)

        self.assertTrue(export(sample_request(config), config))

        self.assertEqual(len(self.collector.requests), 2)

    def test_gives_up_after_second_server_error(self):
        self.collector = StubCollector([500, 500, 200])
        config = self.config(endpoint=self.collector.url)

        self.assertFalse(export(sample_request(config), config))

        self.assertEqual(len(self.collector.requests), 2)
        self.assertIn("HTTP 500", self.log_text())

    def test_does_not_retry_client_error(self):
        self.collector = StubCollector([401])
        config = self.config(endpoint=self.collector.url)

        self.assertFalse(export(sample_request(config), config))

        self.assertEqual(len(self.collector.requests), 1)
        self.assertIn("HTTP 401", self.log_text())
        self.assertIn("stub-body", self.log_text())

    def test_connection_error_returns_false(self):
        config = self.config(endpoint="http://127.0.0.1:9/v1/traces")
        self.assertFalse(export(sample_request(config), config))
        self.assertIn("ERROR", self.log_text())

    def test_missing_credentials_sends_nothing(self):
        self.collector = StubCollector([200])
        config = self.config(endpoint=self.collector.url, api_key=None)

        self.assertFalse(export(sample_request(config), config))

        self.assertEqual(self.collector.requests, [])
        self.assertIn("ARIZE_API_KEY", self.log_text())

    def test_dry_run_logs_spans_without_sending(self):
        self.collector = StubCollector([200])
        config = self.config(endpoint=self.collector.url, dry_run=True, api_key=None, space_id=None)

        self.assertTrue(export(sample_request(config), config))

        self.assertEqual(self.collector.requests, [])
        lines = [l for l in self.log_text().splitlines() if "DRY RUN span " in l]
        self.assertEqual(len(lines), 3)
        spans = [json.loads(l.split("DRY RUN span ", 1)[1]) for l in lines]
        self.assertEqual([s["kind"] for s in spans], ["AGENT", "LLM", "TOOL"])
        self.assertEqual(spans[1]["parent"], spans[0]["span_id"])
        self.assertEqual(spans[2]["parent"], spans[0]["span_id"])
        self.assertEqual(spans[0]["attributes"]["input.value"], "secret prompt")
        self.assertEqual(spans[2]["status"], "OK")

    def test_log_never_contains_api_key(self):
        self.collector = StubCollector([401])
        config = self.config(endpoint=self.collector.url, verbose=True)
        log.configure(self.log_path, verbose=True)
        export(sample_request(config), config)
        self.assertNotIn("key-123", self.log_text())


if __name__ == "__main__":
    unittest.main()
