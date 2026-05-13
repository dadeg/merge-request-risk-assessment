"""Render a `Verdict` into the markdown comment body we post on the MR."""

from __future__ import annotations

from .risk import RiskFactor, Verdict

_HEAD_SHA_MARKER = "<!-- mrbot: head_sha={sha} -->"


def _suggestion(verdict: Verdict, fixable: list[RiskFactor], intrinsic: list[RiskFactor]) -> str:
    """Pick a one-liner suggestion that matches the makeup of risk factors.

    Even high-risk MRs can be approved — they often *should* be — when
    the risks are intentional and the reviewer has weighed them. The
    wording reflects "consider carefully, then decide", not "do not
    approve".
    """
    if verdict.risk == "low":
        return "Approve."

    has_fixable = bool(fixable)
    has_intrinsic = bool(intrinsic)

    if has_fixable and has_intrinsic:
        return (
            "Suggest the fixes above to the author, and carefully consider "
            "the inherent risks before approving."
        )
    if has_fixable:
        return (
            "Suggest the fixes above to the author. Approve once they're "
            "addressed or explicitly accepted as-is."
        )
    if has_intrinsic:
        return "Carefully consider the risks above before approving."
    # No factors at all but non-low risk — shouldn't happen, but be safe.
    return "Use your judgement."


def _format_factor(rf: RiskFactor, *, with_fix: bool) -> list[str]:
    out: list[str] = []
    bullet = f"- **{rf.factor}**" if rf.factor else "- _(unspecified factor)_"
    if rf.evidence:
        bullet += f" — {rf.evidence}"
    out.append(bullet)
    if with_fix and rf.actionable_fix:
        out.append(f"  _Suggested fix:_ {rf.actionable_fix}")
    return out


def render(verdict: Verdict, head_sha: str) -> str:
    """Build the markdown body. One comment per MR head SHA.

    For low-risk MRs we keep it short. For medium/high we split risk
    factors into two groups:
      - **Fixable concerns** — the AI proposed a concrete in-MR fix
      - **Inherent risks** — no fix is proposed (often because the risky
        change IS the point of the MR, e.g. removing a public endpoint)

    The reviewer suggestion at the bottom adapts to which groups are
    present, so we don't tell a reviewer to "fix" something that's
    intentional.
    """
    risk_label = verdict.risk.upper()
    confidence = verdict.confidence
    triviality = " · _trivial_" if verdict.is_trivial else ""

    fixable = [rf for rf in verdict.risk_factors if rf.actionable_fix]
    intrinsic = [rf for rf in verdict.risk_factors if not rf.actionable_fix]

    lines: list[str] = []
    lines.append(f"**risk-bot:** **{risk_label}** risk · confidence: {confidence}{triviality}")
    lines.append("")
    if verdict.summary:
        lines.append(f"_{verdict.summary}_")
        lines.append("")

    if verdict.risk == "low":
        lines.append("No risk factors found.")
    else:
        if fixable:
            lines.append("**Fixable concerns**")
            lines.append("")
            for rf in fixable:
                lines.extend(_format_factor(rf, with_fix=True))

        if intrinsic:
            if fixable:
                lines.append("")
            lines.append("**Inherent risks** _(no in-MR fix; review carefully)_")
            lines.append("")
            for rf in intrinsic:
                lines.extend(_format_factor(rf, with_fix=False))

        if not fixable and not intrinsic:
            # Fallback: model gave no structured factors. Use reasons list.
            lines.append("**Why this isn't low risk**")
            lines.append("")
            for r in verdict.reasons or ["(no specific factors provided)"]:
                lines.append(f"- {r}")

    lines.append("")
    lines.append(f"**Suggestion to reviewer:** {_suggestion(verdict, fixable, intrinsic)}")

    lines.append("")
    lines.append(_HEAD_SHA_MARKER.format(sha=head_sha))
    return "\n".join(lines)
