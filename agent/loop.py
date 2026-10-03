"""The reason -> act -> observe control loop, tying
every other module together into one run.

One call to the LLM is one "turn": the model receives the running
transcript plus the tool schemas, and returns either free-text content, one
or more tool calls, or both. Every tool call this turn is dispatched and
answered with a ``role: "tool"`` message before the next turn begins. This
is the "genuine reason -> act -> observe cycle driven by the LLM" the
spec requires (the "autonomous loop" goal) -- the *sequence* of what gets probed is
never scripted in this file; this file only enforces the safety envelope
around whatever the model decides (budget, repeat detection, scope,
validation) and performs the actual mechanical dispatch.

High-level structure of ``AgentLoop.run``:

1. Resolve the live LLM model and build the system prompt.
2. Loop turns until either the model calls ``finish_investigation`` (the
   completion condition) or the hard step/token budget is exhausted
   (the budget backstop) -- whichever comes first.
3. Within each turn, detect and defuse consecutive-repeat behavior
   before it can burn further budget on a stuck loop.
4. After the loop ends, optionally run the adversarial verifier pass and
   semantic de-duplication (bonuses) over the accepted findings.
5. Serialize findings.json, the human-readable summary,
   the optional Markdown report (bonus), and the token/cost summary
   (bonus).

Nothing here ever writes partial/garbage output: whichever branch the loop
exits through, step 4-5 always run over whatever findings were accepted so
far, so a budget cutoff mid-run still produces valid, complete deliverables.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field

from .budget import BudgetTracker
from .config import RunConfig
from .cost import CostSummary, build_cost_summary
from .dedup import dedup_findings
from .llm_client import LLMClient
from .logging_setup import StepLogger, configure_logging
from .prompts import build_system_prompt
from .report import render_full_report, render_summary
from .schemas import Finding
from .toolbox import ToolBox
from .verifier import verify_finding

logger = logging.getLogger("agent.loop")

# Machine-matchable mirror of prompts.py's RECON_TEXT "SUGGESTED EARLY
# ORDERING" step 1 -- the same well-known, target-agnostic sensitive-file
# paths the model is already told in prose to check early. Fix #32: three
# live runs in a row against qwen36-35b-a3b (2026-07-29) showed the model
# reliably *fetching* one of these (a 200 on /.env, real credentials in the
# body) and then simply continuing to explore rather than ever calling
# propose_finding -- across all three runs it produced zero free-form
# reasoning text on any of ~200 turns and called propose_finding zero times,
# favoring the cheapest tool (http_request) every single turn. A prompt
# nudge cannot fix a model that never reasons in text; see
# _maybe_force_propose_finding below, which instead forces tool_choice to
# propose_finding specifically (llm_client.LLMClient.chat's force_tool_name)
# for exactly one turn once a real 200 on one of these paths is observed.
SENSITIVE_PATH_MARKERS = (
    ".env",
    ".git/config",
    ".git/HEAD",
    "config.json",
    "actuator/env",
    "/debug",
)


def _is_sensitive_path(path: str) -> bool:
    return any(marker in path for marker in SENSITIVE_PATH_MARKERS)


# A third safety layer on top of the two below -- a forced-stop counter that
# tripped after MAX_TOTAL_REPEAT_BLOCKS=5 repeat-blocks in one run, treating
# that as unrecoverable drift -- was tried and removed in a later
# simplification pass. The per-call repeat-block refusal in
# _handle_tool_call (below) already prevents the repeated call from
# executing, and the hard step/token budget (agent.budget) already ends the
# run regardless; this counter never fired in any run this project kept a
# log for, and by the time 5 separate repeat-block episodes had happened the
# step budget was typically already mostly spent. See HISTORY.md's
# sixteenth-pass entry.

INITIAL_USER_MESSAGE = (
    "Begin your investigation now. Decide your first action; you'll see the result before "
    "deciding the next one."
)

NO_TOOL_CALL_NUDGE = (
    "You must act by calling exactly one of the available tools (http_request, "
    "discover_api_endpoints, list_visited_endpoints, list_id_candidates, propose_finding, "
    "finish_investigation) each turn "
    "-- plain text with no tool call does not advance the investigation."
)

# Fixed prefix of the coverage-reminder message (see COVERAGE_REMINDER_INTERVAL
# below) -- used to find and remove a stale reminder before adding a fresh one.
_COVERAGE_REMINDER_PREFIX = "Coverage reminder (auto-generated"


def _replace_singleton_message(messages: list[dict], marker: str, new_message: dict) -> None:
    """Delete the existing ``role: "user"`` message (if any) whose content
    starts with ``marker``, then append ``new_message``.

    Used for the coverage reminder and the no-tool-call nudge: each new
    occurrence fully supersedes the previous one (same content, or a strict
    superset of it), so keeping more than one in the transcript is pure
    resend cost with no offsetting benefit -- see COVERAGE_REMINDER_INTERVAL's
    module comment for the real token-cost impact this was found to have.
    A content-marker scan is used instead of a remembered index because two
    independent singletons (reminder, nudge) can each be replaced within the
    same turn; deleting one would silently invalidate a stored index into
    the other.
    """
    for i, m in enumerate(messages):
        if m.get("role") == "user" and (m.get("content") or "").startswith(marker):
            del messages[i]
            break
    messages.append(new_message)

# --- transcript compaction ---------------------------------------------------
# Every LLM call resends the *entire* running transcript (that's how the
# stateless chat-completions API works), and every http_request observation
# can be up to MAX_BODY_CHARS characters. Left unchecked, this makes total
# token usage grow roughly quadratically with step count -- confirmed in
# practice during development: a 50-step-budgeted run exhausted a 400k-token
# budget by step 27 with zero findings, purely from re-sending an
# ever-growing history, never from the model actually being unproductive.
#
# The fix is recency-weighted compaction: only the most recent
# KEEP_FULL_TOOL_MESSAGES tool observations (and, by the same window, the
# most recent assistant messages -- see _compact_transcript) are kept at
# full detail; older ones are clipped to a short excerpt. This is
# deliberately crude (no summarization LLM call -- that would just trade
# token cost for latency/more cost) but effective: it bounds per-turn growth
# to roughly constant rather than linear, while the `list_visited_endpoints`
# tool remains available for the model to recover a full coverage map of
# everything it has tried, compacted or not (see
# agent.tools.http_tool.HttpToolkit.list_visited_endpoints).
KEEP_FULL_TOOL_MESSAGES = 8
OLD_TOOL_MESSAGE_CLIP_CHARS = 300
_COMPACTED_MARKER = "[compacted;"
_COMPACTED_SUFFIX = f"...{_COMPACTED_MARKER} call list_visited_endpoints for the full coverage map]"
_SIG_SUFFIX_TEMPLATE = "...[compacted; sig={sig}; call list_visited_endpoints for the full coverage map]"
_DUPLICATE_SUFFIX_TEMPLATE = (
    "[compacted; duplicate of an earlier response with the same status_code+body "
    "(sig={sig}) -- call list_visited_endpoints for the full history]"
)
_SIG_PATTERN = re.compile(r"sig=([0-9a-f]{12})")


def _parse_json_object(content: str) -> dict | None:
    """Parse ``content`` as JSON and return it iff it's an object, else
    ``None`` -- for anything that isn't valid JSON, or is valid JSON that
    isn't a dict. Shared by ``_tool_response_signature`` and
    ``_strip_headers_for_clip`` below, which each only care about specific
    keys once parsed.
    """
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# Found analyzing a real 150-step/1.5M-token run: the model spent roughly 15
# consecutive steps re-fetching /identity/api/v2/user/dashboard with a
# different query-string parameter each time (?id=8, ?id=9, ?id=10, ...),
# and every single response was byte-identical (the endpoint ignores the
# parameter and just returns the caller's own profile from the session). Each
# of those aged out into its own independent OLD_TOOL_MESSAGE_CLIP_CHARS-sized
# excerpt -- ~15x redundant tokens for zero new information every subsequent
# turn. `_tool_response_signature` + the duplicate check in `_compact_role`
# collapse any aged-out http_request-shaped observation down to a one-line
# pointer once an identical (status_code, body) pair has already been kept in
# full once elsewhere in the transcript, regardless of what path/account/
# method produced it -- the point isn't that the *request* repeated (budget.py
# already handles that), it's that the *response* carried no new signal.
# The identifying hash is embedded in the kept excerpt's own suffix so later
# turns can recognize a repeat even after the first occurrence has itself
# scrolled out of the full-detail window and been clipped.
def _tool_response_signature(content: str) -> str | None:
    """A short stable hash of ``(status_code, body)`` for tool messages shaped
    like an ``http_request`` result, or ``None`` for anything else (other
    tools' results, or content that isn't valid JSON) -- those are left to the
    plain length-based clip below.
    """
    parsed = _parse_json_object(content)
    if parsed is None or "body" not in parsed or "status_code" not in parsed:
        return None
    raw = f"{parsed.get('status_code')}|{parsed.get('body')}"
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]


# Fix #14 (body-before-headers key order) stopped headers from consuming the
# *entire* OLD_TOOL_MESSAGE_CLIP_CHARS budget ahead of the body. It did not
# stop them consuming *part* of it: a real captured response was 875 chars,
# 513 of them headers -- even after #14, an aged-out clip of that response
# still spent well over half its fixed character budget on routine headers
# (Server, Date, CORS/cache/security headers) that carry no disclosure signal
# once a message has scrolled past the full-detail window; only the body
# does. Rather than rely on truncation order alone, drop `headers` from the
# JSON entirely before clipping an aged-out tool message, so the full
# OLD_TOOL_MESSAGE_CLIP_CHARS budget goes to what's actually left (status
# code, body, and the small book-keeping fields).
def _strip_headers_for_clip(content: str) -> str:
    """Drop the ``headers`` field from an ``http_request``-shaped tool
    result before it gets clipped, or return ``content`` unchanged if it
    isn't JSON or has no ``headers`` key (e.g. other tools' results).
    """
    parsed = _parse_json_object(content)
    if parsed is None or "headers" not in parsed:
        return content
    parsed = dict(parsed)
    del parsed["headers"]
    return json.dumps(parsed, default=str)


# Compaction has a side effect worth naming explicitly: once an observation
# scrolls out of the "recent" window, the model can lose track of having
# already tried that exact call and repeat it later (non-consecutively, so
# agent.budget's repeat detector -- which only looks at the *immediately
# preceding* call -- doesn't catch it either). The `list_visited_endpoints`
# tool exists to let the model recover this, but in practice (observed
# across multiple development runs) it never proactively called it even
# once in a 50+ step run. Rather than hope the model develops that habit,
# the loop auto-injects the same coverage summary every COVERAGE_REMINDER_INTERVAL
# steps, at near-zero cost (it's a compact method/path/status list, no
# bodies) compared to what re-trying dead ends already costs.
COVERAGE_REMINDER_INTERVAL = 8

# Found analyzing real live-run token accounting: this reminder (and the
# no-tool-call nudge below) are ``role: "user"`` messages, so
# ``_compact_transcript`` above -- which only clips ``role: "tool"``
# messages -- never touches them. Each reminder is a full re-dump of the
# *entire* request history, so appending a new one every 8 steps rather
# than replacing the last one meant every past reminder (each a growing
# superset of the one before it) was being resent on every subsequent call
# forever. On a real 72-step run this accounted for roughly a quarter of
# total token usage. Since each new reminder/nudge fully supersedes the
# previous one (same content, or a strict superset), the loop now keeps at
# most one of each in the transcript -- the old one is deleted right before
# the new one is appended -- with no loss of information the model could
# actually use.


@dataclass
class RunResult:
    findings: list[Finding]
    rejected_count: int
    stop_reason: str
    steps_used: int
    cost_summary: CostSummary
    verifier_rejections: list[tuple[str, str]] = field(default_factory=list)


class AgentLoop:
    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.llm = LLMClient(config.llm_base_url, config.llm_api_key, config.llm_model)
        self.toolbox = ToolBox.build(config)
        self.budget = BudgetTracker(
            max_steps=config.max_steps,
            max_total_tokens=config.max_total_tokens,
            max_consecutive_repeats=config.max_consecutive_repeats,
        )
        self.step_logger = StepLogger()
        # Fix #32 state: paths that have already triggered a forced
        # propose_finding turn (never re-trigger the same path), and the
        # tool name (if any) the *next* chat() call must be forced to use.
        self._forced_propose_paths: set[str] = set()
        self._force_next_tool: str | None = None

    def _maybe_force_propose_finding(self, name: str, arguments: dict, result: dict) -> None:
        """After a successful ``http_request`` observation, arm a one-turn
        forced ``propose_finding`` call if this looks like the real thing,
        and we haven't already forced one for this exact path. Two triggers:

        - a 200 on one of ``SENSITIVE_PATH_MARKERS`` (fix #32), and
        - a 200 whose body contains another party's personal email -- an
          email that is not the requesting account's own (fix #41, the
          cross-user-disclosure analogue; see ``HttpToolkit.
          foreign_personal_emails`` and the comment above
          ``http_tool._EMAIL_RE``).

        See the module comment above ``SENSITIVE_PATH_MARKERS`` for why a
        hard constraint is used here instead of another soft in-transcript
        nudge. This only forces a *proposal*; validation still gates
        acceptance.
        """
        if name != "http_request" or result.get("status_code") != 200:
            return
        path = arguments.get("path", "")
        # Dedup on the path WITHOUT its query string: the same endpoint fetched
        # as `.../posts/recent` and `.../posts/recent?page=1&size=100` is one
        # disclosure, not two, and forcing a second proposal for it just
        # produces a near-duplicate finding the text-similarity dedup can't
        # reliably merge (one evidence quote ends up far more verbose than the
        # other, so SequenceMatcher's length-sensitive ratio stays below
        # threshold). Stripping the query here matches how _repeated_prefix_
        # failure_reason / _repeated_identical_response_reason (fixes #36/#37)
        # already treat "the same endpoint" -- existence doesn't depend on the
        # query string. Observed live: without this, one run proposed the
        # community posts/recent leak twice (plain and paginated) and both
        # survived as separate findings.
        path_key = path.split("?", 1)[0]
        if path_key in self._forced_propose_paths:
            return
        if _is_sensitive_path(path):
            reason = f"200 response on sensitive path {path!r}"
        else:
            foreign = self.toolbox.http.foreign_personal_emails(
                result.get("body", ""), arguments.get("account")
            )
            if not foreign:
                return
            shown = ", ".join(foreign[:3]) + ("..." if len(foreign) > 3 else "")
            reason = f"200 response on {path!r} exposes another party's email(s): {shown}"
        self._forced_propose_paths.add(path_key)
        self._force_next_tool = "propose_finding"
        self.step_logger.system(f"forcing propose_finding on the next turn: {reason}")

    def _handle_tool_call(self, step: int, tool_call) -> dict:
        """Parse, guard, and dispatch one tool call. Never raises: every
        failure mode (malformed JSON, unknown tool, dispatch error, detected
        repeat) is turned into a normal observation dict so the model can
        react to it on its next turn.
        """
        name = tool_call.function.name
        raw_arguments = tool_call.function.arguments or "{}"

        def _log_and_return(result: dict) -> dict:
            self.step_logger.observation(step, name, result)
            return result

        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            self.step_logger.malformed_call(step, name, raw_arguments, f"invalid JSON: {exc}")
            # Log the matching OBSERVATION line too, same as the dispatch-error
            # path below -- a run.log reader scanning for one INTENT/TOOL_CALL/
            # OBSERVATION triple per step would otherwise find this step's
            # triple silently missing its OBSERVATION line.
            return _log_and_return({"error": f"malformed_arguments: your arguments were not valid JSON: {exc}"})

        self.step_logger.tool_call(step, name, arguments)

        is_repeat = self.budget.record_call(name, arguments)
        if is_repeat and self.budget.consecutive_repeats >= self.budget.max_consecutive_repeats:
            return _log_and_return(
                {
                    "error": "repeat_blocked",
                    "message": (
                        f"You have made this exact {name} call "
                        f"{self.budget.consecutive_repeats} times in a row with no new "
                        f"information. This call was NOT executed. Try a different endpoint, "
                        f"account, or parameter -- or call finish_investigation if you believe "
                        f"exploration is complete."
                    ),
                }
            )

        try:
            result = self.toolbox.dispatch(name, arguments, step=step)
        except (KeyError, TypeError) as exc:
            self.step_logger.malformed_call(step, name, raw_arguments, str(exc))
            result = {"error": f"tool_call_error: {exc}"}
        else:
            self._maybe_force_propose_finding(name, arguments, result)

        return _log_and_return(result)

    @staticmethod
    def _compact_transcript(messages: list[dict]) -> None:
        """Clip older tool-role and assistant-role message contents in
        place. Idempotent (safe to call every turn): already-compacted
        messages are marked with ``_COMPACTED_MARKER`` and skipped, so this
        never re-truncates an already-short message.

        Assistant messages are included for the same reason tool messages
        are: this model has, on at least one historical run (see
        llm_client.py::LLMClient.chat's comment above its
        frequency_penalty/presence_penalty settings, fix #6), degenerated
        into repeating one sentence in its visible ``content`` until it ran
        out of completion tokens. `tool_choice="required"` now suppresses this
        in practice (every live run to date has empty assistant content on
        every turn), but nothing in the loop actually *prevents* a long
        ``content`` from persisting at full size forever if it recurs --
        only ``tool_calls`` is preserved untouched; that's what a later
        turn's tool response is matched against by ``tool_call_id``.

        Tool messages get two extra passes ``_compact_role`` alone doesn't do
        for assistant messages: duplicate-response collapsing (see the
        module comment above ``_tool_response_signature``) -- aged-out
        ``http_request`` observations that carried an already-seen
        (status_code, body) pair are reduced to a one-line pointer instead
        of their own independent clipped excerpt -- and header-stripping
        (see the module comment above ``_strip_headers_for_clip``) -- the
        `headers` field is dropped before an aged-out observation is clipped,
        so the fixed character budget goes to the body, not routine headers.
        """
        AgentLoop._compact_role(messages, role="tool", keep=KEEP_FULL_TOOL_MESSAGES)
        AgentLoop._compact_role(messages, role="assistant", keep=KEEP_FULL_TOOL_MESSAGES)

    @staticmethod
    def _compact_role(messages: list[dict], *, role: str, keep: int) -> None:
        indices = [i for i, m in enumerate(messages) if m.get("role") == role]
        if len(indices) <= keep:
            return

        # Signatures already kept (in a first-occurrence excerpt, from this
        # call or an earlier one) -- only tool messages carry a signature at
        # all; see _tool_response_signature.
        seen_sigs: set[str] = set()
        if role == "tool":
            for i in indices:
                content = messages[i].get("content") or ""
                if _COMPACTED_MARKER in content:
                    match = _SIG_PATTERN.search(content)
                    if match:
                        seen_sigs.add(match.group(1))

        for i in indices[:-keep]:
            content = messages[i].get("content") or ""
            if _COMPACTED_MARKER in content or len(content) <= OLD_TOOL_MESSAGE_CLIP_CHARS:
                continue

            sig = _tool_response_signature(content) if role == "tool" else None
            if sig is not None and sig in seen_sigs:
                messages[i]["content"] = _DUPLICATE_SUFFIX_TEMPLATE.format(sig=sig)
                continue

            clip_source = _strip_headers_for_clip(content) if role == "tool" else content

            if sig is not None:
                seen_sigs.add(sig)
                suffix = _SIG_SUFFIX_TEMPLATE.format(sig=sig)
            else:
                suffix = _COMPACTED_SUFFIX
            messages[i]["content"] = clip_source[:OLD_TOOL_MESSAGE_CLIP_CHARS] + suffix

    def _run_turns(self) -> str:
        """The main while-loop. Returns the stop reason string."""
        system_prompt = build_system_prompt(
            target=self.config.target,
            account_labels=self.toolbox.http.account_labels(),
            max_steps=self.config.max_steps,
        )
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": INITIAL_USER_MESSAGE},
        ]
        tool_schemas = self.toolbox.schemas()
        step = 0

        while True:
            status = self.budget.status(self.llm.usage.total_tokens)
            if status.exhausted:
                return status.reason()
            if self.toolbox.control.finished:
                return f"model declared completion: {self.toolbox.control.finish_reason}"

            step += 1
            self._compact_transcript(messages)
            force_tool_name, self._force_next_tool = self._force_next_tool, None
            try:
                message = self.llm.chat(messages, tools=tool_schemas, force_tool_name=force_tool_name)
            except Exception as exc:  # noqa: BLE001 -- any LLM-side failure ends the run cleanly
                logger.error("LLM call failed at step %d: %s", step, exc)
                return f"llm_error: {exc}"

            self.step_logger.intent(step, message.content)
            messages.append(message.model_dump(exclude_unset=True, exclude_none=True))

            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                # The model talked without acting. Nudge it rather than
                # silently burning the rest of the budget on chat. Replaces
                # any prior nudge rather than stacking another identical
                # copy (see _replace_singleton_message).
                _replace_singleton_message(messages, NO_TOOL_CALL_NUDGE, {"role": "user", "content": NO_TOOL_CALL_NUDGE})
            else:
                for tool_call in tool_calls:
                    result = self._handle_tool_call(step, tool_call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": json.dumps(result, default=str),
                        }
                    )

            # Deliberately unconditional on whether this turn had tool calls:
            # a turn where the model merely described a call in text instead
            # of invoking it (observed in practice) must not silently skip
            # the reminder -- the whole point is that it fires on a step
            # cadence, not on an "eventful" cadence.
            self.budget.record_step()
            self._maybe_inject_coverage_reminder(step, messages)

    def _maybe_inject_coverage_reminder(self, step: int, messages: list[dict]) -> None:
        """Every COVERAGE_REMINDER_INTERVAL steps, replace the previous
        coverage reminder (if any) with a fresh one -- see the module
        comment above COVERAGE_REMINDER_INTERVAL for why this exists and
        why it replaces rather than stacks.
        """
        if step % COVERAGE_REMINDER_INTERVAL != 0:
            return
        coverage = self.toolbox.http.list_visited_endpoints()
        self.step_logger.system(f"auto coverage reminder injected at step {step}: {coverage}")
        _replace_singleton_message(
            messages,
            _COVERAGE_REMINDER_PREFIX,
            {
                "role": "user",
                "content": (
                    "Coverage reminder (auto-generated, not a new observation): here is "
                    "every request made so far this run, so you don't retry a dead end "
                    f"you've already seen: {json.dumps(coverage, default=str)}"
                ),
            },
        )

    def run(self) -> RunResult:
        configure_logging(self.config.run_log_path)
        start = time.monotonic()

        self.step_logger.system(
            f"run starting | target={self.config.target} | "
            f"accounts={self.toolbox.http.account_labels()} | "
            f"max_steps={self.config.max_steps} | max_total_tokens={self.config.max_total_tokens}"
        )
        try:
            model = self.llm.resolve_model()
            self.step_logger.system(f"LLM model resolved: {model}")
            stop_reason = self._run_turns()
        except Exception as exc:  # noqa: BLE001 -- a startup-time model-resolution failure (e.g.
            # /v1/models unreachable, or returning an empty list -- see llm_client.py's RuntimeError
            # for that case) used to propagate uncaught here and crash the process before any output
            # was written, contradicting this module's own "nothing here ever writes partial/garbage
            # output" guarantee above -- every *other* LLM failure (inside _run_turns) was already
            # caught and turned into a clean stop reason with full deliverables written; this was the
            # one gap. Wrapping both calls in the same try/except closes it without duplicating the
            # write-outputs tail below for a startup-only special case.
            logger.error("LLM call failed before the run could start: %s", exc)
            stop_reason = f"llm_error: {exc}"
        self.step_logger.system(f"run stopped: {stop_reason}")

        findings = list(self.toolbox.control.accepted_findings)
        findings, verifier_rejections = self._finalize_findings(findings)

        wall_clock = time.monotonic() - start
        cost_summary = build_cost_summary(self.llm.usage, wall_clock_seconds=wall_clock, steps=self.budget.steps_used)
        self.step_logger.system(cost_summary.render().replace("\n", " | "))

        self._write_outputs(findings, cost_summary)

        return RunResult(
            findings=findings,
            rejected_count=self.toolbox.control.rejected_count,
            stop_reason=stop_reason,
            steps_used=self.budget.steps_used,
            cost_summary=cost_summary,
            verifier_rejections=verifier_rejections,
        )

    def _finalize_findings(self, findings: list[Finding]) -> tuple[list[Finding], list[tuple[str, str]]]:
        """Run the optional verifier and dedup bonus passes over the
        exploration loop's accepted findings, returning the surviving
        findings plus any verifier rejections (for reporting/logging).

        Note: max_total_tokens bounds only the exploration loop, not this
        finalization tail -- verifying an already-discovered finding isn't
        part of the "exploration" budget, and skipping it just because the
        loop happened to hit its ceiling would defeat the bonus verifier's
        purpose. So the final cost_summary (and run.log's own "Token/cost
        accounting" line) can legitimately read a bit higher than the loop's
        own "token budget exhausted (N/max)" stop line -- that's the
        verifier's own LLM call(s) on top, not a bookkeeping bug.
        """
        verifier_rejections: list[tuple[str, str]] = []

        if self.config.verifier_enabled and findings:
            survivors = []
            for finding in findings:
                survives, reason = verify_finding(finding, self.llm)
                if survives:
                    survivors.append(finding)
                else:
                    verifier_rejections.append((finding.title, reason))
                    self.step_logger.system(f"verifier rejected finding {finding.title!r}: {reason}")
            findings = survivors

        if self.config.dedup_enabled and findings:
            before = len(findings)
            findings = dedup_findings(findings)
            if len(findings) != before:
                self.step_logger.system(f"dedup: {before} findings -> {len(findings)} after merging near-duplicates")

        return findings, verifier_rejections

    def _write_outputs(self, findings: list[Finding], cost_summary: CostSummary) -> None:
        self.config.findings_path.write_text(
            json.dumps([f.model_dump() for f in findings], indent=2) + "\n"
        )
        self.config.summary_path.write_text(
            render_summary(findings, rejected_count=self.toolbox.control.rejected_count)
        )
        self.config.report_path.write_text(
            render_full_report(findings, cost_summary_text=cost_summary.render())
        )
