#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

import argparse
import atexit
from dataclasses import dataclass, replace
import os
from pathlib import Path
import random
import secrets
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / "src"))
sys.path.insert(0, str(SCRIPT_DIR / "scripts"))
import certificate  # noqa: E402  (uv-script under scripts/; we call _import_self_signed)
from neo4j_ee.amis import resolve_ami  # noqa: E402
from neo4j_ee.cloudformation import (  # noqa: E402
    create_stack_and_wait,
    upload_template_to_s3,
)
from neo4j_ee.licenses import resolve_license_secret_arns  # noqa: E402
from neo4j_ee.outputs import parse_key_value_text  # noqa: E402

SOURCE_REGION = "us-east-1"
SUPPORTED_REGIONS = [
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2",
]
# Mirrors the InstanceType AllowedValues block in templates/neo4j-private
# .template.yaml, neo4j-public.template.yaml, and
# neo4j-private-existing-vpc.template.yaml. Keep these in sync.
INSTANCE_TYPES = [
    "t3.medium",
    "r8i.large",
    "r8i.xlarge",
    "r8i.2xlarge",
    "r8i.4xlarge",
    "r8i.8xlarge",
    "r8i.12xlarge",
    "r8i.16xlarge",
    "r8i.24xlarge",
    "r8i.32xlarge",
    "r8i.48xlarge",
    "r8i.96xlarge",
]
TEMPLATE_MAP = {
    "Private":     "templates/neo4j-private.template.yaml",
    "Public":      "templates/neo4j-public.template.yaml",
    "ExistingVpc": "templates/neo4j-private-existing-vpc.template.yaml",
}
LOCAL_LICENSE_DIR = SCRIPT_DIR / ".licenses"
LOCAL_LICENSE_FILES = {
    "bloom": LOCAL_LICENSE_DIR / "bloom.license",
    "gds": LOCAL_LICENSE_DIR / "gds.license",
}
_REFRESH_TERMINAL_FAIL = {"Failed", "Cancelled", "RollbackSuccessful", "RollbackFailed"}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Deploy Neo4j Enterprise Edition CloudFormation stack for local testing.",
    )
    p.add_argument(
        "instance_type", nargs="?", default="t3.medium",
        choices=INSTANCE_TYPES, metavar="INSTANCE_TYPE",
        help=(
            "Fully-qualified EC2 instance type (default: t3.medium). "
            "Allowed: t3.medium, r8i.{large,xlarge,2xlarge,4xlarge,"
            "8xlarge,12xlarge,16xlarge,24xlarge,32xlarge,48xlarge,"
            "96xlarge}. Must match the InstanceType AllowedValues "
            "in the template."
        ),
    )
    p.add_argument("--region", dest="region_override", metavar="REGION")
    p.add_argument("--number-of-servers", type=int, default=3, choices=[1, 3])
    p.add_argument("--marketplace", action="store_true")
    p.add_argument("--cert-arn", metavar="ACM_ARN", default="",
                   help="Existing ACM certificate ARN for the NLB TLS listeners. "
                        "If omitted for Private/ExistingVpc (and not Public), a "
                        "self-signed cert is auto-imported via scripts/certificate.py.")
    p.add_argument("--advertised-dns", metavar="DNS_NAME", default="",
                   help="DNS name placed in the cert SAN, the Neo4j advertised "
                        "addresses, and (when --create-private-dns) the Route 53 "
                        "record. Defaults to neo4j-<stack>.neo4j.local for the "
                        "self-signed test path.")
    p.add_argument("--enable-public-tls", action="store_true",
                   help="Public mode only: enable TLS (requires --cert-arn and "
                        "--advertised-dns). Without it, Public stays plain TCP.")
    p.add_argument("--create-private-dns", action="store_true", default=None,
                   help="Private/ExistingVpc: create a Route 53 private DNS "
                        "record mapping AdvertisedDNS to the NLB.")
    p.add_argument("--no-create-private-dns", dest="create_private_dns",
                   action="store_false",
                   help="Disable the Route 53 private DNS record (the default).")
    p.add_argument("--private-dns-zone", metavar="ZONE_NAME", default="",
                   help="Private hosted zone name to create when "
                        "--create-private-dns is set and no hosted zone ID is given.")
    p.add_argument("--private-dns-hosted-zone-id", metavar="ZONE_ID", default="",
                   help="Existing Route 53 private hosted zone ID to receive the "
                        "AdvertisedDNS alias record.")
    p.add_argument("--alert-email", metavar="EMAIL")
    p.add_argument("--mode", default="Private", choices=["Public", "Private", "ExistingVpc"])
    p.add_argument("--allowed-cidr", metavar="CIDR")
    p.add_argument("--vpc-id", metavar="VPC_ID")
    p.add_argument("--subnet-1", metavar="SUBNET_ID")
    p.add_argument("--subnet-2", metavar="SUBNET_ID", default="")
    p.add_argument("--subnet-3", metavar="SUBNET_ID", default="")
    p.add_argument("--private-route-table-1", metavar="RTB_ID", default="",
                   help="Route table ID for PrivateSubnet1Id (required for ExistingVpc).")
    p.add_argument("--create-vpc-endpoints", choices=["true", "false"])
    p.add_argument("--existing-endpoint-sg-id", metavar="SG_ID", default="")
    p.add_argument("--disk-size", type=int, metavar="GB", help="Data volume size in GB (default: 100, min: 100, max: 65536)")
    p.add_argument("--snapshot-id", metavar="SNAPSHOT_ID", help="Snapshot ID to restore Node 1 data volume from (must match --disk-size)")
    p.add_argument("--no-bloom", action="store_true",
                   help="Skip the Bloom plugin install (sets InstallBloom=false). Default: install Bloom.")
    p.add_argument("--no-gds", action="store_true",
                   help="Skip the GDS plugin install (sets InstallGDS=false). Default: install GDS.")
    p.add_argument(
        "--bloom-license-secret-id", metavar="SECRET",
        help="Secrets Manager secret name or ARN holding the Bloom licence. "
             "Resolved to an ARN and passed as BloomLicenseSecretArn; UserData "
             "fetches and installs the licence on first boot and after instance "
             "replacement. Required when Bloom is installed (default on); pass "
             "--no-bloom to skip, or place a licence at .licenses/bloom.license "
             "to auto-upload it.",
    )
    p.add_argument(
        "--gds-license-secret-id", metavar="SECRET",
        help="Secrets Manager secret name or ARN holding the GDS Enterprise licence. "
             "Resolved to an ARN and passed as GdsLicenseSecretArn; UserData "
             "fetches and installs the licence on first boot and after instance "
             "replacement. Required when GDS is installed (default on); pass "
             "--no-gds to skip, or place a licence at .licenses/gds.license "
             "to auto-upload it.",
    )
    p.add_argument(
        "--no-local-licenses", action="store_true",
        help="Do not auto-upload neo4j-ee/.licenses/*.license files when "
             "matching --*-license-secret-id arguments are omitted.",
    )
    p.add_argument(
        "--vpc-file", metavar="PATH",
        help="Path to vpc-*.txt from scripts/create-test-vpc.py. "
             "Auto-detected from .deploy/vpc-*.txt when --mode ExistingVpc and --vpc-id is not provided. "
             "Populates --vpc-id, --subnet-*, --allowed-cidr, --region, and --existing-endpoint-sg-id "
             "from the file; any of those flags override the file values when provided explicitly.",
    )
    return p.parse_args()


def _resolve_vpc_file(explicit: str | None, deploy_dir: Path) -> str | None:
    if explicit:
        return explicit
    vpc_files = sorted(
        deploy_dir.glob("vpc-*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return str(vpc_files[0]) if vpc_files else None


def _parse_vpc_file(path: str) -> dict[str, str]:
    return parse_key_value_text(Path(path).read_text())


def generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def verify_rendered_templates() -> None:
    build_script = SCRIPT_DIR / "templates" / "build.py"
    print("Verifying rendered CloudFormation templates...")
    try:
        subprocess.run([sys.executable, str(build_script), "--verify"], check=True)
    except subprocess.CalledProcessError:
        sys.exit(
            "ERROR: rendered templates are out of sync with templates/src/. "
            "Run neo4j-ee/templates/build.py and retry deploy."
        )


def detect_public_ip() -> str | None:
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as r:
            return r.read().decode().strip()
    except (urllib.error.URLError, OSError):
        return None


@dataclass(frozen=True)
class TlsPlan:
    """Resolved TLS/DNS decision for one deploy, derived without AWS calls.

    ``needs_self_signed_import`` is True when the Private/ExistingVpc test path
    must auto-import a self-signed ACM cert (no ``--cert-arn`` supplied). When
    that import happens the resulting ARN is recorded as
    ``AutoImportedCertificateArn`` so teardown deletes only deploy-created certs
    and never a user-supplied one (see worklog/tls.md D5/D6).
    """

    mode: str
    cert_arn: str
    advertised_dns: str
    enable_public_tls: bool
    create_private_dns: bool
    private_dns_zone: str
    private_dns_hosted_zone_id: str
    needs_self_signed_import: bool


@dataclass(frozen=True)
class ExistingVpcInputs:
    vpc_id: str
    subnet_1: str
    private_route_table_1: str
    subnet_2: str
    subnet_3: str
    create_vpc_endpoints: str
    existing_endpoint_sg_id: str


def resolve_tls_plan(
    *,
    mode: str,
    stack_name: str,
    cert_arn: str,
    advertised_dns: str,
    enable_public_tls: bool,
    create_private_dns: bool,
    private_dns_zone: str,
    private_dns_hosted_zone_id: str,
) -> TlsPlan:
    """Resolve the TLS plan and validate flag combinations (no AWS calls).

    Raises ``ValueError`` for any invalid combination so callers fail before
    uploading a template or creating a stack.
    """
    cert_arn = cert_arn or ""
    advertised_dns = advertised_dns or ""
    private_dns_zone = private_dns_zone or ""
    private_dns_hosted_zone_id = private_dns_hosted_zone_id or ""

    if mode == "Public":
        # Public never creates the Route 53 private DNS resources.
        if create_private_dns or private_dns_zone or private_dns_hosted_zone_id:
            raise ValueError(
                "Public mode does not create Route 53 private DNS. Drop "
                "--create-private-dns / --private-dns-zone / "
                "--private-dns-hosted-zone-id."
            )
        # Fail fast rather than silently shipping a plain-TCP public stack
        # when the operator clearly intended TLS (item 8).
        if (cert_arn or advertised_dns) and not enable_public_tls:
            raise ValueError(
                "Public mode ignores --cert-arn/--advertised-dns unless "
                "--enable-public-tls is set. Add --enable-public-tls, or "
                "drop the TLS inputs for a plain-TCP public stack."
            )
        if enable_public_tls and (not cert_arn or not advertised_dns):
            raise ValueError(
                "--enable-public-tls requires --cert-arn and --advertised-dns."
            )
        return TlsPlan(
            mode=mode,
            cert_arn=cert_arn,
            advertised_dns=advertised_dns,
            enable_public_tls=enable_public_tls,
            create_private_dns=False,
            private_dns_zone="",
            private_dns_hosted_zone_id="",
            needs_self_signed_import=False,
        )

    # Private / ExistingVpc: TLS is mandatory.
    if create_private_dns and not (
        private_dns_zone or private_dns_hosted_zone_id
    ):
        raise ValueError(
            "--create-private-dns requires --private-dns-zone or "
            "--private-dns-hosted-zone-id; the template cannot create a "
            "useful hosted zone with an empty name."
        )
    if not advertised_dns:
        advertised_dns = f"neo4j-{stack_name}.neo4j.local"
    return TlsPlan(
        mode=mode,
        cert_arn=cert_arn,
        advertised_dns=advertised_dns,
        enable_public_tls=False,
        create_private_dns=create_private_dns,
        private_dns_zone=private_dns_zone,
        private_dns_hosted_zone_id=private_dns_hosted_zone_id,
        needs_self_signed_import=not cert_arn,
    )


def build_cfn_parameters(
    *,
    password: str,
    number_of_servers: int,
    instance_type: str,
    allowed_cidr: str,
    install_bloom: str,
    install_gds: str,
    bloom_license_secret_arn: str,
    gds_license_secret_arn: str,
    ssm_param_path: str | None,
    alert_email: str | None,
    disk_size: int | None,
    snapshot_id: str | None,
    mode: str,
    existing_vpc: ExistingVpcInputs | None,
    tls: TlsPlan,
) -> list[dict[str, str]]:
    """Build the CloudFormation parameter list (pure; no AWS calls).

    ``tls.cert_arn`` must already be the final ARN: for the self-signed test
    path the caller imports the cert and passes the resulting ARN in.
    """
    params: list[dict[str, str]] = [
        {"ParameterKey": "Password", "ParameterValue": password},
        {"ParameterKey": "NumberOfServers", "ParameterValue": str(number_of_servers)},
        {"ParameterKey": "InstanceType", "ParameterValue": instance_type},
        {"ParameterKey": "AllowedCIDR", "ParameterValue": allowed_cidr},
        {"ParameterKey": "InstallGDS", "ParameterValue": install_gds},
        {"ParameterKey": "InstallBloom", "ParameterValue": install_bloom},
        {"ParameterKey": "BloomLicenseSecretArn", "ParameterValue": bloom_license_secret_arn},
        {"ParameterKey": "GdsLicenseSecretArn", "ParameterValue": gds_license_secret_arn},
    ]
    if ssm_param_path:
        params.append({"ParameterKey": "ImageId", "ParameterValue": ssm_param_path})
    if alert_email:
        params.append({"ParameterKey": "AlertEmail", "ParameterValue": alert_email})
    if disk_size is not None:
        params.append({"ParameterKey": "DataDiskSize", "ParameterValue": str(disk_size)})
    if snapshot_id:
        params.append({"ParameterKey": "Node1SnapshotId", "ParameterValue": snapshot_id})
    if mode == "ExistingVpc":
        if existing_vpc is None:
            raise ValueError("ExistingVpc mode requires existing_vpc inputs.")
        params += [
            {"ParameterKey": "VpcId", "ParameterValue": existing_vpc.vpc_id},
            {"ParameterKey": "PrivateSubnet1Id", "ParameterValue": existing_vpc.subnet_1},
            {"ParameterKey": "PrivateRouteTable1Id",
             "ParameterValue": existing_vpc.private_route_table_1},
            {"ParameterKey": "PrivateSubnet2Id", "ParameterValue": existing_vpc.subnet_2},
            {"ParameterKey": "PrivateSubnet3Id", "ParameterValue": existing_vpc.subnet_3},
            {"ParameterKey": "CreateVpcEndpoints",
             "ParameterValue": existing_vpc.create_vpc_endpoints},
        ]
        if existing_vpc.existing_endpoint_sg_id:
            params.append({"ParameterKey": "ExistingEndpointSgId",
                            "ParameterValue": existing_vpc.existing_endpoint_sg_id})

    if mode == "Public":
        if tls.enable_public_tls:
            params += [
                {"ParameterKey": "EnableTLS", "ParameterValue": "true"},
                {"ParameterKey": "CertificateArn", "ParameterValue": tls.cert_arn},
                {"ParameterKey": "AdvertisedDNS", "ParameterValue": tls.advertised_dns},
            ]
    else:
        params += [
            {"ParameterKey": "CertificateArn", "ParameterValue": tls.cert_arn},
            {"ParameterKey": "AdvertisedDNS", "ParameterValue": tls.advertised_dns},
            {"ParameterKey": "CreatePrivateDns",
             "ParameterValue": "true" if tls.create_private_dns else "false"},
            {"ParameterKey": "BoltTlsScheme",
             "ParameterValue": "neo4j+ssc" if tls.needs_self_signed_import else "neo4j+s"},
        ]
        if tls.private_dns_zone:
            params.append({"ParameterKey": "PrivateDnsZoneName",
                            "ParameterValue": tls.private_dns_zone})
        if tls.private_dns_hosted_zone_id:
            params.append({"ParameterKey": "PrivateDnsHostedZoneId",
                            "ParameterValue": tls.private_dns_hosted_zone_id})
    return params


def main() -> None:
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()
    verify_rendered_templates()

    if args.mode == "ExistingVpc":
        deploy_dir = SCRIPT_DIR / ".deploy"
        vpc_file_path = (
            _resolve_vpc_file(args.vpc_file, deploy_dir)
            if args.vpc_file or not args.vpc_id
            else None
        )
        if not vpc_file_path and not args.vpc_id:
            sys.exit(
                "ERROR: --mode ExistingVpc requires --vpc-id and --subnet-1, "
                "or a vpc-*.txt file from scripts/create-test-vpc.py in .deploy/"
            )
        if vpc_file_path:
            vpc_fields = _parse_vpc_file(vpc_file_path)
            print(f"VPC config from: {os.path.basename(vpc_file_path)}")
            if not args.vpc_id:
                args.vpc_id = vpc_fields.get("VpcId", "")
            if not args.subnet_1:
                args.subnet_1 = vpc_fields.get("Subnet1Id", "")
            if not args.subnet_2:
                args.subnet_2 = vpc_fields.get("Subnet2Id", "")
            if not args.subnet_3:
                args.subnet_3 = vpc_fields.get("Subnet3Id", "")
            if not args.allowed_cidr:
                args.allowed_cidr = vpc_fields.get("VpcCidr", "")
            if not args.region_override:
                args.region_override = vpc_fields.get("Region", "")
            if not args.existing_endpoint_sg_id and "EndpointSgId" in vpc_fields:
                args.existing_endpoint_sg_id = vpc_fields["EndpointSgId"]
            if not args.private_route_table_1:
                args.private_route_table_1 = vpc_fields.get("RouteTable1Id", "")
            # When the VPC file declares pre-existing endpoints, default to reusing them.
            if vpc_fields.get("WithEndpoints", "").lower() == "true" and args.create_vpc_endpoints is None:
                args.create_vpc_endpoints = "false"

    if args.mode == "ExistingVpc":
        if args.create_vpc_endpoints is None:
            args.create_vpc_endpoints = "true"
        if not args.vpc_id or not args.subnet_1:
            sys.exit("ERROR: --mode ExistingVpc requires --vpc-id and --subnet-1 (--subnet-2 and --subnet-3 required for 3-node).")
        if not args.private_route_table_1:
            sys.exit("ERROR: --mode ExistingVpc requires --private-route-table-1 or a vpc-*.txt file with RouteTable1Id.")
        if args.number_of_servers == 3 and not (args.subnet_2 and args.subnet_3):
            sys.exit("ERROR: --mode ExistingVpc with 3 servers requires --subnet-2 and --subnet-3.")
        if args.create_vpc_endpoints == "false" and not args.existing_endpoint_sg_id:
            sys.exit("ERROR: --existing-endpoint-sg-id is required when --create-vpc-endpoints false")

    instance_type = args.instance_type

    if args.allowed_cidr:
        allowed_cidr = args.allowed_cidr
    elif args.mode in ("Private", "ExistingVpc"):
        allowed_cidr = "10.0.0.0/16"
    else:
        ip = detect_public_ip()
        if not ip:
            sys.exit("ERROR: Could not detect public IP. Pass --allowed-cidr explicitly.")
        allowed_cidr = f"{ip}/32"

    region = args.region_override or random.choice(SUPPORTED_REGIONS)
    ts = int(time.time())
    stack_name = f"test-ee-{ts}"
    password = generate_password()
    install_bloom = "false" if args.no_bloom else "true"
    install_gds = "false" if args.no_gds else "true"

    cleanup_state = {
        "cfn_bucket": None,
        "copied_ami_id": None,
        "auto_imported_cert_arn": None,
        "license_secret_arns": [],
    }

    def cleanup():
        if cleanup_state["cfn_bucket"]:
            bucket = cleanup_state["cfn_bucket"]
            print(f"Cleaning up temporary S3 bucket {bucket}...")
            try:
                s3 = boto3.client("s3", region_name=region)
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket):
                    for obj in page.get("Contents", []):
                        s3.delete_object(Bucket=bucket, Key=obj["Key"])
                s3.delete_bucket(Bucket=bucket)
            except Exception as e:
                print(f"  Warning: could not fully clean up S3 bucket {bucket}: {e}")
        if cleanup_state["auto_imported_cert_arn"]:
            arn = cleanup_state["auto_imported_cert_arn"]
            print(f"\nCleaning up auto-imported ACM certificate {arn}...")
            try:
                boto3.client("acm", region_name=region).delete_certificate(
                    CertificateArn=arn
                )
            except Exception as e:
                print(f"  Warning: could not delete ACM certificate "
                      f"(it may still be in use by the stack): {e}")
        for arn in cleanup_state["license_secret_arns"]:
            print(f"\nCleaning up local licence secret {arn}...")
            try:
                boto3.client("secretsmanager", region_name=region).delete_secret(
                    SecretId=arn, ForceDeleteWithoutRecovery=True
                )
            except Exception as e:
                print(f"  Warning: could not delete licence secret: {e}")

    atexit.register(cleanup)

    ami_info = resolve_ami(
        args,
        region=region,
        stack_name=stack_name,
        script_dir=SCRIPT_DIR,
        source_region=SOURCE_REGION,
    )
    ami_id = ami_info.ami_id
    source_ami_id = ami_info.source_ami_id
    ami_source = ami_info.source
    ssm_param_path = ami_info.ssm_param_path
    cleanup_state["copied_ami_id"] = ami_info.copied_ami_id

    bloom_license_secret_arn, gds_license_secret_arn, created_license_secret_arns = (
        resolve_license_secret_arns(args, region, stack_name, LOCAL_LICENSE_FILES)
    )
    cleanup_state["license_secret_arns"] = created_license_secret_arns

    # Fail early: the template now requires matching license ARNs whenever a
    # plugin is installed (enforced by AWS::CloudFormation::Rules and mirrored
    # by UserData fail-fast guards). Surface the
    # missing licence here before we create the stack so the operator gets a
    # clean error instead of a CloudFormation rollback.
    if install_bloom == "true" and not bloom_license_secret_arn:
        sys.exit(
            "ERROR: InstallBloom=true requires a Bloom licence. Provide one of:\n"
            "  --bloom-license-secret-id <name|arn>  (existing Secrets Manager secret)\n"
            "  place a licence at .licenses/bloom.license  (auto-uploaded)\n"
            "  pass --no-bloom  (skip installing Bloom)"
        )
    if install_gds == "true" and not gds_license_secret_arn:
        sys.exit(
            "ERROR: InstallGDS=true requires a GDS licence. Provide one of:\n"
            "  --gds-license-secret-id <name|arn>  (existing Secrets Manager secret)\n"
            "  place a licence at .licenses/gds.license  (auto-uploaded)\n"
            "  pass --no-gds  (skip installing GDS)"
        )

    # --- TLS resolution -----------------------------------------------------
    # Single-phase: TLS parameters go into the initial create_stack call (no
    # two-phase update). Private/ExistingVpc require TLS; Public is opt-in via
    # --enable-public-tls. resolve_tls_plan validates the flag combination with
    # no AWS calls (see worklog/tls.md, D5/D6).
    try:
        tls = resolve_tls_plan(
            mode=args.mode,
            stack_name=stack_name,
            cert_arn=args.cert_arn,
            advertised_dns=args.advertised_dns,
            enable_public_tls=args.enable_public_tls,
            create_private_dns=bool(args.create_private_dns),
            private_dns_zone=args.private_dns_zone,
            private_dns_hosted_zone_id=args.private_dns_hosted_zone_id,
        )
    except ValueError as e:
        sys.exit(f"ERROR: {e}")

    # When the operator supplied --cert-arn, verify the cert covers
    # AdvertisedDNS before stack creation. The published Neo4jBoltUrl uses
    # AdvertisedDNS as the host; a mismatched cert breaks every client.
    if tls.cert_arn and tls.advertised_dns:
        try:
            certificate._verify_cert_matches_dns(
                boto3.client("acm", region_name=region),
                tls.cert_arn,
                tls.advertised_dns,
            )
        except ValueError as e:
            sys.exit(f"ERROR: {e}")
        except ClientError as e:
            sys.exit(
                f"ERROR: ACM describe_certificate failed for {tls.cert_arn} "
                f"in region {region}: {e}"
            )

    # For the default test path (Private/ExistingVpc, no --cert-arn) import a
    # self-signed cert into ACM with SAN=AdvertisedDNS so tooling connects with
    # neo4j+ssc://. The resulting ARN is the only cert teardown may delete.
    if tls.needs_self_signed_import:
        print(
            f"No --cert-arn given; importing a self-signed ACM cert with "
            f"SAN={tls.advertised_dns} (test path; clients use neo4j+ssc://)..."
        )
        acm = boto3.client("acm", region_name=region)
        imported_arn = certificate._import_self_signed(acm, tls.advertised_dns)
        cleanup_state["auto_imported_cert_arn"] = imported_arn
        tls = replace(tls, cert_arn=imported_arn)
        print(f"  Imported CertificateArn: {imported_arn}")

    cert_arn = tls.cert_arn
    advertised_dns = tls.advertised_dns
    enable_public_tls = tls.enable_public_tls
    create_private_dns = tls.create_private_dns

    print()
    print("=============================================")
    print("  Neo4j EE Deployment")
    print("=============================================")
    print(f"  Stack:          {stack_name}")
    print(f"  Region:         {region}")
    print(f"  Instance:       {instance_type}")
    print(f"  Servers:        {args.number_of_servers}")
    print(f"  Mode:           {args.mode}")
    print(f"  AllowedCIDR:    {allowed_cidr}")
    if args.mode == "Public":
        print(f"  TLS:            {'enabled' if enable_public_tls else 'disabled (plain TCP)'}")
    else:
        print(f"  TLS:            mandatory")
    if cert_arn:
        print(f"  CertificateArn: {cert_arn}")
    if advertised_dns:
        print(f"  AdvertisedDNS:  {advertised_dns}")
    if args.mode != "Public":
        print(f"  PrivateDNS:     {'create' if create_private_dns else 'none'}")
    print(f"  AMI source:     {ami_source}")
    print(f"  Install Bloom:  {install_bloom}")
    print(f"  Install GDS:    {install_gds}")
    print(f"  Bloom licence:  {'yes' if bloom_license_secret_arn else 'no'}")
    print(f"  GDS licence:    {'yes' if gds_license_secret_arn else 'no'}")
    if not args.marketplace:
        print(f"  AMI:            {ami_id}")
        if cleanup_state["copied_ami_id"]:
            print(f"  AMI original:   {source_ami_id} (copied from {SOURCE_REGION})")
    if args.alert_email:
        print(f"  Alert email:    {args.alert_email}")
    print("=============================================")
    print()

    template_file = TEMPLATE_MAP[args.mode]
    def _register_bucket(name: str) -> None:
        cleanup_state["cfn_bucket"] = name

    bucket_name, template_url = upload_template_to_s3(
        script_dir=SCRIPT_DIR,
        template_file=template_file,
        region=region,
        timestamp=ts,
        on_bucket_created=_register_bucket,
    )

    existing_vpc_inputs = (
        ExistingVpcInputs(
            vpc_id=args.vpc_id,
            subnet_1=args.subnet_1,
            private_route_table_1=args.private_route_table_1,
            subnet_2=args.subnet_2,
            subnet_3=args.subnet_3,
            create_vpc_endpoints=args.create_vpc_endpoints,
            existing_endpoint_sg_id=args.existing_endpoint_sg_id,
        )
        if args.mode == "ExistingVpc"
        else None
    )
    cfn_params = build_cfn_parameters(
        password=password,
        number_of_servers=args.number_of_servers,
        instance_type=instance_type,
        allowed_cidr=allowed_cidr,
        install_bloom=install_bloom,
        install_gds=install_gds,
        bloom_license_secret_arn=bloom_license_secret_arn,
        gds_license_secret_arn=gds_license_secret_arn,
        ssm_param_path=ssm_param_path,
        alert_email=args.alert_email,
        disk_size=args.disk_size,
        snapshot_id=args.snapshot_id,
        mode=args.mode,
        existing_vpc=existing_vpc_inputs,
        tls=tls,
    )

    cfn = boto3.client("cloudformation", region_name=region)
    create_stack_and_wait(cfn, stack_name, template_url, cfn_params)

    # TLS is configured in the single create_stack call above (no two-phase
    # update / instance refresh). An auto-imported self-signed cert is left in
    # ACM for the stack's lifetime; teardown removes it (keyed off the recorded
    # AutoImportedCertificateArn), and cleanup() deletes it on a failed deploy.
    # Clear the failure-cleanup marker now that the stack created successfully.
    auto_imported_cert_arn = cleanup_state["auto_imported_cert_arn"]
    cleanup_state["auto_imported_cert_arn"] = None

    deploy_dir = SCRIPT_DIR / ".deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    outputs_file = deploy_dir / f"{stack_name}.txt"

    print(f"Stack created. Writing outputs to {outputs_file}...")
    stack_data = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]

    lines = [
        f"{o['OutputKey']:<20} = {o['OutputValue']}"
        for o in stack_data.get("Outputs", [])
    ]
    extra = [
        ("StackName", stack_name),
        ("Region", region),
        ("Password", password),
        ("NumberOfServers", str(args.number_of_servers)),
        ("InstanceType", instance_type),
        ("Edition", "ee"),
        ("DeploymentMode", args.mode),
        ("AmiSource", ami_source),
        ("InstallAPOC", "yes"),
        ("InstallGDS", install_gds),
        ("InstallBloom", install_bloom),
        # The test runner uses this to gate check_bloom_plugin_loaded and the
        # G3 conf-key audit's Bloom-only expectations. Derived from the
        # InstallBloom CFN parameter so a deploy with --no-bloom does not
        # incorrectly assert Bloom is present.
        ("BloomExpected", "yes" if install_bloom == "true" else "no"),
    ]
    if args.alert_email:
        extra.append(("AlertEmail", args.alert_email))
    if args.mode == "ExistingVpc":
        extra.append(("CreateVpcEndpoints", args.create_vpc_endpoints))
        if args.existing_endpoint_sg_id:
            extra.append(("ExistingEndpointSgId", args.existing_endpoint_sg_id))
    if ssm_param_path:
        extra.extend([("SSMParamPath", ssm_param_path), ("AmiId", ami_id)])
    if cleanup_state["copied_ami_id"]:
        extra.extend([("CopiedAmiId", cleanup_state["copied_ami_id"]), ("SourceRegion", SOURCE_REGION)])
    # Recorded so the validate-private tooling resolves the +ssc Bolt scheme
    # and the tunnels print the right AdvertisedDNS (see worklog/tls.md D5/D6).
    if cert_arn:
        extra.append(("CertificateArn", cert_arn))
    if advertised_dns:
        extra.append(("AdvertisedDNS", advertised_dns))
    # Only set when deploy.py imported the self-signed cert itself. teardown.sh
    # deletes exactly this ACM cert after stack deletion and never a
    # user-supplied --cert-arn (see worklog/tls.md D5/D6).
    if auto_imported_cert_arn:
        extra.append(("AutoImportedCertificateArn", auto_imported_cert_arn))
    # Recorded so test_neo4j can detect a licensed deploy and run the Bloom/GDS
    # Enterprise-mode assertions; absent fields mean the plugin was not installed.
    if bloom_license_secret_arn:
        extra.append(("BloomLicenseSecretArn", bloom_license_secret_arn))
    if gds_license_secret_arn:
        extra.append(("GdsLicenseSecretArn", gds_license_secret_arn))
    if created_license_secret_arns:
        extra.append(("AutoCreatedLicenseSecretArns", ",".join(created_license_secret_arns)))
    extra.append(("StackID", stack_data["StackId"]))

    lines += [f"{k:<20} = {v}" for k, v in extra]
    output = "\n".join(lines) + "\n"
    print(output, end="")
    outputs_file.write_text(output)
    cleanup_state["license_secret_arns"] = []

    print()
    print(f"Outputs saved to {outputs_file}")
    print()
    print(f"To test:      cd ../test_neo4j && uv run test-neo4j --edition ee --stack {stack_name}")
    print(f"To tear down: ./teardown.sh {stack_name}")


if __name__ == "__main__":
    main()
