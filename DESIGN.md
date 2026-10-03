# DESIGN.md

## Control loop

`AgentLoop.run()` (`agent/loop.py`) is a single `while True` turn loop, no framework
(LangGraph/LangChain considered and deliberately skipped -- see "Framework choice" below). Each
turn:

1. Check termination conditions (budget exhausted, or model declared finished) --
   *before* spending anything on an LLM call.
2. Call the LLM with the full running transcript + the six tool schemas.
3. Log the model's intent (its free-text content, if any).
4. Dispatch every tool call it made this turn (usually one; the model sometimes issues several in
   parallel, e.g. probing three endpoints in one turn -- all are honored and each gets its own
   tool-response message).
5. Append each observation to the transcript and go to 1.

This is intentionally the entire control flow. There is no separate "planner" phase, no
pre-scripted sequence of endpoints, and no branching on *what* the model finds -- the branching
that exists is purely about safety (budget, repeats), never about *content* (which endpoint to hit
next is 100% the model's decision, every turn). That's the concrete meaning, in this codebase, of
"reasons its way to bugs, not a hardcoded checklist."

### Framework choice

Hand-rolled loop over LangGraph/LangChain. The loop is genuinely simple (one call, one dispatch,
one append, repeat) and a framework's abstractions (nodes, edges, state reducers) would add
indirection without adding capability here -- the spec explicitly says a clean hand-rolled
loop is "perfectly acceptable and often clearer," and for a single-agent, single-loop system with
six tools, that was true in practice too.

## Tools

Six tools, two toolkits (`agent/toolbox.py` wires both into the schema list the LLM sees):

- **`http_request`** (`agent/tools/http_tool.py`) -- the only network I/O in the codebase. Takes
  method/path/account/headers/json_body/query; resolves the named account's bearer token lazily
  (logging in on first use, caching after); returns status/headers/body, with the body capped at
  4000 characters (generous enough to see several full JSON records -- enough to notice extra
  fields -- while bounding cost on large list endpoints). Errors (connection failures, timeouts)
  come back as data, never as a crash.
- **`list_visited_endpoints`** -- a coverage map (method, path, account, status per past call),
  grounded in what the agent actually did: it costs nothing to maintain and directly supports both
  avoiding pointless repeats and reasoning about what's left to try. It is complementary to
  `discover_api_endpoints` (below), which is the tool that actually surfaces *new* endpoints. A
  generic path-candidate crawler (scan response bodies for path-shaped substrings not yet
  requested, surface them here) was tried as an earlier approach to endpoint discovery and then
  removed: across every live run it was kept for, its output was never anything but trivial
  static-asset paths (favicon, bundled JS/CSS) that never fed into a finding -- an unused
  mechanism, not a hedge worth its complexity. The twenty-seventh pass replaced that dead end with
  `discover_api_endpoints`, which reads the SPA's bundled JavaScript for crAPI's real API surface
  and is how the promoted run's vehicle-location BOLA was reached.
- **`discover_api_endpoints`** (`agent/tools/http_tool.py`, twenty-seventh pass) -- fetches the
  single-page-app's bundled JavaScript and extracts the real API paths the frontend calls, the one
  mechanism that broke the long-standing recall ceiling on blind noun-guessing (the model never
  reached crAPI's real vehicle/community endpoints by guessing alone across ~14 earlier runs). The
  model still decides whether to call it and which of the surfaced endpoints to pursue.
- **`list_id_candidates`** (eighteenth pass) -- a narrower, single-hop successor to the removed
  crawler idea, added after the user proposed a BFS-style crawler and it was checked against
  the spec and rejected (an autonomous multi-hop traversal is exactly the "fixed,
  pre-scripted sequence of requests" the "autonomous loop" goal disallows, and reads as "a perfect scanner" per
  the project's framing). This tool makes zero HTTP requests itself: given a `path` already in history, it
  inspects only that one cached response body and returns ID-shaped fields (generic REST
  convention: `id`/`uuid`/`*_id`/`*Id`/`*Uuid`, not crAPI-specific names), plus candidate paths with
  a same-shaped ID segment substituted (numeric-for-numeric, UUID-for-UUID -- a live check against
  real crAPI found that substituting the wrong "flavor" produces a guaranteed-to-fail candidate, see
  HISTORY's eighteenth-pass section). The model still decides whether to call it and whether to act
  on anything it returns -- every hop remains the model's own decision, unlike the rejected BFS
  design.
- **`propose_finding`** (`agent/tools/control_tool.py`) -- how the model submits a candidate
  finding. This is the one place where "tool use" and "finding validation" (the tool-use boundary and finding-validation requirement) intersect: the tool's own schema has no `category` argument at all (every accepted
  finding is unconditionally `information_disclosure`), and the call runs through the scope gate
  and validator (below) before it's accepted. A rejection comes back as a normal tool result with
  a reason, so the model can react to it next turn -- rejection is part of the loop, not a dead end.
- **`finish_investigation`** -- the model's explicit "I'm done" signal (the completion-condition
  half of the termination requirement). Guarded by a minimum-requests floor
  (`agent/tools/control_tool.py::MIN_REQUESTS_BEFORE_FINISH`, currently 6): a call before that
  floor is refused with a reason rather than honored, because forcing the model to always call
  *some* tool (see "Termination" below for why that's forced) means it can occasionally reach for
  this one reflexively on turn one, before a single real request -- observed in practice, not
  theoretical.

## Scoping to information disclosure

Two independent layers, deliberately redundant with each other:

1. **Prompt layer** (`agent/scope.py::SCOPE_SYSTEM_TEXT`, injected via `agent/prompts.py`): states
   the in-scope/out-of-scope categories from the spec verbatim, and adds one instruction not
   in the spec text itself: *describe findings in terms of what data leaked, never by
   vulnerability-class name* (no "BOLA", "IDOR", "SQLi", etc.). This is what makes layer 2 work.
2. **Code layer** (`agent/scope.py::check_scope`, called from `agent/validation.py`): a
   deterministic keyword scan over the finding's own title + reasoning text. If it mentions an
   out-of-scope technique name (SQL injection, XSS, CSRF, SSRF, RCE, rate limiting, mass
   assignment, JWT forgery, brute force, business-logic abuse...), it's rejected outright,
   regardless of what `category` claims.

Why keyword matching and not a second LLM judgment call: this check must be cheap, deterministic,
and auditable -- a reviewer can see the exact keyword that triggered a rejection. The known cost
is a real disclosure finding that happens to *mention* an unrelated technique in passing would be
rejected too; given this project weighs scope discipline heavily and explicitly wants zero
out-of-scope reports, this false-negative-favoring tradeoff is deliberate (see README's "Known
limitations").

BOLA/IDOR-shaped bugs are explicitly *not* blanket-rejected: the project's own scope list
includes "other users' records inside a list response," so a finding is allowed as long as it's
framed as *what got exposed*, not as *which access-control mechanism failed*.

## Finding validation

`agent/validation.py::validate_finding` runs three checks, all required, none of which trust the
model's self-assessment alone (the goal: "no raw 'the model said so'"):

1. **Scope** (above).
2. **Evidence is grounded** -- a distinctive snippet of the claimed `evidence` string must
   actually appear in a response body the run genuinely captured this session (tracked in
   `HttpToolkit._body_cache`, including response headers, since a leak can live there too). A
   fabricated or hallucinated claim has nothing to match against and is rejected.
3. **Reasoning is substantive** -- `why_disclosure` must clear a minimum length and must not just
   restate the title verbatim (a common weak-effort failure mode).

The optional bonus **adversarial verifier** (`agent/verifier.py`) runs after this, as a genuinely
independent second opinion: a fresh conversation (no shared history with the discovery agent),
system-prompted to actively try to refute the finding, forced into a structured verdict via a tool
call, with one retry if the model produces no tool call at all (a live-testing finding -- see
HISTORY's fix #9 -- since this call can hit the same `tool_choice="required"`-still-empty failure
mode as the main loop). It fails *open* (keeps the finding) on any verifier-side error or on a
second empty response, since a bonus safety net erroring out must never be the reason a validated
finding is lost.

## Termination

Two independent mechanisms, checked in this order every turn (`agent/loop.py::_run_turns`):

1. **Hard budget** (`agent/budget.py`) -- absolute step count and total LLM token ceiling. This is
   the backstop that guarantees the run ends no matter what.
2. **Model-declared completion** -- `finish_investigation`, checked before budget so a model that
   correctly judges it's done doesn't burn one more wasted turn.

Additionally, **consecutive-repeat detection**: identical tool name + arguments (ignoring the
loop-assigned `step` field, and normalizing a trailing slash on `path` -- see the case study below)
repeated `--max-consecutive-repeats` times in a row (default 1 -- zero-tolerance, see HISTORY's
configuration table and the twenty-fourth pass below for why it was tightened from the original 3)
blocks the *next* identical call from actually dispatching and tells the model why, without ending
the run -- it's a nudge, not a stop; the hard step/token budget above is what eventually ends a run
that keeps tripping this repeatedly. (A third layer on top of these two -- a forced-stop counter
that tripped after 5 such repeat-blocks in one run -- was tried and removed in the sixteenth pass as
an unused mechanism; see HISTORY's "A sixteenth pass" section.) Critically, switching only the
`account` argument between two otherwise-identical calls is *never* flagged as a repeat -- that's the
core mechanism for testing cross-account data exposure, and conflating it with wasteful looping was
an explicit trap called out during design.

A further, narrower anti-drift mechanism exists because exact-repeat detection provably wasn't
enough in practice (see the case study below for how it was found): a guard against
pathologically-constructed paths (`agent/tools/http_tool.py::_degenerate_path_reason`). A second,
softer mechanism -- a hint when many consecutive requests to the same path *shape* all failed --
was tried alongside it and removed in the sixteenth pass; see the same HISTORY section. A twentieth
pass then found live evidence for exactly that same problem class, just shaped differently -- a
model hammering one dead endpoint with an ever-different, fabricated query string on each retry,
rather than a sequential-ID path sweep -- and added a hard-refusal guard for it,
`_repeated_prefix_failure_reason` (fix #36): unlike the removed hint, this one blocks the dispatch
outright once the last `MAX_PREFIX_FAILURE_STREAK` requests to the same `(method,
path-before-query-string)` have all failed, since the evidence this time showed a soft nudge
wouldn't have been enough.

Critically, **the model must actually be forced to call a tool every turn** (`tool_choice=
"required"`, `agent/llm_client.py`) rather than left to decide (`tool_choice="auto"`) -- see the
case study below. This is listed under Termination rather than just "Tools" because it turned out
to *be* a termination/progress bug: with `"auto"`, a meaningful fraction of turns silently produced
neither a tool call nor usable content, burning a full budgeted turn on nothing.

## Case study: debugging the real run (why this section is long)

Full blow-by-blow is in `HISTORY.md`'s "Status of the committed real run" section; the summary:
eight live runs were executed, and every early zero-finding result was root-caused to a specific,
fixable bug rather than shrugged off. In order found: (1) unbounded transcript growth exhausted
the token budget by step 27 -- fixed with recency-weighted compaction; (2) a trailing-slash
repeat-detection gap let the model dodge exact-match repeat detection -- fixed by normalizing the
signature; (3) degenerate path construction (extending one path with the same segment forever) --
fixed with a dedicated guard; (4) a sequential-ID sweep with no pivot -- addressed with a
same-family failure-streak hint (later removed in the sixteenth pass as never observed firing);
(5) coverage memory lost to compaction, since the model never
proactively called `list_visited_endpoints` -- fixed by auto-injecting a coverage summary every 8
steps; (6) **the root cause**: `tool_choice="auto"` let the model finish its hidden reasoning trace
and stop with *both* `content` and `tool_calls` empty on a meaningful fraction of turns (sometimes
after the reasoning itself degenerated into repeating one sentence) -- fixed with
`tool_choice="required"` plus mild repetition penalties; (7) that fix exposed a premature
`finish_investigation` call on turn one -- fixed with a minimum-requests floor.

Runs 7-8 (56 and 72 steps) then behaved close to designed: zero forced repeat-blocks in either run;
run 8's own log has exactly one malformed call and one no-tool-call turn out of 72 steps (both
absorbed gracefully, run continued normally -- see HISTORY's fuller account), not the zero of either
that an earlier draft of this document claimed. Otherwise, systematic coverage of all three services
with both accounts and genuine cross-account comparison (the same `dashboard` endpoint hit as both
accounts, each correctly returning only its own record -- see HISTORY for the exact steps). No
query-parameter manipulation actually appears in that log at all; an earlier draft of this document
claimed there was some, which was incorrect and has been removed. Those eight runs still produced
zero accepted findings -- the model never discovered crAPI's actual, non-obvious resource paths,
exploring a plausible-looking but incorrect REST namespace instead.

A follow-up session then found and fixed several real token-efficiency/correctness bugs (a
coverage-reminder/nudge message-accumulation bug, an unguarded assistant-content compaction gap, a
response-truncation field-order bug that silently dropped bodies in favor of boilerplate headers,
and an `on_challenge_list` classifier false-positive bug -- see HISTORY's numbered list, fixes
#12-15). During that session, **run 9** (same budget as run 8) produced this project's first
accepted finding: an unauthenticated `GET /.env` exposing real database credentials -- a generic
dotfile-probing guess, not navigation of crAPI's actual API surface, and confirmed via crAPI's own
git history to be a real upstream bug, not on the public challenge list. An immediate follow-up run
at the identical budget found nothing at all -- real evidence of run-to-run instability at this
model's capability level, reported rather than hidden. Given a clean, non-degenerate harness around
both outcomes, the *ceiling* here reads as this specific model's capability on blind API discovery
within a bounded budget (it still never found the real vehicle/mechanic-report/order paths), while
the *one hit it did land* is genuine evidence the harness can surface a real, generalizing finding
when the model's guess happens to intersect reality. See "another week" below.

A later pass deliberately tried to close that gap by refining `RECON_TEXT` (an explicit
cheap-recon-first ordering, BOLA named by its standard OWASP term, and a nudge toward the domain
nouns `MISSION_TEXT` already gives), validated across four more live runs. Two more real bugs
surfaced along the way -- an evidence-grounding check that could reject a legitimate finding over a
model quoting quirk (literal double-escaped newlines), and an `on_challenge_list` keyword set too
generic to give a stable answer for the same finding across runs (see HISTORY's fixes #16-17) -- both
fixed and regression-tested. The prompt refinement itself did not close the gap: none of the four
runs found anything beyond the already-known `.env` disclosure, and the model still never tried any
of crAPI's real vehicle/mechanic-report/order paths. A JS-bundle-reading nudge tried in the same pass
was removed after live evidence showed it structurally can't work under the current
`MAX_BODY_CHARS` truncation (see `FUTURE_IMPROVEMENTS.md`). Net effect: two durable correctness
fixes shipped; the capability ceiling on blind API discovery, identified above, was unchanged by
this prompt refinement (it was broken later, by a different mechanism -- see the closing note below).

A follow-up experiment tried a much bigger budget instead (150 steps / 1.5M tokens vs. the 40/400k
default) to see whether the ceiling was really capability, not budget. It confirmed budget wasn't
the constraint: the run found 0 findings (worse than the default budget), burning its entire budget
on ~15 consecutive identical `dashboard?id=N` probes against an endpoint that ignores the query
parameter -- more steps just bought more unproductive guessing. That investigation did surface one
real, fixable inefficiency: duplicate tool responses were each independently clipped by transcript
compaction instead of collapsed once recognized as identical, fixed with a content-hash-based
duplicate pointer (HISTORY fix #18). A follow-up closed a related gap: headers still ate into the
fixed clip budget whenever the body itself was short, cutting off mid-header-value for zero
disclosure signal; fixed by dropping `headers` entirely before an aged-out message is clipped
(HISTORY fix #19). 186 tests at that point.

A later pass swapped the model itself rather than the budget or prompt -- see HISTORY's "A model-swap
experiment" section for the full account. It confirmed model capability was a real part of the
remaining gap (a stronger model found a second real disclosure the baseline never attempted) and
surfaced two more real bugs in the process: the bonus verifier's skepticism default had no burden of
proof for personal data and wrongly refuted that finding (fix #20), and an unhandled empty-`choices`
API response crashed a run instead of retrying (fix #21). 190 tests at that point.

An eleventh pass was a dedicated code/documentation review rather than a live run: two independent
fresh audits over every module and every doc found five more real bugs (HISTORY fixes #22-26) --
a challenge-classifier misattribution from first-match-wins instead of best-match-wins (#22, the same
false-positive bug class as fix #15, just not exhaustively closed the first time), a startup-time
`resolve_model()` crash with no exception handling that skipped writing any output (#23), an
order-dependent semantic-dedup clustering bug fixed by switching to proper union-find connected
components (#24), a documented-but-never-implemented lexical-overlap validation check (#25), and a
missing log line on one of two malformed-tool-call code paths (#26) -- plus dead-code removal
(a duplicate `__main__` guard in `cli.py`) and several stale doc cross-references corrected to match
the tenth pass's two-finding result. See HISTORY's "An eleventh pass" section for the full account.
194 tests as of that pass, up from 190.

A twelfth pass combined a final pre-release evaluation with a dead-code/duplication and
documentation-obsolescence sweep. The evaluation found one more real bug (#27): the evidence-body
cache backing finding-grounding (`HttpToolkit._body_cache`) was bounded to ~50 requests of history,
the only place full response bodies are kept for `validation.py`'s grounding check, so a run past
that length (this project has run up to 150 steps) could silently reject a genuine finding whose
evidence had aged out. The sweep found no dead code in `agent/agent/` itself, but did find and
consolidate two real duplications in `agent/tests/` (a `Finding`-builder helper redefined three
times, a `requests.Response` fake redefined twice) into a new shared `agent/tests/_helpers.py`, and
fixed two stale doc references (a claimed `backup_run9_pre_pass10/` backup that doesn't exist on
disk, and this file's own now-corrected "one real finding" sentence in the crawler-removal section
above). See HISTORY's "A twelfth pass" section for the full account. 197 tests as of that pass.

A thirteenth pass reviewed the modules that had gotten comparatively little scrutiny across the
prior twelve (`schemas.py`, `scope.py`, `cost.py`, `report.py`, `prompts.py`, `toolbox.py`,
`logging_setup.py`, `config.py`, `budget.py`, `tools/control_tool.py`, `cli.py`, `__main__.py`) and
found two more real bugs, both in `config.py`: a non-positive `--request-timeout` wasted an entire
run on a misleadingly-labeled stop reason instead of failing fast (#28), and an `--out-dir` colliding
with an existing file crashed with a raw traceback instead of a clean CLI error (#29) -- both
verified live against the real CLI. A second audit checking the twelfth pass's own doc edits found
one broken cross-reference (this file pointed at a HISTORY heading that didn't yet exist as its own
section; fixed by giving it one, see HISTORY's "A thirteenth pass" section for the full account).
201 tests as of that pass.

A fourteenth pass re-checked the thirteenth pass's own two fixes by execution rather than assuming
tests passing meant fully correct, and found fix #28's `<= 0` timeout check missed `NaN` and
`+Infinity` (#30) -- both genuinely CLI-reachable and both reproducing fix #28's exact failure mode.
That re-check also installed `coverage.py` for the first time in this project and used it to close
real gaps two rounds of reading-only review had missed: `control_tool.py`'s schema-rejection branch,
`http_tool.py`'s `RequestException` handling, and -- the largest one -- `AgentLoop.run()`'s own
verifier/dedup finalization wiring, never exercised at the loop level because the test suite's own
config default disables both. It also found and fixed one test-only bug (`test_cli.py`'s dotenv test
gave a false failure specifically under `coverage run`, unrelated to any real behavior difference)
and, as a dedicated systematic pass rather than an opportunistic byproduct, swept every docstring and
comment in `agent/agent/` for staleness, finding and fixing two: a broken `loop.py` cross-reference
to a case study that actually lives in `llm_client.py`, and an order-of-magnitude-stale memory
estimate on `EVIDENCE_BODY_CACHE_SIZE` left over from before that constant was raised 40x in the
twelfth pass. See HISTORY's "A fourteenth pass" section for the full account. 213 tests now.

A fifteenth pass deliberately switched verification technique instead of running a fifteenth reading
review: `hypothesis` property-based testing, installed for the first time in this project, targeted
`config.py`'s numeric bounds, `validation.py`'s untrusted-model-text handling, and `schemas.py`'s
pydantic validators. Direct execution ahead of writing the fuzz properties surfaced the actual bug: the
three other hard-cap settings (`--max-steps`/`--max-tokens`/`--max-consecutive-repeats`) had no bounds
check at all, unlike `--request-timeout` (fixes #28/#30) -- a non-positive value didn't crash, it
silently produced a fully-formed, "successfully completed" empty run instead of a clear startup error
(#31). Fourteen prior passes, several specifically auditing `config.py`, never caught this. The
property tests themselves (100 generated examples each against the validation/schema functions) found
no further bugs -- a genuinely informative negative result, not a wasted pass, after fourteen passes'
worth of hardening on those specific modules. A second bug surfaced only by live CLI verification, not
by any test: the new `--max-tokens` error message initially named the wrong flag
(`--max-total-tokens`, the internal field name). See HISTORY's "A fifteenth pass" section for the full
account, including its take on why switching *technique* found what switching *reviewer* had stopped
finding. 234 tests now.

A sixteenth pass was explicitly scoped as simplification, not a bug hunt: four parallel fresh-eyes
surveys of every module in `agent/agent/` converged on a small set of pure refactors (a shared
`config.py` resolve+validate helper, a shared JSON-parse helper in `loop.py`, extracted
`_finalize_findings`/`_maybe_inject_coverage_reminder` methods, a `_reject` helper in
`control_tool.py`, a public `scope.keyword_pattern` plus a shared pattern-compilation helper, named
prompt-text constants) plus two mechanism removals, both flagged to and approved by the user first:
the same-path-family failure-streak hint (`http_tool.py::_track_family_outcome`, originally fix #4)
and the `MAX_TOTAL_REPEAT_BLOCKS` forced-stop counter (`loop.py`), neither of which had ever visibly
fired in the one committed `run.log` this project has kept (confirmed via a direct grep for `"hint"`
and `"repeat_blocked"`) -- the same "unused mechanism, not a hedge" standard the sixth pass's crawler
removal already established. Verified with the full test suite (226 tests, down from 234 -- the
eight removed were the two mechanisms' own dedicated tests), `pyflakes`, and a live 12-step run
against the real crAPI stack. See HISTORY's "A sixteenth pass" section for the full account.

A seventeenth pass, prompted by collecting a fresh final live run, found three escalating-budget
`qwen36-35b-a3b` runs all coming back with zero findings -- worse than the already-committed
two-finding result, which depended on a one-off model swap not part of this project's
reproducible config. Comparing the three runs' logs directly (not just re-reading code) showed the
model calling `http_request` 199 of 200 times, `propose_finding` zero times, and producing zero
free-form reasoning text on any turn -- despite one run clearly observing a 200 on the real `.env`
leak. Ruled out as a prompt gap or a wiring bug (both were fine); the actual cause is this model's own
tool-selection bias under `tool_choice="required"`, which a soft nudge cannot fix (nothing softer than
a hard constraint moves a model that never reasons in text). Fixed with two changes: (#32) a
narrower, one-turn version of the same `tool_choice` mechanism already used for "any tool" --
`agent/llm_client.py::LLMClient.chat`'s new `force_tool_name` parameter lets `agent/loop.py` compel
`propose_finding` specifically for exactly one turn, triggered only by a real 200 on one of a small,
machine-readable mirror of the sensitive-file paths `RECON_TEXT` already names in prose; and (#33) a
`_drop_unrecognized_kwargs` filter in `agent/toolbox.py::ToolBox.dispatch`, added after live-testing
fix #32 immediately surfaced a chat-template leak that corrupted a forced call with an extra key,
crashing dispatch with an opaque `TypeError` instead of failing through the normal, model-visible
rejection path. Live-verified across 7 runs at default budget: the forced mechanism fired in 3,
producing 2 genuine accepted `.env` findings (one promoted; one with a degenerate `reproduction`
field, not promoted) and 1 case where fix #33's exact grammar-leak mode recurred even with the fix in
place (the model's retries kept omitting required fields outright, correctly rejected each time,
then correctly repeat-blocked). See HISTORY's "A seventeenth pass" section for the full account. Final
verification during this same pass ran the suite five times in a row and hit one more real bug this
way: `hypothesis` fuzzing `validation.py::_normalize` found it wasn't idempotent for an uppercase
`\N`/`\R`/`\T` escape -- the old lowercase-last ordering only case-folded it *after* the
lowercase-only collapse regex had already run, so a real, single-application call (the only way
`validate_finding` ever calls it) could miss collapsing an uppercase escape sequence. Fixed (#34) by
lowercasing first. 234 tests now.

A nineteenth pass cleared all prior outputs and collected one more fresh live run. The first of
three runs found nothing (fix #32's forced retry hallucinated the wrong endpoint and omitted a
required field -- the same imperfect-retry failure mode already documented above, recurring
honestly rather than being hidden). The second accepted 2 findings that turned out to both describe
the identical `.env` disclosure, proposed twice with unrelated wording -- `agent/dedup.py` only ever
compared `why_disclosure` similarity (~0.10 between the two write-ups) and never looked at
`evidence` similarity (~0.95, since the captured credential dump was byte-for-byte identical both
times), so it had no signal to merge them. Fixed (#35) by clustering on
`max(why_disclosure similarity, evidence similarity)`; two existing tests that had accidentally
relied on `make_finding`'s shared default `evidence` value being identical were given distinct,
narrative-matching evidence so they still isolate what they're meant to test. 255 tests now. A third
run post-fix produced the clean single-finding result then committed. See HISTORY's "A nineteenth
pass" section for the full account.

A twentieth pass bumped the budget 3x (`--max-steps 120`/`--max-tokens 1200000`) to directly test
whether more budget changes the outcome. It didn't (same single finding, reconfirming the ninth
pass), but reading that run's own log to answer "why no more findings" surfaced a real,
previously-unseen budget-wasting pattern: 47% of the post-finding `http_request` calls (41 of 87)
went to guessing three non-existent list-style endpoints while stuffing an ever-larger pile of
fabricated query parameters directly into the `path` argument on each retry -- a cross-call
degeneration neither `_degenerate_path_reason` (fix #3, a single-string check) nor the exact-match
repeat detector could see, since no individual query string repeated a pattern within itself. Fixed
(#36) with `_repeated_prefix_failure_reason`: inspects `HttpToolkit.history` and refuses a request
whose `(method, path-before-'?')` matches the last `MAX_PREFIX_FAILURE_STREAK` (3) requests to that
same prefix, all of which failed -- query string deliberately ignored, since an endpoint's existence
doesn't depend on it. Unlike the sixteenth pass's removed failure-streak hint (a nudge that never
fired), this is a hard refusal like the degenerate-path guard, since the evidence this time is that a
soft nudge wouldn't have stopped a model that kept retrying the same wrong idea unprompted for ~40
steps. 6 new tests, 261 total. Live-verified: the guard fired twice on a fresh run at the same 3x
budget, refusing further guesses at a dead endpoint, with the same single `.env` finding produced
with no regression (and the nineteenth pass's dedup fix also confirmed firing correctly on this run).
See HISTORY's "A twentieth pass" section for the full account.

A twenty-first pass added a well-known OpenAPI/Swagger spec-path check to `RECON_TEXT` (a new step
2, existing steps renumbered): standard framework convention paths (`/v3/api-docs`,
`/swagger.json`, etc.) tried at the root and under each service prefix, unauthenticated then once
authenticated. A pure prompt-text change, verified directly against the live stack before writing
it (all such paths 404 on this target, both unauthenticated and authenticated) rather than assumed
-- and that probing surfaced one incidental fact folded into the new text's caveat: `/identity/*`
returns a generic 401 for *any* unrecognized unauthenticated path, not a 404, so an unauthenticated-
only probe there can't tell a real gated route from a dead one. No code or test changes (261 tests
unchanged); live-verified with a fresh 40-step run that the model does pick up and try the new
paths on its own (`/v3/api-docs`, `/swagger.json`, `/identity/v3/api-docs`), with no regression to
the existing `.env` finding. See HISTORY's "A twenty-first pass" section for the full account.

A twenty-second pass added `_repeated_identical_response_reason` (`agent/tools/http_tool.py`), the
mirror image of the twentieth pass's repeated-*failure* guard: three requests to the same path
prefix that all succeed with a byte-identical response mean whatever the model is varying (query
string, body) has no effect, so a further variation is refused the same way a further failing guess
already is. Prompted by a user question about a captured `run.log` showing exactly this pattern
(`?user_id=9` repeated three times, same body every time, no existing guard catching it). Needed a
new `RequestRecord.response_signature` field -- a short hash, not the body itself, since bodies are
deliberately excluded from that record for memory/log-size reasons. 8 new tests (268 total), verified
live two ways: a 60-step run showed no regression, and a direct probe script against real crAPI
confirmed the guard firing on genuine (non-mocked) repeated-identical responses. See HISTORY's "A
twenty-second pass" section for the full account.

A twenty-third pass cleared outputs and re-ran fresh: 89 clean steps, same `.env` finding accepted,
but its `on_challenge_list` field came back `true` (attributed to the chatbot-credential-extraction
challenge) purely because challenge-17's keyword list included the bare word `"credential"`, and the
finding's own text said "credential-based attacks" -- the same false-positive keyword-collision bug
class as fixes #15/#22, just in a keyword neither of those fixes touched. Fixed (38) by dropping
`"credential"` from that challenge's keywords, keeping only `"chatbot"` (the actually distinctive
signal) and `"another user's"`. Since the classifier is a pure post-hoc function over already-
captured finding text, the fix was re-applied directly to the promoted finding rather than requiring
a full re-run; `findings.json`/`summary.md`/`report.md` now correctly read `on_challenge_list: false`,
while `run.log`'s own inline log line is left as an unedited historical artifact, per this project's
established precedent. 2 new regression tests (270 total). See HISTORY's "A twenty-third pass"
section for the full account.

A twenty-fourth pass tightened `DEFAULT_MAX_CONSECUTIVE_REPEATS` from 3 to 1 (fix #39), prompted by
the user spotting a captured `run.log`'s repeated path-guessing pattern -- most of it an honest model
noun-guessing limitation (it never tried the real `/identity/api/v2/vehicle/vehicles` endpoint), but
two consecutive byte-identical `http_request` calls were a genuinely fixable gap: the old default
tolerated up to 3 free repeats before refusing the 4th. A coarser same-topic pivot guard was
considered and set aside pending more evidence, following the precedent of two previously-removed,
similarly-shaped heuristics; the exact-repeat threshold is a narrower, already-tested, already-
configurable knob with a clear justification (a repeat against a deterministic local target can
never show anything new). 2 new tests (272 total). Live-verified in a same-day follow-up: a clean
97-step run showed no regression, and a direct probe against the real target confirmed the guard
refuses an exact-duplicate `GET /.env` call outright while the first call dispatches normally --
see HISTORY's "A twenty-fourth pass" section for the full account.

A twenty-fifth pass was a dedicated simplification pass -- explicitly scoped by the user to "do not
alter or remove existing functionalities," unlike the sixteenth pass's mix of refactors and two
approved removals. Seven parallel read-only review agents covered every module plus `tests/`, each
forbidden from proposing anything but mechanically behavior-preserving changes. Applied: helper
extractions removing real duplication in `loop.py` (`_log_and_return`, a hoisted suffix template),
`http_tool.py` (`_matching_by_prefix`, `_ensure_leading_slash`), `toolbox.py` (`_call`), `config.py`
(`_resolve_path`, plus relocating a misplaced comment), `validation.py` (`_reject`, matching
`control_tool.py`'s existing convention), and `report.py` (`_TITLE`, `_join`); a `dedup.py`
simplification relying on Python's guaranteed dict insertion order instead of a redundant tracking
list, plus replacing a manual reduce loop with `max()`; a cosmetic `scope.py` fix (stray `f`-prefixes
with no placeholder); `cli.py`'s `--help` text now interpolates `config.py`'s `DEFAULT_*` constants
instead of duplicating them as separate hardcoded numbers; and consolidation of duplicated test
scaffolding (`StubToolkit`, `write_creds_file`, a repeated `propose_finding` test helper, a duplicate
test method name) into `tests/_helpers.py`. Several proposed changes were deliberately left alone,
each for a stated reason (risk of obscuring genuine asymmetry, comment-placement risk, regex-
composition risk, or too-trivial to justify an abstraction) -- see HISTORY's "A twenty-fifth pass"
section for the full list. Verified: pyflakes clean, the full 272-test suite unchanged and re-run
five times, and a live 15-step smoke test against the real crAPI stack with no regression on the hot
path (`loop.py`/`http_tool.py`/`toolbox.py`/`validation.py`).

A twenty-sixth pass split the overgrown README into a short `README.md` plus this project's full
pass-by-pass build log in `HISTORY.md` (pure documentation reorganization, no code change). The
**twenty-seventh and twenty-eighth passes then broke the blind-discovery ceiling this case study
spent so long documenting**: the twenty-seventh added `discover_api_endpoints` (reads the SPA's
bundled JavaScript for crAPI's real API surface), which finally reached the real vehicle/community
endpoints that ~14 runs of blind noun-guessing never had, taking the promoted run to three
findings; the twenty-eighth found fix #43, after a *first* fresh run produced a **fabricated**
cross-user dashboard BOLA -- the model quoted a real response body from one request as proof of a
claim about a *different* request (`GET /dashboard/{userId}`, which actually 404s), and the old
evidence-grounding check passed it because the quoted bytes existed in *some* captured body. Fix #43
binds evidence-grounding to the finding's *claimed* endpoint (`agent/dedup.py::endpoint_grounding_key`,
a `(method, path, text)` body cache, `HttpToolkit.bodies_for_endpoint`, and `validate_finding`/
`control_tool` threading the endpoint through). With it in place, a second fresh run -- every finding
verified live by `curl` before promotion -- produced the currently-promoted **five-finding**
deliverable (`.env` [off-list], community-posts PII [Challenge 4], a verbose Spring 404 [off-list,
weakest], a vehicle-location cross-user BOLA [Challenge 1], and a community post-detail PII leak
[Challenge 4]). So the ceiling held for *blind* discovery exactly as this case study concluded, but
a reading-the-frontend-bundle tool was the technique that moved past it. See `HISTORY.md`'s
twenty-sixth through twenty-eighth pass sections for the full account. 322 tests now, pyflakes clean.

## Bonuses implemented

All five from the optional/bonus features, each opt-out via a CLI flag where cost/risk warrants it:

- **Schema validation + retry**: `propose_finding` catches pydantic validation failures on
  malformed candidates and returns them as an ordinary rejection reason; malformed tool-call JSON
  is caught in the loop and fed back as an error observation. Both let the model self-correct on
  its next turn using the normal reason-act-observe cycle, rather than needing separate retry
  machinery.
- **Adversarial verifier pass**: `agent/verifier.py`, described above. On by default, `--no-verifier` to disable.
- **Semantic de-duplication**: `agent/dedup.py` -- normalizes ID-shaped path segments
  (`/reports/42` -> `/reports/{id}`) to group structurally-identical findings, then only merges
  within a group if either the `why_disclosure` reasoning text *or* the `evidence` text is actually
  similar (`difflib` ratio), so two genuinely different bugs on the same endpoint pattern are kept
  separate. (Fix #35, nineteenth pass: originally only `why_disclosure` was checked, which missed a
  real live case -- the same `.env` credential dump proposed twice with unrelated commentary, same
  evidence, different wording -- so `evidence` similarity was added as a second, independent merge
  signal.) On by default, `--no-dedup` to disable.
- **Token/cost accounting**: `agent/cost.py`, printed at the end of every run and folded into
  `report.md`. Reported as token/call counts, not a dollar estimate, since the provided endpoint is
  a flat-rate, free-tier resource with no disclosed per-token price.
- **Markdown report renderer**: `agent/report.py::render_full_report` -> `report.md`.

## What I'd do with another week

- **Close the actual observed gap first: help the model find real resource paths.** Given the case
  study above, this is the highest-value next step. A generic path-candidate crawler over response
  bodies was tried (see the `list_visited_endpoints` bullet above) and removed after evidence showed
  it never contributed to a finding across any live run -- it would only ever have surfaced paths
  already sitting in a response body anyway, not ones a real API discovery technique (e.g.
  spec-guessing, brute-forcing plausible resource nouns) would find, so it wasn't the highest-value
  next step even in principle. In rough priority order: ~~(a) try a stronger model against the same
  unchanged harness to isolate how much of the remaining gap (never finding crAPI's real
  vehicle/mechanic-report/order paths) is model capability vs. anything still latent in the agent~~
  -- done: a model-swap pass (HISTORY, "A model-swap experiment") pointed the unchanged harness at
  `nemotron-3-ultra-free` via OpenCode Zen and confirmed model capability *was* part of the gap --
  it generalized onto a second real vulnerability class (cross-account PII exposure via community
  posts) the original model never attempted in 14 runs, though it's on crAPI's public list rather
  than off it. Two more real bugs surfaced doing this (verifier burden-of-proof, HISTORY fix #20; an
  unhandled empty-`choices` API response, fix #21). Still open: (b) instrument *why* the model
  favors certain guesses (log the reasoning trace in full, not just visible content, when the API
  exposes it) to see whether the *residual* gap (the mechanic-report/order paths, now that the
  twenty-seventh pass's `discover_api_endpoints` reaches crAPI's real vehicle/community endpoints)
  is reachable with better prompting or is a harder sampling issue. (c) done, partially: the eighteenth pass added
  `list_id_candidates`, a single-hop, model-directed tool that extracts ID-shaped values (not
  path-shaped substrings, unlike the removed crawler) from a response the model already fetched, to
  make the BOLA/excessive-data-exposure pivot `RECON_TEXT` already instructs cheaper and more
  reliable than expecting the model to eyeball a nested `uuid`/`user_id` field itself. Whether the
  baseline model actually reaches for it in a real run is still unconfirmed -- see README's "Known
  limitations."
- **Broaden validation beyond substring grounding.** The evidence-grounding check is deliberately
  simple (a snippet must appear in a captured body); a week of runway would go toward a
  lightweight structural diff (e.g., actually parsing JSON and confirming the claimed field/value
  pair is present, rather than a text substring), which would be both stricter and more forgiving
  of reasonable paraphrasing.
- **Multi-run stability harness.** Automate running the agent N times against a freshly-recreated
  crAPI instance and diffing findings, to get real numbers for the project's own "Stability"
  metric instead of the qualitative argument in this document.
- ~~**Smarter truncation.**~~ Done: `agent/tools/http_tool.py::_truncate`/`_find_top_level_array`
  now keep whole JSON list items up to the character budget (dropping trailing items with an
  explicit count) instead of cutting mid-record, for a bare array or the first array-valued field
  of a top-level object (the common `{"posts": [...]}` list-endpoint shape). Verified against a
  synthetic body shaped like the real `/community/api/v2/community/posts/recent` response.
- **Expand the challenge-list classifier's recall** (`agent/challenge_reference.py`) with a larger,
  more carefully curated keyword set, still kept firmly out of the model's own context.
- **Chatbot-specific probing.** crAPI's LLM chatbot service (challenge 17: "extract credentials of
  another user using the chatbot") is a distinct interaction surface (natural-language, not plain
  REST) that the current `http_request` tool can technically reach but that a purpose-built
  "chat with the bot" tool would exercise far more effectively.
- **Persist and diff across runs** so repeat invocations against the same target don't
  rediscover (and re-spend budget on) the same already-known finding.

Ideas that would add real value but sit outside what *this project* asks for entirely (a
pluggable multi-vulnerability-class scope, CI/CD integration, human-in-the-loop review, ticket
filing, multi-target auth support, a model-agnostic reliability layer, adaptive budgeting) are
written up separately in [`FUTURE_IMPROVEMENTS.md`](FUTURE_IMPROVEMENTS.md), each with why it was
deliberately not built here and what it would offer if it were.
