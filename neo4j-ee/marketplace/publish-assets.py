#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.43.117"]
# ///
"""Publish the EE Marketplace listing assets for one version.

Works in the seller account and us-east-1:
  1. Checks that the AMI ingestion role exists and carries its managed policy.
  2. Creates a public S3 bucket for the listing assets, if it is missing.
  3. Uploads the three templates and their architecture diagrams under a
     version prefix, then reads each URL back without credentials.

It prints the values the Add version page asks for, one block per delivery
option. Each step is safe to run again.

Run from neo4j-ee/:
  ./marketplace/publish-assets.py --profile marketplace --version 2026.10.1
"""

import argparse
import json
import os
import struct
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
SELLER_ACCOUNT_ID = "385155106615"
EE_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = EE_ROOT / "templates"
DIAGRAMS_DIR = EE_ROOT / "marketplace"
ROLE_NAME = "aws_marketplace_ami_ingestion"
INGESTION_POLICY_ARN = "arn:aws:iam::aws:policy/AWSMarketplaceAmiIngestion"
SSM_ALIAS_PREFIX = "/aws/service/marketplace/prod-vwigx4rbvvb5s/"
DIAGRAM_SIZE = (1100, 700)


@dataclass(frozen=True)
class DeliveryOption:
    title: str
    template: Path
    diagram: Path


DELIVERY_OPTIONS = [
    DeliveryOption(
        "Public",
        TEMPLATES_DIR / "neo4j-public.template.yaml",
        DIAGRAMS_DIR / "neo4j-public-architecture.png",
    ),
    DeliveryOption(
        "Private",
        TEMPLATES_DIR / "neo4j-private.template.yaml",
        DIAGRAMS_DIR / "neo4j-private-architecture.png",
    ),
    DeliveryOption(
        "Private Existing VPC",
        TEMPLATES_DIR / "neo4j-private-existing-vpc.template.yaml",
        DIAGRAMS_DIR / "neo4j-private-existing-vpc-architecture.png",
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    parser.add_argument(
        "--version",
        required=True,
        help="Listing version title, for example 2026.10.1. Also the key prefix.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation.")
    return parser.parse_args()


def png_size(path: Path) -> tuple[int, int]:
    """Read the width and height from a PNG file's IHDR chunk."""
    with path.open("rb") as handle:
        header = handle.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit(f"{path} is not a PNG file.")
    return struct.unpack(">II", header[16:24])


def check_local_files(version: str) -> None:
    """Stop unless every template and diagram is ready to submit."""
    for option in DELIVERY_OPTIONS:
        for path in (option.template, option.diagram):
            if not path.is_file():
                sys.exit(f"Missing {path}.")
        size = png_size(option.diagram)
        if size != DIAGRAM_SIZE:
            sys.exit(f"{option.diagram.name} is {size[0]}x{size[1]}, not 1100x700.")
        # The ImageId default names the SSM alias AWS Marketplace creates from
        # the version title. A mismatch breaks any launch that keeps the default.
        alias = f"Default: {SSM_ALIAS_PREFIX}{version}\n"
        if alias not in option.template.read_text():
            sys.exit(
                f"{option.template.name} does not default ImageId to "
                f"{SSM_ALIAS_PREFIX}{version}. Fix templates/src/parameters-common.yaml"
                " and rebuild."
            )
    # The committed templates must match a fresh build of the partials.
    result = subprocess.run(
        [sys.executable, str(TEMPLATES_DIR / "build.py"), "--verify"],
        cwd=TEMPLATES_DIR,
        check=False,
    )
    if result.returncode != 0:
        sys.exit("templates/build.py --verify failed. Rebuild the templates first.")


def ingestion_role_arn(iam) -> str:
    """Return the ingestion role ARN after checking its managed policy."""
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchEntity":
            sys.exit(f"Role {ROLE_NAME} does not exist in this account.")
        raise
    attached = iam.list_attached_role_policies(RoleName=ROLE_NAME)["AttachedPolicies"]
    if not any(p["PolicyArn"] == INGESTION_POLICY_ARN for p in attached):
        sys.exit(f"Role {ROLE_NAME} does not carry {INGESTION_POLICY_ARN}.")
    return role["Arn"]


def ensure_public_bucket(s3, bucket: str) -> None:
    """Create the bucket if missing and allow public reads of its objects."""
    try:
        s3.create_bucket(Bucket=bucket)
        print(f"Created bucket {bucket}.")
    except ClientError as error:
        if error.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
        print(f"Bucket {bucket} already exists.")
    # ACLs stay blocked. Only the bucket policy below grants public reads.
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadForMarketplace",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/*",
            }
        ],
    }
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(policy))


def upload(s3, bucket: str, key: str, path: Path, content_type: str) -> str:
    """Upload one file, check that it reads back publicly, return its URL."""
    s3.upload_file(str(path), bucket, key, ExtraArgs={"ContentType": content_type})
    url = f"https://{bucket}.s3.{REGION}.amazonaws.com/{key}"
    try:
        with urllib.request.urlopen(url) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    if status != 200:
        sys.exit(f"{url} returned {status}, not 200.")
    return url


def main() -> None:
    args = parse_args()
    if not args.profile:
        sys.exit("Set AWS_PROFILE or pass --profile. This script never guesses.")
    check_local_files(args.version)

    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    identity = session.client("sts").get_caller_identity()
    account = identity["Account"]
    if account != SELLER_ACCOUNT_ID:
        sys.exit(f"Account {account} is not the seller account {SELLER_ACCOUNT_ID}.")
    bucket = f"neo4j-ee-marketplace-assets-{account}"

    print(f"Account:  {account}")
    print(f"Identity: {identity['Arn']}")
    print(f"Bucket:   {bucket} (created if missing, objects publicly readable)")
    print(f"Uploads:  3 templates and 3 diagrams under {args.version}/")
    if not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted. Nothing was changed.")

    role_arn = ingestion_role_arn(session.client("iam"))
    s3 = session.client("s3")
    ensure_public_bucket(s3, bucket)

    print()
    print("Version values:")
    print(f"  Version title:                {args.version}")
    print(f"  IAM access role ARN:          {role_arn}")
    for option in DELIVERY_OPTIONS:
        template_url = upload(
            s3,
            bucket,
            f"{args.version}/{option.template.name}",
            option.template,
            "text/yaml",
        )
        diagram_url = upload(
            s3,
            bucket,
            f"{args.version}/{option.diagram.name}",
            option.diagram,
            "image/png",
        )
        print()
        print(f"Delivery option: {option.title}")
        print("  AMI parameter name:           ImageId")
        print(f"  CloudFormation template link: {template_url}")
        print(f"  Architecture diagram link:    {diagram_url}")


if __name__ == "__main__":
    main()
