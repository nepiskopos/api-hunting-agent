"""Offline tests for agent.prompts: the assembled system prompt must contain
the scope/tool/recon text and run-specific details, and must NOT leak the
challenge-list reference (agent.challenge_reference is deliberately kept out
of the model's context -- this is a regression guard for that boundary).
"""

from __future__ import annotations

import unittest

from agent.challenge_reference import PUBLIC_INFO_DISCLOSURE_CHALLENGES
from agent.prompts import build_system_prompt


class SystemPromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prompt = build_system_prompt(
            target="http://localhost:8888", account_labels=["primary", "secondary"], max_steps=42
        )

    def test_includes_target_url(self) -> None:
        self.assertIn("http://localhost:8888", self.prompt)

    def test_includes_account_labels(self) -> None:
        self.assertIn("primary", self.prompt)
        self.assertIn("secondary", self.prompt)

    def test_includes_step_budget(self) -> None:
        self.assertIn("42 steps", self.prompt)

    def test_includes_scope_boundary(self) -> None:
        self.assertIn("INFORMATION DISCLOSURE ONLY", self.prompt)
        self.assertIn("OUT OF SCOPE", self.prompt)

    def test_out_of_scope_keywords_mentioned_as_forbidden_not_endorsed(self) -> None:
        # Sanity check the scope text and prompt are actually wired together.
        self.assertIn("SQL injection", self.prompt)

    def test_mentions_all_tools(self) -> None:
        for tool_name in (
            "http_request",
            "discover_api_endpoints",
            "list_visited_endpoints",
            "list_id_candidates",
            "propose_finding",
            "finish_investigation",
        ):
            self.assertIn(tool_name, self.prompt)

    def test_never_mentions_challenge_list_or_its_entries(self) -> None:
        # The whole point of agent.challenge_reference is that it never
        # reaches the model -- assert none of its curated titles leak in.
        lowered = self.prompt.lower()
        self.assertNotIn("challenge", lowered)
        for ref in PUBLIC_INFO_DISCLOSURE_CHALLENGES:
            self.assertNotIn(ref.title.lower(), lowered)

    def test_single_account_prompt_still_builds(self) -> None:
        prompt = build_system_prompt(target="http://x", account_labels=["solo"], max_steps=10)
        self.assertIn("solo", prompt)


if __name__ == "__main__":
    unittest.main()
