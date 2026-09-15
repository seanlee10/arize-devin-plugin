# Repository Guidelines

## Project Structure & Module Organization

This Python plugin exports Devin CLI turns to Arize AX as OpenInference spans over OTLP/HTTP.

- `scripts/hook.py` is the hook entry point; `hooks.json` registers `UserPromptSubmit`, `Stop`, and `SessionEnd`.
- `scripts/devin_tracing/` separates configuration, logging, SQLite reads, session state, span construction, export, and event handling.
- `tests/` contains unit and integration tests; `tests/fixtures/make_db.py` builds synthetic Devin databases.
- `.devin-plugin/` holds plugin metadata; `skills/setup-devin-tracing/` contains the setup workflow.
- `README.md` documents usage; `docs/superpowers/specs/` records the design.

## Build, Test, and Development Commands

Run commands from the repository root:

- `python3 -m pip install opentelemetry-proto`: install the runtime dependency.
- `python3 -m unittest`: run the complete test suite.
- `python3 -m unittest tests.test_handler`: run hook lifecycle tests only.
- `devin plugins install --local "$PWD"`: install this checkout in Devin.
- `ARIZE_DRY_RUN=true ARIZE_VERBOSE=true devin -p -- "hello"`: exercise tracing locally and inspect `/tmp/arize-devin.log`.

There is no separate build step. Python 3.13 is the documented tested interpreter.

## Coding Style & Naming Conventions

Follow existing Python style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Keep modules focused on their existing responsibilities. Use short docstrings and dataclasses for structured records where appropriate. No formatter or linter configuration is currently provided.

## Testing Guidelines

Use standard-library `unittest`, `test_*.py` files, and descriptive `test_*` methods. Reuse synthetic database fixtures and temporary directories; use dry-run exports or the local HTTP collector instead of production services. Add regression tests for changed behavior, especially duplicate exports, interrupted turns, malformed data, and export failures. Install `opentelemetry-proto` before validation: dependent tests otherwise skip. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

This repository has no established commit history convention yet. Use concise, imperative commit subjects. PRs should explain the behavior change, include test commands and results, and link relevant issues. Update usage documentation when configuration or hook behavior changes.

## Security & Architecture Constraints

Preserve hooks’ zero exit status and silent stdout. Read Devin’s database read-only and retain degraded Turn-only tracing when reads fail. Never commit credentials or real session data. Trace content can include prompts, reasoning, and tool output; use synthetic content during development and keep credentials out of logs.
