#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

import argparse
import os
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

SCRIPT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = SCRIPT_DIR.parent / ".deploy"
VPC_CIDR = "10.42.0.0/16"


def parse_args():
    p = argparse.ArgumentParser(
        description="Create a minimal private-networking VPC for ExistingVpc template testing.",
    )
    p.add_argument("--region", required=True, metavar="REGION")
    p.add_argument(
        "--with-endpoints", action="store_true",
        help="Also create ssm, ssmmessages, logs, secretsmanager interface endpoints "
             "with a shared endpoint SG. Required for Path B (CreateVpcEndpoints=false) testing.",
    )
    return p.parse_args()


def _create_each(items, create, delete, undo, label):
    """Create one resource per item, registering an undo as each one exists."""
    ids = []
    for item in items:
        resource_id = create(item)
        undo.append((f"{label} {resource_id}", lambda r=resource_id: delete(r)))
        ids.append(resource_id)
    return ids


def _delete_endpoints(ec2, vpc_id: str) -> None:
    endpoints = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "vpc-endpoint-state", "Values": ["available", "pending"]},
        ]
    )["VpcEndpoints"]
    if not endpoints:
        return
    ec2.delete_vpc_endpoints(
        VpcEndpointIds=[ep["VpcEndpointId"] for ep in endpoints]
    )
    for _ in range(30):
        remaining = ec2.describe_vpc_endpoints(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {
                    "Name": "vpc-endpoint-state",
                    "Values": ["deleting", "available", "pending"],
                },
            ]
        )["VpcEndpoints"]
        if not remaining:
            return
        time.sleep(10)


def _delete_security_group(ec2, group_id: str) -> None:
    # Endpoint ENIs can linger after the endpoints report deleted.
    for _ in range(12):
        try:
            ec2.delete_security_group(GroupId=group_id)
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "DependencyViolation":
                raise
            time.sleep(10)
    raise RuntimeError(f"security group {group_id} still has dependencies")


def _wait_nat_gateways_deleted(ec2, nat_ids: list[str]) -> None:
    if nat_ids:
        ec2.get_waiter("nat_gateway_deleted").wait(
            NatGatewayIds=nat_ids,
            WaiterConfig={"Delay": 15, "MaxAttempts": 20},
        )


def _rollback(undo) -> None:
    print("ERROR: VPC creation failed. Rolling back the resources created so far...")
    while undo:
        label, action = undo.pop()
        try:
            action()
        except Exception as e:
            print(f"  Warning: could not delete {label}: {e}")


def main():
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()
    region = args.region
    ts = int(time.time())

    DEPLOY_DIR.mkdir(exist_ok=True)
    output_file = DEPLOY_DIR / f"vpc-{ts}.txt"

    ec2 = boto3.client("ec2", region_name=region)

    print(f"Creating test VPC in region {region}...")

    # Enumerate 3 AZs — never hardcode suffixes
    azs = [
        az["ZoneName"]
        for az in ec2.describe_availability_zones(
            Filters=[{"Name": "state", "Values": ["available"]}]
        )["AvailabilityZones"][:3]
    ]
    if len(azs) < 3:
        raise SystemExit(f"ERROR: need at least 3 available AZs in {region}, found {len(azs)}")
    print(f"  AZs: {' '.join(azs)}")

    # LIFO list of (label, delete callable) for everything created so far.
    undo = []
    try:
        _create_vpc(ec2, args, region, ts, azs, output_file, undo)
    except (Exception, KeyboardInterrupt):
        _rollback(undo)
        raise


def _create_vpc(ec2, args, region, ts, azs, output_file, undo) -> None:
    # VPC
    vpc_id = ec2.create_vpc(CidrBlock=VPC_CIDR)["Vpc"]["VpcId"]
    undo.append((f"VPC {vpc_id}", lambda: ec2.delete_vpc(VpcId=vpc_id)))
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    print(f"  VPC: {vpc_id}")

    # Internet Gateway
    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    undo.append(
        (f"IGW {igw_id}", lambda: ec2.delete_internet_gateway(InternetGatewayId=igw_id))
    )
    ec2.attach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id)
    undo.append((
        f"IGW attachment {igw_id}",
        lambda: ec2.detach_internet_gateway(InternetGatewayId=igw_id, VpcId=vpc_id),
    ))
    print(f"  IGW: {igw_id}")

    # Private subnets (10.42.0–2.0/24)
    private_subnets = _create_each(
        [(f"10.42.{i}.0/24", az) for i, az in enumerate(azs)],
        lambda ca: ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=ca[0], AvailabilityZone=ca[1],
        )["Subnet"]["SubnetId"],
        lambda s: ec2.delete_subnet(SubnetId=s),
        undo, "subnet",
    )
    print(f"  Private subnets: {' '.join(private_subnets)}")

    # Public subnets (10.42.10–12.0/24, for NAT gateways)
    public_subnets = _create_each(
        [(f"10.42.{10 + i}.0/24", az) for i, az in enumerate(azs)],
        lambda ca: ec2.create_subnet(
            VpcId=vpc_id, CidrBlock=ca[0], AvailabilityZone=ca[1],
        )["Subnet"]["SubnetId"],
        lambda s: ec2.delete_subnet(SubnetId=s),
        undo, "subnet",
    )
    print(f"  Public subnets:  {' '.join(public_subnets)}")

    # Main route table — add IGW route and associate public subnets
    main_rt = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.main", "Values": ["true"]},
        ]
    )["RouteTables"][0]["RouteTableId"]
    ec2.create_route(RouteTableId=main_rt, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw_id)
    for subnet_id in public_subnets:
        ec2.associate_route_table(RouteTableId=main_rt, SubnetId=subnet_id)

    # Private route tables (one per AZ)
    private_rts = _create_each(
        azs,
        lambda _az: ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"],
        lambda rt: ec2.delete_route_table(RouteTableId=rt),
        undo, "route table",
    )
    print(f"  Private route tables: {' '.join(private_rts)}")

    # EIPs for NAT Gateways
    eip_alloc_ids = _create_each(
        azs,
        lambda _az: ec2.allocate_address(Domain="vpc")["AllocationId"],
        lambda a: ec2.release_address(AllocationId=a),
        undo, "EIP",
    )
    print(f"  EIPs: {' '.join(eip_alloc_ids)}")

    # NAT Gateways (one per public subnet)
    # Runs after the NAT deletes below (LIFO) and before the EIPs are released.
    nat_ids: list[str] = []
    undo.append((
        "NAT gateway deletion wait",
        lambda: _wait_nat_gateways_deleted(ec2, nat_ids),
    ))
    for pub, eip in zip(public_subnets, eip_alloc_ids):
        nat_id = ec2.create_nat_gateway(SubnetId=pub, AllocationId=eip)[
            "NatGateway"
        ]["NatGatewayId"]
        nat_ids.append(nat_id)
        undo.append(
            (f"NAT gateway {nat_id}", lambda n=nat_id: ec2.delete_nat_gateway(NatGatewayId=n))
        )
    print(f"  NAT gateways: {' '.join(nat_ids)}")
    print("  Waiting for NAT gateways to become available...")
    ec2.get_waiter("nat_gateway_available").wait(
        NatGatewayIds=nat_ids,
        WaiterConfig={"Delay": 15, "MaxAttempts": 40},
    )
    print("  NAT gateways available.")

    # Default routes in private route tables + subnet associations
    for rt, nat, priv in zip(private_rts, nat_ids, private_subnets):
        ec2.create_route(RouteTableId=rt, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=nat)
        ec2.associate_route_table(RouteTableId=rt, SubnetId=priv)

    # Interface endpoints (Path B only)
    endpoint_sg_id = ""
    if args.with_endpoints:
        print("  Creating shared endpoint security group...")
        endpoint_sg_id = ec2.create_security_group(
            GroupName=f"neo4j-test-endpoint-sg-{ts}",
            Description="Shared interface endpoint SG for test VPC",
            VpcId=vpc_id,
        )["GroupId"]
        undo.append((
            f"endpoint SG {endpoint_sg_id}",
            lambda: _delete_security_group(ec2, endpoint_sg_id),
        ))
        ec2.authorize_security_group_ingress(
            GroupId=endpoint_sg_id,
            IpPermissions=[{
                "IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
                "IpRanges": [{"CidrIp": VPC_CIDR}],
            }],
        )
        print(f"  Endpoint SG: {endpoint_sg_id}")

        undo.append(("VPC endpoints", lambda: _delete_endpoints(ec2, vpc_id)))
        for svc in ("ssm", "ssmmessages", "logs", "secretsmanager"):
            ep_id = ec2.create_vpc_endpoint(
                VpcId=vpc_id,
                ServiceName=f"com.amazonaws.{region}.{svc}",
                VpcEndpointType="Interface",
                SubnetIds=private_subnets,
                SecurityGroupIds=[endpoint_sg_id],
                PrivateDnsEnabled=True,
            )["VpcEndpoint"]["VpcEndpointId"]
            print(f"  Endpoint ({svc}): {ep_id}")

        print("  Waiting for endpoints to become available...")
        for _ in range(30):
            pending = ec2.describe_vpc_endpoints(
                Filters=[
                    {"Name": "vpc-id", "Values": [vpc_id]},
                    {"Name": "vpc-endpoint-state", "Values": ["pending"]},
                ]
            )["VpcEndpoints"]
            if not pending:
                break
            print(f"  Still pending ({len(pending)})...")
            time.sleep(10)
        print("  Endpoints available.")

    lines = [
        f"VpcId               = {vpc_id}",
        f"Subnet1Id           = {private_subnets[0]}",
        f"Subnet2Id           = {private_subnets[1]}",
        f"Subnet3Id           = {private_subnets[2]}",
        f"VpcCidr             = {VPC_CIDR}",
        f"Region              = {region}",
        f"WithEndpoints       = {'true' if args.with_endpoints else 'false'}",
    ]
    if endpoint_sg_id:
        lines.append(f"EndpointSgId        = {endpoint_sg_id}")
    lines += [
        f"PublicSubnet1Id     = {public_subnets[0]}",
        f"PublicSubnet2Id     = {public_subnets[1]}",
        f"PublicSubnet3Id     = {public_subnets[2]}",
        f"NatGateway1Id       = {nat_ids[0]}",
        f"NatGateway2Id       = {nat_ids[1]}",
        f"NatGateway3Id       = {nat_ids[2]}",
        f"Eip1AllocationId    = {eip_alloc_ids[0]}",
        f"Eip2AllocationId    = {eip_alloc_ids[1]}",
        f"Eip3AllocationId    = {eip_alloc_ids[2]}",
        f"RouteTable1Id       = {private_rts[0]}",
        f"RouteTable2Id       = {private_rts[1]}",
        f"RouteTable3Id       = {private_rts[2]}",
        f"IgwId               = {igw_id}",
    ]
    output_file.write_text("\n".join(lines) + "\n")

    print()
    print(f"VPC created. Output written to {output_file}")
    print()
    print(f"VpcId:     {vpc_id}")
    print(f"Subnet1Id: {private_subnets[0]}")
    print(f"Subnet2Id: {private_subnets[1]}")
    print(f"Subnet3Id: {private_subnets[2]}")
    print(f"VpcCidr:   {VPC_CIDR}")
    if endpoint_sg_id:
        print(f"EndpointSgId: {endpoint_sg_id}")


if __name__ == "__main__":
    main()
