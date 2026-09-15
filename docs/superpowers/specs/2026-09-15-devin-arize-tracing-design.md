# Arize AX Tracing for Devin CLI — Design

**Date:** 2026-09-15
**Status:** Approved design, pending implementation plan
**Target:** Devin CLI `3000.10.21` (macOS / Linux), Arize AX only

## Goal

Automatically trace every local Devin CLI session to Arize AX as OpenInference spans. This is the Devin counterpart of Arize's `claude-code-tracing` plugin, and it goes further: one LLM span per model call with real token counts, plus tool spans with accurate error status. It ships as a Devin plugin installable with `devin plugins install`.

## Non-goals (v1)

- Phoenix or any backend other than Arize AX (cloud or on-prem OTLP endpoint)
- Subagent (`run_subagent`) traces
- Compaction spans (`PostCompaction`)
- Devin cloud sessions (plugin hooks run only in local CLI and Desktop sessions)
- Windows

## Verified facts (spike, 2026-09-15)

A throwaway logging plugin, installed with `--local`, ran against `devin -p` and `devin -c -p` sessions:

1. **Plugin layout works:** `.devin-plugin/plugin.json` plus a root `hooks.json` in the same format as `.devin/hooks.v1.json` (event names as top-level keys). Install requires `--yes` when run non-interactively.
2. **Plugin root:** hook processes get `DEVIN_PLUGIN_ROOT` (and `CLAUDE_PLUGIN_ROOT`) pointing at the cached copy under `~/.local/share/devin/cli/plugins/cache/…/<version>/`. Commands run through a shell, so `${VAR:-default}` expansion works. `DEVIN_PROJECT_DIR` is also set.
3. **Environment:** the user's shell environment (e.g. `ARIZE_*`) is inherited by hooks.
4. **Session identity:** the hook `session_id` is Devin's session name (e.g. `typical-armadillo`), not a UUID. It equals `sessions.id` in `~/.local/share/devin/cli/sessions.db`.
5. **Payloads observed:**
   - `SessionStart`: `session_id`, `source` (fires again on `-c` resume with the same `session_id`)
   - `UserPromptSubmit`: `session_id`, `prompt_id`, `prompt`
   - `PreToolUse` / `PostToolUse`: `tool_name`, `tool_input`, `tool_use_id`, `tool_response {success, output, error}`
   - `Stop`: `session_id`, `prompt_id`, `stop_hook_active`, `last_assistant_message`
   - `SessionEnd`: `session_id`, `prompt_id`, `reason`
6. **Tool hooks are unreliable for status:** a tool call that failed validation (reading a missing file) fired `PreToolUse` but **not** `PostToolUse`. `exec` of a failing command reports `success: true`, and the exit code appears only in the output text.
7. **JSON transcripts** (`transcripts/*.json`, ATIF format) are **not** written automatically. Do not depend on them.
8. **`sessions.db` is written incrementally.** When `Stop` fires, `sessions.main_chain_id` already points at the turn's final assistant node.

### `sessions.db` structure used

- `sessions(id, working_directory, model, agent_mode, main_chain_id, created_at, …)`: `main_chain_id` is the `node_id` of the current head.
- `message_nodes(session_id, node_id, parent_node_id, chat_message TEXT JSON, created_at)`: a forest. The live conversation is the chain from the head following `parent_node_id`. Other branches (e.g. the pre-commit copy of an assistant node) are ignored automatically by walking only this chain.
- `chat_message` JSON:
  - common: `message_id`, `role` (`system|user|assistant|tool`), `content`, `metadata.created_at` (ISO-8601 UTC, µs)
  - user: `metadata.is_user_input: true`
  - assistant: `tool_calls[] {id, name, arguments}`, `thinking.thinking`, `metadata.generation_model`, `metadata.started_generation_at`, `metadata.finish_reason`, `metadata.request_id`, `metadata.metrics {input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, ttft_ms, total_time_ms}`
    - `input_tokens` **excludes** cached tokens. Observed: `num_tokens_preceding` = `input_tokens + cache_read_tokens`.
  - tool: `tool_call_id`, `content` (result text), `metadata.extensions["chisel/tool_result_meta"] {success, failure_reason?, kind}`, `["chisel/tool_call_timing"] {started_at, finished_at, duration_ms}` (absent on validation failures), `["chisel/tool_failure"] {reason}`

This schema is internal and undocumented. All knowledge of it lives in one module (`devin_db.py`), and every field access tolerates absence.

## Architecture

Collect at turn start; build and send everything at `Stop`.

```
UserPromptSubmit ──► state: pending turn {prompt_id, prompt, started_at}
Stop ─────────────► read sessions.db ─► build spans ─► durable outbox ─► OTLP export
SessionEnd ───────► queue pending turn (marked incomplete) ─► retain state until outbox drains
```

`PreToolUse`/`PostToolUse` are **not** registered. The database is the source of truth for tools (fact 6), and skipping those hooks avoids starting Python on every tool call.

### Repository layout (repo root = the plugin)

```
arize-devin-plugin/
├── .devin-plugin/plugin.json          # name: arize-devin-tracing
├── hooks.json
├── scripts/
│   ├── hook.py                        # entry point: python3 hook.py <Event> (stdin parsing, never raises)
│   └── devin_tracing/
│       ├── __init__.py
│       ├── config.py                  # env var parsing
│       ├── log.py                     # file logging (never logs env or secrets)
│       ├── state.py                   # per-session state file, atomic writes
│       ├── devin_db.py                # sessions.db reader -> Turn model
│       ├── handler.py                 # event handling (UserPromptSubmit / Stop / SessionEnd)
│       ├── spans.py                   # Turn model -> OTLP protobuf spans
│       └── export.py                  # OTLP/HTTP export, dry run
├── skills/setup-devin-tracing/SKILL.md
├── tests/                             # stdlib unittest, fixtures
├── docs/superpowers/specs/…
└── README.md
```

### hooks.json

```json
{
  "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "timeout": 10,
    "command": "${ARIZE_PYTHON:-python3} \"$DEVIN_PLUGIN_ROOT/scripts/hook.py\" UserPromptSubmit"}]}],
  "Stop":             [{"matcher": "", "hooks": [{"type": "command", "timeout": 15,
    "command": "${ARIZE_PYTHON:-python3} \"$DEVIN_PLUGIN_ROOT/scripts/hook.py\" Stop"}]}],
  "SessionEnd":       [{"matcher": "", "hooks": [{"type": "command", "timeout": 15,
    "command": "${ARIZE_PYTHON:-python3} \"$DEVIN_PLUGIN_ROOT/scripts/hook.py\" SessionEnd"}]}]
}
```

`hook.py` always exits 0 and prints nothing to stdout, because Devin interprets hook stdout as control JSON.

## Components

### config.py

| Variable | Default | Meaning |
|---|---|---|
| `ARIZE_TRACE_ENABLED` | `true` | If not `true`, hooks return immediately |
| `ARIZE_API_KEY` | — | Required unless dry run |
| `ARIZE_SPACE_ID` | — | Required unless dry run |
| `ARIZE_OTLP_ENDPOINT` | `https://otlp.arize.com/v1/traces` | OTLP/HTTP traces URL (on-prem override) |
| `ARIZE_PROJECT_NAME` | `devin-cli` | Arize project |
| `ARIZE_DRY_RUN` | `false` | Write span summary JSON to the log instead of sending |
| `ARIZE_VERBOSE` | `false` | Debug-level log lines |
| `ARIZE_LOG_FILE` | `/tmp/arize-devin.log` | Empty string disables logging |
| `ARIZE_PYTHON` | `python3` | Interpreter used by hooks.json |
| `ARIZE_MAX_CONTENT_CHARS` | `10000` | Per-attribute truncation limit |
| `DEVIN_SESSIONS_DB` | `~/.local/share/devin/cli/sessions.db` | Override for tests |
| `ARIZE_DEVIN_STATE_DIR` | `~/.arize-devin` | State directory |

Missing credentials without dry run produce one error log line; nothing is sent.

### state.py

- File: `$ARIZE_DEVIN_STATE_DIR/<session_id>.json`. Writes go to a temp file followed by `os.replace` (atomic). Hooks within a session run sequentially, so no locking is needed.
- Shape: `{"turn_count": int, "last_exported_node_id": int|null, "last_exported_prompt_id": str|null, "pending": {"prompt_id", "prompt", "started_at_ns"}|null, "pending_exports": [base64_protobuf]}`
- Pending exports keep their original trace and span IDs and are retried oldest-first on each hook. A failed retry does not block the Devin event or discard newer queued turns.
- `continuation` is true when `Stop` fires with no pending turn and the same `prompt_id` as the last export.
- `session_id` is sanitized to `[A-Za-z0-9._-]` before use in a path.
- On `SessionEnd`, an empty state file is deleted. State with pending exports is retained; files not modified for more than 7 days are garbage-collected.

### devin_db.py

`load_turn(db_path, session_id, after_node_id) -> Turn | None`

1. Open read-only (`file:…?mode=ro`, `uri=True`), 2s busy timeout.
2. Read `main_chain_id` and the session row (`working_directory`, `model`, `agent_mode`).
3. Load the session's nodes into a dict, walk from the head via `parent_node_id` into a list, then reverse it.
4. Turn boundary: take nodes after the **latest** node that is either `metadata.is_user_input == true` or `node_id == after_node_id`, whichever comes later in the chain. Include the user node itself when it is the boundary.
5. Build the model:
   - `Turn {session_id, turn_number, user_prompt, started_at, ended_at, head_node_id, working_directory, agent_mode, llm_calls: [LlmCall]}`
     - `turn_number` = count of `is_user_input` nodes in the chain up to and including the boundary. It is derived from the DB so it stays correct across `devin -c` resumes, where the state file was deleted at the previous `SessionEnd`. `state.turn_count` is used only in the degraded fallback.
   - `LlmCall {request_id, model, started_at, ended_at, input_messages, output_text, reasoning, finish_reason, tokens {input, output, cache_read, cache_write}, ttft_ms, tool_calls: [ToolCall]}`
   - `ToolCall {id, name, arguments, output, success, failure_reason, started_at, ended_at}`
   - `input_messages` for a call are the user, tool, and system messages between the previous assistant node and this one.
   - Each tool result node attaches to the `ToolCall` whose `id == tool_call_id`. Timing comes from `chisel/tool_call_timing`, with fallback to the result node's `created_at` for both ends. A tool call with no result node gets `success=None`, `output=None`.
6. Returns `None` if the session or head is missing or there are no assistant nodes after the boundary. Malformed JSON nodes are skipped with a warning.

### spans.py

`build_request(turn, meta, config) -> ExportTraceServiceRequest`, where `meta = {prompt_id, last_assistant_message, incomplete: bool, continuation: bool}`. The turn number comes from `turn.turn_number`.

- One new random 128-bit trace id per exported turn. Span ids are random 64-bit.
- **Resource attributes:** `service.name=devin-cli`, `openinference.project.name=<project>`, `arize.project.name=<project>`.
- **Turn span** `Turn <n>`: kind AGENT, spans `turn.started_at → turn.ended_at`
  - `openinference.span.kind=AGENT`, `session.id`, `input.value=user_prompt`, `output.value=last_assistant_message` (falls back to the last `output_text`), `devin.prompt_id`, `devin.turn_number`, `devin.working_directory`, `devin.agent_mode`, `devin.incomplete` (only when true)
  - `llm.token_count.*` aggregated across calls
- **LLM span** `<model>`: kind LLM, child of Turn, timed `started_generation_at → created_at`
  - `llm.model_name`, `llm.token_count.prompt = input + cache_read + cache_write`, `llm.token_count.completion`, `llm.token_count.total`, `llm.token_count.prompt_details.cache_read`, `llm.token_count.prompt_details.cache_write`
  - `llm.input_messages.{i}.message.role/content` (from `input_messages`)
  - `llm.output_messages.0.message.role=assistant`, `.content`, `.tool_calls.{j}.tool_call.function.name/arguments` (JSON)
  - `input.value` (last input message), `output.value` (output text, or a tool-call summary)
  - `devin.reasoning` (thinking text), `devin.finish_reason`, `devin.request_id`, `devin.ttft_ms`
- **Tool span** `<tool name>`: kind TOOL, child of Turn. Tool-call IDs correlate it with the requesting LLM without placing its execution interval outside the completed LLM span.
  - `tool.name`, `tool.parameters` (JSON), `input.value` (JSON args), `output.value`
  - Status `ERROR` with message `failure_reason` when `success is False`, otherwise `OK`. Also `devin.tool_success`.
- `session.id` goes on every span.
- All string attributes are truncated to `ARIZE_MAX_CONTENT_CHARS`, with `…[truncated N chars]` appended.

### export.py

- Serialize the request to protobuf and `POST` it with `urllib.request` to `ARIZE_OTLP_ENDPOINT`, using headers `Content-Type: application/x-protobuf`, `space_id`, `api_key`. Timeout 5s.
- Retry once on connection error or HTTP 5xx or 429. Log anything other than 2xx with status and at most 500 bytes of the response body. Never raise.
- Dry run: log one JSON line per span (`name`, `kind`, `trace_id`, `span_id`, `parent`, `status`, attribute keys and truncated values) and send nothing.
- Log files are opened without following symlinks and forced to mode `0600` because dry-run output contains trace content.
- Dependency: `opentelemetry-proto` (brings `protobuf`). No `grpcio`. If the import fails, log once: `pip install opentelemetry-proto`.

### hook.py (event handling)

Read stdin JSON. If tracing is disabled or input is invalid, exit 0. Wrap everything in `try/except Exception` and log the traceback.

- **UserPromptSubmit:** load state. If `pending` is set with a different `prompt_id` (the previous turn never got `Stop`, e.g. Ctrl+C), export that turn first with `incomplete=true`. Then set `pending = {prompt_id, prompt, started_at_ns=now}` and increment `turn_count`.
- **Every hook:** retry serialized requests in `pending_exports`, oldest first. Keep a failed request for the next hook.
- **Stop:** `turn = load_turn(db, session_id, state.last_exported_node_id)`.
  - If a turn is found: atomically save the serialized request in `pending_exports` together with `last_exported_node_id = turn.head_node_id` and `pending = null`, then attempt export. Retrying reuses the original trace and span IDs.
  - **Fallback** (no DB, schema mismatch, or no turn): if `pending` exists, export a Turn-only span built from pending plus `last_assistant_message`, with `devin.degraded=true`, and clear `pending`.
  - Re-firing: when a blocking stop hook makes the agent continue, `Stop` fires again. The `after_node_id` boundary then exports only the continuation, as a new trace with the same `devin.prompt_id` and `devin.continuation=true`.
- **SessionEnd:** if `pending` is set, run the Stop logic with `incomplete=true`. Delete the state file only after the outbox is empty, then GC old state files.

## Error handling summary

| Failure | Behavior |
|---|---|
| Python deps missing | One log line per hook invocation; Devin unaffected |
| Credentials missing | Log error; queue retained for next hook |
| DB locked, missing, or schema changed | Degraded Turn span from hook data |
| Malformed node JSON | Skip node, warn |
| Network or HTTP error | One retry, then log; serialized request retained |
| Any unexpected exception | Traceback to log, exit 0 |
| Hook timeout (Devin-side) | Devin kills the hook and fails open; the next event retries the saved request with the same IDs |

**Privacy:** spans and pending export state contain prompts, model output, reasoning, tool arguments and tool output, the same as the Claude Code plugin. State and logs use user-only permissions. Logs never contain environment variables or credentials.

## Testing

- **Framework:** stdlib `unittest` (`python3 -m unittest discover tests`). Tests that need `opentelemetry-proto` are skipped if it is not installed.
- **Fixtures:** `tests/fixtures/make_db.py` builds a `sessions.db` with the real schema (DDL copied from Devin 3000.10.21) and synthetic content. Scenarios:
  1. single call, no tools
  2. parallel tool calls, one success and one `ValidationError` failure (no timing extension)
  3. multi-turn session (second turn must exclude first-turn nodes)
  4. forked chain (duplicate assistant node on a side branch must be ignored)
  5. malformed node JSON
  6. missing session / `main_chain_id` null
- **Unit tests:**
  - `devin_db`: every scenario above
  - `spans`: hierarchy (parents), token math, status codes, truncation, attribute presence
  - `state`: atomic write, sanitization, pending flush rules
  - `export`: dry-run output, header construction, retry on 5xx (local `http.server` stub)
  - `hook.py` end to end with the fixture DB plus dry run, including the Stop re-fire and SessionEnd-without-Stop flows
- **Manual verification:** install with `devin plugins install --local --yes .`
  1. `ARIZE_DRY_RUN=true devin -p "…"` and confirm the log shows the Turn → LLM → TOOL tree
  2. A real run against Arize AX, confirming the trace appears in project `devin-cli` with token counts and a failed-tool ERROR span

## Open risks

- **`sessions.db` schema is internal** and may change in any Devin release. Mitigation: isolated reader, tolerant parsing, degraded fallback, and the tested Devin version recorded in the README.
- **Model pricing:** Devin model ids (e.g. `swe-1-6-slow`) may not be priced by Arize, so token counts will be correct but cost may be empty.
- **Session name collisions:** `session.id` is a human-readable name that could repeat across machines. Acceptable for v1.
