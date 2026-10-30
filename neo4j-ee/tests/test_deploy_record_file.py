"""Unit tests for the deploy.py record helpers (no AWS calls).

The generated Neo4j password must reach the 0600 record file but never the
terminal output.
"""

from __future__ import annotations

from pathlib import Path
import stat
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))

import deploy  # noqa: E402

PASSWORD = "S3cretPassw0rdValue"
OUTPUT_LINES = ["Neo4jURI            = neo4j+ssc://example:7687"]
EXTRA = [
    ("StackName", "test-ee-1"),
    ("Password", PASSWORD),
    ("Region", "us-east-1"),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class FormatRecordTests(unittest.TestCase):
    def test_file_view_contains_password_key_and_value(self) -> None:
        text = deploy.format_record(OUTPUT_LINES, EXTRA)
        self.assertIn(f"Password             = {PASSWORD}", text)

    def test_terminal_view_has_no_password_value(self) -> None:
        text = deploy.format_record(
            OUTPUT_LINES,
            EXTRA,
            secret_keys=("Password",),
            mask="<stored in file>",
        )
        self.assertNotIn(PASSWORD, text)
        self.assertIn("Password             = <stored in file>", text)

    def test_terminal_view_keeps_other_fields(self) -> None:
        text = deploy.format_record(
            OUTPUT_LINES, EXTRA, secret_keys=("Password",), mask="x"
        )
        self.assertIn("StackName            = test-ee-1", text)
        self.assertIn("Region               = us-east-1", text)
        self.assertIn(OUTPUT_LINES[0], text)


class WriteRecordFileTests(unittest.TestCase):
    def test_new_file_is_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stack.txt"
            deploy.write_record_file(path, "Password = abc\n")
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(path.read_text(), "Password = abc\n")

    def test_overwriting_0644_file_ends_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stack.txt"
            path.write_text("old contents that are longer than the new ones\n")
            path.chmod(0o644)
            deploy.write_record_file(path, "Password = abc\n")
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(path.read_text(), "Password = abc\n")


if __name__ == "__main__":
    unittest.main()
