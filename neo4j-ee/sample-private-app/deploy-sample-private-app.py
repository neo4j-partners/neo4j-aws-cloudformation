#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Deploy the Neo4j sample private Lambda app with plain CloudFormation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import boto3
from botocore.exceptions import ClientError


SCRIPT_DIR = Path(__file__).resolve().parent
EE_DIR = SCRIPT_DIR.parent
DEPLOY_DIR = EE_DIR / ".deploy"
TEMPLATE_FILE = SCRIPT_DIR / "sample-private-app.template.yaml"
LAMBDA_DIR = SCRIPT_DIR / "lambda"

sys.path.insert(0, str(EE_DIR / "src"))
from neo4j_ee.outputs import (  # noqa: E402
    read_outputs,
    require_field,
    resolve_bolt_scheme,
    resolve_outputs_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deploy the sample private Lambda app against an existing neo4j-ee "
            "Private or ExistingVpc stack."
        )
    )
    parser.add_argument(
        "stack_name",
        nargs="?",
        help="EE stack name. Defaults to the most recent .deploy/*.txt file.",
    )
    parser.add_argument(
        "--suffix",
        default="",
        help="Suffix appended to the sample app stack name.",
    )
    parser.add_argument(
        "--enable-resilience",
        action="store_true",
        help="Deploy the optional stop/start resilience Lambda.",
    )
    return parser.parse_args()


def require_ssm(ssm, name: str) -> str:
    try:
        return ssm.get_parameter(Name=name)["Parameter"]["Value"]
    except ClientError as exc:
        raise SystemExit(
            f"ERROR: SSM parameter {name} not found.\n"
            "Is the EE stack fully deployed and in Private mode?"
        ) from exc


def stack_status(cfn, stack_name: str) -> str | None:
    try:
        return cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def describe_stack_id(cfn, stack_name: str) -> str:
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    stack_id = stacks[0].get("StackId", "")
    if not stack_id:
        raise SystemExit(f"ERROR: Could not resolve stack ID for {stack_name}.")
    return stack_id


def print_header(
    ee_stack: str,
    app_stack: str,
    region: str,
    ssm_prefix: str,
    enable_resilience: bool,
    bolt_scheme: str,
    bolt_tls_enabled: bool,
) -> None:
    print("=== Neo4j Sample Private App Deploy ===")
    print()
    print(f"  EE Stack:       {ee_stack}")
    print(f"  App Stack:      {app_stack}")
    print(f"  Region:         {region}")
    print(f"  SSM Prefix:     {ssm_prefix}")
    print(f"  Resilience:     {str(enable_resilience).lower()}")
    print(f"  Bolt Scheme:    {bolt_scheme}")
    print(f"  Bolt TLS:       {str(bolt_tls_enabled).lower()}")
    print()


def clean_lambda_dir() -> None:
    keep = {"handler.py", "requirements.txt"}
    for child in LAMBDA_DIR.iterdir():
        if child.name in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def install_lambda_dependencies() -> None:
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "-q",
            "-r",
            str(LAMBDA_DIR / "requirements.txt"),
            "-t",
            str(LAMBDA_DIR),
            "--python-version",
            "3.13",
        ],
        check=True,
    )


def package_lambda() -> Path:
    print("Packaging Lambda...")
    clean_lambda_dir()
    install_lambda_dependencies()

    zip_path = SCRIPT_DIR / "lambda.zip"
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(LAMBDA_DIR.rglob("*")):
            if path.name == ".lock":
                continue
            if path.is_file():
                archive.write(path, path.relative_to(LAMBDA_DIR))

    (LAMBDA_DIR / ".lock").unlink(missing_ok=True)

    print(f"  zip: {zip_path} ({zip_path.stat().st_size} bytes)")
    return zip_path


def ensure_deploy_bucket(s3, account_id: str, region: str) -> str:
    bucket = f"neo4j-sample-private-app-deploy-{account_id}-{region}"
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"Deploy bucket {bucket} already exists.")
        return bucket
    except ClientError:
        pass

    print(f"Creating deploy bucket {bucket}...")
    kwargs = {"Bucket": bucket}
    if region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    s3.create_bucket(**kwargs)
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    return bucket


def upload_lambda_zip(s3, bucket: str, key: str, zip_path: Path) -> str:
    print(f"Uploading Lambda zip to s3://{bucket}/{key}...")
    with zip_path.open("rb") as body:
        response = s3.put_object(Bucket=bucket, Key=key, Body=body)
    version_id = response.get("VersionId", "")
    print(f"  version: {version_id}")
    zip_path.unlink()
    return version_id


def cfn_parameters(values: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"ParameterKey": key, "ParameterValue": value}
        for key, value in values.items()
    ]


def deploy_stack(
    cfn,
    app_stack_name: str,
    template_body: str,
    parameters: dict[str, str],
    neo4j_stack: str,
) -> None:
    common = {
        "StackName": app_stack_name,
        "TemplateBody": template_body,
        "Capabilities": ["CAPABILITY_IAM"],
        "Parameters": cfn_parameters(parameters),
        "Tags": [
            {"Key": "Project", "Value": "neo4j-sample-private-app"},
            {"Key": "Neo4jStack", "Value": neo4j_stack},
        ],
    }
    status = stack_status(cfn, app_stack_name)
    if status == "DELETE_IN_PROGRESS":
        print(f"Stack {app_stack_name} is DELETE_IN_PROGRESS; waiting for deletion...")
        cfn.get_waiter("stack_delete_complete").wait(
            StackName=app_stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 120},
        )
        status = None
    elif status == "CREATE_IN_PROGRESS":
        print(f"Stack {app_stack_name} is CREATE_IN_PROGRESS; waiting for creation...")
        cfn.get_waiter("stack_create_complete").wait(
            StackName=app_stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 120},
        )
        status = stack_status(cfn, app_stack_name)
    elif status in {
        "UPDATE_IN_PROGRESS",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
    }:
        print(f"Stack {app_stack_name} is {status}; waiting for update...")
        cfn.get_waiter("stack_update_complete").wait(
            StackName=app_stack_name,
            WaiterConfig={"Delay": 10, "MaxAttempts": 120},
        )
        status = stack_status(cfn, app_stack_name)

    exists = status is not None and status != "DELETE_COMPLETE"
    print(f"Deploying CloudFormation stack {app_stack_name}...")
    try:
        if exists:
            cfn.update_stack(**common)
            waiter = cfn.get_waiter("stack_update_complete")
        else:
            cfn.create_stack(**common)
            waiter = cfn.get_waiter("stack_create_complete")
    except ClientError as exc:
        if exists and "No updates are to be performed" in str(exc):
            print("No CloudFormation updates to perform.")
            return
        raise
    waiter.wait(
        StackName=app_stack_name,
        WaiterConfig={"Delay": 10, "MaxAttempts": 120},
    )


def stack_outputs(cfn, app_stack_name: str) -> dict[str, str]:
    stack = cfn.describe_stacks(StackName=app_stack_name)["Stacks"][0]
    return {
        output["OutputKey"]: output["OutputValue"]
        for output in stack.get("Outputs", [])
    }


def write_json(path: Path, payload: dict[str, str]) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {path}")


def write_invoke_script(script_path: Path) -> None:
    script_path.write_text(
        """#!/bin/bash
# invoke.sh - Call the Neo4j sample private Lambda via its IAM-authenticated Function URL.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_FILE=$(ls -t "${SCRIPT_DIR}/../.deploy"/sample-private-app-*.json 2>/dev/null | head -1 || true)
if [ -z "${APP_FILE}" ]; then
  echo "ERROR: No sample-private-app deployment found. Run uv run deploy-sample-private-app.py first." >&2
  exit 1
fi

FUNCTION_URL=$(python3 -c "import json; d=json.load(open('${APP_FILE}')); print(d['function_url'])")
REGION=$(python3 -c "import json; d=json.load(open('${APP_FILE}')); print(d['region'])")

eval "$(aws configure export-credentials --format env 2>/dev/null)"

BODY_FILE=$(mktemp)
STATUS=$(curl --silent --show-error --output "${BODY_FILE}" --write-out "%{http_code}" \\
  --aws-sigv4 "aws:amz:${REGION}:lambda" \\
  --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \\
  -H "x-amz-security-token: ${AWS_SESSION_TOKEN:-}" \\
  -H "Content-Type: application/json" \\
  "${FUNCTION_URL}")

if [ "${STATUS}" -lt 200 ] || [ "${STATUS}" -ge 300 ]; then
  echo "ERROR: Function URL returned HTTP ${STATUS}" >&2
  cat "${BODY_FILE}" >&2
  echo >&2
  rm -f "${BODY_FILE}"
  exit 1
fi

python3 -m json.tool <"${BODY_FILE}"
rm -f "${BODY_FILE}"
"""
    )
    script_path.chmod(0o755)
    print(f"Wrote {script_path}")


def write_validate_script(script_path: Path) -> None:
    script_path.write_text(
        """#!/bin/bash
# validate.sh - Trigger the resilience test: stop a follower via SSM, verify it rejoins.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_FILE=$(ls -t "${SCRIPT_DIR}/../.deploy"/sample-private-app-*.json 2>/dev/null | head -1 || true)
if [ -z "${APP_FILE}" ]; then
  echo "ERROR: No sample-private-app deployment found. Run uv run deploy-sample-private-app.py first." >&2
  exit 1
fi

VALIDATE_URL=$(python3 -c "import json; d=json.load(open('${APP_FILE}')); print(d['validate_url'])")
REGION=$(python3 -c "import json; d=json.load(open('${APP_FILE}')); print(d['region'])")
if [ -z "${VALIDATE_URL}" ]; then
  echo "ERROR: This sample app was deployed without --enable-resilience." >&2
  echo "Redeploy with uv run deploy-sample-private-app.py --enable-resilience to create the test-only stop/start Lambda." >&2
  exit 1
fi

eval "$(aws configure export-credentials --format env 2>/dev/null)"

BODY_FILE=$(mktemp)
STATUS=$(curl --silent --show-error --output "${BODY_FILE}" --write-out "%{http_code}" \\
  --max-time 310 --aws-sigv4 "aws:amz:${REGION}:lambda" \\
  --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \\
  -H "x-amz-security-token: ${AWS_SESSION_TOKEN:-}" \\
  -H "Content-Type: application/json" \\
  "${VALIDATE_URL}")

if [ "${STATUS}" -lt 200 ] || [ "${STATUS}" -ge 300 ]; then
  echo "ERROR: Function URL returned HTTP ${STATUS}" >&2
  cat "${BODY_FILE}" >&2
  echo >&2
  rm -f "${BODY_FILE}"
  exit 1
fi

python3 -m json.tool <"${BODY_FILE}"
rm -f "${BODY_FILE}"
"""
    )
    script_path.chmod(0o755)
    print(f"Wrote {script_path}")


def assert_tls_conformance(session, function_arn: str) -> None:
    """Invoke the deployed Lambda and fail the deploy if its in-VPC TLS
    conformance probe reports a hard failure (plaintext accepted, HTTPS down,
    or served cert identity != AdvertisedDNS). Skipped for plaintext stacks."""
    print()
    print("Running in-VPC TLS conformance probe...")
    lam = session.client("lambda")
    resp = lam.invoke(
        FunctionName=function_arn,
        Payload=json.dumps({"tls_probe": True}).encode(),
    )
    payload = json.loads(resp["Payload"].read())

    if resp.get("FunctionError"):
        raise SystemExit(
            f"ERROR: Lambda invocation failed: {payload}"
        )
    if payload.get("statusCode") != 200:
        raise SystemExit(
            f"ERROR: Lambda returned status {payload.get('statusCode')}: "
            f"{payload.get('body')}"
        )

    body = json.loads(payload["body"])
    probe = body.get("tls_conformance", {})

    if not probe.get("applicable"):
        print(f"  TLS conformance: skipped — {probe.get('detail', 'plaintext stack')}")
        return

    for name, result in probe.get("checks", {}).items():
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}] {name}: {result['detail']}")
    strict = probe.get("strict_tls_info", {})
    print(f"  [INFO] strict_tls: {strict.get('detail', 'n/a')}")

    if not probe.get("passed"):
        raise SystemExit(
            "ERROR: TLS conformance probe FAILED — see [FAIL] lines above. "
            "TLS is not correctly enforced end-to-end for this stack."
        )
    print("  TLS conformance: PASS")


def main() -> None:
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()
    suffix = f"-{args.suffix}" if args.suffix else ""

    try:
        outputs_file = resolve_outputs_file(DEPLOY_DIR, args.stack_name)
        fields = read_outputs(outputs_file)
        neo4j_stack = require_field(fields, "StackName", outputs_file)
        region = require_field(fields, "Region", outputs_file)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(
            "ERROR: No EE deployment found. Run ../deploy.py first, then "
            "uv run deploy-sample-private-app.py [stack-name]."
        ) from exc
    deployment_mode = fields.get("DeploymentMode", "")
    number_of_servers = fields.get("NumberOfServers", "1")
    # TLS is signalled by a non-empty AdvertisedDNS (mandatory for Private and
    # ExistingVpc); operator/app clients use the +ssc scheme. Mirrors
    # neo4j_ee.outputs.resolve_bolt_scheme so tooling does not drift.
    bolt_tls_enabled = bool(fields.get("AdvertisedDNS"))

    if deployment_mode not in {"Private", "ExistingVpc"}:
        raise SystemExit(
            "ERROR: Sample private app requires DeploymentMode=Private or "
            f"ExistingVpc (got '{deployment_mode}')."
        )

    session = boto3.Session(region_name=region)
    cfn = session.client("cloudformation")
    s3 = session.client("s3")
    ssm = session.client("ssm")
    sts = session.client("sts")

    app_stack_name = f"neo4j-sample-private-app-{neo4j_stack}{suffix}"
    ssm_prefix = f"/neo4j-ee/{neo4j_stack}"
    neo4j_stack_id = describe_stack_id(cfn, neo4j_stack)
    bolt_scheme = resolve_bolt_scheme(fields)

    print_header(
        neo4j_stack,
        app_stack_name,
        region,
        ssm_prefix,
        args.enable_resilience,
        bolt_scheme,
        bolt_tls_enabled,
    )

    print("Reading SSM parameters from EE stack...")
    vpc_id = require_ssm(ssm, f"{ssm_prefix}/vpc-id")
    external_sg_id = require_ssm(ssm, f"{ssm_prefix}/external-sg-id")
    password_secret_arn = require_ssm(ssm, f"{ssm_prefix}/password-secret-arn")
    vpc_endpoint_sg_id = require_ssm(ssm, f"{ssm_prefix}/vpc-endpoint-sg-id")
    private_subnet_1_id = require_ssm(ssm, f"{ssm_prefix}/private-subnet-1-id")
    subnet_ids = [private_subnet_1_id]
    private_subnet_2_id = ""
    if number_of_servers != "1":
        private_subnet_2_id = require_ssm(ssm, f"{ssm_prefix}/private-subnet-2-id")
        subnet_ids.append(private_subnet_2_id)

    print(f"  vpc-id:              {vpc_id}")
    print(f"  external-sg-id:      {external_sg_id}")
    print(f"  password-secret-arn: {password_secret_arn}")
    print(f"  vpc-endpoint-sg-id:  {vpc_endpoint_sg_id}")
    print(f"  private-subnet-1-id: {private_subnet_1_id}")
    if private_subnet_2_id:
        print(f"  private-subnet-2-id: {private_subnet_2_id}")
    else:
        print("  private-subnet-2-id: not present for single-server EE stack")
    print()

    zip_path = package_lambda()
    account_id = sts.get_caller_identity()["Account"]
    bucket = ensure_deploy_bucket(s3, account_id, region)
    lambda_key = f"{app_stack_name}/lambda.zip"
    lambda_version_id = upload_lambda_zip(s3, bucket, lambda_key, zip_path)

    parameters = {
        "SsmPrefix": ssm_prefix,
        "VpcId": vpc_id,
        "SubnetIds": ",".join(subnet_ids),
        "ExternalSgId": external_sg_id,
        "VpcEndpointSgId": vpc_endpoint_sg_id,
        "PasswordSecretArn": password_secret_arn,
        "Neo4jStackName": neo4j_stack,
        "Neo4jStackId": neo4j_stack_id,
        "LambdaS3Bucket": bucket,
        "LambdaS3Key": lambda_key,
        "LambdaS3ObjectVersion": lambda_version_id,
        "EnableResilienceTestFunction": str(args.enable_resilience).lower(),
        "NumberOfServers": number_of_servers,
        "BoltTlsEnabled": str(bolt_tls_enabled).lower(),
        "AdvertisedDNS": fields.get("AdvertisedDNS", ""),
    }
    deploy_stack(
        cfn,
        app_stack_name,
        TEMPLATE_FILE.read_text(),
        parameters,
        neo4j_stack,
    )

    outputs = stack_outputs(cfn, app_stack_name)
    function_url = outputs["FunctionUrl"]
    function_arn = outputs["FunctionArn"]
    validate_url = outputs.get("ResilienceFunctionUrl", "")
    validate_arn = outputs.get("ResilienceFunctionArn", "")

    print()
    print(f"  Function URL:  {function_url}")
    print(f"  Function ARN:  {function_arn}")
    if args.enable_resilience:
        print(f"  Validate URL:  {validate_url}")
        print(f"  Validate ARN:  {validate_arn}")
    else:
        print("  Validate URL:  disabled (rerun with --enable-resilience)")

    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    app_local_file = DEPLOY_DIR / f"sample-private-app-{neo4j_stack}{suffix}.json"
    write_json(
        app_local_file,
        {
            "stack_name": app_stack_name,
            "neo4j_stack": neo4j_stack,
            "region": region,
            "function_url": function_url,
            "function_arn": function_arn,
            "validate_url": validate_url,
            "validate_arn": validate_arn,
            "bolt_scheme": bolt_scheme,
            "bolt_tls_enabled": str(bolt_tls_enabled).lower(),
        },
    )

    write_invoke_script(SCRIPT_DIR / "invoke.sh")
    write_validate_script(SCRIPT_DIR / "validate.sh")

    assert_tls_conformance(session, function_arn)

    print()
    print("=============================================")
    print("  Deploy complete.")
    print("  To invoke:    ./invoke.sh")
    if args.enable_resilience:
        print(
            "  To validate:  ./validate.sh  "
            "(stops a follower, waits for recovery; ~60-120s)"
        )
    else:
        print(
            "  Validation Lambda disabled. Redeploy with --enable-resilience "
            "for stop/start testing."
        )
    print("  To tear down: ./teardown-sample-private-app.sh")
    print("  (Always tear down the sample app BEFORE the parent EE stack -")
    print("   this stack owns ingress rules on the EE security groups.)")
    print("=============================================")


if __name__ == "__main__":
    main()
