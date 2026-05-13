"""Risk scoring: feed the MR context to cursor-agent, parse strict JSON back."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .ai import cursor_agent
from .gatherer import MRContext, render_context_for_ai


def _find_prompt_path() -> Path:
    """Locate prompts/mr_risk.md.

    Tries, in order:
      1. $MRBOT_PROMPT_PATH (escape hatch)
      2. ./prompts/mr_risk.md (cwd, normal `python -m mrbot` from repo root)
      3. <repo>/prompts/mr_risk.md (relative to this file, dev install)
    """
    import os

    override = os.getenv("MRBOT_PROMPT_PATH")
    if override:
        p = Path(override)
        if p.exists():
            return p

    cwd_path = Path.cwd() / "prompts" / "mr_risk.md"
    if cwd_path.exists():
        return cwd_path

    return Path(__file__).resolve().parent.parent.parent / "prompts" / "mr_risk.md"


@dataclass
class RiskFactor:
    """One thing about the MR that contributes to its non-low risk verdict.

    `actionable_fix` empty + `intent_aligned` true means: the risky thing
    is the deliberate purpose of the MR; reviewer needs to read carefully
    but there's nothing for the author to fix.

    `actionable_fix` non-empty: there's a concrete change the author
    could make to reduce or eliminate this risk factor.

    `actionable_fix` empty + `intent_aligned` false: AI saw something
    risky but couldn't suggest a fix and isn't sure it's intentional —
    treat as needing reviewer judgement.
    """

    factor: str
    evidence: str = ""
    actionable_fix: str = ""
    intent_aligned: bool = False


# Backward-compat alias — older callers / tests may still import the old name.
BlockingFactor = RiskFactor


@dataclass
class Verdict:
    risk: str  # "low" | "medium" | "high"
    confidence: str  # "low" | "medium" | "high"
    summary: str
    # True only when the diff is strictly tests/docs/comments — see prompt.
    # Drives auto-merge eligibility; meaningless when risk != "low".
    is_trivial: bool = False
    reasons: list[str] = field(default_factory=list)
    risk_factors: list[RiskFactor] = field(default_factory=list)
    raw: dict | None = None


_VALID_RISK = {"low", "medium", "high"}
_VALID_CONFIDENCE = {"low", "medium", "high"}


class RiskScoreError(RuntimeError):
    """Raised when we can't extract a valid verdict from the AI response."""


def _load_prompt() -> str:
    path = _find_prompt_path()
    if not path.exists():
        raise RiskScoreError(f"prompt not found at {path}")
    return path.read_text()


def _build_stdin(ctx: MRContext) -> str:
    prompt = _load_prompt()
    rendered = render_context_for_ai(ctx)
    return f"{prompt}\n\n=== MR CONTEXT ===\n{rendered}\n"


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9]*\s*\n(.*)\n```\s*$", re.DOTALL)


def _strip_fences(s: str) -> str:
    """If the model wrapped its output in a ```json ... ``` fence, peel it."""
    s = s.strip()
    m = _FENCE_RE.match(s)
    if m:
        return m.group(1).strip()
    return s


def _extract_json_object(text: str) -> dict:
    """Find the first valid JSON object in `text`.

    Uses `json.JSONDecoder.raw_decode` which (unlike a regex/brace-count
    approach) understands string literals, escape sequences, and nested
    braces. Walks every `{` position until one parses cleanly, so the
    model can prepose explanatory prose without breaking us.
    """
    s = _strip_fences(text)
    if not s:
        raise RiskScoreError("AI returned empty output")

    # strict=False allows literal control characters (newlines, tabs) inside
    # string values. The model very often puts diff snippets in `evidence`
    # fields with raw newlines instead of `\n` escapes, and we'd rather be
    # lenient than reject an otherwise-valid verdict.
    decoder = json.JSONDecoder(strict=False)

    try:
        obj, _ = decoder.raw_decode(s)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    last_err: json.JSONDecodeError | None = None
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(s, i)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if isinstance(obj, dict):
            return obj

    if last_err is not None:
        # Heuristic: if the output starts with `{` and the parse error is one
        # of the "ran off the end of the input" varieties, the model almost
        # certainly returned a truncated reply (cursor-agent / model stream
        # cut off mid-string). Surface that clearly so the audit log + retry
        # behavior makes sense.
        msg = str(last_err)
        truncated_signals = ("Unterminated string", "Expecting", "Unexpected end of")
        if s.startswith("{") and any(sig in msg for sig in truncated_signals):
            raise RiskScoreError(
                f"AI output appears truncated ({len(text)} chars total): "
                f"{last_err}; head={text[:200]!r}"
            ) from last_err
        raise RiskScoreError(
            f"could not parse AI JSON: {last_err}; head={text[:300]!r} tail={text[-300:]!r}"
        ) from last_err
    raise RiskScoreError(f"no JSON object found in AI output: head={text[:500]!r}")


def _unwrap_verdict_envelope(obj: dict) -> dict:
    """Find the inner verdict if the model wrapped it in an envelope.

    Some runs return e.g. `{"verdict": {"risk": ...}}` or
    `{"response": {"risk": ...}}` or `{"data": {"risk": ...}}` instead
    of the bare schema. Walk one level of dict-valued keys looking for
    one that has a `risk` field.
    """
    if "risk" in obj:
        return obj
    for v in obj.values():
        if isinstance(v, dict) and "risk" in v:
            return v
    return obj


def _parse_verdict(obj: dict) -> Verdict:
    obj = _unwrap_verdict_envelope(obj)
    risk = str(obj.get("risk", "")).lower().strip()
    confidence = str(obj.get("confidence", "")).lower().strip()
    if risk not in _VALID_RISK:
        sample = json.dumps(obj, ensure_ascii=False)[:500]
        raise RiskScoreError(
            f"invalid risk value: {obj.get('risk')!r} — "
            f"top-level keys={sorted(obj.keys())!r}; sample={sample!r}"
        )
    if confidence not in _VALID_CONFIDENCE:
        sample = json.dumps(obj, ensure_ascii=False)[:500]
        raise RiskScoreError(
            f"invalid confidence value: {obj.get('confidence')!r} — sample={sample!r}"
        )

    reasons_raw = obj.get("reasons") or []
    reasons = [str(r).strip() for r in reasons_raw if str(r).strip()]

    factors_raw = obj.get("risk_factors") or obj.get("blocking_factors") or []
    factors: list[RiskFactor] = []
    for raw in factors_raw:
        if isinstance(raw, str):
            factors.append(RiskFactor(factor=raw.strip()))
        elif isinstance(raw, dict):
            factors.append(
                RiskFactor(
                    factor=str(raw.get("factor", "")).strip(),
                    evidence=str(raw.get("evidence", "")).strip(),
                    actionable_fix=str(raw.get("actionable_fix", "")).strip(),
                    intent_aligned=bool(raw.get("intent_aligned", False)),
                )
            )

    # Enforce the schema invariant — be forgiving and just clamp.
    if risk == "low":
        factors = []

    is_trivial = bool(obj.get("is_trivial", False))
    # Belt-and-braces: triviality is only meaningful for low-risk MRs.
    if risk != "low":
        is_trivial = False

    return Verdict(
        risk=risk,
        confidence=confidence,
        summary=str(obj.get("summary", "")).strip(),
        is_trivial=is_trivial,
        reasons=reasons,
        risk_factors=factors,
        raw=obj,
    )


def score(
    ctx: MRContext,
    cmd: str,
    args: list[str],
    timeout_ms: int,
) -> Verdict:
    """Run cursor-agent against the MR context and return a parsed verdict."""
    stdin_text = _build_stdin(ctx)
    raw_stdout = cursor_agent.run(cmd, args, stdin_text, timeout_ms)
    inner = cursor_agent.extract_inner_text(raw_stdout)
    obj = _extract_json_object(inner)
    return _parse_verdict(obj)
