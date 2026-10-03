"""Offline tests for the degenerate-path guard in agent.tools.http_tool.

Regression test for a real failure mode found during development: the model
extended a path with the same segment(s) repeatedly (e.g.
'.../repair/1/repair/1/repair/1/...'), which kept escaping exact-match
repeat detection (agent.budget) because the string grew each time.
"""

from __future__ import annotations

import unittest

from agent.tools.http_tool import _degenerate_path_reason


class DegeneratePathGuardTests(unittest.TestCase):
    def test_normal_path_is_fine(self) -> None:
        self.assertIsNone(_degenerate_path_reason("/identity/api/v2/user/dashboard"))

    def test_reasonable_nested_resource_path_is_fine(self) -> None:
        self.assertIsNone(_degenerate_path_reason("/identity/api/v2/vehicle/abcd-1234-uuid/location"))

    def test_repeated_two_segment_pattern_is_caught(self) -> None:
        path = "/identity/api/v2/user/9/vehicle/1" + "/repair/1" * 6
        reason = _degenerate_path_reason(path)
        self.assertIsNotNone(reason)
        self.assertIn("repeats", reason)

    def test_repeated_single_segment_pattern_is_caught(self) -> None:
        path = "/a/" + "x/" * 5
        self.assertIsNotNone(_degenerate_path_reason(path))

    def test_overly_long_path_is_caught_even_without_repetition(self) -> None:
        path = "/identity/api/v2/" + "/".join(f"segment{i}" for i in range(30))
        self.assertIsNotNone(_degenerate_path_reason(path))

    def test_three_repeats_is_the_threshold_not_two(self) -> None:
        # Two repeats of a pattern is still plausibly a real, if unusual, path.
        path = "/identity/api/v2/user/9/vehicle/1/repair/1/repair/1"
        self.assertIsNone(_degenerate_path_reason(path))


if __name__ == "__main__":
    unittest.main()
