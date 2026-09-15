"""Per-session hook state, persisted as JSON between hook invocations.

Hooks within one Devin session run sequentially, so atomic replace is enough;
no locking is needed."""

import json
import os
import re
import tempfile
import time


def state_path(state_dir, session_id):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)
    return os.path.join(state_dir, safe + ".json")


class SessionState:
    def __init__(self, path, turn_count=0, last_exported_node_id=None, last_exported_prompt_id=None, pending=None):
        self.path = path
        self.turn_count = turn_count
        self.last_exported_node_id = last_exported_node_id
        self.last_exported_prompt_id = last_exported_prompt_id
        self.pending = pending

    @classmethod
    def load(cls, state_dir, session_id):
        path = state_path(state_dir, session_id)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        turn_count = data.get("turn_count")
        node_id = data.get("last_exported_node_id")
        prompt_id = data.get("last_exported_prompt_id")
        pending = data.get("pending")
        return cls(
            path,
            turn_count=turn_count if isinstance(turn_count, int) else 0,
            last_exported_node_id=node_id if isinstance(node_id, int) else None,
            last_exported_prompt_id=prompt_id if isinstance(prompt_id, str) else None,
            pending=pending if isinstance(pending, dict) else None,
        )

    def save(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        data = {"turn_count": self.turn_count, "last_exported_node_id": self.last_exported_node_id,
                "last_exported_prompt_id": self.last_exported_prompt_id, "pending": self.pending}
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def delete(self):
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def gc_stale(state_dir, max_age_seconds):
    try:
        names = os.listdir(state_dir)
    except OSError:
        return
    cutoff = time.time() - max_age_seconds
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(state_dir, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
        except OSError:
            pass
