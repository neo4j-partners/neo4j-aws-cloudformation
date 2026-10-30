"""Unit tests for deploy.py pure helpers (no AWS calls).

resolve_tls_plan and build_cfn_parameters are pure so the TLS/DNS decision and
the CloudFormation parameter set can be verified without creating a stack
(worklog/tls.md phase 7).
"""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "neo4j-ee"))

import deploy  # noqa: E402


def _params(pairs: list[dict[str, str]]) -> dict[str, str]:
    return {p["ParameterKey"]: p["ParameterValue"] for p in pairs}


class ResolveTlsPlanTests(unittest.TestCase):
    def test_private_default_self_signed(self) -> None:
        plan = deploy.resolve_tls_plan(
            mode="Private",
            stack_name="test-ee-1",
            cert_arn="",
            advertised_dns="",
            enable_public_tls=False,
            create_private_dns=False,
            private_dns_zone="",
            private_dns_hosted_zone_id="",
        )
        self.assertEqual(plan.advertised_dns, "neo4j-test-ee-1.neo4j.local")
        self.assertTrue(plan.needs_self_signed_import)
        self.assertFalse(plan.create_private_dns)
        self.assertEqual(plan.cert_arn, "")

    def test_private_supplied_cert(self) -> None:
        plan = deploy.resolve_tls_plan(
            mode="Private",
            stack_name="s",
            cert_arn="arn:aws:acm:us-east-1:1:certificate/abc",
            advertised_dns="neo4j.example.com",
            enable_public_tls=False,
            create_private_dns=False,
            private_dns_zone="",
            private_dns_hosted_zone_id="",
        )
        self.assertFalse(plan.needs_self_signed_import)
        self.assertEqual(plan.advertised_dns, "neo4j.example.com")
        self.assertEqual(plan.cert_arn, "arn:aws:acm:us-east-1:1:certificate/abc")

    def test_existing_vpc_with_private_dns_zone(self) -> None:
        plan = deploy.resolve_tls_plan(
            mode="ExistingVpc",
            stack_name="s",
            cert_arn="",
            advertised_dns="",
            enable_public_tls=False,
            create_private_dns=True,
            private_dns_zone="neo4j.local",
            private_dns_hosted_zone_id="",
        )
        self.assertTrue(plan.create_private_dns)
        self.assertEqual(plan.private_dns_zone, "neo4j.local")
        self.assertTrue(plan.needs_self_signed_import)

    def test_existing_vpc_with_private_dns_hosted_zone_id(self) -> None:
        plan = deploy.resolve_tls_plan(
            mode="ExistingVpc",
            stack_name="s",
            cert_arn="",
            advertised_dns="",
            enable_public_tls=False,
            create_private_dns=True,
            private_dns_zone="",
            private_dns_hosted_zone_id="Z123",
        )
        self.assertTrue(plan.create_private_dns)
        self.assertEqual(plan.private_dns_hosted_zone_id, "Z123")

    def test_create_private_dns_without_zone_rejected(self) -> None:
        with self.assertRaises(ValueError):
            deploy.resolve_tls_plan(
                mode="Private",
                stack_name="s",
                cert_arn="",
                advertised_dns="",
                enable_public_tls=False,
                create_private_dns=True,
                private_dns_zone="",
                private_dns_hosted_zone_id="",
            )

    def test_public_without_tls(self) -> None:
        plan = deploy.resolve_tls_plan(
            mode="Public",
            stack_name="s",
            cert_arn="",
            advertised_dns="",
            enable_public_tls=False,
            create_private_dns=False,
            private_dns_zone="",
            private_dns_hosted_zone_id="",
        )
        self.assertFalse(plan.enable_public_tls)
        self.assertFalse(plan.needs_self_signed_import)
        self.assertEqual(plan.cert_arn, "")

    def test_public_with_tls(self) -> None:
        plan = deploy.resolve_tls_plan(
            mode="Public",
            stack_name="s",
            cert_arn="arn:aws:acm:us-east-1:1:certificate/abc",
            advertised_dns="neo4j.example.com",
            enable_public_tls=True,
            create_private_dns=False,
            private_dns_zone="",
            private_dns_hosted_zone_id="",
        )
        self.assertTrue(plan.enable_public_tls)
        self.assertFalse(plan.needs_self_signed_import)

    def test_public_tls_inputs_without_enable_rejected(self) -> None:
        # Item 8: fail fast instead of silently shipping a plain-TCP stack.
        with self.assertRaises(ValueError):
            deploy.resolve_tls_plan(
                mode="Public",
                stack_name="s",
                cert_arn="arn:aws:acm:us-east-1:1:certificate/abc",
                advertised_dns="neo4j.example.com",
                enable_public_tls=False,
                create_private_dns=False,
                private_dns_zone="",
                private_dns_hosted_zone_id="",
            )

    def test_public_enable_tls_without_cert_rejected(self) -> None:
        with self.assertRaises(ValueError):
            deploy.resolve_tls_plan(
                mode="Public",
                stack_name="s",
                cert_arn="",
                advertised_dns="",
                enable_public_tls=True,
                create_private_dns=False,
                private_dns_zone="",
                private_dns_hosted_zone_id="",
            )

    def test_public_private_dns_flags_rejected(self) -> None:
        # Item 4: Public never creates Route 53 private DNS.
        with self.assertRaises(ValueError):
            deploy.resolve_tls_plan(
                mode="Public",
                stack_name="s",
                cert_arn="",
                advertised_dns="",
                enable_public_tls=False,
                create_private_dns=True,
                private_dns_zone="neo4j.local",
                private_dns_hosted_zone_id="",
            )


class BuildCfnParametersTests(unittest.TestCase):
    def _base_kwargs(self, mode: str, tls: deploy.TlsPlan) -> dict:
        return dict(
            password="pw",
            number_of_servers=3,
            instance_type="t3.medium",
            allowed_cidr="10.0.0.0/16",
            install_bloom="true",
            install_gds="true",
            bloom_license_secret_arn="arn:bloom",
            gds_license_secret_arn="arn:gds",
            ssm_param_path=None,
            alert_email=None,
            disk_size=None,
            snapshot_id=None,
            mode=mode,
            existing_vpc=None,
            tls=tls,
        )

    def test_private_self_signed_params(self) -> None:
        tls = deploy.resolve_tls_plan(
            mode="Private", stack_name="s", cert_arn="", advertised_dns="",
            enable_public_tls=False, create_private_dns=False,
            private_dns_zone="", private_dns_hosted_zone_id="",
        )
        # Caller supplies the imported ARN before building params.
        tls = deploy.replace(tls, cert_arn="arn:imported")
        params = _params(deploy.build_cfn_parameters(**self._base_kwargs("Private", tls)))
        self.assertEqual(params["CertificateArn"], "arn:imported")
        self.assertEqual(params["AdvertisedDNS"], "neo4j-s.neo4j.local")
        self.assertEqual(params["CreatePrivateDns"], "false")
        self.assertNotIn("PrivateDnsZoneName", params)

    def test_private_with_private_dns_zone_params(self) -> None:
        tls = deploy.resolve_tls_plan(
            mode="Private", stack_name="s",
            cert_arn="arn:user", advertised_dns="neo4j.example.com",
            enable_public_tls=False, create_private_dns=True,
            private_dns_zone="neo4j.local", private_dns_hosted_zone_id="",
        )
        params = _params(deploy.build_cfn_parameters(**self._base_kwargs("Private", tls)))
        self.assertEqual(params["CreatePrivateDns"], "true")
        self.assertEqual(params["PrivateDnsZoneName"], "neo4j.local")
        self.assertNotIn("PrivateDnsHostedZoneId", params)

    def test_public_no_tls_omits_tls_params(self) -> None:
        tls = deploy.resolve_tls_plan(
            mode="Public", stack_name="s", cert_arn="", advertised_dns="",
            enable_public_tls=False, create_private_dns=False,
            private_dns_zone="", private_dns_hosted_zone_id="",
        )
        params = _params(deploy.build_cfn_parameters(**self._base_kwargs("Public", tls)))
        self.assertNotIn("EnableTLS", params)
        self.assertNotIn("CertificateArn", params)
        self.assertNotIn("AdvertisedDNS", params)
        self.assertNotIn("CreatePrivateDns", params)

    def test_public_tls_params(self) -> None:
        tls = deploy.resolve_tls_plan(
            mode="Public", stack_name="s",
            cert_arn="arn:user", advertised_dns="neo4j.example.com",
            enable_public_tls=True, create_private_dns=False,
            private_dns_zone="", private_dns_hosted_zone_id="",
        )
        params = _params(deploy.build_cfn_parameters(**self._base_kwargs("Public", tls)))
        self.assertEqual(params["EnableTLS"], "true")
        self.assertEqual(params["CertificateArn"], "arn:user")
        self.assertEqual(params["AdvertisedDNS"], "neo4j.example.com")
        self.assertNotIn("CreatePrivateDns", params)

    def test_existing_vpc_params(self) -> None:
        tls = deploy.resolve_tls_plan(
            mode="ExistingVpc", stack_name="s",
            cert_arn="arn:user", advertised_dns="neo4j.example.com",
            enable_public_tls=False, create_private_dns=True,
            private_dns_zone="", private_dns_hosted_zone_id="Z123",
        )
        kwargs = self._base_kwargs("ExistingVpc", tls)
        kwargs["existing_vpc"] = deploy.ExistingVpcInputs(
            vpc_id="vpc-1",
            subnet_1="subnet-1",
            private_route_table_1="rtb-1",
            subnet_2="subnet-2",
            subnet_3="subnet-3",
            create_vpc_endpoints="true",
            existing_endpoint_sg_id="",
        )
        params = _params(deploy.build_cfn_parameters(**kwargs))
        self.assertEqual(params["VpcId"], "vpc-1")
        self.assertEqual(params["PrivateSubnet2Id"], "subnet-2")
        self.assertEqual(params["PrivateSubnet3Id"], "subnet-3")
        self.assertEqual(params["CreatePrivateDns"], "true")
        self.assertEqual(params["PrivateDnsHostedZoneId"], "Z123")
        self.assertNotIn("ExistingEndpointSgId", params)

    def test_existing_vpc_without_inputs_raises(self) -> None:
        tls = deploy.resolve_tls_plan(
            mode="ExistingVpc", stack_name="s",
            cert_arn="arn:user", advertised_dns="neo4j.example.com",
            enable_public_tls=False, create_private_dns=False,
            private_dns_zone="", private_dns_hosted_zone_id="",
        )
        with self.assertRaises(ValueError):
            deploy.build_cfn_parameters(**self._base_kwargs("ExistingVpc", tls))

    def _private_tls(self) -> deploy.TlsPlan:
        return deploy.resolve_tls_plan(
            mode="Private", stack_name="s",
            cert_arn="arn:user", advertised_dns="neo4j.example.com",
            enable_public_tls=False, create_private_dns=False,
            private_dns_zone="", private_dns_hosted_zone_id="",
        )

    def test_oidc_omitted_by_default(self) -> None:
        params = _params(
            deploy.build_cfn_parameters(**self._base_kwargs("Private", self._private_tls()))
        )
        self.assertFalse([key for key in params if key.startswith("Oidc")])

    def test_oidc_params_pass_only_set_values(self) -> None:
        kwargs = self._base_kwargs("Private", self._private_tls())
        kwargs["oidc"] = deploy.OidcInputs(
            issuer_uri="https://idp.example.com/pool",
            client_id="client-a,client-b",
            browser_client_id="client-b",
            groups_claim="cognito:groups",
            admin_group="neo4j-admins",
        )
        params = _params(deploy.build_cfn_parameters(**kwargs))
        self.assertEqual(
            {key: value for key, value in params.items() if key.startswith("Oidc")},
            {
                "OidcIssuerUri": "https://idp.example.com/pool",
                "OidcClientId": "client-a,client-b",
                "OidcBrowserClientId": "client-b",
                "OidcGroupsClaim": "cognito:groups",
                "OidcAdminGroup": "neo4j-admins",
            },
        )


if __name__ == "__main__":
    unittest.main()
