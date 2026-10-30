#!/usr/bin/env bash
# create-test-vpc.sh — Create a minimal private-networking VPC for ExistingVpc template testing.
#
# Usage:
#   scripts/create-test-vpc.sh --region <region> [--with-endpoints]
#
# --with-endpoints: also creates ssm, ssmmessages, logs, secretsmanager
#   interface endpoints in the private subnets with a shared
#   endpoint security group.
#   Required for Path B (CreateVpcEndpoints=false) testing.
#
# Writes .deploy/vpc-<ts>.txt with all resource IDs for use by teardown-test-vpc.sh.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-default}"

REGION=""
WITH_ENDPOINTS=false

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="${SCRIPT_DIR}/../.deploy"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --region)         REGION="$2"; shift 2 ;;
        --with-endpoints) WITH_ENDPOINTS=true; shift ;;
        *) echo "ERROR: Unknown argument '$1'" >&2; exit 1 ;;
    esac
done

if [[ -z "$REGION" ]]; then
    echo "ERROR: --region is required" >&2
    exit 1
fi

VPC_CIDR="10.42.0.0/16"
TS=$(date +%s)
mkdir -p "$DEPLOY_DIR"
OUTPUT_FILE="${DEPLOY_DIR}/vpc-${TS}.txt"

echo "Creating test VPC in region ${REGION}..."

# Enumerate 3 AZs — never hardcode suffixes
mapfile -t AZS < <(aws ec2 describe-availability-zones \
    --region "$REGION" \
    --state available \
    --query 'AvailabilityZones[0:3].ZoneName' \
    --output text | tr '\t' '\n')

if [[ ${#AZS[@]} -lt 3 ]]; then
    echo "ERROR: need at least 3 available AZs in ${REGION}, found ${#AZS[@]}" >&2
    exit 1
fi
echo "  AZs: ${AZS[*]}"

# VPC
VPC_ID=$(aws ec2 create-vpc \
    --region "$REGION" \
    --cidr-block "$VPC_CIDR" \
    --query 'Vpc.VpcId' --output text)
echo "  VPC: $VPC_ID"

aws ec2 modify-vpc-attribute --region "$REGION" --vpc-id "$VPC_ID" \
    --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --region "$REGION" --vpc-id "$VPC_ID" \
    --enable-dns-hostnames '{"Value":true}'

# Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
    --region "$REGION" \
    --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --region "$REGION" \
    --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"
echo "  IGW: $IGW_ID"

# Private subnets (NAT-egress only)
PRIVATE_SUBNET_1=$(aws ec2 create-subnet \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "10.42.0.0/24" --availability-zone "${AZS[0]}" \
    --query 'Subnet.SubnetId' --output text)
PRIVATE_SUBNET_2=$(aws ec2 create-subnet \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "10.42.1.0/24" --availability-zone "${AZS[1]}" \
    --query 'Subnet.SubnetId' --output text)
PRIVATE_SUBNET_3=$(aws ec2 create-subnet \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "10.42.2.0/24" --availability-zone "${AZS[2]}" \
    --query 'Subnet.SubnetId' --output text)
echo "  Private subnets: $PRIVATE_SUBNET_1  $PRIVATE_SUBNET_2  $PRIVATE_SUBNET_3"

# Public subnets (for NAT gateways)
PUBLIC_SUBNET_1=$(aws ec2 create-subnet \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "10.42.10.0/24" --availability-zone "${AZS[0]}" \
    --query 'Subnet.SubnetId' --output text)
PUBLIC_SUBNET_2=$(aws ec2 create-subnet \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "10.42.11.0/24" --availability-zone "${AZS[1]}" \
    --query 'Subnet.SubnetId' --output text)
PUBLIC_SUBNET_3=$(aws ec2 create-subnet \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --cidr-block "10.42.12.0/24" --availability-zone "${AZS[2]}" \
    --query 'Subnet.SubnetId' --output text)
echo "  Public subnets:  $PUBLIC_SUBNET_1  $PUBLIC_SUBNET_2  $PUBLIC_SUBNET_3"

# Main route table — add IGW route and associate public subnets
MAIN_RT=$(aws ec2 describe-route-tables \
    --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" "Name=association.main,Values=true" \
    --query 'RouteTables[0].RouteTableId' --output text)
aws ec2 create-route --region "$REGION" \
    --route-table-id "$MAIN_RT" \
    --destination-cidr-block "0.0.0.0/0" \
    --gateway-id "$IGW_ID" > /dev/null
aws ec2 associate-route-table --region "$REGION" \
    --route-table-id "$MAIN_RT" --subnet-id "$PUBLIC_SUBNET_1" > /dev/null
aws ec2 associate-route-table --region "$REGION" \
    --route-table-id "$MAIN_RT" --subnet-id "$PUBLIC_SUBNET_2" > /dev/null
aws ec2 associate-route-table --region "$REGION" \
    --route-table-id "$MAIN_RT" --subnet-id "$PUBLIC_SUBNET_3" > /dev/null

# Private route tables (one per AZ, routes to per-AZ NAT gateway)
RT_1=$(aws ec2 create-route-table \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --query 'RouteTable.RouteTableId' --output text)
RT_2=$(aws ec2 create-route-table \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --query 'RouteTable.RouteTableId' --output text)
RT_3=$(aws ec2 create-route-table \
    --region "$REGION" --vpc-id "$VPC_ID" \
    --query 'RouteTable.RouteTableId' --output text)
echo "  Private route tables: $RT_1  $RT_2  $RT_3"

# EIPs for NAT Gateways
EIP_1=$(aws ec2 allocate-address \
    --region "$REGION" --domain vpc \
    --query 'AllocationId' --output text)
EIP_2=$(aws ec2 allocate-address \
    --region "$REGION" --domain vpc \
    --query 'AllocationId' --output text)
EIP_3=$(aws ec2 allocate-address \
    --region "$REGION" --domain vpc \
    --query 'AllocationId' --output text)
echo "  EIPs: $EIP_1  $EIP_2  $EIP_3"

# NAT Gateways (in public subnets)
NAT_1=$(aws ec2 create-nat-gateway \
    --region "$REGION" \
    --subnet-id "$PUBLIC_SUBNET_1" --allocation-id "$EIP_1" \
    --query 'NatGateway.NatGatewayId' --output text)
NAT_2=$(aws ec2 create-nat-gateway \
    --region "$REGION" \
    --subnet-id "$PUBLIC_SUBNET_2" --allocation-id "$EIP_2" \
    --query 'NatGateway.NatGatewayId' --output text)
NAT_3=$(aws ec2 create-nat-gateway \
    --region "$REGION" \
    --subnet-id "$PUBLIC_SUBNET_3" --allocation-id "$EIP_3" \
    --query 'NatGateway.NatGatewayId' --output text)
echo "  NAT gateways: $NAT_1  $NAT_2  $NAT_3"
echo "  Waiting for NAT gateways to become available..."
aws ec2 wait nat-gateway-available --region "$REGION" \
    --filter "Name=nat-gateway-id,Values=${NAT_1},${NAT_2},${NAT_3}"
echo "  NAT gateways available."

# Default routes in private route tables and subnet associations
aws ec2 create-route --region "$REGION" \
    --route-table-id "$RT_1" \
    --destination-cidr-block "0.0.0.0/0" \
    --nat-gateway-id "$NAT_1" > /dev/null
aws ec2 create-route --region "$REGION" \
    --route-table-id "$RT_2" \
    --destination-cidr-block "0.0.0.0/0" \
    --nat-gateway-id "$NAT_2" > /dev/null
aws ec2 create-route --region "$REGION" \
    --route-table-id "$RT_3" \
    --destination-cidr-block "0.0.0.0/0" \
    --nat-gateway-id "$NAT_3" > /dev/null

aws ec2 associate-route-table --region "$REGION" \
    --route-table-id "$RT_1" --subnet-id "$PRIVATE_SUBNET_1" > /dev/null
aws ec2 associate-route-table --region "$REGION" \
    --route-table-id "$RT_2" --subnet-id "$PRIVATE_SUBNET_2" > /dev/null
aws ec2 associate-route-table --region "$REGION" \
    --route-table-id "$RT_3" --subnet-id "$PRIVATE_SUBNET_3" > /dev/null

# Interface Endpoints (Path B only)
ENDPOINT_SG_ID=""

if [[ "$WITH_ENDPOINTS" == "true" ]]; then
    echo "  Creating shared endpoint security group..."
    ENDPOINT_SG_ID=$(aws ec2 create-security-group \
        --region "$REGION" \
        --group-name "neo4j-test-endpoint-sg-${TS}" \
        --description "Shared interface endpoint SG for test VPC" \
        --vpc-id "$VPC_ID" \
        --query 'GroupId' --output text)
    aws ec2 authorize-security-group-ingress \
        --region "$REGION" \
        --group-id "$ENDPOINT_SG_ID" \
        --protocol tcp --port 443 \
        --cidr "$VPC_CIDR" > /dev/null
    echo "  Endpoint SG: $ENDPOINT_SG_ID"

    for SVC in ssm ssmmessages logs secretsmanager; do
        EP_ID=$(aws ec2 create-vpc-endpoint \
            --region "$REGION" \
            --vpc-id "$VPC_ID" \
            --service-name "com.amazonaws.${REGION}.${SVC}" \
            --vpc-endpoint-type Interface \
            --subnet-ids "$PRIVATE_SUBNET_1" "$PRIVATE_SUBNET_2" "$PRIVATE_SUBNET_3" \
            --security-group-ids "$ENDPOINT_SG_ID" \
            --private-dns-enabled \
            --query 'VpcEndpoint.VpcEndpointId' --output text)
        echo "  Endpoint (${SVC}): $EP_ID"
    done

    echo "  Waiting for endpoints to become available..."
    for i in $(seq 1 30); do
        pending=$(aws ec2 describe-vpc-endpoints \
            --region "$REGION" \
            --filters "Name=vpc-id,Values=${VPC_ID}" \
                      "Name=vpc-endpoint-state,Values=pending" \
            --query 'length(VpcEndpoints)' --output text)
        if [[ "$pending" == "0" ]]; then
            break
        fi
        echo "  Still pending ($pending)..."
        sleep 10
    done
    echo "  Endpoints available."
fi

# Write output file
{
    echo "VpcId               = ${VPC_ID}"
    echo "Subnet1Id           = ${PRIVATE_SUBNET_1}"
    echo "Subnet2Id           = ${PRIVATE_SUBNET_2}"
    echo "Subnet3Id           = ${PRIVATE_SUBNET_3}"
    echo "VpcCidr             = ${VPC_CIDR}"
    echo "Region              = ${REGION}"
    echo "WithEndpoints       = ${WITH_ENDPOINTS}"
    if [[ -n "$ENDPOINT_SG_ID" ]]; then
        echo "EndpointSgId        = ${ENDPOINT_SG_ID}"
    fi
    echo "PublicSubnet1Id     = ${PUBLIC_SUBNET_1}"
    echo "PublicSubnet2Id     = ${PUBLIC_SUBNET_2}"
    echo "PublicSubnet3Id     = ${PUBLIC_SUBNET_3}"
    echo "NatGateway1Id       = ${NAT_1}"
    echo "NatGateway2Id       = ${NAT_2}"
    echo "NatGateway3Id       = ${NAT_3}"
    echo "Eip1AllocationId    = ${EIP_1}"
    echo "Eip2AllocationId    = ${EIP_2}"
    echo "Eip3AllocationId    = ${EIP_3}"
    echo "RouteTable1Id       = ${RT_1}"
    echo "RouteTable2Id       = ${RT_2}"
    echo "RouteTable3Id       = ${RT_3}"
    echo "IgwId               = ${IGW_ID}"
} > "$OUTPUT_FILE"

echo ""
echo "VPC created. Output written to ${OUTPUT_FILE}"
echo ""
echo "VpcId:     ${VPC_ID}"
echo "Subnet1Id: ${PRIVATE_SUBNET_1}"
echo "Subnet2Id: ${PRIVATE_SUBNET_2}"
echo "Subnet3Id: ${PRIVATE_SUBNET_3}"
echo "VpcCidr:   ${VPC_CIDR}"
if [[ -n "$ENDPOINT_SG_ID" ]]; then
    echo "EndpointSgId: ${ENDPOINT_SG_ID}"
fi
