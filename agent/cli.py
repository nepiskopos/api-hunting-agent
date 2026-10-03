"""Command-line interface: the single-command entry point (``python -m agent --target ... --creds ...``).

Deliberately thin: argument parsing and process-level concerns (exit codes,
top-level error messages) only. All real logic lives in ``agent.config``
(assembling a validated ``RunConfig``) and ``agent.loop`` (running it).

Every flag below is optional and has an environment-variable equivalent
(loaded from a ``.env`` file if present -- see ``.env.example``), so the
agent can be configured either interactively (CLI flags) or entirely through
the environment (Docker/container use). CLI flags always win when given;
see ``agent.config.build_config`` for the exact precedence rule.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from .config import (
    ConfigError,
    DEFAULT_MAX_CONSECUTIVE_REPEATS,
    DEFAULT_MAX_STEPS,
    DEFAULT_MAX_TOTAL_TOKENS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    build_config,
)
from .loop import AgentLoop


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent",
        description=(
            "Autonomous information-disclosure hunting agent for OWASP crAPI. "
            "Runs end-to-end with no human intervention after launch."
        ),
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Base URL of the crAPI gateway, e.g. http://localhost:8888. Falls back to the "
        "CRAPI_TARGET env var, then 'target' in the credentials file.",
    )
    parser.add_argument(
        "--creds",
        type=Path,
        default=None,
        help="Path to a credentials JSON file (see creds.example.json). Falls back to the "
        "CREDS_PATH env var, then './creds.json'.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"Hard cap on agent turns. Falls back to the MAX_STEPS env var, then {DEFAULT_MAX_STEPS}.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Hard cap on total LLM tokens for the run. Falls back to the MAX_TOTAL_TOKENS env "
        f"var, then {DEFAULT_MAX_TOTAL_TOKENS}.",
    )
    parser.add_argument(
        "--max-consecutive-repeats",
        type=int,
        default=None,
        help="How many identical tool calls in a row before the next one is refused as a repeat. "
        f"Falls back to the MAX_CONSECUTIVE_REPEATS env var, then {DEFAULT_MAX_CONSECUTIVE_REPEATS}.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=None,
        help="Per-HTTP-request timeout in seconds, for calls to the crAPI target. Falls back to "
        f"the REQUEST_TIMEOUT_SECONDS env var, then {DEFAULT_REQUEST_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory to write findings.json / run.log / summary.md / report.md into. Falls "
        "back to the OUT_DIR env var, then the current directory.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the LLM model id. Falls back to the LLM_MODEL env var, then "
        "auto-detection from /v1/models.",
    )
    parser.add_argument(
        "--no-verifier",
        action="store_true",
        help="Disable the bonus adversarial verifier pass (on by default; also settable via "
        "VERIFIER_ENABLED=false).",
    )
    parser.add_argument(
        "--no-dedup",
        action="store_true",
        help="Disable the bonus semantic de-duplication pass (on by default; also settable via "
        "DEDUP_ENABLED=false).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # find_dotenv(usecwd=True) is required, not cosmetic: plain load_dotenv() searches from the
    # calling *module's* file location upward (python-dotenv's default), not from the process's
    # actual working directory -- those coincide only when you happen to invoke `python -m agent`
    # from inside the installed package's own directory. usecwd=True makes ".env in the directory
    # you run the command from" (as documented in README) literally true regardless of where the
    # package itself lives. No-op if no .env is found either way.
    load_dotenv(find_dotenv(usecwd=True))
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        config = build_config(
            target=args.target,
            creds_path=args.creds,
            max_steps=args.max_steps,
            max_total_tokens=args.max_tokens,
            max_consecutive_repeats=args.max_consecutive_repeats,
            request_timeout_seconds=args.request_timeout,
            out_dir=args.out_dir,
            model_override=args.model,
            no_verifier=args.no_verifier,
            no_dedup=args.no_dedup,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = AgentLoop(config).run()

    print()
    print(f"Stop reason: {result.stop_reason}")
    print(f"Steps used:  {result.steps_used}")
    print(f"Findings accepted: {len(result.findings)}  (proposals rejected: {result.rejected_count})")
    if result.verifier_rejections:
        print(f"Findings rejected by verifier pass: {len(result.verifier_rejections)}")
    print(f"Wrote: {config.findings_path}, {config.run_log_path}, {config.summary_path}, {config.report_path}")
    print()
    print(result.cost_summary.render())

    return 0
