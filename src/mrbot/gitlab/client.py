"""Thin httpx-based GitLab REST client.

Only covers the endpoints this bot needs:
- get the current user (to resolve user_id for self-MR detection)
- look up a project by `group/path`
- list open MRs for a project
- fetch an MR's diff and pipeline status
- post a note (comment) on an MR

Nothing here knows anything about risk scoring or comment formatting.
"""

from __future__ import annotations

import json as _json
from typing import Any
from urllib.parse import quote

import httpx


class GitLabError(RuntimeError):
    """Raised on any non-2xx response from GitLab.

    `status_code` carries the HTTP status when the failure was a real
    HTTP response; it's None for transport errors (timeouts, DNS, etc).
    Callers use it to decide between transient-retry and permanent-fail.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitLabClient:
    def __init__(self, base_url: str, token: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.base_url}/api/v4",
            headers={"PRIVATE-TOKEN": token, "User-Agent": "mrbot/0.1"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GitLabClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        resp = self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise GitLabError(
                f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )
        return resp

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Like `_request`, but parses JSON and raises a useful error on bad bodies.

        The plain `.json()` call would otherwise blow up with an unhelpful
        `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` when
        GitLab returns an HTML page (auth bounce) or empty body.
        """
        resp = self._request(method, path, **kwargs)
        try:
            return resp.json()
        except _json.JSONDecodeError as e:
            ctype = resp.headers.get("content-type", "<missing>")
            body = resp.text[:500].replace("\n", " ")
            final_url = str(resp.url)
            raise GitLabError(
                f"{method} {path} -> HTTP {resp.status_code} but body was not JSON. "
                f"final_url={final_url!r} content-type={ctype!r} body={body!r}"
            ) from e

    def current_user(self) -> dict[str, Any]:
        return self._request_json("GET", "/user")

    def get_project(self, path_with_namespace: str) -> dict[str, Any]:
        encoded = quote(path_with_namespace, safe="")
        return self._request_json("GET", f"/projects/{encoded}")

    def list_open_mrs(self, project_id: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._request_json(
                "GET",
                f"/projects/{project_id}/merge_requests",
                params={
                    "state": "opened",
                    "scope": "all",
                    "per_page": 100,
                    "page": page,
                    "order_by": "updated_at",
                    "sort": "desc",
                },
            )
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    def get_mr(self, project_id: int, mr_iid: int) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/projects/{project_id}/merge_requests/{mr_iid}",
            params={"include_diverged_commits_count": "false"},
        )

    def get_mr_changes(self, project_id: int, mr_iid: int) -> dict[str, Any]:
        return self._request_json(
            "GET", f"/projects/{project_id}/merge_requests/{mr_iid}/changes"
        )

    def get_pipeline(self, project_id: int, pipeline_id: int) -> dict[str, Any]:
        return self._request_json("GET", f"/projects/{project_id}/pipelines/{pipeline_id}")

    def post_mr_note(self, project_id: int, mr_iid: int, body: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/notes",
            json={"body": body},
        )

    def approve_mr(self, project_id: int, mr_iid: int, sha: str | None = None) -> dict[str, Any]:
        """Approve an MR. Optionally pin to `sha` so we don't accidentally
        approve a force-pushed-over version we never reviewed."""
        body: dict[str, Any] = {}
        if sha:
            body["sha"] = sha
        return self._request_json(
            "POST",
            f"/projects/{project_id}/merge_requests/{mr_iid}/approve",
            json=body,
        )

    def merge_mr(
        self,
        project_id: int,
        mr_iid: int,
        sha: str,
        squash: bool = False,
    ) -> dict[str, Any]:
        """Merge an MR immediately. `sha` MUST be the head commit we
        evaluated; GitLab rejects the call if the head moved underneath us."""
        return self._request_json(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}/merge",
            json={
                "sha": sha,
                "merge_when_pipeline_succeeds": False,
                "squash": squash,
                "should_remove_source_branch": False,
            },
        )

    def get_file_raw(self, project_id: int, file_path: str, ref: str) -> str | None:
        """Fetch a single file's raw contents from a project at a given ref.

        Returns None if the file doesn't exist (404), raises GitLabError on
        any other failure.
        """
        encoded = quote(file_path, safe="")
        try:
            resp = self._request(
                "GET",
                f"/projects/{project_id}/repository/files/{encoded}/raw",
                params={"ref": ref},
            )
        except GitLabError as e:
            # 404 is a normal "file not present" signal; bubble everything else.
            if e.status_code == 404:
                return None
            raise
        return resp.text
