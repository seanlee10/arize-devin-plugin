---
name: setup-devin-tracing
description: Set up, verify, or troubleshoot Arize AX tracing for Devin CLI sessions. Use when the user wants to enable/disable tracing, configure Arize credentials or project name, check that traces are arriving, or debug missing traces.
---

# Set up Arize AX tracing for Devin CLI

The `arize-devin-tracing` plugin exports every Devin CLI turn to Arize AX from its `UserPromptSubmit`, `Stop`, and `SessionEnd` hooks. Hooks inherit the shell environment Devin was started from, so all configuration is environment variables.

Work through these steps in order. Run commands yourself where you can, and report results to the user.

## 1. Check Python dependencies

```bash
${ARIZE_PYTHON:-python3} -c "import opentelemetry.proto, google.protobuf; print('ok')"
```

If this fails, install into that interpreter:

```bash
${ARIZE_PYTHON:-python3} -m pip install opentelemetry-proto
```

If the user keeps dependencies in a virtualenv, set `ARIZE_PYTHON` to that venv's `python` (absolute path).

## 2. Check credentials

Check whether the variables are set. **Never print their values.**

```bash
[ -n "$ARIZE_API_KEY" ] && echo "ARIZE_API_KEY: set" || echo "ARIZE_API_KEY: NOT set"
[ -n "$ARIZE_SPACE_ID" ] && echo "ARIZE_SPACE_ID: set" || echo "ARIZE_SPACE_ID: NOT set"
```

If they are missing, ask the user for their Arize **Space ID** and **API key** (Arize UI → Space Settings → API Keys). Tell them to add these lines to their shell profile (`~/.zshrc` or `~/.bashrc`) themselves, so the key never passes through the conversation:

```bash
export ARIZE_API_KEY="..."
export ARIZE_SPACE_ID="..."
export ARIZE_PROJECT_NAME="devin-cli"   # optional
```

Then have them open a new terminal before starting `devin`.

Optional settings:

| Variable | Default | Purpose |
|---|---|---|
| `ARIZE_PROJECT_NAME` | `devin-cli` | Arize project to write to |
| `ARIZE_OTLP_ENDPOINT` | `https://otlp.arize.com/v1/traces` | On-prem / regional OTLP HTTP endpoint |
| `ARIZE_TRACE_ENABLED` | `true` | Set `false` to turn tracing off |
| `ARIZE_DRY_RUN` | `false` | Log spans locally instead of sending |
| `ARIZE_VERBOSE` | `false` | Debug logging |
| `ARIZE_LOG_FILE` | `/tmp/arize-devin.log` | Log path (empty disables) |
| `ARIZE_MAX_CONTENT_CHARS` | `10000` | Truncation per attribute |

## 3. Verify with a dry run

```bash
ARIZE_DRY_RUN=true ARIZE_VERBOSE=true devin -p -- "Say hello in one word."
grep "DRY RUN span" /tmp/arize-devin.log | tail -5
```

Expect one `AGENT` span named `Turn N` and at least one `LLM` span. If nothing is logged, go to Troubleshooting.

## 4. Verify a real export

```bash
ARIZE_VERBOSE=true devin -p -- "Say hello in one word."
tail -5 /tmp/arize-devin.log
```

A line `exported N spans to …` means Arize accepted the batch. The trace appears in the Arize project within a minute or two. Filter by `session.id` using the Devin session name shown by `devin list`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Log file never created | Check the plugin is installed and its hooks are loaded: `devin plugins info arize-devin-tracing`, or `/hooks` inside Devin. Check `ARIZE_TRACE_ENABLED` is not `false`. |
| `No module named 'opentelemetry'` | Step 1 (wrong interpreter → set `ARIZE_PYTHON`) |
| `ARIZE_API_KEY and ARIZE_SPACE_ID must be set` | Variables not exported in the shell that launched `devin` (step 2) |
| `HTTP 401` / `HTTP 403` | Wrong API key or space ID |
| `devin.degraded=true` on turn spans | Devin's `sessions.db` could not be read or its schema changed. Report the Devin version (`devin version`). |
| Cost columns empty | Arize has no pricing for Devin's model IDs (e.g. `swe-1-6-slow`). Token counts are still correct. |
