"""Load EE Private stack configuration from the deploy file and Secrets Manager."""

from __future__ import annotations

import dataclasses
import getpass
import sys
from pathlib import Path

import boto3

_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_NEO4J_EE_DIR / "src"))
from neo4j_ee.outputs import read_outputs, resolve_bolt_scheme, truthy  # noqa: E402


_REQUIRED_FIELDS = ("StackName", "Region", "Neo4jOperatorBastionId", "Neo4jInternalDNS")


@dataclasses.dataclass(frozen=True)
class StackConfig:
    stack_name: str
    region: str
    bastion_id: str
    nlb_dns: str
    bolt_scheme: str
    password: str
    install_apoc: bool
    install_gds: bool
    install_bloom: bool
    advertised_dns: str
    certificate_arn: str
    create_private_dns: bool


def _fetch_secret(secret_name: str, region: str) -> str:
    sm = boto3.client("secretsmanager", region_name=region)
    return sm.get_secret_value(SecretId=secret_name)["SecretString"]


def load_config(
    outputs_path: Path,
    password_override: str | None = None,
) -> StackConfig:
    if not outputs_path.exists():
        raise FileNotFoundError(
            f"{outputs_path} not found. Run deploy.py first to create a stack."
        )

    fields = read_outputs(outputs_path)

    missing = [f for f in _REQUIRED_FIELDS if f not in fields]
    if missing:
        raise ValueError(
            f"Required field(s) missing from {outputs_path.name}: {', '.join(missing)}"
        )

    deployment_mode = fields.get("DeploymentMode", "Public")
    if deployment_mode not in ("Private", "ExistingVpc"):
        raise ValueError(
            f"validate-private only supports Private-mode stacks. "
            f"This stack has DeploymentMode={deployment_mode}."
        )

    edition = fields.get("Edition", "").lower()
    if edition and edition != "ee":
        raise ValueError(
            f"validate-private only supports Enterprise Edition stacks. "
            f"This stack has Edition={edition}."
        )

    stack_name = fields["StackName"]
    region = fields["Region"]
    secret_name = f"neo4j/{stack_name}/password"
    bolt_scheme = resolve_bolt_scheme(fields)

    if password_override is not None:
        password = password_override
    else:
        try:
            password = _fetch_secret(secret_name, region)
        except Exception:
            password = fields.get("Password", "")

    if not password and sys.stdin.isatty():
        password = getpass.getpass("Enter neo4j password: ")
    if not password:
        raise ValueError(
            f"No password available. Ensure the Secrets Manager secret "
            f"'{secret_name}' exists, or provide --password."
        )

    return StackConfig(
        stack_name=stack_name,
        region=region,
        bastion_id=fields["Neo4jOperatorBastionId"],
        nlb_dns=fields["Neo4jInternalDNS"],
        bolt_scheme=bolt_scheme,
        password=password,
        install_apoc=truthy(fields.get("InstallAPOC")),
        install_gds=truthy(fields.get("InstallGDS")),
        install_bloom=truthy(fields.get("InstallBloom")),
        advertised_dns=fields.get("AdvertisedDNS", ""),
        certificate_arn=(
            fields.get("CertificateArn")
            or fields.get("AutoImportedCertificateArn", "")
        ),
        # Neo4jPrivateDnsHostedZoneId is emitted by outputs-private.yaml only
        # under the CreatePrivateDns condition, so its presence is the
        # authoritative signal that the stack owns an in-VPC record for
        # AdvertisedDNS (no extra AWS call needed).
        create_private_dns=bool(fields.get("Neo4jPrivateDnsHostedZoneId")),
    )
