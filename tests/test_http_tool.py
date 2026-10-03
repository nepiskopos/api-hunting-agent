"""Offline tests for agent.tools.http_tool.HttpToolkit. The underlying
``requests.Session`` is replaced with a small fake -- no real network calls,
no live crAPI instance needed. Covers auth/token caching, unauthenticated
requests, the degenerate-path guard, response body truncation, evidence
grounding via recent_bodies(), and list_id_candidates.
"""

from __future__ import annotations

import unittest

import json

import requests

from agent.config import RunConfig
from agent.schemas import Account, Credentials
from agent.tools.http_tool import (
    EVIDENCE_BODY_CACHE_SIZE,
    MAX_BODY_CHARS,
    MAX_DISCOVERY_BYTES_PER_SCRIPT,
    MAX_IDENTICAL_RESPONSE_STREAK,
    MAX_PREFIX_FAILURE_STREAK,
    AuthError,
    HttpToolkit,
    RequestRecord,
    _coverage_segments,
    _discovered_path_is_visited,
    _extract_api_paths_from_js,
    _extract_id_candidates,
    _id_shape,
    _is_id_shaped_value,
    _looks_like_id_key,
    _personal_emails_in,
    _repeated_identical_response_reason,
    _repeated_prefix_failure_reason,
    _truncate,
)
from tests._helpers import FakeResponse as _FakeResponse


class _FakeSession:
    """Stands in for requests.Session: .post() is used only by _login,
    .request() by http_request. Both are scriptable via responses_by_url.
    """

    def __init__(self):
        self.post_responses: list[_FakeResponse] = []
        self.request_responses: list[_FakeResponse] = []
        self.requests_made: list[dict] = []

    def post(self, url, json=None, timeout=None):
        self.requests_made.append({"method": "POST", "url": url, "json": json})
        return self.post_responses.pop(0)

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, allow_redirects=None):
        self.requests_made.append({"method": method, "url": url, "headers": headers, "json": json})
        return self.request_responses.pop(0)


def _make_config(accounts=None) -> RunConfig:
    accounts = accounts or [Account(label="primary", email="a@example.com", password="pw")]
    return RunConfig(
        target="http://target.test",
        credentials=Credentials(accounts=accounts),
        llm_base_url="http://llm.test",
        llm_api_key="key",
        llm_model=None,
    )


def _toolkit_with_fake_session(accounts=None) -> tuple[HttpToolkit, _FakeSession]:
    toolkit = HttpToolkit(_make_config(accounts))
    fake = _FakeSession()
    # HttpToolkit now creates one requests.Session per account label (see
    # HttpToolkit.__init__), so tests inject a factory returning the same
    # pre-scripted fake for every label rather than setting a single shared
    # session attribute -- every test here only ever uses one label (or
    # None), so this reproduces the old single-fake-session behavior exactly.
    toolkit._session_factory = lambda: fake
    return toolkit, fake


class AuthTests(unittest.TestCase):
    def test_login_caches_token_across_calls(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.post_responses = [_FakeResponse(text='{"token": "abc123"}')]
        fake.request_responses = [_FakeResponse(text="{}"), _FakeResponse(text="{}")]

        toolkit.http_request(step=1, method="GET", path="/x", account="primary")
        toolkit.http_request(step=2, method="GET", path="/y", account="primary")

        self.assertEqual(len(fake.post_responses), 0)  # only ever logged in once
        auth_headers = [r["headers"]["Authorization"] for r in fake.requests_made if r["method"] == "GET"]
        self.assertTrue(all(h == "Bearer abc123" for h in auth_headers))

    def test_pretoken_account_skips_login_entirely(self) -> None:
        toolkit, fake = _toolkit_with_fake_session([Account(label="primary", email="a@x.com", token="pretoken")])
        fake.request_responses = [_FakeResponse(text="{}")]
        toolkit.http_request(step=1, method="GET", path="/x", account="primary")
        self.assertEqual(fake.requests_made[0]["headers"]["Authorization"], "Bearer pretoken")

    def test_unauthenticated_request_has_no_authorization_header(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text="{}")]
        toolkit.http_request(step=1, method="GET", path="/x", account=None)
        self.assertIsNone(fake.requests_made[0]["headers"])

    def test_login_failure_surfaces_as_observation_not_exception(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.post_responses = [_FakeResponse(status_code=401, text='{"message": "bad creds"}')]
        result = toolkit.http_request(step=1, method="GET", path="/x", account="primary")
        self.assertIn("auth_error", result["error"])

    def test_unknown_account_label_raises_auth_error_internally_but_returns_observation(self) -> None:
        toolkit, _ = _toolkit_with_fake_session()
        result = toolkit.http_request(step=1, method="GET", path="/x", account="nonexistent")
        self.assertIn("auth_error", result["error"])

    def test_token_for_unknown_label_raises(self) -> None:
        toolkit, _ = _toolkit_with_fake_session()
        with self.assertRaises(AuthError):
            toolkit._token_for("nonexistent")

    def test_login_connection_error_surfaces_as_observation_not_exception(self) -> None:
        # _login's `except requests.RequestException` path -- previously
        # untested (only the "bad status code" and "no token in response"
        # AuthError paths had coverage).
        toolkit, fake = _toolkit_with_fake_session()
        fake.post = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("refused"))
        result = toolkit.http_request(step=1, method="GET", path="/x", account="primary")
        self.assertIn("auth_error", result["error"])
        self.assertIn("refused", result["error"])

    def test_login_non_json_response_raises_auth_error_not_valueerror(self) -> None:
        # _login's `except ValueError: token = None` path (resp.json() on a
        # non-JSON 200 body) -- previously untested.
        toolkit, fake = _toolkit_with_fake_session()
        fake.post_responses = [_FakeResponse(status_code=200, text="not json at all")]
        result = toolkit.http_request(step=1, method="GET", path="/x", account="primary")
        self.assertIn("auth_error", result["error"])
        self.assertIn("did not return a token", result["error"])

    def test_login_json_response_missing_token_raises_auth_error(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.post_responses = [_FakeResponse(status_code=200, text='{"message": "ok but no token field"}')]
        result = toolkit.http_request(step=1, method="GET", path="/x", account="primary")
        self.assertIn("auth_error", result["error"])
        self.assertIn("did not return a token", result["error"])

    def test_request_connection_error_surfaces_as_observation_not_exception(self) -> None:
        # http_request's own `except requests.RequestException` path
        # (post-auth, the actual GET/POST/etc. dispatch) -- previously
        # untested.
        toolkit, fake = _toolkit_with_fake_session()
        fake.request = lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.Timeout("timed out"))
        result = toolkit.http_request(step=1, method="GET", path="/x", account=None)
        self.assertIn("request_failed", result["error"])
        self.assertIn("timed out", result["error"])
        self.assertIsNone(result["status_code"])


class SessionIsolationTests(unittest.TestCase):
    """Regression tests for the fix to a real bug: HttpToolkit used to share
    one requests.Session across every account, so a session-scoped cookie
    the target set for one account could silently leak onto a request made
    "as" a different account. Each account label (and unauthenticated calls)
    must now get its own, never-shared session.
    """

    def test_distinct_accounts_get_distinct_sessions(self) -> None:
        toolkit = HttpToolkit(
            _make_config([Account(label="primary", email="a@x.com", password="pw"), Account(label="secondary", email="b@x.com", password="pw")])
        )
        self.assertIsNot(toolkit._session_for("primary"), toolkit._session_for("secondary"))

    def test_unauthenticated_session_is_distinct_from_any_account_session(self) -> None:
        toolkit = HttpToolkit(_make_config([Account(label="primary", email="a@x.com", password="pw")]))
        self.assertIsNot(toolkit._session_for("primary"), toolkit._session_for(None))

    def test_same_label_reuses_the_same_session_across_calls(self) -> None:
        # Isolation is per-label, not per-call -- token caching and any
        # legitimate same-account cookie state should still persist across
        # requests for the same account.
        toolkit = HttpToolkit(_make_config([Account(label="primary", email="a@x.com", password="pw")]))
        self.assertIs(toolkit._session_for("primary"), toolkit._session_for("primary"))


def _record(step, method, path, status_code, *, error=None, response_signature=None):
    return RequestRecord(
        step=step,
        method=method,
        path=path,
        account=None,
        status_code=status_code,
        error=error,
        response_signature=response_signature,
    )


class HttpRequestTests(unittest.TestCase):
    def test_path_without_leading_slash_is_normalized(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text="{}")]
        toolkit.http_request(step=1, method="get", path="no-leading-slash", account=None)
        self.assertEqual(fake.requests_made[0]["url"], "http://target.test/no-leading-slash")
        self.assertEqual(fake.requests_made[0]["method"], "GET")

    def test_long_response_body_is_truncated(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        long_body = "x" * (MAX_BODY_CHARS + 500)
        fake.request_responses = [_FakeResponse(text=long_body)]
        result = toolkit.http_request(step=1, method="GET", path="/x", account=None)
        self.assertTrue(result["body_truncated"])
        self.assertEqual(result["body_original_length"], len(long_body))
        self.assertLess(len(result["body"]), len(long_body))

    def test_short_response_body_is_not_truncated(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text="short")]
        result = toolkit.http_request(step=1, method="GET", path="/x", account=None)
        self.assertFalse(result["body_truncated"])
        self.assertEqual(result["body"], "short")

    def test_body_precedes_headers_in_key_order(self) -> None:
        # agent.loop._compact_transcript keeps only a prefix of an aged-out
        # observation's serialized content -- body must come first so that
        # prefix isn't entirely consumed by routine response headers,
        # silently dropping the field that actually carries
        # information-disclosure signal. See the comment above this dict's
        # construction in http_tool.py.
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(headers={"Server": "nginx", "Date": "now", "X-Extra": "boilerplate"})]
        result = toolkit.http_request(step=1, method="GET", path="/x", account=None)
        keys = list(result.keys())
        self.assertLess(keys.index("body"), keys.index("headers"))

    def test_history_records_every_request(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(), _FakeResponse()]
        toolkit.http_request(step=1, method="GET", path="/x", account=None)
        toolkit.http_request(step=2, method="GET", path="/y", account=None)
        self.assertEqual(len(toolkit.history), 2)

    def test_recent_bodies_includes_response_body_and_headers(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text='{"secret": "value12345"}', headers={"X-Custom": "leaked-header-value"})]
        toolkit.http_request(step=1, method="GET", path="/x", account=None)
        bodies = toolkit.recent_bodies()
        self.assertTrue(any("secret" in b for b in bodies))
        self.assertTrue(any("leaked-header-value" in b for b in bodies))

    def test_recent_bodies_retains_evidence_across_a_full_length_run(self) -> None:
        # Regression test: EVIDENCE_BODY_CACHE_SIZE used to be small enough
        # (100 entries = 50 requests) that a run longer than that -- this
        # project has actually run up to ~150 steps -- would silently evict
        # the body an early finding's evidence came from, making
        # agent.validation._evidence_is_grounded reject a genuine finding.
        # Reproduce a full-length run and confirm the first request's
        # distinctive body is still present at the end.
        toolkit, fake = _toolkit_with_fake_session()
        num_requests = 130
        fake.request_responses = [
            _FakeResponse(text='{"marker": "unique-evidence-marker-000"}' if i == 0 else json.dumps({"i": i}))
            for i in range(num_requests)
        ]
        for i in range(num_requests):
            toolkit.http_request(step=i + 1, method="GET", path=f"/x/{i}", account=None)
        bodies = toolkit.recent_bodies()
        self.assertTrue(any("unique-evidence-marker-000" in b for b in bodies))

    def test_bodies_for_endpoint_binds_to_the_claimed_endpoint(self) -> None:
        # Fix #43: evidence grounding must consider only responses from a
        # request matching the finding's claimed endpoint, not any body
        # captured this run.
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [
            _FakeResponse(text='{"from": "endpoint-a-body"}'),
            _FakeResponse(text='{"from": "endpoint-b-body"}'),
        ]
        toolkit.http_request(step=1, method="GET", path="/a/1", account=None)
        toolkit.http_request(step=2, method="GET", path="/b/2", account=None)

        a_bodies = toolkit.bodies_for_endpoint("GET /a/{id}")
        self.assertTrue(any("endpoint-a-body" in b for b in a_bodies))
        self.assertFalse(any("endpoint-b-body" in b for b in a_bodies))

    def test_bodies_for_endpoint_collapses_nanoid_and_ignores_query(self) -> None:
        # The concrete request carried an opaque nanoid id and a query string;
        # the finding's claimed endpoint uses a {postId} placeholder and no
        # query. They must still line up (nanoid handled by dedup's
        # _segment_is_id, query stripped).
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text='{"author": "leaked-post-author"}')]
        toolkit.http_request(
            step=1, method="GET", path="/community/posts/opnKUzCwDCLjnkmUUJR24B?page=1", account=None
        )
        bodies = toolkit.bodies_for_endpoint("GET /community/posts/{postId}")
        self.assertTrue(any("leaked-post-author" in b for b in bodies))

    def test_bodies_for_endpoint_none_or_unparseable_returns_all(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text='{"x": "some-body-text"}')]
        toolkit.http_request(step=1, method="GET", path="/a/1", account=None)
        # None -> permissive (every captured body), same as recent_bodies().
        self.assertEqual(toolkit.bodies_for_endpoint(None), toolkit.recent_bodies())
        # A string that is not "METHOD /path" -> permissive fallback, never a
        # silent empty result that would spuriously reject a finding.
        self.assertEqual(toolkit.bodies_for_endpoint("not an endpoint"), toolkit.recent_bodies())

    def test_fabricated_bola_evidence_from_a_different_request_is_rejected(self) -> None:
        # Exact reproduction of the 2026-10-03 live false positive that
        # motivated fix #43. The model proposed a cross-user BOLA on
        # GET /identity/api/v2/user/dashboard/{userId}, but:
        #   - the claimed /dashboard/9 path had only ever 404'd, and
        #   - the quoted "other user" body in fact came from a *different*
        #     request (the secondary account fetching its own /dashboard).
        # Grounding the evidence to the claimed endpoint's own responses must
        # reject it; the old any-captured-body check accepted it.
        from agent.validation import validate_finding

        toolkit, fake = _toolkit_with_fake_session(
            [
                Account(label="primary", email="a@example.com", token="tok-a"),
                Account(label="secondary", email="b@example.com", token="tok-b"),
            ]
        )
        secondary_body = '{"id":9,"name":"Agent secondary","email":"agent.secondary@example.com"}'
        fake.request_responses = [
            _FakeResponse(status_code=404, text='{"detail":"No static resource .../dashboard/9."}'),
            _FakeResponse(text=secondary_body),
        ]
        # Step 1: the claimed BOLA path, by primary -> 404 (no leak here).
        toolkit.http_request(step=1, method="GET", path="/identity/api/v2/user/dashboard/9", account="primary")
        # Step 2: a DIFFERENT request -- secondary fetching its own dashboard.
        toolkit.http_request(step=2, method="GET", path="/identity/api/v2/user/dashboard", account="secondary")

        result = validate_finding(
            title="Dashboard endpoint reveals other users' PII",
            endpoint="GET /identity/api/v2/user/dashboard/{userId}",
            evidence=f"GET /identity/api/v2/user/dashboard/9 returns: {secondary_body}",
            why_disclosure=(
                "The dashboard endpoint accepts a userId in the path and returns another "
                "user's email and name to a non-owner caller, a BOLA disclosure."
            ),
            toolkit=toolkit,
        )
        self.assertFalse(result.accepted)
        self.assertIn("claimed endpoint", result.reason)

    def test_genuine_finding_grounded_in_its_own_endpoint_is_accepted(self) -> None:
        # The companion to the test above: when the leaked body really does
        # come from the claimed endpoint's own response, fix #43 leaves the
        # finding accepted (it only narrows, never over-rejects).
        from agent.validation import validate_finding

        toolkit, fake = _toolkit_with_fake_session()
        leak = '{"author":{"email":"pogba006@example.com","vehicleid":"cd515c12-0fc1-48ae-8b61-9230b70a845b"}}'
        fake.request_responses = [_FakeResponse(text=leak)]
        toolkit.http_request(
            step=1, method="GET", path="/community/api/v2/community/posts/opnKUzCwDCLjnkmUUJR24B", account=None
        )
        result = validate_finding(
            title="Community post exposes author email and vehicle id",
            endpoint="GET /community/api/v2/community/posts/{postId}",
            evidence=f"Response body: {leak}",
            why_disclosure=(
                "The post-detail endpoint returns the author's email address and vehicle id "
                "to any authenticated caller, correlating accounts with their vehicles."
            ),
            toolkit=toolkit,
        )
        self.assertTrue(result.accepted)

    def test_body_cache_size_covers_project_max_documented_run(self) -> None:
        # The largest run this project has actually exercised is ~150 steps
        # (documented in HISTORY.md's ninth pass); each entry covers half a
        # request (body + headers), so the cache must hold well over 300
        # entries to have margin.
        self.assertGreaterEqual(EVIDENCE_BODY_CACHE_SIZE, 400)

    def test_degenerate_path_is_refused_without_dispatching(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        pathological = "/a/" + "repeat/1/" * 5
        result = toolkit.http_request(step=1, method="GET", path=pathological, account=None)
        self.assertEqual(result["error"], "degenerate_path")
        self.assertEqual(len(fake.requests_made), 0)  # never actually dispatched
        self.assertEqual(len(toolkit.history), 0)  # not recorded either

    def test_repeated_prefix_failure_is_refused_without_dispatching(self) -> None:
        # Real gap found on a live run: hammering the same non-existent
        # endpoint with a different query string each time looked "new" to
        # both the exact-match repeat detector and the degenerate-path guard
        # (neither of which noticed, since no single call repeats a pattern
        # within itself). MAX_PREFIX_FAILURE_STREAK requests to the same
        # (method, path-before-'?') that all fail should refuse the next one.
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(status_code=404) for _ in range(MAX_PREFIX_FAILURE_STREAK)]
        for i in range(MAX_PREFIX_FAILURE_STREAK):
            result = toolkit.http_request(
                step=i + 1, method="GET", path=f"/api/listThings?attempt={i}", account=None
            )
            self.assertEqual(result["status_code"], 404)
        result = toolkit.http_request(
            step=MAX_PREFIX_FAILURE_STREAK + 1,
            method="GET",
            path="/api/listThings?attempt=final&more=junk",
            account=None,
        )
        self.assertEqual(result["error"], "repeated_prefix_failure")
        self.assertEqual(len(fake.requests_made), MAX_PREFIX_FAILURE_STREAK)  # the refused call never dispatched
        self.assertEqual(len(toolkit.history), MAX_PREFIX_FAILURE_STREAK)  # nor was it recorded

    def test_repeated_prefix_failure_allows_up_to_the_threshold(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(status_code=404) for _ in range(MAX_PREFIX_FAILURE_STREAK)]
        for i in range(MAX_PREFIX_FAILURE_STREAK):
            result = toolkit.http_request(step=i + 1, method="GET", path=f"/api/listThings?a={i}", account=None)
            self.assertEqual(result["status_code"], 404)
        self.assertEqual(len(toolkit.history), MAX_PREFIX_FAILURE_STREAK)

    def test_repeated_prefix_failure_ignores_query_string_when_matching(self) -> None:
        self.assertIsNotNone(
            _repeated_prefix_failure_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 404),
                    _record(2, "GET", "/api/x?a=2&b=3", 404),
                    _record(3, "GET", "/api/x", 404),
                ],
                method="GET",
                path="/api/x?a=4",
            )
        )

    def test_repeated_prefix_failure_a_success_breaks_the_streak(self) -> None:
        self.assertIsNone(
            _repeated_prefix_failure_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 404),
                    _record(2, "GET", "/api/x?a=2", 200),
                    _record(3, "GET", "/api/x?a=3", 404),
                ],
                method="GET",
                path="/api/x?a=4",
            )
        )

    def test_repeated_prefix_failure_ignores_a_different_prefix(self) -> None:
        self.assertIsNone(
            _repeated_prefix_failure_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 404),
                    _record(2, "GET", "/api/x?a=2", 404),
                    _record(3, "GET", "/api/x?a=3", 404),
                ],
                method="GET",
                path="/api/y?a=4",
            )
        )

    def test_repeated_prefix_failure_treats_connection_error_as_a_failure(self) -> None:
        self.assertIsNotNone(
            _repeated_prefix_failure_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", None, error="timeout"),
                    _record(2, "GET", "/api/x?a=2", None, error="timeout"),
                    _record(3, "GET", "/api/x?a=3", 404),
                ],
                method="GET",
                path="/api/x?a=4",
            )
        )

    def test_repeated_identical_response_is_refused_without_dispatching(self) -> None:
        # Real gap found live: hammering a *live* endpoint with a different
        # query string each time, where the endpoint ignores the parameter
        # and always returns the same body, looked "new" to every existing
        # guard (all of which only track failures). MAX_IDENTICAL_RESPONSE_STREAK
        # successes in a row with byte-identical (status_code, body) should
        # refuse the next variation.
        toolkit, fake = _toolkit_with_fake_session()
        same_body = '{"id": 8, "name": "same every time"}'
        fake.request_responses = [_FakeResponse(text=same_body) for _ in range(MAX_IDENTICAL_RESPONSE_STREAK)]
        for i in range(MAX_IDENTICAL_RESPONSE_STREAK):
            result = toolkit.http_request(
                step=i + 1, method="GET", path=f"/identity/api/v2/user/dashboard?user_id={i}", account=None
            )
            self.assertEqual(result["status_code"], 200)
        result = toolkit.http_request(
            step=MAX_IDENTICAL_RESPONSE_STREAK + 1,
            method="GET",
            path="/identity/api/v2/user/dashboard?user_id=999",
            account=None,
        )
        self.assertEqual(result["error"], "repeated_identical_response")
        self.assertEqual(len(fake.requests_made), MAX_IDENTICAL_RESPONSE_STREAK)  # the refused call never dispatched
        self.assertEqual(len(toolkit.history), MAX_IDENTICAL_RESPONSE_STREAK)  # nor was it recorded

    def test_repeated_identical_response_allows_up_to_the_threshold(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        same_body = '{"id": 8}'
        fake.request_responses = [_FakeResponse(text=same_body) for _ in range(MAX_IDENTICAL_RESPONSE_STREAK)]
        for i in range(MAX_IDENTICAL_RESPONSE_STREAK):
            result = toolkit.http_request(step=i + 1, method="GET", path=f"/x?a={i}", account=None)
            self.assertEqual(result["status_code"], 200)
        self.assertEqual(len(toolkit.history), MAX_IDENTICAL_RESPONSE_STREAK)

    def test_repeated_identical_response_ignores_query_string_when_matching(self) -> None:
        self.assertIsNotNone(
            _repeated_identical_response_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 200, response_signature="sig-a"),
                    _record(2, "GET", "/api/x?a=2&b=3", 200, response_signature="sig-a"),
                    _record(3, "GET", "/api/x", 200, response_signature="sig-a"),
                ],
                method="GET",
                path="/api/x?a=4",
            )
        )

    def test_repeated_identical_response_a_different_body_breaks_the_streak(self) -> None:
        self.assertIsNone(
            _repeated_identical_response_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 200, response_signature="sig-a"),
                    _record(2, "GET", "/api/x?a=2", 200, response_signature="sig-b"),
                    _record(3, "GET", "/api/x?a=3", 200, response_signature="sig-a"),
                ],
                method="GET",
                path="/api/x?a=4",
            )
        )

    def test_repeated_identical_response_ignores_a_different_prefix(self) -> None:
        self.assertIsNone(
            _repeated_identical_response_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 200, response_signature="sig-a"),
                    _record(2, "GET", "/api/x?a=2", 200, response_signature="sig-a"),
                    _record(3, "GET", "/api/x?a=3", 200, response_signature="sig-a"),
                ],
                method="GET",
                path="/api/y?a=4",
            )
        )

    def test_repeated_identical_response_a_failure_breaks_the_streak(self) -> None:
        # A repeated-failure streak is a different, already-handled case
        # (_repeated_prefix_failure_reason) -- this guard must not also fire
        # on it just because the failures happen to share a signature.
        self.assertIsNone(
            _repeated_identical_response_reason(
                history=[
                    _record(1, "GET", "/api/x?a=1", 200, response_signature="sig-a"),
                    _record(2, "GET", "/api/x?a=2", 200, response_signature="sig-a"),
                    _record(3, "GET", "/api/x?a=3", 404, response_signature=None),
                ],
                method="GET",
                path="/api/x?a=4",
            )
        )

    def test_repeated_identical_response_live_flow_produces_matching_signatures(self) -> None:
        # End-to-end (not hand-built records): three real http_request calls
        # with the identical response body must actually produce the same
        # response_signature on each RequestRecord, and a differing body
        # must not.
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [
            _FakeResponse(text='{"id": 8}'),
            _FakeResponse(text='{"id": 8}'),
            _FakeResponse(text='{"id": 9}'),
        ]
        toolkit.http_request(step=1, method="GET", path="/x?a=1", account=None)
        toolkit.http_request(step=2, method="GET", path="/x?a=2", account=None)
        toolkit.http_request(step=3, method="GET", path="/x?a=3", account=None)
        sigs = [r.response_signature for r in toolkit.history]
        self.assertEqual(sigs[0], sigs[1])
        self.assertNotEqual(sigs[0], sigs[2])

    def test_list_visited_endpoints_reflects_history(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(status_code=404)]
        toolkit.http_request(step=1, method="GET", path="/missing", account=None)
        coverage = toolkit.list_visited_endpoints()
        self.assertEqual(coverage["count"], 1)
        self.assertEqual(coverage["requests"][0]["status_code"], 404)


class StructureAwareTruncationTests(unittest.TestCase):
    def test_bare_array_keeps_whole_items_and_drops_the_rest(self) -> None:
        items = [{"id": i, "note": "x" * 50} for i in range(200)]
        body = json.dumps(items)
        rendered, truncated, original_length = _truncate(body, limit=500)
        self.assertTrue(truncated)
        self.assertEqual(original_length, len(body))
        kept = json.loads(rendered.split(" ... [")[0])
        self.assertEqual(kept, items[: len(kept)])
        self.assertIn(f"{200 - len(kept)} more item(s) omitted", rendered)

    def test_wrapped_array_field_is_truncated_by_item_not_mid_record(self) -> None:
        body = json.dumps({"posts": [{"id": i, "content": "y" * 50} for i in range(200)], "count": 200})
        rendered, truncated, _ = _truncate(body, limit=500)
        self.assertTrue(truncated)
        kept = json.loads(rendered.split(" ... [")[0])
        self.assertEqual(kept["count"], 200)
        self.assertLess(len(kept["posts"]), 200)
        for record in kept["posts"]:
            self.assertIn("content", record)  # every kept item is whole, never cut mid-record

    def test_no_note_when_every_item_fits_after_compact_reserialization(self) -> None:
        body = json.dumps([{"id": 1}, {"id": 2}], indent=4)  # padded with whitespace
        rendered, truncated, original_length = _truncate(body, limit=len(body) - 1)
        self.assertFalse(truncated)
        self.assertEqual(json.loads(rendered), [{"id": 1}, {"id": 2}])

    def test_non_json_body_falls_back_to_plain_character_cut(self) -> None:
        body = "<html>not json" + "x" * 500 + "</html>"
        rendered, truncated, original_length = _truncate(body, limit=50)
        self.assertTrue(truncated)
        self.assertTrue(rendered.startswith(body[:50]))

    def test_single_item_array_is_not_worth_item_truncation(self) -> None:
        body = json.dumps({"vehicles": ["only-one"], "junk": "z" * 5000})
        rendered, truncated, _ = _truncate(body, limit=200)
        self.assertTrue(truncated)
        self.assertTrue(rendered.startswith(body[:200]))  # plain cut, not item-aware


class IdKeyAndValueShapeTests(unittest.TestCase):
    def test_recognizes_bare_and_snake_case_and_camel_case_id_keys(self) -> None:
        for key in ("id", "ID", "Id", "user_id", "USER_ID", "vehicleId", "mechanicId"):
            self.assertTrue(_looks_like_id_key(key), key)

    def test_recognizes_uuid_keys(self) -> None:
        # Regression test: a live run against real crAPI found
        # GET /identity/api/v2/vehicle/vehicles returns each vehicle's real
        # path identifier under a bare "uuid" field, which the original
        # id/_id/Id-only check missed entirely.
        for key in ("uuid", "UUID", "vehicle_uuid", "vehicleUuid"):
            self.assertTrue(_looks_like_id_key(key), key)

    def test_does_not_false_positive_on_ordinary_words_ending_in_id(self) -> None:
        # These end in lowercase "id" but are not ID fields at all -- the
        # bare-"id"/"_id" checks require an exact match or an underscore, and
        # the camelCase check requires a capital "I", so none should match.
        for key in ("valid", "paid", "grid", "avoid", "solid", "is_valid"):
            self.assertFalse(_looks_like_id_key(key), key)

    def test_int_and_id_shaped_strings_are_id_shaped(self) -> None:
        self.assertTrue(_is_id_shaped_value(42))
        self.assertTrue(_is_id_shaped_value("42"))
        self.assertTrue(_is_id_shaped_value("550e8400-e29b-41d4-a716-446655440000"))
        self.assertTrue(_is_id_shaped_value("a" * 16))

    def test_bool_and_arbitrary_strings_are_not_id_shaped(self) -> None:
        self.assertFalse(_is_id_shaped_value(True))
        self.assertFalse(_is_id_shaped_value(False))
        self.assertFalse(_is_id_shaped_value("not an id"))
        self.assertFalse(_is_id_shaped_value(None))

    def test_id_shape_classifies_numeric_uuid_and_hex(self) -> None:
        self.assertEqual(_id_shape("42"), "numeric")
        self.assertEqual(_id_shape("550e8400-e29b-41d4-a716-446655440000"), "uuid")
        self.assertEqual(_id_shape("a" * 16), "hex")


class ExtractIdCandidatesTests(unittest.TestCase):
    def test_finds_nested_id_fields_by_key_name(self) -> None:
        body = json.dumps({"id": 8, "owner": {"user_id": 42}, "reports": [{"mechanicId": 7}]})
        ids = _extract_id_candidates(body)
        values = {(entry["field"], entry["value"]) for entry in ids}
        self.assertIn(("id", 8), values)
        self.assertIn(("user_id", 42), values)
        self.assertIn(("mechanicId", 7), values)

    def test_ignores_non_id_fields_and_non_id_shaped_values(self) -> None:
        body = json.dumps({"vin": "1G1YY26U485100001", "valid": True, "note": "not an id"})
        self.assertEqual(_extract_id_candidates(body), [])

    def test_deduplicates_repeated_values(self) -> None:
        body = json.dumps([{"user_id": 5}, {"user_id": 5}, {"user_id": 9}])
        ids = _extract_id_candidates(body)
        self.assertEqual(len({entry["value"] for entry in ids}), 2)

    def test_respects_limit(self) -> None:
        body = json.dumps([{"user_id": i} for i in range(50)])
        ids = _extract_id_candidates(body, limit=3)
        self.assertEqual(len(ids), 3)

    def test_non_json_body_returns_empty_list(self) -> None:
        self.assertEqual(_extract_id_candidates("<html>not json</html>"), [])

    def test_extracts_from_truncated_but_valid_json_prefix(self) -> None:
        # _truncate's structure-aware path appends a trailing note after
        # otherwise-valid JSON (see _truncate) -- extraction must strip that
        # note and still parse the kept prefix.
        items = [{"id": i, "user_id": 100 + i} for i in range(200)]
        rendered, truncated, _ = _truncate(json.dumps(items), limit=500)
        self.assertTrue(truncated)
        ids = _extract_id_candidates(rendered)
        self.assertTrue(any(entry["field"] == "user_id" for entry in ids))


class ListIdCandidatesTests(unittest.TestCase):
    def test_returns_error_when_path_not_yet_requested(self) -> None:
        toolkit, _ = _toolkit_with_fake_session()
        result = toolkit.list_id_candidates(path="/vehicle/8")
        self.assertEqual(result["error"], "no_cached_response")

    def test_finds_ids_and_builds_candidate_paths(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [
            _FakeResponse(text=json.dumps({"id": 8, "mechanic_id": 42, "owner_id": 99}))
        ]
        toolkit.http_request(step=1, method="GET", path="/vehicle/8", account=None)

        result = toolkit.list_id_candidates(path="/vehicle/8")
        self.assertEqual(result["source_path"], "/vehicle/8")
        self.assertEqual(result["source_method"], "GET")
        found_values = {entry["value"] for entry in result["ids_found"]}
        # "8" is excluded: it just restates the path's own segment, not a
        # new pivot candidate.
        self.assertEqual(found_values, {42, 99})
        self.assertEqual(set(result["candidate_paths"]), {"/vehicle/42", "/vehicle/99"})

    def test_excludes_candidate_paths_already_requested(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [
            _FakeResponse(text=json.dumps({"id": 8, "mechanic_id": 42})),
            _FakeResponse(text="{}"),
        ]
        toolkit.http_request(step=1, method="GET", path="/vehicle/8", account=None)
        toolkit.http_request(step=2, method="GET", path="/vehicle/42", account=None)  # already tried

        result = toolkit.list_id_candidates(path="/vehicle/8")
        self.assertEqual(result["candidate_paths"], [])

    def test_no_pivot_segment_still_returns_ids_found_with_no_candidate_paths(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text=json.dumps({"posts": [{"author_id": 7}]}))]
        toolkit.http_request(step=1, method="GET", path="/community/api/v2/community/posts/recent", account=None)

        result = toolkit.list_id_candidates(path="/community/api/v2/community/posts/recent")
        self.assertEqual({entry["value"] for entry in result["ids_found"]}, {7})
        self.assertEqual(result["candidate_paths"], [])

    def test_only_substitutes_ids_matching_the_pivot_segments_shape(self) -> None:
        # Regression test: a live run against real crAPI found
        # GET /identity/api/v2/vehicle/{carId}/location only accepts a UUID
        # for carId (a numeric id 400s: "Failed to convert 'carId'"), but the
        # response for a UUID-pivoted path can still contain an unrelated
        # numeric id (e.g. a nested vehicleLocation.id) -- substituting that
        # numeric value into the UUID-typed slot would produce a candidate
        # guaranteed to fail. Only same-shaped substitutions should be offered.
        toolkit, fake = _toolkit_with_fake_session()
        vehicle_uuid = "4e425b9e-38d6-495a-9b71-7f29229bcb3a"
        other_uuid = "11111111-2222-3333-4444-555555555555"
        fake.request_responses = [
            _FakeResponse(text=json.dumps({"vehicleLocation": {"id": 3}, "other_uuid": other_uuid}))
        ]
        toolkit.http_request(step=1, method="GET", path=f"/identity/api/v2/vehicle/{vehicle_uuid}/location", account=None)

        result = toolkit.list_id_candidates(path=f"/identity/api/v2/vehicle/{vehicle_uuid}/location")
        self.assertEqual(result["candidate_paths"], [f"/identity/api/v2/vehicle/{other_uuid}/location"])

    def test_list_id_candidates_path_without_leading_slash_is_normalized(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        fake.request_responses = [_FakeResponse(text="{}")]
        toolkit.http_request(step=1, method="GET", path="/x", account=None)
        result = toolkit.list_id_candidates(path="x")
        self.assertEqual(result["source_path"], "/x")


class ExtractApiPathsTests(unittest.TestCase):
    """Pure-function tests for _extract_api_paths_from_js (no toolkit/network)."""

    def test_extracts_quoted_api_paths(self) -> None:
        text = """let a="api/v2/vehicle/vehicles";const b='api/mechanic/mechanic_report';"""
        self.assertEqual(
            _extract_api_paths_from_js(text),
            {"api/v2/vehicle/vehicles", "api/mechanic/mechanic_report"},
        )

    def test_keeps_param_placeholders(self) -> None:
        self.assertEqual(
            _extract_api_paths_from_js('x="api/v2/vehicle/<carId>/location"'),
            {"api/v2/vehicle/<carId>/location"},
        )
        self.assertEqual(
            _extract_api_paths_from_js('x="api/v2/community/posts/{postId}/comment"'),
            {"api/v2/community/posts/{postId}/comment"},
        )

    def test_drops_external_absolute_urls(self) -> None:
        # A third-party SDK URL that happens to contain "api/" must not be
        # surfaced as a path on this target (observed live: a chatbot CDN URL).
        text = '''a="https://react-chatbotify.com/docs/api/bot_options";b="api/v2/user/dashboard"'''
        self.assertEqual(_extract_api_paths_from_js(text), {"api/v2/user/dashboard"})

    def test_strips_trailing_slash_and_dedupes(self) -> None:
        text = 'a="api/shop/orders/";b="api/shop/orders"'
        self.assertEqual(_extract_api_paths_from_js(text), {"api/shop/orders"})

    def test_ignores_strings_without_api_marker(self) -> None:
        self.assertEqual(_extract_api_paths_from_js('a="/static/js/main.js";b="hello"'), set())


class DiscoverApiEndpointsTests(unittest.TestCase):
    def test_scans_root_and_bundles_and_returns_sorted_unique_paths(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        html = (
            '<html><head>'
            '<script src="/static/js/main.abc.js"></script>'
            '<script src="static/js/chunk.def.js"></script>'
            '</head></html>'
        )
        bundle1 = 'fetch("api/v2/vehicle/vehicles");get("api/v2/community/posts/recent")'
        bundle2 = 'post("api/mechanic/mechanic_report");get("api/v2/vehicle/vehicles")'  # dup across bundles
        fake.request_responses = [
            _FakeResponse(text=html),
            _FakeResponse(text=bundle1),
            _FakeResponse(text=bundle2),
        ]
        result = toolkit.discover_api_endpoints()
        self.assertEqual(
            result["discovered_paths"],
            [
                "api/mechanic/mechanic_report",
                "api/v2/community/posts/recent",
                "api/v2/vehicle/vehicles",
            ],
        )
        self.assertEqual(result["count"], 3)
        self.assertFalse(result["discovered_paths_truncated"])
        self.assertEqual(result["scripts_scanned"], ["/static/js/main.abc.js", "static/js/chunk.def.js"])

    def test_resolves_script_urls_relative_root_and_absolute(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        html = (
            '<script src="static/js/rel.js"></script>'
            '<script src="/abs-root.js"></script>'
            '<script src="https://cdn.example.com/ext.js"></script>'
        )
        fake.request_responses = [_FakeResponse(text=html)] + [_FakeResponse(text="") for _ in range(3)]
        toolkit.discover_api_endpoints()
        urls = [r["url"] for r in fake.requests_made]
        self.assertEqual(
            urls,
            [
                "http://target.test/",  # the page itself
                "http://target.test/static/js/rel.js",  # page-relative
                "http://target.test/abs-root.js",  # root-relative
                "https://cdn.example.com/ext.js",  # absolute, left as-is
            ],
        )

    def test_page_without_scripts_still_scans_inline_html(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        html = '<html><script>var base="api/v2/user/dashboard";</script></html>'
        fake.request_responses = [_FakeResponse(text=html)]
        result = toolkit.discover_api_endpoints()
        self.assertEqual(result["discovered_paths"], ["api/v2/user/dashboard"])
        self.assertEqual(result["scripts_scanned"], [])

    def test_oversized_script_is_skipped(self) -> None:
        toolkit, fake = _toolkit_with_fake_session()
        html = '<script src="/big.js"></script>'
        huge = "x" * (MAX_DISCOVERY_BYTES_PER_SCRIPT + 1)
        fake.request_responses = [_FakeResponse(text=html), _FakeResponse(text=huge)]
        result = toolkit.discover_api_endpoints()
        self.assertEqual(result["discovered_paths"], [])
        self.assertEqual(result["scripts_scanned"], [])  # skipped, not scanned

    def test_root_fetch_failure_returns_error_not_raise(self) -> None:
        toolkit, _ = _toolkit_with_fake_session()

        def _boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        # Replace the already-created None-session's request with a raiser.
        toolkit._session_for(None).request = _boom
        result = toolkit.discover_api_endpoints()
        self.assertIn("request_failed", result["error"])
        self.assertEqual(result["discovered_paths"], [])
        self.assertEqual(result["count"], 0)

    def test_makes_no_api_calls_for_the_model(self) -> None:
        # Discovery must only fetch the page + its scripts, never any of the
        # discovered API paths themselves (scope: no autonomous crawler).
        toolkit, fake = _toolkit_with_fake_session()
        html = '<script src="/app.js"></script>'
        fake.request_responses = [
            _FakeResponse(text=html),
            _FakeResponse(text='get("api/v2/vehicle/vehicles")'),
        ]
        toolkit.discover_api_endpoints()
        requested = [r["url"] for r in fake.requests_made]
        self.assertNotIn("http://target.test/identity/api/v2/vehicle/vehicles", requested)
        self.assertEqual(requested, ["http://target.test/", "http://target.test/app.js"])
        # And nothing was recorded into the model-visible request history.
        self.assertEqual(toolkit.history, [])


class CoverageSegmentsTests(unittest.TestCase):
    """Fix #42's path-normalization used to match discovered paths against
    already-visited ones regardless of service prefix or id instantiation.
    """

    def test_lowercases_and_strips_slashes(self) -> None:
        self.assertEqual(_coverage_segments("/Api/V2/Posts/"), ("api", "v2", "posts"))

    def test_collapses_placeholder_and_id_segments_to_star(self) -> None:
        self.assertEqual(
            _coverage_segments("api/v2/vehicle/<carId>/location"),
            ("api", "v2", "vehicle", "*", "location"),
        )
        self.assertEqual(
            _coverage_segments("api/v2/vehicle/{carId}/location"),
            ("api", "v2", "vehicle", "*", "location"),
        )
        self.assertEqual(
            _coverage_segments("api/v2/vehicle/:carId/location"),
            ("api", "v2", "vehicle", "*", "location"),
        )

    def test_numeric_and_uuid_segments_collapse_to_star(self) -> None:
        self.assertEqual(_coverage_segments("/community/posts/8"), ("community", "posts", "*"))
        self.assertEqual(
            _coverage_segments("/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location"),
            ("vehicle", "*", "location"),
        )

    def test_visited_match_ignores_service_prefix_and_id_flavor(self) -> None:
        # Discovered path (frontend-relative, placeholder) vs. the full path the
        # model actually requested (service prefix + concrete uuid).
        visited = [_coverage_segments("/identity/api/v2/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location")]
        self.assertTrue(_discovered_path_is_visited("api/v2/vehicle/<carId>/location", visited))

    def test_unvisited_path_not_matched(self) -> None:
        visited = [_coverage_segments("/identity/api/v2/user/dashboard")]
        self.assertFalse(_discovered_path_is_visited("api/v2/community/posts/recent", visited))

    def test_empty_discovered_path_never_matches(self) -> None:
        self.assertFalse(_discovered_path_is_visited("", [("a", "b")]))


class DiscoverCoverageAndCacheTests(unittest.TestCase):
    """Fix #42: discover_api_endpoints partitions untried vs. already-visited
    paths and reuses a cached scan on a repeat call for the same page.
    """

    def _toolkit_with_bundle(self):
        toolkit, fake = _toolkit_with_fake_session()
        html = '<script src="/app.js"></script>'
        bundle = (
            'get("api/v2/community/posts/recent");'
            'get("api/v2/vehicle/<carId>/location");'
            'get("api/v2/user/dashboard")'
        )
        fake.request_responses = [_FakeResponse(text=html), _FakeResponse(text=bundle)]
        return toolkit, fake

    def test_partitions_untried_and_already_visited(self) -> None:
        toolkit, _ = self._toolkit_with_bundle()
        # Pretend the model already fetched the dashboard and one concrete
        # vehicle location (full path with service prefix + real uuid).
        toolkit.history = [
            _record(1, "GET", "/identity/api/v2/user/dashboard", 200),
            _record(2, "GET", "/identity/api/v2/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location", 200),
        ]
        result = toolkit.discover_api_endpoints()
        self.assertEqual(result["untried_paths"], ["api/v2/community/posts/recent"])
        self.assertEqual(
            sorted(result["already_visited_paths"]),
            ["api/v2/user/dashboard", "api/v2/vehicle/<carId>/location"],
        )
        self.assertEqual(result["untried_count"], 1)

    def test_repeat_call_reuses_cache_without_refetching(self) -> None:
        toolkit, fake = self._toolkit_with_bundle()
        first = toolkit.discover_api_endpoints()
        self.assertTrue(first["rescanned"])
        calls_after_first = len(fake.requests_made)

        second = toolkit.discover_api_endpoints()
        self.assertFalse(second["rescanned"])
        # No new HTTP fetch happened on the repeat call.
        self.assertEqual(len(fake.requests_made), calls_after_first)
        # Same discovered surface either way.
        self.assertEqual(second["discovered_paths"], first["discovered_paths"])

    def test_cached_repeat_recomputes_visited_from_current_history(self) -> None:
        toolkit, _ = self._toolkit_with_bundle()
        first = toolkit.discover_api_endpoints()
        self.assertIn("api/v2/community/posts/recent", first["untried_paths"])
        # Model then visits posts/recent; a cached repeat must reflect that.
        toolkit.history.append(_record(5, "GET", "/community/api/v2/community/posts/recent", 200))
        second = toolkit.discover_api_endpoints()
        self.assertFalse(second["rescanned"])
        self.assertNotIn("api/v2/community/posts/recent", second["untried_paths"])
        self.assertIn("api/v2/community/posts/recent", second["already_visited_paths"])

    def test_scan_failure_is_not_cached(self) -> None:
        toolkit, _ = _toolkit_with_fake_session()

        def _boom(*args, **kwargs):
            raise requests.ConnectionError("down")

        toolkit._session_for(None).request = _boom
        result = toolkit.discover_api_endpoints()
        self.assertIn("request_failed", result["error"])
        # Nothing cached, so a later successful call still scans.
        self.assertEqual(toolkit._discovery_cache, {})


class PersonalEmailExtractionTests(unittest.TestCase):
    """Fix #41: the cross-user-disclosure signal. _personal_emails_in drops
    role/system mailboxes; foreign_personal_emails additionally drops the
    requesting account's own address. Bodies mirror the real crAPI shapes
    verified live (see http_tool._EMAIL_RE's module comment).
    """

    def test_extracts_lowercased_unique_personal_emails(self) -> None:
        self.assertEqual(
            _personal_emails_in('[{"email":"Adam007@example.com"},{"email":"adam007@EXAMPLE.com"}]'),
            {"adam007@example.com"},
        )

    def test_role_and_system_mailboxes_are_excluded(self) -> None:
        self.assertEqual(
            _personal_emails_in("support@acme.com no-reply@acme.com postmaster@x.io admin@y.net"),
            set(),
        )

    def test_personal_email_alongside_role_mailbox_is_kept(self) -> None:
        self.assertEqual(
            _personal_emails_in("support@acme.com and real.user@example.com"),
            {"real.user@example.com"},
        )

    def test_empty_or_non_email_text_returns_empty_set(self) -> None:
        self.assertEqual(_personal_emails_in(""), set())
        self.assertEqual(_personal_emails_in('{"id": 8, "status": "OK"}'), set())

    def test_foreign_personal_emails_excludes_requesting_accounts_own(self) -> None:
        toolkit, _ = _toolkit_with_fake_session(
            [Account(label="primary", email="a@example.com", token="t")]
        )
        # Dashboard shape: only the caller's own email -> nothing foreign.
        self.assertEqual(
            toolkit.foreign_personal_emails('{"email":"a@example.com"}', "primary"), []
        )
        # Feed shape: other users' emails -> all foreign, sorted.
        self.assertEqual(
            toolkit.foreign_personal_emails(
                '[{"email":"b@example.com"},{"email":"a@example.com"},{"email":"c@example.com"}]',
                "primary",
            ),
            ["b@example.com", "c@example.com"],
        )

    def test_foreign_personal_emails_flags_other_configured_account(self) -> None:
        # As caller "primary", seeing the SECOND configured account's email
        # is still cross-user disclosure -- only the caller's own is excluded.
        toolkit, _ = _toolkit_with_fake_session(
            [
                Account(label="primary", email="a@example.com", token="t"),
                Account(label="secondary", email="b@example.com", token="t"),
            ]
        )
        self.assertEqual(
            toolkit.foreign_personal_emails('{"email":"b@example.com"}', "primary"),
            ["b@example.com"],
        )

    def test_foreign_personal_emails_unauthenticated_caller_flags_any_user_email(self) -> None:
        toolkit, _ = _toolkit_with_fake_session(
            [Account(label="primary", email="a@example.com", token="t")]
        )
        # account=None: no caller email to exclude -- a logged-out response
        # exposing any user's email is disclosure.
        self.assertEqual(
            toolkit.foreign_personal_emails('{"email":"a@example.com"}', None),
            ["a@example.com"],
        )


if __name__ == "__main__":
    unittest.main()
