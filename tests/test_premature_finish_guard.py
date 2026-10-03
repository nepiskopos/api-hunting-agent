"""Offline test for the premature-finish guard in ControlToolkit.

Regression test for a failure mode found during development: once
tool_choice was forced to "required" (agent.llm_client) to fix a worse bug
(the model never calling any tool at all), the model sometimes reached for
finish_investigation on literally the first turn, before making a single
real request. This drives ControlToolkit directly (no live target or LLM
needed) to confirm the guard rejects that and only accepts completion once
a minimum amount of real exploration has happened.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from agent.tools.control_tool import MIN_REQUESTS_BEFORE_FINISH, ControlToolkit


def _control_with_n_requests(n: int) -> ControlToolkit:
    http = MagicMock()
    http.history = [object()] * n
    return ControlToolkit(http=http)


class PrematureFinishGuardTests(unittest.TestCase):
    def test_refuses_finish_with_no_requests_made(self) -> None:
        control = _control_with_n_requests(0)
        result = control.finish_investigation(summary="done already")
        self.assertFalse(result["acknowledged"])
        self.assertFalse(control.finished)
        self.assertIn("too early", result["reason"])

    def test_refuses_finish_just_under_threshold(self) -> None:
        control = _control_with_n_requests(MIN_REQUESTS_BEFORE_FINISH - 1)
        result = control.finish_investigation(summary="done")
        self.assertFalse(result["acknowledged"])
        self.assertFalse(control.finished)

    def test_accepts_finish_at_threshold(self) -> None:
        control = _control_with_n_requests(MIN_REQUESTS_BEFORE_FINISH)
        result = control.finish_investigation(summary="explored thoroughly")
        self.assertTrue(result["acknowledged"])
        self.assertTrue(control.finished)
        self.assertEqual(control.finish_reason, "explored thoroughly")

    def test_accepts_finish_well_past_threshold(self) -> None:
        control = _control_with_n_requests(MIN_REQUESTS_BEFORE_FINISH + 20)
        result = control.finish_investigation(summary="done")
        self.assertTrue(result["acknowledged"])
        self.assertTrue(control.finished)


if __name__ == "__main__":
    unittest.main()
