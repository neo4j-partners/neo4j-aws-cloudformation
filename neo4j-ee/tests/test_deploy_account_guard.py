"""Unit tests for the AWS account guard (defect D8), no AWS calls.

check_expected_account and format_account_banner in deploy.py are pure. The
teardown.sh guard is exercised in a temp dir with a stub `aws` on PATH that
records every call; the real aws CLI is never invoked.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
EE_DIR = REPO_ROOT / "neo4j-ee"
sys.path.insert(0, str(EE_DIR))

import deploy  # noqa: E402


class CheckExpectedAccountTests(unittest.TestCase):
    def test_match_passes(self) -> None:
        deploy.check_expected_account("123456789012", "123456789012")

    def test_match_ignores_surrounding_whitespace(self) -> None:
        deploy.check_expected_account("123456789012\n", " 123456789012 ")

    def test_mismatch_exits_naming_both_accounts(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            deploy.check_expected_account("111111111111", "123456789012")
        message = str(ctx.exception)
        self.assertIn("111111111111", message)
        self.assertIn("123456789012", message)

    def test_empty_expected_is_a_no_op(self) -> None:
        deploy.check_expected_account("111111111111", "")
        deploy.check_expected_account("111111111111", "   ")

    def test_non_12_digit_expected_rejected(self) -> None:
        for bad in ("12345", "1234567890123", "12345678901a", "arn:aws"):
            with self.subTest(expected=bad):
                with self.assertRaises(SystemExit):
                    deploy.check_expected_account("123456789012", bad)


class FormatAccountBannerTests(unittest.TestCase):
    def test_banner_has_account_profile_region_and_arn(self) -> None:
        banner = deploy.format_account_banner(
            "123456789012", "arn:aws:iam::123456789012:user/ryan",
            "marketplace", "us-east-1",
        )
        self.assertIn("Account: 123456789012", banner)
        self.assertIn("Profile: marketplace", banner)
        self.assertIn("Region: us-east-1", banner)
        self.assertIn("arn:aws:iam::123456789012:user/ryan", banner)


class ParseArgsExpectedAccountTests(unittest.TestCase):
    def _parse(self, argv: list[str], env: dict[str, str]) -> str:
        old_argv, old_env = sys.argv, os.environ.get("EXPECTED_AWS_ACCOUNT")
        try:
            sys.argv = ["deploy.py", *argv]
            os.environ.pop("EXPECTED_AWS_ACCOUNT", None)
            os.environ.update(env)
            return deploy.parse_args().expected_account
        finally:
            sys.argv = old_argv
            os.environ.pop("EXPECTED_AWS_ACCOUNT", None)
            if old_env is not None:
                os.environ["EXPECTED_AWS_ACCOUNT"] = old_env

    def test_defaults_to_empty(self) -> None:
        self.assertEqual(self._parse([], {}), "")

    def test_env_fallback(self) -> None:
        self.assertEqual(
            self._parse([], {"EXPECTED_AWS_ACCOUNT": "123456789012"}),
            "123456789012",
        )

    def test_flag_wins_over_env(self) -> None:
        self.assertEqual(
            self._parse(
                ["--expected-account", "222222222222"],
                {"EXPECTED_AWS_ACCOUNT": "123456789012"},
            ),
            "222222222222",
        )


STUB_AWS = """#!/bin/bash
echo "$*" >> "${STUB_LOG}"
case "$1 $2" in
  "sts get-caller-identity")
    [ "${STUB_STS_FAIL:-}" = "1" ] && exit 255
    case "$*" in
      *Arn*) echo "arn:aws:iam::${STUB_ACCOUNT}:user/test" ;;
      *) echo "${STUB_ACCOUNT}" ;;
    esac
    exit 0 ;;
  "cloudformation delete-stack")
    # Guard passed: abort here so the rest of the teardown never runs.
    exit 99 ;;
  *) exit 98 ;;
esac
"""


class TeardownGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copy(EE_DIR / "teardown.sh", self.tmp / "teardown.sh")
        (self.tmp / ".deploy").mkdir()
        (self.tmp / ".deploy" / "test-stack.txt").write_text(
            "StackName = test-stack\nRegion = us-east-1\n"
        )
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "aws"
        stub.write_text(STUB_AWS)
        stub.chmod(0o755)
        self.log = self.tmp / "aws-calls.log"

    def _run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        full_env = {
            k: v for k, v in os.environ.items()
            if k not in ("AWS_PROFILE", "EXPECTED_AWS_ACCOUNT")
        }
        full_env.update(
            PATH=f"{self.tmp / 'bin'}:{os.environ['PATH']}",
            STUB_LOG=str(self.log),
            STUB_ACCOUNT="123456789012",
            **env,
        )
        return subprocess.run(
            ["bash", str(self.tmp / "teardown.sh"), *args, "test-stack"],
            capture_output=True, text=True, env=full_env, cwd=self.tmp,
        )

    def _calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def _assert_no_delete(self) -> None:
        for call in self._calls():
            self.assertNotIn("delete", call)

    def test_mismatch_exits_1_before_any_delete(self) -> None:
        result = self._run("--expected-account", "999999999999")
        self.assertEqual(result.returncode, 1)
        self.assertIn("mismatch", result.stderr)
        self._assert_no_delete()

    def test_env_expected_account_mismatch_exits_1(self) -> None:
        result = self._run(EXPECTED_AWS_ACCOUNT="999999999999")
        self.assertEqual(result.returncode, 1)
        self._assert_no_delete()

    def test_malformed_expected_account_exits_1(self) -> None:
        result = self._run("--expected-account", "12345")
        self.assertEqual(result.returncode, 1)
        self._assert_no_delete()

    def test_sts_failure_exits_1_with_sso_hint(self) -> None:
        result = self._run(AWS_PROFILE="work", STUB_STS_FAIL="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("aws sso login --profile work", result.stderr)
        self._assert_no_delete()

    def test_match_prints_banner_and_proceeds_to_first_delete(self) -> None:
        result = self._run("--expected-account", "123456789012",
                           AWS_PROFILE="work")
        self.assertIn(
            "Account: 123456789012  Profile: work  Region: us-east-1",
            result.stdout,
        )
        # The stub aborts the first delete-stack, so nothing past it runs.
        calls = self._calls()
        self.assertTrue(calls[-1].startswith("cloudformation delete-stack"))
        self.assertEqual(
            [c for c in calls if "delete" in c],
            [c for c in calls if c.startswith("cloudformation delete-stack")],
        )

    def test_unset_profile_warns_and_uses_default(self) -> None:
        result = self._run("--expected-account", "999999999999")
        self.assertIn("AWS_PROFILE not set, using default", result.stderr)
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()
