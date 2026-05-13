"""Subprocess wrapper around the `cursor-agent` CLI.

We invoke cursor-agent in non-interactive mode and pipe the prompt +
context in via stdin. The CLI is configured via env vars so the user
can swap binaries / flags without touching code.
"""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger(__name__)


class CursorAgentError(RuntimeError):
    """Raised when the cursor-agent CLI fails or returns unparseable output."""


def run(cmd: str, args: list[str], stdin_text: str, timeout_ms: int) -> str:
    """Invoke `cmd args...` with `stdin_text`, return stdout.

    Raises CursorAgentError on non-zero exit or timeout.
    """
    timeout_s = max(1.0, timeout_ms / 1000.0)
    try:
        proc = subprocess.run(
            [cmd, *args],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as e:
        raise CursorAgentError(f"cursor-agent binary not found: {cmd!r}") from e
    except subprocess.TimeoutExpired as e:
        raise CursorAgentError(f"cursor-agent timed out after {timeout_s}s") from e

    if proc.returncode != 0:
        raise CursorAgentError(
            f"cursor-agent exit={proc.returncode}: {proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def _looks_like_error_envelope(d: dict) -> bool:
    """cursor-agent's `--output-format json` flags errors with `is_error: true`
    (or sometimes `subtype: 'error'`). Either signals we shouldn't treat
    `result` as a model reply."""
    return bool(d.get("is_error")) or str(d.get("subtype", "")).lower() == "error"


def extract_inner_text(raw_stdout: str) -> str:
    """cursor-agent --output-format json wraps the model output in an envelope.

    Handles a few shapes:
    - raw plain text (returned as-is)
    - one JSON object with a `result` / `text` / `output` field containing text
    - newline-delimited JSON events where the last `assistant`/`text` event
      carries the final answer

    Raises CursorAgentError when the envelope reports an error (`is_error: true`
    or `subtype: 'error'`). Anything we can't parse is returned unchanged so
    the caller can still try to find JSON in it.
    """
    s = raw_stdout.strip()
    if not s:
        return s

    # Try whole-blob JSON first.
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        if _looks_like_error_envelope(parsed):
            err_msg = (
                parsed.get("result")
                or parsed.get("error")
                or parsed.get("message")
                or "(no message)"
            )
            raise CursorAgentError(
                f"cursor-agent envelope reports error: {str(err_msg)[:500]}"
            )
        for key in ("result", "text", "output", "content", "response"):
            v = parsed.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return s

    # Try NDJSON.
    last_text: str | None = None
    for line in s.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        if _looks_like_error_envelope(evt):
            err_msg = (
                evt.get("result")
                or evt.get("error")
                or evt.get("message")
                or "(no message)"
            )
            raise CursorAgentError(
                f"cursor-agent stream reports error: {str(err_msg)[:500]}"
            )
        for key in ("result", "text", "output", "content", "response"):
            v = evt.get(key)
            if isinstance(v, str) and v.strip():
                last_text = v
                break

    return last_text if last_text is not None else s
