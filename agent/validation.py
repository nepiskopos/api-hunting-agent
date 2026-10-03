"""Finding validation: turning a model's claim into a justified finding
("no raw 'the model said so'").

The spec requires that before a finding is recorded, the agent
"justify why it is information disclosure (what data leaked, to whom, why
that is unintended)". This module is the code-level check that a candidate
finding actually clears that bar, applied in addition to (not instead of)
the scope gate in ``agent.scope``.

Three independent, deterministic checks (all must pass):

1. **Evidence is grounded.** The ``evidence`` string the model provides must
   be substantiated by an actual HTTP response the toolkit really captured
   this run -- specifically, a normalized excerpt of it must appear in the
   response body of a request *matching the finding's claimed endpoint*. This
   is the concrete answer to "no raw 'the model said so'": the model cannot
   simply assert a leak happened; the leaked text must be traceable to a real,
   logged observation of the very endpoint the finding names. Binding to the
   claimed endpoint (not merely to any body captured this run) is fix #43,
   which closed a false-positive hole where the model quoted one request's
   response as proof of a claim about a different request -- see
   ``HttpToolkit.bodies_for_endpoint``.
2. **Reasoning is substantive, not a restatement.** ``why_disclosure`` must
   be non-trivial prose that is meaningfully distinct from the title (a
   naive model failure mode is copying the title into the reasoning field
   verbatim) and must reference *what* was exposed and *why* it's unintended
   -- checked heuristically via minimum length and lexical overlap with the
   title, not by asking the model to grade itself.
3. **Scope.** Delegates to ``agent.scope.check_scope`` (kept separate so the
   two concerns -- "is this real/justified" vs. "is this in scope" -- stay
   independently testable and independently loggable).

A stronger, optional second-pass adversarial check (a *separate* LLM call
that actively tries to refute the finding) is available as the bonus
"verifier pass" in ``agent.verifier`` -- deliberately layered on top of this
module rather than replacing it, per the project's own split between a
mandatory justification requirement (the finding-validation requirement) and an optional verifier
enhancement (the optional/bonus features).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .scope import check_scope
from .tools.http_tool import HttpToolkit

MIN_WHY_DISCLOSURE_CHARS = 40


def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase, for a forgiving substring match
    against response bodies that may be pretty-printed differently than the
    model's paraphrase of them.

    Also collapses literal ``\\n``/``\\r``/``\\t`` -- a backslash character
    followed by a letter, NOT an actual whitespace character -- the same way
    as real whitespace. This is a real, observed model quirk: when a tool
    call's JSON arguments are built from a multi-line response body, the
    model sometimes double-escapes the embedded newlines, so the *decoded*
    argument string contains a literal two-character backslash-n instead of
    an actual newline. Left unhandled, that literal backslash becomes a
    token boundary the following letter survives (e.g. "crapi\\nDB_USER"
    tokenizes as "crapi" and "ndb_user", not "crapi" and "db_user"), so
    every extracted candidate snippet gets a stray leading letter and can
    never match the real, correctly-formatted captured body -- silently
    rejecting a genuinely grounded finding as fabricated. Observed live: a
    verbatim-content /.env finding failed this check for exactly this
    reason on a real run.

    Lowercasing happens *before* the backslash-escape collapse, not after
    (fix #34, caught live by `hypothesis` fuzzing `test_normalize_is_idempotent`
    in the seventeenth pass): the escape regex only matches a lowercase
    ``n``/``r``/``t``, so with the old lowercase-last ordering, an uppercase
    ``\\N``/``\\R``/``\\T`` survived the first pass untouched -- the trailing
    ``.lower()`` would only turn it lowercase *after* the collapse regex had
    already run, so the sequence was collapsed only on a second application.
    Folding case first guarantees every application is idempotent, which
    matters here specifically because ``validate_finding`` normalizes with
    this function exactly once per candidate, not twice.
    """
    text = re.sub(r"\\[nrt]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ValidationResult:
    accepted: bool
    reason: str | None  # populated iff not accepted


def _evidence_is_grounded(
    evidence: str,
    toolkit: HttpToolkit,
    endpoint: str | None = None,
    min_snippet_chars: int = 12,
) -> bool:
    """Check that some non-trivial fragment of ``evidence`` actually appears
    in a response body this run genuinely captured *from the request matching
    the finding's claimed endpoint*.

    We don't require the *entire* evidence string to match verbatim (the
    model may reasonably summarize a JSON blob, e.g. "the response includes
    email: alice@example.com for a different user"), so instead we require
    that a meaningful token/snippet extracted from evidence (the longest
    "word-ish" run of characters) shows up in at least one captured
    response. This is intentionally permissive on formatting but strict on
    substance: a fabricated leak (no matching real captured data) will not
    have any matching snippet.

    ``endpoint`` (the finding's claimed ``"METHOD /path"``) narrows the
    responses considered to those the matching request actually produced --
    see ``HttpToolkit.bodies_for_endpoint`` for why (fix #43). ``None`` keeps
    the pre-fix behavior of considering every captured body, used only by
    unit tests exercising the snippet logic in isolation; production always
    supplies the claimed endpoint.
    """
    normalized_evidence = _normalize(evidence)
    # Grab candidate snippets: quoted substrings, and long alnum/punct runs
    # (emails, tokens, VINs, IDs, field values) that are unlikely to appear
    # in unrelated text by coincidence.
    candidates = set(re.findall(r"[A-Za-z0-9@._\-/]{%d,}" % min_snippet_chars, normalized_evidence))
    candidates |= set(re.findall(r'"([^"]{%d,})"' % min_snippet_chars, evidence))
    if not candidates:
        # Evidence too short/generic to extract a distinctive snippet from;
        # fall back to requiring the whole (short) evidence string to match.
        candidates = {normalized_evidence} if len(normalized_evidence) >= min_snippet_chars else set()

    # RequestRecord itself only stores a short excerpt (memory/log-size
    # reasons); the fuller bodies needed for grounding checks are kept in a
    # bounded ring buffer on the toolkit, here filtered to the claimed
    # endpoint's own responses (see HttpToolkit.bodies_for_endpoint()).
    for body in toolkit.bodies_for_endpoint(endpoint):
        normalized_body = _normalize(body)
        if any(candidate.lower() in normalized_body for candidate in candidates):
            return True
    return False


def _word_set(text: str) -> set[str]:
    """Significant (4+ letter) lowercase word tokens, for the lexical-overlap
    check below. Matches on plain alphabetic runs only (no digits/punctuation
    joined in), so e.g. "victim.user@example.com" contributes "victim" and
    "example" as separate tokens, not one long non-matching blob."""
    return set(re.findall(r"[a-z]{4,}", _normalize(text)))


def _reasoning_is_substantive(title: str, why_disclosure: str) -> bool:
    stripped = why_disclosure.strip()
    if len(stripped) < MIN_WHY_DISCLOSURE_CHARS:
        return False
    normalized_why = _normalize(why_disclosure)
    normalized_title = _normalize(title)
    # Reject a near-verbatim copy of the title standing in for real reasoning.
    if normalized_why == normalized_title:
        return False
    # Also reject a *near*-restatement: padding the title with a few trivial
    # extra words (e.g. appending "and this is bad for users") would dodge
    # the exact-match check just above while still being almost entirely a
    # copy of the title, not real reasoning about what leaked/to whom/why
    # it's unintended. Only trips when nearly every significant title word
    # reappears AND the reasoning isn't meaningfully longer than the title --
    # genuine reasoning naturally reuses some title vocabulary (the subject
    # matter is the same) but adds real length/specifics beyond it, so this
    # doesn't false-trigger on real findings (see test_scope_and_validation.py
    # for grounded, accepted examples that share title vocabulary but pass).
    title_words = _word_set(title)
    if title_words and len(stripped) < len(title.strip()) * 1.5:
        overlap = len(title_words & _word_set(why_disclosure)) / len(title_words)
        if overlap >= 0.8:
            return False
    return True


def _reject(reason: str) -> ValidationResult:
    return ValidationResult(accepted=False, reason=reason)


def validate_finding(
    *,
    title: str,
    why_disclosure: str,
    evidence: str,
    toolkit: HttpToolkit,
    endpoint: str | None = None,
) -> ValidationResult:
    """Run all validation checks for a candidate finding. Pure function of
    its inputs plus the toolkit's request history -- no LLM call involved.

    ``endpoint`` is the finding's claimed ``"METHOD /path"``; passing it binds
    the evidence-grounding check to that endpoint's own captured responses
    (fix #43). It is keyword-optional so unit tests of the other checks need
    not supply it, but the production caller (``control_tool``) always does.
    """
    in_scope, scope_reason = check_scope(title, why_disclosure)
    if not in_scope:
        return _reject(scope_reason)

    if not _reasoning_is_substantive(title, why_disclosure):
        return _reject(
            "rejected: why_disclosure is too short or just restates the title. "
            "Must explain what was exposed, to whom, and why it is unintended "
            f"(minimum {MIN_WHY_DISCLOSURE_CHARS} characters of real reasoning)."
        )

    if not _evidence_is_grounded(evidence, toolkit, endpoint):
        return _reject(
            "rejected: evidence does not match any response actually captured this run "
            "from the claimed endpoint. Findings must be grounded in a real observed "
            "http_request result for the endpoint they name, not summarized/paraphrased "
            "from memory, invented, or taken from a different request's response."
        )

    return ValidationResult(accepted=True, reason=None)
