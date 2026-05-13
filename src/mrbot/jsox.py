"""Detect a project's JSOX-compliance posture from `.gitlab-ci.yml`.

JSOX is a compliance regime — JSOX-compliant repos REQUIRE human
approvals and can't be auto-approved/merged by a bot. So mrbot uses
this signal in the *opposite* direction from what its name suggests:

  - `JSOX_COMPLIANCE: true|1|yes|on`   → repo DOES need compliance →
                                          NEVER auto-approve / auto-merge
  - `JSOX_COMPLIANCE: false|0|no|off`  → repo does NOT need compliance →
                                          auto-actions are allowed
  - flag absent (or file missing)      → unknown → SAFE DEFAULT, treat
                                          as "needs compliance"; no
                                          auto-actions

We deliberately do NOT pull in PyYAML for this. The variable is almost
always declared at the top-level `variables:` block, but it could also
appear under any include/extends. A regex that ignores indentation
catches every realistic placement.
"""

from __future__ import annotations

import logging
import re

from .gitlab.client import GitLabClient, GitLabError

log = logging.getLogger(__name__)


_JSOX_TRUTHY_RE = re.compile(
    r"""^\s*JSOX_COMPLIANCE\s*:\s*['"]?(true|1|yes|on)['"]?\s*(?:\#.*)?$""",
    re.IGNORECASE | re.MULTILINE,
)

_JSOX_FALSY_RE = re.compile(
    r"""^\s*JSOX_COMPLIANCE\s*:\s*['"]?(false|0|no|off)['"]?\s*(?:\#.*)?$""",
    re.IGNORECASE | re.MULTILINE,
)


def file_declares_jsox_compliant(content: str) -> bool:
    """Pure regex check — `JSOX_COMPLIANCE: <truthy>`."""
    return bool(_JSOX_TRUTHY_RE.search(content))


def file_declares_jsox_not_compliant(content: str) -> bool:
    """Pure regex check — `JSOX_COMPLIANCE: <falsy>`."""
    return bool(_JSOX_FALSY_RE.search(content))


def is_project_safe_for_auto_action(
    client: GitLabClient, project_id: int, ref: str
) -> bool:
    """Return True only when the project is provably NOT JSOX-compliant.

    Anything ambiguous (file missing, flag absent, flag truthy, or any
    error fetching the file) returns False — auto-actions stay off
    unless we have explicit evidence the repo is exempt.
    """
    try:
        content = client.get_file_raw(project_id, ".gitlab-ci.yml", ref)
    except GitLabError as e:
        log.warning("could not fetch .gitlab-ci.yml for project %s @ %s: %s", project_id, ref, e)
        return False
    if not content:
        return False
    if file_declares_jsox_compliant(content):
        return False
    return file_declares_jsox_not_compliant(content)
