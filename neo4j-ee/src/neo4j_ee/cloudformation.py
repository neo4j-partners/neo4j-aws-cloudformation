"""CloudFormation output helpers for EE tooling."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
import urllib.parse

import boto3


def describe_stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    """Return a CloudFormation stack's outputs as a dictionary."""
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {
        output["OutputKey"]: output["OutputValue"]
        for output in stack.get("Outputs", [])
    }


def nlb_dns_from_outputs(cfn, stack_name: str) -> str:
    """Resolve the NLB DNS name from known stack output shapes."""
    outputs = describe_stack_outputs(cfn, stack_name)
    if outputs.get("Neo4jInternalDNS"):
        return outputs["Neo4jInternalDNS"]

    for key in ("Neo4jBoltUrl", "Neo4jHttpsUrl"):
        value = outputs.get(key, "")
        if not value:
            continue
        hostname = urllib.parse.urlparse(value).hostname
        if hostname:
            return hostname

    sys.exit(
        "ERROR: Could not resolve the NLB DNS name from stack outputs. "
        "Expected Neo4jInternalDNS, Neo4jBoltUrl, or Neo4jHttpsUrl."
    )


def upload_template_to_s3(
    *,
    script_dir: Path,
    template_file: str,
    region: str,
    timestamp: int,
    on_bucket_created: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """Upload a generated template to a temporary S3 bucket.

    on_bucket_created: optional callback invoked with the bucket name as soon as
    create_bucket succeeds, so the caller can register cleanup before any
    follow-on call (upload_file, etc.) can fail and leak the bucket.
    """
    account_id = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    bucket_name = f"neo4j-ee-cfn-{account_id}-{region}-{timestamp}"
    print(f"Uploading template to s3://{bucket_name}...")
    s3 = boto3.client("s3", region_name=region)
    if region == "us-east-1":
        s3.create_bucket(Bucket=bucket_name)
    else:
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": region},
        )
    if on_bucket_created is not None:
        on_bucket_created(bucket_name)
    template_path = script_dir / template_file
    template_key = template_path.name
    s3.upload_file(str(template_path), bucket_name, template_key)
    template_url = f"https://{bucket_name}.s3.{region}.amazonaws.com/{template_key}"
    return bucket_name, template_url


def create_stack_and_wait(
    cfn,
    stack_name: str,
    template_url: str,
    cfn_params: list[dict[str, str]],
) -> None:
    """Create a stack and wait for CREATE_COMPLETE."""
    print(f"Creating stack {stack_name}...")
    cfn.create_stack(
        StackName=stack_name,
        TemplateURL=template_url,
        Capabilities=["CAPABILITY_IAM"],
        DisableRollback=True,
        Parameters=cfn_params,
    )
    print("Waiting for stack to complete (this takes a few minutes)...")
    cfn.get_waiter("stack_create_complete").wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 30, "MaxAttempts": 60},
    )
