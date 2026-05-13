"""Merge an MR via the GitLab REST API."""

from __future__ import annotations

import logging

from ..gitlab.client import GitLabClient

log = logging.getLogger(__name__)


def post(client: GitLabClient, project_id: int, mr_iid: int, sha: str) -> None:
    """Merge `sha` into the target branch immediately.

    GitLab rejects the call if the MR head has moved past `sha`, which
    is the safety net we want — we never accidentally merge code we
    didn't evaluate.
    """
    result = client.merge_mr(project_id, mr_iid, sha=sha)
    log.info(
        "merged %s!%s — state=%s sha=%s",
        project_id,
        mr_iid,
        result.get("state", "?"),
        (result.get("merge_commit_sha") or "")[:8],
    )
