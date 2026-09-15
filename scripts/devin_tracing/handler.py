"""Hook event handling: UserPromptSubmit, Stop, SessionEnd."""

import time

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
    if event == "UserPromptSubmit":
        _on_prompt(state, session_id, payload, config)
    elif event == "Stop":
        _export_turn(state, session_id, payload.get("prompt_id"), payload.get("last_assistant_message"),
                     incomplete=False, config=config)
    elif event == "SessionEnd":
        if state.pending:
            _export_turn(state, session_id, state.pending.get("prompt_id"), None, incomplete=True, config=config)
        state.delete()
        gc_stale(config.state_dir, STATE_MAX_AGE_SECONDS)
    else:
        log.debug("ignoring event %s" % event)


def _on_prompt(state, session_id, payload, config):
    prompt_id = payload.get("prompt_id")
    if state.pending and state.pending.get("prompt_id") != prompt_id:
        log.info("turn %s in session %s ended without Stop; exporting as incomplete"
                 % (state.pending.get("prompt_id"), session_id))
        _export_turn(state, session_id, state.pending.get("prompt_id"), None, incomplete=True, config=config)
    state.turn_count += 1
    prompt = payload.get("prompt")
    state.pending = {"prompt_id": prompt_id, "prompt": prompt if isinstance(prompt, str) else None,
                     "started_at_ns": time.time_ns()}
    state.save()


def _export_turn(state, session_id, prompt_id, last_assistant_message, incomplete, config):
    """Build and send the current turn. State is saved before the network call
    so a hook timeout during export can never cause a duplicate export."""
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
    state.save()
    export(request, config)
