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

# Default repo allowlist, mirroring danbot's `PROJECT_REPO_MAP` default in
# danbot/src/lib/config.js. Values are bare GitLab repo names (no group
# prefix) — the same shape danbot uses for local clone directory names.
# At boot we resolve each name to its real `group/project` path by
# reading `git remote get-url origin` from the local clone, falling back
# to a GitLab API search when the local clone is missing.
DEFAULT_PROJECT_REPO_MAP: dict[str, str] = {
    "ADJSVC": "invoice-adjustment-service",
    "ADBC": "advertiser-balance-calculator",
    "ARGENPP": "argenerator-prepay",
    "BILLMODS": "billing-module-registry",
    "BPOTOOLS": "bpo-tools-daemon",
    "CABR": "customer-balance-recon-tool",
    "CASH": "cash-application-services",
    "CASHMIR": "cash-application-services-email",
    "CLINHERIT": "credit-limit-inheritance",
    "DBRS": "detailed-billing-report-service",
    "DIRE": "dire-service",
    "DISCOSTU": "discount-service",
    "EDICT": "event-driven-invoice-creator",
    "EINV": "e-invoicing-service",
    "EOMDASH": "bpo-monitor-daemon",
    "ERPS": "erpservice",
    "FEES": "fee-service",
    "FITC": "finance-crons",
    "FLRDA": "first-last-revenue-dates-abacus",
    "GREGOR": "gregor",
    "HACKATHON": "hackathon-retainx",
    "IAS": "intacct-sync-api",
    "INVSVC": "invoice-service",
    "KDLS": "kafka-dead-letter-service",
    "LEDGER": "ledger_recon_ui",
    "MIW": "manual-invoicing-webapp",
    "MOC": "master-of-coin",
    "MONEY": "finance-guru",
    "NANOTOOLS": "moneynanotools",
    "OPSGENIE": "opsgenie",
    "OTRAV": "address-validation-cron",
    "PES": "processengine-services",
    "PESI": "processengine-services-intacct",
    "PRELUDE": "prelude",
    "PRISM": "prism",
    "PRODCAT": "product-catalog",
    "PREDICT": "predict",
    "REDS": "recon-data-services",
    "REVREC": "revenue-receiver-service",
    "REVREG": "revenue-recognizer",
    "SEED": "seed",
    "TAXMAN": "tax-service",
    "VCSS": "vertex-customer-sync-service",
    "ZEUS": "zuora-enabled-usage-sender",
    "ZIPS": "zuora-invoice-processor-service",
}


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


def _json_list(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return list(default)
    parsed = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        raise ValueError(f"env var {name} must be a JSON array of strings, got {raw!r}")
    return parsed


def _json_str_dict_or_none(name: str) -> dict[str, str] | None:
    """Parse a JSON-object env var into a {str: str} dict.

    Returns None if the env var is unset/empty so the caller can fall
    back to other sources (danbot .env, bundled default).
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise ValueError(f"env var {name} must be a JSON object of string→string, got {raw!r}")
    return parsed


def _read_project_repo_map_from_danbot_env(env_path: Path) -> dict[str, str] | None:
    """Read just the `PROJECT_REPO_MAP=...` line out of danbot's .env.

    Mirrors slack-bot's `load_project_repo_map`. Returns None on any
    failure (file missing, JSON malformed, line absent) so the caller
    can fall back to the bundled default.
    """
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("PROJECT_REPO_MAP="):
                continue
            json_blob = stripped.split("=", 1)[1].strip()
            if json_blob and json_blob[0] in ("'", '"'):
                json_blob = json_blob[1:-1]
            if not json_blob:
                return None
            data = json.loads(json_blob)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


@dataclass
class Config:
    # GitLab
    gitlab_base_url: str
    gitlab_token: str
    gitlab_user_id: int | None
    # Bare-name allowlist shared with danbot. Keys are Jira-style project
    # codes (informational only); values are GitLab repo names without
    # group prefix. Group is recovered from the local clone's origin.
    project_repo_map: dict[str, str]
    project_repo_map_source: str  # where it came from, for logging
    local_repo_base_path: Path
    danbot_env_path: Path
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

    danbot_env_path = Path(
        os.getenv("DANBOT_ENV_PATH", "/Users/ddegreef/indeed/danbot/.env")
    )

    # Project repo map resolution order:
    #  1. PROJECT_REPO_MAP env var (explicit narrowing, e.g. for first-run)
    #  2. PROJECT_REPO_MAP line read from DANBOT_ENV_PATH (canonical source)
    #  3. Bundled DEFAULT_PROJECT_REPO_MAP (last-resort fallback)
    explicit = _json_str_dict_or_none("PROJECT_REPO_MAP")
    if explicit is not None:
        project_repo_map = explicit
        project_repo_map_source = "PROJECT_REPO_MAP env var"
    else:
        from_danbot = _read_project_repo_map_from_danbot_env(danbot_env_path)
        if from_danbot:
            project_repo_map = from_danbot
            project_repo_map_source = f"danbot env at {danbot_env_path}"
        else:
            project_repo_map = dict(DEFAULT_PROJECT_REPO_MAP)
            project_repo_map_source = "bundled DEFAULT_PROJECT_REPO_MAP"

    return Config(
        gitlab_base_url=os.getenv("GITLAB_BASE_URL", "https://code.corp.indeed.com").rstrip("/"),
        gitlab_token=token,
        gitlab_user_id=user_id,
        project_repo_map=project_repo_map,
        project_repo_map_source=project_repo_map_source,
        local_repo_base_path=Path(
            os.getenv("LOCAL_REPO_BASE_PATH", "/Users/ddegreef/indeed-danbot")
        ),
        danbot_env_path=danbot_env_path,
        poll_interval_seconds=_int("GITLAB_POLL_INTERVAL_SECONDS", 30),
        include_draft_mrs=_bool("GITLAB_INCLUDE_DRAFT_MRS", False),
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
