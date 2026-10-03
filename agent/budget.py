"""Cost/loop safety: hard step & token caps, plus consecutive-repeat
detection.

Two independent safeguards live here, and it's worth being precise about how
they differ (the distinction is worth being able to see clearly):

- **Hard budget** (``BudgetTracker.step_exhausted`` /
  ``token_exhausted``): an absolute ceiling that guarantees termination no
  matter what the model does. This is the backstop of last resort.
- **Repeat detection** (``BudgetTracker.record_call``): a much cheaper-to-hit
  trigger for the specific failure mode of the model asking for the *exact
  same thing* over and over with no new information -- which the hard budget
  would eventually catch, but only after wasting most of the run's cost on
  it. Repeat detection lets the loop intervene (refuse the repeated call)
  far earlier than the token/step ceiling would.

What counts as "the same call" is deliberately narrow: identical tool name
*and* identical arguments (method, path, account, query, body). Changing
only the ``account`` argument -- the whole mechanism for testing "does
account A get to see account B's data" -- is a *different* call and is never
flagged as a repeat, even though the endpoint and method are unchanged. This
was an explicit design consideration:
a naive same-endpoint check would have made the agent's main cross-account
technique look like wasteful looping.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


def _signature(tool_name: str, arguments: dict) -> str:
    """A stable, order-independent signature for a tool call's arguments,
    excluding the ``step`` field (which is loop-assigned metadata, not part
    of what makes two calls "the same").

    ``path`` (when present, i.e. for ``http_request``) has its trailing
    slash normalized: observed during development, the model would toggle
    between e.g. ``/identity/api/v2/user/8`` and
    ``/identity/api/v2/user/8/`` -- semantically the same request -- which
    defeated exact-string repeat detection and let it burn several turns on
    a distinction without a difference before finally repeating one exact
    string enough times to trip the blocker. Normalizing this one, narrow
    case closes that gap without trying to solve general "are these two
    paths semantically equivalent" (out of scope for a cheap, deterministic
    signature check).
    """
    args = {k: v for k, v in arguments.items() if k != "step"}
    path = args.get("path")
    if isinstance(path, str) and len(path) > 1:
        args["path"] = path.rstrip("/")
    return f"{tool_name}:{json.dumps(args, sort_keys=True, default=str)}"


@dataclass
class BudgetStatus:
    """Snapshot of the hard step/token ceiling only -- repeat-tracking state
    (``consecutive_repeats``) lives on ``BudgetTracker`` itself and is read
    directly by the loop right after ``record_call``, since that's the one
    place it changes; it doesn't need its own snapshot type.
    """

    steps_used: int
    max_steps: int
    tokens_used: int
    max_tokens: int

    @property
    def step_exhausted(self) -> bool:
        return self.steps_used >= self.max_steps

    @property
    def token_exhausted(self) -> bool:
        return self.tokens_used >= self.max_tokens

    @property
    def exhausted(self) -> bool:
        return self.step_exhausted or self.token_exhausted

    def reason(self) -> str | None:
        if self.step_exhausted:
            return f"step budget exhausted ({self.steps_used}/{self.max_steps})"
        if self.token_exhausted:
            return f"token budget exhausted ({self.tokens_used}/{self.max_tokens})"
        return None


@dataclass
class BudgetTracker:
    """Mutable run-scoped tracker; one instance per ``AgentLoop`` run."""

    max_steps: int
    max_total_tokens: int
    max_consecutive_repeats: int

    steps_used: int = 0
    _last_signature: str | None = field(default=None, repr=False)
    consecutive_repeats: int = 0

    def record_step(self) -> None:
        self.steps_used += 1

    def record_call(self, tool_name: str, arguments: dict) -> bool:
        """Update repeat tracking for one dispatched tool call.

        Returns ``True`` iff this call was identical to the immediately
        preceding one (i.e. contributes to the consecutive-repeat counter).
        """
        sig = _signature(tool_name, arguments)
        is_repeat = sig == self._last_signature
        self.consecutive_repeats = self.consecutive_repeats + 1 if is_repeat else 0
        self._last_signature = sig
        return is_repeat

    def status(self, tokens_used: int) -> BudgetStatus:
        return BudgetStatus(
            steps_used=self.steps_used,
            max_steps=self.max_steps,
            tokens_used=tokens_used,
            max_tokens=self.max_total_tokens,
        )
