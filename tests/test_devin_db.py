import os
import tempfile
import unittest

from devin_tracing.devin_db import load_turn, parse_ts
from tests.fixtures.make_db import SessionBuilder, create_db, ts


class DevinDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "sessions.db")
        self.conn = create_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def session(self, sid="brave-otter"):
        return SessionBuilder(self.conn, sid)


class TestSingleCall(DevinDbTestCase):
    def test_single_call_without_tools(self):
        s = self.session()
        s.system()
        s.user("hi there", at=ts(1))
        s.assistant("hello!", thinking="greet", started=ts(2), at=ts(3),
                    input_tokens=10, output_tokens=5, cache_read=90, cache_write=7)
        s.set_head()

        turn = load_turn(self.db_path, "brave-otter", None)

        self.assertEqual(turn.session_id, "brave-otter")
        self.assertEqual(turn.turn_number, 1)
        self.assertEqual(turn.user_prompt, "hi there")
        self.assertEqual(turn.working_directory, "/work/proj")
        self.assertEqual(turn.agent_mode, "normal")
        self.assertEqual(turn.head_node_id, 2)
        self.assertEqual(turn.started_at_ns, parse_ts(ts(1)))
        self.assertEqual(turn.ended_at_ns, parse_ts(ts(3)))
        self.assertEqual(len(turn.llm_calls), 1)
        call = turn.llm_calls[0]
        self.assertEqual(call.model, "swe-1-6-slow")
        self.assertEqual(call.output_text, "hello!")
        self.assertEqual(call.reasoning, "greet")
        self.assertEqual(call.finish_reason, "stop")
        self.assertEqual(call.request_id, "req-1")
        self.assertEqual(call.started_at_ns, parse_ts(ts(2)))
        self.assertEqual(call.ended_at_ns, parse_ts(ts(3)))
        self.assertEqual((call.input_tokens, call.output_tokens, call.cache_read_tokens, call.cache_write_tokens),
                         (10, 5, 90, 7))
        self.assertEqual(call.ttft_ms, 150)
        self.assertEqual(call.input_messages, [{"role": "user", "content": "hi there"}])
        self.assertEqual(call.tool_calls, [])


class TestTools(DevinDbTestCase):
    def test_parallel_tool_calls_with_success_and_validation_failure(self):
        s = self.session()
        s.user("read files")
        s.assistant("", tool_calls=[("c1", "read", {"file_path": "a.txt"}), ("c2", "read", {"file_path": "missing.txt"})],
                    finish_reason="tool_calls", request_id="req-a")
        s.tool("c1", "file contents", started=ts(4, 100), finished=ts(4, 200))
        s.tool("c2", "not found", success=False, failure_reason="ValidationError", timing=False, at=ts(5, 300))
        s.assistant("done", started=ts(6), at=ts(7), request_id="req-b")
        s.set_head()

        turn = load_turn(self.db_path, "brave-otter", None)

        self.assertEqual(len(turn.llm_calls), 2)
        first, second = turn.llm_calls
        self.assertEqual([t.id for t in first.tool_calls], ["c1", "c2"])
        ok, failed = first.tool_calls
        self.assertEqual(ok.name, "read")
        self.assertEqual(ok.arguments, {"file_path": "a.txt"})
        self.assertEqual(ok.output, "file contents")
        self.assertIs(ok.success, True)
        self.assertIsNone(ok.failure_reason)
        self.assertEqual(ok.started_at_ns, parse_ts(ts(4, 100)))
        self.assertEqual(ok.ended_at_ns, parse_ts(ts(4, 200)))
        self.assertIs(failed.success, False)
        self.assertEqual(failed.failure_reason, "ValidationError")
        self.assertEqual(failed.started_at_ns, parse_ts(ts(5, 300)))
        self.assertEqual(failed.ended_at_ns, parse_ts(ts(5, 300)))
        self.assertEqual(second.input_messages, [
            {"role": "tool", "content": "file contents", "tool_call_id": "c1"},
            {"role": "tool", "content": "not found", "tool_call_id": "c2"},
        ])
        self.assertEqual(second.output_text, "done")

    def test_tool_call_without_result_has_unknown_status(self):
        s = self.session()
        s.user("go")
        s.assistant("", tool_calls=[("c1", "exec", {"command": "sleep 100"})])
        s.set_head()

        call = load_turn(self.db_path, "brave-otter", None).llm_calls[0]

        self.assertIsNone(call.tool_calls[0].success)
        self.assertIsNone(call.tool_calls[0].output)


class TestTurnBoundaries(DevinDbTestCase):
    def test_second_turn_excludes_first_turn_nodes(self):
        s = self.session()
        s.user("first", at=ts(1))
        s.assistant("one", at=ts(2))
        s.user("second", at=ts(10))
        s.assistant("two", started=ts(11), at=ts(12))
        s.set_head()

        turn = load_turn(self.db_path, "brave-otter", None)

        self.assertEqual(turn.turn_number, 2)
        self.assertEqual(turn.user_prompt, "second")
        self.assertEqual([c.output_text for c in turn.llm_calls], ["two"])
        self.assertEqual(turn.started_at_ns, parse_ts(ts(10)))

    def test_after_node_id_exports_only_continuation(self):
        s = self.session()
        s.user("do it")
        s.assistant("partial")
        exported = s.last
        s.system("stop hook said: run tests")
        s.assistant("continued", started=ts(20), at=ts(21))
        s.set_head()

        turn = load_turn(self.db_path, "brave-otter", exported)

        self.assertEqual([c.output_text for c in turn.llm_calls], ["continued"])
        self.assertEqual(turn.user_prompt, "do it")
        self.assertEqual(turn.turn_number, 1)

    def test_nothing_new_after_exported_node_returns_none(self):
        s = self.session()
        s.user("do it")
        s.assistant("done")
        s.set_head()

        self.assertIsNone(load_turn(self.db_path, "brave-otter", s.last))

    def test_forked_chain_ignores_side_branch(self):
        s = self.session()
        s.user("q")
        user_node = s.last
        s.assistant("draft copy", request_id="stale")
        s.assistant("committed", parent=user_node, request_id="live")
        s.set_head()

        turn = load_turn(self.db_path, "brave-otter", None)

        self.assertEqual([c.request_id for c in turn.llm_calls], ["live"])


class TestRobustness(DevinDbTestCase):
    def test_malformed_node_is_skipped(self):
        s = self.session()
        s.user("q")
        s.raw("{not json")
        s.assistant("fine")
        s.set_head()

        turn = load_turn(self.db_path, "brave-otter", None)

        self.assertEqual([c.output_text for c in turn.llm_calls], ["fine"])

    def test_missing_session_returns_none(self):
        self.assertIsNone(load_turn(self.db_path, "nope", None))

    def test_null_head_returns_none(self):
        s = self.session()
        s.user("q")
        self.conn.commit()

        self.assertIsNone(load_turn(self.db_path, "brave-otter", None))

    def test_missing_db_file_returns_none(self):
        self.assertIsNone(load_turn(os.path.join(self.tmp.name, "absent.db"), "brave-otter", None))

    def test_missing_metrics_yield_zero_tokens(self):
        s = self.session()
        s.user("q")
        s.assistant("a", metrics=False)
        s.set_head()

        call = load_turn(self.db_path, "brave-otter", None).llm_calls[0]

        self.assertEqual((call.input_tokens, call.output_tokens, call.cache_read_tokens, call.cache_write_tokens),
                         (0, 0, 0, 0))
        self.assertIsNone(call.ttft_ms)


class TestParseTs(unittest.TestCase):
    def test_parses_microsecond_utc(self):
        self.assertEqual(parse_ts("1970-01-01T00:00:01.000002Z"), 1_000_002_000)

    def test_parses_offset_form(self):
        self.assertEqual(parse_ts("1970-01-01T00:00:01+00:00"), 1_000_000_000)

    def test_invalid_returns_none(self):
        self.assertIsNone(parse_ts(None))
        self.assertIsNone(parse_ts("garbage"))


if __name__ == "__main__":
    unittest.main()
