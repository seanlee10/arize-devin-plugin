import json
import os
import tempfile
import time
import unittest

from devin_tracing.state import SessionState, gc_stale, state_path


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "state")

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_defaults(self):
        s = SessionState.load(self.dir, "brave-otter")
        self.assertEqual(s.turn_count, 0)
        self.assertIsNone(s.last_exported_node_id)
        self.assertIsNone(s.last_exported_prompt_id)
        self.assertIsNone(s.pending)

    def test_save_and_load_round_trip(self):
        s = SessionState.load(self.dir, "brave-otter")
        s.turn_count = 2
        s.last_exported_node_id = 41
        s.last_exported_prompt_id = "p-0"
        s.pending = {"prompt_id": "p", "prompt": "hi", "started_at_ns": 5}
        s.save()

        loaded = SessionState.load(self.dir, "brave-otter")

        self.assertEqual(loaded.turn_count, 2)
        self.assertEqual(loaded.last_exported_node_id, 41)
        self.assertEqual(loaded.last_exported_prompt_id, "p-0")
        self.assertEqual(loaded.pending, {"prompt_id": "p", "prompt": "hi", "started_at_ns": 5})

    def test_save_leaves_no_temp_files(self):
        s = SessionState.load(self.dir, "brave-otter")
        s.save()
        self.assertEqual(os.listdir(self.dir), ["brave-otter.json"])

    def test_corrupt_file_loads_defaults(self):
        os.makedirs(self.dir)
        with open(state_path(self.dir, "brave-otter"), "w") as f:
            f.write("{broken")
        self.assertEqual(SessionState.load(self.dir, "brave-otter").turn_count, 0)

    def test_wrong_types_load_defaults(self):
        os.makedirs(self.dir)
        with open(state_path(self.dir, "brave-otter"), "w") as f:
            json.dump({"turn_count": "x", "last_exported_node_id": "y", "pending": [1]}, f)
        s = SessionState.load(self.dir, "brave-otter")
        self.assertEqual((s.turn_count, s.last_exported_node_id, s.pending), (0, None, None))

    def test_session_id_is_sanitized_in_path(self):
        path = state_path(self.dir, "../../etc/passwd")
        self.assertEqual(os.path.dirname(path), self.dir)
        self.assertEqual(os.path.basename(path), ".._.._etc_passwd.json")

    def test_delete(self):
        s = SessionState.load(self.dir, "brave-otter")
        s.save()
        s.delete()
        self.assertFalse(os.path.exists(state_path(self.dir, "brave-otter")))
        s.delete()  # idempotent

    def test_gc_removes_only_old_state_files(self):
        os.makedirs(self.dir)
        old = state_path(self.dir, "old")
        new = state_path(self.dir, "new")
        other = os.path.join(self.dir, "keep.txt")
        for p in (old, new, other):
            open(p, "w").close()
        eight_days_ago = time.time() - 8 * 86400
        os.utime(old, (eight_days_ago, eight_days_ago))
        os.utime(other, (eight_days_ago, eight_days_ago))

        gc_stale(self.dir, max_age_seconds=7 * 86400)

        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(other))

    def test_gc_missing_dir_is_noop(self):
        gc_stale(os.path.join(self.tmp.name, "absent"), max_age_seconds=1)


if __name__ == "__main__":
    unittest.main()
