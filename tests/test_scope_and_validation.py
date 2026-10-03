"""Offline unit tests for the scope gate and finding validation. No network or live crAPI instance required -- these exercise the
pure-Python decision logic in isolation, which is exactly the layer that
must be correct regardless of what any particular LLM run happens to find.
"""

from __future__ import annotations

import unittest

from agent.scope import check_scope
from agent.validation import MIN_WHY_DISCLOSURE_CHARS, _normalize, validate_finding
from tests._helpers import StubToolkit as _StubToolkit


class ScopeGateTests(unittest.TestCase):
    def test_allows_plain_disclosure_language(self) -> None:
        allowed, reason = check_scope(
            "Other user's email exposed in dashboard response",
            "The /identity/api/v2/user/dashboard endpoint returns another account's email "
            "address and phone number to the calling user, which should not be visible.",
        )
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_rejects_sql_injection_language(self) -> None:
        allowed, reason = check_scope(
            "SQL injection dumps user table",
            "Using a crafted SQL injection payload in the coupon field returns all rows.",
        )
        self.assertFalse(allowed)
        self.assertIn("sql injection", reason)

    def test_rejects_ssrf_language(self) -> None:
        allowed, _ = check_scope("SSRF to internal metadata", "Triggering SSRF against google.com")
        self.assertFalse(allowed)

    def test_rejects_rate_limiting_language(self) -> None:
        allowed, _ = check_scope("Rate limit bypass", "No rate-limit on contact mechanic form allows DoS")
        self.assertFalse(allowed)

    def test_rejects_rce_adjacent_to_punctuation(self) -> None:
        # Regression test: an earlier version matched "rce" via the space-
        # padded substring pair " rce"/"rce ", which "(RCE)" evades entirely
        # (bounded by parens on both sides, not spaces). The word-boundary
        # regex must still catch it.
        allowed, reason = check_scope(
            "Debug endpoint enables (RCE)",
            "A crafted payload to this debug endpoint enables (RCE) on the host.",
        )
        self.assertFalse(allowed)
        self.assertIn("rce", reason)

    def test_does_not_reject_brute_force_as_rce(self) -> None:
        # "force" contains the substring "rce" -- the word-boundary regex
        # must not false-trigger on it the way a naive unbounded "rce"
        # substring check would.
        allowed, reason = check_scope(
            "Login endpoint has no protection",
            "Nothing wrong here, just describing a brute force attempt that failed.",
        )
        # (This still gets rejected -- but for "brute force", not "rce".)
        self.assertFalse(allowed)
        self.assertIn("brute force", reason)
        self.assertNotIn("'rce'", reason)

    def test_rejects_hyphenated_mass_assignment(self) -> None:
        # Regression test: only the space form of "mass assignment" was
        # ever explicitly listed; the hyphenated form must now also be
        # caught via the flexible word/hyphen separator, not just the
        # keywords ("rate limit", "brute force") that happened to have a
        # manually-added hyphen duplicate before.
        allowed, reason = check_scope(
            "Profile update accepts extra fields",
            "This is a mass-assignment issue where extra fields are accepted.",
        )
        self.assertFalse(allowed)
        self.assertIn("mass assignment", reason)

    def test_bola_style_finding_is_allowed_when_framed_as_disclosure(self) -> None:
        # Assignment scope explicitly includes "other users' records inside a
        # list response" -- this must NOT be rejected just because the root
        # cause is an authorization gap, as long as it's described as a
        # disclosure and doesn't use the technique's name.
        allowed, reason = check_scope(
            "Vehicle endpoint returns another account's VIN and location",
            "Requesting /identity/api/v2/vehicle/<id> with a different numeric id than the "
            "caller's own vehicle returns full VIN and location data belonging to another user, "
            "who did not intend for this account to see it.",
        )
        self.assertTrue(allowed, reason)


class FindingValidationTests(unittest.TestCase):
    def test_rejects_evidence_not_grounded_in_any_captured_response(self) -> None:
        toolkit = _StubToolkit(bodies=['{"id": 1, "name": "unrelated"}'])
        result = validate_finding(
            title="Fabricated leak",
            why_disclosure="This endpoint definitely leaks the admin password to everyone, trust me.",
            evidence="admin_password: hunter2222222",
            toolkit=toolkit,
        )
        self.assertFalse(result.accepted)
        self.assertIn("not match any response", result.reason)

    def test_accepts_evidence_grounded_in_a_captured_response(self) -> None:
        captured_body = '{"id": 8, "email": "victim.user@example.com", "role": "ROLE_USER"}'
        toolkit = _StubToolkit(bodies=[captured_body])
        result = validate_finding(
            title="Other user's email exposed",
            why_disclosure=(
                "The dashboard endpoint returned victim.user@example.com while authenticated as "
                "a different account, exposing another user's email address unintentionally."
            ),
            evidence='Response included "email": "victim.user@example.com" for a different account',
            toolkit=toolkit,
        )
        self.assertTrue(result.accepted, result.reason)

    def test_extremely_short_evidence_with_no_extractable_snippet_is_rejected(self) -> None:
        # _evidence_is_grounded's fallback for evidence too short/generic to
        # extract a distinctive >=12-char snippet from (no long alnum run,
        # no quoted substring) falls back to matching the *whole* evidence
        # string -- but only if that whole string itself is still >= 12
        # chars; below that, `candidates` is empty and grounding always
        # fails, even if the short string genuinely does appear in a
        # captured body. Previously untested branch.
        toolkit = _StubToolkit(bodies=["the word short appears right here"])
        result = validate_finding(
            title="Something leaked",
            why_disclosure="This endpoint leaks a short value to unauthorized callers, which is bad.",
            evidence="short",  # 5 chars, under the 12-char extraction threshold
            toolkit=toolkit,
        )
        self.assertFalse(result.accepted)
        self.assertIn("not match any response", result.reason)

    def test_accepts_evidence_with_literal_double_escaped_newlines(self) -> None:
        # Regression test: a real live run had the model emit evidence
        # containing a literal two-character backslash-n (over-escaped when
        # it built the tool call's JSON arguments) instead of an actual
        # newline, even though the content was otherwise a verbatim copy of
        # a genuinely captured response body. The old _normalize only
        # collapsed real whitespace, so the stray backslash split each
        # candidate token and left a leftover "n" glued onto the next one
        # (e.g. "crapi\\nDB_USER" -> "ndb_user" instead of "db_user"),
        # which then never matched the correctly-formatted captured body --
        # silently rejecting a genuinely grounded finding as fabricated.
        captured_body = "DB_NAME=crapi\nDB_USER=crapi\nDB_PASSWORD=crapi\nMONGO_DB_HOST=mongodb\n"
        toolkit = _StubToolkit(bodies=[captured_body])
        evidence_with_literal_backslash_n = "DB_NAME=crapi\\nDB_USER=crapi\\nMONGO_DB_HOST=mongodb"
        result = validate_finding(
            title="Database credentials exposed via /.env",
            why_disclosure=(
                "The /.env file returns plaintext database credentials to any unauthenticated "
                "caller, exposing DB_USER and MONGO_DB_HOST which should never be public."
            ),
            evidence=evidence_with_literal_backslash_n,
            toolkit=toolkit,
        )
        self.assertTrue(result.accepted, result.reason)

    def test_normalize_collapses_uppercase_escape_letters_on_first_pass(self) -> None:
        # Fix #34, caught live by hypothesis fuzzing (test_normalize_is_idempotent
        # in test_property_based.py): the old lowercase-last ordering only
        # case-folded an uppercase \N/\R/\T *after* the backslash-collapse
        # regex had already run (which only matches lowercase n/r/t), so a
        # single _normalize call left an uppercase escape sequence uncollapsed
        # -- it only vanished on a second application. validate_finding calls
        # _normalize exactly once, so this was a real, reachable gap, not
        # just an idempotency nicety.
        self.assertEqual(_normalize("crapi\\NDB_USER"), "crapi db_user")
        self.assertEqual(_normalize("\\R\\T"), "")

    def test_rejects_why_disclosure_too_short(self) -> None:
        toolkit = _StubToolkit(bodies=["irrelevant"])
        result = validate_finding(
            title="Something leaked",
            why_disclosure="it leaks",  # well under MIN_WHY_DISCLOSURE_CHARS
            evidence="irrelevant",
            toolkit=toolkit,
        )
        self.assertLess(len("it leaks"), MIN_WHY_DISCLOSURE_CHARS)
        self.assertFalse(result.accepted)

    def test_rejects_why_disclosure_that_just_restates_title(self) -> None:
        toolkit = _StubToolkit(bodies=["irrelevant response body of some length"])
        title = "The endpoint leaks another user's phone number to the caller account"
        result = validate_finding(
            title=title,
            why_disclosure=title,
            evidence="irrelevant response body of some length",
            toolkit=toolkit,
        )
        self.assertFalse(result.accepted)

    def test_rejects_why_disclosure_that_pads_the_title_with_trivial_words(self) -> None:
        # A near-restatement that dodges the exact-match check above by
        # tacking on a few trivial extra words, without adding any real
        # reasoning about what leaked/to whom/why it's unintended.
        toolkit = _StubToolkit(bodies=["irrelevant response body of some length"])
        title = "The endpoint leaks another user's phone number to the caller account"
        result = validate_finding(
            title=title,
            why_disclosure=title + " and users never agreed to it",
            evidence="irrelevant response body of some length",
            toolkit=toolkit,
        )
        self.assertFalse(result.accepted)
        self.assertIn("restates the title", result.reason)

    def test_out_of_scope_candidate_rejected_before_grounding_check(self) -> None:
        # Even with perfectly grounded evidence, an out-of-scope technique
        # mention must still be rejected by the scope layer.
        toolkit = _StubToolkit(bodies=["' OR 1=1 -- results dump"])
        result = validate_finding(
            title="SQL injection leaks table",
            why_disclosure="A SQL injection payload in the search field returns the full table.",
            evidence="' OR 1=1 -- results dump",
            toolkit=toolkit,
        )
        self.assertFalse(result.accepted)
        self.assertIn("sql injection", result.reason)


if __name__ == "__main__":
    unittest.main()
