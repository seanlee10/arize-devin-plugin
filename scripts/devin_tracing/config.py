"""Environment-driven configuration."""

import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_ENDPOINT = "https://otlp.arize.com/v1/traces"
DEFAULT_MAX_CONTENT_CHARS = 10000


def _flag(value, default):
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    enabled: bool = True
    api_key: Optional[str] = None
    space_id: Optional[str] = None
    endpoint: str = DEFAULT_ENDPOINT
    project_name: str = "devin-cli"
    dry_run: bool = False
    verbose: bool = False
    log_file: Optional[str] = "/tmp/arize-devin.log"
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS
    sessions_db: str = ""
    state_dir: str = ""

    @property
    def has_credentials(self):
        return bool(self.api_key and self.space_id)

    @classmethod
    def from_env(cls):
        env = os.environ
        home = env.get("HOME") or os.path.expanduser("~")
        try:
            max_chars = int(env.get("ARIZE_MAX_CONTENT_CHARS", DEFAULT_MAX_CONTENT_CHARS))
        except ValueError:
            max_chars = DEFAULT_MAX_CONTENT_CHARS
        return cls(
            enabled=_flag(env.get("ARIZE_TRACE_ENABLED"), True),
            api_key=env.get("ARIZE_API_KEY") or None,
            space_id=env.get("ARIZE_SPACE_ID") or None,
            endpoint=env.get("ARIZE_OTLP_ENDPOINT") or DEFAULT_ENDPOINT,
            project_name=env.get("ARIZE_PROJECT_NAME") or "devin-cli",
            dry_run=_flag(env.get("ARIZE_DRY_RUN"), False),
            verbose=_flag(env.get("ARIZE_VERBOSE"), False),
            log_file=env.get("ARIZE_LOG_FILE", "/tmp/arize-devin.log") or None,
            max_content_chars=max_chars,
            sessions_db=env.get("DEVIN_SESSIONS_DB") or os.path.join(home, ".local/share/devin/cli/sessions.db"),
            state_dir=env.get("ARIZE_DEVIN_STATE_DIR") or os.path.join(home, ".arize-devin"),
        )
