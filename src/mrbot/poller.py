"""The polling loop: walk every project, evaluate every open MR.

This module has all the side-effectful glue:
- pull MRs from GitLab
- gather context, score risk, render comment
- consult the gate, post (or skip)
- record decision in state + audit log
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import comment, gate, risk
from .actions import approve as approve_action
from .actions import merge as merge_action
from .actions import post_comment
from .config import Config
from .gatherer import MRContext, gather
from .gitlab.client import GitLabClient, GitLabError
from .state.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchedProject:
    """A repo we successfully resolved on GitLab."""

    path_with_namespace: str
    project_id: int


def _audit(audit_path: Path, payload: dict[str, Any]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _verdict_payload(v: risk.Verdict | None) -> dict[str, Any]:
    if v is None:
        return {}
    return {
        "risk": v.risk,
        "confidence": v.confidence,
        "summary": v.summary,
        "is_trivial": v.is_trivial,
        "reasons": v.reasons,
        "risk_factors": [
            {
                "factor": rf.factor,
                "evidence": rf.evidence,
                "actionable_fix": rf.actionable_fix,
                "intent_aligned": rf.intent_aligned,
            }
            for rf in v.risk_factors
        ],
    }


# CI states under which we're willing to auto-merge. Stricter than for
# posting comments: we won't merge if the pipeline failed, was canceled,
# or is still waiting for human input.
_CI_OK_FOR_MERGE = frozenset({"success", "skipped", "none"})


def _record(
    store: Store,
    audit_path: Path,
    ctx: MRContext,
    decision: str,
    *,
    reason: str = "",
    verdict: risk.Verdict | None = None,
    body: str | None = None,
    defer: bool = False,
) -> None:
    """Persist a decision.

    `defer=True` means the head SHA is NOT recorded in seen_mrs — the
    next polling cycle should re-evaluate it. Use for transient skips
    like 'CI still running'. Audit log is always written.
    """
    if not defer:
        store.record_decision(
            ctx.project_id,
            ctx.mr_iid,
            ctx.head_sha,
            decision,
            risk=verdict.risk if verdict else None,
            confidence=verdict.confidence if verdict else None,
        )
    payload: dict[str, Any] = {
        "ts": int(time.time()),
        "decision": decision,
        "reason": reason,
        "deferred": defer,
        "project_id": ctx.project_id,
        "project_path": ctx.project_path,
        "mr_iid": ctx.mr_iid,
        "head_sha": ctx.head_sha,
        "title": ctx.title,
        "url": ctx.web_url,
        "author": ctx.author_username,
        "ci_state": ctx.ci_state,
        "verdict": _verdict_payload(verdict),
    }
    if body is not None:
        payload["comment_body"] = body
    _audit(audit_path, payload)


def _maybe_auto_act(
    cfg: Config,
    client: GitLabClient,
    audit_path: Path,
    ctx: MRContext,
    verdict: risk.Verdict,
    current_user_id: int,
) -> list[str]:
    """After commenting, optionally approve and/or merge the MR.

    Returns a list of action names that succeeded — e.g. `["approved",
    "merged"]` or `[]` — for the caller to fold into a single INFO log.

    Eligibility for AUTO-APPROVE (all must hold):
      - ALLOW_AUTO_APPROVE
      - verdict.risk == "low"
      - author is not the current user (GitLab forbids self-approval)

    Additional gate for AUTO-MERGE:
      - ALLOW_AUTO_MERGE
      - verdict.is_trivial (only test/doc/comment changes)
      - CI is in {success, skipped, none}
    """
    actions: list[str] = []

    if not cfg.allow_auto_approve and not cfg.allow_auto_merge:
        return actions
    if verdict.risk != "low":
        return actions

    if ctx.author_id and current_user_id and ctx.author_id == current_user_id:
        log.debug(
            "%s!%s authored by current user — skipping approve/merge",
            ctx.project_path,
            ctx.mr_iid,
        )
        return actions

    if cfg.allow_auto_approve:
        try:
            approve_action.post(client, ctx.project_id, ctx.mr_iid, ctx.head_sha)
            actions.append("approved")
            _audit(audit_path, _action_audit_payload(ctx, verdict, action="approved"))
        except GitLabError as e:
            log.error("approval failed for %s!%s: %s", ctx.project_path, ctx.mr_iid, e)
            _audit(
                audit_path,
                _action_audit_payload(ctx, verdict, action="approve_failed", reason=str(e)),
            )
            return actions

    if not cfg.allow_auto_merge:
        return actions
    if not verdict.is_trivial:
        return actions
    if ctx.ci_state not in _CI_OK_FOR_MERGE:
        log.info(
            "%s!%s trivial+low but CI=%s; not auto-merging",
            ctx.project_path,
            ctx.mr_iid,
            ctx.ci_state,
        )
        _audit(
            audit_path,
            _action_audit_payload(
                ctx,
                verdict,
                action="merge_skipped_ci",
                reason=f"CI state {ctx.ci_state!r} not in {sorted(_CI_OK_FOR_MERGE)}",
            ),
        )
        return actions

    try:
        merge_action.post(client, ctx.project_id, ctx.mr_iid, ctx.head_sha)
        actions.append("merged")
        _audit(audit_path, _action_audit_payload(ctx, verdict, action="merged"))
    except GitLabError as e:
        log.error("auto-merge failed for %s!%s: %s", ctx.project_path, ctx.mr_iid, e)
        _audit(
            audit_path,
            _action_audit_payload(ctx, verdict, action="merge_failed", reason=str(e)),
        )

    return actions


def _action_audit_payload(
    ctx: MRContext,
    verdict: risk.Verdict,
    *,
    action: str,
    reason: str = "",
) -> dict[str, Any]:
    """Build an audit-log entry for an approval / merge attempt."""
    return {
        "ts": int(time.time()),
        "decision": action,
        "reason": reason,
        "project_id": ctx.project_id,
        "project_path": ctx.project_path,
        "mr_iid": ctx.mr_iid,
        "head_sha": ctx.head_sha,
        "title": ctx.title,
        "url": ctx.web_url,
        "author": ctx.author_username,
        "ci_state": ctx.ci_state,
        "verdict": _verdict_payload(verdict),
    }


def _resolve_user_id(cfg: Config, client: GitLabClient) -> int:
    if cfg.gitlab_user_id:
        return cfg.gitlab_user_id
    me = client.current_user()
    uid = int(me["id"])
    log.info("resolved current user @%s id=%s", me.get("username"), uid)
    return uid


def _resolve_watch_repos(client: GitLabClient, paths: list[str]) -> list[WatchedProject]:
    """Look each `group/repo` path up via the GitLab API and return the
    set we could resolve. Logs a warning for each path the token can't see."""
    out: list[WatchedProject] = []
    for path in paths:
        try:
            project = client.get_project(path)
        except GitLabError as e:
            log.warning("could not resolve repo %r: %s", path, e)
            continue
        out.append(
            WatchedProject(
                path_with_namespace=str(project.get("path_with_namespace") or path),
                project_id=int(project["id"]),
            )
        )
        log.info(
            "watching %s (id=%s)",
            project.get("path_with_namespace") or path,
            project.get("id"),
        )
    return out


def _process_mr(
    cfg: Config,
    client: GitLabClient,
    store: Store,
    audit_path: Path,
    project_path: str,
    mr_summary: dict[str, Any],
    current_user_id: int,
) -> None:
    project_id = int(mr_summary["project_id"])
    mr_iid = int(mr_summary["iid"])
    head_sha = str(mr_summary.get("sha") or "")

    if head_sha and store.has_seen(project_id, mr_iid, head_sha):
        return

    try:
        ctx = gather(client, mr_summary, project_path)
    except GitLabError as e:
        log.warning("could not gather %s!%s: %s", project_path, mr_iid, e)
        return

    if not ctx.head_sha:
        log.warning("skipping %s!%s — no head SHA", project_path, mr_iid)
        return

    if store.has_seen(ctx.project_id, ctx.mr_iid, ctx.head_sha):
        return

    pre_gate = gate.decide(cfg, ctx, already_seen=False)

    # Decisions we can make WITHOUT spending tokens on the AI. Both the
    # terminal skips (draft, disabled) and the deferred skips (CI in
    # progress) bail before risk.score().
    pre_ai_skip_actions = {
        "skipped_draft",
        "skipped_disabled",
        "skipped_ci_in_progress",
    }
    if pre_gate.action in pre_ai_skip_actions:
        if pre_gate.defer:
            log.info(
                "deferring %s!%s (%s) — will retry next cycle",
                project_path,
                ctx.mr_iid,
                pre_gate.action,
            )
        _record(
            store,
            audit_path,
            ctx,
            pre_gate.action,
            reason=pre_gate.reason,
            defer=pre_gate.defer,
        )
        return

    try:
        verdict = risk.score(
            ctx,
            cmd=cfg.cursor_agent_cmd,
            args=cfg.cursor_agent_args,
            timeout_ms=cfg.cursor_agent_timeout_ms,
        )
    except Exception as e:
        log.exception("risk scoring failed for %s!%s", project_path, mr_iid)
        # Defer errors so transient failures (cursor-agent timeout, model
        # blip) get retried next cycle. Permanent failures will keep
        # showing up in the audit log until a human investigates.
        _record(store, audit_path, ctx, "error", reason=f"risk_score: {e}", defer=True)
        return

    body = comment.render(verdict, ctx.head_sha)

    if pre_gate.action == "skipped_dry_run":
        _record(
            store,
            audit_path,
            ctx,
            pre_gate.action,
            reason=pre_gate.reason,
            verdict=verdict,
            body=body,
        )
        return

    try:
        post_comment.post(client, ctx.project_id, ctx.mr_iid, body)
    except GitLabError as e:
        # 401/403/404 won't fix themselves — mark terminal so we stop
        # retrying every cycle. 5xx and transport errors stay deferred.
        is_permanent = e.status_code in (401, 403, 404)
        decision = "error_forbidden" if is_permanent else "error"
        log.error(
            "posting comment failed for %s!%s (%s): %s",
            project_path,
            mr_iid,
            decision,
            e,
        )
        _record(
            store,
            audit_path,
            ctx,
            decision,
            reason=f"post_comment: {e}",
            verdict=verdict,
            defer=not is_permanent,
        )
        return

    _record(store, audit_path, ctx, "commented", verdict=verdict, body=body)

    extra_actions = _maybe_auto_act(
        cfg, client, audit_path, ctx, verdict, current_user_id
    )

    actions = ["commented"] + extra_actions
    triv_tag = " · trivial" if verdict.is_trivial else ""
    log.info(
        "%s on %s!%s (%s%s) — %s",
        " + ".join(actions),
        project_path,
        ctx.mr_iid,
        verdict.risk.upper(),
        triv_tag,
        ctx.web_url,
    )


def run_forever(cfg: Config) -> None:
    if not cfg.watch_repos:
        raise RuntimeError(
            "WATCH_REPOS is empty. Set it in .env to a comma-separated list of "
            "GitLab project paths, e.g. WATCH_REPOS=group/repo1,group/sub/repo2"
        )

    store = Store(cfg.state_db_path)
    cfg.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    with GitLabClient(cfg.gitlab_base_url, cfg.gitlab_token) as client:
        current_user_id = _resolve_user_id(cfg, client)
        projects = _resolve_watch_repos(client, cfg.watch_repos)
        if not projects:
            raise RuntimeError(
                "No projects from WATCH_REPOS could be resolved. Check the paths "
                "and that your GitLab token has access to them."
            )

        log.info(
            "starting poll loop: %d project(s), every %ds, dry_run=%s",
            len(projects),
            cfg.poll_interval_seconds,
            cfg.dry_run,
        )

        while True:
            cycle_start = time.time()
            for project in projects:
                try:
                    mrs = client.list_open_mrs(project.project_id)
                except GitLabError as e:
                    log.error("listing MRs for %s failed: %s", project.path_with_namespace, e)
                    continue
                log.debug("project %s: %d open MR(s)", project.path_with_namespace, len(mrs))
                for mr in mrs:
                    try:
                        _process_mr(
                            cfg,
                            client,
                            store,
                            cfg.audit_log_path,
                            project.path_with_namespace,
                            mr,
                            current_user_id,
                        )
                    except Exception:
                        log.exception(
                            "unhandled error processing %s!%s",
                            project.path_with_namespace,
                            mr.get("iid"),
                        )

            elapsed = time.time() - cycle_start
            sleep_for = max(1.0, cfg.poll_interval_seconds - elapsed)
            log.debug("cycle took %.1fs, sleeping %.1fs", elapsed, sleep_for)
            time.sleep(sleep_for)
