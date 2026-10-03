"""Bonus: token/cost accounting printed at the end of a run (the optional/bonus features, bonus bullet 4).

The LLM endpoint this project uses is a flat-rate, free-tier resource, not a
metered commercial API with a disclosed per-token price -- so "cost" here is
reported as token counts and call counts (a defensible, honest proxy) rather
than a fabricated dollar figure. A dollar estimate would be inventing a number the spec gives no
basis for.
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm_client import TokenUsage


@dataclass
class CostSummary:
    calls: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    wall_clock_seconds: float
    steps: int

    def render(self) -> str:
        avg = self.total_tokens / self.calls if self.calls else 0
        return (
            "Token/cost accounting\n"
            f"  LLM calls:          {self.calls}\n"
            f"  Agent steps:        {self.steps}\n"
            f"  Prompt tokens:      {self.prompt_tokens}\n"
            f"  Completion tokens:  {self.completion_tokens}\n"
            f"  Total tokens:       {self.total_tokens}\n"
            f"  Avg tokens/call:    {avg:.0f}\n"
            f"  Wall clock:         {self.wall_clock_seconds:.1f}s\n"
        )


def build_cost_summary(usage: TokenUsage, *, wall_clock_seconds: float, steps: int) -> CostSummary:
    return CostSummary(
        calls=usage.calls,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        wall_clock_seconds=wall_clock_seconds,
        steps=steps,
    )
