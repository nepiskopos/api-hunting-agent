"""Offline tests for the budget/repeat-detection tracker."""

from __future__ import annotations

import unittest

from agent.budget import BudgetTracker


class BudgetTrackerTests(unittest.TestCase):
    def test_step_exhaustion(self) -> None:
        tracker = BudgetTracker(max_steps=2, max_total_tokens=1_000_000, max_consecutive_repeats=3)
        tracker.record_step()
        self.assertFalse(tracker.status(tokens_used=0).step_exhausted)
        tracker.record_step()
        self.assertTrue(tracker.status(tokens_used=0).step_exhausted)

    def test_token_exhaustion(self) -> None:
        tracker = BudgetTracker(max_steps=1000, max_total_tokens=100, max_consecutive_repeats=3)
        self.assertTrue(tracker.status(tokens_used=150).token_exhausted)
        self.assertFalse(tracker.status(tokens_used=50).token_exhausted)

    def test_identical_calls_increment_repeat_counter(self) -> None:
        tracker = BudgetTracker(max_steps=100, max_total_tokens=1_000_000, max_consecutive_repeats=3)
        args = {"method": "GET", "path": "/identity/api/v2/user/dashboard", "account": "primary"}
        self.assertFalse(tracker.record_call("http_request", args))
        self.assertTrue(tracker.record_call("http_request", args))
        self.assertTrue(tracker.record_call("http_request", args))
        self.assertEqual(tracker.consecutive_repeats, 2)

    def test_different_account_is_not_a_repeat(self) -> None:
        # This is the specific case the repeat-detector must get right: switching accounts on an
        # otherwise-identical call is the core cross-account technique, not
        # wasted repetition, and must not be flagged.
        tracker = BudgetTracker(max_steps=100, max_total_tokens=1_000_000, max_consecutive_repeats=3)
        args_a = {"method": "GET", "path": "/identity/api/v2/vehicle/1", "account": "primary"}
        args_b = {"method": "GET", "path": "/identity/api/v2/vehicle/1", "account": "secondary"}
        self.assertFalse(tracker.record_call("http_request", args_a))
        self.assertFalse(tracker.record_call("http_request", args_b))
        self.assertEqual(tracker.consecutive_repeats, 0)

    def test_step_field_ignored_in_signature(self) -> None:
        # 'step' is loop-assigned metadata, not semantic to the call.
        tracker = BudgetTracker(max_steps=100, max_total_tokens=1_000_000, max_consecutive_repeats=3)
        self.assertFalse(tracker.record_call("http_request", {"step": 1, "method": "GET", "path": "/x"}))
        self.assertTrue(tracker.record_call("http_request", {"step": 2, "method": "GET", "path": "/x"}))

    def test_trailing_slash_variants_are_treated_as_the_same_call(self) -> None:
        # Observed during development: the model toggled between
        # '/identity/api/v2/user/8' and '/identity/api/v2/user/8/' to dodge
        # exact-match repeat detection. Both must count as the same call.
        tracker = BudgetTracker(max_steps=100, max_total_tokens=1_000_000, max_consecutive_repeats=3)
        self.assertFalse(tracker.record_call("http_request", {"method": "GET", "path": "/identity/api/v2/user/8"}))
        self.assertTrue(tracker.record_call("http_request", {"method": "GET", "path": "/identity/api/v2/user/8/"}))
        self.assertEqual(tracker.consecutive_repeats, 1)

    def test_interleaved_different_call_resets_repeat_counter(self) -> None:
        tracker = BudgetTracker(max_steps=100, max_total_tokens=1_000_000, max_consecutive_repeats=3)
        tracker.record_call("http_request", {"path": "/a"})
        tracker.record_call("http_request", {"path": "/a"})
        self.assertEqual(tracker.consecutive_repeats, 1)
        tracker.record_call("http_request", {"path": "/b"})
        self.assertEqual(tracker.consecutive_repeats, 0)


if __name__ == "__main__":
    unittest.main()
