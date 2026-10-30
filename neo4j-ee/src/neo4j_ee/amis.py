"""AMI resolution helpers for EE deploy tooling."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sys

import boto3
from botocore.exceptions import ClientError


# The bootstrap expects the AMI to carry the neo4j user, the Neo4j yum repo,
# python3.11, and the CloudWatch agent. create-ami.sh began baking these on this
# date. Marketplace AMIs are opaque (no tags, uuid names), so creation date is
# the only signal. Bump this if create-ami.sh changes what the AMI must carry.
MIN_BOOTSTRAP_AMI_DATE = date(2026, 5, 15)


@dataclass(frozen=True)
class AmiInfo:
    ami_id: str
    source_ami_id: str
    source: str
    ssm_param_path: str
    copied_ami_id: str | None = None
    marketplace_product_code: str = ""
    marketplace_ami_owner: str = ""
    marketplace_ami_name: str = ""


def resolve_marketplace_ami(region: str, product_code: str) -> dict:
    ec2 = boto3.client("ec2", region_name=region)
    images = ec2.describe_images(
        Owners=["aws-marketplace"],
        Filters=[
            {"Name": "product-code", "Values": [product_code]},
            {"Name": "state", "Values": ["available"]},
        ],
    )["Images"]
    if not images:
        sys.exit(
            "ERROR: Could not find an available AWS Marketplace AMI for "
            f"product code {product_code!r} in {region}. Confirm the account "
            "is subscribed to the Marketplace listing and that the product is "
            "available in the selected region."
        )
    return max(images, key=lambda img: img["CreationDate"])


def check_marketplace_ami_contents(image: dict, region: str) -> None:
    """Exit if the listing AMI predates the AMI contents the bootstrap needs."""
    created = date.fromisoformat(image["CreationDate"][:10])
    if created >= MIN_BOOTSTRAP_AMI_DATE:
        return
    sys.exit(
        f"ERROR: Marketplace AMI {image['ImageId']} in {region} was created on "
        f"{created}, before {MIN_BOOTSTRAP_AMI_DATE}.\n"
        "       It lacks the neo4j user, the Neo4j yum repo, python3.11, and the "
        "CloudWatch agent that the bootstrap needs, so every node would fail.\n"
        "       Publish an AMI built by the current marketplace/create-ami.sh to "
        "the listing, or pass --allow-old-marketplace-ami to deploy anyway.\n"
        "       See docs/LOCAL-TESTING.md#choose-an-ami."
    )


def _put_ami_ssm_param(region: str, stack_name: str, ami_id: str) -> str:
    ssm_param_path = f"/neo4j-ee/test/{stack_name}/ami-id"
    print(f"Creating SSM parameter {ssm_param_path} -> {ami_id}...")
    boto3.client("ssm", region_name=region).put_parameter(
        Name=ssm_param_path,
        Type="String",
        Value=ami_id,
        Overwrite=True,
    )
    return ssm_param_path


def _wait_for_ami(ec2, ami_id: str, region: str) -> None:
    ec2.get_waiter("image_available").wait(
        ImageIds=[ami_id],
        WaiterConfig={"Delay": 30, "MaxAttempts": 60},
    )
    print(f"AMI available in {region}.")


def _tag_copied_ami(
    ec2, ami_id: str, source_ami_id: str, source_region: str
) -> None:
    try:
        ec2.create_tags(
            Resources=[ami_id],
            Tags=[
                {"Key": "SourceAmiId", "Value": source_ami_id},
                {"Key": "SourceRegion", "Value": source_region},
            ],
        )
    except ClientError as exc:
        print(f"Warning: could not tag copied AMI {ami_id}: {exc}")


def resolve_ami(
    args: argparse.Namespace,
    *,
    region: str,
    stack_name: str,
    script_dir: Path,
    source_region: str,
) -> AmiInfo:
    """Resolve the AMI used by the stack and create an SSM image parameter."""
    if args.marketplace:
        product_code = args.marketplace_product_code
        image = resolve_marketplace_ami(region, product_code)
        if not args.allow_old_marketplace_ami:
            check_marketplace_ami_contents(image, region)
        ami_id = image["ImageId"]
        print(
            "Resolved Marketplace AMI "
            f"{ami_id} in {region} from product code {product_code}."
        )
        return AmiInfo(
            ami_id=ami_id,
            source_ami_id="",
            source="marketplace",
            ssm_param_path=_put_ami_ssm_param(region, stack_name, ami_id),
            marketplace_product_code=product_code,
            marketplace_ami_owner=image.get("OwnerId", ""),
            marketplace_ami_name=image.get("Name", ""),
        )

    ami_id_file = script_dir / "marketplace" / "ami-id.txt"
    if not ami_id_file.exists():
        sys.exit(
            f"ERROR: {ami_id_file} not found. Run marketplace/create-ami.sh first,\n"
            "       or use --marketplace to deploy from the live Marketplace listing."
        )
    source_ami_id = ami_id_file.read_text().strip()
    ec2 = boto3.client("ec2", region_name=region)
    copied_ami_id: str | None = None

    if region != source_region:
        existing = ec2.describe_images(
            Owners=["self"],
            Filters=[
                {
                    "Name": "description",
                    "Values": [f"Copied from {source_ami_id} in {source_region}"],
                }
            ],
        )["Images"]
        available = sorted(
            [img for img in existing if img["State"] == "available"],
            key=lambda img: img["CreationDate"],
            reverse=True,
        )
        pending = [img for img in existing if img["State"] == "pending"]
        if available:
            copied_ami_id = available[0]["ImageId"]
            print(f"Reusing existing copied AMI {copied_ami_id} in {region}.")
        elif pending:
            copied_ami_id = pending[0]["ImageId"]
            print(
                f"Found in-progress AMI copy {copied_ami_id} in {region} - "
                "waiting for it to become available..."
            )
            _wait_for_ami(ec2, copied_ami_id, region)
            _tag_copied_ami(ec2, copied_ami_id, source_ami_id, source_region)
        else:
            print(f"Copying AMI {source_ami_id} from {source_region} to {region}...")
            resp = ec2.copy_image(
                SourceRegion=source_region,
                SourceImageId=source_ami_id,
                Name=f"neo4j-ee-copy-{source_ami_id}",
                Description=f"Copied from {source_ami_id} in {source_region}",
            )
            copied_ami_id = resp["ImageId"]
            print(f"Copied AMI: {copied_ami_id} - waiting for it to become available...")
            _wait_for_ami(ec2, copied_ami_id, region)
            _tag_copied_ami(ec2, copied_ami_id, source_ami_id, source_region)
        ami_id = copied_ami_id
    else:
        ami_id = source_ami_id

    return AmiInfo(
        ami_id=ami_id,
        source_ami_id=source_ami_id,
        source="local",
        ssm_param_path=_put_ami_ssm_param(region, stack_name, ami_id),
        copied_ami_id=copied_ami_id,
    )
