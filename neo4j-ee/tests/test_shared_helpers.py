from __future__ import annotations

import contextlib
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee" / "src"))
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee" / "validate-private" / "src"))

from neo4j_ee.amis import resolve_ami
from neo4j_ee.cloudformation import create_stack_and_wait, nlb_dns_from_outputs
from neo4j_ee.outputs import (
    latest_outputs_file,
    parse_key_value_text,
    read_outputs,
    require_field,
    resolve_bolt_scheme,
    resolve_outputs_file,
    truthy,
)
from validate_private.checks import _java_major, _parse_key_value_lines


class OutputHelperTests(unittest.TestCase):
    def test_parse_key_value_text_ignores_non_assignments(self) -> None:
        fields = parse_key_value_text(
            "StackName = demo\n"
            "ignored line\n"
            "InstallBloom=true\n"
            "Password = abc=123\n"
        )

        self.assertEqual(fields["StackName"], "demo")
        self.assertEqual(fields["InstallBloom"], "true")
        self.assertEqual(fields["Password"], "abc=123")
        self.assertNotIn("ignored line", fields)

    def test_resolve_outputs_file_uses_stack_name_or_latest_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            deploy_dir = Path(tmp)
            older = deploy_dir / "older.txt"
            newer = deploy_dir / "newer.txt"
            older.write_text("StackName = older\n")
            newer.write_text("StackName = newer\n")

            self.assertEqual(resolve_outputs_file(deploy_dir, "older"), older)
            self.assertEqual(latest_outputs_file(deploy_dir), newer)
            self.assertEqual(read_outputs(newer)["StackName"], "newer")

    def test_require_field_and_truthy_helpers(self) -> None:
        source = Path("demo.txt")
        fields = {"StackName": "demo", "InstallGDS": "yes"}

        self.assertEqual(require_field(fields, "StackName", source), "demo")
        self.assertTrue(truthy(fields["InstallGDS"]))
        self.assertFalse(truthy(""))
        with self.assertRaisesRegex(ValueError, "Could not read Password"):
            require_field(fields, "Password", source)

    def test_resolve_bolt_scheme_uses_cluster_and_tls_outputs(self) -> None:
        # No AdvertisedDNS => TLS off => plain scheme.
        self.assertEqual(resolve_bolt_scheme({"NumberOfServers": "1"}), "bolt")
        self.assertEqual(resolve_bolt_scheme({"NumberOfServers": "3"}), "neo4j")
        # AdvertisedDNS present => TLS on => +ssc (encrypted, trust-any), which
        # is correct for both real-ACM and self-signed test certs.
        self.assertEqual(
            resolve_bolt_scheme(
                {"NumberOfServers": "1", "AdvertisedDNS": "neo4j.example.com"}
            ),
            "bolt+ssc",
        )
        self.assertEqual(
            resolve_bolt_scheme(
                {"NumberOfServers": "3", "AdvertisedDNS": "neo4j.example.com"}
            ),
            "neo4j+ssc",
        )
        # An empty AdvertisedDNS (Public without --enable-public-tls) stays plain.
        self.assertEqual(
            resolve_bolt_scheme({"NumberOfServers": "3", "AdvertisedDNS": ""}),
            "neo4j",
        )


class AmiHelperTests(unittest.TestCase):
    def test_marketplace_mode_does_not_require_local_ami_file(self) -> None:
        args = types.SimpleNamespace(marketplace=True)

        info = resolve_ami(
            args,
            region="us-east-1",
            stack_name="demo",
            script_dir=Path("/does/not/exist"),
            source_region="us-east-1",
        )

        self.assertEqual(info.source, "marketplace")
        self.assertEqual(info.ami_id, "")
        self.assertEqual(info.ssm_param_path, "")
        self.assertIsNone(info.copied_ami_id)


class ValidatePrivateHelperTests(unittest.TestCase):
    def test_java_major_parses_modern_and_legacy_version_lines(self) -> None:
        self.assertEqual(_java_major('openjdk version "21.0.6" 2025-01-21 LTS'), 21)
        self.assertEqual(_java_major('java version "1.8.0_412"'), 8)
        self.assertIsNone(_java_major("not a java version line"))

    def test_parse_key_value_lines_ignores_non_key_value_output(self) -> None:
        values = _parse_key_value_lines(
            "neo4j_rpm_version=2025.04.0\n"
            "noise\n"
            "java_version=openjdk version \"21.0.6\" 2025-01-21 LTS\n"
        )

        self.assertEqual(values["neo4j_rpm_version"], "2025.04.0")
        self.assertEqual(values["java_version"], 'openjdk version "21.0.6" 2025-01-21 LTS')
        self.assertNotIn("noise", values)


class CloudFormationHelperTests(unittest.TestCase):
    def test_nlb_dns_prefers_internal_dns_output(self) -> None:
        cfn = FakeCloudFormation(
            outputs={"Neo4jInternalDNS": "internal.example.com"}
        )

        self.assertEqual(nlb_dns_from_outputs(cfn, "demo"), "internal.example.com")

    def test_nlb_dns_falls_back_to_uri_hostname(self) -> None:
        cfn = FakeCloudFormation(
            outputs={"Neo4jBoltUrl": "neo4j://lb.example.com:7687"}
        )

        self.assertEqual(nlb_dns_from_outputs(cfn, "demo"), "lb.example.com")

    def test_create_stack_and_wait_passes_expected_stack_inputs(self) -> None:
        cfn = FakeCloudFormation(outputs={})
        params = [{"ParameterKey": "Password", "ParameterValue": "secret"}]

        with contextlib.redirect_stdout(io.StringIO()):
            create_stack_and_wait(
                cfn,
                "demo",
                "https://example.com/template.yaml",
                params,
            )

        self.assertEqual(cfn.created_stack["StackName"], "demo")
        self.assertEqual(
            cfn.created_stack["TemplateURL"],
            "https://example.com/template.yaml",
        )
        self.assertEqual(cfn.created_stack["Capabilities"], ["CAPABILITY_IAM"])
        self.assertTrue(cfn.created_stack["DisableRollback"])
        self.assertEqual(cfn.created_stack["Parameters"], params)
        self.assertEqual(cfn.waited_for, "stack_create_complete")


class FakeCloudFormation:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.created_stack: dict[str, object] = {}
        self.waited_for = ""

    def describe_stacks(self, StackName: str) -> dict[str, list[dict[str, object]]]:
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {"OutputKey": key, "OutputValue": value}
                        for key, value in self.outputs.items()
                    ]
                }
            ]
        }

    def create_stack(self, **kwargs: object) -> None:
        self.created_stack = kwargs

    def get_waiter(self, name: str) -> "FakeCloudFormation":
        self.waited_for = name
        return self

    def wait(self, **kwargs: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
