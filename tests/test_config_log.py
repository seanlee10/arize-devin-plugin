import os
import tempfile
import unittest
from unittest import mock

from devin_tracing import log
from devin_tracing.config import Config


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/u"}, clear=True):
            c = Config.from_env()
        self.assertTrue(c.enabled)
        self.assertIsNone(c.api_key)
        self.assertEqual(c.endpoint, "https://otlp.arize.com/v1/traces")
        self.assertEqual(c.project_name, "devin-cli")
        self.assertFalse(c.dry_run)
        self.assertFalse(c.verbose)
        self.assertEqual(c.log_file, "/tmp/arize-devin.log")
        self.assertEqual(c.max_content_chars, 10000)
        self.assertEqual(c.sessions_db, "/home/u/.local/share/devin/cli/sessions.db")
        self.assertEqual(c.state_dir, "/home/u/.arize-devin")

    def test_overrides(self):
        env = {
            "HOME": "/home/u", "ARIZE_TRACE_ENABLED": "false", "ARIZE_API_KEY": "k", "ARIZE_SPACE_ID": "s",
            "ARIZE_OTLP_ENDPOINT": "https://otlp.example.com/v1/traces", "ARIZE_PROJECT_NAME": "p",
            "ARIZE_DRY_RUN": "TRUE", "ARIZE_VERBOSE": "1", "ARIZE_LOG_FILE": "", "ARIZE_MAX_CONTENT_CHARS": "50",
            "DEVIN_SESSIONS_DB": "/x.db", "ARIZE_DEVIN_STATE_DIR": "/state",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            c = Config.from_env()
        self.assertFalse(c.enabled)
        self.assertEqual((c.api_key, c.space_id), ("k", "s"))
        self.assertEqual(c.endpoint, "https://otlp.example.com/v1/traces")
        self.assertEqual(c.project_name, "p")
        self.assertTrue(c.dry_run)
        self.assertTrue(c.verbose)
        self.assertIsNone(c.log_file)
        self.assertEqual(c.max_content_chars, 50)
        self.assertEqual((c.sessions_db, c.state_dir), ("/x.db", "/state"))

    def test_invalid_max_chars_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"HOME": "/h", "ARIZE_MAX_CONTENT_CHARS": "lots"}, clear=True):
            self.assertEqual(Config.from_env().max_content_chars, 10000)

    def test_has_credentials(self):
        self.assertFalse(Config(api_key="k").has_credentials)
        self.assertTrue(Config(api_key="k", space_id="s").has_credentials)


class TestLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "trace.log")

    def tearDown(self):
        log.configure(None, False)
        self.tmp.cleanup()

    def read(self):
        with open(self.path) as f:
            return f.read()

    def test_writes_errors_and_warnings(self):
        log.configure(self.path, verbose=False)
        log.error("boom")
        log.warn("careful")
        content = self.read()
        self.assertIn("ERROR boom", content)
        self.assertIn("WARN careful", content)

    def test_debug_only_when_verbose(self):
        log.configure(self.path, verbose=False)
        log.debug("hidden")
        log.info("shown")
        log.configure(self.path, verbose=True)
        log.debug("visible")
        content = self.read()
        self.assertNotIn("hidden", content)
        self.assertIn("shown", content)
        self.assertIn("visible", content)

    def test_disabled_log_file_writes_nothing(self):
        log.configure(None, verbose=True)
        log.error("nowhere")
        self.assertFalse(os.path.exists(self.path))

    def test_unwritable_path_does_not_raise(self):
        log.configure(os.path.join(self.tmp.name, "no", "such", "dir", "x.log"), verbose=True)
        log.error("ignored")


if __name__ == "__main__":
    unittest.main()
