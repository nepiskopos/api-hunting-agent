"""Information-Disclosure Hunting Agent.

An autonomous LLM-driven agent that probes a running OWASP crAPI instance and
reports information-disclosure findings only (see ``agent.scope`` for the
exact in-scope/out-of-scope definition).

Package layout (read in this order to understand the system):

- ``schemas``        -- data contracts: credentials input, tool calls, findings.
- ``challenge_reference`` -- static local copy of the public crAPI challenge
  list, used *only* to tag findings for the ``on_challenge_list`` field.
  Never fed to the model as a checklist (see module docstring for why).
- ``config``         -- ``RunConfig``: everything a run needs, gathered from
  CLI flags, env vars and the credentials file.
- ``llm_client``     -- thin OpenAI-compatible client wrapper + token accounting.
- ``prompts``        -- the system prompt that defines the mission and the
  scope boundary for the model.
- ``tools``          -- the tool functions the model can invoke (HTTP request,
  coverage listing, finding proposal, completion). The model never performs
  I/O itself; these do.
- ``toolbox``        -- assembles the tool-call JSON schemas and dispatches a
  model tool call to the right toolkit method.
- ``budget``         -- step/token hard caps and repeat/loop detection.
- ``scope``          -- the code-level gate that enforces information-disclosure-only.
- ``validation``     -- turns a raw model claim into a justified, evidence-backed finding.
- ``verifier``        -- optional adversarial second-pass check (bonus).
- ``dedup``           -- optional semantic de-duplication of findings (bonus).
- ``cost``            -- optional token/cost accounting summary (bonus).
- ``report``          -- optional Markdown report renderer (bonus).
- ``loop``           -- the AgentLoop: the actual reason -> act -> observe control loop.
- ``logging_setup``  -- structured, human-readable ``run.log`` writer.
- ``cli``            -- argument parsing and process wiring.
- ``__main__``       -- ``python -m agent`` entry point.
"""

__version__ = "0.1.0"
