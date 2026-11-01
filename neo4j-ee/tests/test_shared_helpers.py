from __future__ import annotations

import contextlib
import io
from pathlib import Path
import signal
import sys
import tempfile
import types
import unittest
from unittest import mock

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
    resolve_direct_bolt_scheme,
    resolve_outputs_file,
    truthy,
)
from neo4j_ee.password import fetch_admin_password
from validate_private.checks import _java_major, _parse_key_value_lines
from validate_private.tunnel import PortForward


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

    def test_resolve_direct_bolt_scheme_never_routes(self) -> None:
        # A laptop port-forward cannot follow routing tables to the NLB's
        # private DNS name, so the scheme is always direct, 1 node or 3.
        self.assertEqual(resolve_direct_bolt_scheme({"NumberOfServers": "1"}), "bolt")
        self.assertEqual(resolve_direct_bolt_scheme({"NumberOfServers": "3"}), "bolt")
        # A caller-supplied certificate verifies against the system store.
        self.assertEqual(
            resolve_direct_bolt_scheme(
                {"NumberOfServers": "3", "AdvertisedDNS": "neo4j.example.com"}
            ),
            "bolt+s",
        )
        # The self-signed certificate deploy.py imports verifies against no
        # trust store, so the tunnel scheme skips verification.
        self.assertEqual(
            resolve_direct_bolt_scheme(
                {
                    "NumberOfServers": "1",
                    "AdvertisedDNS": "neo4j.example.com",
                    "AutoImportedCertificateArn": "arn:aws:acm:::certificate/x",
                }
            ),
            "bolt+ssc",
        )
        self.assertEqual(
            resolve_direct_bolt_scheme(
                {
                    "NumberOfServers": "3",
                    "AdvertisedDNS": "",
                    "AutoImportedCertificateArn": "x",
                }
            ),
            "bolt",
        )


class AmiHelperTests(unittest.TestCase):
    def test_marketplace_mode_does_not_require_local_ami_file(self) -> None:
        args = types.SimpleNamespace(
            marketplace=True,
            marketplace_product_code="prod-code",
            allow_old_marketplace_ami=False,
        )
        ec2 = mock.Mock()
        ec2.describe_images.return_value = {
            "Images": [
                {"ImageId": "ami-old", "CreationDate": "2026-06-01T00:00:00Z"},
                {
                    "ImageId": "ami-new",
                    "CreationDate": "2026-07-01T00:00:00Z",
                    "OwnerId": "111122223333",
                    "Name": "neo4j-ee-new",
                },
            ]
        }
        ssm = mock.Mock()
        clients = {"ec2": ec2, "ssm": ssm}

        with mock.patch("neo4j_ee.amis.boto3.client") as client:
            client.side_effect = lambda service, **_: clients[service]
            info = resolve_ami(
                args,
                region="us-east-1",
                stack_name="demo",
                script_dir=Path("/does/not/exist"),
                source_region="us-east-1",
            )

        self.assertEqual(info.source, "marketplace")
        self.assertEqual(info.ami_id, "ami-new")
        self.assertEqual(info.ssm_param_path, "/neo4j-ee/test/demo/ami-id")
        self.assertEqual(info.marketplace_product_code, "prod-code")
        self.assertEqual(info.marketplace_ami_owner, "111122223333")
        self.assertEqual(info.marketplace_ami_name, "neo4j-ee-new")
        self.assertIsNone(info.copied_ami_id)
        ssm.put_parameter.assert_called_once_with(
            Name="/neo4j-ee/test/demo/ami-id",
            Type="String",
            Value="ami-new",
            Overwrite=True,
        )


    def _resolve_with_listing_image(self, created: str, allow_old: bool):
        args = types.SimpleNamespace(
            marketplace=True,
            marketplace_product_code="prod-code",
            allow_old_marketplace_ami=allow_old,
        )
        ec2 = mock.Mock()
        ec2.describe_images.return_value = {
            "Images": [{"ImageId": "ami-listing", "CreationDate": created}]
        }
        clients = {"ec2": ec2, "ssm": mock.Mock()}
        with mock.patch("neo4j_ee.amis.boto3.client") as client:
            client.side_effect = lambda service, **_: clients[service]
            return resolve_ami(
                args,
                region="us-east-1",
                stack_name="demo",
                script_dir=Path("/does/not/exist"),
                source_region="us-east-1",
            )

    def test_marketplace_mode_rejects_ami_older_than_bootstrap_contract(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self._resolve_with_listing_image(
                "2025-12-17T10:00:00.000Z", allow_old=False
            )

        self.assertIn("ami-listing", str(ctx.exception))
        self.assertIn("2025-12-17", str(ctx.exception))

    def test_marketplace_mode_allows_old_ami_with_override(self) -> None:
        info = self._resolve_with_listing_image(
            "2025-12-17T10:00:00.000Z", allow_old=True
        )

        self.assertEqual(info.ami_id, "ami-listing")


class FetchAdminPasswordTests(unittest.TestCase):
    def test_reads_the_secret_the_record_names(self) -> None:
        fields = {"Neo4jPasswordSecretArn": "arn:secret", "Region": "us-east-2"}
        client = mock.Mock()
        client.get_secret_value.return_value = {"SecretString": "pw"}
        with mock.patch("neo4j_ee.password.boto3.client", return_value=client) as make:
            self.assertEqual(fetch_admin_password(fields, Path("r.txt")), "pw")
        make.assert_called_once_with("secretsmanager", region_name="us-east-2")
        client.get_secret_value.assert_called_once_with(SecretId="arn:secret")

    def test_missing_secret_arn_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "Neo4jPasswordSecretArn"):
            fetch_admin_password({"Region": "us-east-1"}, Path("r.txt"))


class RunUntilInterruptedTests(unittest.TestCase):
    def _forward(self) -> PortForward:
        forward = PortForward("us-east-1", "i-bastion", "nlb.internal", 7687, 7687)
        forward._proc = mock.Mock(pid=4242)
        return forward

    def test_sigterm_handler_stays_installed_until_close_returns(self) -> None:
        forward = self._forward()
        before = signal.getsignal(signal.SIGTERM)
        seen = []

        def close() -> None:
            seen.append(signal.getsignal(signal.SIGTERM))
            forward._proc = None

        with (
            mock.patch.object(forward, "start", side_effect=KeyboardInterrupt),
            mock.patch.object(forward, "close", side_effect=close),
        ):
            self.assertIsNone(forward.run_until_interrupted())
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], before)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)

    def test_second_interrupt_during_cleanup_is_absorbed(self) -> None:
        forward = self._forward()
        before = signal.getsignal(signal.SIGTERM)
        with (
            mock.patch.object(forward, "start", side_effect=KeyboardInterrupt),
            mock.patch.object(forward, "close", side_effect=KeyboardInterrupt),
            mock.patch("validate_private.tunnel.os.killpg") as killpg,
        ):
            self.assertIsNone(forward.run_until_interrupted())
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)


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

    def test_create_stack_and_wait_reports_stack_id_before_wait(self) -> None:
        cfn = FakeCloudFormation(outputs={})
        seen: list[tuple[str, str]] = []

        def on_created(stack_id: str) -> None:
            seen.append((stack_id, cfn.waited_for))

        with contextlib.redirect_stdout(io.StringIO()):
            create_stack_and_wait(
                cfn,
                "demo",
                "https://example.com/template.yaml",
                [],
                on_stack_created=on_created,
            )

        # Called once with the StackId, while no waiter had started yet.
        self.assertEqual(seen, [(FAKE_STACK_ID, "")])


FAKE_STACK_ID = (
    "arn:aws:cloudformation:us-east-1:123456789012:stack/demo/abc-123"
)


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

    def create_stack(self, **kwargs: object) -> dict[str, str]:
        self.created_stack = kwargs
        return {"StackId": FAKE_STACK_ID}

    def get_waiter(self, name: str) -> "FakeCloudFormation":
        self.waited_for = name
        return self

    def wait(self, **kwargs: object) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
