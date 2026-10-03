"""Offline tests for agent.verifier: the bonus adversarial verifier pass.
The LLMClient is a bare mock -- no network calls, no live LLM.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.verifier import _VERIFIER_SYSTEM_PROMPT, verify_finding
from tests._helpers import make_finding as _finding


def _verdict_message(refuted: bool, reason: str = "because"):
    tool_call = MagicMock()
    tool_call.function.arguments = json.dumps({"refuted": refuted, "reason": reason})
    return SimpleNamespace(tool_calls=[tool_call])


class VerifyFindingTests(unittest.TestCase):
    def test_survives_when_not_refuted(self) -> None:
        llm = MagicMock()
        llm.chat.return_value = _verdict_message(refuted=False, reason="looks legitimate")
        survives, reason = verify_finding(_finding(), llm)
        self.assertTrue(survives)
        self.assertEqual(reason, "looks legitimate")

    def test_rejected_when_refuted(self) -> None:
        llm = MagicMock()
        llm.chat.return_value = _verdict_message(refuted=True, reason="caller owns this data")
        survives, reason = verify_finding(_finding(), llm)
        self.assertFalse(survives)
        self.assertEqual(reason, "caller owns this data")

    def test_fails_open_on_no_tool_call_after_retry(self) -> None:
        llm = MagicMock()
        llm.chat.return_value = SimpleNamespace(tool_calls=None)
        survives, reason = verify_finding(_finding(), llm)
        self.assertTrue(survives)
        self.assertIn("no verdict", reason)
        self.assertEqual(llm.chat.call_count, 2)  # exactly one retry, not more

    def test_retries_once_then_succeeds(self) -> None:
        llm = MagicMock()
        llm.chat.side_effect = [
            SimpleNamespace(tool_calls=None),  # first attempt: model produced no tool call
            _verdict_message(refuted=True, reason="second attempt caught it"),
        ]
        survives, reason = verify_finding(_finding(), llm)
        self.assertFalse(survives)
        self.assertEqual(reason, "second attempt caught it")
        self.assertEqual(llm.chat.call_count, 2)

    def test_fails_open_on_llm_exception(self) -> None:
        llm = MagicMock()
        llm.chat.side_effect = RuntimeError("connection reset")
        survives, reason = verify_finding(_finding(), llm)
        self.assertTrue(survives)
        self.assertIn("verifier error", reason)

    def test_fails_open_on_malformed_verdict_json(self) -> None:
        llm = MagicMock()
        bad_tool_call = MagicMock()
        bad_tool_call.function.arguments = "{not valid json"
        llm.chat.return_value = SimpleNamespace(tool_calls=[bad_tool_call])
        survives, reason = verify_finding(_finding(), llm)
        self.assertTrue(survives)
        self.assertIn("verifier error", reason)

    def test_missing_reason_defaults_to_placeholder(self) -> None:
        llm = MagicMock()
        tool_call = MagicMock()
        tool_call.function.arguments = json.dumps({"refuted": False})
        llm.chat.return_value = SimpleNamespace(tool_calls=[tool_call])
        _, reason = verify_finding(_finding(), llm)
        self.assertEqual(reason, "(no reason given)")

    def test_verifier_called_with_forced_tool_choice(self) -> None:
        # verify_finding delegates tool_choice enforcement to llm.chat's own
        # "tools given => required" rule (agent.llm_client); this just
        # confirms the verdict tool schema is actually passed through.
        llm = MagicMock()
        llm.chat.return_value = _verdict_message(refuted=False)
        verify_finding(_finding(), llm)
        _, kwargs = llm.chat.call_args
        self.assertEqual(kwargs["tools"][0]["function"]["name"], "submit_verdict")


class VerifierPromptBurdenOfProofTests(unittest.TestCase):
    """Fix #20: the verifier must not demand a privacy policy before treating
    another account's personal data as sensitive by default."""

    def test_prompt_places_burden_of_proof_on_personal_data_being_public(self) -> None:
        self.assertIn("treat this as disclosure BY DEFAULT", _VERIFIER_SYSTEM_PROMPT)

    def test_prompt_names_the_two_valid_ways_to_rebut_personal_data(self) -> None:
        self.assertIn("the calling user is the data's own owner", _VERIFIER_SYSTEM_PROMPT)
        self.assertIn("deliberately public", _VERIFIER_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
