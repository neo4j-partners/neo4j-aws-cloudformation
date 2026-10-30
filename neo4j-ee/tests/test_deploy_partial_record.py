"""Unit tests for deploy.write_partial_record (no AWS calls).

The .failed record lets teardown.sh clean up a deploy whose stack create
failed or was interrupted, without being picked up as the "latest stack" by
tooling that globs .deploy/*.txt.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))

import deploy  # noqa: E402


def _parse(path: Path) -> dict[str, str]:
    fields = {}
    for line in path.read_text().splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


class WritePartialRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.deploy_dir = Path(self._tmp.name) / ".deploy"

    def _write(self, **kwargs: object) -> Path:
        return deploy.write_partial_record(
            self.deploy_dir, "test-ee-1", "us-east-1", "Private", **kwargs
        )

    def test_writes_failed_suffix_not_txt(self) -> None:
        path = self._write()
        self.assertEqual(path, self.deploy_dir / "test-ee-1.failed")
        self.assertTrue(path.is_file())
        self.assertEqual(list(self.deploy_dir.glob("*.txt")), [])

    def test_mode_is_0600(self) -> None:
        path = self._write()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_mode_is_0600_when_overwriting_looser_file(self) -> None:
        self.deploy_dir.mkdir()
        existing = self.deploy_dir / "test-ee-1.failed"
        existing.write_text("old\n")
        existing.chmod(0o644)
        path = self._write()
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_required_keys(self) -> None:
        fields = _parse(self._write())
        self.assertEqual(
            fields,
            {
                "StackName": "test-ee-1",
                "Region": "us-east-1",
                "DeploymentMode": "Private",
            },
        )

    def test_all_optional_keys(self) -> None:
        fields = _parse(
            self._write(
                ssm_param_path="/neo4j/test-ee-1/ami",
                copied_ami_id="ami-0123",
                license_secret_arns=["arn:one", "arn:two"],
                auto_imported_cert_arn="arn:cert",
                stack_id="arn:stack",
            )
        )
        self.assertEqual(fields["SSMParamPath"], "/neo4j/test-ee-1/ami")
        self.assertEqual(fields["CopiedAmiId"], "ami-0123")
        self.assertEqual(fields["AutoCreatedLicenseSecretArns"], "arn:one,arn:two")
        self.assertEqual(fields["AutoImportedCertificateArn"], "arn:cert")
        self.assertEqual(fields["StackID"], "arn:stack")

    def test_omits_empty_optional_keys(self) -> None:
        text = self._write(
            ssm_param_path="",
            copied_ami_id="",
            license_secret_arns=[],
            auto_imported_cert_arn="",
            stack_id="",
        ).read_text()
        for key in (
            "SSMParamPath",
            "CopiedAmiId",
            "AutoCreatedLicenseSecretArns",
            "AutoImportedCertificateArn",
            "StackID",
        ):
            self.assertNotIn(key, text)

    def test_omits_password(self) -> None:
        text = self._write(stack_id="arn:stack").read_text()
        self.assertNotIn("Password", text)

    def test_rewrite_adds_stack_id(self) -> None:
        self.assertNotIn("StackID", _parse(self._write()))
        self.assertEqual(_parse(self._write(stack_id="arn:stack"))["StackID"], "arn:stack")

    def test_format_matches_teardown_read_field(self) -> None:
        # teardown.sh read_field: grep "^Key" | sed 's/^[^=]*= *//'
        text = self._write(stack_id="arn:stack").read_text()
        self.assertIn("StackID              = arn:stack\n", text)


if __name__ == "__main__":
    unittest.main()
