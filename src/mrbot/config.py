"""Loads runtime config from `.env` (and the process environment).

Single source of truth: import `load_config()` and read attributes off
the returned `Config`. Don't read `os.environ` from anywhere else.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"env var {name}={raw!r} is not an integer") from e


def _csv(name: str) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _json_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return list(default)
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        raise ValueError(f"env var {name} must be a JSON array of strings, got {raw!r}")
    return parsed


@dataclass
class Config:
    # GitLab
    gitlab_base_url: str
    gitlab_token: str
    gitlab_user_id: int | None
    # Repos to watch. Each entry is a fully-qualified GitLab project path
    # like "group/repo" or "group/subgroup/repo".
    watch_repos: list[str]
    poll_interval_seconds: int
    include_draft_mrs: bool

    # Comment behavior
    enable_comments: bool
    dry_run: bool
    # Auto-actions, gated separately so you can have approval without
    # merging (or vice versa). Both also short-circuit when DRY_RUN=true.
    allow_auto_approve: bool
    allow_auto_merge: bool

    # AI
    cursor_agent_cmd: str
    cursor_agent_args: list[str]
    cursor_agent_timeout_ms: int

    # Storage
    state_db_path: Path
    audit_log_path: Path

    # Misc derived
    repo_root: Path = field(default_factory=Path.cwd)


def load_config(env_path: str | os.PathLike[str] | None = None) -> Config:
    """Load `.env` then construct a `Config`.

    `env_path` is mainly for tests; in normal use we pick up `./.env`.
    Missing `GITLAB_TOKEN` raises immediately so we fail loud at boot.
    """
    if env_path is None:
        load_dotenv()
    else:
        load_dotenv(env_path)

    token = os.getenv("GITLAB_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GITLAB_TOKEN is empty. Put your GitLab Personal Access Token "
            "(scope: api) into .env. See .env.example."
        )

    user_id_raw = os.getenv("GITLAB_USER_ID", "").strip()
    if user_id_raw:
        try:
            user_id: int | None = int(user_id_raw)
        except ValueError as e:
            raise RuntimeError(
                f"GITLAB_USER_ID must be a numeric GitLab user ID (e.g. 12345), "
                f"not a username. Got {user_id_raw!r}. Leave blank to auto-resolve."
            ) from e
    else:
        user_id = None

    return Config(
        gitlab_base_url=os.getenv("GITLAB_BASE_URL", "https://gitlab.com").rstrip("/"),
        gitlab_token=token,
        gitlab_user_id=user_id,
        watch_repos=_csv("WATCH_REPOS"),
        poll_interval_seconds=_int("POLL_INTERVAL_SECONDS", 60),
        include_draft_mrs=_bool("INCLUDE_DRAFT_MRS", False),
        enable_comments=_bool("ENABLE_COMMENTS", True),
        dry_run=_bool("DRY_RUN", True),
        allow_auto_approve=_bool("ALLOW_AUTO_APPROVE", False),
        allow_auto_merge=_bool("ALLOW_AUTO_MERGE", False),
        cursor_agent_cmd=os.getenv("CURSOR_AGENT_CMD", "cursor-agent"),
        cursor_agent_args=_json_list(
            "CURSOR_AGENT_ARGS_JSON", ["-p", "--output-format", "json", "--trust"]
        ),
        cursor_agent_timeout_ms=_int("CURSOR_AGENT_TIMEOUT_MS", 180_000),
        state_db_path=Path(os.getenv("STATE_DB_PATH", "./state/seen.sqlite")),
        audit_log_path=Path(os.getenv("AUDIT_LOG_PATH", "./logs/audit.jsonl")),
    )
