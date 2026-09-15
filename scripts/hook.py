#!/usr/bin/env python3
"""Devin CLI hook entry point: `hook.py <EventName>` with the event JSON on stdin.

Always exits 0 and never writes to stdout (Devin reads hook stdout as control
JSON). Diagnostics go to ARIZE_LOG_FILE."""

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from devin_tracing import log  # noqa: E402
from devin_tracing.config import Config  # noqa: E402


def main(argv):
    config = Config.from_env()
    log.configure(config.log_file, config.verbose)
    if not config.enabled or len(argv) < 2 or not argv[1]:
        return

    event = argv[1]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        log.error("invalid hook payload for %s" % event)
        return

    try:
        from devin_tracing.handler import handle
    except ImportError as e:
        log.error("%s; install dependencies with: pip install opentelemetry-proto" % e)
        return

    log.debug("%s session=%s prompt_id=%s" % (event, payload.get("session_id"), payload.get("prompt_id")))
    handle(event, payload, config)


if __name__ == "__main__":
    try:
        main(sys.argv)
    except Exception:
        log.error("unhandled error:\n" + traceback.format_exc())
    sys.exit(0)
