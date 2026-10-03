"""Offline tests for agent.toolbox: the tool-call JSON schemas the model
sees, and dispatch() routing. Uses real HttpToolkit/ControlToolkit instances
(cheap, pure-Python objects) but never makes a network call.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.toolbox import ToolBox
from agent.tools.control_tool import ControlToolkit
from agent.tools.http_tool import HttpToolkit
from tests._helpers import FakeResponse


def _config_with_accounts(labels):
    config = MagicMock()
    config.credentials.accounts = [
        MagicMock(label=label, token="tok", email=f"{label}@example.com", password=None) for label in labels
    ]
    return config


class ToolSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toolbox = ToolBox(http=HttpToolkit(_config_with_accounts(["primary", "secondary"])), control=None)
        self.toolbox.control = ControlToolkit(http=self.toolbox.http)
        self.schemas = self.toolbox.schemas()

    def test_exactly_six_tools_exposed(self) -> None:
        names = [s["function"]["name"] for s in self.schemas]
        self.assertEqual(
            names,
            [
                "http_request",
                "discover_api_endpoints",
                "list_visited_endpoints",
                "list_id_candidates",
                "propose_finding",
                "finish_investigation",
            ],
        )

    def test_http_request_account_enum_includes_configured_labels_and_null(self) -> None:
        http_schema = self.schemas[0]
        account_enum = http_schema["function"]["parameters"]["properties"]["account"]["enum"]
        self.assertIn("primary", account_enum)
        self.assertIn("secondary", account_enum)
        self.assertIn(None, account_enum)

    def test_discover_api_endpoints_takes_optional_page_path(self) -> None:
        schema = self.schemas[1]
        self.assertEqual(schema["function"]["name"], "discover_api_endpoints")
        params = schema["function"]["parameters"]
        # page_path is the only parameter and it is optional (no `required`).
        self.assertEqual(list(params["properties"]), ["page_path"])
        self.assertNotIn("required", params)

    def test_list_id_candidates_requires_path(self) -> None:
        schema = self.schemas[3]
        self.assertEqual(schema["function"]["parameters"]["required"], ["path"])

    def test_propose_finding_has_no_category_argument(self) -> None:
        propose_schema = self.schemas[4]
        properties = propose_schema["function"]["parameters"]["properties"]
        self.assertNotIn("category", properties)

    def test_propose_finding_required_fields(self) -> None:
        propose_schema = self.schemas[4]
        required = set(propose_schema["function"]["parameters"]["required"])
        self.assertEqual(
            required, {"title", "endpoint", "evidence", "why_disclosure", "reproduction", "confidence"}
        )

    def test_finish_investigation_requires_summary(self) -> None:
        finish_schema = self.schemas[5]
        self.assertEqual(finish_schema["function"]["parameters"]["required"], ["summary"])


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.toolbox = ToolBox(http=HttpToolkit(_config_with_accounts(["primary"])), control=None)
        self.toolbox.control = ControlToolkit(http=self.toolbox.http)

    def test_dispatch_unknown_tool_raises_keyerror(self) -> None:
        with self.assertRaises(KeyError):
            self.toolbox.dispatch("not_a_real_tool", {}, step=1)

    def test_dispatch_list_visited_endpoints_routes_to_http_toolkit(self) -> None:
        result = self.toolbox.dispatch("list_visited_endpoints", {}, step=1)
        self.assertEqual(result, {"count": 0, "requests": []})

    def test_dispatch_finish_investigation_routes_to_control_toolkit(self) -> None:
        result = self.toolbox.dispatch("finish_investigation", {"summary": "premature"}, step=1)
        self.assertFalse(result["acknowledged"])  # blocked by MIN_REQUESTS_BEFORE_FINISH, 0 requests made
        self.assertFalse(self.toolbox.control.finished)

    def test_dispatch_propose_finding_routes_to_control_toolkit(self) -> None:
        result = self.toolbox.dispatch(
            "propose_finding",
            {
                "title": "x",
                "endpoint": "GET /y",
                "evidence": "z",
                "why_disclosure": "too short",
                "reproduction": ["step"],
                "confidence": "low",
            },
            step=1,
        )
        self.assertFalse(result["accepted"])  # rejected by validation, but routed correctly

    def test_dispatch_propose_finding_with_invalid_confidence_hits_schema_rejection(self) -> None:
        # Passes validate_finding's checks (scope, substantive reasoning,
        # evidence grounding) but fails Finding's own pydantic schema
        # (confidence outside the allowed Literal["high", "medium", "low"])
        # -- exercises ControlToolkit.propose_finding's `except Exception`
        # branch (previously untested; that branch implements the
        # the spec's bonus structured-output validation + retry
        # requirement, see control_tool.py's docstring).
        self.toolbox.http._body_cache.append(
            ("GET", "/identity/api/v2/user/dashboard", '"email": "victim@example.com"')
        )
        result = self.toolbox.dispatch(
            "propose_finding",
            {
                "title": "Other user's email exposed",
                "endpoint": "GET /identity/api/v2/user/dashboard",
                "evidence": '"email": "victim@example.com"',
                "why_disclosure": (
                    "This endpoint returns another account's email address to the caller unintentionally."
                ),
                "reproduction": ["step 1"],
                "confidence": "extremely-confident",  # not in the Confidence Literal
            },
            step=1,
        )
        self.assertFalse(result["accepted"])
        self.assertIn("did not match the required schema", result["reason"])
        self.assertEqual(self.toolbox.control.rejected_count, 1)

    def test_dispatch_drops_unrecognized_kwargs_instead_of_raising(self) -> None:
        # Fix #32's live discovery: a chat-template leak can tack an extra,
        # unrecognized key (here 'method', apparently bled in from a second,
        # corrupted tool call) onto otherwise-legitimate arguments. This must
        # be dropped, not raise TypeError -- dispatch's own docstring already
        # promises only an *unknown tool name* raises.
        self.toolbox.http._body_cache.append(("GET", "/.env", '"DB_PASSWORD=crapi"'))
        result = self.toolbox.dispatch(
            "propose_finding",
            {
                "title": "Sensitive .env File Exposed",
                "endpoint": "GET /.env",
                "evidence": '"DB_PASSWORD=crapi"',
                "why_disclosure": "The .env file is served verbatim to unauthenticated requests, exposing real database credentials to anyone.",
                "reproduction": ["GET /.env"],
                "confidence": "high",
                "method": "GET",  # unrecognized -- propose_finding has no such parameter
            },
            step=1,
        )
        self.assertTrue(result["accepted"])

    def test_dispatch_list_id_candidates_routes_to_http_toolkit(self) -> None:
        result = self.toolbox.dispatch("list_id_candidates", {"path": "/never/requested"}, step=1)
        self.assertEqual(result["error"], "no_cached_response")

    def test_dispatch_discover_api_endpoints_routes_to_http_toolkit(self) -> None:
        # Route a discover_api_endpoints call through dispatch with a fake
        # session so no real network call is made; the root page references
        # one bundle that embeds a single API path literal.
        html = '<html><script src="/static/js/app.js"></script></html>'
        bundle = 'fetch("api/v2/vehicle/vehicles")'
        responses = iter([FakeResponse(text=html), FakeResponse(text=bundle)])
        self.toolbox.http._session_factory = lambda: SimpleNamespace(
            request=lambda *a, **k: next(responses)
        )
        result = self.toolbox.dispatch("discover_api_endpoints", {}, step=1)
        self.assertEqual(result["discovered_paths"], ["api/v2/vehicle/vehicles"])
        self.assertEqual(result["scripts_scanned"], ["/static/js/app.js"])

    def test_dispatch_http_request_drops_unrecognized_kwargs_instead_of_raising(self) -> None:
        self.toolbox.http._session_factory = lambda: SimpleNamespace(
            request=lambda *args, **kwargs: FakeResponse(text="{}")
        )
        result = self.toolbox.dispatch(
            "http_request",
            {"method": "GET", "path": "/x", "account": None, "unexpected_extra_field": "leaked"},
            step=1,
        )
        self.assertIsNone(result["error"])
        self.assertEqual(result["status_code"], 200)


if __name__ == "__main__":
    unittest.main()
