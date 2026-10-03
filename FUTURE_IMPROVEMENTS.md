# Future Improvements & Improvement Opportunities

This file merges what were originally two separate documents, because on reflection the split was
itself a source of confusion rather than a useful distinction to keep permanently separate:

- **Part 1 -- in-scope opportunities.** Ideas that fit squarely within what this project asks
  for (a focused, single-target, single-vulnerability-class agent) but weren't built, from a fresh
  end-to-end review of the requirements against the implementation. Distinct from `DESIGN.md`'s
  "What I'd do with another week" section, which lists the highest-priority such items already
  triaged as worth doing first -- this is the fuller, untriaged list they were drawn from.
- **Part 2 -- out-of-scope ideas.** Ideas that would add real value but that were deliberately
  **not** built here because they fall outside what the project defines as its goal. Per the
  project's own instruction to "keep it simple" and "resist over-engineering," none of these are
  implemented -- this is a record of what was considered and consciously left out, and why it would
  matter in a broader context.
- **Part 3 -- lower-confidence / speculative ideas.** Flagged for completeness, not recommended.

Everything in Parts 1 and 3 was verified by reading the actual current source, not inferred from
documentation, as of the review that produced it (most recently updated 2026-07-27) -- file/line
references may drift as the code changes. Nothing here has been implemented (except where a status
note says otherwise); this is a findings/ideas document, for the user to triage and decide what (if
anything) to act on.

---

## Part 1: In-scope opportunities (not yet built)

### 1. Detection-technique coverage (cheap, in-scope additions)

These follow the same philosophy already used to justify existing prompt content
(`agent/prompts.py::RECON_TEXT`'s version-prefix and singular/plural guessing): generic,
framework-agnostic black-box techniques, not crAPI-specific "answer key" knowledge, so adding them
doesn't cross into the line the project has otherwise carefully avoided.

- **A generic path-candidate crawler -- already tried once, removed, revisit only with new
  evidence.** The idea: scan every response body for path-shaped substrings not yet requested, and
  surface the untried ones via `list_visited_endpoints` -- pure generic text/JSON-shape inspection,
  zero crAPI-specific knowledge, zero new tool-call surface. This was actually implemented
  (`agent/tools/http_tool.py::_extract_candidate_paths`) and then removed in the same session: none
  of the project's real findings came from a crawler-surfaced path (they came from direct
  guesses, ordinary cross-account exploration, and -- for the vehicle/community endpoints -- the
  later `discover_api_endpoints` JS-bundle reader), and across every live run conducted, the
  crawler's own output was never anything but trivial static-asset paths (favicon, bundled JS/CSS)
  that never fed into a finding.
  One live data point doesn't prove the *idea* is wrong -- a target that embeds more genuine
  same-origin API links inside its own JSON responses (unlike crAPI, which mostly returns plain
  data records, not links) could make this worth real value. **Bar for reintroducing it:** don't
  rebuild speculatively; only bring it back if a live run's own log shows the model being shown a
  response containing a real, actionable, not-yet-tried path that it then failed to follow up on --
  i.e. reintroduce it to close an observed gap, not a hypothetical one.
- **API-documentation/spec discovery -- substantially superseded.** crAPI's own onboarding docs
  (`crapi/docs/happy-path.md`) tell a *normal user* to browse the Swagger UI to discover available
  APIs -- this is a legitimate, publicly-documented convention, not a hidden bug location. A later
  pass added a generic "cheap sensitive/config path" step to `RECON_TEXT` (`.env`, `.git/config`,
  `actuator/env`, etc.), and live runs since then show the model independently generalizing this
  into trying `/swagger.json`, `/api-docs`, `/api-docs.json` on its own -- without those specific
  paths ever being named in the prompt. No dedicated bullet naming Swagger/OpenAPI paths appears
  needed at this point; only worth revisiting if a future run shows the model failing to try
  anything in this family on its own.
- **SPA JS-bundle endpoint extraction -- tried as a prompt nudge, removed, then built properly as a
  tool (done, twenty-seventh pass).** `RECON_TEXT` briefly told the model that a single-page app's
  own static JS bundle contains its API call sites and is worth reading when path-guessing stalls.
  One live run followed this correctly (fetched the real `/static/js/main.<hash>.js`), but
  `agent.tools.http_tool.MAX_BODY_CHARS` (4000) only ever surfaces the file's first ~4000
  characters -- for this target's webpack bundle, that was entirely vendor/license boilerplate (a
  minified lodash-style regex table), not application code. That was a correct observation with the
  wrong fix: a truncation limit sized for JSON API responses cannot show a meaningful fraction of a
  multi-MB static asset through `http_request`'s in-transcript body at all. The right form, built in
  the twenty-seventh pass, is `HttpToolkit.discover_api_endpoints`: it fetches the page's referenced
  bundles *tool-side* (up to `MAX_DISCOVERY_BYTES_PER_SCRIPT`, 16 MB each) and returns only the
  extracted API path literals, so the bundle never passes through the transcript truncation budget.
  This became the headline recall mechanism (it is how the promoted run reaches crAPI's real
  vehicle/community endpoints) -- see `HISTORY.md`'s twenty-seventh-pass section.
- **HTTP method enumeration.** `agent/toolbox.py`'s `http_request` tool hard-codes the `method`
  enum to `["GET", "POST", "PUT", "PATCH", "DELETE"]`. Neither `OPTIONS` nor `HEAD` is reachable by
  the model at all, even though `HttpToolkit.http_request` does nothing method-specific and
  `requests` would happily send them. `OPTIONS` is a near-zero-cost way to discover undocumented
  methods/routes via the `Allow:` header. This is a one-line schema change with real recall upside
  and directly matches the spec's "exposed internal or undocumented endpoints" scope bullet.
- **Common framework diagnostic/debug endpoints -- now implemented.** `RECON_TEXT`'s "suggested
  early ordering" now includes `/actuator/env` and `/debug` in its first-move sensitive-path list
  (`.env`, `.git/config`, `.git/HEAD`, `config.json`, `/actuator/env`, `/debug`). `/actuator/heapdump`
  and `/metrics` specifically are still not named; low priority unless a run shows the model missing
  them despite trying neighboring actuator paths.
- **Sentinel/special ID pivots -- substantially superseded.** The BOLA instruction added in the
  same pass ("retry the identical request... with a neighboring/different identifier") already
  covers this in spirit, and live runs show the model generalizing it into concrete attempts at
  `/user/me`, `/user/1`, `/user/9` unprompted by any literal example. No further bullet needed unless
  a future run shows a gap.
- **Header-leakage nudge.** `HttpToolkit.http_request` already returns full response headers and
  folds them into the groundable evidence cache, but nothing in the prompt tells the model to
  actually look at `Server:`/`X-Powered-By:`/CORS headers for version or internal-detail leakage --
  currently purely incidental whether the model notices. Cheap prompt addition, directly matches the
  "leaked in headers" scope bullet.
- **JWT payload decoding.** No tool lets the model reliably base64url-decode a JWT it receives
  (login tokens, or any token appearing in a response body). LLMs are unreliable at manual
  base64 decoding by eye (padding, URL-safe alphabet substitutions), so a JWT payload containing
  cleartext PII/role/internal-id fields is a class of disclosure the current toolset structurally
  can't surface well regardless of the model's skill or diligence. A small, deterministic
  `decode_jwt` tool (no network I/O, same "code-only, non-model-judgment" philosophy as
  `list_visited_endpoints`) would close this gap cheaply.
- **Structured response diffing between accounts.** The model currently must eyeball two raw
  (possibly-truncated) JSON blobs to spot one extra field -- exactly the subtlety the project's own
  case study says the model under-exploits. A `diff_responses` tool that takes two prior observations
  (or two accounts + one endpoint) and returns a structured field-level diff would make the
  cross-account excessive-data-exposure technique far more reliable than expecting an LLM to notice
  an extra key by eye. (Distinct from item 8 below, which is about *validating* a claim after the
  fact, not *surfacing* the lead during exploration. Also distinct from the eighteenth pass's
  `list_id_candidates`, which surfaces ID *values* to pivot to -- this item is about diffing two
  already-fetched *responses* field-by-field, a complementary but separate lever.)
- **Multipart/form-data support.** `http_request`'s `json_body` parameter only ever sends
  `json=json_body` -- file-upload endpoints (e.g. crAPI's profile picture upload) are entirely
  unreachable, closing off a plausible, unexplored disclosure surface (e.g. an upload response
  leaking an internal storage path).

### 2. Reliability / resilience

- **No retry on transient connection failure.** A `requests.RequestException` (timeout, connection
  reset) is returned as a plain observation dict, structurally identical in shape to a genuine
  target error -- the model has no way to distinguish "the network blipped" from "the target
  actually misbehaved," and could waste a turn or draw a wrong conclusion from a one-off network
  hiccup. A single automatic retry specifically on connection-level exceptions (never on HTTP error
  status codes, which are legitimate signal) would reduce this noise cheaply.
- **Binary/non-text response bodies aren't guarded.** `resp.text` is used unconditionally with no
  Content-Type check -- a binary body (e.g. an image) would produce garbage bytes that eat into the
  fixed `MAX_BODY_CHARS` truncation budget for zero signal. A cheap content-type guard
  (skip/summarize non-text bodies) would free up that budget for content that's actually
  inspectable.
- ~~**Windowed, not lifetime, repeat-block counter.**~~ Moot as of the sixteenth pass:
  `MAX_TOTAL_REPEAT_BLOCKS` itself was removed entirely (never observed firing in any run this
  project kept a log for; see README's "A sixteenth pass" section), so there is no lifetime counter
  left to make windowed. The per-call `repeat_blocked` refusal and the hard step/token budget remain
  the two real safety nets.
- **Step budget counts turns, not tool calls.** `BudgetTracker.record_step()` increments once per
  LLM turn regardless of how many tool calls that turn contained -- `DESIGN.md` itself notes the
  model "sometimes issues several [tool calls] in parallel," all of which are honored. `--max-steps`
  therefore doesn't tightly bound the number of actual HTTP requests, since a run could make
  meaningfully more real requests than its step count suggests. Worth at least documenting the
  semantics explicitly (or switching the cap to count dispatched tool calls) since "a hard cap on
  steps" is a literal project requirement (the cost/safety & logging goals).

### 3. Completion gate: request count without coverage

`agent/tools/control_tool.py`'s `MIN_REQUESTS_BEFORE_FINISH` (6) only counts raw requests made,
with no requirement that more than one endpoint family or more than one configured account has
actually been touched. A model could satisfy the floor by probing one endpoint a handful of times
and then finish, having never used a second account or a second service at all. A stronger gate --
e.g. requiring at least N distinct services visited, and (when >=2 accounts are configured) that
every account has been used in at least one `http_request` call -- would be a direct, code-only
lever on cross-account coverage. Same enforcement philosophy as the existing floor (deterministic,
no model judgment involved), just measuring coverage instead of raw count.

### 4. Verifier pass

- **The verifier never sees the raw captured response -- only the discovering model's own quoted
  text.** `agent/verifier.py::verify_finding` builds its candidate text purely from `Finding`
  fields (`title`/`endpoint`/`evidence`/`why_disclosure`); it has no access to
  `HttpToolkit.recent_bodies()` or the specific grounding snippet `agent/validation.py` matched
  against. This means the verifier reviews the discoverer's *summary* of the fact, not the fact
  itself -- it could be fooled by a technically-grounded-but-misleadingly-selective quote (e.g.
  quoting the caller's own email while implying it belongs to another user). Feeding the verifier
  the actual matched response snippet alongside the claim would make it a materially more
  independent check, consistent with the "no raw the-model-said-so" principle the mandatory
  validator already applies to evidence -- just currently missing from the verifier's own inputs.
- **Binary refuted/not-refuted verdict, no confidence adjustment.** `_VERDICT_TOOL`'s schema
  only has `refuted` + `reason` -- a verifier that's unconvinced-but-not-certain has no way to
  express that except accepting or rejecting outright. Letting the verifier downgrade (rather than
  only veto) a finding's stated `confidence` would give a human triager a more calibrated signal in
  `findings.json` for near-zero added complexity.

### 5. Reporting / output quality

- **`reproduction` and `endpoint` are unvalidated free text, unlike `evidence`.**
  `agent/validation.py::validate_finding` grounds the `evidence` string against
  `HttpToolkit.recent_bodies()` but never checks that `reproduction`'s steps, or the `endpoint`'s
  claimed method+path, correspond to anything actually in `HttpToolkit.history`. A model could
  submit an accurate, well-grounded `evidence` string alongside hallucinated or subtly wrong
  reproduction steps and it would still pass validation -- undermining the field a human triager
  most needs to trust. A cheap fix: cross-check that `endpoint`'s normalized method+path actually
  appears in `toolkit.history`. This directly extends the project's own "no raw the-model-said-so"
  principle to the two fields it currently doesn't cover.
- **No back-reference from a finding to the `run.log` step(s)/request(s) that produced it.**
  `agent.schemas.Finding` has no `source_steps` (or similar) field; a human triager wanting to
  verify a finding must re-read the whole log to find the relevant lines. Since
  `ControlToolkit.propose_finding` already has access to `step` and `HttpToolkit.history` at the
  moment a finding is accepted, this is a cheap addition with outsized debuggability/trust payoff.
- **Findings render in discovery order, not confidence order.** `agent/report.py::render_summary`
  / `render_full_report` iterate the findings list as-is. Sorting by confidence (high first) before
  rendering is a trivial change with real triage-speed value.
- **Rejection reasons aren't broken down by cause.** `agent/report.py::render_summary` tracks only
  an aggregate `rejected_count`; there's no breakdown by which gate rejected a candidate (scope
  keyword hit, evidence-grounding failure, weak reasoning, verifier veto). A cheap per-reason tally
  in `summary.md`/`report.md` would strengthen the honest-limitations narrative the project already
  leans on.
- **Dedup's ID-shape regex is narrower than real ID formats.** `agent/dedup.py::_ID_SEGMENT_RE`
  only recognizes plain numeric IDs, UUIDs, and 16+-char hex strings as "ID-shaped" -- a short
  opaque slug/token ID (base62 or mixed-case, under 16 chars) in a path won't be normalized, so
  structurally-identical findings on such an endpoint family could under-merge. Worth a regex
  broadening pass.

### 6. Bonus-feature depth: structured-output enforcement (the optional/bonus features)

`agent/llm_client.py` never uses guided/constrained decoding (`response_format` or a vLLM-specific
guided-JSON parameter) for tool-call arguments -- only `tool_choice="required"`. Bonus 1 (the optional/bonus features)
reads as "structured-output enforcement with schema validation + retry"; the current implementation
covers the "+ retry" half (pydantic validation inside `propose_finding`, with the ordinary
reason-act-observe loop as the retry mechanism) but not enforcement *at generation time*. Since the
configured endpoint is vLLM-hosted, it plausibly supports guided decoding for tool-call arguments,
which would make malformed tool-call JSON structurally impossible for this specific model/endpoint
-- a deeper lever than Part 2 item 7's general model-agnostic reliability layer (that's for *future,
untested* models); this is about squeezing more out of the one model/endpoint this project
actually targets.

### 7. A local self-scoring script for this project's own goal metrics (the evaluation metrics)

Self-scoring against the public crAPI challenge list is currently manual/informal, and no scoring
script is provided. `agent/challenge_reference.py` already exists as a code-only, model-invisible
post-hoc classifier populating each finding's `on_challenge_list` field -- but nothing aggregates it
into an actual scorecard. A small, separate, non-runtime script (e.g. `agent/scripts/self_score.py`,
never imported by the live agent loop, run only by the engineer before release) that ingests
`findings.json` and prints a rough recall/precision-against-public-list summary would directly
close this documented gap. Distinct from Part 2 item 2's multi-run/database persistence idea and
from `DESIGN.md`'s multi-run stability harness (neither scores against the public challenge list at
all).

### 8. Test coverage gaps

- **The verifier/dedup wiring inside `AgentLoop.run()` has thin end-to-end coverage.**
  `verify_finding`/`dedup_findings` are each unit-tested in isolation (`test_verifier.py`,
  `test_dedup_and_schemas.py`), but check whether the actual lines in `agent/loop.py` that call them
  after `_run_turns()` and populate `RunResult.verifier_rejections` have integration coverage
  confirming a verifier-rejected finding is actually dropped from `result.findings` in a real
  (stubbed) run, not just that `verify_finding` returns `False` in a vacuum.
- **Multi-tool-call turns.** `DESIGN.md` documents that the model sometimes issues several tool
  calls in one turn and all are honored -- worth confirming the `for tool_call in tool_calls:` loop
  body in `agent/loop.py` (one `budget.record_call`/repeat-check per call, one step increment for
  the whole turn) has a dedicated test for the >=2-calls-in-one-turn case specifically, not just
  single-call turns.

### Correctness gaps found and already fixed

A 2026-07-26 review found two items that were correctness gaps in already-shipped safeguards, not
"nice to have" ideas: the scope-gate keyword matcher was evadable by punctuation and inconsistent
across keywords (fixed in `agent/scope.py`), and `HttpToolkit` shared a single `requests.Session`
across all configured accounts, risking cross-account cookie leakage that would undermine the
harness's own cross-account comparison technique (fixed with per-account sessions). Both are
documented as fixes #10-11 in `HISTORY.md`'s "Status of the committed real run" numbered list and
`DESIGN.md`'s case study -- not repeated here to avoid the two documents drifting out of sync.

---

## Part 2: Out-of-scope ideas

Ideas that would add real value in a broader context but that were deliberately **not** built here
because they fall outside what this project defines as its goal (a focused, single-target,
single-vulnerability-class, small, focused agent).

### 1. Pluggable vulnerability-class scope (beyond information disclosure)

**What it is:** Generalize `agent.scope`'s in-scope/out-of-scope keyword lists and system-prompt
text into a swappable "scope profile" -- e.g. a profile for BOLA/IDOR hunting, one for
injection classes, one for business-logic abuse -- selected at startup rather than hard-coded to
information disclosure.

**Why it's out of scope here:** The spec is explicit and strict: "information disclosure
... and nothing else." Building a multi-class engine would directly contradict the "Scope
discipline" evaluation criterion, which rewards *narrowness*, not flexibility.

**Value it would add:** A real internal pentesting tool almost never wants only one bug class.
A profile system would let the same control loop, tool boundary, and validation pipeline
(the genuinely reusable 80% of this codebase) be pointed at different engagement types without
a rewrite -- turning this from a one-off script into an actual internal tool.

### 2. Multi-target / multi-run persistence and trend tracking

**What it is:** A small local database (SQLite is enough) recording every finding across every
run and every target, with de-duplication *across runs* (not just within one, which
`agent.dedup` already does), so re-running against the same target doesn't re-spend budget
rediscovering something already known, and so a security team could track "is this app getting
more or less leaky over time."

**Why it's out of scope here:** The project targets one real run producing one
`findings.json`; there is no notion of "the same target across time" in a project. Building
persistence for a single-shot deliverable would be exactly the over-engineering the spec
warns against.

**Value it would add:** This is close to table-stakes for any tool meant to run repeatedly
(e.g., in CI against a staging environment on every deploy) -- without it, every run re-pays the
full discovery cost of every previous run.

### 3. CI/CD integration ("break the build on new disclosure")

**What it is:** A mode where the agent runs against a staging deployment as part of a CI
pipeline, diffs its findings against the last known-good baseline (see item 2), and fails the
pipeline if a *new* information-disclosure finding appears.

**Why it's out of scope here:** No CI system, staging environment, or "baseline" concept exists
in this project -- there is one target, one run, one output.

**Value it would add:** This is the actual end-goal of "autonomous penetration-testing agents"
per the project's framing ("building autonomous penetration-testing agents") --
catching a regression automatically before it reaches production, rather than relying on a human
to notice.

### 4. Human-in-the-loop review before findings are finalized

**What it is:** An optional interactive step (a simple CLI prompt, or a small local web page)
where a human reviewer sees each accepted finding, with the agent's evidence and reasoning, and
can approve, edit, or discard it before `findings.json` is written -- essentially a human
"second opinion" alongside (or instead of) the automated bonus verifier pass.

**Why it's out of scope here:** The spec explicitly requires the agent to "run end-to-end
with a single command and no human intervention after launch" -- a review step would directly
violate that requirement.

**Value it would add:** In a real engagement, a security engineer's judgment is the actual
source of truth; an agent that surfaces well-evidenced candidates for a human to triage quickly
is often more valuable *in practice* than one that must be fully autonomous, since it can afford
to be more aggressive about what it proposes.

### 5. Ticket-filing integration (Jira / DefectDojo / GitHub Issues)

**What it is:** Once a finding is accepted, automatically file it as a ticket in whatever
vulnerability-tracking system a security team already uses, with the evidence and reproduction
steps pre-filled.

**Why it's out of scope here:** No ticketing system is part of this project's environment or
deliverables; `findings.json` and the Markdown report are the complete, self-contained output
the spec calls for.

**Value it would add:** Closes the gap between "the agent found something" and "an engineer is
actually going to fix it" -- the single biggest practical drop-off point for any automated
security tool that only produces a report nobody reads.

### 6. Broader authentication support (OAuth2, API keys, mTLS)

**What it is:** `agent.tools.http_tool.HttpToolkit` currently only understands crAPI's specific
login flow (`POST /identity/api/auth/login` returning a bearer JWT). A real multi-target tool
would need a pluggable auth strategy per target: OAuth2 client-credentials/authorization-code
flows, static API keys in custom headers, mutual TLS, cookie-based sessions, etc.

**Why it's out of scope here:** The project's target is fixed (crAPI), and crAPI has exactly
one auth mechanism. Building a generalized auth abstraction for targets that don't exist in this
project is speculative.

**Value it would add:** This is the single largest blocker to pointing this codebase at any
target other than crAPI -- without it, the tool boundary, loop, scope gate, and validation
pipeline (the genuinely target-agnostic 80% of the code) can't be reused elsewhere at all.

### 7. Model-agnostic reliability layer (beyond the one provided model)

**What it is:** The single most impactful fix found during development
(`tool_choice="required"`, see `DESIGN.md`'s case study) was specific to how the provided
`qwen36-35b-a3b` endpoint behaves with `tool_choice="auto"`. A more mature version of this agent
would detect *which* model it's talking to and apply model-specific reliability workarounds (or
probe for the failure mode directly at startup and adapt), rather than hard-coding one fix that
happens to suit one model.

**Why it's out of scope here:** The project uses one specific endpoint/model; building a
compatibility layer for models that were never tested against would be speculative engineering
with no way to validate it here.

**Value it would add:** Makes the harness durable against the fact that the hosted model can be swapped out from time to time -- right now, a sufficiently different
replacement model could reintroduce the exact failure mode this project spent most of its
debugging effort on.

### 8. Cost-aware adaptive budgeting

**What it is:** Instead of a fixed `--max-steps`/`--max-tokens` ceiling chosen up front, have the
agent request more budget dynamically based on signal quality -- e.g., extend the budget once if
it just found and validated a real finding (suggesting the target is fruitful), or cut a run
short if `SEQUENTIAL_FAILURE_STREAK_HINT_THRESHOLD`-style signals suggest the exploration has
truly dried up, rather than a human picking one number for every run regardless of how it's going.

**Why it's out of scope here:** The spec explicitly asks for a fixed, predictable "Hard cap
on steps and/or tokens" -- an adaptive budget is a reasonable evolution of that requirement, not
a literal reading of it, and risks looking like an attempt to dodge the cost-safety requirement
rather than satisfy it.

**Value it would add:** The development runs in `DESIGN.md`'s case study show budget spent very
unevenly relative to how productive a run turned out to be; a mechanism that spends more where
it's paying off (and cuts losses faster where it isn't) would improve the Recall/Stability
trade-off this project's own goals measures, without raising the *average* cost per run.

---

## Part 3: Lower-confidence / speculative ideas (flagging only, not recommending priority)

- **Replay-before-accept.** `agent/validation.py`'s grounding check confirms a finding's evidence
  appeared *somewhere* in a response already captured this run, but never re-executes the finding's
  own `reproduction` steps immediately before final acceptance to confirm the disclosure is stable
  (as opposed to a one-off -- a race condition, a resource mutated by another test action, a
  transient 500). Given `reproduction` is a required schema field and Finding Quality is 20% of the
  score (the evaluation criteria), a "replay once more before accepting" step would make that field something the
  agent verified itself rather than merely asserted. Adjacent to, but distinct from, `DESIGN.md`'s
  "broaden validation beyond substring grounding" item (that's about match strictness at one point
  in time; this is about stability across a second execution).
- **Binary-artifact metadata inspection.** Section 2's scope bullet mentions secrets/tokens leaked
  "in responses, headers, or artifacts" -- the word "artifacts" isn't fully exploited. crAPI's
  challenge list references downloadable binary artifacts (e.g. a QR code for order returns); no
  current tool inspects downloadable files for embedded metadata (EXIF, PDF metadata, etc.) as a
  distinct disclosure surface. Flagged as lower-confidence and higher-effort than everything above
  -- crAPI's actual artifact-serving behavior wasn't verified during this review, deliberately, to
  avoid this document itself becoming crAPI-specific answer-key content.

---

## Summary judgment

Part 1's items 1-3 are the most direct, cheapest levers on discovering more of crAPI's actual
resource surface and exercising cross-account comparison more thoroughly. The project's own case
study no longer attributes the remaining recall gap purely to model capability: a stronger model
(the tenth-pass model-swap experiment) did generalize onto a second real resource path via genuine
cross-account comparison, so at least part of the original gap *was* model capability, not a harness
bug -- but even that model never found crAPI's vehicle/mechanic-report/order paths either, so real
headroom remains on both fronts. Items 4-8 are real quality/trust/coverage improvements but
lower-leverage against that specific gap. Part 2 is deliberately out of scope for this project's
own definition of the task, kept as a record of what a broader tool built on this codebase would
need. Part 3 is deliberately unprioritized speculation, included for completeness.
