#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.43.117"]
# ///

import argparse
import atexit
import os
from pathlib import Path
import random
import secrets
import string
import sys
import time

import boto3

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_REGION = "us-east-1"
SUPPORTED_REGIONS = [
    "us-east-1", "us-east-2", "us-west-2",
    "eu-west-1", "eu-central-1",
    "ap-southeast-1", "ap-southeast-2",
]
INSTANCE_TYPES = {"t3": "t3.medium", "r8i": "r8i.large"}


def parse_args():
    p = argparse.ArgumentParser(
        description="Deploy Neo4j Community Edition CloudFormation stack for local testing.",
    )
    p.add_argument(
        "instance_family", nargs="?", default="t3",
        choices=list(INSTANCE_TYPES), metavar="INSTANCE_FAMILY",
        help="Instance family: t3 (default) or r8i",
    )
    p.add_argument("--region", dest="region_override", metavar="REGION")
    return p.parse_args()


def generate_password():
    # Append a digit to guarantee the template AllowedPattern (letters + digits) is satisfied.
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(15)) + str(secrets.randbelow(10))


def main():
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()

    instance_type = INSTANCE_TYPES[args.instance_family]
    region = args.region_override or random.choice(SUPPORTED_REGIONS)
    ts = int(time.time())
    stack_name = f"test-standalone-{ts}"
    password = generate_password()
    install_apoc = "yes"

    cleanup_state = {"copied_ami_id": None, "cleanup_ami": False}

    def cleanup():
        if cleanup_state["cleanup_ami"] and cleanup_state["copied_ami_id"]:
            ami = cleanup_state["copied_ami_id"]
            print(f"\nCleaning up copied AMI {ami} in {region}...")
            try:
                ec2 = boto3.client("ec2", region_name=region)
                ec2.deregister_image(ImageId=ami)
                snaps = ec2.describe_snapshots(
                    Filters=[{"Name": "description", "Values": [f"*{ami}*"]}]
                )["Snapshots"]
                for snap in snaps:
                    ec2.delete_snapshot(SnapshotId=snap["SnapshotId"])
            except Exception:
                pass

    atexit.register(cleanup)

    ami_id_file = os.path.join(SCRIPT_DIR, "marketplace", "ami-id.txt")
    if not os.path.exists(ami_id_file):
        sys.exit(f"ERROR: {ami_id_file} not found. Run create-ami.sh first.")
    source_ami_id = Path(ami_id_file).read_text().strip()

    ec2 = boto3.client("ec2", region_name=region)
    copied_ami_id = ""

    if region != SOURCE_REGION:
        print(f"Copying AMI {source_ami_id} from {SOURCE_REGION} to {region}...")
        resp = ec2.copy_image(
            SourceRegion=SOURCE_REGION,
            SourceImageId=source_ami_id,
            Name=f"neo4j-ce-copy-{stack_name}",
            Description=f"Copied from {source_ami_id} in {SOURCE_REGION} for {stack_name}",
        )
        copied_ami_id = resp["ImageId"]
        cleanup_state["copied_ami_id"] = copied_ami_id
        cleanup_state["cleanup_ami"] = True
        print(f"Copied AMI: {copied_ami_id} — waiting for it to become available...")
        ec2.get_waiter("image_available").wait(ImageIds=[copied_ami_id])
        print(f"AMI available in {region}.")
        ami_id = copied_ami_id
    else:
        ami_id = source_ami_id

    ssm_param_path = f"/neo4j-ce/test/{stack_name}/ami-id"
    print(f"Creating SSM parameter {ssm_param_path} -> {ami_id}...")
    boto3.client("ssm", region_name=region).put_parameter(
        Name=ssm_param_path, Type="String", Value=ami_id, Overwrite=True,
    )

    print()
    print("=============================================")
    print("  Neo4j CE Deployment")
    print("=============================================")
    print(f"  Stack:        {stack_name}")
    print(f"  Region:       {region}")
    print(f"  Instance:     {instance_type} (family: {args.instance_family})")
    print(f"  Root disk:    20 GB gp3")
    print(f"  Data disk:    30 GB gp3")
    print(f"  APOC:         {install_apoc}")
    print(f"  AMI:          {ami_id}")
    if copied_ami_id:
        print(f"  AMI source:   {source_ami_id} (copied from {SOURCE_REGION})")
    print("=============================================")
    print()

    template_body = Path(os.path.join(SCRIPT_DIR, "neo4j.template.yaml")).read_text()

    cfn_params = [
        {"ParameterKey": "Password", "ParameterValue": password},
        {"ParameterKey": "InstallAPOC", "ParameterValue": install_apoc},
        {"ParameterKey": "ImageId", "ParameterValue": ssm_param_path},
        {"ParameterKey": "InstanceType", "ParameterValue": instance_type},
        {"ParameterKey": "AllowedCIDR", "ParameterValue": "0.0.0.0/0"},
    ]

    cfn = boto3.client("cloudformation", region_name=region)
    print(f"Creating stack {stack_name}...")
    cfn.create_stack(
        StackName=stack_name,
        TemplateBody=template_body,
        Capabilities=["CAPABILITY_IAM"],
        DisableRollback=True,
        Parameters=cfn_params,
    )
    print("Waiting for stack to complete (this takes a few minutes)...")
    cfn.get_waiter("stack_create_complete").wait(
        StackName=stack_name,
        WaiterConfig={"Delay": 15, "MaxAttempts": 120},
    )

    cleanup_state["cleanup_ami"] = False

    deploy_dir = os.path.join(SCRIPT_DIR, ".deploy")
    os.makedirs(deploy_dir, exist_ok=True)
    outputs_file = os.path.join(deploy_dir, f"{stack_name}.txt")

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
        ("InstallAPOC", install_apoc),
        ("InstanceType", instance_type),
        ("SSMParamPath", ssm_param_path),
        ("AmiId", ami_id),
        ("DiskSize", "20"),
        ("DataDiskSize", "30"),
        ("VolumeType", "gp3"),
        ("Edition", "ce"),
    ]
    if copied_ami_id:
        extra.extend([("CopiedAmiId", copied_ami_id), ("SourceRegion", SOURCE_REGION)])

    lines += [f"{k:<20} = {v}" for k, v in extra]
    output = "\n".join(lines) + "\n"
    print(output, end="")
    with open(outputs_file, "w") as f:
        f.write(output)

    print()
    print(f"Outputs saved to {outputs_file}")
    print()
    print(f"To test:      cd test_ce && uv run test-ce --stack {stack_name}")
    print(f"To tear down: ./teardown.sh {stack_name}")


if __name__ == "__main__":
    main()
