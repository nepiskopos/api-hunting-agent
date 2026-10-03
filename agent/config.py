"""Run configuration: gathers CLI flags, environment variables (including a
``.env`` file, see ``agent.cli.main``) and the credentials file into a
single immutable ``RunConfig``.

Centralizing this here (rather than scattering ``os.environ`` reads through
the codebase) makes the agent's inputs auditable at a glance and is what
the design notes call "settling the architecture before
the loop" -- every other module receives a ``RunConfig`` instance rather than
reaching into the environment itself.

Precedence for every setting below is **CLI flag > environment variable >
hard-coded default**. Secrets (``LLM_BASE_URL``/``LLM_API_KEY``) are the one
exception: they are environment-only, on purpose (see ``build_config``'s
docstring).
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from .schemas import Credentials

logger = logging.getLogger("agent.config")

# ---------------------------------------------------------------------------
# Defaults for the cost/loop safety caps required by the cost/safety & logging goals.
# These are deliberately generous enough for a real exploration run against
# crAPI's ~10-15 endpoints while still being a hard, finite ceiling. These
# numbers are a pragmatic choice -- there is no "correct" value prescribed by the spec.
# ---------------------------------------------------------------------------
DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_TOTAL_TOKENS = 400_000
# Was 3 until a live run (2026-07-29, twenty-fourth pass) showed the actual
# cost of that slack: the model issued the exact same http_request call
# (identical method/path/account) twice in a row for zero new information,
# and the old default of 3 tolerates that -- it only refuses the *fourth*
# identical call, so two free, wasted repeats always go through undetected.
# There is no legitimate reason for this loop's model to reissue a
# byte-identical call to a local, deterministic target: unlike a flaky
# remote API, a repeat here can never observe something the first call
# didn't already show. Tightened to 1 -- the very first repeat is refused
# before dispatch, same as every other pre-dispatch guard in this codebase
# (degenerate path, repeated-prefix-failure, repeated-identical-response).
DEFAULT_MAX_CONSECUTIVE_REPEATS = 1
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_CREDS_PATH = "creds.json"
DEFAULT_OUT_DIR = "."


class ConfigError(RuntimeError):
    """Raised for any problem with CLI args, env vars, or the creds file."""


def _resolve(cli_value, env_name: str, default, caster):
    """CLI flag > environment variable > hard-coded default -- the one
    precedence rule this module's docstring promises, applied uniformly for
    every numeric setting rather than spelled out separately at each call
    site.
    """
    if cli_value is not None:
        return cli_value
    raw = os.environ.get(env_name)
    return caster(raw) if raw else default


def _resolve_path(cli_value: Path | None, env_name: str, default: str) -> Path:
    """CLI flag > environment variable > hard-coded default, for the two
    path-valued settings (``creds_path``, ``out_dir``) -- the same
    precedence rule as ``_resolve``, just returning a ``Path`` instead of a
    caster-converted scalar.
    """
    return cli_value if cli_value is not None else Path(os.environ.get(env_name, default))


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Every hard-cap numeric setting (max_steps, max_total_tokens,
# max_consecutive_repeats, request_timeout_seconds) must fail closed on a
# non-positive/non-finite value at startup, rather than either crashing
# later with a misleading stop_reason (request_timeout_seconds: fix #28,
# NaN/Infinity: fix #30) or silently producing a fully-formed, "successful"
# but useless empty run (max_steps/max_total_tokens/max_consecutive_repeats:
# fix #31 -- e.g. BudgetStatus.step_exhausted is True immediately when
# max_steps<=0, so a typo like --max-steps 0 wasn't caught at all). This
# helper applies that check uniformly instead of repeating the same
# resolve+validate+raise shape at each call site.
def _resolve_positive(cli_value, env_name: str, default, caster, *, flag: str, kind: str = "a positive integer"):
    """``_resolve(...)`` plus a fail-closed positivity check, raising
    ``ConfigError`` in the exact shape every other bad config value uses.

    ``math.isfinite`` is applied uniformly to every field's resolved value,
    not just the float one -- it's always ``True`` for a Python ``int``, so
    this changes nothing for the three int-typed hard caps (NaN/Infinity
    can't reach them anyway: ``int(raw)``/argparse's ``type=int`` both raise
    ``ValueError`` on non-integer text before this check ever runs), while
    still catching NaN/``inf`` for ``request_timeout_seconds``, which uses
    ``type=float`` and genuinely can receive them (fix #30).
    """
    value = _resolve(cli_value, env_name, default, caster)
    if not (math.isfinite(value) and value > 0):
        raise ConfigError(f"--{flag}/{env_name} must be {kind}, got {value}")
    return value


@dataclass(frozen=True)
class RunConfig:
    """Everything a single agent run needs. Constructed once, passed down."""

    target: str
    credentials: Credentials

    llm_base_url: str
    llm_api_key: str
    llm_model: str | None  # None => auto-detect via /v1/models at startup

    max_steps: int = DEFAULT_MAX_STEPS
    max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS
    max_consecutive_repeats: int = DEFAULT_MAX_CONSECUTIVE_REPEATS

    out_dir: Path = field(default_factory=lambda: Path("."))
    findings_path: Path = field(init=False)
    run_log_path: Path = field(init=False)
    summary_path: Path = field(init=False)
    report_path: Path = field(init=False)

    verifier_enabled: bool = True
    dedup_enabled: bool = True
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        # dataclass is frozen, so use object.__setattr__ for derived paths.
        object.__setattr__(self, "findings_path", self.out_dir / "findings.json")
        object.__setattr__(self, "run_log_path", self.out_dir / "run.log")
        object.__setattr__(self, "summary_path", self.out_dir / "summary.md")
        object.__setattr__(self, "report_path", self.out_dir / "report.md")


def load_credentials(creds_path: Path) -> Credentials:
    """Load and validate the credentials file (the input contract)."""
    if not creds_path.exists():
        raise ConfigError(f"credentials file not found: {creds_path}")
    try:
        raw = json.loads(creds_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"credentials file is not valid JSON: {creds_path}: {exc}") from exc
    try:
        credentials = Credentials.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError, kept broad for a clean CLI message
        raise ConfigError(f"credentials file failed validation: {creds_path}:\n{exc}") from exc

    if len(credentials.accounts) < 2:
        logger.warning(
            "only %d account(s) configured; cross-account 'excessive data exposure' checks "
            "will be skipped this run (see Credentials docstring in agent.schemas).",
            len(credentials.accounts),
        )
    return credentials


def build_config(
    *,
    target: str | None = None,
    creds_path: Path | None = None,
    max_steps: int | None = None,
    max_total_tokens: int | None = None,
    max_consecutive_repeats: int | None = None,
    request_timeout_seconds: float | None = None,
    out_dir: Path | None = None,
    model_override: str | None = None,
    no_verifier: bool = False,
    no_dedup: bool = False,
) -> RunConfig:
    """Assemble a ``RunConfig`` from CLI args + environment, failing fast.

    Every parameter is optional; ``None`` means "use the environment variable
    if set, else the hard-coded default" (see the module docstring for the
    precedence rule). This is what lets the same agent be configured either
    entirely by CLI flags (interactive use) or entirely by a ``.env`` file /
    container environment variables (Docker use) -- see ``.env.example``.

    Precedence for ``target`` specifically: ``--target`` CLI flag >
    ``CRAPI_TARGET`` env var > ``target`` key inside the credentials file.
    This mirrors the project's own example (``--target
    http://localhost:8888 --creds creds.json``) while still letting the
    credentials file or the environment be fully self-contained if preferred.

    The LLM base URL and API key are *only* ever read from environment
    variables, never from a CLI flag or the credentials file -- this is a
    deliberate, non-negotiable choice: it is the only way to guarantee the
    secret can't accidentally end up in shell history, a committed creds.json,
    or run.log (the cost/safety & logging goals: "do not commit the key").
    """
    llm_base_url = os.environ.get("LLM_BASE_URL")
    llm_api_key = os.environ.get("LLM_API_KEY")
    if not llm_base_url or not llm_api_key:
        raise ConfigError(
            "LLM_BASE_URL and LLM_API_KEY must be set in the environment or a .env file "
            "(never pass the API key as a CLI flag). See README for setup."
        )

    resolved_creds_path = _resolve_path(creds_path, "CREDS_PATH", DEFAULT_CREDS_PATH)
    credentials = load_credentials(resolved_creds_path)

    resolved_target = target or os.environ.get("CRAPI_TARGET") or credentials.target
    if not resolved_target:
        raise ConfigError(
            "no target given: pass --target, set CRAPI_TARGET, or set 'target' in the "
            "credentials file."
        )
    resolved_target = resolved_target.rstrip("/")

    resolved_out_dir = _resolve_path(out_dir, "OUT_DIR", DEFAULT_OUT_DIR)
    try:
        resolved_out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # e.g. --out-dir points at a path that already exists as a regular
        # file (FileExistsError) or a parent segment isn't a directory
        # (NotADirectoryError) -- without this, it's the one misconfiguration
        # that reaches the user as a raw traceback instead of the clean
        # "error: ..." message every other bad CLI arg/env var gets (see
        # agent.cli.main's ConfigError handling).
        raise ConfigError(f"cannot create --out-dir '{resolved_out_dir}': {exc}") from exc

    resolved_max_steps = _resolve_positive(max_steps, "MAX_STEPS", DEFAULT_MAX_STEPS, int, flag="max-steps")
    resolved_max_total_tokens = _resolve_positive(
        max_total_tokens, "MAX_TOTAL_TOKENS", DEFAULT_MAX_TOTAL_TOKENS, int, flag="max-tokens"
    )
    # A non-positive max_consecutive_repeats isn't destructive on its own
    # (agent.budget.BudgetTracker's ``consecutive_repeats >=
    # max_consecutive_repeats`` still only trips on an actual repeat, so 0
    # and negative values behave identically to 1 -- blocking on the very
    # first repeated call), but that collapsed, non-monotonic range is
    # itself a sign the value is a mistake; rejected for the same
    # fail-fast-at-startup consistency as the other two caps.
    resolved_max_consecutive_repeats = _resolve_positive(
        max_consecutive_repeats,
        "MAX_CONSECUTIVE_REPEATS",
        DEFAULT_MAX_CONSECUTIVE_REPEATS,
        int,
        flag="max-consecutive-repeats",
    )
    # requests.Session.request raises a bare ValueError (not
    # requests.RequestException) for a non-positive or NaN timeout, and a
    # bare OverflowError for an infinite one -- uncaught by http_tool.py's
    # `except requests.RequestException` on every call site, so any of these
    # would otherwise blow up the very first HTTP request of the run with a
    # misleading stop_reason ("llm_error: ...") from AgentLoop.run()'s
    # unrelated startup-failure catch-all (fix #23). argparse's
    # ``type=float`` happily parses CLI input like "nan"/"inf" for
    # --request-timeout, so both are genuinely reachable, not just
    # theoretical -- verified directly (fix #30).
    resolved_request_timeout_seconds = _resolve_positive(
        request_timeout_seconds,
        "REQUEST_TIMEOUT_SECONDS",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
        float,
        flag="request-timeout",
        kind="a finite positive number",
    )

    return RunConfig(
        target=resolved_target,
        credentials=credentials,
        llm_base_url=llm_base_url,
        llm_api_key=llm_api_key,
        llm_model=model_override or os.environ.get("LLM_MODEL"),
        max_steps=resolved_max_steps,
        max_total_tokens=resolved_max_total_tokens,
        max_consecutive_repeats=resolved_max_consecutive_repeats,
        request_timeout_seconds=resolved_request_timeout_seconds,
        out_dir=resolved_out_dir,
        verifier_enabled=False if no_verifier else _env_bool("VERIFIER_ENABLED", True),
        dedup_enabled=False if no_dedup else _env_bool("DEDUP_ENABLED", True),
    )
