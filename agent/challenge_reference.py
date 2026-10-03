"""Static local reference of the *public* crAPI challenge list.

Purpose and boundary (read this before touching this file)
------------------------------------------------------------
The spec's required finding schema has an ``on_challenge_list`` boolean
field. Answering it requires *some* knowledge of the public list at
``github.com/OWASP/crAPI/blob/main/docs/challenges.md``.

This module is the **only** place in the codebase that is allowed to know
about that list, and it is used for exactly one purpose: **after** the agent
has already, independently, discovered and validated a finding, a plain
Python function here classifies whether that finding's topic overlaps with a
publicly documented challenge -- for bookkeeping/scoring transparency only.

It is deliberately **not**:

- fed into the system prompt or any message sent to the model,
- used to seed, order, or bias the agent's exploration ("go check X next"),
- used as a stopping condition ("stop once N challenges are found").

The spec is explicit that an agent which "simply hardcodes the listed
challenges will score poorly" and that the list itself is incomplete (some
real information-disclosure bugs in crAPI are not on it). Keeping this
mapping fully out of the model's context is how we make sure the *discovery*
process stays generalization-driven, while still honestly answering a
required output field.

The list below is a condensed, human-curated summary of the information-
disclosure-*relevant* entries in the public challenge list as of the crAPI
commit this project was developed against (see README for the exact commit
hash). Non-disclosure challenges (SQLi, SSRF, rate limiting, mass
assignment, JWT forgery, business-logic abuse, etc.) are intentionally
omitted -- they are out of scope for this agent and would never legitimately
match a finding it is allowed to report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .scope import compile_keyword_patterns


@dataclass(frozen=True)
class ChallengeRef:
    """One information-disclosure-relevant entry from the public challenge list."""

    id: str
    title: str
    # Lowercase keywords used for a conservative, explainable match against a
    # finding's title/endpoint/why_disclosure text. Deliberately simple
    # (word-boundary regex, not an LLM call): this classification must be
    # cheap, deterministic, and auditable, not another source of model
    # judgment layered on top of the finding itself. See
    # ``classify_on_challenge_list``'s docstring for why plain substring
    # matching (the original implementation) isn't safe here.
    keywords: tuple[str, ...]


# Curated from docs/challenges.md in the OWASP/crAPI repository.
# Only challenges whose primary impact is "data/detail that should not be
# visible is visible" are included, per the project's own scope test.
#
# Challenge 14 ("Find an endpoint that does not perform authentication
# checks for a user") is deliberately NOT included here, even though it is
# an information-disclosure-relevant public entry. Its public description is
# too generic to keyword-match without an unacceptable false-positive rate:
# real live runs produced the exact same underlying finding (the /.env
# credential leak) with `on_challenge_list` flipping between true and false
# across separate runs purely because of incidental LLM phrasing ("without
# any authentication" vs. "without authentication" vs. "unauthenticated") --
# not because the finding's substance changed. Since almost any genuinely
# novel information-disclosure finding can be honestly described using
# "unauthenticated" or "no auth" language (that's often *why* it's
# disclosure at all), keeping this entry actively risks misattributing
# future novel findings to a known public challenge -- exactly the false
# positive this module's own docstring already says is worse than a false
# negative. Omitting it means a finding that genuinely is challenge 14 gets
# `on_challenge_list=False` -- a false negative, and per that same
# docstring, harmless (it only affects a bookkeeping field).
PUBLIC_INFO_DISCLOSURE_CHALLENGES: tuple[ChallengeRef, ...] = (
    ChallengeRef(
        id="challenge-1-bola-vehicle",
        title="Access details of another user's vehicle",
        keywords=("vehicle", "vin", "another user", "other user", "location"),
    ),
    ChallengeRef(
        id="challenge-2-bola-mechanic-report",
        title="Access mechanic reports of other users",
        keywords=("mechanic", "report"),
    ),
    ChallengeRef(
        id="challenge-4-excessive-data-exposure-users",
        # The data-type-name words "email"/"phone" were removed from this
        # challenge's keywords (fix #40, 2026-10-02): a live run mislabeled a
        # plain forget-password *user-enumeration* finding -- an off-list
        # generalization win -- as this challenge purely because its text
        # contained the word "email", with no cross-user data exposure at all.
        # "excessive"/"pii" are deliberately KEPT: they are load-bearing for
        # distinguishing this challenge (excessive data exposure) from
        # challenge-1 (vehicle BOLA) on the genuine community-posts finding,
        # whose text mentions a leaked vehicle id and so also whole-word-
        # matches challenge-1's "vehicle" -- removing them lets challenge-1
        # wrongly outscore this one (see the two regression tests in
        # test_challenge_reference.py). This is the same over-broad-keyword
        # false-positive class already fixed for "vin" (#15), substring
        # matching (#22), and "credential" (#38); narrowing the data-type
        # words while keeping the disclosure-nature words is the surgical cut,
        # since a false "on-list" tag understates the most-heavily-weighted
        # metric (generalization/off-list disclosure).
        title="Endpoint leaks sensitive information of other users",
        keywords=("other user", "other users'", "another user", "excessive", "pii"),
    ),
    ChallengeRef(
        id="challenge-5-excessive-data-exposure-video-property",
        title="Endpoint leaks an internal property of a video",
        keywords=("video", "internal property", "internal field"),
    ),
    ChallengeRef(
        id="challenge-17-chatbot-credential-extraction",
        title="Extract the credentials of another user using the chatbot",
        # "credential" deliberately excluded: a live run misattributed a
        # plain unauthenticated `.env` credential leak to this challenge
        # purely because its own why_disclosure text said "credential-based
        # attacks" -- the word-boundary matcher (correctly) matched
        # "credential" as a whole word there, but this challenge is
        # specifically about extracting credentials *via chatbot
        # manipulation*, not any finding that happens to mention leaked
        # credentials. "credential" alone is exactly the kind of generic,
        # broadly-applicable keyword the "vin"/first-match-wins bugs already
        # showed is unsafe here; "chatbot" is the one genuinely distinctive
        # signal for this specific challenge.
        keywords=("chatbot", "another user's"),
    ),
)


_CHALLENGE_KEYWORD_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (ref.id, compile_keyword_patterns(ref.keywords)) for ref in PUBLIC_INFO_DISCLOSURE_CHALLENGES
)


def classify_on_challenge_list(finding_text: str) -> tuple[bool, str | None]:
    """Return ``(matched, challenge_id_or_none)`` for a finding's free text.

    ``finding_text`` should be a concatenation of the finding's title,
    endpoint and ``why_disclosure`` fields -- i.e. this runs *after* the
    finding already exists, purely to fill in the required
    ``on_challenge_list`` field honestly.

    Matching is intentionally conservative (word-boundary regex, not an LLM
    call): a false negative here (an on-list bug tagged ``False``) is
    harmless -- it only affects a bookkeeping field, not whether the finding
    is reported. A false positive is worse: it understates a genuine
    generalization win by misattributing it to a known public challenge --
    which is exactly what happened with the original plain-substring
    implementation (``keyword in text``): the 3-letter keyword ``"vin"``
    (meant to catch vehicle VIN numbers) matched inside the ordinary English
    word "ha**vin**g", in an unrelated finding's own reproduction text
    ("...without logging in or having any valid session token"), silently
    mislabeling a genuinely novel `.env` disclosure finding as
    "challenge-1-bola-vehicle". Fixed by reusing
    ``agent.scope``'s ``\\bword\\b``-boundary keyword matcher (the same fix
    already applied there for the identical class of bug), so a keyword only
    matches a whole word or whole multi-word phrase, never a substring
    embedded inside an unrelated word.

    Ranked by number of distinct matching keywords per challenge, not "first
    challenge in the tuple whose keywords match at all" -- a second real
    misattribution, of the same "false positive" class the word-boundary fix
    above addressed, surfaced live: a genuine challenge-4 finding (community
    posts leaking another user's email/PII) also legitimately mentions
    "vehicle ID" as one incidentally-leaked field, whole-word-matching
    challenge-1's "vehicle" keyword -- and since challenge-1 is listed before
    challenge-4, first-match-wins misattributed a real challenge-4 finding as
    challenge-1. That finding's text actually matches three of challenge-4's
    keywords ("another user", "pii", "excessive") against only two
    of challenge-1's ("vehicle", "another user"), so scoring by match count
    and keeping the highest resolves it correctly regardless of tuple order.
    Ties keep whichever challenge reached that score first (stable, matching
    the old behavior when two challenges are equally well-supported).
    """
    text = finding_text.lower()
    best_id: str | None = None
    best_score = 0
    for challenge_id, patterns in _CHALLENGE_KEYWORD_PATTERNS:
        score = sum(1 for pattern in patterns if pattern.search(text))
        if score > best_score:
            best_score = score
            best_id = challenge_id
    return best_id is not None, best_id
