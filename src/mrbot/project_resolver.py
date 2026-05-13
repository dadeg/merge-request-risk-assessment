"""Resolve danbot-style bare repo names to real GitLab `group/project` paths.

`PROJECT_REPO_MAP` (shared with danbot) only stores the bare repo name,
because each repo lives in a different GitLab group. We recover the
group at boot:

  1. Read `git remote get-url origin` from `<LOCAL_REPO_BASE_PATH>/<name>`
     and parse the SSH or HTTPS URL into `group/path`. (Cheap and offline.)
  2. If the local clone is missing or has no usable remote, fall back to
     `GET /projects?search=<name>` and look for an exact `path` match.
  3. If that still finds nothing, log a warning and skip that repo.

The result is cached for the life of the process — projects don't move
between groups during a poll loop.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .gitlab.client import GitLabClient, GitLabError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedProject:
    """A repo we successfully located on GitLab."""

    repo_name: str          # bare name from PROJECT_REPO_MAP value, e.g. "invoice-service"
    path_with_namespace: str  # full GitLab path, e.g. "money/invoice-service"
    project_id: int


# git@host:group/sub/repo.git  OR  https://host/group/sub/repo.git
_SSH_RE = re.compile(r"^[^@]+@[^:]+:(?P<path>.+?)(?:\.git)?$")
_HTTP_RE = re.compile(r"^https?://[^/]+/(?P<path>.+?)(?:\.git)?$")


def _parse_origin_path(url: str) -> str | None:
    url = url.strip()
    for rx in (_SSH_RE, _HTTP_RE):
        m = rx.match(url)
        if m:
            return m.group("path").strip("/")
    return None


def _origin_path_from_clone(clone_dir: Path) -> str | None:
    if not (clone_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(clone_dir), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _parse_origin_path(proc.stdout)


def _search_gitlab(client: GitLabClient, repo_name: str) -> str | None:
    """Fallback: ask GitLab for projects whose path matches `repo_name` exactly."""
    try:
        results = client._request_json(  # noqa: SLF001 — intentional reuse
            "GET",
            "/projects",
            params={"search": repo_name, "per_page": 50, "simple": "true"},
        )
    except GitLabError as e:
        log.debug("GitLab search for %r failed: %s", repo_name, e)
        return None

    for project in results or []:
        if project.get("path") == repo_name:
            return str(project.get("path_with_namespace") or "") or None
    return None


def resolve_all(
    client: GitLabClient,
    repo_map: dict[str, str],
    local_repo_base_path: Path,
) -> list[ResolvedProject]:
    """Turn `{KEY: bare_name}` into a deduped list of `ResolvedProject`.

    Logs a warning for each repo we can't resolve, and skips it. Returns
    an empty list if nothing resolved (caller decides what to do).
    """
    seen_names: set[str] = set()
    out: list[ResolvedProject] = []

    for key, repo_name in repo_map.items():
        if not repo_name or repo_name in seen_names:
            continue
        seen_names.add(repo_name)

        clone_dir = local_repo_base_path / repo_name
        path = _origin_path_from_clone(clone_dir)
        source = "local clone"

        if not path:
            path = _search_gitlab(client, repo_name)
            source = "gitlab search"

        if not path:
            log.warning(
                "could not resolve repo %r (key=%s): no local clone at %s and no exact match via GitLab search",
                repo_name,
                key,
                clone_dir,
            )
            continue

        try:
            project = client.get_project(path)
        except GitLabError as e:
            log.warning(
                "resolved %r → %s but GitLab rejected the lookup: %s",
                repo_name,
                path,
                e,
            )
            continue

        resolved = ResolvedProject(
            repo_name=repo_name,
            path_with_namespace=str(project.get("path_with_namespace") or path),
            project_id=int(project["id"]),
        )
        log.info(
            "watching %s (id=%s) — resolved from %s",
            resolved.path_with_namespace,
            resolved.project_id,
            source,
        )
        out.append(resolved)

    return out
