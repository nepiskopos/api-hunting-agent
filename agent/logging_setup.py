"""Observability: structured, human-readable step logging to ``run.log``
(the cost/safety & logging goals -- "Log each step: the model's intent, the tool call,
the result. We will read these logs.").

Format chosen (the spec left this unspecified):
plain, chronological text lines, one logical entry per event, each tagged
with the step number and an event kind (``INTENT`` / ``TOOL_CALL`` /
``OBSERVATION`` / ``SYSTEM``). This is deliberately not JSON-lines: a human
reviewer reading run.log top-to-bottom should be able to follow the run
without a parser, per the project's own framing ("we will read these
logs"), while remaining `grep`-able by step number or event kind.

The same logger fans out to both the ``run.log`` file (always, at INFO) and
the console (so a live run is visible to whoever launched it), via the
standard ``logging`` module -- other modules in this package get their own
named loggers (``agent.llm``, ``agent.tools.http``, ...) that propagate up
to this one, so warnings/errors raised deep in, say, the HTTP toolkit land
in the same file automatically.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

RUN_LOGGER_NAME = "agent.run"

_MAX_LOG_VALUE_CHARS = 1000


def configure_logging(run_log_path: Path, *, level: int = logging.INFO) -> None:
    """Set up the root ``agent`` logger to write to both ``run_log_path``
    and the console. Safe to call more than once in the same process (each
    call replaces the previous handlers, closing them first so the old
    log file's descriptor isn't leaked -- matters for tests that build many
    ``AgentLoop``s in one process; a real CLI invocation only ever calls
    this once).
    """
    root = logging.getLogger("agent")
    root.setLevel(level)
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%S")

    file_handler = logging.FileHandler(run_log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console_handler = logging.StreamHandler(stream=sys.stderr)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)


def _clip(value: object) -> str:
    text = str(value)
    if len(text) > _MAX_LOG_VALUE_CHARS:
        return text[:_MAX_LOG_VALUE_CHARS] + f"... [clipped, {len(text) - _MAX_LOG_VALUE_CHARS} more chars]"
    return text


class StepLogger:
    """Writes the three per-step event kinds the spec calls for, plus
    free-form lifecycle ``system`` messages (run start/end, budget hits).

    One instance is created by ``agent.loop.AgentLoop`` and used for the
    entire run.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(RUN_LOGGER_NAME)

    def system(self, message: str) -> None:
        self._logger.info("SYSTEM: %s", message)

    def intent(self, step: int, text: str | None) -> None:
        stripped = text.strip() if text else ""
        shown = stripped or "(model gave no reasoning text alongside its tool call)"
        self._logger.info("[step %d] INTENT: %s", step, _clip(shown))

    def tool_call(self, step: int, name: str, arguments: dict) -> None:
        self._logger.info("[step %d] TOOL_CALL: %s(%s)", step, name, _clip(arguments))

    def observation(self, step: int, name: str, result: object) -> None:
        self._logger.info("[step %d] OBSERVATION (%s): %s", step, name, _clip(result))

    def malformed_call(self, step: int, name: str, raw_arguments: str, error: str) -> None:
        self._logger.warning(
            "[step %d] MALFORMED_TOOL_CALL: %s(%s) -- %s", step, name, _clip(raw_arguments), error
        )
