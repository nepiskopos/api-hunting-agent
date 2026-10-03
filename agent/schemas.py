"""Data contracts used throughout the agent.

This module defines two independent things that are easy to conflate but
must stay separate:

1. **Input contract** (``Account`` / ``Credentials``) -- the shape of the
   ``creds.json`` file the operator hands to the agent. This file's format is *not* prescribed by
   the spec ("document what it expects") -- this is our documented answer.

2. **Output contract** (``Finding``) -- the *exact* schema required by the
   the spec (the finding schema, "Required finding schema"). Field names, types and
   the allowed ``confidence`` values are copied verbatim from the spec
   text; do not rename fields here without also updating the spec.

Both are `pydantic` models so that (a) a malformed ``creds.json`` fails fast
with a readable error before any HTTP/LLM calls are made, and (b) findings
the model proposes can be schema-validated before being written to
``findings.json`` (see ``agent.validation`` and the bonus retry logic in
``agent.loop``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class Account(BaseModel):
    """A single crAPI identity the agent is allowed to authenticate as.

    Exactly one of ``token`` or ``password`` must be usable to obtain a
    session:

    - If ``token`` is set, the agent uses it as-is (a pre-obtained bearer JWT).
    - Otherwise, the agent logs in itself via ``email`` + ``password`` against
      crAPI's identity service (``POST /identity/api/auth/login``) and caches
      the resulting token for the rest of the run.

    ``label`` is a short, human-chosen name (e.g. ``"primary"``,
    ``"secondary"``) used in prompts, logs and reproduction steps so a
    reviewer can tell *which* account a request was made as -- this matters
    a lot for "excessive data exposure" findings, which are only provable by
    showing account A can see account B's data.
    """

    label: str = Field(..., description="Short human-readable name for this identity, e.g. 'primary'.")
    email: str = Field(..., description="crAPI account email (used for login if no token is given).")
    password: str | None = Field(
        default=None,
        description="crAPI account password. Required unless 'token' is supplied.",
    )
    token: str | None = Field(
        default=None,
        description="Pre-obtained bearer JWT for this account. If set, login is skipped.",
    )

    @model_validator(mode="after")
    def _require_password_or_token(self) -> "Account":
        if not self.password and not self.token:
            raise ValueError(
                f"account '{self.label}': must supply either 'password' (for the agent to log "
                f"in itself) or a pre-obtained 'token'."
            )
        return self


class Credentials(BaseModel):
    """Top-level shape of the ``creds.json`` file passed via ``--creds``.

    Example (see ``creds.example.json`` at the project root)::

        {
          "target": "http://localhost:8888",
          "accounts": [
            {"label": "primary",   "email": "a@example.com", "password": "..."},
            {"label": "secondary", "email": "b@example.com", "password": "..."}
          ]
        }

    ``target`` here is optional and, if present, is only used as a fallback
    when ``--target`` is not passed on the command line -- the CLI flag always
    wins (see ``agent.config``).

    At least two accounts are recommended (not enforced) so the agent can
    test cross-account "excessive data exposure" (the scope definition):
    fetching account B's records while authenticated as account A. A single
    account still lets the agent look for verbose errors, leaked internal
    endpoints, secrets in responses/headers, etc., so one account is accepted
    -- ``agent.config.load_credentials`` logs a warning in that case that
    cross-account checks will be skipped.
    """

    target: str | None = Field(default=None, description="Fallback target base URL, overridden by --target.")
    accounts: list[Account] = Field(..., min_length=1, description="One or more crAPI identities to test with.")

    @field_validator("accounts")
    @classmethod
    def _unique_labels(cls, accounts: list[Account]) -> list[Account]:
        labels = [a.label for a in accounts]
        if len(labels) != len(set(labels)):
            raise ValueError(f"account labels must be unique, got: {labels}")
        return accounts


# Module-level (not nested in Finding below) so it can be imported directly --
# see agent.tools.control_tool's propose_finding tool schema, which takes a
# `confidence: Confidence` argument.
Confidence = Literal["high", "medium", "low"]


class Finding(BaseModel):
    """The required output schema (the finding schema), reproduced exactly.

    Field-for-field mapping to the spec's JSON schema:

    ============ ================================================================
    field         meaning
    ============ ================================================================
    title         short description
    endpoint      "METHOD /path"
    category      always "information_disclosure" -- enforced by the scope gate
                  in ``agent.scope`` *before* a finding ever reaches this model
    evidence      the leaked data / response excerpt, secrets redacted to last 4 chars
    why_disclosure reasoning: what was exposed, to whom, why unintended
    reproduction  ordered list of steps/requests to reproduce
    confidence    "high" | "medium" | "low"
    on_challenge_list  whether this matches a known public crAPI challenge
                  (see ``agent.challenge_reference`` -- computed by code, not
                  claimed by the model)
    ============ ================================================================
    """

    title: str = Field(..., min_length=1, description="Short description of the finding.")
    endpoint: str = Field(..., description="'METHOD /path', e.g. 'GET /identity/api/v2/user/dashboard'.")
    category: Literal["information_disclosure"] = "information_disclosure"
    evidence: str = Field(..., min_length=1, description="Leaked data / response excerpt, secrets redacted.")
    why_disclosure: str = Field(..., min_length=1, description="What leaked, to whom, why it is unintended.")
    reproduction: list[str] = Field(..., min_length=1, description="Ordered steps/requests to reproduce.")
    confidence: Confidence = Field(..., description="high | medium | low.")
    on_challenge_list: bool = Field(..., description="Matches a known public crAPI challenge (code-computed).")

    @field_validator("endpoint")
    @classmethod
    def _endpoint_shape(cls, v: str) -> str:
        parts = v.split(" ", 1)
        if len(parts) != 2 or not parts[0].isalpha() or not parts[1].startswith("/"):
            raise ValueError(f"endpoint must look like 'METHOD /path', got: {v!r}")
        return v
