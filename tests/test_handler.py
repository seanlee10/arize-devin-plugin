import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from devin_tracing import log
from devin_tracing.config import Config
from devin_tracing.state import SessionState, state_path
from tests.fixtures.make_db import SessionBuilder, create_db, ts

try:
    import opentelemetry.proto  # noqa: F401
    HAVE_PROTO = True
except ImportError:  # pragma: no cover
    HAVE_PROTO = False

if HAVE_PROTO:
    from devin_tracing.handler import handle

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SID = "brave-otter"


@unittest.skipUnless(HAVE_PROTO, "opentelemetry-proto not installed")
class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "sessions.db")
        self.state_dir = os.path.join(self.tmp.name, "state")
        self.log_path = os.path.join(self.tmp.name, "trace.log")
        self.conn = create_db(self.db_path)
        self.s = SessionBuilder(self.conn, SID)
        self.config = Config(dry_run=True, sessions_db=self.db_path, state_dir=self.state_dir,
                             log_file=self.log_path)
        log.configure(self.log_path, verbose=False)

    def tearDown(self):
        self.conn.close()
        log.configure(None, False)
        self.tmp.cleanup()

    def fire(self, event, **payload):
        payload.setdefault("session_id", SID)
        payload["hook_event_name"] = event
        handle(event, payload, self.config)

    def exported(self):
        """Dry-run spans grouped per export (trace)."""
        if not os.path.exists(self.log_path):
            return []
        traces = {}
        with open(self.log_path) as f:
            for line in f:
                if "DRY RUN span " in line:
                    span = json.loads(line.split("DRY RUN span ", 1)[1])
                    traces.setdefault(span["trace_id"], []).append(span)
        return list(traces.values())

    def state(self):
        return SessionState.load(self.state_dir, SID)

    def write_turn(self, prompt, reply, tool=True, second=1):
        self.s.user(prompt, at=ts(second))
        if tool:
            self.s.assistant("", tool_calls=[("c-%s" % prompt, "exec", {"command": "ls"})],
                             started=ts(second + 1), at=ts(second + 2))
            self.s.tool("c-%s" % prompt, "out", started=ts(second + 3), finished=ts(second + 3, 5), at=ts(second + 4))
        self.s.assistant(reply, started=ts(second + 5), at=ts(second + 6))
        self.s.set_head()


class TestTurnFlow(HandlerTestCase):
    def test_prompt_then_stop_exports_full_turn(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="list files")
        self.assertEqual(self.state().pending["prompt_id"], "p1")
        self.assertEqual(self.state().turn_count, 1)
        self.write_turn("list files", "here they are")

        self.fire("Stop", prompt_id="p1", stop_hook_active=False, last_assistant_message="here they are")

        traces = self.exported()
        self.assertEqual(len(traces), 1)
        self.assertEqual([s["kind"] for s in traces[0]], ["AGENT", "LLM", "TOOL", "LLM"])
        turn = traces[0][0]
        self.assertEqual(turn["name"], "Turn 1")
        self.assertEqual(turn["attributes"]["devin.prompt_id"], "p1")
        self.assertEqual(turn["attributes"]["output.value"], "here they are")
        self.assertNotIn("devin.continuation", turn["attributes"])
        state = self.state()
        self.assertIsNone(state.pending)
        self.assertEqual(state.last_exported_node_id, self.s.last)
        self.assertEqual(state.last_exported_prompt_id, "p1")

    def test_repeated_stop_without_new_nodes_exports_once(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="hi")
        self.write_turn("hi", "hello", tool=False)
        self.fire("Stop", prompt_id="p1", last_assistant_message="hello")
        self.fire("Stop", prompt_id="p1", last_assistant_message="hello")

        self.assertEqual(len(self.exported()), 1)

    def test_stop_refire_exports_continuation(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="work")
        self.write_turn("work", "first pass", tool=False)
        self.fire("Stop", prompt_id="p1", last_assistant_message="first pass")
        self.s.system("stop hook: run the tests")
        self.s.assistant("ran tests", started=ts(30), at=ts(31))
        self.s.set_head()

        self.fire("Stop", prompt_id="p1", stop_hook_active=True, last_assistant_message="ran tests")

        traces = self.exported()
        self.assertEqual(len(traces), 2)
        continuation = traces[1][0]
        self.assertIs(continuation["attributes"]["devin.continuation"], True)
        self.assertEqual(continuation["attributes"]["output.value"], "ran tests")
        self.assertEqual(len(traces[1]), 2)

    def test_second_turn_is_numbered_from_db(self):
        for n, (pid, prompt) in enumerate([("p1", "one"), ("p2", "two")]):
            self.fire("UserPromptSubmit", prompt_id=pid, prompt=prompt)
            self.write_turn(prompt, "reply " + prompt, tool=False, second=n * 10)
            self.fire("Stop", prompt_id=pid, last_assistant_message="reply " + prompt)

        self.assertEqual([t[0]["name"] for t in self.exported()], ["Turn 1", "Turn 2"])

    def test_resumed_session_after_state_deleted_still_exports_only_new_turn(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="one")
        self.write_turn("one", "r1", tool=False)
        self.fire("Stop", prompt_id="p1", last_assistant_message="r1")
        self.fire("SessionEnd", prompt_id="p1", reason="other")
        self.assertFalse(os.path.exists(state_path(self.state_dir, SID)))

        self.fire("SessionStart", source="resume")
        self.fire("UserPromptSubmit", prompt_id="p2", prompt="two")
        self.write_turn("two", "r2", tool=False, second=20)
        self.fire("Stop", prompt_id="p2", last_assistant_message="r2")

        traces = self.exported()
        self.assertEqual(len(traces), 2)
        self.assertEqual(traces[1][0]["name"], "Turn 2")
        self.assertEqual(traces[1][0]["attributes"]["input.value"], "two")


class TestFailSafes(HandlerTestCase):
    def test_failed_export_is_retried_with_same_request(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="hello")
        self.write_turn("hello", "hi", tool=False)

        with mock.patch("devin_tracing.handler.export", return_value=False) as send:
            self.fire("Stop", prompt_id="p1", last_assistant_message="hi")
        queued = self.state().pending_exports
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(queued), 1)

        with mock.patch("devin_tracing.handler.export", return_value=True) as retry:
            self.fire("SessionStart", source="resume")
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(self.state().pending_exports, [])

        first_request = send.call_args.args[0].SerializeToString()
        retried_request = retry.call_args.args[0].SerializeToString()
        self.assertEqual(retried_request, first_request)

    def test_session_end_keeps_failed_pending_export(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="bye")
        self.write_turn("bye", "done", tool=False)
        with mock.patch("devin_tracing.handler.export", return_value=False):
            self.fire("SessionEnd", prompt_id="p1", reason="other")

        self.assertTrue(os.path.exists(state_path(self.state_dir, SID)))
        self.assertEqual(len(self.state().pending_exports), 1)

    def test_prompt_after_missing_stop_flushes_previous_turn_as_incomplete(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="interrupted")
        self.write_turn("interrupted", "partial", tool=False)

        self.fire("UserPromptSubmit", prompt_id="p2", prompt="next")

        traces = self.exported()
        self.assertEqual(len(traces), 1)
        self.assertIs(traces[0][0]["attributes"]["devin.incomplete"], True)
        self.assertEqual(traces[0][0]["attributes"]["devin.prompt_id"], "p1")
        self.assertEqual(self.state().pending["prompt_id"], "p2")

    def test_session_end_flushes_pending_and_deletes_state(self):
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="bye")
        self.write_turn("bye", "partial", tool=False)

        self.fire("SessionEnd", prompt_id="p1", reason="other")

        traces = self.exported()
        self.assertEqual(len(traces), 1)
        self.assertIs(traces[0][0]["attributes"]["devin.incomplete"], True)
        self.assertFalse(os.path.exists(state_path(self.state_dir, SID)))

    def test_session_end_without_pending_exports_nothing(self):
        self.fire("SessionStart", source="startup")
        self.fire("SessionEnd", reason="other")
        self.assertEqual(self.exported(), [])

    def test_stop_without_db_sends_degraded_turn(self):
        self.config.sessions_db = os.path.join(self.tmp.name, "missing.db")
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="hello")

        self.fire("Stop", prompt_id="p1", last_assistant_message="hi back")

        traces = self.exported()
        self.assertEqual(len(traces), 1)
        self.assertEqual(len(traces[0]), 1)
        attributes = traces[0][0]["attributes"]
        self.assertIs(attributes["devin.degraded"], True)
        self.assertEqual(attributes["input.value"], "hello")
        self.assertEqual(attributes["output.value"], "hi back")
        self.assertIsNone(self.state().pending)

    def test_stop_without_db_or_pending_exports_nothing(self):
        self.config.sessions_db = os.path.join(self.tmp.name, "missing.db")
        self.fire("Stop", prompt_id="p1", last_assistant_message="x")
        self.assertEqual(self.exported(), [])

    def test_disabled_does_nothing(self):
        self.config.enabled = False
        self.fire("UserPromptSubmit", prompt_id="p1", prompt="x")
        self.assertFalse(os.path.exists(state_path(self.state_dir, SID)))

    def test_missing_session_id_does_nothing(self):
        handle("UserPromptSubmit", {"prompt": "x"}, self.config)
        self.assertFalse(os.path.exists(self.state_dir))


@unittest.skipUnless(HAVE_PROTO, "opentelemetry-proto not installed")
class TestHookEntryPoint(unittest.TestCase):
    def run_hook(self, event, stdin, extra_env=None):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, ARIZE_DRY_RUN="true", ARIZE_LOG_FILE=os.path.join(tmp, "t.log"),
                       ARIZE_DEVIN_STATE_DIR=os.path.join(tmp, "state"),
                       DEVIN_SESSIONS_DB=os.path.join(tmp, "none.db"))
            env.update(extra_env or {})
            result = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "hook.py"), event],
                                    input=stdin, capture_output=True, text=True, env=env, timeout=30)
            log_path = env["ARIZE_LOG_FILE"]
            log_text = ""
            if os.path.exists(log_path):
                with open(log_path) as f:
                    log_text = f.read()
        return result, log_text

    def test_valid_event_exits_zero_with_empty_stdout(self):
        payload = json.dumps({"session_id": SID, "prompt_id": "p", "prompt": "hi"})
        result, _ = self.run_hook("UserPromptSubmit", payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invalid_stdin_exits_zero(self):
        result, log_text = self.run_hook("Stop", "not json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid hook payload", log_text)

    def test_unexpected_exception_is_logged_and_exits_zero(self):
        payload = json.dumps({"session_id": SID, "prompt_id": "p", "prompt": "hi"})
        # A file where the state directory should be makes state writes fail.
        with tempfile.NamedTemporaryFile() as blocker:
            result, log_text = self.run_hook("UserPromptSubmit", payload,
                                             {"ARIZE_DEVIN_STATE_DIR": os.path.join(blocker.name, "sub")})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("Traceback", log_text)

    def test_missing_event_argument_exits_zero(self):
        result, _ = self.run_hook("", "{}")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
