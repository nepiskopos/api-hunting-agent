"""Assembles the OpenAI tool-call schema list and dispatches calls to the
right toolkit method.

This is the single seam between "what the model is allowed to ask for"
(the JSON schemas below, sent with every chat-completions call) and "what
actually runs" (``HttpToolkit`` / ``ControlToolkit``). Keeping the schema
definitions next to the dispatch table means adding or changing a tool's
argument shape can't silently drift out of sync with what ``dispatch``
expects.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass

from .config import RunConfig
from .tools.control_tool import ControlToolkit
from .tools.http_tool import HttpToolkit

logger = logging.getLogger("agent.toolbox")


def _drop_unrecognized_kwargs(func, arguments: dict) -> dict:
    """Return ``arguments`` with any key ``func`` doesn't accept removed.

    Fix #32's live verification (2026-07-29) surfaced a real failure mode
    while testing the new forced-propose_finding mechanism (see loop.py):
    this model's tool-calling output occasionally leaked raw chat-template
    control tokens (``</tool_call><tool_call>\\n<function=http_request>``)
    into a string argument, and the corrupted call ended up carrying an
    extra key (``method``) that ``propose_finding`` doesn't accept at all,
    turning what should have been an ordinary rejected/incomplete proposal
    into an opaque ``unexpected keyword argument`` ``TypeError``. Dropping
    unrecognized keys before dispatch means a call like that now fails (if
    it fails at all) via the same structured, model-visible rejection path
    as any other malformed-but-not-crashing call, not a raised exception.
    This only removes the *extra-argument* crash; a call with an unknown
    tool name or with required arguments missing can still raise, exactly as
    ``dispatch``'s own docstring states (and the loop catches it there).
    """
    accepted = set(inspect.signature(func).parameters)
    dropped = [k for k in arguments if k not in accepted]
    if dropped:
        logger.warning("dropping unrecognized arguments for %s: %s", func.__qualname__, dropped)
        return {k: v for k, v in arguments.items() if k in accepted}
    return arguments


def _call(func, arguments: dict, **extra):
    """Call ``func`` with the model's ``arguments`` (filtered through
    ``_drop_unrecognized_kwargs``) plus any loop-supplied ``extra`` keyword
    arguments (e.g. ``step``). Shared by every ``dispatch`` branch below
    that forwards a model-supplied argument dict to a toolkit method.
    """
    return func(**extra, **_drop_unrecognized_kwargs(func, arguments))

# --- schema for the http_request tool ---------------------------------------
# `account` intentionally has no default in the schema (the model must be
# explicit about which identity, or none, it's using) but is nullable --
# unauthenticated requests are a legitimate and important probe
# (in scope: "exposed internal or undocumented endpoints").

def _http_request_schema(account_labels: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make one HTTP request against the target crAPI instance. This is the only "
                "way to interact with the target -- you have no other means of making network "
                "calls. Returns status code, headers, and a (possibly truncated) body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    },
                    "path": {
                        "type": "string",
                        "description": "Path starting with '/', e.g. '/identity/api/v2/user/dashboard'.",
                    },
                    "account": {
                        "type": ["string", "null"],
                        "enum": account_labels + [None],
                        "description": (
                            "Which configured account's bearer token to attach, or null for an "
                            "unauthenticated request."
                        ),
                    },
                    "headers": {
                        "type": "object",
                        "description": "Extra request headers to set (optional).",
                        "additionalProperties": {"type": "string"},
                    },
                    "json_body": {
                        "type": "object",
                        "description": "JSON request body for POST/PUT/PATCH (optional).",
                        "additionalProperties": True,
                    },
                    "query": {
                        "type": "object",
                        "description": "Query-string parameters (optional).",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["method", "path"],
            },
        },
    }


LIST_VISITED_ENDPOINTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_visited_endpoints",
        "description": (
            "List every HTTP request made so far this run (method, path, account used, status "
            "code), to help you track coverage and avoid pointlessly repeating an identical "
            "request. Takes no arguments."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

LIST_ID_CANDIDATES_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_id_candidates",
        "description": (
            "Inspect the cached response from a request you already made (no new HTTP call) and "
            "return ID-looking fields found in it (e.g. a numeric or UUID id/user_id/vehicleId "
            "value), plus ready-to-use candidate paths if the given path itself has an ID segment "
            "you could substitute -- useful for probing whether another user's/resource's ID "
            "returns data that should not be visible to you (excessive data exposure / BOLA-style "
            "checks). Does not fetch anything itself; you still decide whether to request any "
            "candidate via http_request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "A path you already requested this run with http_request.",
                },
            },
            "required": ["path"],
        },
    },
}

DISCOVER_API_ENDPOINTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "discover_api_endpoints",
        "description": (
            "Fetch the target's frontend page and the JavaScript bundles it loads, and return "
            "the API endpoint paths referenced inside them -- i.e. the real endpoints the "
            "application itself calls, which is far more reliable than guessing resource names. "
            "The returned paths are frontend-relative (e.g. 'api/v2/vehicle/vehicles'); prepend a "
            "known service prefix (see the topology in your instructions) to form a full path, "
            "then fetch the interesting ones with http_request. This reads static assets only and "
            "makes no API calls for you -- you still decide what to request and with which "
            "account. Call it once early; it is deterministic, so repeating it yields nothing new."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "page_path": {
                    "type": "string",
                    "description": (
                        "Frontend page to start from (optional; defaults to '/'). Only change "
                        "this if the app's HTML is served somewhere other than the root."
                    ),
                },
            },
        },
    },
}

PROPOSE_FINDING_SCHEMA = {
    "type": "function",
    "function": {
        "name": "propose_finding",
        "description": (
            "Submit a candidate information-disclosure finding. It will be checked against real "
            "captured evidence and scope rules before being accepted -- you will be told if it "
            "was rejected and why. Do not call this for anything other than information "
            "disclosure (see scope rules); category is always information_disclosure and is set "
            "automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short description of the finding."},
                "endpoint": {"type": "string", "description": "'METHOD /path' that exposes the data."},
                "evidence": {
                    "type": "string",
                    "description": (
                        "The leaked data / response excerpt that proves this, taken from an "
                        "actual response you observed. Redact secrets/tokens to their last 4 "
                        "characters."
                    ),
                },
                "why_disclosure": {
                    "type": "string",
                    "description": "What was exposed, to whom, and why that is unintended. Be specific.",
                },
                "reproduction": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ordered steps/requests a reviewer could follow to reproduce this.",
                },
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            },
            "required": ["title", "endpoint", "evidence", "why_disclosure", "reproduction", "confidence"],
        },
    },
}

FINISH_INVESTIGATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish_investigation",
        "description": (
            "Declare that you have finished investigating and no further probing is likely to "
            "surface new information disclosure. Ends the run."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief summary of what was explored and why you're stopping."},
            },
            "required": ["summary"],
        },
    },
}


@dataclass
class ToolBox:
    """Owns both toolkits and exposes the (schemas, dispatch) pair the loop needs."""

    http: HttpToolkit
    control: ControlToolkit

    @classmethod
    def build(cls, config: RunConfig) -> "ToolBox":
        http = HttpToolkit(config)
        control = ControlToolkit(http=http)
        return cls(http=http, control=control)

    def schemas(self) -> list[dict]:
        return [
            _http_request_schema(self.http.account_labels()),
            DISCOVER_API_ENDPOINTS_SCHEMA,
            LIST_VISITED_ENDPOINTS_SCHEMA,
            LIST_ID_CANDIDATES_SCHEMA,
            PROPOSE_FINDING_SCHEMA,
            FINISH_INVESTIGATION_SCHEMA,
        ]

    def dispatch(self, name: str, arguments: dict, *, step: int) -> dict:
        """Run one tool call by name. Never raises for a "normal" failure
        (bad target response, rejected finding) -- those are returned as
        data. Raises ``KeyError``/``TypeError`` only for a call the model
        made with an unknown tool name or malformed arguments, which the
        loop's schema-validation/retry layer (a bonus feature) is responsible
        for catching.
        """
        if name == "http_request":
            return _call(self.http.http_request, arguments, step=step)
        if name == "discover_api_endpoints":
            return _call(self.http.discover_api_endpoints, arguments)
        if name == "list_visited_endpoints":
            return self.http.list_visited_endpoints()
        if name == "list_id_candidates":
            return _call(self.http.list_id_candidates, arguments)
        if name == "propose_finding":
            return _call(self.control.propose_finding, arguments)
        if name == "finish_investigation":
            return _call(self.control.finish_investigation, arguments)
        raise KeyError(f"unknown tool: {name!r}")
