"""Unit tests for deploy.get_caller_identity (no AWS calls; fake sts client)."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from botocore.exceptions import ClientError, NoCredentialsError, TokenRetrievalError

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))

import deploy  # noqa: E402


class FakeSts:
    def __init__(self, response: dict[str, str] | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = 0

    def get_caller_identity(self) -> dict[str, str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class GetCallerIdentityTests(unittest.TestCase):
    def test_success_returns_account_and_arn(self) -> None:
        sts = FakeSts(
            {
                "Account": "123456789012",
                "Arn": "arn:aws:sts::123456789012:assumed-role/x/y",
                "UserId": "AROAEXAMPLE:y",
            }
        )
        identity = deploy.get_caller_identity(client=sts)
        self.assertEqual(identity["Account"], "123456789012")
        self.assertEqual(identity["Arn"], "arn:aws:sts::123456789012:assumed-role/x/y")
        self.assertEqual(sts.calls, 1)

    def _exit_message(self, error: Exception) -> str:
        with mock.patch.dict(os.environ, {"AWS_PROFILE": "marketplace"}):
            with self.assertRaises(SystemExit) as ctx:
                deploy.get_caller_identity(client=FakeSts(error=error))
        return str(ctx.exception.code)

    def test_token_retrieval_error_exits_with_login_hint(self) -> None:
        msg = self._exit_message(
            TokenRetrievalError(provider="sso", error_msg="expired")
        )
        self.assertIn("aws sso login --profile marketplace", msg)
        self.assertIn("TokenRetrievalError", msg)

    def test_no_credentials_error_exits_cleanly(self) -> None:
        msg = self._exit_message(NoCredentialsError())
        self.assertIn("aws sso login --profile marketplace", msg)
        self.assertIn("NoCredentialsError", msg)

    def test_client_error_exits_cleanly(self) -> None:
        err = ClientError(
            {"Error": {"Code": "ExpiredToken", "Message": "secret-detail"}},
            "GetCallerIdentity",
        )
        msg = self._exit_message(err)
        self.assertIn("aws sso login --profile marketplace", msg)
        self.assertIn("ClientError", msg)
        self.assertNotIn("Traceback", msg)
        self.assertNotIn("secret-detail", msg)

    def test_unexpected_exception_is_not_swallowed(self) -> None:
        with self.assertRaises(RuntimeError):
            deploy.get_caller_identity(client=FakeSts(error=RuntimeError("boom")))

    def test_sts_client_gets_fallback_region_when_none_given(self) -> None:
        fake = FakeSts({"Account": "123456789012", "Arn": "arn:aws:sts::1:x"})
        with mock.patch.object(deploy.boto3, "client", return_value=fake) as make:
            deploy.get_caller_identity()
        make.assert_called_once_with("sts", region_name="us-east-1")

    def test_sts_client_keeps_explicit_region(self) -> None:
        fake = FakeSts({"Account": "123456789012", "Arn": "arn:aws:sts::1:x"})
        with mock.patch.object(deploy.boto3, "client", return_value=fake) as make:
            deploy.get_caller_identity("eu-west-1")
        make.assert_called_once_with("sts", region_name="eu-west-1")


if __name__ == "__main__":
    unittest.main()
