"""Read one turn of a Devin CLI session from `sessions.db`.

This is the only module that knows Devin's internal storage schema
(verified against Devin CLI 3000.10.21). Every field access tolerates absence
so that schema drift degrades output instead of crashing.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from . import log


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: object
    output: Optional[str] = None
    success: Optional[bool] = None
    failure_reason: Optional[str] = None
    started_at_ns: Optional[int] = None
    ended_at_ns: Optional[int] = None


@dataclass
class LlmCall:
    request_id: Optional[str]
    model: Optional[str]
    started_at_ns: Optional[int]
    ended_at_ns: Optional[int]
    output_text: str
    reasoning: Optional[str]
    finish_reason: Optional[str]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    ttft_ms: Optional[int]
    input_messages: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)


@dataclass
class Turn:
    session_id: str
    turn_number: int
    user_prompt: Optional[str]
    started_at_ns: Optional[int]
    ended_at_ns: Optional[int]
    head_node_id: int
    working_directory: Optional[str]
    agent_mode: Optional[str]
    llm_calls: list = field(default_factory=list)


def parse_ts(value):
    """ISO-8601 timestamp -> integer nanoseconds since epoch (None if unparseable)."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = int(dt.timestamp())
    return seconds * 1_000_000_000 + dt.microsecond * 1_000


def _int(value):
    return value if isinstance(value, int) else 0


def _meta(msg):
    meta = msg.get("metadata")
    return meta if isinstance(meta, dict) else {}


def _extensions(msg):
    ext = _meta(msg).get("extensions")
    return ext if isinstance(ext, dict) else {}


def _is_user_input(msg):
    return msg.get("role") == "user" and _meta(msg).get("is_user_input") is True


def _read_chain(conn, session_id):
    row = conn.execute(
        "SELECT main_chain_id, working_directory, agent_mode FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    head, working_directory, agent_mode = row

    nodes = {}
    for node_id, parent_id, body in conn.execute(
        "SELECT node_id, parent_node_id, chat_message FROM message_nodes WHERE session_id = ?", (session_id,)
    ):
        nodes[node_id] = (parent_id, body)

    chain = []
    seen = set()
    current = head
    while current is not None and current in nodes and current not in seen:
        seen.add(current)
        parent_id, body = nodes[current]
        try:
            msg = json.loads(body)
        except (TypeError, ValueError):
            log.warn("skipping malformed message node %s in session %s" % (current, session_id))
            msg = None
        if msg is not None and not isinstance(msg, dict):
            msg = None
        chain.append((current, msg))
        current = parent_id
    chain.reverse()
    return chain, {"head": head, "working_directory": working_directory, "agent_mode": agent_mode}


def _build_llm_call(msg, input_messages):
    meta = _meta(msg)
    metrics = meta.get("metrics") if isinstance(meta.get("metrics"), dict) else {}
    thinking = msg.get("thinking")
    reasoning = thinking.get("thinking") if isinstance(thinking, dict) else None
    tool_calls = []
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict) and tc.get("id"):
            tool_calls.append(ToolCall(id=tc["id"], name=tc.get("name") or "unknown", arguments=tc.get("arguments")))
    ttft = metrics.get("ttft_ms")
    return LlmCall(
        request_id=meta.get("request_id"),
        model=meta.get("generation_model"),
        started_at_ns=parse_ts(meta.get("started_generation_at")),
        ended_at_ns=parse_ts(meta.get("created_at")),
        output_text=msg.get("content") if isinstance(msg.get("content"), str) else "",
        reasoning=reasoning,
        finish_reason=meta.get("finish_reason"),
        input_tokens=_int(metrics.get("input_tokens")),
        output_tokens=_int(metrics.get("output_tokens")),
        cache_read_tokens=_int(metrics.get("cache_read_tokens")),
        cache_write_tokens=_int(metrics.get("cache_creation_tokens")),
        ttft_ms=ttft if isinstance(ttft, int) else None,
        input_messages=input_messages,
        tool_calls=tool_calls,
    )


def _apply_tool_result(tool_call, msg):
    ext = _extensions(msg)
    result_meta = ext.get("chisel/tool_result_meta") if isinstance(ext.get("chisel/tool_result_meta"), dict) else {}
    timing = ext.get("chisel/tool_call_timing") if isinstance(ext.get("chisel/tool_call_timing"), dict) else {}
    failure = ext.get("chisel/tool_failure") if isinstance(ext.get("chisel/tool_failure"), dict) else {}
    created = parse_ts(_meta(msg).get("created_at"))

    tool_call.output = msg.get("content") if isinstance(msg.get("content"), str) else None
    success = result_meta.get("success")
    tool_call.success = success if isinstance(success, bool) else (False if failure else None)
    tool_call.failure_reason = result_meta.get("failure_reason") or failure.get("reason")
    tool_call.started_at_ns = parse_ts(timing.get("started_at")) or created
    tool_call.ended_at_ns = parse_ts(timing.get("finished_at")) or created


def load_turn(db_path, session_id, after_node_id):
    """Return the Turn for nodes after the latest user prompt (or after
    `after_node_id` if that is later), or None if there is nothing to export."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=2)
    except sqlite3.Error as e:
        log.warn("cannot open sessions db %s: %s" % (db_path, e))
        return None
    try:
        chain, info = _read_chain(conn, session_id)
    except sqlite3.Error as e:
        log.warn("cannot read sessions db %s: %s" % (db_path, e))
        return None
    finally:
        conn.close()
    if not chain:
        return None

    boundary = -1
    user_prompt = None
    user_ts = None
    turn_number = 0
    for index, (node_id, msg) in enumerate(chain):
        if msg is not None and _is_user_input(msg):
            boundary = index
            turn_number += 1
            user_prompt = msg.get("content") if isinstance(msg.get("content"), str) else None
            user_ts = parse_ts(_meta(msg).get("created_at"))
        if node_id == after_node_id:
            boundary = index
            user_ts = None

    llm_calls = []
    pending_inputs = []
    tool_calls_by_id = {}
    last_ts = None
    for node_id, msg in chain[boundary + 1:]:
        if msg is None:
            continue
        role = msg.get("role")
        if role == "assistant":
            call = _build_llm_call(msg, pending_inputs)
            pending_inputs = []
            llm_calls.append(call)
            for tc in call.tool_calls:
                tool_calls_by_id[tc.id] = tc
            last_ts = call.ended_at_ns or last_ts
        elif role == "tool":
            entry = {"role": "tool", "content": msg.get("content") if isinstance(msg.get("content"), str) else ""}
            if msg.get("tool_call_id"):
                entry["tool_call_id"] = msg["tool_call_id"]
            pending_inputs.append(entry)
            tool_call = tool_calls_by_id.get(msg.get("tool_call_id"))
            if tool_call is not None:
                _apply_tool_result(tool_call, msg)
                last_ts = tool_call.ended_at_ns or last_ts
        elif role == "user":
            content = msg.get("content")
            pending_inputs.append({"role": "user", "content": content if isinstance(content, str) else ""})

    # The boundary user prompt is the first input of the first call.
    if boundary >= 0 and chain[boundary][1] is not None and _is_user_input(chain[boundary][1]) and llm_calls:
        llm_calls[0].input_messages.insert(0, {"role": "user", "content": user_prompt or ""})

    if not llm_calls:
        return None

    started = user_ts or llm_calls[0].started_at_ns
    return Turn(
        session_id=session_id,
        turn_number=turn_number,
        user_prompt=user_prompt,
        started_at_ns=started,
        ended_at_ns=last_ts,
        head_node_id=info["head"],
        working_directory=info["working_directory"],
        agent_mode=info["agent_mode"],
        llm_calls=llm_calls,
    )
