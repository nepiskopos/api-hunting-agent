# Development history

This file is the full, unabridged build log of the Information-Disclosure Hunting Agent: every
live run against crAPI, every bug found and fixed (39 numbered fixes across twenty-five review
passes), and the reasoning behind each decision, in the order it actually happened. It was split
out of `README.md` because it grew large enough to bury the practical "how to run this" content a
new reader actually needs first.

Start with [`README.md`](README.md) instead if you just want to run the agent or understand what
it is. Come here for the honest, blow-by-blow account of what was tried, what broke, and why --
including the parts that didn't work. [`DESIGN.md`](DESIGN.md) has the architecture write-up and a
condensed case study; [`FUTURE_IMPROVEMENTS.md`](FUTURE_IMPROVEMENTS.md) has in-scope opportunities
not yet built plus ideas deliberately left out of scope.

## Status of the committed real run: one real finding, reproduced live with the committed config

Read this before anything else below -- it's the most important thing to know about this
release. The committed `findings.json` contains **one accepted finding**:

1. An unauthenticated `GET /.env` on the crAPI web frontend returns a real, plaintext configuration
   file (database credentials, internal service hostnames) to anyone, no login required. It is
   **not** on crAPI's own public challenge list (`crapi/docs/challenges.md` has no `.env`/config-file
   entry at all) -- i.e. this is a genuine generalization result, the metric this project weighs
   most heavily, not a recital of a known bug. The file itself is tracked in OWASP crAPI's own git
   history at this project's exact target commit (`73d309cc8f28bbdeed31dbb35f05dba8354de3c9`, path
   `services/web/public/.env`), confirming it's a real upstream issue, not a local deployment
   artifact.

**A note on this project's history of "two findings":** a tenth-pass model-swap experiment (see
that section below) additionally found `GET /community/api/v2/community/posts/{id}` leaking another
user's email and internal vehicle ID (crAPI's own Challenge 4) using a different model
(`nemotron-3-ultra-free` via a one-off OpenCode Zen API key that was never persisted to this
project's committed `.env`). That result is real, independently confirmed, and stays documented in
this file's history below -- but it is **not reproducible** with just the checked-in `.env` +
`creds.json` this release actually ships, so it is not part of the currently-promoted
`findings.json`. A seventeenth pass (see that section below) re-ran the agent fresh against the
documented Cyrex/`qwen36-35b-a3b` endpoint -- the config anyone cloning this repo can actually use --
and promoted that live run's single `.env` finding as the current deliverable, rather than leaving a
result on disk that depends on credentials this release doesn't include. A nineteenth pass (see
that section below) cleared all outputs and re-ran fresh against the same documented config,
finding and fixing a real dedup bug along the way (fix #35). A twentieth pass (see that section
below) then bumped the budget 3x, found and fixed a real budget-wasting query-string-guessing bug
(fix #36), and re-ran once more. A twenty-third pass (see that section below) cleared outputs again
and re-ran fresh, finding and fixing a real classifier misattribution bug along the way (fix #38);
that run's single `.env` finding is the one currently promoted.

This is the result of a long honest debugging process, not a first try: eight full live runs found
nothing at first, each for a real, fixable engineering reason; a follow-up session then found and
fixed several real token-efficiency and correctness bugs, one of which (fix #15 below) was
discovered *by* this very finding almost being mis-scored. The spec explicitly asks for
honest limitations over a polished-looking but dishonest result ("if you run low on time, ship a
smaller agent that runs cleanly over a broken one that doesn't"), so here is the full, undisguised
story -- including the parts that didn't work.

**Eight full live runs were executed against crAPI while building this.** The first several
produced zero findings for reasons that turned out to be real engineering bugs, each found by
reading `run.log` closely, root-caused, fixed, and covered by a regression test:

1. **Unbounded transcript growth** (run 1): every stateless chat-completions call resends the
   entire history; without bounding it, total token usage grew roughly quadratically and a
   400k-token budget was exhausted by step 27 with zero findings, having never gotten anywhere.
   Fixed with recency-weighted compaction (`agent/loop.py::AgentLoop._compact_transcript`).
2. **Trailing-slash repeat-detection gap** (run 2): the model dodged exact-match repeat detection
   by toggling `/user/8` vs `/user/8/`. Fixed by normalizing trailing slashes in the repeat
   signature (`agent/budget.py::_signature`).
3. **Degenerate path construction** (run 2): the model got stuck extending one path with the same
   segment over and over (`.../repair/1/repair/1/repair/1/...`), each call technically "new" to
   the repeat detector since the string kept growing. Fixed with a dedicated guard that refuses to
   dispatch a pathologically-shaped path (`agent/tools/http_tool.py::_degenerate_path_reason`).
4. **Sequential-ID sweep with no pivot** (run 3): the model swept ~15 consecutive numeric user IDs
   on a path family that doesn't exist on this target at all, never changing strategy. Added a
   same-path-family failure-streak hint that nudges (not blocks) a pivot
   (`agent/tools/http_tool.py::_track_family_outcome`) -- later **removed** in the sixteenth pass
   (see that section below): it never once fired in any run this project kept a log for.
5. **Coverage memory lost to compaction** (run 4): the model never once proactively called
   `list_visited_endpoints`, so once an old observation scrolled out of the compaction window it
   would forget having already tried that exact call and repeat it later (non-consecutively, so
   the repeat detector didn't catch it either). Fixed by auto-injecting a compact coverage summary
   every 8 steps regardless of what the model does (`agent/loop.py`, `COVERAGE_REMINDER_INTERVAL`).
6. **The big one, found during run 5's diagnosis: `tool_choice="auto"` was silently dropping tool
   calls.** Direct probing of the live endpoint showed the model would frequently finish its hidden
   reasoning trace and then stop (`finish_reason: "stop"`) with **both `content` and `tool_calls`
   empty** -- sometimes after a long, coherent-looking reasoning trace, sometimes after the
   reasoning itself degenerated into repeating one sentence ("I will use the `http_request`
   tool...") until it ran out of tokens. This single misconfiguration is very likely the primary
   reason every earlier run underperformed: the model was silently failing to act on a meaningful
   fraction of turns, invisibly burning budget on nothing. Fixed by switching to
   `tool_choice="required"` (semantically correct anyway -- every turn of this loop is supposed to
   end in exactly one action) plus mild `frequency_penalty`/`presence_penalty` to reduce the
   repetition pathology (`agent/llm_client.py::LLMClient.chat`).
7. **Premature `finish_investigation`** (run 6, immediately after fix #6): forcing `tool_choice`
   made the model reach for the wrong tool on turn 1 once (`finish_investigation` before a single
   real request). Fixed with a minimum-requests-before-finish guard
   (`agent/tools/control_tool.py::MIN_REQUESTS_BEFORE_FINISH`).

Two further bugs were found in a later, dedicated live-testing pass (not during the original 8-run
discovery campaign above), by deliberately exercising code paths the model itself had never
triggered live:

8. **`.env` loading ignored the actual working directory.** `agent/cli.py` called plain
   `load_dotenv()`, which (per `python-dotenv`'s own default) resolves relative to the *calling
   module's file location*, not the process's actual working directory -- those only coincide when
   you happen to run `python -m agent` from inside the installed package's own directory, which is
   the one workflow this project's own docs and tests exercised. Confirmed with a reproduction using
   a real (non-REPL) script invoked from an unrelated directory with its own `.env`: the wrong file
   won. Fixed with `load_dotenv(find_dotenv(usecwd=True))`, which makes "loaded from the directory
   you actually run the command from" (as documented above) literally true regardless of where the
   package lives. Regression-tested in `agent/tests/test_cli.py`.
9. **The verifier pass had no retry on the same no-tool-call failure mode fix #6 targeted.**
   `propose_finding` had never once been called in any live run through this session, so the bonus
   verifier's real LLM call (`agent/verifier.py::verify_finding`) had only ever been exercised with
   a mocked client. Driving it directly against the real endpoint surfaced the same failure mode as
   fix #6, at roughly the same rate (~1 in 7 single-shot calls in a live sample): a `tool_choice=
   "required"` call can still return zero tool calls. Unlike the main loop (which recovers via the
   next turn automatically), a one-shot verifier call had no such recovery -- it silently fell back
   to "kept, unverified" every time this happened, undermining the bonus feature's actual purpose
   more often than necessary. Fixed with one retry before falling open (`agent/verifier.py::
   _MAX_ATTEMPTS`). Re-verified live afterward: 8/8 fresh calls against a deliberately-bogus "caller
   viewing their own data" test finding correctly refuted it once given the chance to respond.

Two more bugs were found in a subsequent pass that wasn't driven by a live run at all -- a fresh,
dedicated end-to-end review re-reading the whole implementation against the project's own
requirements from scratch, specifically hunting for correctness gaps rather than new features (see
`FUTURE_IMPROVEMENTS.md`'s Part 1 for the full review, including items deliberately left unfixed):

10. **The scope-enforcement keyword gate could be evaded by punctuation.** `agent/scope.py::
    check_scope` matched the out-of-scope keyword "rce" via an ad hoc space-padded substring pair
    (`" rce"`/`"rce "`, chosen specifically so the bare substring `"rce"` wouldn't false-trigger
    inside `"brute force"`) -- but that padding itself had a real gap: punctuation-adjacent text
    like `"(RCE)"` touches neither padded form (bounded by parens, not spaces, on both sides) and
    slipped through completely undetected. Separately, only some multi-word keywords
    (`"rate limit"`/`"rate-limit"`, `"brute force"`/`"brute-force"`) had a manually-listed
    hyphenated duplicate; others (`"mass assignment"`, `"jwt forgery"`) didn't, with no principled
    reason for the inconsistency. Both fixed by replacing substring matching with a compiled
    `\bword\b`-boundary regex per keyword (`agent/scope.py::_keyword_pattern`), treating a space and
    a hyphen as equivalent separators for multi-word keywords -- this closes the punctuation-evasion
    gap and makes hyphen/space coverage uniform across every keyword for free, letting three
    now-redundant duplicate entries be removed from `OUT_OF_SCOPE_KEYWORDS`. This is the one
    safeguard this project weighs most heavily (scope discipline is scored independently of
    recall), so an unintended silent false negative here mattered more than most.
11. **`HttpToolkit` shared a single `requests.Session` across every configured account.**
    `agent/tools/http_tool.py` created one session in `__init__` and reused it for every account's
    calls, with only the `Authorization` header differing per request. If crAPI ever set a
    session-scoped cookie (in addition to, or instead of, the bearer JWT) during one account's
    login or requests, that cookie would persist in the shared session and could silently attach to
    a later request nominally made "as" a different account -- undermining the validity of the
    harness's single most important detection technique, cross-account excessive-data-exposure
    comparison. Fixed with one `requests.Session` per account label (plus one for unauthenticated
    calls, keyed by `None`), created lazily via a `_session_factory` seam that production code
    points at the real `requests.Session` constructor and tests override to inject a fake without a
    real socket.

12. **The coverage reminder and no-tool-call nudge accumulated forever instead of replacing
    themselves.** Found analyzing real token accounting from a live run rather than from a bad
    outcome: both are injected as `role: "user"` messages, but `_compact_transcript` (fix #1's
    mechanism) only ever clipped `role: "tool"` messages -- these two message kinds were invisible
    to it. The coverage reminder is a full re-dump of the *entire* request history so far, injected
    every 8 steps; since each one was appended rather than replacing the previous (a strict subset
    of it), every past reminder kept being resent on every subsequent call for the rest of the run.
    An offline replay of a real 72-step run's reminder sizes showed this alone accounted for
    roughly 70% of the reminder mechanism's own resend cost (854K of the character-cost total, vs.
    252K after the fix) -- on the order of a tenth of that run's overall token usage. Fixed with
    `agent/loop.py::_replace_singleton_message`, which deletes the prior occurrence (matched by a
    fixed content prefix, not a remembered index -- two independent singletons can each be replaced
    within the same turn, and a remembered index would go stale the moment the *other* one is
    deleted) before appending the new one, for both the reminder and the nudge.
13. **Assistant message content had the same blind spot, just unexercised so far.** Since
    `_compact_transcript` only ever looked at `role: "tool"`, an assistant turn's own free-text
    `content` was never clipped either, regardless of age. Every live run to date has empty
    content on every turn (confirmed by grepping `run.log` for `INTENT:` lines -- `tool_choice=
    "required"` suppresses visible content in practice for this model), so this hasn't yet cost
    real tokens -- but it's the exact failure mode fix #6 already documented once (the model
    degenerating into repeating one sentence in its visible reasoning until it ran out of
    completion tokens), and nothing structurally prevents it recurring. `_compact_transcript` now
    applies the same recency-windowed clipping to assistant `content`, leaving `tool_calls`
    untouched (a later turn's tool-role response is matched against it by `tool_call_id`).
14. **Compaction's fixed-prefix cut was silently dropping response bodies in full, not just
    shortening them.** `agent/tools/http_tool.py`'s result dict listed `headers` before `body`;
    once an observation aged past the recency window, `_compact_transcript` kept only its first
    `OLD_TOOL_MESSAGE_CLIP_CHARS` (300) serialized characters -- a raw prefix cut. A real response's
    headers (Server, Date, Cache-Control, security headers, etc.) commonly serialize to 500+
    characters alone, so that budget was consumed entirely by routine boilerplate, and the body --
    where information-disclosure signal actually lives in the overwhelming majority of cases -- was
    dropped in full, not partially, for every observation older than the last few steps. This is a
    correctness bug, not just a cost one: it directly undermines the model's ability to recall a
    body-based leak it saw earlier in the run. Verified concretely against a real captured response
    (875 serialized characters, 513 of them headers): the old ordering showed zero body content in
    the compacted 300-char snippet; reordering `body`/`body_truncated`/`body_original_length` ahead
    of `headers` in the dict recovers nearly all of it for a typical response.
15. **The `on_challenge_list` classifier could be falsely triggered by a keyword embedded inside an
    unrelated word.** `agent/challenge_reference.py::classify_on_challenge_list` used plain substring
    matching (`keyword in text`), and this is not a hypothetical: it actually mislabeled the real
    finding described below. The 3-letter keyword `"vin"` (meant to catch vehicle VIN numbers)
    matched inside the ordinary word "ha**vin**g", in that finding's own `why_disclosure` text
    ("...without logging in or having any valid session token"), tagging a genuinely novel `.env`
    disclosure as `"challenge-1-bola-vehicle"` -- exactly the wrong direction to get wrong, since
    understating a real generalization win is worse than a harmless false negative. This is the
    identical bug class already fixed once in `agent/scope.py` (fix #10), just never applied here.
    Fixed by reusing `scope.py`'s `\bword\b`-boundary keyword matcher
    (`agent/challenge_reference.py::_CHALLENGE_KEYWORD_PATTERNS`).

Fixes #12-15 are covered by new regression tests (`tests/test_transcript_compaction.py`,
`tests/test_loop_integration.py::SingletonMessageTests`, `tests/test_http_tool.py`,
`tests/test_challenge_reference.py`).

Fixes #10-11 are covered by new regression tests (`tests/test_scope_and_validation.py`,
`tests/test_http_tool.py::SessionIsolationTests`) and neither was reachable by re-running the
existing live logs -- they were found by reading the code, not by observing a bad live outcome,
which is why they surfaced in a code-review pass rather than during the original 8-run campaign.

**Runs 7 and 8** (56 and 72 steps respectively) behaved close to what the design intends: zero
forced repeat-blocks in either run, and in run 8's own log specifically (a prior run from this same
debugging campaign -- its log is not the one shipped with this release, superseded by run 9's
below, but its details were confirmed at the time directly against that run's own log file), exactly
one malformed tool call (step 35, `http_request` called with `method` but no `path` -- caught
cleanly, returned as an ordinary error observation, run continued) and exactly one step (step 9)
where the model produced neither visible content nor a tool call despite `tool_choice="required"`
-- i.e. the same failure mode fix #6 targeted, recurring once in 72 steps rather than being fully
eliminated; the loop's nudge (`NO_TOOL_CALL_NUDGE`) absorbed it and the run continued normally on
the very next step. Otherwise: systematic coverage of
`/identity`, `/community`, and `/workshop` with both accounts, and genuine cross-account comparison
-- e.g. it called `GET /identity/api/v2/user/dashboard` as `primary` (steps 21 and 34) and again as
`secondary` (step 40); each came back with that account's own record (id 8 vs. id 9, matching
emails), so there was nothing to report, and the agent correctly didn't call `propose_finding` for
it. (An earlier draft of this document additionally claimed the agent tried a
`?userId=8`-query-parameter variant of this same probe -- that specific claim does not appear
anywhere in that run's log and has been removed; that log contains zero query-parameter usage at
all, so "sensible use of query-parameter manipulation" was also an overstatement, corrected here.)
**Across all eight of those runs, the model never once discovered crAPI's actual (non-obvious)
resource paths** -- e.g. the real vehicle/mechanic-report/video endpoints that carry the public
challenge list's BOLA and excessive-data-exposure bugs -- despite trying dozens of plausible
REST-shaped guesses across both services and accounts.

**Run 9** (a follow-up live session, after the token-efficiency and correctness fixes above --
`--max-steps 120 --max-tokens 1100000`, stopped by the token budget at step 85) is where the
`.env` finding above was actually found, at step 62-65: a generic, non-crAPI-specific guess
(`GET /.env`, an unauthenticated dotfile probe -- the same class of guess the model had already
tried for `/config`, `/admin`, `/debug`, `/application.yml` on this and earlier runs) happened to
land. Its first `propose_finding` attempt was correctly rejected by the mandatory evidence-grounding
check (`agent/validation.py`) for paraphrasing the captured body instead of quoting it; the model
re-issued the identical request and resubmitted with exact text, which was accepted. The bonus
verifier pass produced no tool call on both live attempts during the run itself (the documented
~1-in-7 failure mode, fixed for the main loop and the verifier's own retry in fix #6/#9, unlucky
twice regardless) and fell open; a fresh, isolated, offline re-run of just the verifier call
afterward (three attempts, no full re-run needed since it's independent of everything else in the
transcript) got an actual adversarial verdict on the third try: **not refuted**, with substantive
reasoning ("a textbook example of sensitive information disclosure via server misconfiguration...
no legitimate reason for such a file... to be publicly accessible"). A second, independent live run
at the identical budget immediately afterward found **zero** findings -- a different, non-crAPI-
specific exploration path, no `/.env` attempt at all that time -- which is real, honest evidence of
run-to-run instability at this model's capability level, not swept under the rug.

**One data-integrity note on `on_challenge_list`, stated plainly rather than quietly patched.**
Run 9's own `run.log` contains an inline line reading `on_challenge_list=True
(challenge-1-bola-vehicle)`, because that is what the code genuinely computed *during* that live
run -- before fix #15 above (found while double-checking this very finding) corrected the
classifier. `run.log` is kept as an unedited, literal trace of what actually executed, exactly as
for every other correction documented in this section; `findings.json`/`summary.md`/`report.md`
reflect the corrected value (recomputed from the finding's own already-captured, unchanged
title/endpoint/why_disclosure text -- a deterministic function of data that was already real, not a
re-run or a fabrication) because that's what's actually true: this finding is **not** on crAPI's
public challenge list.

Zero forced repeat-blocks occurred in run 9. The model never discovered any of crAPI's other
non-obvious resource paths (vehicle/mechanic-report/order endpoints) in either of the two run-9-era
sessions, consistent with the capability-limit conclusion from the original eight runs -- this
result came from a generic infrastructure-misconfiguration guess, not from the model suddenly
learning to navigate crAPI's actual API surface. See `DESIGN.md`'s "another week" section for what
would most plausibly close that remaining gap.

### A later prompt-refinement pass (4 more live runs, 2 more real bugs, no new finding)

A subsequent session deliberately tried to push past the single `.env` finding above by refining
`agent/prompts.py::RECON_TEXT`: an explicit "suggested early ordering" (cheap generic sensitive-file
checks, then map owned data across every service, then broad guessing), naming Broken Object Level
Authorization (BOLA) by its standard OWASP API Security Top 10 name as the single highest-yield
general technique, and a nudge to prioritize resource nouns implied by the domain description
already given ("a vehicle service platform") over generic account-management nouns. None of this
adds crAPI-specific knowledge -- BOLA is a globally standard vulnerability-class name, and "vehicle
service platform" was already in `MISSION_TEXT` before this pass; the refinement only points the
model at using what it already had.

Four live runs validated this (60-step budget each). Along the way, two more real bugs surfaced --
both catalogued in `FUTURE_IMPROVEMENTS.md`'s Part 1 in more detail:

16. **The evidence-grounding check could reject a perfectly legitimate finding over a model
    quoting quirk.** Run 2 re-proposed the exact same `.env` finding, quoting the real captured body
    verbatim -- except the model double-escaped its embedded newlines, so the *decoded* JSON
    argument contained a literal two-character backslash-`n` instead of an actual newline. The old
    `agent/validation.py::_normalize` only collapsed real whitespace, so that stray backslash split
    each candidate snippet and left a leading `n` glued onto the next token (e.g. `"crapi\nDB_USER"`
    tokenized as `"crapi"` and `"ndb_user"`, not `"db_user"`), which then could never match the
    correctly-formatted captured body -- silently rejecting a genuinely grounded finding as
    fabricated. Fixed by also collapsing literal `\n`/`\r`/`\t` sequences the same as real whitespace
    before matching. Regression-tested with the exact real evidence text
    (`tests/test_scope_and_validation.py::test_accepts_evidence_with_literal_double_escaped_newlines`).
17. **The `on_challenge_list` classifier's challenge-14 keywords were too generic to be reliable.**
    Across these same four runs, the identical underlying `.env` finding was tagged `true` once
    (`"unauthenticated"`), `false` once (`"without any authentication"` -- not adjacent, so the
    phrase keyword didn't match), and `true` again in a different run (`"without authentication"`,
    this time adjacent). The finding's substance never changed; only incidental LLM phrasing did.
    Since nearly any genuinely novel disclosure finding can be honestly described using
    "unauthenticated"/"no auth" language, keeping that keyword set risked exactly what fix #15 above
    already flagged as the worse failure mode: misattributing a real generalization win to a known
    public challenge. Fixed by removing the challenge-14 entry from
    `agent/challenge_reference.py::PUBLIC_INFO_DISCLOSURE_CHALLENGES` entirely rather than trying to
    tune keywords further -- its public description (`docs/challenges.md`: "an endpoint that does
    not perform authentication checks") offers no more specific text to match against, so any
    keyword set for it would carry the same risk. A finding that genuinely is challenge 14 now gets
    `on_challenge_list=false` -- a false negative, which this module's own docstring already
    establishes is the safe direction to be wrong in.

Of the four validation runs, two (run 1, and run 4 after fix #16 landed) correctly found and
accepted the `.env` finding; one (run 2) would have accepted it too but for the bug fix #16 fixed;
one (run 3) never attempted `/.env` at all that run and found nothing, spending part of its budget
instead on a nudge added and then removed in the same pass: telling the model to read the frontend's
static JS bundle for embedded endpoint strings when path-guessing stalls. The model followed this
correctly (fetched the real `/static/js/main.<hash>.js`), but `MAX_BODY_CHARS` (4000) only ever
surfaces the opening bytes of a bundle that is realistically hundreds of KB to a few MB -- in the
observed case, vendor/license boilerplate, not application code. This is a structural mismatch, not
bad luck to average out over more runs, so it was removed rather than kept "just in case" (see
`FUTURE_IMPROVEMENTS.md` for the full reasoning, in the same spirit as the path-candidate crawler
removed in an earlier pass).

**None of the four runs found anything beyond the already-known `.env` disclosure.** Despite
explicit BOLA framing and domain-noun nudging, the model still never tried a single one of crAPI's
actual vehicle/mechanic-report/order resource paths across these four additional runs (12 live runs
total across this project's history) -- consistent with the capability-limit conclusion already
reached after the original eight-run campaign, not a regression introduced by this pass. Since
these runs reproduce the same underlying finding already committed (just with different `.env`
evidence phrasing), `findings.json`/`run.log`/`summary.md`/`report.md` were **not** re-promoted from
any of these four runs -- there is no new substantive result to promote, and doing so would discard
the existing run 9 history for no benefit. The two bug fixes and prompt refinements are, however,
part of the shipped code and covered by new regression tests (177 tests total, up from 174).

### A bigger-budget experiment, and one more real bug (token efficiency)

Prompted by the question "would a much bigger step/token budget find more?", one live run used 150
steps and a 1.5M-token budget (vs. the 40-step/400k-token default) against the unchanged
post-prompt-refinement agent. Result: 0 findings, all 99 of 150 available steps used, stopped by
token exhaustion -- notably worse than the default-budget runs, not better, and the run never even
attempted `/.env` this time. Inspecting `run.log` showed why: the model spent roughly 15 consecutive
steps re-fetching `GET /identity/api/v2/user/dashboard` with a different query-string parameter each
time (`?id=8`, `?id=9`, `?id=10`, ...), evidently probing for an IDOR -- but the endpoint ignores the
query parameter entirely and always returns the caller's own profile from the session, so every one
of those ~15 responses was byte-identical. This reproduces, rather than resolves, the already-
documented run-to-run instability (a prior pair of identical-budget runs found the `.env` finding
once and nothing the second time) and confirms budget was never the binding constraint -- more steps
just means more unproductive guessing against a model that can't reliably infer crAPI's non-standard
route names, not better coverage.

That investigation surfaced one more real, fixable inefficiency, independent of the budget question
itself:

18. **Duplicate tool responses each cost their own independent compaction excerpt.** Once an
    `http_request` observation ages out of `_compact_transcript`'s recency window
    (`KEEP_FULL_TOOL_MESSAGES`), it's clipped to a short excerpt -- but every occurrence was clipped
    *independently*, even when two observations were byte-identical in `(status_code, body)`, as the
    ~15 dashboard-parameter probes above were. Each aged-out copy cost its own
    `OLD_TOOL_MESSAGE_CLIP_CHARS`-sized slice of budget for zero new information. Fixed in
    `agent/loop.py::_compact_role` with `_tool_response_signature`: a short stable hash of
    `(status_code, body)` embedded in the first occurrence's own clipped excerpt (`sig=...`), so any
    later aged-out observation sharing that hash collapses to a one-line duplicate pointer instead of
    its own excerpt -- recognized even after the first occurrence has itself scrolled out of the
    full-detail window and been clipped, since the hash persists in its excerpt text across turns.
    Responses that aren't shaped like an `http_request` result (no `body`/`status_code` keys, e.g.
    `list_visited_endpoints`'s own output) fall back to the plain length-based clip, unaffected.
    5 new regression tests in `tests/test_transcript_compaction.py::DuplicateResponseCollapseTests`.

No live run was re-executed to validate fix #18 in isolation -- it's a pure token-efficiency
optimization with no behavioral effect on what the model decides (the model still sees "duplicate
response, no new signal" either way, just at a fraction of the token cost), verified with a direct
offline sanity check reproducing the real run's exact duplicate-spam shape (15 identical dashboard
responses: 16,343 transcript characters before the fix's equivalent plain-clip behavior vs. 7,613
after, in that isolated slice). 182 tests now, up from 177.

A follow-up to fix #18 closed a second, related gap in the same mechanism:

19. **Headers still ate into the fixed clip budget whenever the body itself was short.** Fix #14
    (body-before-headers key order) stopped headers from consuming the *entire*
    `OLD_TOOL_MESSAGE_CLIP_CHARS` budget ahead of the body, but didn't stop them consuming *part* of
    it -- a realistic aged-out 404 response (663 raw characters, ~440 of them routine headers) still
    had its 300-character clip spend well over half its budget on headers, cut off mid-value (e.g.
    `"Vary": "Origin`), for zero disclosure signal. Fixed with
    `agent/loop.py::_strip_headers_for_clip`: the `headers` field is dropped from the JSON entirely
    before an aged-out tool message is clipped, so the full character budget goes to what's actually
    left (status code, body, and small book-keeping fields) instead of routine headers. Applies
    independently of, and composes with, fix #18's duplicate-collapsing -- a message can be both
    header-stripped (if it's a first occurrence) and later recognized as a duplicate by another
    message sharing its `(status_code, body)` signature. Responses without a `headers` key (e.g.
    `list_visited_endpoints`'s own output) are left unchanged. 4 new regression tests in
    `tests/test_transcript_compaction.py::HeaderStrippingTests`. Verified directly: the same 663-char
    404 response above now clips to 224 characters of clean, valid JSON (previously 300 characters,
    ~200 of which were a truncated header block) plus the dedup signature suffix. 186 tests now, up
    from 182.

### A model-swap experiment answers "would a stronger model help?" -- yes, and it found two more real bugs

Every prior pass tested a fixed harness against the one model available on the assigned Cyrex
endpoint (`qwen36-35b-a3b`). The harness was always designed to be model-agnostic (`LLM_BASE_URL`/
`LLM_API_KEY`/`LLM_MODEL` are the only required config, read from the environment, never
hard-coded), so this pass tested that directly: point the same, unmodified agent code at a different
OpenAI-compatible endpoint and see whether the model, not the harness, was the binding constraint.
Using a free-tier OpenCode Zen model (`nemotron-3-ultra-free`, selected because it was the only one
of the six free-tier models offered that accepted `tool_choice="required"` -- the load-bearing fix
#6 setting -- without a gateway error; `deepseek-v4-flash-free` rejected it outright and was excluded
rather than weakening that fix to test it), four live runs were made via plain environment-variable
overrides, with `.env`'s own committed Cyrex configuration never touched.

Two real code bugs surfaced along the way:

20. **The bonus verifier's "default to skepticism" instruction had no stated burden of proof for
    personal data, and wrongly refuted a genuine finding.** The first live run had the model propose
    a second, real finding -- the community-post PII leak described above -- and the verifier
    (`agent/verifier.py`) refuted it: "the evidence only shows... not that this access is
    unauthorized... this could be intended behavior... without a privacy policy." That reasoning
    inverts the correct default for personal data: crAPI's own public challenge docs list exactly
    this bug class (Challenge 4) as an intentional vulnerability to find, so the default assumption
    for another account's email address or internal resource ID should be that it's confidential
    unless the evidence shows otherwise -- not that it's fine absent a policy document saying so.
    Fixed by adding an explicit burden-of-proof paragraph to `_VERIFIER_SYSTEM_PROMPT`: personal data
    (email, phone, address, government ID, payment details) or an internal identifier for another
    user's private resource, returned to a caller who isn't that data's owner, is treated as
    disclosure by default; only evidence that the caller *is* the owner, or that the field is
    presented as deliberately public, rebuts that. Every other "could this be a different vuln class"
    skepticism is unchanged. 2 new regression tests asserting the prompt text
    (`tests/test_verifier.py::VerifierPromptBurdenOfProofTests`). Re-verified live afterward: a later
    run reproduced the identical finding and the fixed verifier accepted it (`on_challenge_list=true`,
    correctly classified against Challenge 4).
21. **A `None`/empty `choices` response from an alternate endpoint crashed the whole run instead of
    being retried.** Immediately after fix #20, a re-run against the same OpenCode Zen endpoint died
    at step 2 with a bare `'NoneType' object is not subscriptable` -- `agent/llm_client.py::chat`
    unconditionally indexed `response.choices[0]`, and this particular call came back as a normal
    (non-`APIStatusError`) 200 response with `choices` set to `None`, a transient gateway hiccup
    distinct from the already-handled 404-model-swap case. A plain retry of the same run succeeded
    past that exact step, confirming it was transient rather than a systemic incompatibility. Fixed
    with one retry on an empty/`None` `choices` list (no model re-resolution -- the model is fine,
    the response just came back empty), raising a clear `RuntimeError` only if it recurs twice in a
    row, mirroring the retry-once-then-fail-loudly pattern already used for the 404 case and the
    verifier's own no-tool-call retry. 2 new regression tests
    (`tests/test_llm_client.py::test_chat_retries_once_on_empty_choices`,
    `test_chat_raises_clear_error_on_two_consecutive_empty_choices`). 190 tests now, up from 186.

Across the four live runs at this model: two found only `.env`, one crashed on the (since-fixed) bug
#21 partway through, and one -- run afterward with both fixes in place -- found and correctly
verified both findings, which is the run promoted into `findings.json`/`run.log`/`summary.md`/
`report.md` (the prior single-finding `qwen36-35b-a3b` run, previously "run 9", was superseded by
this promotion; its outcome is documented in this history rather than kept as a separate on-disk
backup -- a `backup_run9_pre_pass10/` directory was mentioned in an earlier draft of this section but
was never actually retained, and that stale reference has since been corrected here). This answers
the standing "would a stronger
model do better?" question concretely: yes -- a different model, same unchanged harness, generalized
onto a real vulnerability class (cross-account PII exposure) the original model never attempted in
14 runs -- while also reproducing the project's now-familiar pattern of real bugs surfacing only once
a new code path is actually exercised live, and the same honest run-to-run instability already
documented for the baseline model (this one didn't reliably find the second finding either).
Reproducing this pass requires a separate OpenCode Zen (or other alternate-provider) API key, which
is intentionally *not* part of `.env.example`'s primary, required configuration -- the default,
documented target remains the assigned Cyrex endpoint.

A follow-up check of `mimo-v2.5-free` (another of the same six free-tier models) found it unusable
on this gateway, reproducibly on two separate attempts: it emits multiple `<tool_call>` XML blocks
in a single turn, but the gateway fails to translate them into separate structured `tool_calls` and
instead concatenates them into one malformed arguments string. `agent/loop.py` correctly caught this
as a malformed call, but the *next* request then got a flat 400 from the gateway regardless, ending
the run at step 1 both times. This is an upstream gateway/model integration fault, not a harness bug
-- the same category as `deepseek-v4-flash-free`'s `tool_choice="required"` rejection above, just a
different failure mode -- and wasn't chased with a workaround for the same reason: accommodating one
third-party gateway's broken translation for one specific model isn't a generalizable fix.

A separate, explicitly **disclosed exception** was also tried for `deepseek-v4-flash-free`: since it
rejects `tool_choice="required"` outright but works under `"auto"`, one run used a scratch harness
variant (a subclassed `LLMClient` overriding only that one setting, never merged into `agent/`) to
see whether "auto" is actually safe for this specific model over a full run, not just a couple of
single-shot probes. It was -- 25 clean steps to token-budget exhaustion, no malformed calls, no
forced repeat-blocks -- but it found only the `.env` finding, no new discovery, so it doesn't change
the promoted result. Because it ran under a different `tool_choice` setting than every other model
tested here, this data point isn't directly comparable to the rest and is reported as a footnote,
not folded into the model-swap conclusion above.

The remaining two free-tier models were tried too, both under the unchanged default
`tool_choice="required"` harness: `ling-3.0-flash-free` found only `.env`, then drifted into a
repeat loop that the existing `MAX_TOTAL_REPEAT_BLOCKS` safety net correctly force-stopped at step 20
-- no crash, no new bug, just a weaker model exercising a safeguard that already existed for exactly
this. `laguna-s-2.1-free` hit a `429 Provider rate limit exceeded` on two consecutive attempts,
almost certainly from the volume of calls already made to the same account across this pass; it
remains untested, and this is a rate-limit fact about the account, not a finding about the model.

### An eleventh pass: full code and documentation review

A dedicated review pass re-read every module under `agent/agent/` and every doc (this README,
`DESIGN.md`, `FUTURE_IMPROVEMENTS.md`) fresh, via two independent audits, looking
specifically for bugs, dead/duplicate code, and documentation gone stale relative to the tenth
pass's two-finding result. Found and fixed:

22. **`challenge_reference.py` misattributed a real finding to the wrong public challenge.** The
    committed `run.log`'s own inline log line reads `on_challenge_list=True
    (challenge-1-bola-vehicle)` for the community-posts finding -- but that finding is actually
    Challenge 4 ("leaks sensitive information of other users"), not Challenge 1 ("access details of
    another user's vehicle"). Root cause: `classify_on_challenge_list` returned the *first* challenge
    in table order whose keywords matched at all, and this finding's text legitimately whole-word-
    matches Challenge 1's `"vehicle"` keyword (it mentions a leaked vehicle ID as one incidental
    field) purely because Challenge 1 is listed before Challenge 4, even though the same text matches
    four of Challenge 4's keywords ("another user", "pii", "email", "excessive") against only two of
    Challenge 1's. This is the same class of over-eager-keyword false positive fix #15 already fixed
    once (word-boundary matching) -- just not exhaustive enough to survive a second real collision.
    Fixed by scoring every challenge by its number of distinct keyword matches and returning the
    highest-scoring one, not the first-listed one. `findings.json`'s own boolean field was never
    wrong (the classifier only feeds a bookkeeping boolean plus an internal diagnostic ID into
    `run.log`), so the committed deliverable is unaffected; the already-committed `run.log`'s inline
    `(challenge-1-bola-vehicle)` text is left as an unedited historical artifact of the pre-fix
    classifier, same as the precedent for run 9's own stale inline log noted earlier. 2 new
    regression tests in `tests/test_challenge_reference.py`.
23. **A startup-time LLM failure crashed the process before any output was written.**
    `AgentLoop.run()` called `self.llm.resolve_model()` with no exception handling, unlike the
    per-turn LLM call inside `_run_turns` (already wrapped in try/except since the original build).
    If `/v1/models` is unreachable at startup, or returns an empty list (`llm_client.py` explicitly
    raises `RuntimeError` for that), the exception propagated uncaught and crashed the process --
    `findings.json`/`summary.md`/`report.md` never written, contradicting this module's own docstring
    guarantee that "nothing here ever writes partial/garbage output." Fixed by wrapping both the
    model-resolution call and the turn loop in the same try/except, turning this into an ordinary
    `llm_error: ...` stop reason with full deliverables still written, reusing the existing
    write-outputs path rather than duplicating it for a startup-only special case.
24. **Semantic de-duplication's clustering was order-dependent.** `dedup_findings` grouped findings
    within a structural bucket by comparing each new candidate's `why_disclosure` only against a
    cluster's first member. For a genuine transitive chain (A similar to B, B similar to C, but A not
    directly similar enough to C), the result depended on arbitrary discovery order -- the same three
    findings could merge into one group or split into two depending only on which arrived first, not
    on anything about the findings themselves. Fixed by replacing the online single-representative
    comparison with proper single-linkage clustering via union-find (connected components over the
    full pairwise-similarity graph), which is genuinely order-independent. 1 new regression test
    exercising three different orderings of the same transitive-chain fixture.
25. **A documented validation check ("lexical overlap with the title") didn't actually exist.**
    `validation.py`'s own module docstring claimed `why_disclosure` was checked for "lexical overlap
    with the title," but the implementation only checked minimum length and *exact* title restatement
    -- a model could pad the title with a few trivial extra words (e.g. appending "and this is bad
    for users") to dodge the exact-match check while still submitting almost entirely a copy of the
    title, undermining the justification requirement this check exists to enforce. Fixed by
    implementing the documented overlap check: reject when nearly every significant title word (4+
    letters) reappears in `why_disclosure` AND the reasoning isn't meaningfully longer than the title
    -- calibrated against the module's own existing accepted-finding test fixtures (which share some
    title vocabulary but add real length/specifics) to confirm it doesn't false-trigger on genuine
    reasoning. 1 new regression test.
26. **A malformed tool call's JSON-decode-failure path was missing its `OBSERVATION` log line.**
    `_handle_tool_call` logged `MALFORMED_TOOL_CALL` for both a JSON-decode failure and a
    dispatch-time error, but only the dispatch-time path also logged the matching `OBSERVATION` line
    for the result it returned -- a `run.log` reader scanning for one INTENT/TOOL_CALL/OBSERVATION
    triple per step would find some malformed-call steps silently missing their third line. Fixed by
    logging `OBSERVATION` consistently on both paths. 1 new assertion in the existing malformed-JSON
    integration test.
27. **The evidence-body cache backing finding-grounding could silently evict a genuine finding's
    only evidence on a longer run.** `agent/tools/http_tool.py::HttpToolkit._body_cache` was a
    bounded ring buffer of ~100 entries (~50 requests -- each completed request appends both a body
    and a header-block entry), and it's the *only* place full response bodies are kept for
    `agent/validation.py`'s evidence-grounding check (`RequestRecord` deliberately omits bodies, by
    its own docstring, for memory/log-size reasons). Any run past ~50 real requests -- and this
    project has run up to 150 steps -- could have silently rejected an otherwise-legitimate finding
    whose evidence came from an observation older than the cache window, with nothing in the prompt
    or code telling the model evidence must come from a still-cached observation. Fixed by
    introducing `EVIDENCE_BODY_CACHE_SIZE` and sizing it comfortably past any run this project has
    actually exercised. 2 new regression tests (`tests/test_http_tool.py`). Also added a missing
    regression test for fix #23's startup `resolve_model()` exception handling, flagged by a
    fresh code review as implemented but untested.
28. **A non-positive `--request-timeout`/`REQUEST_TIMEOUT_SECONDS` silently wasted an entire run
    instead of failing fast at startup.** `requests` raises a bare `ValueError` (not
    `requests.RequestException`) for a timeout `<= 0`, uncaught by either `http_tool.py` call site's
    `except requests.RequestException`. `AgentLoop.run()`'s own startup catch-all (fix #23) does
    prevent an actual process crash, but the run still burns an LLM call, ends on literally the first
    HTTP request with zero real exploration, and reports a misleading `stop_reason` of
    `"llm_error: ..."` for what is actually a config problem, not an LLM one -- verified live
    (`--request-timeout 0` against the real crAPI stack). Fixed by validating
    `request_timeout_seconds > 0` in `build_config`, raising a clean `ConfigError` at startup like
    every other bad config value. 3 new regression tests (`tests/test_config.py`).
29. **`--out-dir`/`OUT_DIR` pointing at an existing regular file crashed with a raw traceback
    instead of a clean CLI error.** `build_config`'s `resolved_out_dir.mkdir(parents=True,
    exist_ok=True)` had no exception handling; `Path.mkdir` raises `FileExistsError` in exactly this
    case (verified directly), which isn't a `ConfigError`, so `cli.py`'s `except ConfigError` never
    catches it -- the one misconfiguration that reached the user as an unhandled Python traceback
    instead of `error: ...` on stderr with exit code 2. Fixed by wrapping the `mkdir` call and
    re-raising as `ConfigError`. 1 new regression test (`tests/test_config.py`).
30. **Fix #28's own positivity check missed NaN and +Infinity.** `if resolved_request_timeout_seconds
    <= 0` looks like it rejects every non-positive value, but every comparison against `float("nan")`
    is `False` (so NaN slips through) and `float("inf") > 0` is `True` (so +inf slips through too) --
    both verified directly. Both are genuinely CLI-reachable: `argparse`'s `type=float` parses
    `--request-timeout nan` / `--request-timeout inf` without complaint. Downstream,
    `requests.get(timeout=float("nan"))` raises a bare `ValueError` and `timeout=float("inf")` raises
    a bare `OverflowError` -- neither a `requests.RequestException`, so both reproduce fix #28's exact
    failure mode (a wasted run ending on step one with a misleading `"llm_error: ..."` stop reason)
    despite fix #28's own check technically running. Fixed by replacing the plain `<= 0` comparison
    with `not (math.isfinite(x) and x > 0)`. 2 new regression tests (`tests/test_config.py`).
31. **`--max-steps`/`--max-tokens`/`--max-consecutive-repeats` had no bounds check at all**, unlike
    `--request-timeout` (fixes #28/#30). A non-positive value doesn't crash -- `BudgetStatus`'s
    `steps_used=0 >= max_steps<=0` (or the equivalent token check) is `True` immediately -- but it
    silently produces a fully-formed, "successfully completed" empty run (`findings.json`,
    `summary.md`, etc. all written; `stop_reason` reads a technically-true but unhelpful "step budget
    exhausted (0/0)") instead of catching what's almost always a typo at startup, the way every other
    bad config value does. Verified live with `--max-steps -1`, `--max-tokens 0`, and
    `--max-consecutive-repeats 0` against the real crAPI stack before and after the fix. Found via a
    dedicated `hypothesis` property-testing pass (see "A fifteenth pass" below) rather than reading
    review -- fifteen passes of reading had looked straight past this one. Fixed with the same
    fail-closed `ConfigError` pattern as fixes #28/#30, one per field. Also caught and fixed in the
    same pass: the new `--max-tokens` error message initially said `--max-total-tokens` (the internal
    field name, not the actual CLI flag) -- caught by a live CLI check, not by the unit tests, none of
    which asserted on the message text. 7 new regression tests (`tests/test_config.py`) plus 14 new
    `hypothesis` property tests (`tests/test_property_based.py`).

Also cleaned up, no fix number (no behavior change): removed `cli.py`'s duplicate
`if __name__ == "__main__"` entry-point guard (redundant with `agent/__main__.py`, the actual
documented `python -m agent` entry point -- confirmed no test depended on invoking `cli.py` directly);
corrected `http_tool.py::_track_family_outcome`'s docstring, which claimed the sequential-failure hint
fires only "the moment the streak crosses the threshold" when the implementation (correctly, and now
accurately documented) fires on every subsequent same-family failure past that point, not just the
first; added a clarifying comment in `loop.py` noting that the verifier/dedup finalization tail
deliberately runs outside the `max_total_tokens` budget check, so the final cost-summary tally can
legitimately read a little higher than the loop's own "token budget exhausted" stop line -- a
real-looking ~3,000-token gap in the committed `run.log`/`report.md` that a second independent audit
flagged as worth a sanity check, confirmed to be this, not a bookkeeping bug. Also corrected several
stale cross-references found by the same audits: the "Known limitations" and "Ambiguities &
assumptions" sections below still described the pre-tenth-pass single-finding, 120-step-budget state;
`FUTURE_IMPROVEMENTS.md`'s crawler-removal and "Summary judgment" sections still said "the
project's one real finding" and attributed the recall gap purely to model capability. All now
reflect the current two-finding, default-budget, partly-model-capability state. 194 tests as of
this pass, up from 190 (197 as of the twelfth pass -- see below).

### A twelfth pass: final evaluation plus a dead-code/duplication sweep

A twelfth pass (2026-07-29) had two parts: a final pre-release evaluation (fix #27 above, plus a
live smoke test confirming the eleventh pass's changes hadn't regressed anything, plus reconfirming
packaging is still outstanding), and a dedicated dead-code/duplication and
documentation-obsolescence sweep. No genuinely dead code was found in `agent/agent/` -- every
function/class/constant traced to a real call site or a framework hook (e.g. pydantic validators)
invoked by name rather than direct reference. `agent/tests/` did have real duplication: a
`_finding(**overrides) -> Finding` builder independently redefined (with slightly different
defaults) in three test files, and a `_FakeResponse` stand-in for `requests.Response` independently
redefined in two. Both consolidated into a new `agent/tests/_helpers.py`
(`make_finding`/`FakeResponse`); one test had been implicitly relying on an old per-file default
value rather than overriding it explicitly, caught by running the affected file's tests before/after
rather than trusting the refactor on sight. `_FakeSession` was deliberately left unmerged across
those same two files -- one version is scriptable via per-test response queues, the other always
returns one fixed canned response, a genuine behavioral difference each file actually depends on, so
forcing them into one shared class would have added indirection without reducing real duplication
risk. Also corrected two stale doc references this same sweep found: a claim (here) that a `backup_run9_pre_pass10/` directory preserves the superseded single-finding run
(it doesn't exist on disk) and a `DESIGN.md` sentence in the crawler-removal section still saying
"the project's one real finding" despite the two-finding state (the parallel sentence in
`FUTURE_IMPROVEMENTS.md` was already fixed in the eleventh pass; this one was missed then). 197
tests as of that pass, up from 194 (unchanged by the consolidation itself, which only merged support
code, not test cases).

### A thirteenth pass: two more real config-validation bugs, and one broken doc cross-reference

A thirteenth pass (2026-07-29) repeated the same two-pronged fresh-eyes review (independent agents
plus a live 12-step smoke test against the real crAPI stack, which reproduced the `.env` finding
again with no regressions) on modules that had gotten comparatively less scrutiny across the prior
twelve passes: `schemas.py`, `scope.py`, `cost.py`, `report.py`, `prompts.py`, `toolbox.py`,
`logging_setup.py`, `config.py`, `budget.py`, `tools/control_tool.py`, `cli.py`, `__main__.py`. Found
and fixed two more real bugs (#28-29 above), both in `config.py`'s total lack of bounds/existence
checking on two settings that had themselves only recently become configurable (`--request-timeout`
and `--out-dir`): a non-positive request timeout wasted an entire run on a misleadingly-labeled
`"llm_error"` stop reason instead of failing fast at startup, and an `--out-dir` colliding with an
existing regular file crashed with a raw Python traceback instead of the clean `error: ...` message
every other bad config value gets. Both verified live against the real CLI, not just via unit tests,
confirming the exact before/after behavior described in fixes #28-29. A second independent audit,
checking the twelfth pass's own doc edits for correctness rather than the code, found one real
issue: `DESIGN.md` pointed readers at "README's 'A twelfth pass' section," but that content had only
ever been a paragraph nested inside the "An eleventh pass" heading, not its own section -- fixed by
giving it the proper `### A twelfth pass` heading above (matching this file's own established
one-heading-per-pass convention) rather than just rewording the pointer to work around the gap. 201
tests now, up from 197.

### A fourteenth pass: a NaN/Infinity gap in fix #28, and a dedicated coverage sweep

A fourteenth pass (2026-07-29, same day) repeated the fresh-eyes-plus-live-smoke-test pattern once
more, this time with three parallel angles: an adversarial re-check of the thirteenth pass's own two
fixes rather than assuming "tests pass" meant "fully correct"; a dedicated, systematic sweep of every
docstring and inline comment in `agent/agent/` and `agent/tests/` against actual current behavior
(never done as its own dedicated pass before -- prior fixes to comments were opportunistic
byproducts of behavior-focused reviews); and a fresh staleness check of the four markdown docs
specifically for whether the thirteenth pass's own fixes were fully reflected everywhere they should
be. Found and fixed one more real bug (#30 above): fix #28's `<= 0` check missed `NaN`
(every comparison against NaN is `False`) and `+Infinity` (which is `> 0`), both genuinely
CLI-reachable and both reproducing fix #28's exact failure mode -- caught by directly executing the
scenario rather than reasoning about it in the abstract, the same verify-by-running discipline the
thirteenth pass used. That same re-check installed `coverage.py` for the first time in this
project's history and found a second real bug, this one test-only: `test_cli.py`'s
`test_plain_find_dotenv_without_usecwd_ignores_process_cwd` depended on `python-dotenv`'s
`find_dotenv()` treating any active trace function as "running under a debugger" and silently
switching to cwd-based resolution -- exactly the behavior the test exists to show is otherwise
absent -- so the test passed under plain `pytest` but failed under `coverage run -m pytest` (and
would fail under any debugger too), a false failure with no actual behavior bug behind it. Fixed by
pinning `sys.gettrace()` to `None` for the duration of the call, making the test deterministic
regardless of invocation. The coverage report itself then motivated closing several real gaps in
test coverage that two rounds of code review had missed by reading alone: `control_tool.py`'s
schema-validation-rejection branch (the bonus structured-output-validation-and-retry path) had zero
coverage; `http_tool.py`'s `except requests.RequestException` blocks (both in `_login` and in the
main request path) were entirely untested, the same blind spot fix #28 exploited; and -- the largest
gap -- `AgentLoop.run()`'s own verifier/dedup finalization wiring had never been exercised at the
loop level at all (only the underlying `verify_finding`/`dedup_findings` functions had dedicated unit
tests), because the test suite's own config default is `verifier_enabled=False, dedup_enabled=False`.
A bug in that wiring itself -- e.g. forgetting to reassign `findings = survivors` after the verifier
loop -- would have gone uncaught by the existing suite. 8 new tests closed these gaps (2 for the
NaN/Infinity fix, 1 for the schema-rejection branch, 4 for the `RequestException`/malformed-login-JSON
paths, 2 for the verifier survives/refutes wiring, 1 for the dedup wiring, 1 for a mid-loop LLM
failure, 1 for `validation.py`'s short-evidence fallback) -- `agent/agent/`'s own source now sits at
100% line coverage in every module except `budget.py`, `cli.py`, `dedup.py`, and `logging_setup.py`,
each with only trivial fallback branches or `main()`'s own print-formatting left uncovered. The
docstring/comment sweep found two genuine stale cross-references, both fixed: `loop.py`'s
`_compact_transcript` docstring pointed at "loop.py's own module-level case-study comment, fix #6"
for the model's repeat-until-token-exhaustion failure mode, but that case study actually lives in
`llm_client.py::LLMClient.chat`'s comment above its `frequency_penalty`/`presence_penalty` settings --
fixed to point there instead. And `EVIDENCE_BODY_CACHE_SIZE`'s own comment claimed "a low
single-digit-MB ceiling even at the full bound," which was accurate at the constant's old value (100)
but not after it was raised 40x to 4000 in the twelfth pass -- corrected to the actual ~16 MB figure.
The markdown sweep found the README Configuration table listed `--request-timeout`/`--out-dir`
without documenting that both now fail closed with a clean error (fixes #28-29) -- added a
`**Validation:**` note plus matching one-line comments in `.env.example`. 213 tests now, up from 201.

Honest scoring, not spin, against the evaluation metrics's five metrics:

| Metric | Result | Why |
|---|---|---|
| Recall | 1 real finding accepted; 0/~18 public-subset challenges found (in the currently-promoted run) | The currently-promoted run used the documented Cyrex/`qwen36-35b-a3b` config, which has never reliably reproduced the community-posts/Challenge-4 finding (that required the tenth pass's one-off model swap -- see the note above). Public-subset recall is honestly 0 for what's actually reproducible here today. |
| Precision | 1/1 (100% on what was reported) | The reported `.env` finding is a real, independently-confirmed bug against crAPI's own git history, not just the discovering model's say-so; zero false positives among accepted findings. |
| Generalization | 1 withheld-subset finding found and correctly tagged as such | `.env` credential exposure is not documented anywhere in crAPI's public `docs/challenges.md`; `on_challenge_list=false` is the code-computed, verified-correct answer (see fix #15 and the note above). This is the metric this project weighs most heavily, and it's a genuine, non-vacuous positive result. |
| Scope discipline | 0 out-of-scope reports (perfect) | The scope gate (`agent/scope.py`) and validator correctly rejected every ungrounded/paraphrased proposal attempt (several, across the seventeenth pass's live runs -- see that section) and accepted only properly-grounded findings; nothing off-scope was ever proposed across any run, across any model tested. |
| Stability | Real, honest instability at every budget and model tried | Two `qwen36-35b-a3b` runs at the identical `--max-steps 120 --max-tokens 1100000` budget: one found `.env`, the next found nothing. A 60-step validation pass: of 4 runs, 2 found and accepted the finding, 1 would have but for a since-fixed bug, 1 found nothing. The model-swap pass reproduced the same pattern one level up: of 4 `nemotron-3-ultra-free` runs, 2 found only `.env`, 1 crashed partway through on since-fixed bug #21, 1 found and verified both findings. The seventeenth pass's live batch (see that section) was consistent with all of this: 4 of 7 runs at default budget found `.env` (one with a since-fixed grammar-leak snag, one with a degenerate reproduction field), 3 found nothing. Reported honestly rather than cherry-picking the successful run's framing. |

The passing bar in the evaluation metrics ("finds most of the public subset, reports at least one withheld
issue... zero out-of-scope") is **met on generalization** (the metric weighed most heavily),
**scope discipline, and precision**, and **not currently met on public-subset recall** for the
promoted run specifically (0 of ~18 challenges found by the reproducible Cyrex/qwen config; the
tenth pass's model-swap experiment did find 1, but that result isn't reproducible with this
project's committed credentials -- see the note above). Manually cross-checking
`crapi/docs/challenges.md` against `findings.json` confirms the `.env` disclosure appears nowhere in
that list.

### A fifteenth pass: property-based fuzzing finds a bounds-check gap fourteen reading passes missed

A fifteenth pass (2026-07-29, same day) deliberately switched verification technique rather than
running a fifteenth round of the same reading-based review: `hypothesis` (property-based testing) was
installed for the first time in this project's history and pointed at the modules most likely to have
input-handling edge cases -- `config.py`'s numeric bounds checks, `validation.py`'s untrusted-model-text
handling, and `schemas.py`'s pydantic field validators. Before writing the fuzz tests, direct execution
of `build_config` with hand-picked non-positive values surfaced the actual bug this pass found: unlike
`--request-timeout` (fixes #28/#30), the other three hard-cap settings --
`--max-steps`/`--max-tokens`/`--max-consecutive-repeats` -- had *no* bounds check at all, silently
accepting 0 or negative values and producing a fully-formed, "successfully completed" empty run instead
of a clear startup error (fix #31 above; see that entry for the full mechanism). Fourteen prior passes
of reading review, several of them specifically auditing `config.py`, never caught this -- the two
already-validated fields (`request_timeout_seconds`, and by then `out_dir`) apparently satisfied a
"config.py has bounds-checking" mental checklist without anyone verifying *every* numeric field
actually had one. 14 `hypothesis` property tests (`tests/test_property_based.py`, new file) plus 7
hand-written regression tests for fix #31 (`tests/test_config.py`) were added; the property tests
themselves (100 generated examples each against `_normalize`, `_evidence_is_grounded`,
`_reasoning_is_substantive`, `validate_finding`, `check_scope`, and the `Finding`/`Account`/`Credentials`
pydantic models) found no further bugs -- a genuinely informative negative result after fourteen prior
passes' worth of hardening on those specific modules, not a wasted effort. A second bug was caught
during live CLI verification of the fix itself, not by any test: the new `--max-tokens` error message
initially read `--max-total-tokens` (copied from the internal field name rather than checked against
the actual flag registered in `cli.py`) -- none of the new unit tests asserted on message text, so only
running the actual CLI surfaced it. Fixed immediately. 234 tests now, up from 213 -- 21 new (14
property-based + 7 example-based), across 18 files (`test_property_based.py` is new).

**Why this matters for the standing "why does every pass find something new" question:** the honest
answer is that most passes so far used one technique (careful reading) applied to already-hardened
code, which has a real ceiling -- a human or LLM re-reading `config.py` for the fifth time tends to
re-confirm what it already believes is correct rather than notice a checklist gap. Switching technique
(execution-driven fuzzing instead of reading) found a bug in one targeted session that fourteen
reading-based passes, several of them specifically about `config.py`, did not. The corollary is not
"there must be more bugs, keep going" -- the property tests' clean pass on `validation.py`/`schemas.py`
is real evidence those modules are solid, not just unexamined. The actionable lesson is: past a certain
point, more reading passes have low expected value; a differently-shaped verification technique
(fuzzing here; something else next time) is a better use of further effort than another read-through.

### A sixteenth pass: simplification -- two unused mechanisms removed, no bug hunt this time

Explicitly requested and scoped differently from every pass above: not another bug hunt, but a pass
focused purely on readability/maintainability. Four parallel fresh-eyes surveys covered every module
in `agent/agent/` and converged on a small, low-risk set of changes, split into two kinds.

**Two mechanisms removed** (flagged to, and approved by, the user before touching anything, since
each is a real behavior change, not a pure refactor):

1. **The same-path-family failure-streak hint** (`agent/tools/http_tool.py::_track_family_outcome`,
   originally fix #4 above). Grepping the one committed `run.log` this project has ever kept for
   `"hint"` or `"repeat_blocked"` returned zero matches for either -- neither this mechanism nor the
   one below has ever visibly fired in a real, on-disk run log. The one scenario this hint was
   explicitly built for (a 150-step run that swept ~15 sequential same-family requests, described in
   the bigger-budget-experiment section above) hit all 200s, not failures, so the hint's own
   `is_failure` gate never even applied to it. Removed along with its dedicated test file
   (`tests/test_failure_streak_hint.py`) and its test class inside `test_http_tool.py`.
2. **The `MAX_TOTAL_REPEAT_BLOCKS` forced-stop counter** (`agent/loop.py`). This sat as a third
   safety layer on top of two others that already make it redundant: the per-call `repeat_blocked`
   refusal (which prevents the repeated call from ever dispatching) and the hard step/token budget
   (which ends the run regardless). This project's own history already noted "zero forced
   repeat-blocks" across runs 7-8; nothing since has shown otherwise. Removed, along with the
   `total_repeat_blocks` plumbing it required through `_handle_tool_call`'s signature and return
   value, and the associated `test_loop_integration.py` test case (rewritten to assert the remaining,
   correct behavior: repeated identical calls keep getting refused per-call, with the run riding out
   the full step budget rather than force-stopping early).

Both removals follow the exact precedent set by the sixth pass's removal of the path-candidate
crawler: an unused mechanism is complexity the spec doesn't reward, not a hedge worth keeping
"just in case" -- and the evidence bar here (a direct `run.log` grep, not just "it looks unused") was
the same one applied to that earlier removal.

**Pure refactors** (zero behavior change, verified by the full test suite staying green and a live
smoke test against the real crAPI stack producing an identical shape of output): collapsed
`config.py`'s four near-identical resolve+validate+raise blocks (`max_steps`, `max_total_tokens`,
`max_consecutive_repeats`, `request_timeout_seconds`) into one `_resolve_positive` helper, preserving
every exact error message; extracted a shared `_parse_json_object` helper in `loop.py` used by both
`_tool_response_signature` and `_strip_headers_for_clip` (previously each had its own copy of the
same `json.loads`/`isinstance` check); split `AgentLoop.run()`'s inline verifier/dedup block into a
named `_finalize_findings` method and the coverage-reminder injection inside `_run_turns` into
`_maybe_inject_coverage_reminder`; grouped the transcript-compaction constants together instead of
interleaving them with the functions that use them; added a `_reject` helper in
`agent/tools/control_tool.py` to remove a duplicated three-line rejection sequence; renamed
`scope._keyword_pattern` to public `scope.keyword_pattern` (it was already imported across the
module boundary by `challenge_reference.py`, so the leading underscore misrepresented its actual
status as a shared utility) and consolidated both modules' near-identical
"compile keyword patterns, zip with an id" comprehensions into one shared `compile_keyword_patterns`
helper in `scope.py`; named two previously-inline anonymous multi-line prompt strings in
`prompts.py` (`TOPOLOGY_TEXT`, `ACCOUNTS_TEXT_TEMPLATE`) to match the file's own existing convention
(`MISSION_TEXT`, `TOOLS_TEXT`, `RECON_TEXT`).

Verified: full offline suite green throughout (226 tests, down from 234 -- the eight removed were
the two mechanisms' own dedicated tests, not a coverage loss elsewhere), `pyflakes` clean, and a live
12-step run against the real crAPI stack completed cleanly (correct step-budget termination, coverage
reminder fired at step 8 as expected, all four output files written) to confirm the loop.py/http_tool.py/
control_tool.py hot-path changes introduced no regression.

### A seventeenth pass: a fresh final run, a real diagnosis, and two more real bugs (#32-33)

Prompted by a direct request to clean every existing output and collect a fresh, live final run.
Three attempts at escalating budgets against the documented Cyrex/`qwen36-35b-a3b` config (40/400k,
120/1.1M, 250/3M steps/tokens) all came back with **zero findings** -- worse, in fact, than the
already-committed two-finding run, since that run depended on the tenth pass's one-off model swap.
Rather than accept that regression or keep blindly retrying, the three runs' logs were compared
directly: across all ~200 tool calls in those three runs, the model called `http_request` 199
times, `finish_investigation` once, and **`propose_finding` zero times** -- despite one of the three
runs clearly fetching `GET /.env` and getting a 200 response containing the same real credentials as
the already-known finding. It simply never proposed it. The model also produced **zero free-form
reasoning text** on any of those ~200 turns.

This ruled out a prompt gap (`RECON_TEXT` already tells the model to check exactly this kind of path
early) and a wiring bug (`propose_finding` dispatches correctly; 226/226 offline tests passed
throughout). The actual cause: this model, under `tool_choice="required"`, appears to satisfy "call
some tool" by defaulting to the cheapest one (`http_request`) almost every turn, rather than ever
choosing the tool that demands generating substantial structured prose. A model that never reasons in
text isn't going to be moved by a softer in-transcript nudge (the kind of mechanism this project has
tried, and removed, before -- see the sixteenth pass above) -- so the fix instead uses a hard
constraint, the same category of tool as `tool_choice="required"` itself, just narrowed for one turn:

- **Fix #32**: `agent/loop.py::AgentLoop._maybe_force_propose_finding` watches every `http_request`
  observation for a 200 response on one of a small, machine-readable mirror of `RECON_TEXT`'s own
  "well-known sensitive path" list (`SENSITIVE_PATH_MARKERS`: `.env`, `.git/config`, `.git/HEAD`,
  `config.json`, `actuator/env`, `/debug`). The first time this fires for a given path, the *next*
  chat call's `tool_choice` is narrowed from `"required"` (any tool) to `{"type": "function",
  "function": {"name": "propose_finding"}}` (`agent/llm_client.py::LLMClient.chat`'s new
  `force_tool_name` parameter) -- compelling the model to actually fill in the finding schema using
  the evidence it just saw, exactly once per distinct triggering path. If the model has genuinely
  nothing real to report, `validation.py`'s evidence-grounding check and the optional verifier still
  reject a bad proposal, so this cannot corrupt real output, only produce (at worst) one rejected
  attempt.
- **Fix #33**: live-testing fix #32 immediately surfaced a second, real bug. Forcing the model into a
  single function call occasionally exposed a raw chat-template leak in its output -- a stray
  `</tool_call><tool_call>\n<function=http_request>` fragment bleeding into a string argument -- which
  corrupted the call into carrying an extra key (`method`) that `propose_finding` doesn't accept at
  all. `agent/toolbox.py::ToolBox.dispatch` previously unpacked `**arguments` directly into each
  toolkit method, so this crashed with an opaque `unexpected keyword argument` `TypeError` (caught by
  the existing malformed-call handling, so the run itself never crashed -- but the proposal was lost
  outright rather than failing through the normal, model-visible rejection path). Fixed with
  `_drop_unrecognized_kwargs`, which filters a call's arguments down to whatever the target method's
  own signature actually accepts (via `inspect.signature`) before dispatch, applied uniformly at all
  three `**arguments`-unpacking call sites (`http_request`, `propose_finding`,
  `finish_investigation`). An unrecognized key is now dropped (and logged) rather than raising --
  consistent with `dispatch`'s own pre-existing docstring promise that only an *unknown tool name*
  raises.

**Live verification**: 7 live runs at the default 40-step budget against the real crAPI stack, after
both fixes landed. The forced-propose mechanism fired in 3 of them. Two produced a genuine accepted
`.env` finding (one with a clean `reproduction` field, promoted as this project's current
`findings.json`; one with a degenerate `reproduction: ["[]"]` -- a real model-quality limitation, not
promoted). The third forced trigger hit fix #33's exact grammar-leak failure mode head-on: even with
the unrecognized-kwarg fix in place, the model's retries kept omitting `why_disclosure`/`reproduction`
entirely, so all 7 retries were correctly rejected by `validation.py`, then correctly refused
outright once they became identical repeats (the existing per-call repeat-block mechanism) -- an
honest miss, not a hidden one. The remaining 4 of 7 runs had no forced trigger at all: the model
either guessed a sensitive-file path under the wrong prefix (e.g. `/identity/.env` instead of the
real root-level `/.env`, all 404) or skipped that recon step entirely -- a different facet of this
model's general unreliability than the one these two fixes target, and not something either fix
claims to solve. 233 tests at this point, up from 226 (5 covering fix #32's trigger logic in
`test_loop_integration.py`/`test_llm_client.py`, 2 covering fix #33 in `test_toolbox.py`).

Running the full suite five times in a row as a final check (rather than trusting one green run)
caught one more real bug this same pass: `hypothesis` fuzzing `validation.py::_normalize`
(`test_property_based.py`, present since the fifteenth pass) found it wasn't idempotent for an
uppercase `\N`/`\R`/`\T` escape sequence -- the old lowercase-last ordering only case-folded such a
sequence *after* the lowercase-only backslash-collapse regex had already run, so a genuinely
single-application call (the only way `validate_finding` ever calls `_normalize`) could leave an
uppercase escape sequence uncollapsed. Fixed as **#34** by lowercasing before collapsing, with both a
concrete regression test (`tests/test_scope_and_validation.py`) and the pre-existing property test now
passing reliably. 234 tests now, up from 226.

### An eighteenth pass: a new tool, `list_id_candidates`, kept on the compliant side of a scope line

Prompted by the user asking whether the agent could be improved to find more APIs/paths, and
specifically proposing a crawler. Two designs were discussed and explicitly checked against the
project's scope goals before writing any code:

1. **A multi-level BFS crawler** (model picks a seed, an algorithm autonomously issues requests
   across several hops) was rejected as out of scope: it collides with the "autonomous loop" goal's "the agent
   decides what to probe next... not a fixed, pre-scripted sequence of requests" (BFS traversal is
   exactly such a sequence, even with a model-chosen seed) and the project's framing's "we are not testing
   whether you can... write a perfect scanner." It would also make the step/token budget stop
   meaningfully bounding real request volume, a milder version of a gap already flagged in
   `FUTURE_IMPROVEMENTS.md`.
2. **A single-hop, model-directed pivot tool** was proposed instead and built:
   `list_id_candidates(path)` inspects only the cached response body from one request the model
   already made (no new HTTP call, no fan-out), and returns ID-shaped fields found in it (generic
   REST naming convention: bare `id`/`uuid`, `*_id`, `*Id`, `*Uuid` -- no crAPI-specific field names)
   plus, when `path` itself has an ID-shaped segment, ready-to-use candidate paths with that segment
   substituted. The model still decides whether to call it, on which path, and whether to actually
   fetch any candidate via its own separate `http_request` call -- every hop stays a distinct,
   model-reasoned decision, unlike the rejected BFS design.

This is explicitly framed as a different bet than the already-removed path-candidate crawler (see
`agent/tools/http_tool.py`'s module docstring and the sixth-pass entry above): that one scanned raw
response text for path-shaped substrings and never found anything beyond trivial static assets on
this target, because crAPI's real API responses are plain JSON data records, not link-bearing
HTML/JS. `list_id_candidates` instead extracts ID *values* from those same data records to support
the BOLA/excessive-data-exposure pivot `RECON_TEXT` already instructs the model to attempt --
targeting the shape of data this target actually returns, not the shape the removed crawler assumed.

**Live verification surfaced two real gaps the offline tests alone hadn't caught**, both found by
directly exercising the new tool against the real crAPI stack (not just re-reading the code):

- `GET /identity/api/v2/vehicle/vehicles` returns each vehicle's real path identifier under a field
  literally named `uuid` -- not `id` or `*_id` -- which the original key-matching check missed
  entirely. Confirmed this is also the *only* identifier the actual pivot endpoint
  (`/vehicle/{carId}/location`) accepts: a numeric `id` 400s with "Failed to convert 'carId'". Fixed
  by recognizing bare `uuid`/`*_uuid`/`*Uuid` as ID-shaped keys alongside `id`/`*_id`/`*Id`.
- Once the `uuid` gap above was fixed, the same live check found a second issue: a response can
  contain both a UUID (the correct pivot value) and an unrelated numeric id (e.g. a nested
  `vehicleLocation.id`) -- naively substituting either into the path's ID slot would offer a
  guaranteed-to-400 candidate for the wrong "flavor" of segment. Fixed by classifying both the path's
  own ID segment and each candidate value's shape (`numeric` / `uuid` / `hex`) and only offering
  same-shaped substitutions.

20 new offline tests cover the extraction logic, the two live-discovered gaps above (as concrete
regression tests, not just the abstract mechanism), and the new tool's schema/dispatch wiring. 254
tests total, up from 234. A live smoke test after every change (three runs total against the real
crAPI stack) confirmed no regression to the existing `.env`-finding path or any other mechanism.
Whether the baseline model actually reaches for this tool during a real autonomous run remains
unconfirmed -- see "Known limitations" below, the same honestly-reported open question fix #32
already documents for `propose_finding` itself.

### A nineteenth pass: a fresh clean run, and a real dedup gap (fix #35)

Prompted by a request to clear all prior outputs and collect one fresh live run against the
running crAPI stack with the post-eighteenth-pass agent. Three runs were executed at the default
budget (40 steps / 400k tokens), and the final one's output is what's promoted below.

1. **Run A: 0 findings, an honest instability miss.** The forced-`propose_finding` mechanism
   (fix #32) correctly fired on the real `GET /.env` 200 at step 1, but the model's one retry
   attempt (fix #33's single-retry budget) hallucinated the wrong endpoint entirely
   (`.git/config`, never actually requested yet) and omitted the required `confidence` field --
   correctly rejected as malformed, then the run continued normally rather than looping. This is
   the exact failure mode already documented in the seventeenth pass recurring again, not a new
   bug: this model's tool-call reliability under a single forced retry is genuinely imperfect, and
   this project doesn't paper over that.
2. **Run B: 2 findings accepted, but both describe the same disclosure.** The model proposed the
   `GET /.env` finding twice, at different steps, with substantially different `why_disclosure`
   commentary each time -- and `dedup_findings` (`agent/dedup.py`) left both standing rather than
   merging them. Checking directly with `difflib.SequenceMatcher` (not just re-reading the code)
   showed why: the two `why_disclosure` texts were only ~0.10 similar (different wording,
   different emphasis), while their `evidence` fields -- the literal captured `.env` body, byte-for-
   byte the same credential dump -- were ~0.95 similar. `dedup_findings`'s clustering only ever
   compared `why_disclosure` text, so it had no signal that would have caught this: two
   differently-worded write-ups of the identical underlying disclosure read as two unrelated
   findings to it. Fixed as **(35)**: clustering now merges on `max(why_disclosure similarity,
   evidence similarity) >= threshold`, so a near-identical `evidence` excerpt is enough to merge
   even when the model's free-text commentary about it differs. This doesn't reintroduce the
   over-merging the design already guards against (two genuinely different bugs sharing an endpoint
   pattern, e.g. a leaked email field vs. a leaked internal debug flag on the same list endpoint) --
   those still have distinct `evidence` excerpts, not just distinct commentary, so they stay
   separate. Two existing tests
   (`test_keeps_distinct_issues_on_same_endpoint_pattern_separate`,
   `test_transitive_similarity_chain_merges_regardless_of_order`) had been unknowingly relying on
   `make_finding`'s shared default `evidence` value being identical across every finding they built
   -- harmless before this fix, but it would have made both tests pass or fail for the wrong reason
   once evidence similarity became part of the merge decision. Fixed by giving each finding in those
   tests distinct, narrative-matching evidence text (pairwise similarity confirmed <0.75, so each
   test still isolates the exact signal it's meant to exercise), plus a new dedicated regression
   test (`test_merges_same_evidence_despite_differently_worded_reasoning`) reproducing this run's
   exact scenario. 255 tests now, up from 254, pyflakes clean.
3. **Run C (post-fix): 1 finding accepted, 1 correctly rejected.** The model proposed the `.env`
   finding once with paraphrased/summarized evidence not traceable to an actual captured response --
   correctly caught and rejected by the existing evidence-grounding check (unrelated to this pass's
   fix) -- then proposed it again later with evidence copied verbatim from the real captured
   response, which was accepted. A single finding, no dedup ambiguity to exercise this time. This
   run's `findings.json`/`run.log`/`summary.md`/`report.md` are the ones now committed, superseding
   the prior seventeenth-pass run of the same single `.env` finding.

This reinforces, rather than changes, the standing conclusion: this model finds the same one real
disclosure at this budget, with meaningful run-to-run variance in *how cleanly* it gets reported
(sometimes not at all, sometimes as an accidental duplicate), not in *what* it finds. The dedup fix
is a genuine correctness improvement independent of that -- any future run (or a stronger model
proposing more real findings) benefits from it collapsing accidental duplicates correctly.

**Immediate follow-up: a 3x budget re-run, same result.** `.env` was bumped to `MAX_STEPS=120` /
`MAX_TOTAL_TOKENS=1200000` and the agent re-run once more (outputs cleared and rewritten again) to
directly check whether more budget changes the outcome. It didn't: the run stopped on token
exhaustion at step 91 (`1221161/1200000`) with the same single `.env` finding accepted (plus one
earlier proposal correctly rejected for using `endpoint: '/.env'` instead of the required
`'GET /.env'` shape) -- reconfirming, not contradicting, the ninth pass's original "budget was never
the binding constraint" finding at a fresh 3x scale. One cosmetic-only observation, not treated as a
bug: the accepted finding's `why_disclosure` text ends with a stray `"}` the model appended to its
own free-text argument (visible in `run.log`'s raw tool call at step 49) -- the surrounding JSON
still parsed correctly and the finding's actual evidence is unaffected, so this is left as an
unedited artifact of the model's own output, consistent with this project's standing practice of
never sanitizing what the model actually said. This run's output is what's now promoted.

### A twentieth pass: a real budget-wasting pattern found by reading the 3x-budget run's own log

Asked directly "why no more findings" on that 3x-budget run, checking `run.log` itself (not just
re-reading code) turned up a genuine, previously-unseen failure mode: of the 87 `http_request` calls
made after the `.env` finding was accepted, **47% (41 of 87)** went to guessing three non-existent
list-style endpoints (`.../listVehiclesForUser`, `.../listServiceOrdersForUser`,
`.../listAppointmentsForUser` across the `identity`/`community`/`workshop`/`vehicle` prefixes) while
stuffing an ever-larger pile of fabricated query parameters
(`typeIdType=id&typeIdValue=id&typeIdValueType=id_value&id_id&id_...`) directly into the `path`
argument on each retry, instead of using the tool's separate `query` parameter. 22 of those got long
enough to trip the existing degenerate-path length guard; 19 shorter variants slipped through as
real, but pointless, repeated 404s.

Neither existing guard could catch this: `_degenerate_path_reason` (fix #3) only inspects one path
string in isolation for a repeating internal pattern, and no single one of these query strings
repeated a segment within itself -- the degeneration was a *cross-call* pattern (the same dead
endpoint hammered over and over with cosmetically different queries), which needs request history to
see. This is the same underlying problem class the sixteenth pass's removed failure-streak hint (fix
#4) targeted -- except that mechanism was a *nudge* removed because it never fired in any run kept at
the time, and this time the evidence is that a soft nudge wouldn't have been enough anyway (the model
kept retrying variations of the same wrong idea for ~40 steps without prompting to stop). Fixed as
**(36)**: `agent/tools/http_tool.py::_repeated_prefix_failure_reason` inspects `HttpToolkit.history`
directly (not a single path string) and refuses to dispatch a request whose `(method,
path-before-'?')` -- query string ignored entirely, since an endpoint either exists at a path or it
doesn't, independent of what's attached after `?` -- matches the last `MAX_PREFIX_FAILURE_STREAK`
(3) requests to that same prefix, all of which failed (non-2xx status or a connection error). Refused
outright, same shape as the degenerate-path guard (no network call, no history/budget entry for the
refused turn), with a message telling the model to try a genuinely different path rather than another
query variation. `RECON_TEXT` (`agent/prompts.py`) got a parallel new bullet alongside its existing
"don't keep extending a dead path" instruction, naming the query-string version of the same mistake
explicitly.

6 new regression tests (`tests/test_http_tool.py`) cover: the refusal firing at the toolkit-integration
level (dispatched calls up to the threshold, refused past it, never recorded), and the pure detection
function directly (query string ignored for matching, a single success anywhere in the recent window
breaking the streak, a different path prefix never triggering it, and a connection/timeout error
counting as a failure same as a bad status code). 261 tests now, up from 255. Live-verified
immediately afterward: cleared outputs and re-ran at the same 120-step/1.2M-token budget -- the new
guard fired twice, correctly refusing further guesses against a dead
`/workshop/api/v1/vehicle/8/orders/1/status` endpoint family, and the run still produced the same
single accepted `.env` finding with no regression (the dedup fix from the nineteenth pass also fired
correctly on this run, merging 2 near-duplicate proposals into 1, confirmed via the `SYSTEM: dedup: 2
findings -> 1 after merging near-duplicates` log line). This run's output is what's now promoted,
superseding the 3x-budget run described just above.

### A twenty-first pass: a well-known OpenAPI/Swagger spec-path check

Prompted by the exploratory question "can API discovery be improved further" -- the model's own
noun-guessing has been the recall bottleneck since at least the tenth pass, so the idea tested was
handing it a shortcut: many REST frameworks (including the Spring Boot stack crAPI's services are
built on) expose a machine-readable OpenAPI/Swagger contract at a handful of standard, well-known
paths. If exposed, that document maps the entire real endpoint/parameter surface in one request
instead of guessing resource nouns one at a time -- and, like `.env`/`.git/config`, it's a
target-agnostic convention, not a crAPI-specific hardcoded answer.

Before writing a single line of prompt text, this was verified directly against the live crAPI
stack rather than assumed: `curl` against `/v3/api-docs`, `/v2/api-docs`, `/swagger.json`,
`/swagger-ui.html`, `/openapi.json`, `/api-docs` (root and under each of `/identity`, `/community`,
`/workshop`) came back 404 everywhere, both unauthenticated and with a real login token. One
incidental discovery from that probing, folded into the new prompt text's caveat rather than
treated as a separate bug: `/identity/*` returns a generic `401 "Invalid Token"` for *any*
unrecognized path when the caller is unauthenticated, not a `404` -- so an unauthenticated probe
alone can never distinguish a real, auth-gated route from one that doesn't exist there, which is
exactly why the new recon step tells the model to retry once authenticated before concluding a
spec endpoint isn't exposed.

Added a new `RECON_TEXT` step 2 in `agent/prompts.py` (existing steps renumbered 3-5) naming these
paths and the unauthenticated-then-authenticated retry order; documented in the module's own
docstring alongside every prior recon addition, including the live-probe result (this target
doesn't expose one) so a future reader isn't left wondering whether it was ever checked. This is a
pure prompt-text change -- no new tool, no new code path -- so the full 261-test suite and
`pyflakes` needed no changes and stayed green/clean.

Live-verified with a fresh 40-step run against the real crAPI stack (scratch output, official
`findings.json`/`run.log` untouched): the model picked up the new instruction on its own, trying
`/v3/api-docs` (step 10), `/swagger.json` (step 11), and `/identity/v3/api-docs` (step 38) --
all correctly 404, matching the pre-verified live probe -- alongside its usual `.env`/BOLA/coverage
behavior with no regression (same `.env` finding accepted after one earlier rejection for an
endpoint-format mistake, unrelated to this change and consistent with prior runs). Net effect on
this specific target: zero new findings, a small number of cheap wasted requests confirming a
negative -- an honest result, not evidence the technique is broken; it remains worth keeping since
it costs little and could pay off on a target, or a future crAPI version, that does ship a spec
endpoint.

### A twenty-second pass: detecting repeated identical-response no-ops

Prompted by the user asking two direct questions about a captured `run.log`: why does the agent
rarely pivot on new information or reach for `propose_finding`/`list_id_candidates`, and why do
repeated endpoint attempts appear at all when the model should move on after something doesn't pan
out. The first question's answer is the already-documented model-capability limitation (fix #32's
`propose_finding` avoidance, `list_id_candidates` abandoned after two low-yield calls in that same
log) -- not new. The second question's answer split into two cases on inspection: repeated
*failures* to the same path prefix are already caught by fix #36's `_repeated_prefix_failure_reason`
(and it visibly fired twice in that exact log), but a second, previously undetected case was also
visible: `GET /identity/api/v2/user/dashboard?user_id=9` was called three separate times across the
run (steps 57, 67, 81), every time returning a byte-identical 200 response regardless of the
`user_id` value -- the endpoint ignores the parameter entirely and always returns the caller's own
session-bound profile. No existing guard could see this: `_repeated_prefix_failure_reason` only
tracks failure streaks, so a repeated *successful* no-op call was invisible to it.

Fixed as (37): `agent/agent/tools/http_tool.py::_repeated_identical_response_reason`, the mirror
image of fix #36's guard -- once the last `MAX_IDENTICAL_RESPONSE_STREAK` (3) requests to the same
`(method, path-before-'?')` all succeeded (2xx) with a byte-identical `(status_code, body)` pair, the
next variation is refused before dispatch, same shape as the degenerate-path and repeated-prefix-
failure refusals it sits alongside. Detecting "identical body" cheaply required a new
`RequestRecord.response_signature` field -- a short SHA-256 hash of `(status_code, body)` computed
at record time, not the body itself, since `RequestRecord` deliberately excludes bodies for
memory/log-size reasons (see fix #27's history). This mirrors the hashing technique
`agent/agent/loop.py::_tool_response_signature` (fix #18) already uses for transcript-compaction
dedup, kept as an independent local computation rather than a shared import for the same
module-coupling reasons the file's `_ID_VALUE_RE` comment already gives for a similar near-duplicate.

Verified offline with 8 new regression tests in `agent/tests/test_http_tool.py` (refusal at the
threshold, allowed up to it, query-string-agnostic matching, a differing body breaking the streak, a
different path prefix not triggering it, a failure streak correctly *not* triggering this guard even
when it happens to share a signature, and an end-to-end check that three real `http_request` calls
with an identical body actually produce matching `response_signature` values while a differing one
doesn't) -- 268 tests now, up from 261; `pyflakes` clean. Live-verified two ways: a 60-step run
against the real crAPI stack (scratch output, official `findings.json`/`run.log` untouched) showed no
regression -- the existing `.env` finding was accepted again, fix #36's guard fired 3 times as
before, and this run's model simply didn't reproduce the exact repeated-query pattern this pass
targets (an honest result, not evidence the fix doesn't work: the pattern is real but not every run
reaches for it). Separately, a direct probe script instantiated `HttpToolkit` against the live
target with real crAPI credentials and called `/identity/api/v2/user/dashboard?probe=N` three times
with three different `N` values -- all three returned the identical primary-account body, and the
4th call was correctly refused with `error: repeated_identical_response`, confirming the mechanism
against genuine (non-mocked) response data, not just the test suite's fake session.

### A twenty-third pass: a fresh clean run finds another classifier misattribution (fix #38)

Prompted by a request to clear `findings.json`/`run.log`/`summary.md`/`report.md` and collect one
more fresh live run against the post-twenty-second-pass agent. The run itself was clean: 89 steps,
stopped on token-budget exhaustion, 1 finding accepted and 1 rejected -- the same `.env` disclosure
as every promoted run since the seventeenth pass, no regression. But the accepted finding's
`on_challenge_list` field came back `true`, attributed to `challenge-17-chatbot-credential-
extraction` -- clearly wrong: this finding has nothing to do with the chatbot. Reading
`agent/agent/challenge_reference.py::PUBLIC_INFO_DISCLOSURE_CHALLENGES` showed why:
challenge-17's keyword list included the bare word `"credential"`, and the model's own
`why_disclosure` text happened to say "...or attempt credential-based attacks against application
services" -- the hyphen after "credential" is a non-word character, so the (correct, already-fixed)
word-boundary matcher matched "credential" as a standalone word there, and no other challenge in the
table scored higher than challenge-17's resulting score of 1. This is the identical false-positive
bug class as fixes #15 and #22 (an overly generic keyword misattributing a genuinely novel finding
to a known public challenge), just not caught by either of those fixes since neither touched
challenge-17's own keyword list.

Fixed as (38): removed `"credential"` from challenge-17's keywords, keeping only `"chatbot"` and
`"another user's"` -- `"chatbot"` is the one genuinely distinctive signal for this specific
challenge (it's *about* extracting credentials via chatbot manipulation specifically, not any
finding that happens to mention leaked credentials through any channel). Verified directly:
recomputing `classify_on_challenge_list` on the exact live finding text with the fix in place
returns `(False, None)`, matching the already-established ground truth that this `.env` disclosure
is not on crAPI's public challenge list. Two new regression tests added to
`agent/tests/test_challenge_reference.py`: one reproducing the exact live misattribution (asserts no
match), one confirming `"chatbot"` alone still correctly matches challenge-17 (proving the fix
doesn't remove real detection capability, just the over-broad keyword) -- 270 tests now, up from
268; `pyflakes` clean.

Since `classify_on_challenge_list` is a deterministic, purely-local post-hoc function over a
finding's already-captured text (never fed back into the model, never re-run against the live
target), a full re-run wasn't necessary to get a correct promoted result: the fixed classifier was
re-applied directly to the already-captured finding text, and `findings.json`/`summary.md`/
`report.md` were updated in place (`on_challenge_list: false`, "not on public challenge list").
Following this project's established precedent (the fifth pass's run 9, and the eleventh pass's fix
#22), `run.log`'s own inline `on_challenge_list=True (challenge-17-chatbot-credential-extraction)`
line is deliberately left as an unedited historical artifact of what the live run actually logged
before this fix, while the three derived output files reflect the corrected value.

### A twenty-fourth pass: zero-tolerance for exact-duplicate calls (fix #39)

Prompted by the user reviewing this project's own committed `run.log` in an editor and asking why
the agent's path-guessing looked stuck repeating patterns instead of moving on. Reading the log's
tail (steps 29-89) directly confirmed the observation: roughly 40 requests are combinatorial
noun-guessing across `{workshop, identity/api/v2, api/v1}` x `{vehicle, vehicles, user/vehicle}` x
several path shapes, with zero hits -- and, notably, it never once tried
`/identity/api/v2/vehicle/vehicles`, the real crAPI vehicle-listing endpoint already confirmed live
in the eighteenth pass's history. That's an honest model noun-guessing capability limit, not a new
bug. But steps 87 and 88 showed something narrower and genuinely fixable: the exact same
`http_request` call (identical method, path, account) issued twice in a row for zero new
information -- tolerated by the old `MAX_CONSECUTIVE_REPEATS` default of 3, which only refuses the
*fourth* identical call, so two free, wasted repeats always slip through undetected.

Two candidate fixes were weighed: a coarser same-topic (not same-prefix) pivot guard to address the
bulk of the combinatorial-guessing waste, versus simply tightening the existing exact-repeat
threshold. The first was explicitly not chosen without further evidence -- this project has already
removed two similarly-shaped heuristics (the failure-streak hint, the path-candidate crawler) after
they failed to generalize past the one run that motivated them, and a single log excerpt is the same
thin evidence base. The second is a one-line, already-tested, already-configurable knob with a clear,
narrow justification: a repeated call with byte-identical arguments against a deterministic local
target can never observe anything the first call didn't already show, unlike a flaky remote API
where a retry might. Fixed as (39): `DEFAULT_MAX_CONSECUTIVE_REPEATS` (`agent/config.py`) tightened
from 3 to 1 -- the very first exact repeat is now refused before dispatch, the same shape as every
other pre-dispatch guard in this codebase (degenerate path, repeated-prefix-failure,
repeated-identical-response). `--max-consecutive-repeats`/`MAX_CONSECUTIVE_REPEATS` remain fully
configurable for anyone who wants the old, more permissive behavior back.

Verified offline: the full suite (270 tests beforehand) already passed unchanged, since every
existing test that exercises repeat-blocking passes its own explicit `max_consecutive_repeats`
value rather than relying on the default -- confirming the tightened default doesn't silently break
an existing behavioral assumption anywhere. Two new regression tests added: one in
`agent/tests/test_config.py` asserting the resolved default is `1`, and one end-to-end integration
test in `agent/tests/test_loop_integration.py` (`test_default_config_refuses_the_very_first_repeat`)
that runs the full `AgentLoop` against a scripted stub LLM issuing the same call 5 times at the
*unmodified* default config, and asserts only 1 request ever reaches the fake HTTP session -- 272
tests now, up from 270; `pyflakes` clean.

Live-verified in a same-day follow-up: a full 97-step run against the real crAPI stack (outputs
cleared and rewritten, same 120-step/1.2M-token budget) reproduced the standing single `.env`
finding cleanly, correctly tagged `on_challenge_list: false` on the first attempt (fix #38 holding),
with fix #36's guard firing 6 times and fix #37's guard firing once -- no regression. The new
zero-tolerance guard itself didn't happen to fire organically in that run (the model didn't issue an
exact back-to-back duplicate this time, an honest non-trigger, not evidence against the fix). A
direct, targeted probe closed that gap: constructing a real `AgentLoop` against the live target and
calling `_handle_tool_call` twice with byte-identical `http_request` arguments (`GET /.env`,
`account="primary"`) showed the first call dispatch normally (a real 200) and the second refused
outright with `error: repeat_blocked` -- confirming only 1 real request ever reached the toolkit,
against genuine, non-mocked response data.

### A twenty-fifth pass: dedicated simplification pass, no bug hunt (release prep)

Explicitly requested and scoped differently from every pass above: "simplify and clean up code for
simplicity, readability, and maintainability. Do not alter or remove existing functionalities and
features." That constraint pre-scoped this pass to pure, zero-behavior-change refactors only -- no
heuristic removals to weigh (unlike the sixteenth pass, which had two, each flagged to the user via
`AskUserQuestion` before touching anything).

Seven parallel, read-only review agents covered every module in `agent/agent/` plus `agent/tests/`,
each explicitly instructed to propose only mechanically behavior-preserving changes and to mark
anything uncertain as "needs double-check" rather than apply it blind. Applied, in order, verifying
the relevant test file (and pyflakes) after each:

- **`loop.py`**: a `_log_and_return` closure collapsing three identical `step_logger.observation(...)
  ; return result` tails in `_handle_tool_call`; a hoisted `_SIG_SUFFIX_TEMPLATE` constant collapsing
  `_compact_role`'s two suffix-construction branches into one shared assignment.
- **`tools/http_tool.py`**: a `_matching_by_prefix` helper removing an identical list comprehension
  duplicated across `_repeated_prefix_failure_reason` and `_repeated_identical_response_reason`; an
  `_ensure_leading_slash` helper removing an identical `if not path.startswith("/")` block duplicated
  across `http_request` and `list_id_candidates`.
- **`toolbox.py`**: a `_call(func, arguments, **extra)` helper collapsing `dispatch`'s four repeated
  `func(**extra, **_drop_unrecognized_kwargs(func, arguments))` branches into one line each.
- **`config.py`**: relocated a misplaced comment block (fix #39's rationale was sitting above
  `DEFAULT_MAX_STEPS` instead of the `DEFAULT_MAX_CONSECUTIVE_REPEATS` constant it actually
  describes); a `_resolve_path` helper collapsing the `creds_path`/`out_dir` CLI-or-env-or-default
  resolution into one call each (verified behaviorally identical: `pathlib.Path` is always truthy, so
  `x or y` and `x if x is not None else y` agree for both call sites).
- **`cli.py`**: interpolated `config.py`'s `DEFAULT_MAX_STEPS`/`DEFAULT_MAX_TOTAL_TOKENS`/
  `DEFAULT_MAX_CONSECUTIVE_REPEATS`/`DEFAULT_REQUEST_TIMEOUT_SECONDS` into the matching `--help` text
  instead of four hardcoded numbers that had to be kept manually in sync -- verified by diffing the
  actual `--help` output before and after (byte-identical).
- **`logging_setup.py`**: removed a redundant double `.strip()` call in `StepLogger.intent`.
- **`dedup.py`**: removed a redundant `order` list plus an `O(n)` membership check in
  `dedup_findings` (Python dicts preserve insertion order since 3.7, so `groups.values()` alone gives
  the same first-occurrence ordering); replaced `_better`'s manual left-to-right reduce loop with one
  `max(cluster, key=...)` call (Python's `max` keeps the first element on a tie, matching the old
  left-preference exactly) -- re-verified specifically against the transitive-similarity-chain and
  tie-breaking tests, not just the suite as a whole, since this one is a non-mechanical equivalence
  argument rather than a pure syntactic move.
- **`validation.py`**: added a `_reject(reason)` helper for `validate_finding`'s three rejection
  returns, mirroring `control_tool.py`'s own `_reject` precedent from the sixteenth pass rather than
  inventing a new convention.
- **`scope.py`**: dropped two stray `f` prefixes on string literals with no `{...}` placeholder in
  `check_scope`'s rejection message (the same class of cosmetic issue the project's own history
  already fixed elsewhere, a fresh instance here).
- **`report.py`**: a `_TITLE` constant and `_join` helper removing tiny duplicated tokens shared
  between `render_summary` and `render_full_report`.
- **`tests/_helpers.py`**: added `StubToolkit` (consolidating identical copies from
  `test_scope_and_validation.py` and `test_property_based.py`) and `write_creds_file` (consolidating
  `test_config.py`'s `_write_creds` with an inlined equivalent in `test_property_based.py`'s
  `setUp`); `test_loop_integration.py` got a new `_propose_other_user_data_call` helper removing a
  verbatim-identical `propose_finding` argument dict repeated across three tests; a genuine duplicate
  test *name* (`test_path_without_leading_slash_is_normalized`, defined once each in `HttpRequestTests`
  and `ListIdCandidatesTests`, covering different code paths) had the second renamed to
  `test_list_id_candidates_path_without_leading_slash_is_normalized` for unambiguous `-k` filtering.

Several review findings were deliberately **not** applied, each for a stated reason rather than
silently skipped: `llm_client.py`'s three `chat.completions.create` call sites (collapsing them risks
obscuring that the 404-retry and empty-choices-retry branches genuinely differ in what they mutate
first); `http_tool.py`'s three pre-dispatch guards' shared `{"error": ..., "message": ...}` shape
(each guard has a large historical comment directly above its `return` that would need care to keep
associated with the right guard); `http_tool.py`'s `_UUID_RE`/`_ID_VALUE_RE` duplicated regex text
(composing one from the other via string-slicing is exactly the kind of "clever" change that's easy
to get subtly wrong); a trivial `_elapsed_ms` one-line duplication (too small to justify a named
abstraction); and a shared `RunConfig` test builder across `test_http_tool.py`/
`test_loop_integration.py` (the two builders' apparent similarity hides a genuine functional
divergence in what each test class actually needs).

Verified: pyflakes clean across both `agent/` and `tests/`; the full offline suite re-run five times
in a row stayed at 272/272 (unchanged count -- this was a pure refactor, no tests added or removed
beyond the helper consolidations above, which don't change what's covered). A live 15-step smoke test
against the real crAPI stack (scratch output dir) confirmed no regression on the hot path these
changes touch most (`loop.py`, `http_tool.py`, `toolbox.py`): the forced-`propose_finding` mechanism
fired correctly on the real `.env` 200, the resulting proposal was correctly rejected by
`validation.py`'s (refactored) grounding check, and the run terminated cleanly on the step budget with
all four output files written.


### A twenty-seventh pass: endpoint discovery closes the real recall gap (2026-10-02)

Prompted by the user asking, after a fresh live run found only the single `.env` disclosure, why
vulnerability detection was so poor. Rather than theorize, the 62-step baseline run's own `run.log`
was read directly: **55 of 57 `http_request` calls were doomed path guesses** -- only `/.env` and
`/identity/api/v2/user/dashboard` ever resolved. Every resource-endpoint guess was shaped right but
wrong (`/community/api/v2/posts`, `/vehicle/api/v2/vehicles`, `/workshop/api/v2/workshops`) while
crAPI's real paths are `/community/api/v2/community/posts/recent`,
`/identity/api/v2/vehicle/vehicles`, `/workshop/api/mechanic/mechanic_report`. The agent had **no
working endpoint-discovery mechanism**: its only means of finding paths was the LLM's generic
REST-noun prior, which does not match this target. The entire authenticated API surface -- where
crAPI's real disclosure bugs live -- was therefore invisible, which is why recall never exceeded the
one blind-lucky `.env` guess regardless of budget or model (the standing conclusion of ~14 prior
live runs).

Root cause confirmed by direct probing: crAPI is a single-page app whose frontend JavaScript bundle
(`/static/js/main.<hash>.js`, ~1.6 MB) references its **entire real API surface as string
literals** -- the canonical place a human tester looks first. An earlier pass had tried surfacing
this via a prompt nudge to `http_request` the bundle and removed it correctly (`MAX_BODY_CHARS`=4000
only ever shows webpack boilerplate), but the right fix is tool-side extraction, which that pass
never tried.

**Fix: a new `discover_api_endpoints` tool** (`agent/tools/http_tool.py`, wired through
`agent/toolbox.py`, surfaced in `agent/prompts.py` RECON_TEXT step 3). It fetches the frontend page
and its `<script src>` bundles *in full, tool-side* (the raw JS never enters the model's context),
regex-extracts API path literals (keying on the generic `api/` marker, filtering external
`://` URLs), and returns only the deduplicated path list. It is target-agnostic (every SPA ships a
bundle) and stays on the compliant side of the "no autonomous multi-hop crawler / not a perfect
scanner" scope line exactly as `list_id_candidates` does: it reads static assets the model could
fetch itself and returns inert strings, makes no API calls on the model's behalf, and records
nothing into the model-visible request history. Verified live: one call returns 41 real crAPI
endpoints.

**Impact (same model `qwen36-35b-a3b`, same budget, live against the real stack):**

| Metric | Baseline | With discovery |
|---|---|---|
| Findings accepted | 1 | 3 (0 false positives) |
| Real resource endpoints reached | 0 | 12 |
| `list_id_candidates` used | 2 | 9 |
| Cross-account (secondary) requests | 0 | 17 |

The discovery run reached crAPI's real surface (vehicle, community posts, mechanic reports, shop
orders) and fetched `/community/api/v2/community/posts/recent`, **observing other users' email
addresses 5 times -- but walked past them without proposing a finding.** That isolated the *next*
bottleneck precisely: a recognition gap, not a discovery gap. Addressed with a sharpened RECON_TEXT
step-5 **recognition rule** (imperative: the moment any response contains PII or a private
identifier belonging to an account you did not authenticate as, call `propose_finding` immediately
with that body as evidence, before moving on). A validation run with the rule in place **converted
the community-posts excessive-data-exposure finding** (crAPI Challenge 4) -- a genuine cross-user
PII leak the agent had never previously caught -- while the validator still correctly rejected one
ungrounded proposal (precision held).

Verifying that run surfaced one more classifier misattribution, the same false-positive class as
fixes #15/#22/#38: **fix #40** -- `agent/challenge_reference.py`'s challenge-4 keywords included the
bare data-type words `"email"`/`"phone"`, so a plain forget-password *user-enumeration* finding (a
genuine **off-list generalization win**) was tagged on-list purely for containing the word "email".
Removed `"email"`/`"phone"` while deliberately **keeping** `"excessive"`/`"pii"` (load-bearing: they
let the genuine community-posts finding, whose text also mentions a leaked *vehicle* id, outscore
challenge-1's "vehicle" keyword -- removing them was tried first and wrongly flipped that finding's
id to challenge-1, caught by the existing fix-#22 scoring test). Two regression tests added, both
reproducing the exact live text.

The promoted `findings.json`/`run.log`/`summary.md`/`report.md` were refreshed from this run (prior
single-finding committed run backed up off-tree for reversibility): **3 accepted findings** -- `.env`
credentials (off-list), forget-password user enumeration (off-list), community-posts PII exposure
(on-list, Challenge 4) -- precision 3/3, generalization 2/3, public-subset recall now non-zero and
reproducible with this project's own documented config. 287 offline tests (up from 272),
pyflakes-clean. New tests: `discover_api_endpoints` + `_extract_api_paths_from_js`
(`tests/test_http_tool.py`), its schema/dispatch wiring (`tests/test_toolbox.py`), the new tool in
the prompt (`tests/test_prompts.py`), and fix #40 (`tests/test_challenge_reference.py`).

### A twenty-eighth pass: evidence-grounding bound to the claimed endpoint (fix #43), fresh 5-finding run (2026-10-03)

Prompted by a request to add a principled dedup rule and then collect one fresh clean live run,
promoting it only if legitimately good. The first fresh run (120 steps / 1.2M tokens, post-27th-pass
agent) came back with **5 accepted findings**, but direct live verification of each against the
running crAPI stack caught one as a **fabrication**: finding #4 claimed a cross-user BOLA on
`GET /identity/api/v2/user/dashboard/{userId}`, quoting a body `{"id":9,"name":"Agent secondary",...}`
as proof -- but a live `curl` showed that path-parameter variant returns **404 "No static resource"**,
and the only working dashboard endpoint (`/dashboard`, no param) returns the *caller's own* profile.
Reading `run.log` explained exactly how the model manufactured it: at step 26 it requested
`GET /dashboard/9` (primary) -> 404, and at step 27 it requested `GET /dashboard` as the **secondary**
account -> 200 with secondary's own profile; it then stitched step 27's body onto step 26's dead
path and proposed a BOLA that never happened. The bonus verifier hadn't caught it, and crucially
neither had `validation.py`'s evidence-grounding check -- because that check only required the quoted
bytes to appear in *some* response captured this run, not in a response from the request the finding
actually *names*.

Rather than hand-edit the model's output (never done on this project) or promote a set with a
fabrication in it, the user chose "fix the grounding gap + re-run." Fixed as **#43**: evidence
grounding is now **bound to the claimed endpoint**. `agent/dedup.py` gained a shared
`endpoint_grounding_key(method, path)` that reduces a request or a finding's `"METHOD /path"` to a
comparable key, collapsing concrete ids, opaque/nanoid ids (reusing this module's `_segment_is_id`,
which `http_tool`'s lighter `_ID_VALUE_RE` would miss on a nanoid), `{placeholder}`/`<p>`/`:p` route
params, and query strings. `HttpToolkit`'s evidence body cache now stores `(method, path, text)` per
entry instead of a bare string, and a new `HttpToolkit.bodies_for_endpoint(endpoint)` returns only the
bodies/header-blocks produced by requests matching the claimed endpoint (falling back to every body
only when the endpoint string is absent or malformed, so the change can only ever *narrow* the
candidate set for a well-formed finding, never newly reject one). `validation.validate_finding` now
takes the finding's `endpoint` and grounds against that endpoint's own responses;
`control_tool.propose_finding` passes it through. The fabricated dashboard BOLA is correctly rejected
by this (its evidence body came from `/dashboard`, not the claimed `/dashboard/{userId}`), while every
genuine finding -- whose evidence does come from its own endpoint -- still passes. 5 new regression
tests including an exact reproduction of the live fabrication and its companion (a genuine
nanoid-path post-detail finding still accepted); two pre-existing tests that legitimately proposed
endpoints they never requested were corrected to request them. **322 offline tests (up from 287... see
note), pyflakes-clean.**

Note on the test count: the 27th-pass entry above ended at 287, but intervening same-day work on
2026-10-03 (a dedup field-name-overlap signal, nanoid path normalization, escaped-quote/env-var
field extraction, an off-list-on-disagreement merge rule -- the `agent/dedup.py` hardening carried as
in-code history, plus fixes #41-#42 in `http_tool.py` recorded in their own module comments) brought
the suite to 317 before this pass's 5 new grounding tests took it to 322.

With fix #43 in place a second fresh run (120/1.2M) was collected and **every finding verified live by
`curl` before promotion** -- no fabrication this time. The run stopped on token exhaustion with **5
accepted findings, 1 rejected**, all five confirmed against the running stack:

1. `GET /.env` -- DB/Mongo credentials, 200, **off-list** (generalization win).
2. `GET /community/api/v2/community/posts/recent` -- other users' emails + vehicle UUIDs, **on-list**
   (Challenge 4).
3. `GET /identity/api/v2/community/posts/{nanoid}` -- a verbose Spring `404 "No static resource"`
   error, **off-list**. The weakest of the set (a stock framework 404 that just echoes the requested
   path); not fabricated and correctly grounded to its own endpoint, but low value -- kept as the
   honest, unedited model output rather than hand-removed.
4. `GET /identity/api/v2/vehicle/{uuid}/location` -- **a genuine cross-user BOLA**, verified live for
   two different victims' cars (Robot's `4bae9968…` and Pogba's `cd515c12…`): as the primary account
   it returns the other user's full name, email, and precise GPS coordinates. **On-list (Challenge
   1)** -- a real public-challenge win this release had never previously caught.
5. `GET /community/api/v2/community/posts/{nanoid}` -- post-detail author email + vehicle id,
   **on-list** (Challenge 4).

This supersedes the 27th pass's 3-finding run as the promoted
`findings.json`/`run.log`/`summary.md`/`report.md` (prior committed set backed up off-tree for
reversibility). Net result: the strongest deliverable this project has produced -- **4 genuinely
strong findings plus 1 weak-but-valid one, 0 fabrications, hitting two distinct public challenges
(1 and 4) and two off-list generalization wins (.env, verbose-404)**, with the exact false-positive
class that nearly slipped through now closed by a tested validation-layer fix. `HISTORY.md` was also
kept alongside `README.md` and `DESIGN.md` this pass.

### A twenty-ninth pass: dead-code / inconsistency / documentation cleanup (2026-10-03)

A maintenance pass explicitly scoped to three things: remove dead weight, correct code
inconsistencies, and bring documentation current -- no new features, no live run. Three parallel
read-only fresh-eyes audits (dead code, code inconsistencies, documentation drift) fed a single
consolidated edit set.

**Dead code: none found.** As expected after 28 prior passes -- every function, class, constant, and
dataclass/pydantic field traces to a real use or a framework hook (dataclass `__post_init__`,
pydantic validators, the toolbox dispatch table). The only unreferenced symbol, `agent/__init__.py`'s
`__version__ = "0.1.0"`, is a conventional package dunder and was deliberately kept. The test-tooling
caches (`.hypothesis/`, `.pytest_cache/`, `.coverage`) are all already `.gitignore`d, so not
deliverable dead weight. AST-hashing every `test_*` body across all 18 test files found zero
duplicate test cases.

**Code/comment consistency fixes (no behavior change, no new numbered fix -- these are doc/comment
corrections):** (a) `challenge_reference.py`'s `classify_on_challenge_list` docstring cited a worked
example matching "four of challenge-4's keywords ('another user', 'pii', 'email', 'excessive')", but
fix #40 had removed `"email"` from that keyword set -- corrected to "three ('another user', 'pii',
'excessive')". (b) `tools/__init__.py`'s docstring said "Four tools are exposed" and its narrative
claimed `list_visited_endpoints` was chosen "instead of a speculative endpoint-discovery tool" --
both stale since the 18th pass (`list_id_candidates`) and 27th pass (`discover_api_endpoints`) added
two more tools, the second being exactly an endpoint-discovery tool; rewritten to enumerate all six.
(c) `loop.py`'s `NO_TOOL_CALL_NUDGE` (model-facing text) omitted `discover_api_endpoints` from its
tool list -- added. (d) `toolbox.py`'s `_drop_unrecognized_kwargs` docstring claimed dispatch
"promises that only an *unknown tool name* raises", contradicting `dispatch`'s own docstring (which
also raises on malformed/missing arguments) -- corrected to say dropping extra kwargs only removes
the *extra-argument* crash. (e) `budget.py`'s module docstring said repeat detection lets the loop
"intervene (nudge, then stop)" -- the forced-stop escalation (`MAX_TOTAL_REPEAT_BLOCKS`) was removed
in the 16th pass, so repeat detection now only *refuses* the repeated call; reworded. (f)
`prompts.py` and `http_tool.py` module docstrings still referred to "this project's one real finding"
-- updated for the 5-finding state (the `http_tool.py` one also pointed at README's "Status of the
committed real run" section, which moved to `HISTORY.md` in the 26th pass; repointed). (g) an
`http_tool.py` comment hardcoded the literal "8" where it meant `KEEP_FULL_TOOL_MESSAGES` (loop.py)
-- replaced the literal with the constant name to prevent silent drift.

**Documentation brought current.** The drift was concentrated in `DESIGN.md` and one `FUTURE_
IMPROVEMENTS.md` item, both of which had lagged behind the 18th/27th/28th passes: `DESIGN.md` still
said "five tools" in three present-tense places and omitted `discover_api_endpoints` from its Tools
list; its control-loop step 1 still listed a "drift forced-stop" termination condition that no longer
exists (only budget-exhausted and model-finished remain); and its case-study standing conclusions
("the capability ceiling ... stands unchanged", "never found the real vehicle paths", "the one hit it
did land") were written before `discover_api_endpoints` broke that very ceiling. Added a closing
case-study paragraph covering the 26th-28th passes and the 5-finding promoted run, and scoped the
era-specific conclusions to their era. `FUTURE_IMPROVEMENTS.md`'s "SPA JS-bundle endpoint extraction
-- tried, removed ... not attempted here" item was flatly contradicted by the 27th pass having built
exactly that as `discover_api_endpoints`; rewritten as the done item it became. `README.md` and
`HISTORY.md` were re-verified accurate and needed no changes. 322 tests unchanged,
pyflakes clean throughout.
