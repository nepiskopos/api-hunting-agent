"""Shared test helpers for agent/tests/.

Named with a leading underscore (not `test_*`) so pytest/unittest discovery
never collects this module as a test file in its own right.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.schemas import Finding


def write_creds_file(dir_path, data: dict) -> Path:
    """Write ``data`` as ``creds.json`` under ``dir_path`` (a str or Path
    directory) and return its path.

    Consolidates two near-identical copies previously kept in
    test_config.py (as ``_write_creds``) and inlined directly in
    test_property_based.py's ``setUp``.
    """
    path = Path(dir_path) / "creds.json"
    path.write_text(json.dumps(data))
    return path


class FakeResponse:
    """Minimal stand-in for a requests.Response: enough for HttpToolkit's
    status_code/text/headers/json() usage, no real network call.

    Consolidates two near-identical copies previously kept in
    test_http_tool.py and test_loop_integration.py.
    """

    def __init__(self, status_code=200, text="{}", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self):
        return json.loads(self.text)


class StubToolkit:
    """Minimal stand-in for HttpToolkit: validation/property-based tests
    only need ``recent_bodies()`` / ``bodies_for_endpoint()``.

    ``bodies_for_endpoint`` ignores the endpoint and returns every stubbed
    body, because these tests exercise the snippet/scope/reasoning policy in
    isolation, not the endpoint-binding introduced in fix #43 (which is
    covered by dedicated tests against the real HttpToolkit in
    test_http_tool.py).

    Consolidates two identical copies previously kept in
    test_scope_and_validation.py and test_property_based.py.
    """

    def __init__(self, bodies: list[str]) -> None:
        self._bodies = bodies

    def recent_bodies(self) -> list[str]:
        return self._bodies

    def bodies_for_endpoint(self, endpoint: str | None) -> list[str]:
        return self._bodies


def make_finding(**overrides) -> Finding:
    """Build a valid Finding with sensible defaults; pass keyword overrides
    for whatever fields a given test actually cares about.

    Consolidates three near-identical copies of this helper previously
    kept in test_cost_and_report.py, test_dedup_and_schemas.py, and
    test_verifier.py -- none of those tests asserted on the *default*
    values of fields they didn't override, so one shared set of defaults
    is behaviorally equivalent to all three.
    """
    base = dict(
        title="Other user's data exposed",
        endpoint="GET /identity/api/v2/user/dashboard",
        evidence='"email": "victim@example.com"',
        why_disclosure="This endpoint returns another account's email address to the caller unintentionally.",
        reproduction=["step 1"],
        confidence="high",
        on_challenge_list=False,
    )
    base.update(overrides)
    return Finding(**base)
