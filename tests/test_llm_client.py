"""Offline tests for agent.llm_client: model resolution, tool_choice/penalty
wiring, the 404-retry path, and token-usage accounting. The OpenAI SDK
client is replaced with a small fake -- no network access, no real API key.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from openai import APIStatusError

from agent.llm_client import DEFAULT_MAX_TOKENS, LLMClient, TokenUsage


def _usage(prompt=10, completion=5, total=15):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total)


def _response(content=None, tool_calls=None, usage=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage or _usage())


class TokenUsageTests(unittest.TestCase):
    def test_accumulates_across_calls(self) -> None:
        usage = TokenUsage()
        usage.add(_usage(10, 5, 15))
        usage.add(_usage(20, 8, 28))
        self.assertEqual(usage.calls, 2)
        self.assertEqual(usage.prompt_tokens, 30)
        self.assertEqual(usage.completion_tokens, 13)
        self.assertEqual(usage.total_tokens, 43)

    def test_none_usage_still_counts_the_call(self) -> None:
        usage = TokenUsage()
        usage.add(None)
        self.assertEqual(usage.calls, 1)
        self.assertEqual(usage.total_tokens, 0)


class LLMClientTests(unittest.TestCase):
    def _make_client(self) -> LLMClient:
        with patch("agent.llm_client.OpenAI"):
            return LLMClient("https://example.test/v1", "dummy-key")

    def test_resolve_model_picks_first_live_model_when_unconfigured(self) -> None:
        client = self._make_client()
        client._client.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="model-a"), SimpleNamespace(id="model-b")]
        )
        self.assertEqual(client.resolve_model(), "model-a")

    def test_resolve_model_uses_configured_model_even_if_not_live(self) -> None:
        client = self._make_client()
        client._configured_model = "requested-model"
        client._client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="other-model")])
        with self.assertLogs("agent.llm", level="WARNING"):
            resolved = client.resolve_model()
        self.assertEqual(resolved, "requested-model")

    def test_resolve_model_raises_on_empty_list(self) -> None:
        client = self._make_client()
        client._client.models.list.return_value = SimpleNamespace(data=[])
        with self.assertRaises(RuntimeError):
            client.resolve_model()

    def test_chat_without_tools_omits_tool_choice_and_penalties(self) -> None:
        client = self._make_client()
        client.model = "m"
        client._client.chat.completions.create.return_value = _response(content="hi")
        client.chat([{"role": "user", "content": "hi"}])
        kwargs = client._client.chat.completions.create.call_args.kwargs
        self.assertNotIn("tool_choice", kwargs)
        self.assertNotIn("frequency_penalty", kwargs)
        self.assertEqual(kwargs["max_tokens"], DEFAULT_MAX_TOKENS)

    def test_chat_with_tools_forces_tool_choice_required(self) -> None:
        client = self._make_client()
        client.model = "m"
        client._client.chat.completions.create.return_value = _response(tool_calls=[MagicMock()])
        client.chat([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
        kwargs = client._client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["tool_choice"], "required")
        self.assertIn("frequency_penalty", kwargs)
        self.assertIn("presence_penalty", kwargs)

    def test_chat_with_force_tool_name_narrows_tool_choice(self) -> None:
        client = self._make_client()
        client.model = "m"
        client._client.chat.completions.create.return_value = _response(tool_calls=[MagicMock()])
        client.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function"}],
            force_tool_name="propose_finding",
        )
        kwargs = client._client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["tool_choice"], {"type": "function", "function": {"name": "propose_finding"}})
        # Still the same anti-degeneration mitigation as the "required" path.
        self.assertIn("frequency_penalty", kwargs)
        self.assertIn("presence_penalty", kwargs)

    def test_chat_records_usage(self) -> None:
        client = self._make_client()
        client.model = "m"
        client._client.chat.completions.create.return_value = _response(usage=_usage(100, 50, 150))
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.usage.total_tokens, 150)

    def test_chat_resolves_model_first_if_unset(self) -> None:
        client = self._make_client()
        client._client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="auto-model")])
        client._client.chat.completions.create.return_value = _response(content="hi")
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(client.model, "auto-model")

    def test_chat_retries_once_on_404_after_reresolving_model(self) -> None:
        client = self._make_client()
        client.model = "stale-model"
        client._client.models.list.return_value = SimpleNamespace(data=[SimpleNamespace(id="fresh-model")])
        not_found = APIStatusError("not found", response=MagicMock(status_code=404), body=None)
        client._client.chat.completions.create.side_effect = [not_found, _response(content="ok")]
        message = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(message.content, "ok")
        self.assertEqual(client.model, "fresh-model")

    def test_chat_reraises_non_404_api_errors(self) -> None:
        client = self._make_client()
        client.model = "m"
        server_error = APIStatusError("boom", response=MagicMock(status_code=500), body=None)
        client._client.chat.completions.create.side_effect = server_error
        with self.assertRaises(APIStatusError):
            client.chat([{"role": "user", "content": "hi"}])

    def test_chat_retries_once_on_empty_choices(self) -> None:
        # Fix #21: a 200 response with choices=None/[] (observed live against
        # an alternate OpenAI-compatible endpoint) must not surface as a bare
        # NoneType-subscript crash; one retry with the same model recovers it.
        client = self._make_client()
        client.model = "m"
        empty = SimpleNamespace(choices=None, usage=None)
        client._client.chat.completions.create.side_effect = [empty, _response(content="ok")]
        message = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(message.content, "ok")
        self.assertEqual(client._client.chat.completions.create.call_count, 2)

    def test_chat_raises_clear_error_on_two_consecutive_empty_choices(self) -> None:
        client = self._make_client()
        client.model = "m"
        empty = SimpleNamespace(choices=[], usage=None)
        client._client.chat.completions.create.side_effect = [empty, empty]
        with self.assertRaisesRegex(RuntimeError, "no choices on two consecutive attempts"):
            client.chat([{"role": "user", "content": "hi"}])


if __name__ == "__main__":
    unittest.main()
