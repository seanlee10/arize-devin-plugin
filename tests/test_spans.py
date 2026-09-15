import json
import unittest

from devin_tracing.config import Config
from devin_tracing.devin_db import LlmCall, ToolCall, Turn

try:
    from opentelemetry.proto.trace.v1 import trace_pb2
    HAVE_PROTO = True
except ImportError:  # pragma: no cover
    HAVE_PROTO = False

if HAVE_PROTO:
    from devin_tracing.spans import build_request, build_degraded_request, truncate


def attrs(span):
    out = {}
    for kv in span.attributes:
        v = kv.value
        out[kv.key] = getattr(v, v.WhichOneof("value"))
    return out


def make_turn():
    tool_ok = ToolCall(id="c1", name="read", arguments={"file_path": "a.txt"}, output="contents",
                       success=True, started_at_ns=4_000, ended_at_ns=5_000)
    tool_bad = ToolCall(id="c2", name="read", arguments={"file_path": "b.txt"}, output="not found",
                        success=False, failure_reason="ValidationError", started_at_ns=4_500, ended_at_ns=4_600)
    first = LlmCall(request_id="r1", model="swe-1-6-slow", started_at_ns=2_000, ended_at_ns=3_000,
                    output_text="", reasoning="plan", finish_reason="tool_calls",
                    input_tokens=100, output_tokens=20, cache_read_tokens=1000, cache_write_tokens=5, ttft_ms=150,
                    input_messages=[{"role": "user", "content": "read files"}], tool_calls=[tool_ok, tool_bad])
    second = LlmCall(request_id="r2", model="swe-1-6-slow", started_at_ns=6_000, ended_at_ns=7_000,
                     output_text="done", reasoning=None, finish_reason="stop",
                     input_tokens=10, output_tokens=2, cache_read_tokens=1100, cache_write_tokens=0, ttft_ms=90,
                     input_messages=[{"role": "tool", "content": "contents", "tool_call_id": "c1"},
                                     {"role": "tool", "content": "not found", "tool_call_id": "c2"}])
    return Turn(session_id="brave-otter", turn_number=3, user_prompt="read files", started_at_ns=1_000,
                ended_at_ns=7_000, head_node_id=9, working_directory="/work", agent_mode="normal",
                llm_calls=[first, second])


META = {"prompt_id": "p-1", "last_assistant_message": "done (from hook)", "incomplete": False, "continuation": False}


@unittest.skipUnless(HAVE_PROTO, "opentelemetry-proto not installed")
class TestBuildRequest(unittest.TestCase):
    def setUp(self):
        self.config = Config(project_name="my-proj", max_content_chars=10000)
        request = build_request(make_turn(), META, self.config)
        self.resource_spans = request.resource_spans[0]
        self.spans = list(self.resource_spans.scope_spans[0].spans)
        self.by_name = {}
        for s in self.spans:
            self.by_name.setdefault(s.name, []).append(s)

    def test_resource_attributes_carry_project(self):
        res = {kv.key: kv.value.string_value for kv in self.resource_spans.resource.attributes}
        self.assertEqual(res["service.name"], "devin-cli")
        self.assertEqual(res["openinference.project.name"], "my-proj")
        self.assertEqual(res["arize.project.name"], "my-proj")

    def test_span_hierarchy(self):
        self.assertEqual(len(self.spans), 5)
        turn = self.by_name["Turn 3"][0]
        llms = self.by_name["swe-1-6-slow"]
        tools = self.by_name["read"]
        self.assertEqual(turn.parent_span_id, b"")
        self.assertEqual(len({s.trace_id for s in self.spans}), 1)
        self.assertEqual(len(turn.trace_id), 16)
        self.assertEqual(len(turn.span_id), 8)
        for llm in llms:
            self.assertEqual(llm.parent_span_id, turn.span_id)
        for tool in tools:
            self.assertEqual(tool.parent_span_id, turn.span_id)
        self.assertEqual(len({s.span_id for s in self.spans}), 5)

    def test_parent_spans_cover_child_intervals(self):
        by_id = {span.span_id: span for span in self.spans}
        for span in self.spans:
            if not span.parent_span_id:
                continue
            parent = by_id[span.parent_span_id]
            self.assertLessEqual(parent.start_time_unix_nano, span.start_time_unix_nano)
            self.assertGreaterEqual(parent.end_time_unix_nano, span.end_time_unix_nano)

    def test_turn_span_attributes(self):
        turn = self.by_name["Turn 3"][0]
        a = attrs(turn)
        self.assertEqual(a["openinference.span.kind"], "AGENT")
        self.assertEqual(a["session.id"], "brave-otter")
        self.assertEqual(a["input.value"], "read files")
        self.assertEqual(a["output.value"], "done (from hook)")
        self.assertEqual(a["devin.prompt_id"], "p-1")
        self.assertEqual(a["devin.turn_number"], 3)
        self.assertEqual(a["devin.working_directory"], "/work")
        self.assertEqual(a["devin.agent_mode"], "normal")
        self.assertNotIn("devin.incomplete", a)
        self.assertEqual(a["llm.token_count.prompt"], 100 + 1000 + 5 + 10 + 1100)
        self.assertEqual(a["llm.token_count.completion"], 22)
        self.assertEqual(a["llm.token_count.total"], 2215 + 22)
        self.assertEqual((turn.start_time_unix_nano, turn.end_time_unix_nano), (1_000, 7_000))
        self.assertEqual(turn.kind, trace_pb2.Span.SPAN_KIND_INTERNAL)

    def test_llm_span_attributes(self):
        llm = self.by_name["swe-1-6-slow"][0]
        a = attrs(llm)
        self.assertEqual(a["openinference.span.kind"], "LLM")
        self.assertEqual(a["session.id"], "brave-otter")
        self.assertEqual(a["llm.model_name"], "swe-1-6-slow")
        self.assertEqual(a["llm.token_count.prompt"], 1105)
        self.assertEqual(a["llm.token_count.completion"], 20)
        self.assertEqual(a["llm.token_count.total"], 1125)
        self.assertEqual(a["llm.token_count.prompt_details.cache_read"], 1000)
        self.assertEqual(a["llm.token_count.prompt_details.cache_write"], 5)
        self.assertEqual(a["llm.input_messages.0.message.role"], "user")
        self.assertEqual(a["llm.input_messages.0.message.content"], "read files")
        self.assertEqual(a["llm.output_messages.0.message.role"], "assistant")
        self.assertEqual(a["llm.output_messages.0.message.tool_calls.0.tool_call.id"], "c1")
        self.assertEqual(a["llm.output_messages.0.message.tool_calls.1.tool_call.function.name"], "read")
        self.assertEqual(json.loads(a["llm.output_messages.0.message.tool_calls.0.tool_call.function.arguments"]),
                         {"file_path": "a.txt"})
        self.assertEqual(a["input.value"], "read files")
        self.assertIn("read", a["output.value"])
        self.assertEqual(a["devin.reasoning"], "plan")
        self.assertEqual(a["devin.finish_reason"], "tool_calls")
        self.assertEqual(a["devin.request_id"], "r1")
        self.assertEqual(a["devin.ttft_ms"], 150)
        self.assertEqual((llm.start_time_unix_nano, llm.end_time_unix_nano), (2_000, 3_000))

    def test_second_llm_uses_last_input_and_output_text(self):
        a = attrs(self.by_name["swe-1-6-slow"][1])
        self.assertEqual(a["input.value"], "not found")
        self.assertEqual(a["output.value"], "done")
        self.assertEqual(a["llm.input_messages.1.message.role"], "tool")
        self.assertEqual(a["llm.output_messages.0.message.content"], "done")
        self.assertNotIn("devin.reasoning", a)

    def test_tool_spans_and_status(self):
        ok, bad = self.by_name["read"]
        a_ok, a_bad = attrs(ok), attrs(bad)
        self.assertEqual(a_ok["openinference.span.kind"], "TOOL")
        self.assertEqual(a_ok["tool.name"], "read")
        self.assertEqual(json.loads(a_ok["tool.parameters"]), {"file_path": "a.txt"})
        self.assertEqual(json.loads(a_ok["input.value"]), {"file_path": "a.txt"})
        self.assertEqual(a_ok["output.value"], "contents")
        self.assertEqual(a_ok["devin.tool_call_id"], "c1")
        self.assertIs(a_ok["devin.tool_success"], True)
        self.assertEqual(ok.status.code, trace_pb2.Status.STATUS_CODE_OK)
        self.assertEqual(bad.status.code, trace_pb2.Status.STATUS_CODE_ERROR)
        self.assertEqual(bad.status.message, "ValidationError")
        self.assertIs(a_bad["devin.tool_success"], False)
        self.assertEqual((ok.start_time_unix_nano, ok.end_time_unix_nano), (4_000, 5_000))


@unittest.skipUnless(HAVE_PROTO, "opentelemetry-proto not installed")
class TestEdgeCases(unittest.TestCase):
    def spans(self, request):
        return list(request.resource_spans[0].scope_spans[0].spans)

    def test_incomplete_and_continuation_flags(self):
        meta = dict(META, incomplete=True, continuation=True)
        turn = self.spans(build_request(make_turn(), meta, Config()))[0]
        a = attrs(turn)
        self.assertIs(a["devin.incomplete"], True)
        self.assertIs(a["devin.continuation"], True)

    def test_output_falls_back_to_last_llm_text(self):
        meta = dict(META, last_assistant_message=None)
        a = attrs(self.spans(build_request(make_turn(), meta, Config()))[0])
        self.assertEqual(a["output.value"], "done")

    def test_missing_timestamps_fall_back_to_now_and_stay_ordered(self):
        turn = make_turn()
        turn.started_at_ns = None
        turn.ended_at_ns = None
        turn.llm_calls[0].started_at_ns = None
        turn.llm_calls[0].tool_calls[0].ended_at_ns = None
        for s in self.spans(build_request(turn, META, Config())):
            self.assertGreater(s.start_time_unix_nano, 0, s.name)
            self.assertGreaterEqual(s.end_time_unix_nano, s.start_time_unix_nano, s.name)

    def test_unknown_tool_status_is_unset(self):
        turn = make_turn()
        turn.llm_calls[0].tool_calls[0].success = None
        tool = [s for s in self.spans(build_request(turn, META, Config())) if s.name == "read"][0]
        self.assertEqual(tool.status.code, trace_pb2.Status.STATUS_CODE_UNSET)
        self.assertNotIn("devin.tool_success", attrs(tool))

    def test_content_is_truncated(self):
        turn = make_turn()
        turn.user_prompt = "x" * 50
        a = attrs(self.spans(build_request(turn, META, Config(max_content_chars=10)))[0])
        self.assertEqual(a["input.value"], "x" * 10 + "…[truncated 40 chars]")

    def test_truncate_leaves_short_strings(self):
        self.assertEqual(truncate("abc", 10), "abc")

    def test_degraded_request_has_single_turn_span(self):
        request = build_degraded_request(
            session_id="brave-otter", turn_number=2, prompt="hi", started_at_ns=10, output="bye",
            meta=dict(META, incomplete=True), config=Config())
        spans = self.spans(request)
        self.assertEqual(len(spans), 1)
        a = attrs(spans[0])
        self.assertEqual(spans[0].name, "Turn 2")
        self.assertEqual(a["openinference.span.kind"], "AGENT")
        self.assertEqual(a["input.value"], "hi")
        self.assertEqual(a["output.value"], "bye")
        self.assertIs(a["devin.degraded"], True)
        self.assertIs(a["devin.incomplete"], True)
        self.assertEqual(spans[0].start_time_unix_nano, 10)


if __name__ == "__main__":
    unittest.main()
