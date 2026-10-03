"""Offline tests for agent.challenge_reference.classify_on_challenge_list.

Only the classification function is tested here (its actual, intended use).
See tests/test_prompts.py for the regression guard confirming this module's
content never leaks into the model's own context.
"""

from __future__ import annotations

import unittest

from agent.challenge_reference import classify_on_challenge_list


class ClassifyOnChallengeListTests(unittest.TestCase):
    def test_matches_vehicle_bola_language(self) -> None:
        matched, challenge_id = classify_on_challenge_list(
            "Another user's vehicle VIN and location exposed via /identity/api/v2/vehicle/{id}"
        )
        self.assertTrue(matched)
        self.assertEqual(challenge_id, "challenge-1-bola-vehicle")

    def test_matches_mechanic_report_language(self) -> None:
        matched, _ = classify_on_challenge_list("Mechanic report for another user accessible by changing the ID")
        self.assertTrue(matched)

    def test_no_match_for_unrelated_disclosure(self) -> None:
        matched, challenge_id = classify_on_challenge_list(
            "Verbose stack trace in 500 response reveals internal package names"
        )
        self.assertFalse(matched)
        self.assertIsNone(challenge_id)

    def test_case_insensitive(self) -> None:
        matched, _ = classify_on_challenge_list("ANOTHER USER'S VEHICLE DATA EXPOSED")
        self.assertTrue(matched)

    def test_best_scoring_challenge_wins(self) -> None:
        # Text matching multiple challenges' keywords still returns exactly
        # one (id, bool) pair -- not a list -- by design (see docstring):
        # whichever challenge has the most distinct keyword hits, not simply
        # whichever comes first in the tuple.
        matched, challenge_id = classify_on_challenge_list("mechanic report leaks another user's vehicle")
        self.assertTrue(matched)
        self.assertIsInstance(challenge_id, str)

    def test_scores_by_keyword_count_not_tuple_order(self) -> None:
        # Regression test for a real live misattribution: the actual
        # accepted finding's text (community posts leaking another user's
        # email/PII, which also incidentally mentions a leaked vehicle ID)
        # legitimately whole-word-matches challenge-1's "vehicle" keyword,
        # and challenge-1 is listed before challenge-4 in the tuple -- but
        # this text matches four of challenge-4's keywords against only two
        # of challenge-1's, so the correct classification is challenge-4,
        # not "whichever is listed first that matches at all".
        title = "Community post endpoint exposes author PII to other users via predictable ID"
        endpoint = "GET /community/api/v2/community/posts/{id}"
        why_disclosure = (
            "The community post detail endpoint uses a predictable ID and returns the author's "
            "email address and vehicle ID. A different authenticated user can access another "
            "user's post and see their PII (email) and vehicle identifier. This is excessive "
            "data exposure / BOLA-style information disclosure."
        )
        matched, challenge_id = classify_on_challenge_list(f"{title} {endpoint} {why_disclosure}")
        self.assertTrue(matched)
        self.assertEqual(challenge_id, "challenge-4-excessive-data-exposure-users")

    def test_short_keyword_does_not_match_inside_an_unrelated_word(self) -> None:
        # Regression test for a real bug, reproduced with the exact
        # title/endpoint/why_disclosure text from the actual live finding
        # that triggered it: the keyword "vin" (vehicle VIN) matched as a
        # bare substring inside "having" in the why_disclosure text's last
        # sentence, falsely tagging a genuinely novel .env-disclosure
        # finding as "challenge-1-bola-vehicle" on its very first live
        # occurrence. (control_tool.py classifies title+endpoint+
        # why_disclosure only -- not the reproduction steps, which is where
        # unrelated words like "unauthenticated" happen to live instead.)
        title = "Publicly accessible .env file exposing database credentials and internal hostnames"
        endpoint = "GET /.env"
        why_disclosure = (
            "The .env configuration file is served directly over HTTP without any authentication "
            "or authorization checks. It contains plaintext database usernames and passwords for "
            "both PostgreSQL (postgresdb) and MongoDB (mongodb) instances, along with internal "
            "service hostnames and ports. This exposes sensitive infrastructure details and "
            "credentials that could be used to gain unauthorized access to backend databases or "
            "pivot into other internal services. The file is accessible to anyone without logging "
            "in or having any valid session token."
        )
        matched, challenge_id = classify_on_challenge_list(f"{title} {endpoint} {why_disclosure}")
        self.assertFalse(matched)
        self.assertIsNone(challenge_id)

    def test_whole_word_vin_still_matches(self) -> None:
        matched, challenge_id = classify_on_challenge_list("Endpoint exposes another user's VIN unexpectedly")
        self.assertTrue(matched)
        self.assertEqual(challenge_id, "challenge-1-bola-vehicle")

    def test_unauthenticated_language_does_not_match_any_challenge(self) -> None:
        # Regression test: challenge 14's public description ("an endpoint
        # that does not perform authentication checks") is too generic to
        # keyword-match -- across real live runs, the exact same underlying
        # /.env finding flipped between matched/unmatched purely based on
        # incidental phrasing ("unauthenticated" here, "without any
        # authentication" -- non-adjacent, so no match -- in another run's
        # wording), even though the finding itself never changed. Since
        # nearly any genuine information-disclosure finding can honestly be
        # described as "unauthenticated", keeping such a generic keyword
        # would misattribute future novel findings to a known challenge.
        # Challenge 14 is deliberately not in the keyword table at all now
        # (see challenge_reference.py's module comment) -- this phrasing
        # must not match anything.
        matched, challenge_id = classify_on_challenge_list(
            "Database Credentials Exposed via /.env File GET /.env "
            "Any unauthenticated user can retrieve these credentials by simply accessing this URL."
        )
        self.assertFalse(matched)
        self.assertIsNone(challenge_id)

    def test_without_authentication_language_does_not_match_any_challenge(self) -> None:
        matched, challenge_id = classify_on_challenge_list(
            "Publicly accessible .env file exposing database credentials and internal hostnames "
            "GET /.env The .env file containing plaintext database credentials is publicly "
            "accessible without authentication."
        )
        self.assertFalse(matched)
        self.assertIsNone(challenge_id)

    def test_credential_language_alone_does_not_match_chatbot_challenge(self) -> None:
        # Regression test for a real live misattribution (fix #38):
        # reproduced with the exact title/endpoint/why_disclosure text from
        # the actual live finding that triggered it. The why_disclosure text
        # says "...or attempt credential-based attacks against application
        # services" -- the hyphen after "credential" is a non-word
        # character, so the word-boundary matcher (correctly) matches
        # "credential" as a standalone word there. But this finding is a
        # plain unauthenticated .env leak with no chatbot involved at all;
        # challenge-17 is specifically about extracting credentials *via
        # chatbot manipulation*. Before this fix, "credential" alone was
        # enough to score challenge-17 a hit (1) against every other
        # challenge's 0, misattributing a genuine novel disclosure to a
        # known, unrelated public challenge -- exactly the false-positive
        # class this module's docstring says is worse than a false negative.
        title = "Exposed .env file containing database credentials and internal infrastructure details"
        endpoint = "GET /.env"
        why_disclosure = (
            "The .env file is publicly accessible without authentication and contains plaintext "
            "database credentials (username/password for both PostgreSQL and MongoDB), internal "
            "service hostnames (postgresdb, mongodb), and port numbers. This exposes sensitive "
            "authentication details that could be used to directly access backend databases or "
            "attempt credential-based attacks against application services."
        )
        matched, challenge_id = classify_on_challenge_list(f"{title} {endpoint} {why_disclosure}")
        self.assertFalse(matched)
        self.assertIsNone(challenge_id)

    def test_chatbot_keyword_alone_still_matches_challenge_17(self) -> None:
        # The fix removes "credential" from challenge-17's keywords, but
        # "chatbot" -- the one genuinely distinctive signal for this
        # specific challenge -- must still match on its own.
        matched, challenge_id = classify_on_challenge_list(
            "Chatbot can be manipulated into revealing another user's account details"
        )
        self.assertTrue(matched)
        self.assertEqual(challenge_id, "challenge-17-chatbot-credential-extraction")

    def test_generic_email_language_alone_does_not_match_challenge_4(self) -> None:
        # Regression test for a real live misattribution (fix #40):
        # a forget-password *user-enumeration* finding -- a genuine, off-list
        # generalization win -- was tagged challenge-4 ("leaks sensitive info
        # of OTHER users") purely because its text contained the word "email".
        # Reproduced with the exact live title/why_disclosure. The generic
        # info-disclosure vocabulary ("email"/"pii"/"phone"/"excessive") was
        # removed from challenge-4's keywords, keeping only the cross-user
        # phrases that actually distinguish it. A false "on-list" tag
        # understates the most-heavily-weighted metric (off-list generalization).
        # Exact live text from the finding that triggered fix #40.
        title = "User email enumeration via forget-password endpoint"
        why_disclosure = (
            "The forget-password endpoint reveals whether an email address is registered in the "
            "system by returning different HTTP status codes (200 for existing users, 404 for "
            "non-existing users) and different message content. This allows attackers to "
            "enumerate valid user emails, which can be used for targeted phishing or credential "
            "stuffing attacks. The response also echoes back the exact email address provided, "
            "confirming registration status."
        )
        matched, challenge_id = classify_on_challenge_list(f"{title} {why_disclosure}")
        self.assertFalse(matched)
        self.assertIsNone(challenge_id)

    def test_cross_user_language_still_matches_challenge_4(self) -> None:
        # The fix must not break the genuine challenge-4 case: a finding whose
        # text describes excessive exposure of one account's data to a
        # different account still classifies as on-list. (Only the on/off-list
        # boolean is part of findings.json's contract; the specific
        # challenge-id is an internal diagnostic and can legitimately be
        # ambiguous for text that also mentions a vehicle, which
        # test_scores_by_keyword_count_not_tuple_order covers.)
        matched, _ = classify_on_challenge_list(
            "Endpoint discloses another user's PII; this is excessive data exposure to a different account"
        )
        self.assertTrue(matched)


if __name__ == "__main__":
    unittest.main()
