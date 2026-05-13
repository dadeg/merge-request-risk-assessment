"""Post a comment on an MR via the GitLab REST API."""

from __future__ import annotations

import logging

from ..gitlab.client import GitLabClient

log = logging.getLogger(__name__)


def post(client: GitLabClient, project_id: int, mr_iid: int, body: str) -> None:
    note = client.post_mr_note(project_id, mr_iid, body)
    log.info("posted note id=%s on %s!%s", note.get("id"), project_id, mr_iid)
