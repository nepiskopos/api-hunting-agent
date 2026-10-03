"""Property-based (fuzz) tests using ``hypothesis``, added in the fifteenth
review pass as a deliberately different verification technique from the
fourteen prior passes' reading-based review and coverage-driven gap-closing.

Reading-based review has a ceiling: a human (or an LLM doing the same thing)
can only think of the edge cases they think to think of. Hypothesis instead
generates hundreds of adversarial inputs per property and shrinks any
failure to a minimal reproduction -- this is exactly what found bug #30
(NaN/Infinity through ``--request-timeout``) in spirit, but done
systematically here rather than by one person noticing one edge case.

These tests assert *properties* (things that should hold for every input in
a class), not specific examples -- specific-example regression tests for any
bug these tests find belong in the module's own existing test file
(``test_config.py``, ``test_scope_and_validation.py``, etc.), not here.
"""

from __future__ import annotations

import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from agent.config import ConfigError, build_config
from agent.schemas import Account, Credentials, Finding
from agent.scope import check_scope
from agent.validation import _evidence_is_grounded, _normalize, _reasoning_is_substantive, validate_finding
from tests._helpers import StubToolkit as _StubToolkit
from tests._helpers import write_creds_file

# Hypothesis's default health checks flag function-scoped fixtures (tempdir
# creation) as possibly-too-slow; these tests do real (tiny) filesystem I/O
# per example on purpose, so that check is suppressed rather than trying to
# hoist tempdir creation out of the property.
_SUPPRESS_SLOW_SETUP = settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)


class ConfigNumericBoundsPropertyTests(unittest.TestCase):
    """Every hard-cap numeric setting in RunConfig should fail closed
    (ConfigError, not a crash or a silent nonsensical accept) for any
    non-positive value, and round-trip cleanly for any positive one. This is
    the property that bug #30 (NaN/Infinity in request_timeout_seconds) and
    the fifteenth pass's max_steps/max_total_tokens/max_consecutive_repeats
    fix both instantiate -- stated once, generally, instead of by hand-picked
    example.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.creds_path = write_creds_file(
            self.tmp_path, {"accounts": [{"label": "p", "email": "a@x.com", "password": "pw"}]}
        )
        self._env_patcher = patch.dict(
            os.environ, {"LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "dummy-key"}, clear=False
        )
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()
        self._tmp.cleanup()

    @_SUPPRESS_SLOW_SETUP
    @given(st.integers(max_value=0))
    def test_non_positive_max_steps_always_rejected(self, value: int) -> None:
        with self.assertRaises(ConfigError):
            build_config(target="http://x", creds_path=self.creds_path, max_steps=value, out_dir=self.tmp_path)

    @_SUPPRESS_SLOW_SETUP
    @given(st.integers(min_value=1, max_value=10_000_000))
    def test_positive_max_steps_always_accepted_and_roundtrips(self, value: int) -> None:
        config = build_config(
            target="http://x", creds_path=self.creds_path, max_steps=value, out_dir=self.tmp_path
        )
        self.assertEqual(config.max_steps, value)

    @_SUPPRESS_SLOW_SETUP
    @given(st.integers(max_value=0))
    def test_non_positive_max_total_tokens_always_rejected(self, value: int) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x", creds_path=self.creds_path, max_total_tokens=value, out_dir=self.tmp_path
            )

    @_SUPPRESS_SLOW_SETUP
    @given(st.integers(max_value=0))
    def test_non_positive_max_consecutive_repeats_always_rejected(self, value: int) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x",
                creds_path=self.creds_path,
                max_consecutive_repeats=value,
                out_dir=self.tmp_path,
            )

    @_SUPPRESS_SLOW_SETUP
    @given(st.floats())
    def test_request_timeout_rejected_iff_not_finite_positive(self, value: float) -> None:
        should_be_valid = math.isfinite(value) and value > 0
        if should_be_valid:
            config = build_config(
                target="http://x",
                creds_path=self.creds_path,
                request_timeout_seconds=value,
                out_dir=self.tmp_path,
            )
            self.assertEqual(config.request_timeout_seconds, value)
        else:
            with self.assertRaises(ConfigError):
                build_config(
                    target="http://x",
                    creds_path=self.creds_path,
                    request_timeout_seconds=value,
                    out_dir=self.tmp_path,
                )


class ValidationTextPropertyTests(unittest.TestCase):
    """`_normalize`, `_evidence_is_grounded`, `_reasoning_is_substantive`, and
    `validate_finding` accept arbitrary, untrusted model-generated text (an
    LLM's tool-call arguments) -- they must never raise on any string input,
    only ever return a clean accept/reject.
    """

    @given(st.text())
    def test_normalize_never_raises_on_arbitrary_text(self, text: str) -> None:
        _normalize(text)

    @given(st.text())
    def test_normalize_is_idempotent(self, text: str) -> None:
        once = _normalize(text)
        twice = _normalize(once)
        self.assertEqual(once, twice)

    @given(st.text(), st.text(), st.lists(st.text(), max_size=5))
    def test_evidence_is_grounded_never_raises(self, evidence: str, why_disclosure: str, bodies: list[str]) -> None:
        _evidence_is_grounded(evidence, _StubToolkit(bodies))

    @given(st.text(), st.text())
    def test_reasoning_is_substantive_never_raises(self, title: str, why_disclosure: str) -> None:
        _reasoning_is_substantive(title, why_disclosure)

    @given(st.text(), st.text(), st.text(), st.lists(st.text(), max_size=5))
    def test_validate_finding_never_raises_and_always_returns_a_verdict(
        self, title: str, why_disclosure: str, evidence: str, bodies: list[str]
    ) -> None:
        result = validate_finding(
            title=title, why_disclosure=why_disclosure, evidence=evidence, toolkit=_StubToolkit(bodies)
        )
        self.assertIsInstance(result.accepted, bool)
        if not result.accepted:
            self.assertIsInstance(result.reason, str)

    @given(st.text(), st.text())
    def test_check_scope_never_raises(self, title: str, why_disclosure: str) -> None:
        in_scope, reason = check_scope(title, why_disclosure)
        self.assertIsInstance(in_scope, bool)


class SchemaPropertyTests(unittest.TestCase):
    """The pydantic schemas are the boundary between an LLM's raw tool-call
    JSON and the rest of the agent -- they must convert arbitrary input into
    either a valid model or a clean ``pydantic.ValidationError``, never an
    unrelated crash (e.g. an unguarded regex/index operation inside a
    ``field_validator``).
    """

    @given(st.text())
    def test_finding_endpoint_validator_never_raises_unexpectedly(self, endpoint: str) -> None:
        try:
            Finding(
                title="t",
                endpoint=endpoint,
                evidence="e",
                why_disclosure="w",
                reproduction=["step"],
                confidence="low",
                on_challenge_list=False,
            )
        except ValidationError:
            pass

    @given(st.text(min_size=1), st.text(min_size=1), st.one_of(st.none(), st.text()), st.one_of(st.none(), st.text()))
    def test_account_never_raises_unexpectedly(self, label, email, password, token) -> None:
        try:
            Account(label=label, email=email, password=password, token=token)
        except ValidationError:
            pass

    @given(st.lists(st.text(min_size=1), min_size=1, max_size=8))
    def test_credentials_unique_label_check_never_raises_unexpectedly(self, labels: list[str]) -> None:
        accounts = [{"label": label, "email": f"{i}@x.com", "password": "pw"} for i, label in enumerate(labels)]
        try:
            creds = Credentials(accounts=accounts)
            # If it validated, labels must genuinely have been unique.
            self.assertEqual(len(creds.accounts), len(set(labels)))
        except ValidationError:
            # Must only be for the one reason this validator can fail:
            # a genuine duplicate among the generated labels.
            self.assertNotEqual(len(labels), len(set(labels)))


if __name__ == "__main__":
    unittest.main()
