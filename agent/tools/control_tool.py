"""Control tools: how the model records a finding or declares itself done.

Both of these are exposed as ordinary OpenAI tool calls (see
``agent.toolbox``) even though neither one does network I/O -- the
spec's tool-use requirement is about the model never acting directly
(no raw HTTP, no raw "just decide and write JSON"), not specifically about
HTTP. Modeling "record a finding" and "I'm done" as tool calls keeps the
whole control loop uniform: every model turn ends in exactly one dispatched
tool call and one observation, whether that tool touched the network or not.

``propose_finding`` is the load-bearing piece for two of the spec's
functional requirements at once:

- **Scope enforcement:** the tool schema does not even expose a
  ``category`` argument -- every accepted finding is unconditionally tagged
  ``information_disclosure`` in code (see ``agent.schemas.Finding``) -- and
  the free-text fields are scanned by ``agent.scope.check_scope``.
- **Finding validation:** ``agent.validation.validate_finding``
  must accept the candidate before it is ever added to the findings list.

A rejected proposal is not a silent failure: the tool's return value tells
the model *why* it was rejected, so the model can either gather better
evidence and try again, or drop the idea and move on. This makes rejection
itself part of the reason -> act -> observe loop rather than a dead end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..challenge_reference import classify_on_challenge_list
from ..schemas import Confidence, Finding
from ..validation import validate_finding
from .http_tool import HttpToolkit

logger = logging.getLogger("agent.tools.control")

# Guards against a failure mode found during development: with tool_choice
# forced to "required" (see agent.llm_client for why that's necessary at
# all), the model is sometimes unreliable about *which* tool it reaches for
# on a very early turn and can call finish_investigation on step 1 before
# making a single real request -- the tool-call equivalent of a reflexive
# wrong answer, not a genuine judgment that exploration is complete. This
# is deliberately small (a handful of requests, not a thorough sweep) --
# its only job is rejecting a clearly-premature completion, not second-
# guessing a real decision to stop after a reasonable investigation.
MIN_REQUESTS_BEFORE_FINISH = 6


@dataclass
class ControlToolkit:
    """Owns the accumulating findings list and the run's termination flag."""

    http: HttpToolkit
    accepted_findings: list[Finding] = field(default_factory=list)
    rejected_count: int = 0
    finished: bool = False
    finish_reason: str | None = None

    def _reject(self, reason: str, *, title: str) -> dict:
        """Record and report a rejected finding proposal -- shared by both
        rejection paths in ``propose_finding`` below (evidence/scope
        validation, and pydantic schema validation).
        """
        self.rejected_count += 1
        logger.info("finding proposal rejected: %s | title=%r", reason, title)
        return {"accepted": False, "reason": reason}

    def propose_finding(
        self,
        *,
        title: str,
        endpoint: str,
        evidence: str,
        why_disclosure: str,
        reproduction: list[str],
        confidence: Confidence,
    ) -> dict:
        """Validate and, if it passes, accept a candidate finding.

        Returns ``{"accepted": True, "finding_index": N}`` or
        ``{"accepted": False, "reason": "..."}``. Deliberately never raises,
        even for a schema-malformed candidate (e.g. ``confidence`` outside
        the allowed enum, or ``reproduction`` not a list) -- that case is
        caught below and turned into a rejection reason the model can react
        to on its next turn, which is a natural, low-effort implementation
        of the spec's bonus "structured-output validation + retry"
        (the optional/bonus features): the retry loop is just the ordinary
        reason -> act -> observe cycle, no separate retry machinery needed.
        """
        result = validate_finding(
            title=title,
            why_disclosure=why_disclosure,
            evidence=evidence,
            toolkit=self.http,
            endpoint=endpoint,
        )
        if not result.accepted:
            return self._reject(result.reason, title=title)

        on_list, challenge_id = classify_on_challenge_list(f"{title} {endpoint} {why_disclosure}")
        try:
            finding = Finding(
                title=title,
                endpoint=endpoint,
                evidence=evidence,
                why_disclosure=why_disclosure,
                reproduction=reproduction,
                confidence=confidence,
                on_challenge_list=on_list,
            )
        except Exception as exc:  # pydantic.ValidationError, kept broad deliberately
            return self._reject(f"rejected: finding did not match the required schema: {exc}", title=title)
        self.accepted_findings.append(finding)
        logger.info(
            "finding accepted [%d]: %s (%s) confidence=%s on_challenge_list=%s (%s)",
            len(self.accepted_findings), title, endpoint, confidence, on_list, challenge_id,
        )
        return {"accepted": True, "finding_index": len(self.accepted_findings) - 1}

    def finish_investigation(self, *, summary: str) -> dict:
        """The model's explicit completion signal.

        Once called, the loop treats the run as finished after this step --
        see ``agent.loop.AgentLoop`` for how this interacts with the hard
        step/token budget (whichever condition triggers first wins; a
        model-declared finish always writes out real findings, it just also
        stops incurring further cost).

        Refuses (does not set ``finished``) if fewer than
        ``MIN_REQUESTS_BEFORE_FINISH`` requests have been made yet -- see
        that constant's docstring for why premature completion needs a
        floor, not just trust in the model's stated reason.
        """
        requests_made = len(self.http.history)
        if requests_made < MIN_REQUESTS_BEFORE_FINISH:
            logger.info(
                "finish_investigation refused: only %d requests made (need >= %d) | stated reason=%r",
                requests_made, MIN_REQUESTS_BEFORE_FINISH, summary,
            )
            return {
                "acknowledged": False,
                "reason": (
                    f"Refused: only {requests_made} request(s) made so far, which is too early "
                    f"to conclude the investigation is complete. Continue probing -- try more "
                    f"endpoints, accounts, and services before calling finish_investigation again."
                ),
            }
        self.finished = True
        self.finish_reason = summary
        logger.info("model declared investigation finished: %s", summary)
        return {"acknowledged": True}
