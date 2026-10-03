"""The HTTP-request tool and its supporting auth/session state.

This is the *only* place in the codebase that makes network requests against
the crAPI target. The model never sees a raw socket/requests API -- it only
ever emits a structured tool-call (``{"method": ..., "path": ..., ...}``)
which ``HttpToolkit.http_request`` executes and turns into a plain-data
observation dict that gets serialized back into the model's context.

Design choices worth calling out:

- **Response truncation.** Bodies are capped at ``MAX_BODY_CHARS`` characters
  to keep token usage bounded on large list endpoints (crAPI's
  ``/community/api/v2/community/posts/recent`` and similar can return large
  arrays). The cap is generous enough that "excessive data exposure" (extra
  fields on a handful of records) is still visible, while a multi-hundred-row
  dump gets truncated with an explicit ``truncated: true`` + original length
  so the model knows to ask for a narrower request (e.g. add a limit param)
  rather than silently losing signal. Truncation is structure-aware where it
  can be (``_find_top_level_array`` / ``_truncate``): if the body is a JSON
  array, or an object whose first array-valued field has more than one item
  (the common "list endpoint" shape -- e.g. ``{"posts": [...]}``), whole
  items are kept up to the budget and the rest dropped with a count, instead
  of cutting mid-record. This exists purely to spend the same token budget
  on more *complete*, reasoned-about records rather than more truncated
  ones -- observed to matter because the model here has repeatedly failed to
  make full use of what it's already shown; it inspects only generic JSON
  shape, never a crAPI-specific field name. Anything that isn't JSON, or
  JSON with no such array, still gets a plain character cut.
- **Result dict field order matters, separately from the truncation above.**
  This is about a *second*, later truncation: once an observation ages out
  of ``agent.loop``'s recency window, ``_compact_transcript`` keeps only the
  result dict's first ``OLD_TOOL_MESSAGE_CLIP_CHARS`` serialized characters
  -- a raw prefix cut, not a summary. A real response's headers (Server,
  Date, Cache-Control, security headers, etc.) commonly serialize to
  500+ characters on their own, so with ``headers`` listed before ``body``,
  that prefix cut consumed the *entire* budget on routine boilerplate and
  silently dropped the body -- the field that actually carries
  information-disclosure signal in the overwhelming majority of cases --
  in full, for every observation older than the last few steps. Fixed by
  moving ``body``/``body_truncated``/``body_original_length`` ahead of
  ``headers`` in the dict below, so the same fixed budget is spent on the
  field most likely to matter.
- **Auth is per-account, resolved lazily.** The model addresses accounts by
  the ``label`` it was told about in the system prompt (e.g. "primary",
  "secondary"); the toolkit logs in on first use and caches the token. This
  is what makes cross-account "excessive data exposure" checks
  (the scope definition) practical: the model just changes the ``account``
  argument between two otherwise-identical calls.
- **Errors are data, not exceptions.** Connection failures, timeouts and
  non-2xx responses are all returned as a normal observation dict (with an
  ``error`` field set where applicable) rather than raised -- a stack trace
  from the *agent's own* HTTP client must never crash the run; and a 500
  response from the target is exactly the kind of thing (verbose errors,
  the scope definition) the agent is supposed to notice and reason about.
- **``list_visited_endpoints`` (the chosen auxiliary tool).** Rather
  than a speculative "endpoint discovery" tool, the toolkit tracks every
  request it has actually made and exposes a compact summary of it. This is
  cheap, grounded in reality (no guessing at an OpenAPI spec that may not be
  exposed), and directly supports both anti-repeat reasoning and
  noticing patterns across accounts/endpoints without re-reading full raw
  bodies (token economy).

  A generic path-candidate crawler (scanning response bodies for path-shaped
  substrings not yet requested) was tried here and then removed: none of the
  committed run's findings (see HISTORY.md's "Status of the committed
  real run") came from a crawler-surfaced
  path, and across every live run this session the crawler's own output was
  never anything but trivial static-asset paths (favicon, bundled JS/CSS)
  that never fed into a finding. Kept out per the "smallest agent that
  reasons" principle -- an unused mechanism is complexity the spec
  doesn't reward, not a hedge worth keeping "just in case."

- **``discover_api_endpoints`` -- reading the frontend's own API map
  (2026-10-02).** Distinct from (and motivated by the failure of) the removed
  crawler above: that scanned *response bodies* for path-shaped text and only
  ever turned up static-asset paths. This instead fetches the single-page
  app's JavaScript *bundle* and extracts the API path literals the app itself
  calls -- the actual authenticated endpoint surface, which a live run proved
  the model cannot guess (it kept trying ``/community/api/v2/posts`` when the
  real path is ``/community/api/v2/community/posts/recent``). See the
  ``discover_api_endpoints`` comment block further down for the full rationale
  and why it stays within scope. The raw bundle is read tool-side and never
  enters the model's context; only the extracted path list does.

- **``list_id_candidates`` -- a single-hop, model-directed pivot aid, not a
  crawler (eighteenth pass).** Discussed explicitly with the user before
  building: a multi-level BFS crawler that autonomously issues its own
  requests across depth levels was rejected as out of scope (it collides
  with the "autonomous loop" goal's "not a fixed, pre-scripted sequence of
  requests" and the project's framing's "not... a perfect scanner" -- the model would
  only be picking a seed, not reasoning about each individual request). This
  tool stays on the compliant side of that line: given one ``path`` already
  in ``history``, it inspects *only* the cached response body from that one
  prior request (no new HTTP call, no fan-out) and returns generic,
  key-name/value-shape-based ID candidates (e.g. a ``vehicle_id``/``userId``
  field holding a number or UUID) found in it, plus -- if ``path`` itself
  contains an ID-shaped segment -- ready-to-use candidate paths with that
  segment substituted. The model still decides whether to call this tool at
  all, on which path, and whether to actually fetch any returned candidate
  via its own separate ``http_request`` call; nothing here executes
  automatically. This directly targets the project's own "excessive data
  exposure" / cross-resource BOLA-style scope bullet with a generic
  (crAPI-agnostic) heuristic: standard REST field-naming convention
  (``*_id``/``*Id``/bare ``id``) plus value shape (int, UUID, long hex), not
  any crAPI-specific field or path knowledge.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass

import requests

from ..config import RunConfig
from ..dedup import endpoint_grounding_key
from ..schemas import Account

logger = logging.getLogger("agent.tools.http")

# Cap on how much of a response body is fed back into the model's context.
# Chosen to comfortably hold a handful of full JSON records (enough to spot
# extra/unexpected fields) while bounding cost on large list endpoints.
MAX_BODY_CHARS = 4000

# Bound on HttpToolkit._body_cache, in entries (2 per completed request: body
# + header block -- see HttpToolkit.__init__). Sized well past any run this
# project has actually exercised (largest tried: 150 steps, ~130 requests)
# so the evidence-grounding check in agent.validation can't silently reject a
# genuine finding whose evidence came from an observation older than the
# cache window, while still bounding memory against a pathological run with
# no configured step cap. At MAX_BODY_CHARS-sized entries this is a
# ~16 MB ceiling even at the full bound (4000 entries x 4000 chars).
EVIDENCE_BODY_CACHE_SIZE = 4000

# Guards against a specific runaway-generation failure mode observed during
# development: the model extended a path with the same segment(s) over and
# over (e.g. '/user/9/vehicle/1/repair/1/repair/1/repair/1/...'), producing
# ever-longer, never-valid paths. Each such call is technically distinct by
# exact-match repeat detection (agent.budget), since the string keeps
# growing, so it wasn't caught there. These thresholds are about detecting
# *pathological construction*, not about limiting legitimate deep paths --
# real crAPI paths are well under 100 characters and never contain a small
# segment pattern repeated three-plus times.
MAX_PATH_CHARS = 180
MAX_SEGMENT_REPEAT = 3

# A softer anti-drift signal (complementing the degenerate-path guard above,
# fix #4) was tried here and removed in a later simplification pass: a
# "hint" nudge attached to the observation once a streak of same-family
# failing requests got long (e.g. sequential-ID sweeps that all 404).
# Across every live run this project kept a log for at the time, the hint
# was never observed firing, so it was removed per the "smallest agent that
# reasons, no unused mechanisms" principle; see HISTORY.md's sixteenth-pass
# entry. A nineteenth/twentieth-pass live run then hit the same underlying
# problem class in a different shape than originally anticipated: instead of
# a sequential-ID path sweep, the model fixated on a handful of guessed
# endpoint names (e.g. '.../listVehiclesForUser') and, across ~40 of a
# 91-step run, kept retrying each with a longer, differently-assembled junk
# query string appended to the *same* path prefix -- every 404, never
# pivoting away. `_degenerate_path_reason` below (a per-call, self-contained
# check on one path string) can't catch this: no single one of those query
# strings repeats a segment/pattern within itself. Catching a cross-call
# "hammering one dead endpoint" pattern needs request *history*, which is
# what `_repeated_prefix_failure_reason` below adds -- and, unlike the
# removed hint (a nudge that never fired), this one is a hard refusal like
# `_degenerate_path_reason`, since the evidence this time is that the model
# doesn't reliably pivot on its own even after several identical failures.
MAX_PREFIX_FAILURE_STREAK = 3

# Live-run evidence (2026-07-29, twenty-second pass): a query-string sweep
# against '/identity/api/v2/user/dashboard' (?user_id=8, then ?user_id=9,
# tried three separate times across one 89-step run) returned the exact same
# primary-account body every time -- the endpoint ignores the parameter
# entirely and always returns the caller's own session-bound profile.
# MAX_PREFIX_FAILURE_STREAK above only tracks repeated *failures*, so a
# repeated *successful* no-op call like this one was invisible to it: three
# wasted turns confirming the same negative, each looking like a legitimate
# "try a different value" attempt to any failure-based check. This guard
# tracks the mirror-image case -- once the last MAX_IDENTICAL_RESPONSE_STREAK
# requests to one path prefix all succeeded with a byte-identical
# (status_code, body), whatever the model is varying (query string, request
# body) demonstrably has no effect, so a further variation is refused the
# same way a further failing guess already is.
MAX_IDENTICAL_RESPONSE_STREAK = 3

# -- discover_api_endpoints (endpoint discovery from the SPA's own JS) --------
# The project's single biggest recall gap, confirmed concretely by a 62-step
# live run (2026-10-02): crAPI is a single-page app whose real API surface
# (e.g. /community/api/v2/community/posts/recent, /identity/api/v2/vehicle/
# <carId>/location, /workshop/api/mechanic/mechanic_report) does NOT match the
# generic REST-noun prior the model guesses from. Across that run 55 of 57
# http_request calls were doomed path guesses; only /.env and /user/dashboard
# ever resolved, so the entire authenticated API surface -- where crAPI's real
# disclosure bugs live -- stayed invisible and the run found only the one
# blind-lucky /.env file.
#
# A human tester's first move on any SPA is to read the frontend's bundled
# JavaScript, which references every endpoint the app calls as a string
# literal. An earlier pass (see prompts.py's history) tried surfacing this via
# a prompt nudge to http_request the bundle directly and removed it, for a
# correct observation but the wrong fix: MAX_BODY_CHARS (4000) only ever shows
# a multi-MB webpack bundle's opening vendor boilerplate, never the app's own
# call sites. The right form is to extract tool-side -- fetch the bundle(s) in
# full here, never feed the raw JS into the model's context, and return only
# the handful of extracted API path strings. This is target-agnostic (every
# SPA ships a bundle; the regex keys on the generic word "api/", not on any
# crAPI segment) and stays on the compliant side of the "not a perfect
# scanner / no pre-scripted request sequence" scope line the same way
# list_id_candidates does: it reads static assets the model could fetch itself
# and returns inert strings -- it makes no API calls on the model's behalf, and
# the model still reasons about which discovered endpoints (if any) to request.
MAX_DISCOVERED_PATHS = 100  # cap on returned path count (token guard)
MAX_DISCOVERY_SCRIPTS = 20  # cap on how many <script src> bundles to fetch
MAX_DISCOVERY_BYTES_PER_SCRIPT = 16 * 1024 * 1024  # skip an absurdly large asset

_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+\.js)["']""", re.IGNORECASE)

# An API-path-shaped string literal inside the bundle: contains "api/" (the
# near-universal REST marker) surrounded only by path-safe characters,
# including the <param>/{param}/:param placeholder styles frontends embed.
# Generic by design -- it keys on the word "api", not on any crAPI-specific
# path segment.
_API_PATH_IN_JS_RE = re.compile(r"""["'`](/?[\w./:<>{}-]*\bapi/[\w./:<>{}-]+)["'`]""")


def _extract_api_paths_from_js(text: str) -> set[str]:
    """Return the set of API-path-shaped string literals in ``text`` (HTML or
    JS). Drops external/absolute URLs (anything with a scheme ``://``) so only
    same-origin API paths are surfaced. Pure/stateless so it can be unit-tested
    without a toolkit or any network.
    """
    out: set[str] = set()
    for match in _API_PATH_IN_JS_RE.findall(text):
        path = match.strip().rstrip("/")
        if path and "://" not in path:
            out.add(path)
    return out


def _ensure_leading_slash(path: str) -> str:
    """Normalize ``path`` to start with ``/`` -- shared by ``http_request``
    and ``list_id_candidates``, the two entry points that accept a raw path
    string from the model.
    """
    return path if path.startswith("/") else "/" + path


# Fix #42 (don't re-surface already-explored discovered paths, 2026-10-03).
# discover_api_endpoints returned the *same* full path list every call -- a
# live run called it at step 7 and again at step 30, getting a byte-identical
# dump that re-advertised endpoints the model had already requested in between,
# wasting its own attention (and, every call, re-fetching the static SPA bundle
# that can't have changed). Two complementary fixes below: the toolkit caches
# the per-page scan so a repeat call reuses it instead of re-fetching, and the
# returned paths are split into untried vs. already-visited by comparing each
# discovered path against request history.
#
# The comparison is placeholder/id-aware and suffix-based because the two path
# vocabularies differ: a discovered path is frontend-relative with a parameter
# placeholder (``api/v2/vehicle/<carId>/location``) while a visited path is the
# full thing the model actually requested, with a service prefix prepended and
# a concrete id substituted (``/identity/api/v2/vehicle/4bae..-../location``).
# Normalizing both -- lowercasing, collapsing every id-shaped or <param>/{param}
# /:param segment to a single ``*`` -- and asking whether a visited path's
# segment tuple *ends with* the discovered one bridges that gap without keying
# on any crAPI-specific segment.
_PLACEHOLDER_SEG_RE = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|:[\w-]+)$")


def _coverage_segments(path: str) -> tuple[str, ...]:
    """Normalize ``path`` to a tuple of coverage-comparison segments: split on
    ``/``, drop empties, lowercase, and collapse any id-shaped or placeholder
    (``<p>``/``{p}``/``:p``) segment to ``*`` so differently-instantiated forms
    of the same endpoint compare equal. Pure/stateless for unit testing.
    """
    segments: list[str] = []
    for raw in path.strip("/").split("/"):
        if not raw:
            continue
        if _PLACEHOLDER_SEG_RE.match(raw) or _ID_VALUE_RE.match(raw):
            segments.append("*")
        else:
            segments.append(raw.lower())
    return tuple(segments)


def _discovered_path_is_visited(rel_path: str, visited: list[tuple[str, ...]]) -> bool:
    """True if ``rel_path`` (a frontend-relative discovered path) matches a
    path already in request history. A discovered path matches when its
    normalized segment tuple is a suffix of some visited path's -- the visited
    one having extra leading service-prefix segments the model prepended.
    """
    target = _coverage_segments(rel_path)
    if not target:
        return False
    width = len(target)
    return any(len(v) >= width and v[-width:] == target for v in visited)


def _degenerate_path_reason(path: str) -> str | None:
    """Return a human-readable reason iff ``path`` looks like runaway/
    pathological construction rather than a genuine, deliberate request.
    """
    if len(path) > MAX_PATH_CHARS:
        return f"path is unusually long ({len(path)} chars > {MAX_PATH_CHARS})"
    segments = [s for s in path.split("/") if s]
    for period in range(1, 5):
        window = period * MAX_SEGMENT_REPEAT
        if len(segments) < window:
            continue
        tail = segments[-window:]
        if all(tail[i] == tail[i % period] for i in range(len(tail))):
            return f"path repeats the same {period}-segment pattern {MAX_SEGMENT_REPEAT}+ times in a row"
    return None


def _matching_by_prefix(history: list["RequestRecord"], method: str, path: str) -> list["RequestRecord"]:
    """Every past request to this same ``(method, path-before-'?')``,
    ignoring query string -- shared by ``_repeated_prefix_failure_reason``
    and ``_repeated_identical_response_reason``, which both key on this same
    prefix match and only differ in what they check about the matches.
    """
    prefix = path.split("?", 1)[0]
    return [r for r in history if r.method == method and r.path.split("?", 1)[0] == prefix]


def _repeated_prefix_failure_reason(
    history: list["RequestRecord"], method: str, path: str
) -> str | None:
    """Return a human-readable reason iff the last ``MAX_PREFIX_FAILURE_STREAK``
    requests to this same ``(method, path-before-'?')`` -- regardless of query
    string -- all failed (non-2xx status, or a connection/timeout error).

    Complements ``_degenerate_path_reason``: that one catches pathological
    construction visible within a single path string; this one catches a
    model hammering the same dead endpoint with different query-string
    guesses, which looks like a "new" request every time to any single-call
    check. Query string is deliberately ignored for the match -- the
    endpoint either exists at this path prefix or it doesn't, independent of
    which query parameters are attached to a given guess.
    """
    prefix = path.split("?", 1)[0]
    matching = _matching_by_prefix(history, method, path)
    if len(matching) < MAX_PREFIX_FAILURE_STREAK:
        return None
    recent = matching[-MAX_PREFIX_FAILURE_STREAK:]
    if all(r.error is not None or r.status_code is None or not (200 <= r.status_code < 300) for r in recent):
        return (
            f"the last {MAX_PREFIX_FAILURE_STREAK} requests to '{method} {prefix}' "
            f"(ignoring query string) all failed"
        )
    return None


def _repeated_identical_response_reason(
    history: list["RequestRecord"], method: str, path: str
) -> str | None:
    """Return a human-readable reason iff the last
    ``MAX_IDENTICAL_RESPONSE_STREAK`` requests to this same ``(method,
    path-before-'?')`` -- regardless of query string -- all succeeded (2xx)
    with a byte-identical ``(status_code, body)`` pair.

    Complements ``_repeated_prefix_failure_reason``: that one catches
    hammering a *dead* endpoint with different failing guesses; this one
    catches hammering a *live* endpoint with different parameter guesses
    that all silently resolve to the same answer. See
    ``MAX_IDENTICAL_RESPONSE_STREAK``'s module comment for the live-run
    evidence this was found from.
    """
    prefix = path.split("?", 1)[0]
    matching = _matching_by_prefix(history, method, path)
    if len(matching) < MAX_IDENTICAL_RESPONSE_STREAK:
        return None
    recent = matching[-MAX_IDENTICAL_RESPONSE_STREAK:]
    if not all(
        r.error is None and r.status_code is not None and 200 <= r.status_code < 300 and r.response_signature
        for r in recent
    ):
        return None
    if len({r.response_signature for r in recent}) == 1:
        return (
            f"the last {MAX_IDENTICAL_RESPONSE_STREAK} requests to '{method} {prefix}' "
            f"(ignoring query string) all returned a byte-identical successful response"
        )
    return None


# -- list_id_candidates (eighteenth pass) ------------------------------------
# See the module docstring's "list_id_candidates" bullet for the scope
# rationale (single-hop, model-directed, no autonomous requests).

# Bound on how many ID candidates / candidate paths a single call returns.
# Purely a token-cost guard (a large list response could otherwise surface
# dozens of IDs) -- not a correctness limit, since the model can always call
# again after acting on the first batch.
MAX_ID_CANDIDATES = 10

# Same shape as agent.dedup._ID_SEGMENT_RE, kept as a separate local copy
# rather than a shared import: that regex normalizes *path segments* for
# post-hoc finding de-duplication; this one validates *extracted JSON
# values* live during a run -- different modules, different concerns, and
# small enough that sharing it would add an import coupling for no real
# reduction in duplication risk.
_ID_VALUE_RE = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$"
)

# Matches the truncation note _truncate() appends (" ... [N more item(s)
# omitted ...]" or "... [truncated, N more chars]") so a truncated-but-valid
# JSON prefix can still be parsed for ID extraction by stripping it first.
_TRUNCATION_NOTE_RE = re.compile(r"\s*\.\.\.\s*\[[^\]]*\]$")

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Fix #41 (cross-account PII force-propose, 2026-10-03). The forced-propose
# mechanism (fix #32, agent.loop) was first wired only to a 200 on a
# sensitive FILE path (.env etc.). A live discovery run then reached
# /community/api/v2/community/posts/recent, observed five other users' email
# addresses in the body, and simply moved on -- the same "fetches the leak,
# never proposes it" failure fix #32 targets, but for cross-user data
# exposure rather than a config file. A prompt recognition rule
# (prompts.py RECON_TEXT step 5) converts this sometimes; a hard force is the
# reliable lever for a model that walks past observed PII.
#
# The deterministic, target-agnostic signal used below: a 200 response
# returned to caller A contains a personal email address that is NOT A's own
# email. Verified live against this crAPI stack before wiring it (2026-10-03):
# the real Challenge-4 leak (posts/recent) contains seed users'
# emails -- adam007@/pogba006@/robot001@example.com, none of them our own
# configured accounts, which is exactly why a narrower "known other-account
# email" signal would never fire here -- while /user/dashboard correctly
# contains only the caller's OWN email (excluded) and an empty vehicles list
# contains none. Role/system addresses (RFC 2142: postmaster, abuse,
# support, etc.) are filtered out so a site's own contact mailbox baked into
# a page is not mistaken for a user's leaked PII -- a generic concept, not a
# crAPI-specific denylist. This only ARMS a forced proposal; validation.py's
# evidence-grounding/scope gate and the bonus verifier still adjudicate
# acceptance, so a spurious trigger costs a turn, not a false finding.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# RFC 2142 role mailboxes plus the near-universal automated-sender local
# parts. Matched against the local part (before '@'), case-insensitively, so
# a non-personal address is never treated as a leaked user's PII.
_ROLE_EMAIL_LOCALPARTS = frozenset(
    {
        "postmaster", "abuse", "security", "webmaster", "hostmaster",
        "usenet", "news", "www", "uucp", "ftp",
        "info", "support", "sales", "marketing", "contact", "help",
        "admin", "administrator", "root", "noreply", "no-reply",
        "donotreply", "do-not-reply", "mailer-daemon", "notifications",
        "notification", "alerts", "team", "hello", "office", "billing",
    }
)


def _personal_emails_in(text: str) -> set[str]:
    """Lowercased personal email addresses in ``text``, with role/system
    mailboxes (``_ROLE_EMAIL_LOCALPARTS``) removed. See fix #41's module
    comment above for why role addresses are excluded.
    """
    found = {match.lower() for match in _EMAIL_RE.findall(text or "")}
    return {email for email in found if email.split("@", 1)[0] not in _ROLE_EMAIL_LOCALPARTS}


def _id_shape(value: str) -> str:
    """Classify an ID-shaped string as "numeric", "uuid", or "hex" -- used
    to only substitute a path segment with a same-shaped candidate (see
    ``list_id_candidates``). Added after a live run against real crAPI
    surfaced a concrete failure mode: ``GET /identity/api/v2/vehicle/{carId}
    /location`` only accepts a UUID for ``carId`` (a numeric id 400s with
    "Failed to convert 'carId'"), but a response can easily contain both a
    numeric ``id`` and a UUID field for unrelated nested objects -- without
    this check, the numeric one could be substituted into a UUID-typed slot
    and produce a candidate that is guaranteed to fail, wasting the model's
    next turn on a request that was never going to work.
    """
    if value.isdigit():
        return "numeric"
    if _UUID_RE.match(value):
        return "uuid"
    return "hex"


def _looks_like_id_key(key: str) -> bool:
    """Generic REST naming-convention check: bare ``id``/``uuid``, snake_case
    ``*_id``, or camelCase ``*Id``. Deliberately case-sensitive on the
    camelCase form (``key.endswith("Id")``, capital I) so it doesn't
    false-positive on ordinary words that happen to end in lowercase "id"
    (e.g. "valid", "paid", "grid") -- only the snake_case/bare-"id"/"uuid"
    checks are case-folded, since those already require an explicit
    underscore or exact match.

    The bare ``uuid`` case was added after a live run against real crAPI
    surfaced a genuine gap: ``GET /identity/api/v2/vehicle/vehicles``
    returns each vehicle's real path identifier under a field literally
    named ``uuid`` (not ``id`` or ``*_id``) -- confirmed live that this is
    also the *only* identifier accepted by the actual pivot path
    (``/vehicle/{carId}/location`` rejects the numeric ``id`` with a 400,
    "Failed to convert 'carId'"). Missing this key would have silently
    dropped the one identifier this tool most needed to surface for that
    endpoint family.
    """
    lowered = key.lower()
    return (
        lowered in ("id", "uuid")
        or lowered.endswith("_id")
        or lowered.endswith("_uuid")
        or key.endswith("Id")
        or key.endswith("Uuid")
    )


def _is_id_shaped_value(value: object) -> bool:
    if isinstance(value, bool):
        return False  # bool is an int subclass in Python; not an ID.
    if isinstance(value, int):
        return True
    if isinstance(value, str):
        return bool(_ID_VALUE_RE.match(value))
    return False


def _extract_id_candidates(body_text: str, limit: int = MAX_ID_CANDIDATES) -> list[dict]:
    """Walk a JSON response body for fields that look like IDs (see
    ``_looks_like_id_key``/``_is_id_shaped_value``), generic JSON-shape and
    naming-convention inspection only -- no crAPI-specific field names.
    Returns at most ``limit`` distinct ``{"field": ..., "value": ...}``
    entries, most-recently-encountered-during-walk order.
    """
    try:
        parsed = json.loads(body_text)
    except ValueError:
        try:
            parsed = json.loads(_TRUNCATION_NOTE_RE.sub("", body_text))
        except ValueError:
            return []

    found: list[dict] = []
    seen_values: set[str] = set()

    def walk(node: object) -> None:
        if len(found) >= limit:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if len(found) >= limit:
                    return
                if _looks_like_id_key(key) and _is_id_shaped_value(value):
                    value_str = str(value)
                    if value_str not in seen_values:
                        seen_values.add(value_str)
                        found.append({"field": key, "value": value})
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(parsed)
    return found[:limit]


def _find_top_level_array(parsed: object) -> tuple[list | None, str | None]:
    """If ``parsed`` is a JSON array, or a JSON object whose first
    array-valued field has more than one item, return ``(that array, the
    object's key or None for a bare array)``. Otherwise ``(None, None)`` --
    nothing here to truncate by item; the caller falls back to a plain
    character cut. Generic JSON-shape inspection only, no field names.
    """
    if isinstance(parsed, list):
        return parsed, None
    if isinstance(parsed, dict):
        for key, value in parsed.items():
            if isinstance(value, list) and len(value) > 1:
                return value, key
    return None, None


def _truncate(text: str, limit: int) -> tuple[str, bool, int]:
    """Return (possibly-truncated text, was_truncated, original_length).

    Prefers dropping whole trailing list items over cutting mid-record --
    see ``_find_top_level_array`` and the module docstring's "Response
    truncation" bullet for why. Falls back to a plain character cut when the
    body isn't JSON, or has no array shaped like a list-endpoint response.
    """
    original_length = len(text)
    if original_length <= limit:
        return text, False, original_length

    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    array, wrap_key = _find_top_level_array(parsed)

    if array:
        kept: list = []
        for item in array:
            trial = kept + [item]
            candidate = trial if wrap_key is None else {**parsed, wrap_key: trial}
            if len(json.dumps(candidate)) > limit:
                break
            kept.append(item)
        dropped = len(array) - len(kept)
        rendered = json.dumps(kept if wrap_key is None else {**parsed, wrap_key: kept})
        if not dropped:
            # Everything fit once re-serialized compactly (the original was
            # just more verbosely formatted) -- genuinely not truncated.
            return rendered, False, original_length
        note = f" ... [{dropped} more item(s) omitted from a {len(array)}-item list, {original_length} original chars]"
        return rendered + note, True, original_length

    return text[:limit] + f"... [truncated, {original_length - limit} more chars]", True, original_length


@dataclass
class RequestRecord:
    """One completed HTTP call. Fields are exactly what ``list_visited_endpoints``
    (the model's coverage-map tool) and the min-requests-before-finish gate
    (``agent.tools.control_tool.MIN_REQUESTS_BEFORE_FINISH``) consume --
    kept deliberately small rather than mirroring the full HTTP exchange.
    ``response_signature`` is the one exception: a short hash of
    ``(status_code, body)`` for a successful response, not the body itself
    -- cheap enough to keep for every request, and needed by
    ``_repeated_identical_response_reason`` to detect a live endpoint that
    silently ignores whatever the model is varying between calls.
    """

    step: int
    method: str
    path: str
    account: str | None
    status_code: int | None
    error: str | None
    response_signature: str | None = None


class AuthError(RuntimeError):
    """Raised when logging in as a configured account fails outright.

    This is *not* raised for a normal "invalid credentials" HTTP response
    (that's a legitimate observation the model should see and reason about);
    it's raised only when the account the operator configured cannot be
    authenticated at all (e.g. network failure, or crAPI rejecting the
    account the operator told us was valid), which means the run cannot
    proceed meaningfully.
    """


class HttpToolkit:
    """Owns the HTTP session, per-account auth tokens, and request history.

    One instance is created per run and shared by the agent loop; its two
    public tool methods (``http_request``, ``list_visited_endpoints``) are
    exposed to the model via ``agent.toolbox.ToolBox``.
    """

    def __init__(self, config: RunConfig) -> None:
        self._config = config
        # One requests.Session per account label (plus one for unauthenticated
        # calls, keyed by None), never shared across labels. A single shared
        # session previously meant any session-scoped cookie the target set
        # during one account's login/requests would silently persist onto a
        # later request nominally made "as" a different account -- undermining
        # the validity of the agent's core cross-account comparison technique.
        # _session_factory is a seam tests override to inject a fake session
        # without needing a real socket; production code always calls the
        # real requests.Session constructor.
        self._session_factory = requests.Session
        self._sessions: dict[str | None, requests.Session] = {}
        self._accounts: dict[str, Account] = {a.label: a for a in config.credentials.accounts}
        self._tokens: dict[str, str] = {}
        self.history: list[RequestRecord] = []
        # Bounded ring buffer of full (post-truncation) response bodies the
        # model has actually been shown, keyed only by recency. Used by
        # agent.validation to ground a proposed finding's evidence in a real
        # observation rather than a model paraphrase -- see that module's
        # docstring. Bounded so a very long run can't grow this unboundedly
        # (see EVIDENCE_BODY_CACHE_SIZE for why this specific size).
        # Each entry is (method, path, text): the request that produced the
        # body (or header block), paired with it. The (method, path) tag is
        # what lets agent.validation bind a finding's evidence to a response
        # from the request matching its *claimed* endpoint, rather than to any
        # body captured anywhere this run (fix #43).
        self._body_cache: deque[tuple[str, str, str]] = deque(maxlen=EVIDENCE_BODY_CACHE_SIZE)
        # Most-recent (method, body_text) actually shown to the model for a
        # given path, keyed on path alone -- feeds list_id_candidates. Not
        # separately bounded: it self-bounds to at most one entry per
        # distinct path visited this run, and total distinct paths is
        # already implicitly capped by the step/token budget.
        self._last_body_for_path: dict[str, tuple[str, str]] = {}
        # Fix #42: per-page cache of discover_api_endpoints' raw scan result
        # (page_path -> sorted discovered path list). A repeat call for the
        # same page reuses this instead of re-fetching the static SPA bundle,
        # which cannot have changed within a run. Visited/untried partitioning
        # is always recomputed fresh from current history, never cached.
        self._discovery_cache: dict[str, tuple[list[str], list[str]]] = {}

    def _record(self, *, step: int, method: str, path: str, account: str | None,
                status_code: int | None, error: str | None, response_signature: str | None = None) -> None:
        """Append one entry to ``self.history``. Small helper purely to
        avoid repeating the same ``RequestRecord(...)`` construction at each
        of ``http_request``'s three exit points (auth failure, connection
        failure, real response). ``response_signature`` is only ever passed
        by the real-response exit point.
        """
        self.history.append(
            RequestRecord(
                step=step,
                method=method,
                path=path,
                account=account,
                status_code=status_code,
                error=error,
                response_signature=response_signature,
            )
        )

    # -- auth -----------------------------------------------------------

    def account_labels(self) -> list[str]:
        return list(self._accounts.keys())

    def foreign_personal_emails(self, body_text: str, requesting_account: str | None) -> list[str]:
        """Personal email addresses in ``body_text`` that do NOT belong to
        ``requesting_account`` -- i.e. another party's contact PII returned
        to this caller. Sorted, de-duplicated; empty if none.

        This is the deterministic cross-user-disclosure signal fix #41 uses
        to arm a forced ``propose_finding`` turn (see ``agent.loop`` and the
        module comment above ``_EMAIL_RE``). Role/system mailboxes are
        already excluded by ``_personal_emails_in``; here we additionally
        drop the requesting account's OWN email (seeing your own address in
        your own dashboard is not disclosure). The other configured
        account's email is deliberately NOT excluded: as caller A, seeing
        account B's email IS cross-user exposure.
        """
        own = ""
        if requesting_account and requesting_account in self._accounts:
            own = self._accounts[requesting_account].email.lower()
        return sorted(email for email in _personal_emails_in(body_text) if email != own)

    def _session_for(self, label: str | None) -> requests.Session:
        """Return the (lazily-created) session dedicated to ``label`` (or
        the unauthenticated session if ``label`` is ``None``). See
        ``__init__`` for why sessions are per-label rather than shared.
        """
        session = self._sessions.get(label)
        if session is None:
            session = self._session_factory()
            self._sessions[label] = session
        return session

    def _login(self, account: Account) -> str:
        """Authenticate as ``account`` against crAPI's identity service and
        return a bearer token, raising ``AuthError`` if that's not possible.
        """
        url = self._config.target + "/identity/api/auth/login"
        try:
            resp = self._session_for(account.label).post(
                url,
                json={"email": account.email, "password": account.password},
                timeout=self._config.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            raise AuthError(f"login request for account '{account.label}' failed: {exc}") from exc

        if resp.status_code != 200:
            raise AuthError(
                f"login for account '{account.label}' returned HTTP {resp.status_code}: {resp.text[:300]!r}"
            )
        try:
            token = resp.json().get("token")
        except ValueError:
            token = None
        if not token:
            raise AuthError(f"login for account '{account.label}' did not return a token: {resp.text[:300]!r}")
        return token

    def _token_for(self, label: str) -> str:
        if label not in self._accounts:
            raise AuthError(f"unknown account label {label!r}; known labels: {self.account_labels()}")
        if label in self._tokens:
            return self._tokens[label]
        account = self._accounts[label]
        token = account.token or self._login(account)
        self._tokens[label] = token
        return token

    # -- the tool ---------------------------------------------------------

    def http_request(
        self,
        *,
        step: int,
        method: str,
        path: str,
        account: str | None = None,
        headers: dict | None = None,
        json_body: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        """Execute one HTTP request against the target and return an
        observation dict. This is the function bound to the ``http_request``
        tool-call schema (see ``agent.toolbox``).

        Parameters mirror the tool-call schema exactly; ``step`` is supplied
        by the loop (not the model) purely for correlating this call with
        ``run.log`` and the request history, so it never counts against the
        model's own reasoning about arguments.
        """
        method = method.upper()
        path = _ensure_leading_slash(path)

        degenerate_reason = _degenerate_path_reason(path)
        if degenerate_reason:
            # Observed during development: without this guard, the model can
            # get stuck extending a path with the same segment over and over
            # (e.g. '.../repair/1/repair/1/repair/1/...'), each call technically
            # "new" by exact-match repeat detection (agent.budget) since the
            # string keeps growing, but unproductive by any real measure and
            # a growing token-cost drain. Refused without dispatching -- no
            # network call, no history/budget entry beyond the turn itself --
            # and reported back so the model can course-correct.
            return {
                "error": "degenerate_path",
                "message": (
                    f"Refusing to send this request: {degenerate_reason}. Try a shorter, "
                    f"more targeted path instead of extending this one further."
                ),
            }

        failure_reason = _repeated_prefix_failure_reason(self.history, method, path)
        if failure_reason:
            # See MAX_PREFIX_FAILURE_STREAK's module-level comment: a live run
            # burned ~47% of its post-finding budget hammering a handful of
            # guessed endpoints with ever-different query strings, never
            # pivoting after repeated 404s. Refused without dispatching -- no
            # network call, no history/budget entry beyond the turn itself --
            # same shape as the degenerate-path refusal above.
            return {
                "error": "repeated_prefix_failure",
                "message": (
                    f"Refusing to send this request: {failure_reason}. This endpoint likely "
                    f"doesn't exist at this path -- try a genuinely different path instead of "
                    f"another query-string variation of the same one."
                ),
            }

        identical_reason = _repeated_identical_response_reason(self.history, method, path)
        if identical_reason:
            # See MAX_IDENTICAL_RESPONSE_STREAK's module-level comment: a live
            # run confirmed the same negative three times (a query parameter
            # the endpoint silently ignores) before pivoting. Refused without
            # dispatching -- no network call, no history/budget entry beyond
            # the turn itself -- same shape as the two guards above.
            return {
                "error": "repeated_identical_response",
                "message": (
                    f"Refusing to send this request: {identical_reason}. Whatever you're "
                    f"varying (query string, request body, etc.) isn't changing the result -- "
                    f"try a genuinely different path, method, or account instead."
                ),
            }
        url = self._config.target + path

        req_headers = dict(headers or {})
        auth_note = None
        if account:
            try:
                token = self._token_for(account)
                req_headers["Authorization"] = f"Bearer {token}"
                auth_note = account
            except AuthError as exc:
                # Surface the auth failure as a normal observation: the model
                # should be able to reason about "I couldn't authenticate as
                # X", not have the whole run crash.
                self._record(step=step, method=method, path=path, account=account, status_code=None, error=str(exc))
                return {"error": f"auth_error: {exc}", "status_code": None}

        start = time.monotonic()
        try:
            resp = self._session_for(account).request(
                method,
                url,
                headers=req_headers or None,
                params=query,
                json=json_body,
                timeout=self._config.request_timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._record(step=step, method=method, path=path, account=auth_note, status_code=None, error=str(exc))
            return {"error": f"request_failed: {exc}", "status_code": None, "elapsed_ms": round(elapsed_ms, 1)}

        elapsed_ms = (time.monotonic() - start) * 1000
        body_text, truncated, original_length = _truncate(resp.text, MAX_BODY_CHARS)
        response_signature = hashlib.sha256(
            f"{resp.status_code}|{body_text}".encode("utf-8", errors="replace")
        ).hexdigest()[:12]

        self._record(
            step=step,
            method=method,
            path=path,
            account=auth_note,
            status_code=resp.status_code,
            error=None,
            response_signature=response_signature,
        )
        self._body_cache.append((method, path, body_text))
        self._last_body_for_path[path] = (method, body_text)
        # Response headers can themselves be the disclosure (in
        # scope: "secrets/tokens leaked in ... headers"), so make them
        # groundable too by folding them into the same cache as one string
        # -- tagged with the same (method, path) so a header-based finding
        # still binds to its own endpoint.
        if resp.headers:
            self._body_cache.append(
                (method, path, " ".join(f"{k}: {v}" for k, v in resp.headers.items()))
            )

        result = {
            "status_code": resp.status_code,
            # body-related fields come before headers deliberately: when an
            # old observation ages out of the compaction window,
            # agent.loop._compact_transcript keeps only this dict's first
            # OLD_TOOL_MESSAGE_CLIP_CHARS characters -- a prefix cut, not a
            # summary. With headers listed first, a real response's ~500+
            # chars of routine security/infra headers (Server, Date,
            # Cache-Control, etc.) consumed the entire budget, so a
            # compacted observation showed status_code and nothing else --
            # the body (where information-disclosure signal actually lives
            # in the overwhelming majority of cases) was silently dropped
            # in full for every observation older than the last
            # KEEP_FULL_TOOL_MESSAGES (loop.py). Ordering
            # body first means the truncation budget is spent on the field
            # most likely to carry the signal the model needs to recall.
            "body": body_text,
            "body_truncated": truncated,
            "body_original_length": original_length,
            "headers": dict(resp.headers),
            "elapsed_ms": round(elapsed_ms, 1),
            "error": None,
        }
        return result

    def recent_bodies(self) -> list[str]:
        """Every response body (and header block) shown to the model so
        far, most-recent-last. See ``_body_cache`` for why this exists.
        """
        return [text for _method, _path, text in self._body_cache]

    def bodies_for_endpoint(self, endpoint: str | None) -> list[str]:
        """Response bodies/header blocks captured this run from requests whose
        ``(method, normalized path)`` matches ``endpoint`` (a finding's claimed
        ``"METHOD /path"``).

        This is the request-binding half of evidence grounding (fix #43): the
        model proposed a cross-user BOLA on ``GET /…/dashboard/{userId}`` and
        quoted, as proof, a body that had in fact come from a *different*
        request (the secondary account fetching its own ``/…/dashboard``); the
        claimed ``/dashboard/9`` path itself had only ever 404'd. The old
        grounding check accepted it because the quoted bytes existed *somewhere*
        in the cache. Restricting the candidate bodies to those actually
        produced by the claimed endpoint closes that gap without rejecting a
        genuine finding (whose evidence does come from its own endpoint).

        ``endpoint`` ``None`` or not parseable as ``METHOD /path`` falls back to
        every captured body -- the pre-fix behavior -- so this can only ever
        *narrow* the candidate set for a well-formed finding, never newly reject
        one whose endpoint string is malformed.
        """
        if endpoint is None:
            return self.recent_bodies()
        parts = endpoint.strip().split(None, 1)
        # Same shape the Finding schema enforces ("METHOD /path"): an alpha
        # method and a path starting with "/". Anything else is malformed, so
        # fall back to every captured body rather than silently returning none.
        if len(parts) != 2 or not parts[0].isalpha() or not parts[1].startswith("/"):
            return self.recent_bodies()
        want = endpoint_grounding_key(parts[0], parts[1])
        return [
            text
            for method, path, text in self._body_cache
            if endpoint_grounding_key(method, path) == want
        ]

    def list_visited_endpoints(self) -> dict:
        """Return a compact summary of every request made so far this run.

        This is the chosen auxiliary tool: it gives the model a
        coverage map ("what have I already tried, with which account, and
        what happened") without needing to re-read full response bodies,
        which is both a token-economy win and a concrete anti-repeat aid
        (see ``agent.budget`` for the harder, code-enforced repeat cap this
        complements).
        """
        return {
            "count": len(self.history),
            "requests": [
                {
                    "step": r.step,
                    "method": r.method,
                    "path": r.path,
                    "account": r.account,
                    "status_code": r.status_code,
                    "error": r.error,
                }
                for r in self.history
            ],
        }

    def list_id_candidates(self, *, path: str) -> dict:
        """Inspect the cached response body from a single already-made
        request and return ID-shaped values found in it, plus (when
        ``path`` itself has an ID-shaped segment) ready-to-use candidate
        paths with that segment substituted -- a single-hop pivot aid for
        cross-resource/BOLA-style probing. See the module docstring's
        "list_id_candidates" bullet for why this is scope-compliant where a
        multi-hop autonomous crawler would not be: no HTTP request is made
        here, only inspection of a response the model's own prior
        ``http_request`` call already produced.
        """
        path = _ensure_leading_slash(path)
        cached = self._last_body_for_path.get(path)
        if cached is None:
            return {
                "error": "no_cached_response",
                "message": f"No response body cached for {path!r} yet -- call http_request on it first.",
            }
        method, body_text = cached

        path_segments = path.split("/")
        ids = [
            entry
            for entry in _extract_id_candidates(body_text)
            # Exclude an ID that just restates a segment already present in
            # `path` itself -- e.g. GET /vehicle/8 returning {"id": 8} is a
            # self-reference, not a new pivot candidate.
            if str(entry["value"]) not in path_segments
        ]

        candidate_paths: list[str] = []
        pivot_indices = [i for i, seg in enumerate(path_segments) if _ID_VALUE_RE.match(seg)]
        if ids and pivot_indices:
            target_index = pivot_indices[-1]
            target_shape = _id_shape(path_segments[target_index])
            already_tried = {(r.method, r.path) for r in self.history}
            seen_values: set[str] = set()
            for entry in ids:
                value = str(entry["value"])
                # Only substitute a same-shaped value (numeric-for-numeric,
                # uuid-for-uuid) -- see _id_shape's docstring for the live
                # failure mode this avoids (a numeric id can't satisfy a
                # UUID-typed path segment, and vice versa).
                if value in seen_values or _id_shape(value) != target_shape:
                    continue
                seen_values.add(value)
                new_segments = list(path_segments)
                new_segments[target_index] = value
                candidate = "/".join(new_segments)
                if (method, candidate) not in already_tried:
                    candidate_paths.append(candidate)
                if len(candidate_paths) >= MAX_ID_CANDIDATES:
                    break

        return {
            "source_path": path,
            "source_method": method,
            "ids_found": ids,
            "candidate_paths": candidate_paths,
        }

    def _resolve_script_url(self, page_path: str, src: str) -> str:
        """Resolve a ``<script src>`` value against the page it was found on.
        Handles absolute URLs, root-relative (``/foo.js``) and page-relative
        (``static/js/main.js``) forms.
        """
        if src.startswith(("http://", "https://")):
            return src
        if src.startswith("/"):
            return self._config.target + src
        base = page_path.rsplit("/", 1)[0]  # directory of the page
        return f"{self._config.target}{base}/{src}"

    def discover_api_endpoints(self, *, page_path: str = "/") -> dict:
        """Fetch a frontend page and its referenced JavaScript bundles and
        return the API path literals embedded in them -- the real endpoint
        surface the app itself calls, which is almost always far more accurate
        than guessing REST resource nouns. See this module's
        ``discover_api_endpoints`` comment block for the full rationale and the
        scope reasoning (reads static assets, returns inert strings, makes no
        API calls on the model's behalf).

        The raw JS/HTML is read in full here and never returned to the model;
        only the extracted, deduplicated path list is. Paths are frontend-
        relative (e.g. ``api/v2/vehicle/vehicles``); the model prepends a known
        service prefix to form a full path and decides what, if anything, to
        request.
        """
        page_path = _ensure_leading_slash(page_path)

        # Fix #42: a repeat scan of the same page reuses the cached result
        # instead of re-fetching the static SPA bundle (it cannot change within
        # a run). ``None`` cache value means an earlier scan failed outright --
        # retry it rather than caching a failure.
        cached = self._discovery_cache.get(page_path)
        if cached is not None:
            paths, scanned = cached
            rescanned = False
        else:
            result = self._scan_page_for_api_paths(page_path)
            if "error" in result:
                return result
            paths, scanned = result["paths"], result["scanned"]
            self._discovery_cache[page_path] = (paths, scanned)
            rescanned = True

        capped = paths[:MAX_DISCOVERED_PATHS]
        truncated = len(paths) > MAX_DISCOVERED_PATHS

        # Partition against request history (always recomputed, never cached):
        # which discovered endpoints has the model already requested, and which
        # are still untried. Steers it to new surface and off already-explored
        # paths -- the whole point of this fix.
        visited_norm = [_coverage_segments(r.path) for r in self.history]
        untried = [p for p in capped if not _discovered_path_is_visited(p, visited_norm)]
        already_visited = [p for p in capped if _discovered_path_is_visited(p, visited_norm)]

        if rescanned:
            scan_note = "Scanned the frontend bundle(s). "
        else:
            scan_note = (
                "Reused the earlier scan of this page (the static frontend "
                "bundle cannot change within a run); no new request was made. "
            )
        return {
            "source_page": page_path,
            "scripts_scanned": scanned,
            "rescanned": rescanned,
            "discovered_paths": capped,
            "untried_paths": untried,
            "already_visited_paths": already_visited,
            "count": len(capped),
            "untried_count": len(untried),
            "discovered_paths_truncated": truncated,
            "note": (
                scan_note
                + "Focus on 'untried_paths' -- 'already_visited_paths' are "
                "endpoints you have already requested this run, so re-fetching "
                "them observes nothing new. These paths were extracted verbatim "
                "from the frontend's own code and are relative to a service "
                "root. Prepend one of the service prefixes you were told about "
                "(see the topology in your instructions) to form a full path, "
                "then fetch the interesting untried ones with http_request -- "
                "prioritize this platform's business resources over "
                "account-management scaffolding. A <param> or {param} segment "
                "is a placeholder you substitute a real id/uuid for "
                "(list_id_candidates can suggest one from a list response). "
                "This made no API calls for you; you decide what to request and "
                "with which account (including BOLA cross-account checks)."
            ),
        }

    def _scan_page_for_api_paths(self, page_path: str) -> dict:
        """Fetch ``page_path`` and its referenced JS bundles and extract the
        API path literals. Returns ``{"paths": sorted list, "scanned": list of
        script srcs}`` on success, or ``{"error": ...}`` if the page itself
        could not be fetched. Split out from ``discover_api_endpoints`` so the
        latter can cache this result and skip re-fetching on a repeat call.
        """
        session = self._session_for(None)
        timeout = self._config.request_timeout_seconds
        try:
            root = session.request(
                "GET", self._config.target + page_path, timeout=timeout, allow_redirects=True
            )
        except requests.RequestException as exc:
            return {"error": f"request_failed: {exc}", "discovered_paths": [], "count": 0}

        html = root.text or ""
        found = _extract_api_paths_from_js(html)  # some apps inline paths in the page too

        scripts: list[str] = []
        for src in _SCRIPT_SRC_RE.findall(html):
            if src not in scripts:
                scripts.append(src)

        scanned: list[str] = []
        for src in scripts[:MAX_DISCOVERY_SCRIPTS]:
            try:
                resp = session.request(
                    "GET", self._resolve_script_url(page_path, src), timeout=timeout, allow_redirects=True
                )
            except requests.RequestException:
                continue
            text = resp.text or ""
            if len(text) > MAX_DISCOVERY_BYTES_PER_SCRIPT:
                continue
            scanned.append(src)
            found |= _extract_api_paths_from_js(text)

        return {"paths": sorted(found), "scanned": scanned}
