"""Gather everything the risk scorer needs about a single MR."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .gitlab.client import GitLabClient, GitLabError

# Hard cap on diff size we send to the AI. Anything bigger gets truncated;
# the truncation itself becomes a signal the prompt notices.
_MAX_DIFF_CHARS = 60_000

# GitLab pipeline statuses that mean "still in flight" — we defer commenting
# on these so the AI verdict can incorporate the actual CI outcome.
# Reference: https://docs.gitlab.com/ee/api/pipelines.html
CI_IN_PROGRESS_STATES = frozenset(
    {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}
)


def is_ci_in_progress(state: str) -> bool:
    return state in CI_IN_PROGRESS_STATES


@dataclass
class MRContext:
    """Bundle of everything we know about an MR at evaluation time."""

    project_id: int
    project_path: str
    mr_iid: int
    head_sha: str
    title: str
    description: str
    author_id: int
    author_username: str
    target_branch: str
    source_branch: str
    draft: bool
    web_url: str
    labels: list[str] = field(default_factory=list)

    file_count: int = 0
    additions: int | None = None
    deletions: int | None = None
    diff_text: str = ""
    diff_truncated: bool = False
    changed_files: list[str] = field(default_factory=list)

    ci_state: str = "unknown"  # success | failed | running | manual | skipped | unknown | none
    ci_pipeline_id: int | None = None


def gather(client: GitLabClient, mr: dict[str, Any], project_path: str) -> MRContext:
    """Pull diff + pipeline status for an MR list-item dict.

    `mr` is the raw dict from `GET /projects/:id/merge_requests` which
    is missing the diff and may have stale pipeline info, so we re-fetch.
    """
    project_id = int(mr["project_id"])
    mr_iid = int(mr["iid"])
    head_sha = str(mr.get("sha") or "")

    # Full MR detail (richer than the list item)
    detail = client.get_mr(project_id, mr_iid)
    if not head_sha:
        head_sha = str(detail.get("sha") or "")

    # Diffs
    diff_text, files, truncated, additions, deletions = _fetch_diff(client, project_id, mr_iid)

    # CI status
    ci_state, pipeline_id = _resolve_ci(client, project_id, detail)

    author = detail.get("author") or {}

    return MRContext(
        project_id=project_id,
        project_path=project_path,
        mr_iid=mr_iid,
        head_sha=head_sha,
        title=str(detail.get("title") or ""),
        description=str(detail.get("description") or ""),
        author_id=int(author.get("id") or 0),
        author_username=str(author.get("username") or ""),
        target_branch=str(detail.get("target_branch") or ""),
        source_branch=str(detail.get("source_branch") or ""),
        draft=bool(detail.get("draft") or detail.get("work_in_progress") or False),
        web_url=str(detail.get("web_url") or ""),
        labels=list(detail.get("labels") or []),
        file_count=len(files),
        additions=additions,
        deletions=deletions,
        diff_text=diff_text,
        diff_truncated=truncated,
        changed_files=files,
        ci_state=ci_state,
        ci_pipeline_id=pipeline_id,
    )


def _fetch_diff(
    client: GitLabClient, project_id: int, mr_iid: int
) -> tuple[str, list[str], bool, int | None, int | None]:
    try:
        changes = client.get_mr_changes(project_id, mr_iid)
    except GitLabError:
        return ("", [], False, None, None)

    files: list[str] = []
    parts: list[str] = []
    additions = 0
    deletions = 0
    truncated = False
    used = 0

    for ch in changes.get("changes") or []:
        new_path = ch.get("new_path") or ch.get("old_path") or "<unknown>"
        files.append(new_path)
        diff_blob = ch.get("diff") or ""
        for line in diff_blob.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1
        header = f"--- {ch.get('old_path') or new_path}\n+++ {new_path}\n"
        chunk = header + diff_blob + "\n"
        if used + len(chunk) > _MAX_DIFF_CHARS:
            remaining = max(0, _MAX_DIFF_CHARS - used)
            if remaining:
                parts.append(chunk[:remaining])
                used += remaining
            truncated = True
            break
        parts.append(chunk)
        used += len(chunk)

    diff_text = "".join(parts)
    return (diff_text, files, truncated, additions or None, deletions or None)


def _resolve_ci(
    client: GitLabClient, project_id: int, detail: dict[str, Any]
) -> tuple[str, int | None]:
    pipeline = detail.get("head_pipeline") or detail.get("pipeline")
    if not pipeline:
        return ("none", None)
    pipeline_id = pipeline.get("id")
    state = str(pipeline.get("status") or "unknown")
    if pipeline_id:
        try:
            full = client.get_pipeline(project_id, int(pipeline_id))
            state = str(full.get("status") or state)
        except GitLabError:
            pass
    return (state, int(pipeline_id) if pipeline_id else None)


def render_context_for_ai(ctx: MRContext) -> str:
    """Plain-text MR context to feed cursor-agent stdin."""
    parts: list[str] = []
    parts.append(f"MR: {ctx.project_path}!{ctx.mr_iid} — {ctx.title}")
    parts.append(f"URL: {ctx.web_url}")
    parts.append(f"Author: @{ctx.author_username}")
    parts.append(f"Branch: {ctx.source_branch} -> {ctx.target_branch}")
    parts.append(f"Draft: {ctx.draft}")
    parts.append(f"Labels: {', '.join(ctx.labels) if ctx.labels else '(none)'}")
    parts.append(f"CI status: {ctx.ci_state}")
    parts.append(
        f"Diff stats: {ctx.file_count} file(s), "
        f"+{ctx.additions or 0} -{ctx.deletions or 0} lines"
        + (" [TRUNCATED]" if ctx.diff_truncated else "")
    )
    parts.append("")
    parts.append("=== MR DESCRIPTION ===")
    parts.append(ctx.description.strip() or "(empty)")
    parts.append("")
    parts.append("=== CHANGED FILES ===")
    parts.extend(ctx.changed_files or ["(none)"])
    parts.append("")
    parts.append("=== UNIFIED DIFF ===")
    parts.append(ctx.diff_text or "(no diff available)")
    if ctx.diff_truncated:
        parts.append("")
        parts.append("[diff was truncated above for length]")
    return "\n".join(parts)
