"""Integration-style offline tests for agent.loop.AgentLoop: the full
reason -> act -> observe cycle, exercised end-to-end with a scripted stub
LLM (no real network/LLM calls) and a fake HTTP session (no real crAPI).

This is the one place tests/ verifies the pieces actually work *together*
-- budget exhaustion, the completion gate, malformed/unknown tool calls,
and output-file writing -- rather than each module in isolation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.config import RunConfig
from agent.llm_client import TokenUsage
from agent.loop import AgentLoop, _is_sensitive_path
from agent.schemas import Account, Credentials
from tests._helpers import FakeResponse as _FakeResponse


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments) -> None:
        self.id = call_id
        self.function = SimpleNamespace(
            name=name, arguments=arguments if isinstance(arguments, str) else json.dumps(arguments)
        )


class _FakeMessage:
    """Mimics the OpenAI SDK message object closely enough for AgentLoop:
    ``.content``, ``.tool_calls``, and a ``.model_dump()`` the loop appends
    straight into the transcript.
    """

    def __init__(self, content=None, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls

    def model_dump(self, exclude_unset=True, exclude_none=True):
        dumped = {"role": "assistant"}
        if self.content is not None:
            dumped["content"] = self.content
        if self.tool_calls:
            dumped["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in self.tool_calls
            ]
        return dumped


class _ScriptedLLM:
    """Drop-in replacement for LLMClient: pops one pre-scripted _FakeMessage
    per .chat() call. Falls back to a no-op "chatty" message once the script
    runs out, so a test doesn't need to script every single turn if it
    expects the run to end (via budget) before the script would run dry.
    """

    def __init__(self, script: list[_FakeMessage]) -> None:
        self._script = list(script)
        self.usage = TokenUsage()
        self.model = "stub-model"
        self.last_messages: list[dict] | None = None
        self.last_force_tool_name: str | None = None
        self.force_tool_name_calls: list[str | None] = []

    def resolve_model(self) -> str:
        return self.model

    def chat(self, messages, tools=None, temperature=0.2, max_tokens=2048, force_tool_name=None):
        self.last_messages = messages
        self.last_force_tool_name = force_tool_name
        self.force_tool_name_calls.append(force_tool_name)
        self.usage.add(SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120))
        if self._script:
            return self._script.pop(0)
        return _FakeMessage(content="nothing left to do")


class _FakeSession:
    # Default body contains a foreign personal email ("victim@...", not the
    # test accounts' own address): the findings-pipeline tests ground a
    # proposal on it, and it is the realistic shape of a cross-user leak.
    # Tests that need a NON-leaking body (to assert the absence of a forced
    # proposal) pass their own via _session_factory_returning().
    def __init__(self, body: str = '{"email": "victim@example.com", "role": "ROLE_USER"}') -> None:
        self._body = body

    def request(self, method, url, headers=None, params=None, json=None, timeout=None, allow_redirects=None):
        return _FakeResponse(text=self._body)

    def post(self, url, json=None, timeout=None):
        return _FakeResponse(text='{"token": "tok"}')


def _session_factory_returning(body: str):
    """A zero-arg session factory (what HttpToolkit._session_factory expects)
    whose sessions always return ``body`` -- lets a test control exactly what
    a 200 response discloses.
    """
    return lambda: _FakeSession(body)


def _http_call(step_id: str, path: str = "/x") -> _FakeToolCall:
    return _FakeToolCall(step_id, "http_request", {"method": "GET", "path": path})


def _propose_other_user_data_call(call_id: str = "p") -> _FakeToolCall:
    """A ``propose_finding`` call for the same "other user's data exposed"
    finding, reused verbatim across the findings-pipeline tests below --
    each test's point is what happens *after* proposal (accepted, verified,
    refuted), not the content of the proposal itself.
    """
    return _FakeToolCall(
        call_id,
        "propose_finding",
        {
            "title": "Other user's data exposed",
            "endpoint": "GET /x1",
            "evidence": '"email": "victim@example.com"',  # must appear in a captured response body
            "why_disclosure": "This endpoint returns another account's email address unintentionally to the caller.",
            "reproduction": ["GET /x1"],
            "confidence": "high",
        },
    )


def _build_config(tmp_path: Path, **overrides) -> RunConfig:
    defaults = dict(
        target="http://target.test",
        credentials=Credentials(accounts=[Account(label="primary", email="a@example.com", password="pw")]),
        llm_base_url="http://llm.test",
        llm_api_key="key",
        llm_model=None,
        max_steps=50,
        max_total_tokens=10_000_000,
        out_dir=tmp_path,
        verifier_enabled=False,
        dedup_enabled=False,
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


def _make_loop(config: RunConfig, script: list[_FakeMessage], session_factory=_FakeSession) -> AgentLoop:
    loop = AgentLoop(config)
    loop.llm = _ScriptedLLM(script)
    loop.toolbox.http._session_factory = session_factory
    return loop


class StepBudgetTests(unittest.TestCase):
    def test_stops_at_step_budget_and_writes_valid_empty_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _build_config(tmp_path, max_steps=2)
            script = [_FakeMessage(tool_calls=[_http_call("1")]), _FakeMessage(tool_calls=[_http_call("2")])]
            result = _make_loop(config, script).run()

            self.assertIn("step budget exhausted (2/2)", result.stop_reason)
            self.assertEqual(result.findings, [])
            self.assertTrue(config.findings_path.exists())
            self.assertEqual(json.loads(config.findings_path.read_text()), [])
            self.assertTrue(config.run_log_path.exists())
            self.assertIn("run starting", config.run_log_path.read_text())
            self.assertTrue(config.summary_path.exists())
            self.assertTrue(config.report_path.exists())


class CompletionGateTests(unittest.TestCase):
    def test_finish_investigation_refused_before_minimum_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=3)
            script = [
                _FakeMessage(tool_calls=[_FakeToolCall("1", "finish_investigation", {"summary": "done already"})]),
                _FakeMessage(tool_calls=[_http_call("2")]),
                _FakeMessage(tool_calls=[_http_call("3")]),
            ]
            result = _make_loop(config, script).run()
            # Refused on turn 1 (0 requests made) -- run continues to the step budget.
            self.assertIn("step budget exhausted", result.stop_reason)

    def test_finish_investigation_honored_after_minimum_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=50)
            # MIN_REQUESTS_BEFORE_FINISH is 6 -- six real requests, then finish.
            script = [_FakeMessage(tool_calls=[_http_call(str(i), f"/x{i}")]) for i in range(6)]
            script.append(_FakeMessage(tool_calls=[_FakeToolCall("f", "finish_investigation", {"summary": "explored enough"})]))
            result = _make_loop(config, script).run()
            self.assertIn("model declared completion: explored enough", result.stop_reason)
            self.assertEqual(result.steps_used, 7)


class MalformedAndUnknownToolCallTests(unittest.TestCase):
    def test_malformed_json_arguments_do_not_crash_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=1)
            script = [_FakeMessage(tool_calls=[_FakeToolCall("1", "http_request", "{not valid json")])]
            result = _make_loop(config, script).run()
            self.assertIn("step budget exhausted", result.stop_reason)
            log_text = config.run_log_path.read_text()
            self.assertIn("MALFORMED_TOOL_CALL", log_text)
            # A malformed-JSON step must still log a matching OBSERVATION
            # line, same as every other step -- previously missing, so a
            # run.log reader scanning for one INTENT/TOOL_CALL/OBSERVATION
            # triple per step would find this step's triple incomplete.
            self.assertIn("OBSERVATION", log_text)

    def test_unknown_tool_name_does_not_crash_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=1)
            script = [_FakeMessage(tool_calls=[_FakeToolCall("1", "delete_the_database", {})])]
            result = _make_loop(config, script).run()
            self.assertIn("step budget exhausted", result.stop_reason)
            self.assertIn("tool_call_error", config.run_log_path.read_text())

    def test_no_tool_call_turn_gets_nudged_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=1)
            script = [_FakeMessage(content="just thinking out loud, no action")]
            result = _make_loop(config, script).run()
            self.assertIn("step budget exhausted", result.stop_reason)


class SingletonMessageTests(unittest.TestCase):
    """Regression tests for the coverage-reminder/no-tool-call-nudge
    accumulation fix: real live-run token accounting showed these
    ``role: "user"`` messages (never touched by ``_compact_transcript``,
    which only clips ``role: "tool"`` messages) were being appended anew
    every time rather than replacing the previous occurrence, so a full
    history dump kept getting resent on every later call for the rest of
    the run. See ``agent.loop._replace_singleton_message``.
    """

    def test_repeated_no_tool_call_turns_leave_exactly_one_nudge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=5)
            script = [_FakeMessage(content="thinking, no tool call") for _ in range(5)]
            loop = _make_loop(config, script)
            loop.run()
            nudges = [
                m for m in loop.llm.last_messages
                if m.get("role") == "user" and m.get("content", "").startswith("You must act by calling")
            ]
            self.assertEqual(len(nudges), 1)

    def test_repeated_coverage_reminders_leave_exactly_one_reminder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=17)  # two reminders fire: steps 8 and 16
            script = [_FakeMessage(tool_calls=[_http_call(str(i), f"/x{i}")]) for i in range(17)]
            loop = _make_loop(config, script)
            loop.run()
            reminders = [
                m for m in loop.llm.last_messages
                if m.get("role") == "user" and m.get("content", "").startswith("Coverage reminder")
            ]
            self.assertEqual(len(reminders), 1)
            # The one surviving reminder is the newer, more complete one (16
            # requests logged), not the stale 8-request snapshot.
            self.assertIn('"count": 16', reminders[0]["content"])


class RepeatDriftTests(unittest.TestCase):
    def test_repeated_identical_calls_keep_getting_blocked_until_step_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=30, max_consecutive_repeats=2)
            # Same exact call forever -- every 2nd call trips a per-call
            # repeat-block (refused, not dispatched), but nothing forces an
            # early stop: the run rides out the full step budget, relying on
            # BudgetTracker alone as the hard ceiling (the separate
            # MAX_TOTAL_REPEAT_BLOCKS forced-stop layer was removed as an
            # unused mechanism -- see HISTORY.md's sixteenth-pass entry).
            script = [_FakeMessage(tool_calls=[_http_call(str(i))]) for i in range(30)]
            result = _make_loop(config, script).run()
            self.assertIn("step budget exhausted (30/30)", result.stop_reason)
            self.assertEqual(result.steps_used, 30)

    def test_default_config_refuses_the_very_first_repeat(self) -> None:
        # Regression test for a real live-observed waste (twenty-fourth
        # pass): a run.log showed the exact same http_request call issued
        # twice in a row for zero new information -- tolerated by the old
        # default of 3 (which only refuses the *fourth* identical call).
        # At the new default (max_consecutive_repeats=1, not overridden
        # here), the second identical call must be refused before dispatch:
        # only one real request should ever reach the fake HTTP session,
        # no matter how many more turns the script repeats it.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=5)
            script = [_FakeMessage(tool_calls=[_http_call(str(i))]) for i in range(5)]
            loop = _make_loop(config, script)
            result = loop.run()
            self.assertEqual(len(loop.toolbox.http.history), 1)
            self.assertEqual(result.steps_used, 5)


class ForcedProposeFindingTests(unittest.TestCase):
    """Fix #32: a 200 on a well-known sensitive path (see loop.py's
    SENSITIVE_PATH_MARKERS) must force the *next* chat() call's tool_choice
    down to propose_finding specifically, not just leave it at "required".
    """

    def test_is_sensitive_path_matches_known_markers_only(self) -> None:
        self.assertTrue(_is_sensitive_path("/.env"))
        self.assertTrue(_is_sensitive_path("/.git/config"))
        self.assertTrue(_is_sensitive_path("/identity/actuator/env"))
        self.assertFalse(_is_sensitive_path("/identity/api/v2/user/dashboard"))
        self.assertFalse(_is_sensitive_path("/community/api/v2/community/posts/1"))

    def test_200_on_sensitive_path_forces_propose_finding_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=3)
            script = [
                _FakeMessage(tool_calls=[_http_call("1", "/.env")]),
                _FakeMessage(tool_calls=[_http_call("2")]),
                _FakeMessage(tool_calls=[_http_call("3")]),
            ]
            # Neutral body (no personal email) so only the path trigger, not
            # fix #41's PII trigger, is in play here.
            loop = _make_loop(config, script, session_factory=_session_factory_returning('{"id": 1}'))
            loop.run()
            # Turn 1 (before any observation) is never forced; turn 2, right
            # after the /.env 200, must be forced; turn 3 must not be (the
            # one-shot flag is consumed and this exact path never re-triggers).
            self.assertEqual(loop.llm.force_tool_name_calls, [None, "propose_finding", None])

    def test_same_sensitive_path_never_triggers_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=4, max_consecutive_repeats=10)
            script = [
                _FakeMessage(tool_calls=[_http_call("1", "/.env")]),
                _FakeMessage(tool_calls=[_http_call("2", "/.env")]),
                _FakeMessage(tool_calls=[_http_call("3")]),
                _FakeMessage(tool_calls=[_http_call("4")]),
            ]
            loop = _make_loop(config, script, session_factory=_session_factory_returning('{"id": 1}'))
            loop.run()
            # Only the first /.env hit forces a turn; the second (same path,
            # already in _forced_propose_paths) does not force a repeat.
            self.assertEqual(loop.llm.force_tool_name_calls, [None, "propose_finding", None, None])

    def test_non_sensitive_non_leaking_200_never_forces(self) -> None:
        # A non-sensitive 200 whose body discloses nothing foreign (only the
        # caller's OWN email, "a@example.com" per _build_config) must not
        # force -- neither fix #32's path trigger nor fix #41's PII trigger.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=3)
            script = [_FakeMessage(tool_calls=[_http_call(str(i))]) for i in range(3)]
            loop = _make_loop(
                config, script, session_factory=_session_factory_returning('{"id": 1, "status": "ok"}')
            )
            loop.run()
            self.assertEqual(loop.llm.force_tool_name_calls, [None, None, None])


class ForcedProposeOnForeignPiiTests(unittest.TestCase):
    """Fix #41: a 200 whose body contains another party's personal email --
    one that is not the requesting account's own -- must force the *next*
    chat() call down to propose_finding, the cross-user-disclosure analogue
    of fix #32's sensitive-path trigger. Verified live against crAPI's real
    /community/api/v2/community/posts/recent leak before wiring; see
    http_tool._EMAIL_RE's module comment.
    """

    def test_foreign_personal_email_forces_propose_finding_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=3)
            script = [_FakeMessage(tool_calls=[_http_call(str(i), f"/feed{i}")]) for i in range(3)]
            loop = _make_loop(
                config, script, session_factory=_session_factory_returning('[{"email": "stranger@example.com"}]')
            )
            loop.run()
            # Each distinct leaking path forces once; the first two scripted
            # turns run before the script is exhausted at the 3-step budget.
            self.assertEqual(loop.llm.force_tool_name_calls[0], None)
            self.assertEqual(loop.llm.force_tool_name_calls[1], "propose_finding")

    def test_callers_own_email_in_body_never_forces(self) -> None:
        # The requesting account seeing its OWN email (the /user/dashboard
        # shape) is not disclosure and must not force. The call must name the
        # account ("primary", a@example.com per _build_config) so its own
        # address is recognized and excluded.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=3)
            script = [
                _FakeMessage(
                    tool_calls=[_FakeToolCall(str(i), "http_request", {"method": "GET", "path": f"/dash{i}", "account": "primary"})]
                )
                for i in range(3)
            ]
            loop = _make_loop(
                config, script, session_factory=_session_factory_returning('{"email": "a@example.com"}')
            )
            loop.run()
            self.assertEqual(loop.llm.force_tool_name_calls, [None, None, None])

    def test_role_mailbox_in_body_never_forces(self) -> None:
        # A generic role/system address (RFC 2142) is not a user's leaked
        # PII -- a site's own support mailbox baked into a page must not force.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=3)
            script = [_FakeMessage(tool_calls=[_http_call(str(i), f"/page{i}")]) for i in range(3)]
            loop = _make_loop(
                config, script, session_factory=_session_factory_returning('{"contact": "support@acme.com"}')
            )
            loop.run()
            self.assertEqual(loop.llm.force_tool_name_calls, [None, None, None])

    def test_same_leaking_path_never_triggers_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=4, max_consecutive_repeats=10)
            script = [_FakeMessage(tool_calls=[_http_call(str(i), "/feed")]) for i in range(4)]
            loop = _make_loop(
                config, script, session_factory=_session_factory_returning('[{"email": "stranger@example.com"}]')
            )
            loop.run()
            # Same path /feed leaks every time, but only the first hit forces
            # (path already in _forced_propose_paths thereafter).
            self.assertEqual(loop.llm.force_tool_name_calls, [None, "propose_finding", None, None])

    def test_query_string_variants_of_same_path_force_only_once(self) -> None:
        # The same endpoint fetched with and without a query string is one
        # disclosure: the force dedup keys on the path before '?' so a
        # paginated variant does not produce a second near-duplicate forced
        # proposal (observed live on /community/.../posts/recent vs
        # .../posts/recent?page=1&size=100).
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=4, max_consecutive_repeats=10)
            script = [
                _FakeMessage(tool_calls=[_http_call("1", "/community/posts/recent")]),
                _FakeMessage(tool_calls=[_http_call("2", "/community/posts/recent?page=1&size=100")]),
                _FakeMessage(tool_calls=[_http_call("3", "/community/posts/recent?page=2")]),
                _FakeMessage(tool_calls=[_http_call("4", "/community/posts/recent")]),
            ]
            loop = _make_loop(
                config, script, session_factory=_session_factory_returning('[{"email": "stranger@example.com"}]')
            )
            loop.run()
            self.assertEqual(loop.llm.force_tool_name_calls, [None, "propose_finding", None, None])


class FindingsPipelineTests(unittest.TestCase):
    def test_accepted_finding_is_written_to_findings_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=20)
            requests = [_FakeMessage(tool_calls=[_http_call(str(i), f"/x{i}")]) for i in range(6)]
            propose = _FakeMessage(tool_calls=[_propose_other_user_data_call()])
            script = requests + [propose]
            result = _make_loop(config, script).run()
            self.assertEqual(len(result.findings), 1)
            written = json.loads(config.findings_path.read_text())
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0]["category"], "information_disclosure")

    def test_verifier_enabled_drops_a_refuted_finding(self) -> None:
        # AgentLoop.run()'s own verifier-finalization wiring (as opposed to
        # verify_finding() itself, which has its own dedicated unit tests in
        # test_verifier.py) was previously untested at the loop level --
        # _build_config's test default is verifier_enabled=False, so no
        # integration test ever exercised this branch. A bug in the wiring
        # itself (e.g. forgetting to reassign `findings = survivors`) would
        # have gone uncaught.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=20, verifier_enabled=True)
            requests = [_FakeMessage(tool_calls=[_http_call(str(i), f"/x{i}")]) for i in range(6)]
            propose = _FakeMessage(tool_calls=[_propose_other_user_data_call()])
            # finish_investigation ends the main loop deterministically
            # (6 requests already satisfies MIN_REQUESTS_BEFORE_FINISH) so
            # the *next* llm.chat() call is the verifier's own, post-loop
            # call -- not another main-loop turn consuming the verdict
            # message meant for the verifier out of the shared script queue.
            finish = _FakeMessage(tool_calls=[_FakeToolCall("f", "finish_investigation", {"summary": "done"})])
            verdict = _FakeMessage(
                tool_calls=[_FakeToolCall("v", "submit_verdict", {"refuted": True, "reason": "caller owns this data"})]
            )
            script = requests + [propose, finish, verdict]
            result = _make_loop(config, script).run()
            self.assertEqual(result.findings, [])
            self.assertEqual(result.verifier_rejections, [("Other user's data exposed", "caller owns this data")])
            self.assertEqual(json.loads(config.findings_path.read_text()), [])

    def test_verifier_enabled_keeps_a_survives_finding(self) -> None:
        # The mirror case of the test above: verifier's "not refuted" path
        # (the finding survives and is kept) -- also previously untested at
        # the loop level.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=20, verifier_enabled=True)
            requests = [_FakeMessage(tool_calls=[_http_call(str(i), f"/x{i}")]) for i in range(6)]
            propose = _FakeMessage(tool_calls=[_propose_other_user_data_call()])
            finish = _FakeMessage(tool_calls=[_FakeToolCall("f", "finish_investigation", {"summary": "done"})])
            verdict = _FakeMessage(
                tool_calls=[_FakeToolCall("v", "submit_verdict", {"refuted": False, "reason": "looks legitimate"})]
            )
            script = requests + [propose, finish, verdict]
            result = _make_loop(config, script).run()
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.verifier_rejections, [])
            self.assertEqual(len(json.loads(config.findings_path.read_text())), 1)

    def test_dedup_enabled_merges_near_duplicate_findings(self) -> None:
        # Same rationale as the verifier test above: dedup_findings() has
        # its own unit tests (test_dedup_and_schemas.py), but the wiring
        # inside AgentLoop.run() that calls it on the real accumulated
        # findings list was untested at the loop level.
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=20, dedup_enabled=True)
            # The two proposals below name /reports/1 and /reports/2, so those
            # endpoints must actually be requested for their (identical, canned)
            # responses to ground the evidence under the endpoint-binding rule
            # (fix #43). The remaining filler requests pad to MIN_REQUESTS.
            requests = [
                _FakeMessage(tool_calls=[_http_call("r1", "/workshop/api/mechanic/reports/1")]),
                _FakeMessage(tool_calls=[_http_call("r2", "/workshop/api/mechanic/reports/2")]),
            ] + [_FakeMessage(tool_calls=[_http_call(str(i), f"/x{i}")]) for i in range(4)]

            def _propose(call_id, endpoint, why_disclosure):
                return _FakeMessage(
                    tool_calls=[
                        _FakeToolCall(
                            call_id,
                            "propose_finding",
                            {
                                "title": "Mechanic report leaks customer data",
                                "endpoint": endpoint,
                                "evidence": '"email": "victim@example.com"',
                                "why_disclosure": why_disclosure,
                                "reproduction": [f"GET {endpoint}"],
                                "confidence": "high",
                            },
                        )
                    ]
                )

            script = requests + [
                _propose(
                    "p1",
                    "GET /workshop/api/mechanic/reports/1",
                    "Phone numbers of other customers leak through the mechanic report endpoint when you change the id.",
                ),
                _propose(
                    "p2",
                    "GET /workshop/api/mechanic/reports/2",
                    "Email addresses of other customers leak through the mechanic report endpoint when you change the id.",
                ),
            ]
            result = _make_loop(config, script).run()
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(len(json.loads(config.findings_path.read_text())), 1)


class StartupModelResolutionFailureTests(unittest.TestCase):
    """Regression test for fix #23: a resolve_model() failure at startup
    (e.g. the LLM endpoint's /v1/models is unreachable) used to propagate
    uncaught and crash the process before any output was written. run()
    now wraps both resolve_model() and _run_turns() in one try/except so
    this fails exactly like any other LLM error: a clean stop reason plus
    full, valid (empty) output files.
    """

    def test_resolve_model_failure_yields_clean_stop_and_valid_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = _build_config(tmp_path, max_steps=5)
            loop = _make_loop(config, script=[])

            def _boom() -> str:
                raise RuntimeError("no models advertised by endpoint")

            loop.llm.resolve_model = _boom
            result = loop.run()

            self.assertIn("llm_error", result.stop_reason)
            self.assertIn("no models advertised by endpoint", result.stop_reason)
            self.assertEqual(result.findings, [])
            self.assertTrue(config.findings_path.exists())
            self.assertEqual(json.loads(config.findings_path.read_text()), [])


class MidLoopLLMFailureTests(unittest.TestCase):
    """_run_turns()'s per-turn try/except around llm.chat() (as opposed to
    the startup-only resolve_model() failure covered above) -- a real LLM
    failure partway through an otherwise-successful run, e.g. a transient
    network error on turn 2 after turn 1 succeeded. Previously untested.
    """

    def test_mid_loop_chat_failure_ends_run_cleanly_with_prior_progress_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _build_config(Path(tmp), max_steps=20)
            loop = _make_loop(config, script=[_FakeMessage(tool_calls=[_http_call("1")])])
            original_chat = loop.llm.chat
            calls = {"n": 0}

            def _flaky_chat(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("connection reset")
                return original_chat(*args, **kwargs)

            loop.llm.chat = _flaky_chat
            result = loop.run()

            self.assertIn("llm_error", result.stop_reason)
            self.assertIn("connection reset", result.stop_reason)
            self.assertEqual(result.steps_used, 1)  # step 1 succeeded before step 2's failure
            self.assertTrue(config.findings_path.exists())
            self.assertEqual(json.loads(config.findings_path.read_text()), [])
            self.assertTrue(config.run_log_path.exists())
            self.assertIn("run starting", config.run_log_path.read_text())
            self.assertTrue(config.summary_path.exists())
            self.assertTrue(config.report_path.exists())


if __name__ == "__main__":
    unittest.main()
