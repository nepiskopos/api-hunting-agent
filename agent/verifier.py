"""Bonus: an adversarial "verifier" pass (the optional/bonus features, bonus bullet 2).

This is a *second*, independent LLM call made after a finding has already
passed the mandatory validation in ``agent.validation`` -- it does not
replace that validation, it adds a skeptical second opinion on top of it,
per the project's own framing of this as an enhancement layered over the
required "no raw the-model-said-so" justification (the finding-validation requirement).

The verifier is given only the finding's own fields (title, endpoint,
evidence, why_disclosure) in a *fresh* conversation with no shared history
with the discovery agent, and is explicitly instructed to try to refute it.
This asymmetry (attacker LLM vs. skeptic LLM, no shared context) is what
makes it a meaningfully independent check rather than the same reasoning
rubber-stamped twice.

Kept intentionally simple: a forced tool call for a structured yes/no
verdict (avoids fragile free-text parsing), one retry if the model produces
no tool call at all, and a fail-open policy on verifier errors (see
``verify_finding``'s docstring for why) since this is explicitly
optional/bonus and must never be the reason a real finding is lost due to,
say, a transient LLM API hiccup.

The retry exists because live testing against the provided endpoint showed
the verifier call can hit the exact same failure mode documented in
``agent.llm_client`` for the main loop: even with ``tool_choice="required"``,
the model occasionally produces no tool call at all. Measured live: roughly
1 in 7 single-shot verifier calls against the provided model. Without a
retry, that fraction of findings would silently skip verification entirely
(fail-open keeps them, but no actual skeptical check ever ran) rather than
being rare. One retry is enough to make that a second-order effect instead
of a routine one, while keeping the same fail-open guarantee if both calls
come back empty.

Fix #20 (found running a stronger model against the live target): the
original prompt's "default to skepticism" instruction, with no guidance on
who carries the burden of proof, led the verifier to refute a real finding
-- another user's community post leaking their email address and internal
vehicle ID to any authenticated caller -- on the grounds that "no privacy
policy proves this is unintended." That gets the burden of proof backwards
for personal data specifically: crAPI's own public challenge docs list
exactly this class of bug (Challenge 4, "Find an API endpoint that leaks
sensitive information of other users") as an intentional vulnerability, i.e.
the correct default *is* that another account's email/phone/address or an
internal identifier for their private resources is confidential unless the
evidence shows otherwise -- not the reverse. The prompt now says so
explicitly, while leaving every other "could this be a different vuln
class" skepticism unchanged.
"""

from __future__ import annotations

import json
import logging

from .llm_client import LLMClient
from .schemas import Finding

logger = logging.getLogger("agent.verifier")

_VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Submit your skeptical verdict on the candidate finding.",
        "parameters": {
            "type": "object",
            "properties": {
                "refuted": {
                    "type": "boolean",
                    "description": "true if you believe this is NOT genuine, in-scope information disclosure.",
                },
                "reason": {"type": "string", "description": "Brief justification for your verdict."},
            },
            "required": ["refuted", "reason"],
        },
    },
}

_VERIFIER_SYSTEM_PROMPT = """\
You are a skeptical security reviewer. You will be shown ONE candidate
information-disclosure finding (title, endpoint, evidence, and the
discovering agent's reasoning). Your job is to actively try to refute it:
- Is the "evidence" actually sensitive/unintended, or is it plausibly public
  or expected for the calling user?
- Does the reasoning actually establish disclosure to an UNAUTHORIZED or
  UNINTENDED party, or could the caller plausibly be entitled to this data?
- Is this actually information disclosure, or does it look more like a
  different vulnerability class (SQL injection, XSS, CSRF, SSRF, broken
  auth, rate limiting, business-logic abuse, RCE) described in disclosure
  terms?

On the burden of proof for personal data: if the evidence shows a caller
receiving another account's personal data (email address, phone number,
physical address, government ID, payment details) or an internal identifier
used to reference another user's private resource (a database ID/UUID for
someone else's vehicle, order, report, etc.) bound to that other account,
treat this as disclosure BY DEFAULT. The absence of a stated privacy policy
is not evidence that exposing it was intended -- the default expectation for
personal data is confidentiality, and the burden is on the evidence to show
either that the calling user is the data's own owner, or that the field is
presented as deliberately public (e.g. a stated public directory or a
name/nickname the product explicitly displays to everyone), not on the
finder to produce a policy document. Only refute a personal-data finding on
"could be intended" grounds if the evidence itself supports one of those two
things.

Default to skepticism on everything else: if you are not convinced, set
refuted=true. Call submit_verdict with your decision.
"""


_MAX_ATTEMPTS = 2  # one retry if the model produces no tool call at all -- see module docstring


def verify_finding(finding: Finding, llm: LLMClient) -> tuple[bool, str]:
    """Return ``(survives, reason)``. ``survives`` is ``False`` iff the
    verifier actively refuted the finding.

    Fail-open on any verifier-side error (malformed response, API error):
    a bonus safety net erroring out must not delete a finding that already
    passed the mandatory validation pipeline. The error is logged so it's
    visible in run.log, but the finding is kept.
    """
    candidate_text = (
        f"Title: {finding.title}\n"
        f"Endpoint: {finding.endpoint}\n"
        f"Evidence: {finding.evidence}\n"
        f"Reasoning (why_disclosure): {finding.why_disclosure}\n"
        f"Confidence claimed by discoverer: {finding.confidence}\n"
    )
    messages = [
        {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
        {"role": "user", "content": candidate_text},
    ]
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            message = llm.chat(
                messages,
                tools=[_VERDICT_TOOL],
                temperature=0.0,
                max_tokens=1024,
            )
            tool_calls = getattr(message, "tool_calls", None) or []
            if tool_calls:
                args = json.loads(tool_calls[0].function.arguments)
                refuted = bool(args.get("refuted", False))
                reason = str(args.get("reason", "")).strip() or "(no reason given)"
                return (not refuted), reason
            logger.warning(
                "verifier produced no tool call for finding %r (attempt %d/%d)%s",
                finding.title, attempt, _MAX_ATTEMPTS,
                "; retrying" if attempt < _MAX_ATTEMPTS else "; keeping finding (fail-open).",
            )
        return True, "verifier produced no verdict after retry; kept by default"
    except Exception as exc:  # noqa: BLE001 -- deliberate fail-open, see docstring
        logger.warning("verifier pass errored for finding %r: %s; keeping finding (fail-open).", finding.title, exc)
        return True, f"verifier error, kept by default: {exc}"
