"""Decide whether to post a comment for a given MR + verdict.

Returns one of the `decision` strings the audit log + state store use.
The risk verdict itself does NOT gate posting — every MR we see gets
exactly one comment per head SHA. The verdict only changes what's
inside the comment.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .gatherer import MRContext, is_ci_in_progress


@dataclass
class GateDecision:
    action: str  # "post" or one of the "skipped_*" reasons
    reason: str = ""
    # Deferred decisions are NOT recorded in seen_mrs — the next polling
    # cycle should re-evaluate this exact head SHA. Used for transient
    # conditions like "CI still running" or "rate-limited right now".
    defer: bool = False

    @property
    def should_post(self) -> bool:
        return self.action == "post"


def decide(
    cfg: Config,
    ctx: MRContext,
    *,
    already_seen: bool,
) -> GateDecision:
    """Pure function. Caller does the side effects.

    Note: self-authored MRs are NOT skipped here — we still comment on
    our own MRs (handy for self-review). The auto-action step in the
    poller is the one that skips approve/merge for self-authored
    (GitLab forbids self-approval anyway).
    """
    if already_seen:
        return GateDecision("skipped_already_seen", f"head_sha {ctx.head_sha} already evaluated")

    if not cfg.enable_comments:
        return GateDecision("skipped_disabled", "ENABLE_COMMENTS=false")

    if ctx.draft and not cfg.include_draft_mrs:
        return GateDecision("skipped_draft", "MR is draft and GITLAB_INCLUDE_DRAFT_MRS=false")

    if is_ci_in_progress(ctx.ci_state):
        return GateDecision(
            "skipped_ci_in_progress",
            f"CI is {ctx.ci_state!r} (pipeline {ctx.ci_pipeline_id}); waiting for terminal state",
            defer=True,
        )

    if cfg.dry_run:
        return GateDecision("skipped_dry_run", "DRY_RUN=true")

    return GateDecision("post")
