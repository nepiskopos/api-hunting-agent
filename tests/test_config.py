"""Offline tests for agent.config: credentials loading, env-var fallbacks,
and precedence (CLI flag > env var > default). No network or LLM needed --
LLM_BASE_URL/LLM_API_KEY are set to dummy values since build_config only
checks they're present, never calls the endpoint.
"""

from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.config import ConfigError, build_config, load_credentials
from tests._helpers import write_creds_file as _write_creds

_ONE_ACCOUNT = {"accounts": [{"label": "primary", "email": "a@example.com", "password": "pw"}]}
_TWO_ACCOUNTS = {
    "accounts": [
        {"label": "primary", "email": "a@example.com", "password": "pw"},
        {"label": "secondary", "email": "b@example.com", "password": "pw"},
    ]
}


class LoadCredentialsTests(unittest.TestCase):
    def test_missing_file_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_credentials(Path("/nonexistent/creds.json"))

    def test_invalid_json_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            path.write_text("{not valid json")
            with self.assertRaises(ConfigError):
                load_credentials(path)

    def test_schema_invalid_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_creds(tmp, {"accounts": []})  # min_length=1 violated
            with self.assertRaises(ConfigError):
                load_credentials(path)

    def test_single_account_logs_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_creds(tmp, _ONE_ACCOUNT)
            with self.assertLogs("agent.config", level="WARNING") as cm:
                creds = load_credentials(path)
            self.assertEqual(len(creds.accounts), 1)
            self.assertIn("cross-account", cm.output[0])

    def test_two_accounts_no_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_creds(tmp, _TWO_ACCOUNTS)
            logger = logging.getLogger("agent.config")
            # Use a handler-based check instead of assertLogs (which requires
            # at least one record) since we're asserting the *absence* of one.
            records = []
            handler = logging.Handler()
            handler.emit = lambda record: records.append(record)
            logger.addHandler(handler)
            try:
                load_credentials(path)
            finally:
                logger.removeHandler(handler)
            self.assertEqual(records, [])


class BuildConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.creds_path = _write_creds(self._tmp.name, _TWO_ACCOUNTS)
        self._env_patcher = patch.dict(
            os.environ,
            {"LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "dummy-key"},
            clear=False,
        )
        self._env_patcher.start()

    def tearDown(self) -> None:
        self._env_patcher.stop()
        self._tmp.cleanup()

    def test_missing_llm_env_vars_raises(self) -> None:
        with patch.dict(os.environ, {"LLM_BASE_URL": "", "LLM_API_KEY": ""}):
            with self.assertRaises(ConfigError):
                build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)

    def test_missing_target_raises(self) -> None:
        no_target_creds = _write_creds(self._tmp.name, _TWO_ACCOUNTS)
        with self.assertRaises(ConfigError):
            build_config(target=None, creds_path=no_target_creds, out_dir=self.tmp_path)

    def test_cli_target_wins_over_env_and_creds_file(self) -> None:
        creds_with_target = _write_creds(self._tmp.name, {**_TWO_ACCOUNTS, "target": "http://from-creds"})
        with patch.dict(os.environ, {"CRAPI_TARGET": "http://from-env"}):
            config = build_config(target="http://from-cli", creds_path=creds_with_target, out_dir=self.tmp_path)
        self.assertEqual(config.target, "http://from-cli")

    def test_env_target_wins_over_creds_file(self) -> None:
        creds_with_target = _write_creds(self._tmp.name, {**_TWO_ACCOUNTS, "target": "http://from-creds"})
        with patch.dict(os.environ, {"CRAPI_TARGET": "http://from-env"}):
            config = build_config(target=None, creds_path=creds_with_target, out_dir=self.tmp_path)
        self.assertEqual(config.target, "http://from-env")

    def test_creds_file_target_used_as_last_resort(self) -> None:
        creds_with_target = _write_creds(self._tmp.name, {**_TWO_ACCOUNTS, "target": "http://from-creds"})
        config = build_config(target=None, creds_path=creds_with_target, out_dir=self.tmp_path)
        self.assertEqual(config.target, "http://from-creds")

    def test_target_trailing_slash_is_stripped(self) -> None:
        config = build_config(target="http://x.test/", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertEqual(config.target, "http://x.test")

    def test_max_steps_env_fallback(self) -> None:
        with patch.dict(os.environ, {"MAX_STEPS": "77"}):
            config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertEqual(config.max_steps, 77)

    def test_max_steps_cli_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"MAX_STEPS": "77"}):
            config = build_config(
                target="http://x", creds_path=self.creds_path, max_steps=5, out_dir=self.tmp_path
            )
        self.assertEqual(config.max_steps, 5)

    def test_zero_max_steps_raises_config_error(self) -> None:
        # Found via property testing (fifteenth pass): unlike
        # request_timeout_seconds, this field had no bounds check at all.
        # max_steps=0 doesn't crash -- the run terminates immediately with a
        # correctly-labeled "step budget exhausted (0/0)" -- but it silently
        # produces a fully-formed, empty "successful" run instead of catching
        # what's almost always a typo at startup, verified live.
        with self.assertRaises(ConfigError):
            build_config(target="http://x", creds_path=self.creds_path, max_steps=0, out_dir=self.tmp_path)

    def test_negative_max_steps_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build_config(target="http://x", creds_path=self.creds_path, max_steps=-5, out_dir=self.tmp_path)

    def test_negative_max_steps_env_fallback_raises_config_error(self) -> None:
        with patch.dict(os.environ, {"MAX_STEPS": "-1"}):
            with self.assertRaises(ConfigError):
                build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)

    def test_zero_max_total_tokens_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x", creds_path=self.creds_path, max_total_tokens=0, out_dir=self.tmp_path
            )

    def test_negative_max_total_tokens_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x", creds_path=self.creds_path, max_total_tokens=-100, out_dir=self.tmp_path
            )

    def test_zero_max_consecutive_repeats_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x", creds_path=self.creds_path, max_consecutive_repeats=0, out_dir=self.tmp_path
            )

    def test_negative_max_consecutive_repeats_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x",
                creds_path=self.creds_path,
                max_consecutive_repeats=-2,
                out_dir=self.tmp_path,
            )

    def test_max_consecutive_repeats_env_fallback(self) -> None:
        with patch.dict(os.environ, {"MAX_CONSECUTIVE_REPEATS": "9"}):
            config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertEqual(config.max_consecutive_repeats, 9)

    def test_max_consecutive_repeats_cli_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"MAX_CONSECUTIVE_REPEATS": "9"}):
            config = build_config(
                target="http://x",
                creds_path=self.creds_path,
                max_consecutive_repeats=2,
                out_dir=self.tmp_path,
            )
        self.assertEqual(config.max_consecutive_repeats, 2)

    def test_max_consecutive_repeats_default_is_zero_tolerance(self) -> None:
        # Regression test for a real live-observed waste (twenty-fourth
        # pass): a run.log showed the model issuing the exact same
        # http_request call twice in a row for zero new information. The
        # old default of 3 tolerates that (it only refuses the *fourth*
        # identical call), so tightened to 1 -- the very first repeat of a
        # byte-identical call to this deterministic local target is refused
        # before dispatch, since a repeat can never observe anything the
        # first call didn't already show.
        config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertEqual(config.max_consecutive_repeats, 1)

    def test_request_timeout_env_fallback(self) -> None:
        with patch.dict(os.environ, {"REQUEST_TIMEOUT_SECONDS": "30"}):
            config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertEqual(config.request_timeout_seconds, 30.0)

    def test_request_timeout_cli_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"REQUEST_TIMEOUT_SECONDS": "30"}):
            config = build_config(
                target="http://x",
                creds_path=self.creds_path,
                request_timeout_seconds=5.0,
                out_dir=self.tmp_path,
            )
        self.assertEqual(config.request_timeout_seconds, 5.0)

    def test_zero_request_timeout_raises_config_error(self) -> None:
        # requests raises a bare ValueError (not RequestException) for a
        # non-positive timeout, uncaught by http_tool.py's exception
        # handling -- must fail fast here instead, at startup, like every
        # other bad config value.
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x", creds_path=self.creds_path, request_timeout_seconds=0, out_dir=self.tmp_path
            )

    def test_negative_request_timeout_raises_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x", creds_path=self.creds_path, request_timeout_seconds=-5.0, out_dir=self.tmp_path
            )

    def test_negative_request_timeout_env_fallback_raises_config_error(self) -> None:
        with patch.dict(os.environ, {"REQUEST_TIMEOUT_SECONDS": "-1"}):
            with self.assertRaises(ConfigError):
                build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)

    def test_nan_request_timeout_raises_config_error(self) -> None:
        # A plain `<= 0` check misses this: every comparison against NaN is
        # False. requests.get(timeout=float("nan")) raises a bare
        # ValueError, uncaught by http_tool.py -- verified directly.
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x",
                creds_path=self.creds_path,
                request_timeout_seconds=float("nan"),
                out_dir=self.tmp_path,
            )

    def test_infinite_request_timeout_raises_config_error(self) -> None:
        # A plain `<= 0` check also misses this: +inf > 0 is True.
        # requests.get(timeout=float("inf")) raises a bare OverflowError,
        # uncaught by http_tool.py -- verified directly. argparse's
        # type=float happily parses CLI input like "inf", so this is
        # genuinely reachable, not just a theoretical edge case.
        with self.assertRaises(ConfigError):
            build_config(
                target="http://x",
                creds_path=self.creds_path,
                request_timeout_seconds=float("inf"),
                out_dir=self.tmp_path,
            )

    def test_out_dir_colliding_with_existing_file_raises_config_error(self) -> None:
        # Without this, Path.mkdir's FileExistsError would reach the user as
        # a raw traceback instead of the clean "error: ..." message every
        # other misconfiguration gets (see agent.cli.main's ConfigError
        # handling).
        blocking_file = self.tmp_path / "not_a_directory"
        blocking_file.write_text("")
        with self.assertRaises(ConfigError):
            build_config(target="http://x", creds_path=self.creds_path, out_dir=blocking_file)

    def test_verifier_dedup_env_toggle(self) -> None:
        with patch.dict(os.environ, {"VERIFIER_ENABLED": "false", "DEDUP_ENABLED": "0"}):
            config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertFalse(config.verifier_enabled)
        self.assertFalse(config.dedup_enabled)

    def test_no_verifier_flag_always_disables_regardless_of_env(self) -> None:
        with patch.dict(os.environ, {"VERIFIER_ENABLED": "true"}):
            config = build_config(
                target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path, no_verifier=True
            )
        self.assertFalse(config.verifier_enabled)

    def test_output_paths_derived_from_out_dir(self) -> None:
        config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertEqual(config.findings_path, self.tmp_path / "findings.json")
        self.assertEqual(config.run_log_path, self.tmp_path / "run.log")
        self.assertEqual(config.summary_path, self.tmp_path / "summary.md")
        self.assertEqual(config.report_path, self.tmp_path / "report.md")

    def test_out_dir_created_if_missing(self) -> None:
        nested = self.tmp_path / "does" / "not" / "exist"
        build_config(target="http://x", creds_path=self.creds_path, out_dir=nested)
        self.assertTrue(nested.is_dir())

    def test_llm_model_override_wins_over_env(self) -> None:
        with patch.dict(os.environ, {"LLM_MODEL": "env-model"}):
            config = build_config(
                target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path, model_override="cli-model"
            )
        self.assertEqual(config.llm_model, "cli-model")

    def test_llm_model_none_when_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_MODEL", None)
            config = build_config(target="http://x", creds_path=self.creds_path, out_dir=self.tmp_path)
        self.assertIsNone(config.llm_model)


if __name__ == "__main__":
    unittest.main()
