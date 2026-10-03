"""Offline tests for semantic de-duplication (bonus) and the
required schemas (credentials input and findings output).
"""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from agent.dedup import dedup_findings, normalize_endpoint_pattern
from agent.schemas import Account, Credentials
from tests._helpers import make_finding as _finding


class EndpointNormalizationTests(unittest.TestCase):
    def test_numeric_id_collapsed(self) -> None:
        self.assertEqual(
            normalize_endpoint_pattern("GET /workshop/api/mechanic/reports/42"),
            "GET /workshop/api/mechanic/reports/{id}",
        )

    def test_uuid_collapsed(self) -> None:
        self.assertEqual(
            normalize_endpoint_pattern("GET /identity/api/v2/vehicle/3fa85f64-5717-4562-b3fc-2c963f66afa6"),
            "GET /identity/api/v2/vehicle/{id}",
        )

    def test_non_id_segments_preserved(self) -> None:
        self.assertEqual(
            normalize_endpoint_pattern("GET /identity/api/v2/user/dashboard"),
            "GET /identity/api/v2/user/dashboard",
        )

    def test_opaque_nanoid_segment_collapsed(self) -> None:
        # crAPI community-post IDs are 22-char base62 nanoids -- not numeric,
        # not a UUID, not pure hex -- so the original ID rule never matched
        # them and three per-post findings never shared a bucket.
        self.assertEqual(
            normalize_endpoint_pattern("GET /community/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN"),
            "GET /community/api/v2/community/posts/{id}",
        )

    def test_long_resource_word_without_digit_preserved(self) -> None:
        # The opaque-ID rule requires an embedded digit precisely so a long
        # all-letter resource-name segment is never mistaken for an ID.
        self.assertEqual(
            normalize_endpoint_pattern("GET /identity/api/v2/notificationpreferences"),
            "GET /identity/api/v2/notificationpreferences",
        )


class DedupTests(unittest.TestCase):
    def test_merges_same_pattern_and_similar_reasoning(self) -> None:
        f1 = _finding(endpoint="GET /workshop/api/mechanic/reports/1", reproduction=["as user A, GET .../1"])
        f2 = _finding(endpoint="GET /workshop/api/mechanic/reports/2", reproduction=["as user A, GET .../2"])
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 1)
        self.assertIn("as user A, GET .../1", merged[0].reproduction)
        self.assertIn("as user A, GET .../2", merged[0].reproduction)

    def test_keeps_distinct_issues_on_same_endpoint_pattern_separate(self) -> None:
        # Distinct, narrative-matching evidence for each -- a genuine
        # two-different-bugs case has different leaked fields, not just
        # different commentary about the same leaked data.
        f1 = _finding(
            endpoint="GET /community/api/v2/community/posts/1",
            evidence='"author": {"email": "victim@example.com"}',
            why_disclosure="This post response includes another user's raw email address in the author field.",
        )
        f2 = _finding(
            endpoint="GET /community/api/v2/community/posts/2",
            evidence='"is_flagged_by_admin": true',
            why_disclosure="This post response includes an internal 'is_flagged_by_admin' debug field not meant for clients.",
        )
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 2)

    def test_merges_same_evidence_despite_differently_worded_reasoning(self) -> None:
        # Real gap found on a live run: the same disclosed .env credential
        # dump (evidence similarity ~0.95) proposed twice with unrelated
        # commentary (why_disclosure similarity ~0.10) -- why_disclosure
        # alone would keep these as two separate accepted findings.
        f1 = _finding(
            endpoint="GET /.env",
            evidence="DB_NAME=crapi\nDB_USER=crapi\nDB_PASSWORD=crapi\nDB_HOST=postgresdb\nDB_PORT=5432",
            why_disclosure=(
                "The .env file contains sensitive database credentials that could be used to "
                "access the backend databases directly, bypassing application-level security "
                "controls. This is a critical information disclosure vulnerability."
            ),
        )
        f2 = _finding(
            endpoint="GET /.env",
            evidence="DB_NAME=crapi\nDB_USER=crapi\nDB_PASSWORD=crapi\nDB_HOST=postgresdb\nDB_PORT=5432",
            why_disclosure=(
                "Publicly accessible configuration file exposes plaintext credentials for "
                "both the primary and secondary datastores, with no authentication required to "
                "retrieve it."
            ),
        )
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 1)

    def test_merges_length_asymmetric_quotes_of_same_endpoint(self) -> None:
        # Live gap (run-fix41b): the same posts/recent PII leak proposed
        # twice on the byte-identical endpoint, but one evidence excerpt is a
        # short prose-padded snippet and the other the raw JSON prefix, so
        # evidence *text* similarity was only ~0.54 (text containment ~0.74) --
        # both under 0.75, leaving two accepted findings for one disclosure.
        # The JSON field-name sets are identical, so they must now merge.
        f1 = _finding(
            endpoint="GET /community/api/v2/community/posts/recent",
            evidence=(
                '{"posts":[{"id":"abc","title":"t","content":"c","author":'
                '{"nickname":"Robot","email":"robot001@example.com","vehicleid":"u1"},'
                '"authorid":3}] (full response shows emails robot001@, pogba006@, adam007@)'
            ),
            why_disclosure="Recent posts endpoint returns author emails and vehicle IDs for all users.",
        )
        f2 = _finding(
            endpoint="GET /community/api/v2/community/posts/recent",
            evidence=(
                '{"posts":[{"id":"abc","title":"t","content":"c","author":'
                '{"nickname":"Robot","email":"robot001@example.com","vehicleid":"u1"},'
                '"authorid":3},{"id":"def","title":"t2","content":"c2","author":'
                '{"nickname":"Pogba","email":"pogba006@example.com","vehicleid":"u2"},"authorid":2}]}'
            ),
            why_disclosure="The recent posts list exposes PII of multiple users to any authenticated caller.",
        )
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 1)

    def test_merges_same_bola_shown_against_two_objects(self) -> None:
        # Live gap (run-fix41b): one BOLA on /vehicle/{id}/location demonstrated
        # against two different users' vehicles. Same normalized pattern, same
        # response *shape*, but every leaked value differs (UUID, coords, name,
        # email), so evidence text similarity was ~0.72 -- just under threshold.
        # Identical field-name sets make this one finding, not two.
        f1 = _finding(
            endpoint="GET /identity/api/v2/vehicle/4bae9968-ec7f-4de3-a3a0-ba1b2ab5e5e5/location",
            evidence='{"carId":"4bae9968","vehicleLocation":{"id":3,"latitude":"37.74","longitude":"-84.30"},"fullName":"Robot","email":"robot001@example.com"}',
            why_disclosure="Vehicle location endpoint returns another owner's name, email and GPS without authz (BOLA).",
        )
        f2 = _finding(
            endpoint="GET /identity/api/v2/vehicle/cd515c12-0fc1-48ae-8b61-9230b70a845b/location",
            evidence='{"carId":"cd515c12","vehicleLocation":{"id":2,"latitude":"31.28","longitude":"-92.47"},"fullName":"Pogba","email":"pogba006@example.com"}',
            why_disclosure="Any authenticated user can query any vehicle UUID to read its owner's PII and location.",
        )
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 1)

    def test_merges_same_post_detail_across_nanoid_ids(self) -> None:
        # Live gap (run-dedupfix): the same post-detail PII leak reported once
        # per sampled post, each on a distinct 22-char nanoid path. Fixing the
        # opaque-ID normalization buckets them together; identical author
        # field sets (Jaccard 1.0) then merge all three to one. Note f1's
        # evidence uses backslash-escaped quotes (a real model double-escaping
        # artifact) -- without escape-tolerant key extraction it would yield
        # zero keys and stay un-merged purely over escaping.
        f1 = _finding(
            endpoint="GET /community/api/v2/community/posts/VguigU9DTrSwxcWFL4m6XN",
            evidence='Response body includes: \\"author\\":{\\"nickname\\":\\"Robot\\",\\"email\\":\\"robot001@example.com\\",\\"vehicleid\\":\\"u1\\",\\"profile_pic_url\\":\\"\\",\\"created_at\\":\\"2026-10-02T20:07:07.849Z\\"}',
            why_disclosure="Post detail exposes the author's email and vehicle ID to any authenticated caller.",
        )
        f2 = _finding(
            endpoint="GET /community/api/v2/community/posts/opnKUzCwDCLjnkmUUJR24B",
            evidence='Response body includes: "author":{"nickname":"Pogba","email":"pogba006@example.com","vehicleid":"u2","profile_pic_url":"","created_at":"2026-10-02T20:07:07.848Z"}',
            why_disclosure="Fetching any post returns the author's private email address and internal vehicle identifier.",
        )
        f3 = _finding(
            endpoint="GET /community/api/v2/community/posts/Xxznvnq97i3MYDFFFf9VdD",
            evidence='Response body includes: "author":{"nickname":"Adam","email":"adam007@example.com","vehicleid":"u3","profile_pic_url":"","created_at":"2026-10-02T20:07:07.833Z"}',
            why_disclosure="The post endpoint leaks each author's email and vehicle id regardless of ownership.",
        )
        merged = dedup_findings([f1, f2, f3])
        self.assertEqual(len(merged), 1)

    def test_merges_env_var_dump_despite_prose_wrapping(self) -> None:
        # Live gap (run-dedupfix): the same .env credential dump proposed twice
        # on GET /.env -- once as a raw dump, once prose-wrapped and missing a
        # couple of lines -- so evidence text similarity was only ~0.34 (fix
        # #35's ~0.95 text match no longer held). The two dumps still share 9
        # of 11 env-var *names* (Jaccard 0.82), so capturing KEY= config-var
        # names as the data shape merges them. Env vars here are separated by a
        # literal backslash-n, collapsed to whitespace before extraction.
        raw = (
            "DB_NAME=crapi\\nDB_USER=crapi\\nDB_PASSWORD=crapi\\nDB_HOST=postgresdb\\n"
            "DB_PORT=5432\\nSERVER_PORT=8080\\nMONGO_DB_HOST=mongodb\\nMONGO_DB_PORT=27017\\n"
            "MONGO_DB_USER=crapi\\nMONGO_DB_PASSWORD=crapi\\nMONGO_DB_NAME=crapi"
        )
        f1 = _finding(
            endpoint="GET /.env",
            evidence=raw,
            why_disclosure="The .env file exposes internal database credentials and configuration.",
        )
        f2 = _finding(
            endpoint="GET /.env",
            evidence=(
                "Response body contains: DB_NAME=crapi\\nDB_USER=crapi\\nDB_PASSWORD=crapi\\n"
                "DB_HOST=postgresdb\\nDB_PORT=5432\\nMONGO_DB_HOST=mongodb\\nMONGO_DB_USER=crapi\\n"
                "MONGO_DB_PASSWORD=crapi\\nMONGO_DB_NAME=crapi - full connection strings without auth"
            ),
            why_disclosure="Publicly accessible config file leaks plaintext datastore credentials.",
        )
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 1)

    def test_field_overlap_requires_minimum_shared_keys(self) -> None:
        # The field-name signal must not merge two findings that happen to
        # share one or two generic keys ("id", "email") but describe different
        # disclosed data -- this is the guard that keeps the field signal from
        # reintroducing the over-merging the distinct-issues test protects.
        f1 = _finding(
            endpoint="GET /identity/api/v2/user/1",
            evidence='{"id":1,"email":"a@example.com","name":"Alice"}',
            why_disclosure="Returns another user's email and name.",
        )
        f2 = _finding(
            endpoint="GET /identity/api/v2/user/2",
            evidence='{"id":2,"is_admin":true,"internal_role":"root","ssn":"123-45-6789"}',
            why_disclosure="Exposes internal admin and SSN fields.",
        )
        merged = dedup_findings([f1, f2])
        self.assertEqual(len(merged), 2)

    def test_transitive_similarity_chain_merges_regardless_of_order(self) -> None:
        # A~B (ratio ~0.91) and B~C (~0.83) are similar enough to merge, but
        # A~C alone (~0.74) is just under threshold. Comparing only against a
        # cluster's first member made the outcome depend on discovery order
        # (e.g. [B, A, C] would merge all three via B, while [A, B, C] would
        # leave C separate, comparing only against A); single-linkage
        # (compare against every existing member) must merge all three
        # regardless of order. Evidence is deliberately distinct (and
        # pairwise dissimilar) across a/b/c so this test still isolates the
        # why_disclosure-driven transitive chain it's meant to exercise,
        # rather than trivially merging via evidence similarity too.
        a = _finding(
            endpoint="GET /workshop/api/mechanic/reports/1",
            evidence='"phone": "555-0100"',
            why_disclosure=(
                "Phone numbers of other customers leak through the mechanic report endpoint "
                "when you change the id parameter."
            ),
        )
        b = _finding(
            endpoint="GET /workshop/api/mechanic/reports/2",
            evidence='"email": "victim@example.com"',
            why_disclosure=(
                "Email addresses of other customers leak through the mechanic report endpoint "
                "when you change the id parameter."
            ),
        )
        c = _finding(
            endpoint="GET /workshop/api/mechanic/reports/3",
            evidence='"author_email": "someone@example.org"',
            why_disclosure=(
                "Email addresses of other customers leak through the community post endpoint "
                "when you guess the post identifier."
            ),
        )
        for ordering in ([a, b, c], [b, a, c], [c, a, b]):
            merged = dedup_findings(list(ordering))
            self.assertEqual(len(merged), 1, f"order {[f.endpoint for f in ordering]} did not fully merge")

    def test_merge_keeps_off_list_when_duplicates_disagree(self) -> None:
        # Live gap (run-dedupfix): the same .env disclosure proposed twice, one
        # proposal's text incidentally said "PII" (a load-bearing challenge-4
        # keyword) so the classifier tagged just that one on_challenge_list=
        # True. Merging must keep the off-list value -- a false positive
        # (understating a real generalization win) is the worse error.
        off = _finding(
            endpoint="GET /.env",
            evidence="DB_NAME=crapi\\nDB_USER=crapi\\nDB_PASSWORD=crapi\\nDB_HOST=db\\nDB_PORT=5432\\nMONGO_DB_USER=crapi",
            why_disclosure="The .env file exposes internal database credentials to any caller.",
            confidence="medium",
            on_challenge_list=False,
        )
        on = _finding(
            endpoint="GET /.env",
            evidence="Response: DB_NAME=crapi\\nDB_USER=crapi\\nDB_PASSWORD=crapi\\nDB_HOST=db\\nDB_PORT=5432\\nMONGO_DB_USER=crapi - leaks PII",
            why_disclosure="Publicly accessible config file leaks plaintext credentials and PII without auth.",
            confidence="high",  # higher confidence -> would otherwise be the representative
            on_challenge_list=True,
        )
        merged = dedup_findings([off, on])
        self.assertEqual(len(merged), 1)
        self.assertFalse(merged[0].on_challenge_list)

    def test_prefers_higher_confidence_representative(self) -> None:
        low = _finding(confidence="low", reproduction=["low-confidence repro"])
        high = _finding(confidence="high", reproduction=["high-confidence repro"])
        merged = dedup_findings([low, high])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].confidence, "high")


class CredentialsSchemaTests(unittest.TestCase):
    def test_requires_password_or_token(self) -> None:
        with self.assertRaises(ValidationError):
            Account(label="x", email="a@example.com")

    def test_accepts_token_only(self) -> None:
        Account(label="x", email="a@example.com", token="abc123")  # should not raise

    def test_rejects_duplicate_labels(self) -> None:
        with self.assertRaises(ValidationError):
            Credentials(
                accounts=[
                    {"label": "primary", "email": "a@example.com", "password": "pw"},
                    {"label": "primary", "email": "b@example.com", "password": "pw"},
                ]
            )


class FindingSchemaTests(unittest.TestCase):
    def test_category_is_always_information_disclosure(self) -> None:
        f = _finding()
        self.assertEqual(f.category, "information_disclosure")

    def test_rejects_bad_endpoint_shape(self) -> None:
        with self.assertRaises(ValidationError):
            _finding(endpoint="not-a-method-and-path")

    def test_rejects_bad_confidence_value(self) -> None:
        with self.assertRaises(ValidationError):
            _finding(confidence="extremely-high")


if __name__ == "__main__":
    unittest.main()
