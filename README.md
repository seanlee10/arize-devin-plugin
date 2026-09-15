# Arize Devin Tracing

Trace your [Devin CLI](https://docs.devin.ai/cli) sessions to [Arize AX](https://arize.com) as OpenInference spans.

Each prompt becomes one trace:

```
Turn 3                     AGENT  prompt → final reply, total tokens
├── swe-1-6-slow           LLM    model, prompt/completion/cache-read tokens, reasoning, tool calls
│   ├── read               TOOL   arguments, output, status OK
│   └── read               TOOL   status ERROR (ValidationError)
└── swe-1-6-slow           LLM    final reply
```

All traces from one Devin session share `session.id` (the Devin session name, e.g. `sparkling-bangle`), so Arize shows them as a single session.

## How it works

Devin's hook payloads don't include the model name, token counts, or failed tool calls, but Devin writes every model call and tool result to `~/.local/share/devin/cli/sessions.db` as the turn runs. The plugin registers three hooks:

| Hook | What it does |
|---|---|
| `UserPromptSubmit` | Records the pending turn (prompt, start time). If the previous turn never reached `Stop` (e.g. Ctrl+C), exports it as `devin.incomplete`. |
| `Stop` | Reads the turn from `sessions.db` (read-only), builds the span tree, and sends one OTLP batch. |
| `SessionEnd` | Exports any turn still pending, then removes the session's state file. |

Hooks always exit 0 and never write to stdout, so tracing cannot break or block a Devin session. If `sessions.db` can't be read, a Turn span built from hook data alone is sent, marked `devin.degraded=true`.

## Requirements

- Devin CLI (tested with `3000.10.21`) on macOS or Linux
- Python 3 (tested with 3.13) with `opentelemetry-proto`:
  ```bash
  pip install opentelemetry-proto
  ```
- An Arize AX space ID and API key

## Install

From a local checkout:

```bash
devin plugins install --local /path/to/arize-devin-tracing
```

Install from GitHub (requires repository access):

```bash
devin plugins install seanlee10/arize-devin-tracing
```

GitHub-installed plugins sync across your machines.

Add your credentials to your shell profile (`~/.zshrc` / `~/.bashrc`), then open a new terminal:

```bash
export ARIZE_API_KEY="your-api-key"
export ARIZE_SPACE_ID="your-space-id"
```

Inside Devin, `/arize-devin-tracing:setup-devin-tracing` walks through setup and verification.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ARIZE_API_KEY` | — | Arize AX API key (required) |
| `ARIZE_SPACE_ID` | — | Arize AX space ID (required) |
| `ARIZE_PROJECT_NAME` | `devin-cli` | Arize project name |
| `ARIZE_OTLP_ENDPOINT` | `https://otlp.arize.com/v1/traces` | OTLP/HTTP traces endpoint (for on-prem instances) |
| `ARIZE_TRACE_ENABLED` | `true` | Enable or disable tracing |
| `ARIZE_DRY_RUN` | `false` | Write span summaries to the log instead of sending |
| `ARIZE_VERBOSE` | `false` | Debug logging |
| `ARIZE_LOG_FILE` | `/tmp/arize-devin.log` | Log file (set empty to disable) |
| `ARIZE_MAX_CONTENT_CHARS` | `10000` | Max characters per text attribute |
| `ARIZE_PYTHON` | `python3` | Interpreter used to run the hooks |

## Span attributes

| Span | Attributes |
|---|---|
| Turn (`AGENT`) | `session.id`, `input.value`, `output.value`, `llm.token_count.{prompt,completion,total}` (sum over calls), `devin.prompt_id`, `devin.turn_number`, `devin.working_directory`, `devin.agent_mode`, `devin.incomplete`, `devin.continuation`, `devin.degraded` |
| LLM | `llm.model_name`, `llm.token_count.{prompt,completion,total}`, `llm.token_count.prompt_details.{cache_read,cache_write}`, `llm.input_messages.*`, `llm.output_messages.*` (including tool calls), `input.value`, `output.value`, `devin.reasoning`, `devin.finish_reason`, `devin.request_id`, `devin.ttft_ms` |
| TOOL | `tool.name`, `tool.parameters`, `input.value`, `output.value`, `devin.tool_call_id`, `devin.tool_success`; span status `ERROR` with Devin's failure reason when the call failed |

`llm.token_count.prompt` includes cached tokens (Devin reports them separately from `input_tokens`). A stop hook that makes the agent keep going produces an extra trace with `devin.continuation=true` and the same `devin.prompt_id`.

## Privacy

Spans contain your prompts, model output, reasoning, tool arguments, and tool output (file contents, command output), truncated to `ARIZE_MAX_CONTENT_CHARS`. The log file never contains credentials or environment variables.

## Limitations

- Reads Devin's internal `sessions.db`, whose schema can change between Devin releases. Unrecognized data degrades to Turn-only spans rather than failing.
- Subagent (`run_subagent`) internals and compaction events are not traced yet.
- Arize doesn't price Devin model IDs, so cost columns stay empty. Token counts are accurate.
- Local Devin CLI and Desktop sessions only; plugin hooks don't run in Devin cloud sessions.

## Troubleshooting

```bash
tail -f /tmp/arize-devin.log
ARIZE_DRY_RUN=true ARIZE_VERBOSE=true devin -p -- "hello"
```

See `skills/setup-devin-tracing/SKILL.md` for a symptom/fix table.

## Development

```bash
python3 -m unittest            # from the repo root
```

Tests build synthetic `sessions.db` files with Devin's real schema (`tests/fixtures/make_db.py`) and exercise the hooks end to end in dry-run mode.

## Uninstall

```bash
devin plugins remove arize-devin-tracing
rm -rf ~/.arize-devin
```

## License

MIT
