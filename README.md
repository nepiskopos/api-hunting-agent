# Information-Disclosure Hunting Agent

An autonomous LLM-driven agent that probes a running [OWASP crAPI](https://github.com/OWASP/crAPI)
instance and reports **information-disclosure findings only** -- verbose errors, leaked
secrets/PII, excessive data exposure across accounts, exposed internal endpoints -- via a genuine
reason -> act -> observe loop, not a hardcoded checklist or a vulnerability scanner. It decides for
itself what to probe, what counts as a disclosure, and when to stop.

**Documentation map:** this file (what it is, how to run it) -- [`DESIGN.md`](DESIGN.md)
(architecture and a condensed case study) -- [`HISTORY.md`](HISTORY.md) (the full, unabridged
build log: every live run, every bug found and fixed, in order) --
[`FUTURE_IMPROVEMENTS.md`](FUTURE_IMPROVEMENTS.md) (in-scope opportunities not yet built, plus
ideas deliberately left out of scope).

## How it was built

The agent is a plain tool-calling loop around an OpenAI-compatible chat-completions endpoint: each
turn, the model sees the system prompt plus the running transcript and must emit exactly one tool
call (`http_request`, `discover_api_endpoints`, `list_visited_endpoints`, `list_id_candidates`,
`propose_finding`, or `finish_investigation`); the loop dispatches it, appends the observation, and repeats until the
model finishes or a step/token budget is hit. No part of the target's API surface is hardcoded --
the model is told the three top-level prefixes and the account labels available and has to explore
from there.

It was built and hardened iteratively against the real running target rather than designed
up front and shipped once: an initial build, followed by twenty-five review passes (each either a
live run analyzed for a real bug, a fresh code-reading review, or a deliberate change in
verification technique -- property-based fuzzing, coverage-driven gap-closing, a model swap). That
process found and fixed 39 real bugs, several of which only manifested against the live target
(token-budget exhaustion, repeat-detection gaps, a silently-dropped-tool-call failure mode in the
underlying model). The full account -- what broke, why, and what was deliberately *not* changed --
lives in [`HISTORY.md`](HISTORY.md); the architecture rationale lives in [`DESIGN.md`](DESIGN.md).

## Quickstart (< 5 minutes, assuming crAPI is already running)

```bash
cd agent
pip install -r requirements.txt

cp .env.example .env
# edit .env: fill in LLM_API_KEY at minimum (see .env.example for every setting)

python -m agent --creds creds.json
```

`.env` is loaded automatically (via `python-dotenv`) if present in the current directory --
see [Configuration](#configuration-env-file--cli-flags) below for every setting and its
precedence. Everything also works with plain environment variables / CLI flags instead of a
`.env` file, e.g.:

```bash
export LLM_BASE_URL="https://spark.cyrextech.net:8881/v1"
export LLM_API_KEY="<your key -- never commit this>"
python -m agent --target http://localhost:8888 --creds creds.json
```

That's it. On completion you'll have, in the current directory: `findings.json`, `run.log`,
`summary.md`, and `report.md` (see [Outputs](#outputs) below).

## Setting up the target (crAPI)

```bash
git clone https://github.com/OWASP/crAPI.git
cd crAPI/deploy/docker
docker compose -f docker-compose.yml --compatibility up -d
# Web UI / API gateway: http://localhost:8888   |   MailHog: http://localhost:8025
```

Developed and tested against crAPI commit `73d309cc8f28bbdeed31dbb35f05dba8354de3c9`. Wait until
`docker compose ps` shows all services healthy (identity, community, workshop, chatbot, web,
postgres, mongo, chroma, mailhog, gateway) -- typically 1-2 minutes.

**Creating accounts.** This crAPI version does not actually require email verification to log in
(confirmed by reading `UserRegistrationServiceImpl.registerUser` in the crAPI source: the account
is created `ACTIVE` immediately; the "welcome" email is informational only, not a confirmation
step). So accounts can be created directly via the signup API, no MailHog UI steps needed:

```bash
curl -s -X POST http://localhost:8888/identity/api/auth/signup -H "Content-Type: application/json" \
  -d '{"name":"Agent Primary","email":"agent.primary@example.com","password":"ChangeMe123!","number":"5551110001"}'
curl -s -X POST http://localhost:8888/identity/api/auth/signup -H "Content-Type: application/json" \
  -d '{"name":"Agent Secondary","email":"agent.secondary@example.com","password":"ChangeMe123!","number":"5551110002"}'
```

(The project's own text mentions registering via the UI + MailHog; that still works too, if you
prefer it or are on an older crAPI version that does enforce verification.)

## LLM used

Default target: Cyrex-hosted OpenAI-compatible vLLM endpoint, model **`qwen36-35b-a3b`** (confirmed
live via `GET /v1/models` at the start of every run -- the agent re-checks this itself and will use
whatever is actually being served; see `agent/llm_client.py`). It's a reasoning model, so
`max_tokens` is set generously (2048) per call to leave room for its hidden reasoning trace ahead
of the visible tool call.

The harness is model-agnostic (`LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL` are the only required
config, read from the environment, never hard-coded) -- see [`HISTORY.md`](HISTORY.md)'s
"model-swap experiment" for what happened pointing the same unmodified agent at a different model.

## Expected inputs

- `--target` : crAPI gateway base URL (falls back to `target` in the credentials file if omitted).
- `--creds`  : path to a JSON credentials file. **Format is not prescribed by the spec** --
  this project's chosen schema (see `creds.example.json`):

  ```json
  {
    "target": "http://localhost:8888",
    "accounts": [
      {"label": "primary",   "email": "agent.primary@example.com",   "password": "ChangeMe123!"},
      {"label": "secondary", "email": "agent.secondary@example.com", "password": "ChangeMe123!"}
    ]
  }
  ```

  Each account needs either a `password` (the agent logs itself in against
  `POST /identity/api/auth/login` and caches the resulting token) or a pre-obtained `token`. **Two
  accounts are recommended, one is accepted**: excessive-data-exposure checks (in scope:
  "other users' records inside a list response") require a second identity to prove account A can
  see account B's data; with only one account the agent can still find verbose errors, leaked
  secrets/headers, and exposed internal endpoints, just not that category.

- `LLM_BASE_URL` / `LLM_API_KEY` : environment variables only, **never** a CLI flag or a file --
  this is a hard rule, not a style preference (see [Ambiguities & assumptions](#ambiguities--assumptions-made)).

## Configuration: `.env` file / CLI flags

Every setting the agent needs before it can run is available two ways -- as a CLI flag (for
interactive use) or as an environment variable (for a `.env` file or a container's environment,
e.g. Docker). **CLI flag wins if given; otherwise the environment variable; otherwise the
hard-coded default.** `LLM_BASE_URL` / `LLM_API_KEY` are the one exception: environment-only,
never a CLI flag, so the key can never land in shell history or a committed file.

| Setting | CLI flag | Env var | Default |
|---|---|---|---|
| LLM endpoint URL (required) | *(none -- env only)* | `LLM_BASE_URL` | -- |
| LLM API key (required) | *(none -- env only)* | `LLM_API_KEY` | -- |
| LLM model override | `--model` | `LLM_MODEL` | auto-detect via `/v1/models` |
| crAPI target URL | `--target` | `CRAPI_TARGET` | `target` key in the credentials file |
| Credentials file path | `--creds` | `CREDS_PATH` | `creds.json` |
| Output directory | `--out-dir` | `OUT_DIR` | `.` (current directory) |
| Step budget | `--max-steps` | `MAX_STEPS` | `40` |
| Token budget | `--max-tokens` | `MAX_TOTAL_TOKENS` | `400000` |
| Consecutive-repeat cap | `--max-consecutive-repeats` | `MAX_CONSECUTIVE_REPEATS` | `1` |
| Per-request HTTP timeout (seconds) | `--request-timeout` | `REQUEST_TIMEOUT_SECONDS` | `15.0` |
| Adversarial verifier pass (bonus) | `--no-verifier` (disables) | `VERIFIER_ENABLED` | `true` |
| Semantic de-duplication (bonus) | `--no-dedup` (disables) | `DEDUP_ENABLED` | `true` |

**Validation:** all four numeric hard caps (step budget, token budget, consecutive-repeat cap, and
per-request timeout) and the output directory fail fast at startup with a clean `error: ...` message
on stderr and exit code 2, never a silent misconfiguration or a raw traceback:
`--request-timeout`/`REQUEST_TIMEOUT_SECONDS` must be a finite positive number (rejects `<= 0`,
`nan`, and `inf`); `--max-steps`/`--max-tokens`/`--max-consecutive-repeats` (and their env-var
equivalents) must each be a positive integer (rejects `<= 0`); `--out-dir`/`OUT_DIR` must not name a
path that already exists as a regular file.

Copy `.env.example` to `.env` and fill it in -- it's loaded automatically (via `python-dotenv`)
if present in the current directory when you run `python -m agent`, and is gitignored so it's
never committed. Deeper implementation-detail constants of the anti-drift safeguards (response-body
truncation length, the degenerate-path threshold, the transcript compaction window, etc. -- see
`DESIGN.md`) are deliberately **not** environment-configurable: they are fixed tuning inside one
specific safeguard's implementation, not run parameters a user would reasonably want to pick per
invocation. The consecutive-repeat *cap* and the request *timeout* are different -- they're the
direct parameters behind the project's own "loop/repeat detection" and "cost/loop safety"
requirements, exactly parallel to the step/token caps above, so they get the same CLI-flag/env-var
treatment those do.

## Outputs

| File | What it is |
|---|---|
| `findings.json` | The required structured output: an array of findings matching the spec's schema exactly (title, endpoint, category, evidence, why_disclosure, reproduction, confidence, on_challenge_list). |
| `run.log` | Full step-by-step trace: every model intent, tool call, and observation, plus lifecycle/system events (model resolution, budget/verifier/dedup outcomes, stop reason). |
| `summary.md` | The required short human-readable summary: one line per accepted finding. |
| `report.md` | Bonus: a fuller Markdown write-up of each finding plus the token/cost accounting summary. |

## Running with Docker

```bash
cd agent
docker build -t info-disclosure-agent .
```

The agent needs network access to crAPI, which itself runs as its own Docker Compose stack. The
most portable way to give it that (works the same on Linux/Mac/Windows, unlike
`host.docker.internal` tricks) is to attach the agent container to crAPI's own Docker network and
address it by service name instead of `localhost`:

```bash
# Find crAPI's network name and gateway service name once:
docker inspect crapi-web --format '{{json .NetworkSettings.Networks}}'
# -> look at the key name, e.g. "docker_default"; crAPI's gateway is reachable
#    inside that network as plain "http://crapi-web" (port 80, no need for :8888)

docker run --rm \
  --network docker_default \
  --env-file .env \
  -e CRAPI_TARGET=http://crapi-web \
  -v "$(pwd)/creds.json:/app/creds.json:ro" \
  -v "$(pwd)/out:/app/out" \
  info-disclosure-agent --out-dir /app/out
```

This has been verified end-to-end (built, run against a live crAPI stack over the shared Docker
network, output files landed correctly in the mounted `./out` directory). If crAPI is instead
reachable directly from the host at a fixed port (the common case if you followed
[Setting up the target](#setting-up-the-target-crapi) above), you can alternatively run the agent
container with `--network host` on Linux, or set `CRAPI_TARGET=http://host.docker.internal:8888`
on Docker Desktop (Mac/Windows), instead of joining crAPI's network directly.

## Architecture (short version -- see [`DESIGN.md`](DESIGN.md) for the full write-up)

```
CLI (cli.py) -> RunConfig (config.py) -> AgentLoop (loop.py)
                                             |
                     +-----------------------+-----------------------+
                     |                       |                       |
              LLMClient              ToolBox (toolbox.py)      BudgetTracker
             (llm_client.py)        /            |        \      (budget.py)
                                    /             |         \
                         HttpToolkit     ControlToolkit   (repeat detection,
                       (tools/http_tool)  (tools/control_tool)  step/token caps)
                            |                    |
                     crAPI target      scope.py + validation.py
                                                  |
                                          findings.json / summary.md / report.md
                                          (schemas.py, report.py, dedup.py, verifier.py)
```

The model only ever emits structured tool calls (`http_request`, `discover_api_endpoints`,
`list_visited_endpoints`, `list_id_candidates`, `propose_finding`, `finish_investigation`); it never performs I/O itself. See
module docstrings throughout `agent/` for the reasoning behind each piece -- every file's top-of-file
docstring explains *why* it's built the way it is, not just what it does.

## Running the tests

322 tests across 17 files, all offline -- no live crAPI instance or real LLM call needed (one test
file additionally needs `hypothesis`, not in `requirements.txt` since it's a test-only dependency:
`pip install hypothesis`). Network boundaries (the OpenAI SDK client, `requests.Session`) are
replaced with small fakes/mocks, so the suite runs in well under a second:

```bash
python -m unittest discover -s tests -v
```

Coverage includes: the scope gate and finding validation/grounding, semantic de-duplication,
budget/repeat-detection and all the narrower anti-drift guards (degenerate-path,
premature-finish, transcript compaction), credentials/config loading and env-var precedence, the
LLM client's model-resolution/retry/`tool_choice` logic, the bonus verifier and cost/report
renderers, the system prompt (including a regression guard that the challenge-list reference
never leaks into it), the tool-call schemas and dispatch routing, the HTTP toolkit against a fake
session (auth caching, truncation, and the degenerate-path guard end-to-end), CLI argument parsing
and `.env` resolution, a full `AgentLoop` integration suite driving a scripted stub LLM through
budget exhaustion, the completion gate, malformed/unknown tool calls, per-call repeat-blocking, and
the findings-output pipeline, and `hypothesis` property-based tests asserting that the config,
validation, and schema layers never crash on arbitrary/adversarial input.

## Known limitations

- **Endpoint discovery was the dominant recall limitation, and is now addressed (27th pass,
  2026-10-02).** For most of this project's history the baseline model (`qwen36-35b-a3b`) never
  discovered crAPI's real vehicle/mechanic-report/order resource paths, only generic infrastructure
  guesses like `/.env` -- because its only means of finding paths was guessing REST resource nouns,
  which do not match crAPI's idiosyncratic real paths. The `discover_api_endpoints` tool (reads the
  SPA's own JavaScript bundle tool-side for the real API surface) fixed this: the agent now reaches
  the real vehicle/community/mechanic/order endpoints. The committed run (28th pass, 2026-10-03) now
  has **five findings** -- `.env` creds (off-list), community `posts/recent` cross-user PII (Challenge
  4), a verbose Spring 404 error (off-list), a `GET /vehicle/{uuid}/location` **cross-user BOLA**
  (Challenge 1), and community post-detail PII (Challenge 4) -- each verified live before promotion.
  See [`HISTORY.md`](HISTORY.md) for the full account. **Run-to-run instability still persists** --
  which specific findings surface varies between runs at the same budget (this run, for instance,
  did not reproduce the 27th pass's forget-password finding) -- and the model still does not always
  convert an observed leak into a proposal (next bullet). The control loop, tools, scope enforcement,
  and validation all worked correctly and cleanly throughout.
- **The baseline model almost never chooses `propose_finding` on its own.** One diagnosis found it
  calling `http_request` on 199 of 200 observed turns across three back-to-back runs,
  `propose_finding` zero times, even after clearly observing a real leak. A fix forces the tool for
  one turn after a 200 on a well-known sensitive path, but this only helps for that small, generic
  list of paths -- it does nothing for a disclosure the model would otherwise recognize but that
  isn't on that list, so the model's own reluctance to call `propose_finding` remains a real, only
  partly-mitigated limitation, not a solved problem.
- **Keyword-based scope gate.** `agent/scope.py` rejects a candidate finding if its own text
  mentions an out-of-scope technique name (SQL injection, XSS, CSRF, SSRF, etc.), even in passing.
  This deliberately favors precision/scope-discipline over recall (see that module's docstring) --
  a real disclosure finding that happens to *mention* an unrelated technique in its reasoning would
  be rejected. The system prompt instructs the model to avoid this by describing findings purely
  in terms of exposed data, but it's a heuristic, not a guarantee.
- **Evidence grounding is substring-based, and bound to the claimed endpoint (fix #43).**
  `agent/validation.py` requires a distinctive snippet of a finding's evidence to appear in a
  response body the run actually captured **from a request matching the finding's own
  `METHOD /path`** -- not merely somewhere in the run. The endpoint binding was added after a live
  run produced a fabricated cross-user BOLA by quoting one request's response body as proof of a
  claim about a different request (whose real response was a 404); grounding to the claimed endpoint
  rejects that while leaving genuine findings untouched. The snippet match can still occasionally
  reject a real finding if the model paraphrases too loosely; it will typically retry with a tighter
  quote once told why it was rejected.
- **`on_challenge_list` is a best-effort keyword classifier** (`agent/challenge_reference.py`)
  against a small, human-curated summary of the public challenge list, run only after a finding is
  already accepted. It is not, and must never become, an input to discovery (see that module's
  docstring for why) -- so it can under-tag (a real on-list finding classified `false`) but should
  not meaningfully over-tag.
- **No cross-run persistence.** Each run starts with a cold history; the agent does not learn
  across invocations. Within a single run it does track everything it has already tried.
- **Stability is bounded, not guaranteed.** LLM sampling is inherently non-deterministic
  (temperature 0.2 for discovery turns, 0.0 for the verifier); the step/token budget and repeat
  detection bound *how much* it can vary, not eliminate variance entirely. See `DESIGN.md` for
  what more time would buy here.

## Ambiguities & assumptions made

Where the project goals left something open, I made a reasonable assumption and documented it.
The ones that materially shaped this implementation are:

1. **Credentials file format** -- not specified by the spec at all; schema above is this
   project's answer (`agent/schemas.py::Credentials`).
2. **LLM secrets only via env vars** -- chosen so the key can never land in a committed
   `creds.json`, CLI history, or `run.log`.
3. **Tool set beyond `http_request`** -- the spec only gives `http_request` as a firm
   requirement, offering "maybe an endpoint-listing or response-inspection tool" as an example.
   This project adds `list_visited_endpoints` (a coverage map of the run's own history, not a
   speculative spec-fetcher), `list_id_candidates` (single-hop ID-pivot inspection over an already-
   fetched response -- see `HISTORY.md`'s "eighteenth pass" for why a multi-hop crawler was
   considered and rejected as out of scope), `discover_api_endpoints` (reads the frontend's own
   JavaScript bundle tool-side and returns the real API paths it references -- target-agnostic SPA
   recon, raw JS never enters the model's context; see `HISTORY.md`'s "twenty-seventh pass"), plus
   two control tools, `propose_finding` and
   `finish_investigation`, which turn the mandatory finding-validation and termination requirements
   into ordinary, uniform tool calls rather than side-channel logic bolted onto the loop.
4. **`on_challenge_list` computation** -- resolved as a small, code-only, post-hoc keyword
   classifier (`agent/challenge_reference.py`), explicitly never exposed to the model.
5. **Budget numbers** -- default `--max-steps 40` / `--max-tokens 400000`; the currently-committed
   real run used a larger budget instead (see `HISTORY.md` for why). Both are legitimate; no
   "correct" value is given by the spec.
6. **Confidence rubric** -- not specified; this project doesn't hand the model a numeric rubric,
   deliberately, since forcing a rigid formula would be its own kind of "the model said so."
   Instead, `high` is expected when evidence is directly observed and (where applicable)
   cross-account confirmed; `medium`/`low` for weaker single-observation signal -- guidance given
   in the system prompt, final judgment left to the model, backstopped by grounding validation.
7. **Minimal target seeding** -- the model is told the three top-level API prefixes
   (`/identity`, `/community`, `/workshop`) and the account labels available, but nothing about
   specific endpoints or bug locations (see `agent/prompts.py`'s docstring for the reasoning).

## Time spent & what I'd improve next

**Time spent.** Roughly 6–8 focused hours over a few days on the core design and build -- the
intended budget for the exercise -- followed by substantial iterative hardening on top of that
first pass (the full pass-by-pass account is in [`HISTORY.md`](HISTORY.md)).

**What I'd improve next**, highest-value first. The complete list -- in-scope-but-unbuilt (Part 1) and deliberately-out-of-scope (Part 2) -- lives in [`FUTURE_IMPROVEMENTS.md`](FUTURE_IMPROVEMENTS.md):

1. **Make the agent act on what it already sees.** The single biggest limitation (see
   [Known limitations](#known-limitations)) is follow-through, not discovery: the model often
   fetches a response that exposes something -- another user's PII in a list, an ID worth pivoting
   on -- and moves on without calling `propose_finding` or `list_id_candidates`. Deterministic
   forced-proposal heuristics patch the clearest cases, but a more capable model (or fine-tuned
   tool selection) is the real lever. The first experiment I'd run is pointing the *unchanged*
   harness at a stronger model, to cleanly separate model capability from anything still latent in
   the agent.
2. **Automated cross-account response diffing.** BOLA / excessive-exposure findings currently rely
   on the model eyeballing two raw bodies. A tool that structurally diffs account A's vs. account
   B's response to the same endpoint would turn the project's core technique into a deterministic
   signal rather than a judgment call.
3. **Resilience polish for a one-shot run:** a single retry on transient connection failures, and
   an explicit guard for binary / non-text response bodies -- neither of which the current
   happy-path code handles.
4. **A self-scoring script for the goal metrics** (precision, generalization, recall, scope
   discipline, stability) so a run can be evaluated against crAPI's public challenge list
   automatically instead of by hand.
