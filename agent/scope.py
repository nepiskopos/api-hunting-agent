"""Scope definition and the code-level enforcement gate.

A core goal of this project is precision: reporting an out-of-scope bug is a
real cost, and scope discipline matters independently of recall. This
module implements two layers:

1. **Prompt layer** (``SCOPE_SYSTEM_TEXT``): plain-language in-scope /
   out-of-scope definitions, injected into the system prompt (``agent.prompts``)
   so the model is told the boundary before it ever acts.

2. **Code layer** (``check_scope``): a deterministic, non-LLM gate that every
   candidate finding must pass *before* it is accepted, regardless of what
   the model claims. Two independent checks:

   - the finding's ``category`` is hard-coded to ``"information_disclosure"``
     everywhere in this codebase (see ``agent.schemas.Finding`` and the
     ``propose_finding`` tool schema in ``agent.toolbox``, which does not
     even expose a ``category`` argument the model could set) -- so there is
     no code path by which a finding reaches disk tagged as anything else;
   - a keyword scan over the finding's own free text (title +
     why_disclosure) that rejects findings which name an out-of-scope
     *technique* (SQL injection, XSS, CSRF, SSRF, RCE, rate limiting, mass
     assignment, JWT forgery, brute force, business-logic abuse, etc.) as
     their impact, even though the ``category`` field itself would still say
     ``information_disclosure``.

Why a keyword scan and not a second LLM call. A second model call to judge
scope would add cost/latency and, more importantly, would be exactly the
kind of "the model said so" reasoning the spec's finding-validation
requirement explicitly rejects when used as the *only* check.
Keyword matching is crude but fully deterministic and auditable: a reviewer
can see exactly why a candidate was rejected. Known limitation, documented
here rather than hidden: a genuine information-disclosure finding that
happens to *mention* one of these technique names in passing (e.g. "this
endpoint is also vulnerable to SQLi, but note it also leaks...") will be
rejected by this gate. Given this project weighs scope discipline highly
and explicitly rewards true precision over raw recall, this is a deliberate
false-negative-favoring tradeoff -- it is safer to occasionally under-report
than to leak an out-of-scope bug into findings.json. This is also why the
prompt layer instructs the model to describe findings purely in terms of
*what data is exposed*, never by vulnerability-class name.

Matching is word-boundary regex, not plain substring. An earlier version
matched "rce" via the ad hoc substring pair " rce"/"rce " (space-padded
specifically so the bare substring "rce" wouldn't false-trigger inside
"brute force"), but that padding itself had a real gap: punctuation-adjacent
occurrences like "(RCE)" or "RCE," touch neither padded form and slipped
through undetected. ``keyword_pattern`` below replaces that with a proper
``\bword\b`` regex, which excludes "force" correctly (no boundary between
its 'o' and 'r') while still matching any punctuation-adjacent occurrence.
The same helper also treats a space or a hyphen as an equivalent separator
between the words of a multi-word keyword, so a single entry like
"mass assignment" or "jwt forgery" now also catches the hyphenated form
("mass-assignment", "jwt-forgery") without needing a separately-listed
duplicate for every keyword -- previously "rate limit"/"rate-limit" and
"brute force"/"brute-force" each needed an explicit duplicate entry while
"mass assignment" and "jwt forgery" did not, an inconsistency with no
principled reason behind it.
"""

from __future__ import annotations

import re

SCOPE_SYSTEM_TEXT = """\
SCOPE -- read carefully, this is strictly enforced.

You are hunting for INFORMATION DISCLOSURE ONLY. Information disclosure means
the target exposes data or internal detail it should not. In scope:
  - Sensitive user/business data returned to an unauthorized or unintended
    party (PII, emails, phone numbers, vehicle/VIN data, internal IDs,
    financial details).
  - Verbose errors, stack traces, or debug output that leak implementation
    detail.
  - Exposed internal or undocumented endpoints, API versions, or
    admin/diagnostic surfaces.
  - Secrets, tokens, or credentials leaked in responses, headers, or artifacts.
  - Excessive data exposure: an endpoint returning more fields than the
    client needs, e.g. another account's records inside a list response.

OUT OF SCOPE -- do not act on these, do not report them, even if you notice
them while probing: SQL injection, XSS, CSRF, SSRF, broken authentication
(password reset abuse, brute forcing, JWT forgery), rate-limiting /
denial-of-service, business-logic abuse (mass assignment, free items,
balance manipulation, coupon abuse), RCE. If a bug's primary impact is not
"data or detail that should not be visible is visible", it is out of scope.

Some bugs are BOTH an access-control flaw AND a disclosure: if account A can
fetch account B's data via a predictable ID (a BOLA/IDOR-shaped bug), that
IS in scope -- but describe it as a disclosure ("this endpoint returns
another user's PII/vehicle data/etc."), not as an authorization bug. Never
use the vulnerability-class names above in a finding's title or reasoning,
even for an in-scope finding -- describe the data that leaked, to whom, and
why that is unintended, in plain terms.

Do not pursue: crashing the target, injecting payloads to bypass logic,
forging tokens, brute forcing credentials, or exploiting rate limits. If a
probe accidentally triggers a 500 error, that verbose error IS in scope to
report (as a disclosure of implementation detail) -- but do not deliberately
craft injection payloads to cause it.
"""

# Deliberately named after the *technique*, not the impact, so that a finding
# describing pure information disclosure should never need to use these
# words. See module docstring for the tradeoff this implies, and for why
# matching is word-boundary regex rather than plain substring.
OUT_OF_SCOPE_KEYWORDS: tuple[str, ...] = (
    "sql injection", "sqli", "nosql injection",
    "cross-site scripting", "xss",
    "cross-site request forgery", "csrf",
    "server-side request forgery", "ssrf",
    "remote code execution", "rce",
    "denial of service", "dos attack", "layer 7 dos",
    "rate limit",
    "mass assignment",
    "jwt forgery", "forge a valid jwt", "forge jwt", "forged jwt",
    "brute force", "brute forcing",
    "free item", "free coupon", "increase your balance", "unauthorized refund",
    "privilege escalation",
    "command injection",
)


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Compile ``keyword`` into a case-sensitive-on-lowered-text ``\\bword\\b``
    regex, treating a space or hyphen as an equivalent word separator for
    multi-word keywords. See module docstring for why this replaced plain
    substring matching.

    Public (not ``_``-prefixed): shared with ``agent.challenge_reference``,
    which reuses this exact matcher for the identical class of bug
    (substring false-positives) it was written to fix here.
    """
    tokens = [t for t in re.split(r"[\s-]+", keyword) if t]
    body = r"[\s-]+".join(re.escape(t) for t in tokens)
    return re.compile(rf"\b{body}\b")


def compile_keyword_patterns(keywords: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """Compile each of ``keywords`` via ``keyword_pattern``, preserving order.
    Shared helper for building the two modules' respective
    keyword-to-pattern lookup tables.
    """
    return tuple(keyword_pattern(keyword) for keyword in keywords)


_KEYWORD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    zip(OUT_OF_SCOPE_KEYWORDS, compile_keyword_patterns(OUT_OF_SCOPE_KEYWORDS))
)


def check_scope(title: str, why_disclosure: str) -> tuple[bool, str | None]:
    """Return ``(allowed, rejection_reason)`` for a candidate finding.

    ``allowed`` is ``False`` iff an out-of-scope technique keyword appears in
    the finding's own title or reasoning text. Callers should reject the
    finding outright (not just warn) when ``allowed`` is ``False`` -- see
    ``agent.tools.control_tool.ControlToolkit.propose_finding``.
    """
    haystack = f"{title}\n{why_disclosure}".lower()
    for keyword, pattern in _KEYWORD_PATTERNS:
        if pattern.search(haystack):
            return False, (
                f"rejected: finding text mentions out-of-scope technique keyword {keyword!r}. "
                "Information-disclosure findings must be described in terms of what data "
                "leaked, not by vulnerability-class name."
            )
    return True, None
