"""Thin wrapper around an OpenAI-compatible chat-completions endpoint.

Responsibilities:

- reads ``base_url`` / ``api_key`` only from environment variables
  (``LLM_BASE_URL`` / ``LLM_API_KEY``), never from CLI args or files;
- confirms which model is actually live via ``GET /v1/models`` before the
  run starts, since the spec explicitly warns the served model can
  change out from under a hard-coded value;
- gives generous ``max_tokens`` headroom, since the endpoint is a reasoning
  model that may spend tokens on a hidden reasoning trace before its visible
  answer (the cost/safety & logging goals, "HEADS UP");
- accumulates token usage across the run for the bonus cost-accounting
  feature (``agent.cost``);
- retries once on a model-not-found error after re-resolving the live model
  (covers the documented "we swapped the model on the box" failure mode).

The model is never allowed to perform HTTP calls itself (the tool-use boundary) -- this client's only job is turning (system prompt + running
transcript + available tools) into the next assistant message, which the
agent loop then interprets as either a tool call or a final decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openai import APIStatusError, OpenAI

logger = logging.getLogger("agent.llm")

# Reasoning models can spend a large, variable fraction of max_tokens on a
# hidden reasoning trace before emitting the visible tool call / answer.
# This default gives real headroom for that; it is deliberately generous
# rather than tight, since a truncated tool-call argument is a silent
# correctness bug (malformed JSON), not just a wasted call.
DEFAULT_MAX_TOKENS = 2048


@dataclass
class TokenUsage:
    """Running total of token usage across every LLM call in a run."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage) -> None:  # usage: openai.types.CompletionUsage | None
        self.calls += 1
        if usage is None:
            return
        self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
        self.total_tokens += getattr(usage, "total_tokens", 0) or 0


class LLMClient:
    """OpenAI-compatible chat client with model auto-detection and usage tracking."""

    def __init__(self, base_url: str, api_key: str, model: str | None = None) -> None:
        self._client = OpenAI(base_url=base_url, api_key=api_key)
        self._configured_model = model
        self.model: str | None = None
        self.usage = TokenUsage()

    def resolve_model(self) -> str:
        """Query ``/v1/models`` and pick the model to use for this run.

        If a specific model was configured (via ``--model`` or ``LLM_MODEL``),
        we still call ``/v1/models`` first and warn loudly if the configured
        value isn't currently being served, rather than silently trying it
        and hitting a confusing 404 mid-run.
        """
        live_models = [m.id for m in self._client.models.list().data]
        if not live_models:
            raise RuntimeError("LLM endpoint returned an empty /v1/models list.")

        if self._configured_model:
            if self._configured_model not in live_models:
                logger.warning(
                    "configured model %r is not in the live /v1/models list %r; "
                    "using it anyway as requested, but this call will likely fail.",
                    self._configured_model,
                    live_models,
                )
            self.model = self._configured_model
        else:
            self.model = live_models[0]

        logger.info("LLM model resolved: %s (live models: %s)", self.model, live_models)
        return self.model

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        force_tool_name: str | None = None,
    ):
        """Make one chat-completions call, retrying once on a stale model id.

        Returns the raw ``choices[0].message`` object from the SDK (has
        ``.content`` and ``.tool_calls``). Token usage is recorded on
        ``self.usage`` as a side effect so the caller doesn't have to thread
        it through manually.

        ``force_tool_name``, if given, narrows ``tool_choice`` from the usual
        "required" (any tool) down to that one specific function -- see
        ``agent.loop``'s module comment above ``SENSITIVE_PATH_MARKERS``
        (fix #32) for why: a soft in-transcript nudge proved unpersuasive
        against a model that never once produced free-form reasoning text
        across three live runs, so getting it to actually call
        ``propose_finding`` after clear evidence needs a hard constraint,
        not another cue it can silently ignore.
        """
        if self.model is None:
            self.resolve_model()

        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            # "required" rather than "auto": diagnosed against the live
            # endpoint that with tool_choice="auto", this model frequently
            # finishes its hidden reasoning trace and then stops (finish_reason
            # "stop") without ever emitting a tool call -- sometimes with
            # empty content, sometimes with a stray text description of what
            # it intended to call, but no structured tool_calls the loop can
            # dispatch. That single misconfiguration was the actual root
            # cause behind several full runs producing zero findings despite
            # every other safeguard working correctly. "required" is also
            # simply the semantically correct setting here: every turn of
            # this loop is supposed to end in exactly one dispatched action
            # (agent.loop.AgentLoop), so there is never a turn where "don't
            # call any tool" is a valid, intended outcome in the first place.
            kwargs["tool_choice"] = (
                {"type": "function", "function": {"name": force_tool_name}} if force_tool_name else "required"
            )
            # Frequency/presence penalties as a second, complementary
            # mitigation: a separate failure mode observed in the same
            # diagnosis was the hidden reasoning trace itself degenerating
            # into repeating the same sentence ("I will use the X tool...")
            # until max_tokens was exhausted. Mild penalties discourage that
            # kind of token-level repetition without materially changing
            # the model's actual judgment.
            kwargs["frequency_penalty"] = 0.4
            kwargs["presence_penalty"] = 0.3

        try:
            response = self._client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if exc.status_code == 404:
                # "HEADS UP" in the spec: the box may have swapped models
                # mid-run. Re-resolve once and retry before giving up.
                logger.warning("model %r returned 404; re-checking /v1/models and retrying once.", self.model)
                self.model = None
                self.resolve_model()
                kwargs["model"] = self.model
                response = self._client.chat.completions.create(**kwargs)
            else:
                raise

        if not response.choices:
            # Fix #21 (found live against an alternate OpenAI-compatible
            # endpoint): a 200 response with a null/empty ``choices`` list --
            # a transient gateway hiccup distinct from the 404-model-swap case
            # above, and distinct from llm_client's own "no tool call" retry,
            # since it happens before any message is even parsed. Left
            # unhandled, ``response.choices[0]`` raises a bare "'NoneType'
            # object is not subscriptable" that reads like a code bug rather
            # than a transient upstream fault, and ends the whole run on what
            # a single retry cleanly recovered from when reproduced live.
            # One retry, no model re-resolution (the model is fine; the
            # response just came back empty) -- if it happens twice in a row,
            # that's a real outage and should surface as a clear error rather
            # than a silent third attempt.
            logger.warning("LLM response had no choices; retrying once.")
            response = self._client.chat.completions.create(**kwargs)
            if not response.choices:
                raise RuntimeError("LLM response had no choices on two consecutive attempts.")

        self.usage.add(getattr(response, "usage", None))
        return response.choices[0].message
