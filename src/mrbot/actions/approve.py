"""Approve an MR via the GitLab REST API."""

from __future__ import annotations

import logging

from ..gitlab.client import GitLabClient

log = logging.getLogger(__name__)


def post(client: GitLabClient, project_id: int, mr_iid: int, sha: str) -> None:
    result = client.approve_mr(project_id, mr_iid, sha=sha)
    log.info(
        "approved %s!%s — total approvals now %s",
        project_id,
        mr_iid,
        result.get("approvals_left", "?"),
    )
