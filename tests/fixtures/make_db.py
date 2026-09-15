"""Build synthetic Devin `sessions.db` files for tests.

DDL is copied from Devin CLI 3000.10.21. Message content is synthetic but
mirrors the shape observed in real sessions.
"""

import json
import sqlite3

DDL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  working_directory TEXT NOT NULL,
  backend_type TEXT NOT NULL,
  model TEXT NOT NULL,
  agent_mode TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL, title TEXT, main_chain_id INTEGER, shell_last_seen_index INTEGER DEFAULT 0, cogs_json TEXT, workspace_dirs TEXT, hidden INTEGER NOT NULL DEFAULT 0, metadata TEXT);
CREATE TABLE message_nodes (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  node_id INTEGER NOT NULL,
  parent_node_id INTEGER,
  chat_message TEXT NOT NULL,
  created_at INTEGER NOT NULL, metadata TEXT,
  FOREIGN KEY (session_id) REFERENCES sessions(id),
  UNIQUE(session_id, node_id)
);
"""


def ts(second, micro=0):
    """ISO timestamp in the format Devin writes, e.g. 2026-09-15T05:56:25.268772Z."""
    return "2026-09-15T05:56:%02d.%06dZ" % (second, micro)


class SessionBuilder:
    """Appends message nodes to a session. Each add_* links to the previous
    node unless `parent` is given, and returns the new node_id."""

    def __init__(self, conn, session_id, working_directory="/work/proj", model="swe-1-6-slow"):
        self.conn = conn
        self.session_id = session_id
        self.next_id = 0
        self.last = None
        conn.execute(
            "INSERT INTO sessions (id, working_directory, backend_type, model, agent_mode, created_at, last_activity_at, main_chain_id)"
            " VALUES (?, ?, 'windsurf', ?, 'normal', 0, 0, NULL)",
            (session_id, working_directory, model),
        )

    def _add(self, message, parent="last", raw=None):
        node_id = self.next_id
        self.next_id += 1
        parent_id = self.last if parent == "last" else parent
        body = raw if raw is not None else json.dumps(message)
        self.conn.execute(
            "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at) VALUES (?, ?, ?, ?, 0)",
            (self.session_id, node_id, parent_id, body),
        )
        self.last = node_id
        return node_id

    def system(self, content="system prompt", at=ts(0), **kw):
        return self._add({"message_id": "sys-%d" % self.next_id, "role": "system", "content": content,
                          "metadata": {"created_at": at}}, **kw)

    def user(self, content, at=ts(1), **kw):
        return self._add({"message_id": "user-%d" % self.next_id, "role": "user", "content": content,
                          "metadata": {"is_user_input": True, "created_at": at}}, **kw)

    def assistant(self, content="", tool_calls=(), thinking=None, started=ts(2), at=ts(3),
                  input_tokens=100, output_tokens=20, cache_read=1000, cache_write=None,
                  model="swe-1-6-slow", finish_reason="stop", request_id="req-1", metrics=True, **kw):
        msg = {
            "message_id": "asst-%d" % self.next_id,
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {"id": tc_id, "name": name, "arguments": args, "index": i, "kind": "function"}
                for i, (tc_id, name, args) in enumerate(tool_calls)
            ],
            "metadata": {
                "request_id": request_id,
                "finish_reason": finish_reason,
                "started_generation_at": started,
                "created_at": at,
                "generation_model": model,
                "metrics": {
                    "ttft_ms": 150, "total_time_ms": 900, "input_tokens": input_tokens,
                    "output_tokens": output_tokens, "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_write,
                } if metrics else None,
            },
        }
        if thinking is not None:
            msg["thinking"] = {"thinking": thinking, "signature": "sig"}
        return self._add(msg, **kw)

    def tool(self, tool_call_id, content, success=True, failure_reason=None,
             started=ts(4), finished=ts(4, 500), at=ts(5), timing=True, **kw):
        ext = {"chisel/tool_result_meta": {"success": success, "kind": "read"}}
        if failure_reason:
            ext["chisel/tool_result_meta"]["failure_reason"] = failure_reason
            ext["chisel/tool_failure"] = {"reason": failure_reason}
        if timing:
            ext["chisel/tool_call_timing"] = {"started_at": started, "finished_at": finished, "duration_ms": 0}
        return self._add({"message_id": "tool-%d" % self.next_id, "role": "tool", "content": content,
                          "tool_call_id": tool_call_id, "metadata": {"created_at": at, "extensions": ext}}, **kw)

    def raw(self, text, **kw):
        return self._add(None, raw=text, **kw)

    def set_head(self, node_id=None):
        head = self.last if node_id is None else node_id
        self.conn.execute("UPDATE sessions SET main_chain_id = ? WHERE id = ?", (head, self.session_id))
        self.conn.commit()


def create_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    return conn
