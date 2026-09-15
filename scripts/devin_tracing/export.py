"""Send an ExportTraceServiceRequest to Arize AX over OTLP/HTTP (protobuf)."""

import json
import urllib.error
import urllib.request

from . import log

TIMEOUT_SECONDS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DRY_RUN_VALUE_CHARS = 200


def _attribute_value(any_value):
    field = any_value.WhichOneof("value")
    value = getattr(any_value, field) if field else None
    if isinstance(value, str) and len(value) > DRY_RUN_VALUE_CHARS:
        value = value[:DRY_RUN_VALUE_CHARS] + "…"
    return value


def _log_dry_run(request):
    status_names = {0: "UNSET", 1: "OK", 2: "ERROR"}
    for resource_spans in request.resource_spans:
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                attributes = {kv.key: _attribute_value(kv.value) for kv in span.attributes}
                log.info("DRY RUN span " + json.dumps({
                    "name": span.name,
                    "kind": attributes.get("openinference.span.kind"),
                    "trace_id": span.trace_id.hex(),
                    "span_id": span.span_id.hex(),
                    "parent": span.parent_span_id.hex() or None,
                    "status": status_names.get(span.status.code, str(span.status.code)),
                    "start_ns": span.start_time_unix_nano,
                    "end_ns": span.end_time_unix_nano,
                    "attributes": attributes,
                }, ensure_ascii=False))


def _span_count(request):
    return sum(len(ss.spans) for rs in request.resource_spans for ss in rs.scope_spans)


def export(request, config):
    """Returns True on success (or dry run). Never raises."""
    if config.dry_run:
        _log_dry_run(request)
        return True
    if not config.has_credentials:
        log.error("ARIZE_API_KEY and ARIZE_SPACE_ID must be set (or ARIZE_DRY_RUN=true); spans not sent")
        return False

    body = request.SerializeToString()
    headers = {"Content-Type": "application/x-protobuf", "space_id": config.space_id, "api_key": config.api_key}

    for attempt in (1, 2):
        http_request = urllib.request.Request(config.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(http_request, timeout=TIMEOUT_SECONDS) as response:
                response.read()
            log.debug("exported %d spans to %s" % (_span_count(request), config.endpoint))
            return True
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode("utf-8", "replace")
            log.error("export failed: HTTP %d from %s: %s" % (e.code, config.endpoint, detail))
            if e.code not in RETRYABLE_STATUS:
                return False
        except (urllib.error.URLError, OSError) as e:
            log.error("export failed (attempt %d): %s" % (attempt, e))
    return False
