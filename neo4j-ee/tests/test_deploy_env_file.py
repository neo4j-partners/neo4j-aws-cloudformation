"""Unit tests for loading deploy.py flag defaults from neo4j-ee/.env."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee" / "src"))

import deploy  # noqa: E402
from neo4j_ee.envfile import load_env_file  # noqa: E402


class LoadEnvFileTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".env"
        path.write_text(text)
        return path

    def test_missing_file_loads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_env_file(Path(tmp) / ".env"), [])

    def test_loads_settings_and_returns_their_names(self) -> None:
        path = self._write("# comment\nNO_BLOOM=true\nexport REGION=\"us-east-1\"\n")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_env_file(path), ["NO_BLOOM", "REGION"])
            self.assertEqual(deploy.os.environ["NO_BLOOM"], "true")
            self.assertEqual(deploy.os.environ["REGION"], "us-east-1")

    def test_environment_wins_over_file(self) -> None:
        path = self._write("A=file\nB=file\n")
        with mock.patch.dict("os.environ", {"A": "shell"}, clear=True):
            self.assertEqual(load_env_file(path), ["B"])
            self.assertEqual(deploy.os.environ["A"], "shell")
            self.assertEqual(deploy.os.environ["B"], "file")


class ParseArgsEnvDefaultTests(unittest.TestCase):
    def _parse(self, environ: dict[str, str], argv: list[str] | None = None):
        with mock.patch.dict("os.environ", environ, clear=True):
            with mock.patch.object(sys, "argv", ["deploy.py", *(argv or [])]):
                return deploy.parse_args()

    def test_no_env_keeps_the_flag_defaults(self) -> None:
        args = self._parse({})
        self.assertEqual(args.mode, "Private")
        self.assertEqual(args.number_of_servers, 3)
        self.assertEqual(args.instance_type, "t3.medium")
        self.assertEqual(args.oidc_issuer_uri, "")
        self.assertEqual(args.advertised_dns, "")
        self.assertIsNone(args.create_private_dns)
        self.assertFalse(args.no_bloom)
        self.assertIsNone(args.region_override)

    def test_env_supplies_oidc_and_dns_settings(self) -> None:
        args = self._parse(
            {
                "OIDC_ISSUER_URI": "https://issuer.example",
                "OIDC_CLIENT_ID": "web,browser",
                "OIDC_BROWSER_CLIENT_ID": "browser",
                "OIDC_PRINCIPAL_CLAIM": "cognito:username",
                "OIDC_GROUPS_CLAIM": "cognito:groups",
                "OIDC_ADMIN_GROUP": "neo4j-admins",
                "OIDC_READER_GROUP": "neo4j-readers",
                "ADVERTISED_DNS": "neo4j-oidc.neo4j.local",
                "CREATE_PRIVATE_DNS": "true",
                "PRIVATE_DNS_ZONE": "neo4j.local",
            }
        )
        self.assertEqual(args.oidc_issuer_uri, "https://issuer.example")
        self.assertEqual(args.oidc_client_id, "web,browser")
        self.assertEqual(args.oidc_browser_client_id, "browser")
        self.assertEqual(args.oidc_principal_claim, "cognito:username")
        self.assertEqual(args.oidc_groups_claim, "cognito:groups")
        self.assertEqual(args.oidc_admin_group, "neo4j-admins")
        self.assertEqual(args.oidc_reader_group, "neo4j-readers")
        self.assertEqual(args.advertised_dns, "neo4j-oidc.neo4j.local")
        self.assertIs(args.create_private_dns, True)
        self.assertEqual(args.private_dns_zone, "neo4j.local")

    def test_env_supplies_deployment_settings(self) -> None:
        args = self._parse(
            {
                "MODE": "ExistingVpc",
                "REGION": "us-east-1",
                "NUMBER_OF_SERVERS": "1",
                "DISK_SIZE": "200",
                "NO_BLOOM": "true",
                "NO_GDS": "TRUE",
                "MARKETPLACE": "true",
                "CREATE_VPC_ENDPOINTS": "false",
                "INSTANCE_TYPE": "r8i.large",
                "EXPECTED_AWS_ACCOUNT": "385155106615",
            }
        )
        self.assertEqual(args.mode, "ExistingVpc")
        self.assertEqual(args.region_override, "us-east-1")
        self.assertEqual(args.number_of_servers, 1)
        self.assertEqual(args.disk_size, 200)
        self.assertIs(args.no_bloom, True)
        self.assertIs(args.no_gds, True)
        self.assertIs(args.marketplace, True)
        self.assertEqual(args.create_vpc_endpoints, "false")
        self.assertEqual(args.instance_type, "r8i.large")
        self.assertEqual(args.expected_account, "385155106615")

    def test_blank_value_is_ignored(self) -> None:
        args = self._parse({"MODE": "  ", "NO_BLOOM": ""})
        self.assertEqual(args.mode, "Private")
        self.assertFalse(args.no_bloom)

    def test_flag_wins_over_env(self) -> None:
        args = self._parse(
            {"OIDC_ADMIN_GROUP": "from-env", "NUMBER_OF_SERVERS": "1"},
            ["--oidc-admin-group", "from-flag", "--number-of-servers", "3"],
        )
        self.assertEqual(args.oidc_admin_group, "from-flag")
        self.assertEqual(args.number_of_servers, 3)

    def test_no_create_private_dns_flag_overrides_env_true(self) -> None:
        args = self._parse({"CREATE_PRIVATE_DNS": "true"}, ["--no-create-private-dns"])
        self.assertIs(args.create_private_dns, False)

    def test_create_private_dns_false_in_env(self) -> None:
        args = self._parse({"CREATE_PRIVATE_DNS": "false"})
        self.assertIs(args.create_private_dns, False)

    def test_no_create_private_dns_has_no_variable(self) -> None:
        args = self._parse({"NO_CREATE_PRIVATE_DNS": "true"})
        self.assertIsNone(args.create_private_dns)

    def test_invalid_values_exit_with_the_variable_name(self) -> None:
        bad = {
            "CREATE_PRIVATE_DNS": "yes",
            "NO_BLOOM": "1",
            "NUMBER_OF_SERVERS": "2",
            "DISK_SIZE": "big",
            "MODE": "Hybrid",
            "INSTANCE_TYPE": "m5.large",
        }
        for name, value in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(SystemExit) as raised:
                    self._parse({name: value})
                self.assertIn(name, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
