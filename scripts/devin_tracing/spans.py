"""Turn model -> OpenInference spans as an OTLP ExportTraceServiceRequest."""

import json
import os
import time

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

SCOPE_NAME = "arize-devin-tracing"
SERVICE_NAME = "devin-cli"


def truncate(text, limit):
    if text is None or len(text) <= limit:
        return text
    return "%s…[truncated %d chars]" % (text[:limit], len(text) - limit)


def _to_json(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class _SpanBuilder:
    def __init__(self, name, trace_id, parent_id, config):
        self.span = Span(trace_id=trace_id, span_id=os.urandom(8), name=name, kind=Span.SPAN_KIND_INTERNAL)
        if parent_id:
            self.span.parent_span_id = parent_id
        self.limit = config.max_content_chars

    def set(self, key, value):
        if value is None:
            return self
        if isinstance(value, bool):
            any_value = AnyValue(bool_value=value)
        elif isinstance(value, int):
            any_value = AnyValue(int_value=value)
        elif isinstance(value, float):
            any_value = AnyValue(double_value=value)
        else:
            any_value = AnyValue(string_value=truncate(str(value), self.limit))
        self.span.attributes.append(KeyValue(key=key, value=any_value))
        return self

    def times(self, start_ns, end_ns, fallback_ns):
        start = start_ns or end_ns or fallback_ns
        end = end_ns or start
        self.span.start_time_unix_nano = start
        self.span.end_time_unix_nano = max(end, start)
        return self

    def status(self, success, message=None):
        if success is True:
            self.span.status.CopyFrom(Status(code=Status.STATUS_CODE_OK))
        elif success is False:
            self.span.status.CopyFrom(Status(code=Status.STATUS_CODE_ERROR, message=message or "tool failed"))
        return self


def _prompt_tokens(call):
    return call.input_tokens + call.cache_read_tokens + call.cache_write_tokens


def _wrap(spans, config):
    resource = Resource(attributes=[
        KeyValue(key="service.name", value=AnyValue(string_value=SERVICE_NAME)),
        KeyValue(key="openinference.project.name", value=AnyValue(string_value=config.project_name)),
        KeyValue(key="arize.project.name", value=AnyValue(string_value=config.project_name)),
    ])
    scope_spans = ScopeSpans(scope=InstrumentationScope(name=SCOPE_NAME), spans=spans)
    return ExportTraceServiceRequest(resource_spans=[ResourceSpans(resource=resource, scope_spans=[scope_spans])])


def _turn_span(session_id, turn_number, prompt, output, meta, trace_id, config):
    b = _SpanBuilder("Turn %d" % turn_number, trace_id, None, config)
    b.set("openinference.span.kind", "AGENT")
    b.set("session.id", session_id)
    b.set("input.value", prompt)
    b.set("output.value", output)
    b.set("devin.prompt_id", meta.get("prompt_id"))
    b.set("devin.turn_number", turn_number)
    if meta.get("incomplete"):
        b.set("devin.incomplete", True)
    if meta.get("continuation"):
        b.set("devin.continuation", True)
    return b


def _tool_call_summary(tool_calls):
    return "tool_calls: " + ", ".join("%s(%s)" % (tc.name, _to_json(tc.arguments)) for tc in tool_calls)


def build_request(turn, meta, config):
    now = time.time_ns()
    trace_id = os.urandom(16)
    last_text = next((c.output_text for c in reversed(turn.llm_calls) if c.output_text), None)

    turn_b = _turn_span(turn.session_id, turn.turn_number, turn.user_prompt,
                        meta.get("last_assistant_message") or last_text, meta, trace_id, config)
    turn_b.set("devin.working_directory", turn.working_directory)
    turn_b.set("devin.agent_mode", turn.agent_mode)
    prompt_total = sum(_prompt_tokens(c) for c in turn.llm_calls)
    completion_total = sum(c.output_tokens for c in turn.llm_calls)
    turn_b.set("llm.token_count.prompt", prompt_total)
    turn_b.set("llm.token_count.completion", completion_total)
    turn_b.set("llm.token_count.total", prompt_total + completion_total)

    child_spans = []
    for call in turn.llm_calls:
        llm_b = _SpanBuilder(call.model or "llm", trace_id, turn_b.span.span_id, config)
        llm_b.set("openinference.span.kind", "LLM")
        llm_b.set("session.id", turn.session_id)
        llm_b.set("llm.model_name", call.model)
        prompt_tokens = _prompt_tokens(call)
        llm_b.set("llm.token_count.prompt", prompt_tokens)
        llm_b.set("llm.token_count.completion", call.output_tokens)
        llm_b.set("llm.token_count.total", prompt_tokens + call.output_tokens)
        llm_b.set("llm.token_count.prompt_details.cache_read", call.cache_read_tokens)
        llm_b.set("llm.token_count.prompt_details.cache_write", call.cache_write_tokens)
        for i, message in enumerate(call.input_messages):
            llm_b.set("llm.input_messages.%d.message.role" % i, message.get("role"))
            llm_b.set("llm.input_messages.%d.message.content" % i, message.get("content"))
        llm_b.set("llm.output_messages.0.message.role", "assistant")
        llm_b.set("llm.output_messages.0.message.content", call.output_text or None)
        for j, tc in enumerate(call.tool_calls):
            prefix = "llm.output_messages.0.message.tool_calls.%d.tool_call" % j
            llm_b.set(prefix + ".id", tc.id)
            llm_b.set(prefix + ".function.name", tc.name)
            llm_b.set(prefix + ".function.arguments", _to_json(tc.arguments))
        if call.input_messages:
            llm_b.set("input.value", call.input_messages[-1].get("content"))
        llm_b.set("output.value", call.output_text or (_tool_call_summary(call.tool_calls) if call.tool_calls else None))
        llm_b.set("devin.reasoning", call.reasoning or None)
        llm_b.set("devin.finish_reason", call.finish_reason)
        llm_b.set("devin.request_id", call.request_id)
        llm_b.set("devin.ttft_ms", call.ttft_ms)
        llm_b.times(call.started_at_ns, call.ended_at_ns, turn.started_at_ns or now)
        child_spans.append(llm_b.span)

        for tc in call.tool_calls:
            tool_b = _SpanBuilder(tc.name, trace_id, llm_b.span.span_id, config)
            tool_b.set("openinference.span.kind", "TOOL")
            tool_b.set("session.id", turn.session_id)
            tool_b.set("tool.name", tc.name)
            arguments = _to_json(tc.arguments)
            tool_b.set("tool.parameters", arguments)
            tool_b.set("input.value", arguments)
            tool_b.set("output.value", tc.output)
            tool_b.set("devin.tool_call_id", tc.id)
            tool_b.set("devin.tool_success", tc.success)
            tool_b.status(tc.success, tc.failure_reason)
            tool_b.times(tc.started_at_ns, tc.ended_at_ns, llm_b.span.end_time_unix_nano)
            child_spans.append(tool_b.span)

    child_start = min((s.start_time_unix_nano for s in child_spans), default=now)
    child_end = max((s.end_time_unix_nano for s in child_spans), default=now)
    turn_b.times(turn.started_at_ns or child_start, max(turn.ended_at_ns or 0, child_end), now)
    return _wrap([turn_b.span] + child_spans, config)


def build_degraded_request(session_id, turn_number, prompt, started_at_ns, output, meta, config):
    """Turn-only span from hook data, used when sessions.db cannot be read."""
    now = time.time_ns()
    b = _turn_span(session_id, turn_number, prompt, output, meta, os.urandom(16), config)
    b.set("devin.degraded", True)
    b.times(started_at_ns, now, now)
    return _wrap([b.span], config)
