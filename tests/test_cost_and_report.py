"""Offline tests for agent.cost (token/cost accounting, bonus) and
agent.report (required summary + bonus Markdown report rendering).
"""

from __future__ import annotations

import unittest

from agent.cost import build_cost_summary
from agent.llm_client import TokenUsage
from agent.report import render_full_report, render_summary
from tests._helpers import make_finding as _finding


class CostSummaryTests(unittest.TestCase):
    def test_build_from_usage(self) -> None:
        usage = TokenUsage(calls=4, prompt_tokens=100, completion_tokens=40, total_tokens=140)
        summary = build_cost_summary(usage, wall_clock_seconds=12.3, steps=4)
        self.assertEqual(summary.calls, 4)
        self.assertEqual(summary.total_tokens, 140)
        self.assertEqual(summary.steps, 4)

    def test_render_includes_all_fields(self) -> None:
        usage = TokenUsage(calls=2, prompt_tokens=100, completion_tokens=20, total_tokens=120)
        text = build_cost_summary(usage, wall_clock_seconds=5.0, steps=2).render()
        self.assertIn("LLM calls:", text)
        self.assertIn("2", text)
        self.assertIn("Total tokens:", text)
        self.assertIn("120", text)
        self.assertIn("Wall clock:", text)

    def test_render_handles_zero_calls_without_dividing_by_zero(self) -> None:
        usage = TokenUsage()
        text = build_cost_summary(usage, wall_clock_seconds=0.0, steps=0).render()
        self.assertIn("Avg tokens/call:    0", text)


class RenderSummaryTests(unittest.TestCase):
    def test_empty_findings_says_so(self) -> None:
        text = render_summary([])
        self.assertIn("No information-disclosure findings", text)

    def test_lists_each_finding_with_confidence_and_endpoint(self) -> None:
        text = render_summary([_finding()], rejected_count=3)
        self.assertIn("HIGH", text)
        self.assertIn("GET /identity/api/v2/user/dashboard", text)
        self.assertIn("proposals rejected: 3", text)

    def test_notes_challenge_list_status(self) -> None:
        on_list = render_summary([_finding(on_challenge_list=True)])
        off_list = render_summary([_finding(on_challenge_list=False)])
        self.assertIn("on public challenge list", on_list)
        self.assertIn("not on public challenge list", off_list)


class RenderFullReportTests(unittest.TestCase):
    def test_includes_evidence_and_reproduction_steps(self) -> None:
        text = render_full_report(
            [_finding(reproduction=["Log in as secondary", "GET /identity/api/v2/user/dashboard"])]
        )
        self.assertIn("victim@example.com", text)
        self.assertIn("1. Log in as secondary", text)
        self.assertIn("2. GET /identity/api/v2/user/dashboard", text)

    def test_includes_cost_summary_when_given(self) -> None:
        text = render_full_report([], cost_summary_text="Token/cost accounting\n  LLM calls: 3\n")
        self.assertIn("LLM calls: 3", text)

    def test_omits_cost_block_when_not_given(self) -> None:
        text = render_full_report([])
        self.assertNotIn("```\nToken", text)

    def test_total_findings_count_reported(self) -> None:
        text = render_full_report([_finding(), _finding(title="second")])
        self.assertIn("Total findings: 2", text)


if __name__ == "__main__":
    unittest.main()
