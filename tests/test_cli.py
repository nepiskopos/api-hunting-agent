"""Offline tests for agent.cli: argument parsing, error-path exit codes, and the
find_dotenv(usecwd=True) fix (see agent/cli.py::main's comment for the bug this guards against --
plain load_dotenv() with no args resolves relative to the *calling module's* file location, not
the actual process working directory, so it can silently load the wrong .env -- or none -- when
the agent is invoked from anywhere other than the package's own directory).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import find_dotenv

from agent.cli import build_arg_parser, main


class FindDotenvUsesCwdTests(unittest.TestCase):
    """Regression test for the usecwd=True fix: find_dotenv(usecwd=True), called from a real
    module (not a REPL/-c invocation), must resolve to a .env in the actual process cwd, even
    though that cwd is unrelated to wherever this test file itself lives on disk.
    """

    def test_resolves_env_from_process_cwd_not_module_location(self) -> None:
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / ".env").write_text("SOME_MARKER=from-tmp-cwd\n")
            try:
                os.chdir(tmp_path)
                found = find_dotenv(usecwd=True)
            finally:
                os.chdir(original_cwd)
            self.assertEqual(Path(found).resolve(), (tmp_path / ".env").resolve())

    def test_plain_find_dotenv_without_usecwd_ignores_process_cwd(self) -> None:
        """Documents the bug itself: without usecwd=True, find_dotenv() does NOT resolve to the
        tmp cwd's .env (it walks up from this test file's own location instead). This is why
        agent/cli.py must not call plain load_dotenv()/find_dotenv().

        find_dotenv()'s own source treats *any* active trace function
        (``sys.gettrace() is not None``) as "running under a debugger" and
        silently falls back to cwd-based resolution in that case too --
        exactly the ``usecwd=True`` behavior this test exists to show is
        otherwise absent. coverage.py installs a trace function while
        collecting coverage, so this test previously passed under plain
        ``pytest`` but failed under ``coverage run -m pytest`` -- not a
        real behavior difference, just an invocation-dependent false
        failure. Pinning ``sys.gettrace`` to ``None`` for the duration of
        the call makes the test assert the actual no-debugger code path
        regardless of how the test suite itself is invoked.
        """
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / ".env").write_text("SOME_MARKER=from-tmp-cwd\n")
            try:
                os.chdir(tmp_path)
                with patch("sys.gettrace", return_value=None):
                    found = find_dotenv()  # no usecwd=True
            finally:
                os.chdir(original_cwd)
            self.assertNotEqual(Path(found).resolve() if found else None, (tmp_path / ".env").resolve())


class ArgParserTests(unittest.TestCase):
    def test_all_documented_flags_parse(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "--target", "http://x",
                "--creds", "creds.json",
                "--max-steps", "10",
                "--max-tokens", "1000",
                "--max-consecutive-repeats", "5",
                "--request-timeout", "30",
                "--out-dir", "out",
                "--model", "some-model",
                "--no-verifier",
                "--no-dedup",
            ]
        )
        self.assertEqual(args.target, "http://x")
        self.assertEqual(args.creds, Path("creds.json"))
        self.assertEqual(args.max_steps, 10)
        self.assertEqual(args.max_tokens, 1000)
        self.assertEqual(args.max_consecutive_repeats, 5)
        self.assertEqual(args.request_timeout, 30.0)
        self.assertEqual(args.out_dir, Path("out"))
        self.assertEqual(args.model, "some-model")
        self.assertTrue(args.no_verifier)
        self.assertTrue(args.no_dedup)

    def test_all_flags_default_to_none_or_false(self) -> None:
        parser = build_arg_parser()
        args = parser.parse_args([])
        self.assertIsNone(args.target)
        self.assertIsNone(args.creds)
        self.assertIsNone(args.max_steps)
        self.assertIsNone(args.max_consecutive_repeats)
        self.assertIsNone(args.request_timeout)
        self.assertFalse(args.no_verifier)
        self.assertFalse(args.no_dedup)


class MainErrorPathTests(unittest.TestCase):
    def test_main_returns_2_and_prints_error_on_config_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            exit_code = main(["--creds", "/nonexistent/creds.json", "--target", "http://x"])
        self.assertEqual(exit_code, 2)


if __name__ == "__main__":
    unittest.main()
