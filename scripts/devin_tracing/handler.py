"""Hook event handling: UserPromptSubmit, Stop, SessionEnd."""

import base64
import time

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from google.protobuf.message import DecodeError

from . import log
from .devin_db import load_turn
from .export import export
from .spans import build_degraded_request, build_request
from .state import SessionState, gc_stale

STATE_MAX_AGE_SECONDS = 7 * 86400


def handle(event, payload, config):
    if not config.enabled:
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        log.warn("%s payload has no session_id; skipping" % event)
        return

    state = SessionState.load(config.state_dir, session_id)
    export_ready = _flush_pending_exports(state, config)
    if event == "UserPromptSubmit":
        _on_prompt(state, session_id, payload, config, export_ready)
    elif event == "Stop":
        _export_turn(state, session_id, payload.get("prompt_id"), payload.get("last_assistant_message"),
                     incomplete=False, config=config, flush=export_ready)
    elif event == "SessionEnd":
        if state.pending:
            _export_turn(state, session_id, state.pending.get("prompt_id"), None, incomplete=True,
                         config=config, flush=export_ready)
        if state.pending_exports:
            state.save()
        else:
            state.delete()
        gc_stale(config.state_dir, STATE_MAX_AGE_SECONDS)
    else:
        log.debug("ignoring event %s" % event)


def _on_prompt(state, session_id, payload, config, export_ready):
    prompt_id = payload.get("prompt_id")
    if state.pending and state.pending.get("prompt_id") != prompt_id:
        log.info("turn %s in session %s ended without Stop; exporting as incomplete"
                 % (state.pending.get("prompt_id"), session_id))
        _export_turn(state, session_id, state.pending.get("prompt_id"), None, incomplete=True,
                     config=config, flush=export_ready)
    state.turn_count += 1
    prompt = payload.get("prompt")
    state.pending = {"prompt_id": prompt_id, "prompt": prompt if isinstance(prompt, str) else None,
                     "started_at_ns": time.time_ns()}
    state.save()


def _export_turn(state, session_id, prompt_id, last_assistant_message, incomplete, config, flush=True):
    """Queue and send the current turn without losing it on export failure."""
    pending = state.pending
    turn = load_turn(config.sessions_db, session_id, state.last_exported_node_id)
    meta = {
        "prompt_id": prompt_id,
        "last_assistant_message": last_assistant_message,
        "incomplete": incomplete,
        "continuation": pending is None and prompt_id is not None and prompt_id == state.last_exported_prompt_id,
    }

    if turn is not None:
        request = build_request(turn, meta, config)
        state.last_exported_node_id = turn.head_node_id
    elif pending is not None:
        log.warn("no turn data in sessions db for session %s; sending degraded turn span" % session_id)
        request = build_degraded_request(session_id, state.turn_count or 1, pending.get("prompt"),
                                         pending.get("started_at_ns"), last_assistant_message, meta, config)
    else:
        log.debug("nothing to export for session %s" % session_id)
        return

    state.pending = None
    state.last_exported_prompt_id = prompt_id
    state.pending_exports.append(base64.b64encode(request.SerializeToString()).decode("ascii"))
    state.save()
    if flush:
        _flush_pending_exports(state, config)


def _flush_pending_exports(state, config):
    while state.pending_exports:
        encoded = state.pending_exports[0]
        try:
            request = ExportTraceServiceRequest.FromString(base64.b64decode(encoded, validate=True))
        except (ValueError, TypeError, DecodeError):
            log.error("discarding corrupt pending trace export")
            state.pending_exports.pop(0)
            state.save()
            continue
        if not export(request, config):
            return False
        state.pending_exports.pop(0)
        state.save()
    return True
