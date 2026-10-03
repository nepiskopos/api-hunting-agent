"""Human-readable rendering of findings: the required short summary (the structured-output requirement) and the optional, richer Markdown report (an optional, bonus feature).

Kept in one module because both are pure functions of the same
``list[Finding]`` input and differ only in verbosity -- ``render_summary``
is the minimum required "short human-readable summary" alongside
``findings.json``; ``render_full_report`` is strictly more detail, produced
only if the bonus is enabled, and is explicitly optional ("nice to have,
not required").
"""

from __future__ import annotations

from .schemas import Finding

_TITLE = "Information-Disclosure Hunting Agent"


def _join(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def render_summary(findings: list[Finding], *, rejected_count: int = 0) -> str:
    """The required short human-readable summary. One line per
    finding plus a total count -- deliberately terse; ``render_full_report``
    is where the detail lives.
    """
    lines = [f"# {_TITLE} -- Run Summary", ""]
    lines.append(f"Findings accepted: {len(findings)}   |   proposals rejected: {rejected_count}")
    lines.append("")
    if not findings:
        lines.append("No information-disclosure findings were accepted this run.")
    for i, f in enumerate(findings, start=1):
        on_list = "on public challenge list" if f.on_challenge_list else "not on public challenge list"
        lines.append(f"{i}. [{f.confidence.upper()}] {f.title} -- {f.endpoint} ({on_list})")
    return _join(lines)


def render_full_report(findings: list[Finding], *, cost_summary_text: str | None = None) -> str:
    """The bonus Markdown report: one detailed section per finding."""
    lines = [f"# {_TITLE} -- Findings Report", ""]
    lines.append(f"Total findings: {len(findings)}")
    lines.append("")
    for i, f in enumerate(findings, start=1):
        lines.append(f"## {i}. {f.title}")
        lines.append("")
        lines.append(f"- **Endpoint:** `{f.endpoint}`")
        lines.append(f"- **Confidence:** {f.confidence}")
        lines.append(f"- **On public challenge list:** {f.on_challenge_list}")
        lines.append("")
        lines.append(f"**Why this is disclosure:** {f.why_disclosure}")
        lines.append("")
        lines.append("**Evidence:**")
        lines.append("```")
        lines.append(f.evidence)
        lines.append("```")
        lines.append("")
        lines.append("**Reproduction:**")
        for step_no, step in enumerate(f.reproduction, start=1):
            lines.append(f"{step_no}. {step}")
        lines.append("")
    if cost_summary_text:
        lines.append("---")
        lines.append("")
        lines.append("```")
        lines.append(cost_summary_text.rstrip())
        lines.append("```")
    return _join(lines)
