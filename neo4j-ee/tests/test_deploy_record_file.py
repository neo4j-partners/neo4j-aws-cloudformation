"""Unit tests for the deploy.py record helpers (no AWS calls).

The record holds stack outputs and deploy inputs. It never holds the Neo4j
password, which lives only in Secrets Manager.
"""

from __future__ import annotations

import ast
from pathlib import Path
import stat
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))

import deploy  # noqa: E402

OUTPUT_LINES = ["Neo4jURI            = neo4j+ssc://example:7687"]
EXTRA = [
    ("StackName", "test-ee-1"),
    ("Region", "us-east-1"),
]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class FormatRecordTests(unittest.TestCase):
    def test_renders_outputs_and_extra_fields(self) -> None:
        text = deploy.format_record(OUTPUT_LINES, EXTRA)
        self.assertIn(OUTPUT_LINES[0], text)
        self.assertIn("StackName            = test-ee-1", text)
        self.assertIn("Region               = us-east-1", text)

    def test_deploy_records_no_password_field(self) -> None:
        # Every ("Key", value) tuple literal in deploy.py is a record field.
        # The Password CFN parameter is a dict, so it does not match.
        tree = ast.parse((REPO_ROOT / "neo4j-ee" / "deploy.py").read_text())
        keys = {
            node.elts[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Tuple)
            and len(node.elts) == 2
            and isinstance(node.elts[0], ast.Constant)
            and isinstance(node.elts[0].value, str)
        }
        self.assertIn("StackName", keys)
        self.assertNotIn("Password", keys)


class WriteRecordFileTests(unittest.TestCase):
    def test_new_file_is_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stack.txt"
            deploy.write_record_file(path, "StackName = abc\n")
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(path.read_text(), "StackName = abc\n")

    def test_overwriting_0644_file_ends_0600(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stack.txt"
            path.write_text("old contents that are longer than the new ones\n")
            path.chmod(0o644)
            deploy.write_record_file(path, "StackName = abc\n")
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(path.read_text(), "StackName = abc\n")


if __name__ == "__main__":
    unittest.main()
