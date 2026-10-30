#!/usr/bin/env bash
# Validate CloudFormation negative parameter cases via change sets.
#
# The script creates change sets only. It never executes them. If a negative
# case reaches CREATE_COMPLETE / AVAILABLE, the case fails because CloudFormation
# accepted a configuration that should have been rejected.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-default}"

REGION="us-east-1"
IMAGE_SSM_PARAM="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_DIR="${EE_DIR}/templates"

usage() {
  cat <<EOF
Usage: $0 [--region REGION] [--image-ssm-param PARAM]

Options:
  --region REGION          AWS region for change-set validation. Default: us-east-1
  --image-ssm-param PARAM  SSM parameter resolving to a valid AMI id.
                           Default: ${IMAGE_SSM_PARAM}

This script creates and deletes temporary change sets and a temporary S3 bucket.
It does not execute change sets or create stack resources.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)
      REGION="$2"
      shift 2
      ;;
    --image-ssm-param)
      IMAGE_SSM_PARAM="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '$1'" >&2
      usage >&2
      exit 1
      ;;
  esac
done

command -v aws >/dev/null 2>&1 || {
  echo "ERROR: aws CLI is required" >&2
  exit 1
}

python3 "${TEMPLATE_DIR}/build.py" --verify

ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account \
  --output text)
TS=$(date +%s)
BUCKET="neo4j-ee-neg-cfn-${ACCOUNT_ID}-${REGION}-${TS}"
PUBLIC_KEY="neo4j-public.template.yaml"
EXISTING_KEY="neo4j-private-existing-vpc.template.yaml"
PUBLIC_URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/${PUBLIC_KEY}"
EXISTING_URL="https://${BUCKET}.s3.${REGION}.amazonaws.com/${EXISTING_KEY}"

cleanup_bucket() {
  if [[ -n "${BUCKET:-}" ]]; then
    aws s3 rm "s3://${BUCKET}" --recursive --region "$REGION" >/dev/null 2>&1 || true
    aws s3api delete-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1 || true
  fi
}
trap cleanup_bucket EXIT

echo "Creating temporary template bucket: s3://${BUCKET}"
if [[ "$REGION" == "us-east-1" ]]; then
  aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null
else
  aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
fi

aws s3 cp "${TEMPLATE_DIR}/${PUBLIC_KEY}" "s3://${BUCKET}/${PUBLIC_KEY}" --region "$REGION" >/dev/null
aws s3 cp "${TEMPLATE_DIR}/${EXISTING_KEY}" "s3://${BUCKET}/${EXISTING_KEY}" --region "$REGION" >/dev/null

delete_case_stack() {
  local stack_name="$1"
  local change_set_id="${2:-}"

  if [[ -n "$change_set_id" ]]; then
    aws cloudformation delete-change-set \
      --region "$REGION" \
      --change-set-name "$change_set_id" >/dev/null 2>&1 || true
  fi
  aws cloudformation delete-stack \
    --region "$REGION" \
    --stack-name "$stack_name" >/dev/null 2>&1 || true
}

run_case() {
  local case_name="$1"
  local template_url="$2"
  local expected="$3"
  shift 3

  local stack_name="neo4j-ee-neg-${case_name}-${TS}"
  local change_set_name="cs-${case_name}-${TS}"
  local output status reason execution change_set_id

  echo
  echo "== ${case_name} =="
  set +e
  output=$(aws cloudformation create-change-set \
    --region "$REGION" \
    --stack-name "$stack_name" \
    --change-set-name "$change_set_name" \
    --change-set-type CREATE \
    --template-url "$template_url" \
    --capabilities CAPABILITY_IAM \
    --parameters "$@" \
    --query Id \
    --output text 2>&1)
  local rc=$?
  set -e

  if [[ $rc -ne 0 ]]; then
    if [[ "$output" == *"$expected"* ]]; then
      echo "PASS: rejected during create-change-set"
      echo "  ${output}"
      return 0
    fi
    echo "FAIL: create-change-set failed for an unexpected reason" >&2
    echo "$output" >&2
    return 1
  fi

  change_set_id="$output"
  for _ in $(seq 1 36); do
    status=$(aws cloudformation describe-change-set \
      --region "$REGION" \
      --change-set-name "$change_set_id" \
      --query Status \
      --output text 2>/dev/null || echo UNKNOWN)
    execution=$(aws cloudformation describe-change-set \
      --region "$REGION" \
      --change-set-name "$change_set_id" \
      --query ExecutionStatus \
      --output text 2>/dev/null || echo UNKNOWN)
    reason=$(aws cloudformation describe-change-set \
      --region "$REGION" \
      --change-set-name "$change_set_id" \
      --query 'StatusReason' \
      --output text 2>/dev/null || true)

    case "$status" in
      FAILED)
        delete_case_stack "$stack_name" "$change_set_id"
        if [[ "$reason" == *"$expected"* ]]; then
          echo "PASS: rejected by change-set validation"
          echo "  ${reason}"
          return 0
        fi
        echo "FAIL: change set failed for an unexpected reason" >&2
        echo "  ${reason}" >&2
        return 1
        ;;
      CREATE_COMPLETE)
        delete_case_stack "$stack_name" "$change_set_id"
        echo "FAIL: negative case produced executable change set (${execution})" >&2
        return 1
        ;;
    esac

    sleep 5
  done

  delete_case_stack "$stack_name" "$change_set_id"
  echo "FAIL: timed out waiting for change-set validation" >&2
  return 1
}

COMMON_PUBLIC_PARAMS=(
  "ParameterKey=Password,ParameterValue=TestPass123"
  "ParameterKey=AllowedCIDR,ParameterValue=10.0.0.0/16"
  "ParameterKey=ImageId,ParameterValue=${IMAGE_SSM_PARAM}"
  "ParameterKey=NumberOfServers,ParameterValue=1"
)

COMMON_EXISTING_PARAMS=(
  "ParameterKey=Password,ParameterValue=TestPass123"
  "ParameterKey=AllowedCIDR,ParameterValue=10.0.0.0/16"
  "ParameterKey=ImageId,ParameterValue=${IMAGE_SSM_PARAM}"
  "ParameterKey=NumberOfServers,ParameterValue=1"
  "ParameterKey=VpcId,ParameterValue=vpc-00000000000000000"
  "ParameterKey=PrivateSubnet1Id,ParameterValue=subnet-00000000000000000"
  "ParameterKey=PrivateRouteTable1Id,ParameterValue=rtb-00000000000000000"
)

failures=0

run_case \
  "bloom-license" \
  "$PUBLIC_URL" \
  "BloomLicenseSecretArn must be provided when InstallBloom is true" \
  "${COMMON_PUBLIC_PARAMS[@]}" \
  "ParameterKey=InstallBloom,ParameterValue=true" || failures=$((failures + 1))

run_case \
  "gds-license" \
  "$PUBLIC_URL" \
  "GdsLicenseSecretArn must be provided when InstallGDS is true" \
  "${COMMON_PUBLIC_PARAMS[@]}" \
  "ParameterKey=InstallGDS,ParameterValue=true" || failures=$((failures + 1))

run_case \
  "endpoint-sg" \
  "$EXISTING_URL" \
  "ExistingEndpointSgId must be provided when CreateVpcEndpoints is false" \
  "${COMMON_EXISTING_PARAMS[@]}" \
  "ParameterKey=CreateVpcEndpoints,ParameterValue=false" || failures=$((failures + 1))

echo
if [[ "$failures" -eq 0 ]]; then
  echo "Negative change-set validation passed."
else
  echo "Negative change-set validation failed: ${failures} case(s)." >&2
  exit 1
fi
