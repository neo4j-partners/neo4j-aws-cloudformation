#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

import argparse
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

SCRIPT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = SCRIPT_DIR.parent / ".deploy"


def parse_args():
    p = argparse.ArgumentParser(
        description="Delete a test VPC created by scripts/create-test-vpc.py.",
    )
    p.add_argument(
        "vpc_name", nargs="?",
        help="VPC deployment name (vpc-<ts>). Defaults to the most recently modified vpc-*.txt in .deploy/.",
    )
    return p.parse_args()


def _read_vpc_file(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip()
    return fields


def _resolve_vpc_file(vpc_name: str | None) -> Path:
    if vpc_name:
        candidate = vpc_name if vpc_name.endswith(".txt") else f"{vpc_name}.txt"
        path = DEPLOY_DIR / candidate
        if not path.exists():
            sys.exit(f"ERROR: {path} not found")
        return path
    vpc_files = sorted(
        DEPLOY_DIR.glob("vpc-*.txt"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not vpc_files:
        sys.exit(f"ERROR: No vpc-*.txt files in {DEPLOY_DIR}")
    return vpc_files[0]


def main():
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()

    vpc_file = _resolve_vpc_file(args.vpc_name)
    fields = _read_vpc_file(vpc_file)

    vpc_id = fields["VpcId"]
    region = fields["Region"]
    with_endpoints = fields.get("WithEndpoints", "false") == "true"
    endpoint_sg_id = fields.get("EndpointSgId", "")
    nat_ids = [fields[f"NatGateway{i}Id"] for i in range(1, 4)]
    eip_alloc_ids = [fields[f"Eip{i}AllocationId"] for i in range(1, 4)]
    subnet_ids = (
        [fields[f"Subnet{i}Id"] for i in range(1, 4)]
        + [fields[f"PublicSubnet{i}Id"] for i in range(1, 4)]
    )
    private_rts = [fields[f"RouteTable{i}Id"] for i in range(1, 4)]
    igw_id = fields["IgwId"]

    ec2 = boto3.client("ec2", region_name=region)

    print("=== Test VPC Teardown ===")
    print()
    print(f"  VPC:    {vpc_id}")
    print(f"  Region: {region}")
    print()

    # Step 1: Interface endpoints (if created)
    if with_endpoints:
        print("Deleting VPC interface endpoints...")
        endpoints = ec2.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "vpc-endpoint-state", "Values": ["available", "pending"]},
            ]
        )["VpcEndpoints"]
        if endpoints:
            ep_ids = [ep["VpcEndpointId"] for ep in endpoints]
            ec2.delete_vpc_endpoints(VpcEndpointIds=ep_ids)
            print(f"  Waiting for {len(ep_ids)} endpoint(s) to delete...")
            for _ in range(30):
                remaining = ec2.describe_vpc_endpoints(
                    Filters=[
                        {"Name": "vpc-id", "Values": [vpc_id]},
                        {"Name": "vpc-endpoint-state", "Values": ["deleting", "available", "pending"]},
                    ]
                )["VpcEndpoints"]
                if not remaining:
                    break
                print(f"  Still deleting ({len(remaining)} remaining)...")
                time.sleep(10)
        print("  Endpoints deleted.")

        if endpoint_sg_id:
            print(f"Deleting endpoint security group {endpoint_sg_id}...")
            # Endpoint ENIs can linger for a few seconds after the endpoints
            # report deleted; delete_security_group fails with
            # DependencyViolation until they detach, so retry briefly.
            for _ in range(12):
                try:
                    ec2.delete_security_group(GroupId=endpoint_sg_id)
                    break
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "DependencyViolation":
                        raise
                    time.sleep(10)
            else:
                sys.exit(
                    f"ERROR: endpoint SG {endpoint_sg_id} still has dependencies "
                    f"after retries; delete it manually then re-run."
                )
            print("  Endpoint SG deleted.")

    # Step 2: NAT gateways
    print("Deleting NAT gateways...")
    for nat_id in nat_ids:
        try:
            ec2.delete_nat_gateway(NatGatewayId=nat_id)
        except Exception:
            pass
    print("  Waiting for NAT gateways to delete (60–90 s)...")
    ec2.get_waiter("nat_gateway_deleted").wait(
        NatGatewayIds=nat_ids,
        WaiterConfig={"Delay": 15, "MaxAttempts": 20},
    )
    print("  NAT gateways deleted.")

    # Step 3: Release EIPs
    print("Releasing EIPs...")
    for alloc_id in eip_alloc_ids:
        try:
            ec2.release_address(AllocationId=alloc_id)
        except Exception:
            pass
    print("  EIPs released.")

    # Step 4: Delete subnets (implicitly removes route table associations)
    print("Deleting subnets...")
    for subnet_id in subnet_ids:
        try:
            ec2.delete_subnet(SubnetId=subnet_id)
        except Exception:
            pass
    print("  Subnets deleted.")

    # Step 5: Delete private route tables (main RT is deleted with the VPC)
    print("Deleting private route tables...")
    for rt_id in private_rts:
        try:
            ec2.delete_route_table(RouteTableId=rt_id)
        except Exception:
            pass
    print("  Route tables deleted.")

    # Step 6: Detach and delete IGW
    print("Detaching and deleting internet gateway...")
    try:
        ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    except Exception:
        pass
    try:
        ec2.delete_internet_gateway(InternetGatewayId=igw_id)
    except Exception:
        pass
    print("  IGW deleted.")

    # Step 7: Delete VPC
    print("Deleting VPC...")
    ec2.delete_vpc(VpcId=vpc_id)
    print("  VPC deleted.")

    # Step 8: Remove output file
    vpc_file.unlink()

    print()
    print("=============================================")
    print("  VPC teardown complete.")
    print("=============================================")


if __name__ == "__main__":
    main()
