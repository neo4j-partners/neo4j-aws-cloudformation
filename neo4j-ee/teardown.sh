#!/bin/bash
# teardown.sh — Delete a Neo4j EE CloudFormation stack and clean up resources
#
# Reads .deploy/<stack-name>.txt (written by deploy.py) to determine the stack
# name, region, SSM parameter path, and any copied AMI to clean up.
#
# Usage:
#   ./teardown.sh [--delete-volumes] [--expected-account <id>] [stack-name]
#
# If stack-name is omitted, uses the most recently modified file in .deploy/.
# --delete-volumes: after stack deletion, permanently delete everything the
#   stack retains: the EBS data volumes, the password secrets, the NLB
#   access-log bucket and the stack KMS key. The key is scheduled for deletion
#   with the 7-day minimum window. Without this flag they are printed but left
#   intact.
#   Every deploy creates its own secrets, key and bucket, so test deploys torn
#   down without this flag leave them behind, and each key is billed monthly
#   until it is deleted.
# --expected-account: stop unless the credentials resolve to this 12-digit
#   account id. Defaults to EXPECTED_AWS_ACCOUNT.

set -euo pipefail

# Remember whether the caller chose a profile so the identity banner can say
# so; falling back to `default` silently is how the wrong account gets hit.
PROFILE_WAS_SET=true
[ -n "${AWS_PROFILE:-}" ] || PROFILE_WAS_SET=false
export AWS_PROFILE="${AWS_PROFILE:-default}"
# 12-digit account id the credentials must resolve to (env or --expected-account).
EXPECTED_ACCOUNT="${EXPECTED_AWS_ACCOUNT:-}"

DELETE_VOLUMES=false
# Set to true when a cleanup step fails; the record file is then kept so the
# script can be re-run with the same StackID / cert ARN.
TEARDOWN_FAILED=false

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="${SCRIPT_DIR}/.deploy"

# ---------------------------------------------------------------------------
# Parse arguments: [--delete-volumes] [--expected-account <id>] [stack-name]
# ---------------------------------------------------------------------------
STACK_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --delete-volumes)
      DELETE_VOLUMES=true
      shift
      ;;
    --expected-account)
      if [ $# -lt 2 ]; then
        echo "ERROR: --expected-account requires an account id." >&2
        exit 1
      fi
      EXPECTED_ACCOUNT="$2"
      shift 2
      ;;
    -*)
      echo "ERROR: Unknown option '$1'." >&2
      echo "Usage: $0 [--delete-volumes] [--expected-account <id>] [stack-name]" >&2
      exit 1
      ;;
    *)
      if [ -z "${STACK_ARG}" ]; then
        STACK_ARG="$1"
      else
        echo "ERROR: Unexpected argument '$1'." >&2
        exit 1
      fi
      shift
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Helper: read a value from a "Key = Value" file
# ---------------------------------------------------------------------------
read_field() {
  local file="$1"
  local key="$2"
  grep "^${key}" "$file" | sed 's/^[^=]*= *//' | tr -d '\r'
}

force_delete_secret() {
  local secret_id="$1"
  aws secretsmanager delete-secret \
    --region "${REGION}" \
    --secret-id "${secret_id}" \
    --force-delete-without-recovery 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Accelerated VPC teardown (new-VPC modes only)
#
# CloudFormation deletes the VPC last, and the slow tail is its dependents:
# the NLB and (Private mode) the NAT Gateways and interface VPC endpoints all
# leave ENIs that take minutes to detach. Issuing these deletes ourselves the
# moment delete-stack starts lets them drain in parallel with the rest of the
# stack delete. Every call is idempotent and failure-tolerant: CFN treats an
# already-deleted resource as delete-success, so pre-deleting only speeds
# things up. ExistingVpc is never touched (the caller owns that VPC).
# ---------------------------------------------------------------------------
discover_stack_vpc() {
  aws ec2 describe-vpcs \
    --region "${REGION}" \
    --filters "Name=tag:StackID,Values=${STACK_ID}" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null | grep -v '^None$' || true
}

accelerate_vpc_teardown() {
  local vpc="$1" lb_arn nat endpoints

  echo "  Deleting load balancers in ${vpc}..."
  for lb_arn in $(aws elbv2 describe-load-balancers --region "${REGION}" \
        --query "LoadBalancers[?VpcId=='${vpc}'].LoadBalancerArn" \
        --output text 2>/dev/null || true); do
    [ -n "${lb_arn}" ] || continue
    echo "    ${lb_arn}"
    aws elbv2 delete-load-balancer --region "${REGION}" \
      --load-balancer-arn "${lb_arn}" >/dev/null 2>&1 || true
  done

  echo "  Deleting NAT gateways in ${vpc}..."
  for nat in $(aws ec2 describe-nat-gateways --region "${REGION}" \
        --filter "Name=vpc-id,Values=${vpc}" \
        --query 'NatGateways[?State==`available` || State==`pending`].NatGatewayId' \
        --output text 2>/dev/null || true); do
    [ -n "${nat}" ] || continue
    echo "    ${nat}"
    aws ec2 delete-nat-gateway --region "${REGION}" \
      --nat-gateway-id "${nat}" >/dev/null 2>&1 || true
  done

  endpoints=$(aws ec2 describe-vpc-endpoints --region "${REGION}" \
    --filters "Name=vpc-id,Values=${vpc}" \
    --query 'VpcEndpoints[].VpcEndpointId' --output text 2>/dev/null || true)
  if [ -n "${endpoints}" ]; then
    echo "  Deleting VPC endpoints in ${vpc}..."
    echo "    ${endpoints}"
    aws ec2 delete-vpc-endpoints --region "${REGION}" \
      --vpc-endpoint-ids ${endpoints} >/dev/null 2>&1 || true
  fi
}

# ---------------------------------------------------------------------------
# Force-delete a VPC and its remaining dependents.
#
# Last resort, only when the CloudFormation stack delete did not complete.
# Walks the dependency graph in deletable order, tolerating already-gone
# resources, then deletes the VPC. The caller re-issues delete-stack
# afterward so the DELETE_FAILED stack can finalize with its blockers gone.
# ---------------------------------------------------------------------------
force_delete_vpc() {
  local vpc="$1" eni att subnet rtb sg igw

  echo "Force-deleting VPC ${vpc} and remaining dependents..."
  accelerate_vpc_teardown "${vpc}"

  # NLB/endpoint ENIs free up once their owners are gone; available ones we
  # can delete directly, attached ones we force-detach first.
  for eni in $(aws ec2 describe-network-interfaces --region "${REGION}" \
        --filters "Name=vpc-id,Values=${vpc}" \
        --query 'NetworkInterfaces[].NetworkInterfaceId' \
        --output text 2>/dev/null || true); do
    [ -n "${eni}" ] || continue
    att=$(aws ec2 describe-network-interfaces --region "${REGION}" \
      --network-interface-ids "${eni}" \
      --query 'NetworkInterfaces[0].Attachment.AttachmentId' \
      --output text 2>/dev/null || true)
    if [ -n "${att}" ] && [ "${att}" != "None" ]; then
      aws ec2 detach-network-interface --region "${REGION}" \
        --attachment-id "${att}" --force >/dev/null 2>&1 || true
    fi
    aws ec2 delete-network-interface --region "${REGION}" \
      --network-interface-id "${eni}" >/dev/null 2>&1 || true
  done

  for subnet in $(aws ec2 describe-subnets --region "${REGION}" \
        --filters "Name=vpc-id,Values=${vpc}" \
        --query 'Subnets[].SubnetId' --output text 2>/dev/null || true); do
    [ -n "${subnet}" ] || continue
    aws ec2 delete-subnet --region "${REGION}" \
      --subnet-id "${subnet}" >/dev/null 2>&1 || true
  done

  for rtb in $(aws ec2 describe-route-tables --region "${REGION}" \
        --filters "Name=vpc-id,Values=${vpc}" \
        --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' \
        --output text 2>/dev/null || true); do
    [ -n "${rtb}" ] || continue
    aws ec2 delete-route-table --region "${REGION}" \
      --route-table-id "${rtb}" >/dev/null 2>&1 || true
  done

  for sg in $(aws ec2 describe-security-groups --region "${REGION}" \
        --filters "Name=vpc-id,Values=${vpc}" \
        --query "SecurityGroups[?GroupName!='default'].GroupId" \
        --output text 2>/dev/null || true); do
    [ -n "${sg}" ] || continue
    aws ec2 delete-security-group --region "${REGION}" \
      --group-id "${sg}" >/dev/null 2>&1 || true
  done

  for igw in $(aws ec2 describe-internet-gateways --region "${REGION}" \
        --filters "Name=attachment.vpc-id,Values=${vpc}" \
        --query 'InternetGateways[].InternetGatewayId' \
        --output text 2>/dev/null || true); do
    [ -n "${igw}" ] || continue
    aws ec2 detach-internet-gateway --region "${REGION}" \
      --internet-gateway-id "${igw}" --vpc-id "${vpc}" >/dev/null 2>&1 || true
    aws ec2 delete-internet-gateway --region "${REGION}" \
      --internet-gateway-id "${igw}" >/dev/null 2>&1 || true
  done

  if aws ec2 delete-vpc --region "${REGION}" --vpc-id "${vpc}" >/dev/null 2>&1; then
    echo "VPC ${vpc} deleted."
  else
    echo "  WARNING: delete-vpc did not succeed — some dependents may still" \
         "be detaching. Check the VPC console for ${vpc}."
  fi
}

# ---------------------------------------------------------------------------
# Resolve the outputs file
# ---------------------------------------------------------------------------
if [ -n "${STACK_ARG}" ]; then
  OUTPUTS_FILE="${DEPLOY_DIR}/${STACK_ARG}.txt"
  # A deploy that failed or was interrupted leaves only <stack>.failed. It is
  # accepted when named explicitly, never by the no-argument default below.
  if [ ! -f "${OUTPUTS_FILE}" ] && [ -f "${DEPLOY_DIR}/${STACK_ARG}.failed" ]; then
    OUTPUTS_FILE="${DEPLOY_DIR}/${STACK_ARG}.failed"
  fi
elif [ -d "${DEPLOY_DIR}" ]; then
  OUTPUTS_FILE=$(ls -t "${DEPLOY_DIR}"/*.txt 2>/dev/null | head -1 || true)
else
  OUTPUTS_FILE=""
fi

if [ -z "${OUTPUTS_FILE}" ] || [ ! -f "${OUTPUTS_FILE}" ]; then
  echo "ERROR: No deployment found." >&2
  if [ -n "${STACK_ARG}" ]; then
    echo "File not found: ${DEPLOY_DIR}/${STACK_ARG}.txt (or .failed)" >&2
  else
    echo "No .txt files in ${DEPLOY_DIR}/" >&2
  fi
  echo "Usage: $0 [--delete-volumes] [--expected-account <id>] [stack-name]" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
STACK_NAME=$(read_field "${OUTPUTS_FILE}" "StackName")
REGION=$(read_field "${OUTPUTS_FILE}" "Region")
SSM_PARAM_PATH=$(read_field "${OUTPUTS_FILE}" "SSMParamPath" 2>/dev/null || true)
COPIED_AMI_ID=$(read_field "${OUTPUTS_FILE}" "CopiedAmiId" 2>/dev/null || true)
AUTO_LICENSE_SECRET_ARNS=$(read_field "${OUTPUTS_FILE}" "AutoCreatedLicenseSecretArns" 2>/dev/null || true)
STACK_ID=$(read_field "${OUTPUTS_FILE}" "StackID" 2>/dev/null || true)
DEPLOYMENT_MODE=$(read_field "${OUTPUTS_FILE}" "DeploymentMode" 2>/dev/null || true)

if [ -z "${STACK_NAME}" ] || [ -z "${REGION}" ]; then
  echo "ERROR: Could not read StackName or Region from ${OUTPUTS_FILE}." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Account guard: show which account/profile/region will be touched, and stop
# before any delete call if it is not the expected account. Needs REGION, so it
# runs right after the record file is loaded.
# ---------------------------------------------------------------------------
if [ "${PROFILE_WAS_SET}" = false ]; then
  echo "WARNING: AWS_PROFILE not set, using default" >&2
fi
if ! CALLER_ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
    || [ -z "${CALLER_ACCOUNT}" ]; then
  echo "ERROR: could not resolve AWS credentials for profile '${AWS_PROFILE}'." >&2
  echo "Run: aws sso login --profile ${AWS_PROFILE}" >&2
  exit 1
fi
CALLER_ARN=$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || echo "unknown")
echo "Account: ${CALLER_ACCOUNT}  Profile: ${AWS_PROFILE}  Region: ${REGION}"
echo "Caller:  ${CALLER_ARN}"
if [ -n "${EXPECTED_ACCOUNT}" ]; then
  if ! [[ "${EXPECTED_ACCOUNT}" =~ ^[0-9]{12}$ ]]; then
    echo "ERROR: expected account must be a 12-digit id, got '${EXPECTED_ACCOUNT}'." >&2
    exit 1
  fi
  if [ "${CALLER_ACCOUNT}" != "${EXPECTED_ACCOUNT}" ]; then
    echo "ERROR: AWS account mismatch: credentials resolve to ${CALLER_ACCOUNT}, expected ${EXPECTED_ACCOUNT}." >&2
    exit 1
  fi
fi
echo ""

echo "=== Neo4j EE Stack Teardown ==="
echo ""
echo "  Stack:     ${STACK_NAME}"
echo "  Region:    ${REGION}"
if [ -n "${SSM_PARAM_PATH}" ]; then
  echo "  SSM Param: ${SSM_PARAM_PATH}"
fi
if [ -n "${COPIED_AMI_ID}" ]; then
  echo "  Copied AMI: ${COPIED_AMI_ID}"
fi
echo ""

# ---------------------------------------------------------------------------
# Step 0a: Refuse to delete a VPC that other stacks still use
#
# Public and Private stacks create their VPC. Another stack, such as Hume, can
# place security groups and instances in it. Deleting this stack first would
# strand those resources and leave the VPC delete stuck. Any security group
# or ENI in the VPC that this stack does not own blocks the teardown. The
# default security group and NAT gateway ENIs are exempt, because the VPC and
# the stack's NAT gateways own them.
# The guard cannot see a foreign instance whose ENIs carry only this stack's
# security groups. Such an ENI looks stack-owned and does not block teardown.
# ---------------------------------------------------------------------------
# describe-stack-resources returns at most 100 resources and the Private stack
# has more. list-stack-resources paginates, and the CLI follows every page.
STACK_RESOURCES=$(aws cloudformation list-stack-resources \
  --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query 'StackResourceSummaries[].[LogicalResourceId,ResourceType,PhysicalResourceId]' \
  --output text 2>/dev/null || true)

stack_physical_ids_of_type() {
  local rtype="$1"
  [ -n "${STACK_RESOURCES}" ] || return 0
  awk -F'\t' -v t="${rtype}" '$2 == t && $3 != "" && $3 != "None" { print $3 }' \
    <<< "${STACK_RESOURCES}"
}

case "${DEPLOYMENT_MODE}" in
  Public|Private)
    if [ -n "${STACK_ID}" ]; then
      GUARD_VPC_ID=$(discover_stack_vpc)
      if [ -n "${GUARD_VPC_ID}" ]; then
        STACK_SGS=" $(stack_physical_ids_of_type 'AWS::EC2::SecurityGroup' | tr '\n' ' ') "
        # The NLB names its ENIs "ELB net/<name>/<id>" after its own ARN.
        STACK_NLB_DESCS=" $(stack_physical_ids_of_type 'AWS::ElasticLoadBalancingV2::LoadBalancer' \
          | sed 's|^.*:loadbalancer/|ELB_|' | tr '\n' ' ') "
        FOREIGN=""
        for sg in $(aws ec2 describe-security-groups --region "${REGION}" \
              --filters "Name=vpc-id,Values=${GUARD_VPC_ID}" \
              --query "SecurityGroups[?GroupName!='default'].GroupId" \
              --output text 2>/dev/null || true); do
          [[ "${STACK_SGS}" == *" ${sg} "* ]] || FOREIGN="${FOREIGN} sg:${sg}"
        done
        while IFS=$'\t' read -r eni itype desc groups; do
          [ -n "${eni}" ] || continue
          [ "${itype}" = "nat_gateway" ] && continue
          [[ "${STACK_NLB_DESCS}" == *" ${desc/ELB /ELB_} "* ]] && continue
          owned=false
          for g in ${groups}; do
            if [[ "${STACK_SGS}" == *" ${g} "* ]]; then
              owned=true
            else
              owned=false
              break
            fi
          done
          [ "${owned}" = true ] || FOREIGN="${FOREIGN} eni:${eni}"
        # Tab is IFS whitespace, so read collapses an empty text field and
        # shifts the next column left. The '-' fallbacks keep every field
        # non-empty. The join separator is a raw string, because a backtick
        # literal parses as JSON and yields an empty separator.
        done < <(aws ec2 describe-network-interfaces --region "${REGION}" \
              --filters "Name=vpc-id,Values=${GUARD_VPC_ID}" \
              --query "NetworkInterfaces[].[NetworkInterfaceId,InterfaceType,Description || '-',join(' ', Groups[].GroupId) || '-']" \
              --output text 2>/dev/null || true)
        if [ -n "${FOREIGN}" ]; then
          echo "ERROR: VPC ${GUARD_VPC_ID} holds resources this stack does not own:" >&2
          for item in ${FOREIGN}; do
            echo "  ${item}" >&2
          done
          echo "Delete the stack that uses this VPC first, then re-run teardown." >&2
          exit 1
        fi
      fi
    fi
    ;;
  *)
    : # ExistingVpc or unknown mode: the caller owns the VPC
    ;;
esac

# ---------------------------------------------------------------------------
# Step 0b: Record the resources the stack retains on delete
#
# The password secrets, the NLB access-log bucket and the stack KMS key carry
# DeletionPolicy: Retain. Their physical IDs come from the stack before it is
# deleted. On a re-run the stack is gone, so the StackID tag finds them.
# ---------------------------------------------------------------------------
RETAINED_SECRETS=$(stack_physical_ids_of_type 'AWS::SecretsManager::Secret')
RETAINED_BUCKETS=$(stack_physical_ids_of_type 'AWS::S3::Bucket')
RETAINED_KEYS=$(stack_physical_ids_of_type 'AWS::KMS::Key')
if [ -z "${STACK_RESOURCES}" ] && [ -n "${STACK_ID}" ]; then
  TAGGED=$(aws resourcegroupstaggingapi get-resources --region "${REGION}" \
    --tag-filters "Key=StackID,Values=${STACK_ID}" \
    --resource-type-filters secretsmanager:secret s3 kms:key \
    --query 'ResourceTagMappingList[].ResourceARN' \
    --output text 2>/dev/null | tr '\t' '\n' || true)
  RETAINED_SECRETS=$(grep ':secretsmanager:' <<< "${TAGGED}" || true)
  RETAINED_BUCKETS=$(grep ':s3:::' <<< "${TAGGED}" | sed 's/^arn:[^:]*:s3:::\(.*\)$/\1/' || true)
  RETAINED_KEYS=$(grep ':kms:' <<< "${TAGGED}" || true)
fi

# ---------------------------------------------------------------------------
# Step 1: Delete the CloudFormation stack
# ---------------------------------------------------------------------------
echo "Deleting stack ${STACK_NAME}..."
aws cloudformation delete-stack \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"

# ---------------------------------------------------------------------------
# Step 1a: Accelerate VPC teardown for new-VPC modes.
#
# CFN deletes the VPC last; its NLB / NAT Gateways / interface endpoints are
# the slow tail. Deleting them now lets them drain in parallel with the rest
# of the stack delete. ExistingVpc (caller-owned VPC) is deliberately skipped.
# ---------------------------------------------------------------------------
ACCEL_VPC_ID=""
case "${DEPLOYMENT_MODE}" in
  Public|Private)
    if [ -n "${STACK_ID}" ]; then
      echo ""
      echo "Accelerating VPC teardown (deleting slow dependents up front)..."
      ACCEL_VPC_ID=$(discover_stack_vpc)
      if [ -n "${ACCEL_VPC_ID}" ]; then
        echo "  Stack VPC: ${ACCEL_VPC_ID}"
        accelerate_vpc_teardown "${ACCEL_VPC_ID}"
      else
        echo "  No stack VPC found via tag:StackID — skipping acceleration."
      fi
    fi
    ;;
  *)
    : # ExistingVpc or unknown mode: never touch the VPC
    ;;
esac

echo ""
echo "Waiting for stack deletion to complete (dependents draining in parallel)..."
if aws cloudformation wait stack-delete-complete \
     --stack-name "${STACK_NAME}" \
     --region "${REGION}"; then
  echo "Stack deleted."
else
  echo "WARNING: CloudFormation stack delete did not complete cleanly." >&2
  STATUS=$(aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" --region "${REGION}" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "GONE")
  echo "  Stack status: ${STATUS}" >&2
  if [ "${STATUS}" = "GONE" ]; then
    echo "  Stack no longer exists — treating as deleted."
  elif [ -n "${ACCEL_VPC_ID}" ]; then
    force_delete_vpc "${ACCEL_VPC_ID}"
    echo "Re-issuing stack delete now that VPC blockers are gone..."
    aws cloudformation delete-stack \
      --stack-name "${STACK_NAME}" --region "${REGION}" 2>/dev/null || true
    if aws cloudformation wait stack-delete-complete \
         --stack-name "${STACK_NAME}" --region "${REGION}" 2>/dev/null; then
      echo "Stack deleted."
    else
      echo "  WARNING: stack still not deleted — inspect it in the" \
           "CloudFormation console: ${STACK_NAME} (${REGION})." >&2
      TEARDOWN_FAILED=true
    fi
  else
    echo "  No stack VPC discovered to force-delete — inspect the stack in" \
         "the CloudFormation console: ${STACK_NAME} (${REGION})." >&2
    TEARDOWN_FAILED=true
  fi
fi

# ---------------------------------------------------------------------------
# Step 2: Delete the temporary ImageId SSM parameter
# ---------------------------------------------------------------------------
if [ -n "${SSM_PARAM_PATH}" ]; then
  echo ""
  echo "Deleting SSM parameter ${SSM_PARAM_PATH}..."
  aws ssm delete-parameter \
    --region "${REGION}" \
    --name "${SSM_PARAM_PATH}" 2>/dev/null || true
  echo "SSM parameter deleted."
fi

# ---------------------------------------------------------------------------
# Step 2c: Delete the auto-imported self-signed ACM certificate
#
# deploy.py records AutoImportedCertificateArn only when it imported a
# self-signed cert itself for the Private/ExistingVpc test path. A
# user-supplied --cert-arn is recorded as CertificateArn (not
# AutoImportedCertificateArn) and is deliberately left untouched here. The
# cert can only be deleted once the stack (and its NLB listeners) are gone,
# so this runs after stack-delete-complete; ACM briefly reports the cert as
# in use right after deletion, so the delete is retried. A cert that is already
# gone counts as success; any other failure keeps the record file.
# ---------------------------------------------------------------------------
AUTO_IMPORTED_CERT_ARN=$(read_field "${OUTPUTS_FILE}" "AutoImportedCertificateArn" 2>/dev/null || true)
if [ -n "${AUTO_IMPORTED_CERT_ARN}" ]; then
  echo ""
  echo "Deleting auto-imported ACM certificate ${AUTO_IMPORTED_CERT_ARN}..."
  CERT_GONE=false
  for attempt in 1 2 3 4 5; do
    if CERT_ERR=$(aws acm delete-certificate \
        --region "${REGION}" \
        --certificate-arn "${AUTO_IMPORTED_CERT_ARN}" 2>&1); then
      CERT_GONE=true
      break
    fi
    # deploy.py's own failure cleanup may already have deleted the cert.
    if [[ "${CERT_ERR}" == *ResourceNotFoundException* ]]; then
      echo "  Certificate already deleted."
      CERT_GONE=true
      break
    fi
    if [ "${attempt}" -lt 5 ]; then
      echo "  Certificate still in use, retrying in 10s (${attempt}/5)..."
      sleep 10
    fi
  done
  if [ "${CERT_GONE}" = false ]; then
    echo "  WARNING: Could not delete ACM certificate (it may still be" \
         "detaching from the NLB) — check the ACM console."
    TEARDOWN_FAILED=true
  fi
  echo "ACM certificate cleanup done."
fi

# ---------------------------------------------------------------------------
# Step 2d: Force-delete licence secrets created from local .licenses files
# ---------------------------------------------------------------------------
if [ -n "${AUTO_LICENSE_SECRET_ARNS}" ]; then
  echo ""
  echo "Force-deleting auto-created licence secrets..."
  IFS=',' read -r -a LICENSE_ARNS <<< "${AUTO_LICENSE_SECRET_ARNS}"
  for secret_arn in "${LICENSE_ARNS[@]}"; do
    if [ -n "${secret_arn}" ]; then
      echo "  ${secret_arn}"
      force_delete_secret "${secret_arn}"
    fi
  done
  echo "Licence secret cleanup done."
fi

# ---------------------------------------------------------------------------
# Step 2e: Handle retained EBS data volumes
#
# Volumes have DeletionPolicy: Retain so they survive stack deletion by design.
# Default: enumerate them and print a rerun hint so the operator can decide.
# --delete-volumes: permanently delete each volume.
# ---------------------------------------------------------------------------
if [ -n "${STACK_ID}" ]; then
  echo ""
  echo "Looking up retained data volumes for StackID=${STACK_ID}..."
  VOLUME_IDS=$(aws ec2 describe-volumes \
    --region "${REGION}" \
    --filters \
      "Name=tag:StackID,Values=${STACK_ID}" \
      "Name=tag:Role,Values=neo4j-cluster-data" \
    --query "Volumes[*].[VolumeId,Size,AvailabilityZone,CreateTime]" \
    --output text 2>/dev/null || true)

  if [ -z "${VOLUME_IDS}" ]; then
    echo "No retained data volumes found."
  elif [ "${DELETE_VOLUMES}" = true ]; then
    echo "Deleting retained data volumes..."
    while IFS=$'\t' read -r vol_id size az created; do
      echo "  Deleting ${vol_id} (${size}GB, ${az}, created ${created})..."
      if aws ec2 delete-volume --region "${REGION}" --volume-id "${vol_id}"; then
        echo "  Deleted ${vol_id}."
      else
        echo "  WARNING: Failed to delete ${vol_id} — check AWS console."
        TEARDOWN_FAILED=true
      fi
    done <<< "${VOLUME_IDS}"
    if [ "${TEARDOWN_FAILED}" = true ]; then
      echo "Some cleanup steps failed (see warnings above)."
    else
      echo "All data volumes deleted."
    fi
  else
    echo "Retained data volumes (not deleted — pass --delete-volumes to remove):"
    while IFS=$'\t' read -r vol_id size az created; do
      echo "  ${vol_id}  ${size}GB  ${az}  created ${created}"
    done <<< "${VOLUME_IDS}"
    echo ""
    echo "To delete: ./teardown.sh --delete-volumes ${STACK_NAME}"
  fi
fi

# ---------------------------------------------------------------------------
# Step 2f: Handle the retained secrets, log bucket and stack KMS key
#
# These survive stack deletion so that a deleted stack never loses its
# credentials or its audit trail by accident. --delete-volumes removes them.
# Secrets go first, then the bucket, then the key the secrets were encrypted
# with. KMS enforces a 7-day minimum wait before a key is gone.
# ---------------------------------------------------------------------------
if [ -n "${RETAINED_SECRETS}${RETAINED_BUCKETS}${RETAINED_KEYS}" ]; then
  echo ""
  if [ "${DELETE_VOLUMES}" = true ]; then
    echo "Deleting retained secrets, log bucket and stack key..."
    for secret_id in ${RETAINED_SECRETS}; do
      echo "  Secret ${secret_id}"
      force_delete_secret "${secret_id}"
    done
    for bucket in ${RETAINED_BUCKETS}; do
      echo "  Bucket ${bucket}"
      if ! aws s3 rm "s3://${bucket}" --recursive --region "${REGION}" >/dev/null \
          || ! aws s3api delete-bucket --bucket "${bucket}" --region "${REGION}"; then
        BUCKET_ERR=$(aws s3api head-bucket --bucket "${bucket}" --region "${REGION}" 2>&1 || true)
        if [[ "${BUCKET_ERR}" != *"Not Found"* && "${BUCKET_ERR}" != *"404"* ]]; then
          echo "  WARNING: Could not delete bucket ${bucket}. Check the S3 console."
          TEARDOWN_FAILED=true
        fi
      fi
    done
    for key_id in ${RETAINED_KEYS}; do
      KEY_STATE=$(aws kms describe-key --key-id "${key_id}" --region "${REGION}" \
        --query 'KeyMetadata.KeyState' --output text 2>/dev/null || echo "Missing")
      case "${KEY_STATE}" in
        PendingDeletion|Missing)
          echo "  Key ${key_id} is already ${KEY_STATE}."
          ;;
        *)
          echo "  Key ${key_id} (scheduled for deletion in 7 days)"
          if ! aws kms schedule-key-deletion --key-id "${key_id}" \
              --pending-window-in-days 7 --region "${REGION}" >/dev/null; then
            echo "  WARNING: Could not schedule deletion of key ${key_id}."
            TEARDOWN_FAILED=true
          fi
          ;;
      esac
    done
  else
    echo "Retained secrets, log bucket and stack key (not deleted, pass --delete-volumes to remove):"
    for item in ${RETAINED_SECRETS} ${RETAINED_BUCKETS} ${RETAINED_KEYS}; do
      echo "  ${item}"
    done
    echo ""
    echo "To delete: ./teardown.sh --delete-volumes ${STACK_NAME}"
  fi
fi

if [ "${TEARDOWN_FAILED}" = true ]; then
  echo ""
  echo "Keeping ${OUTPUTS_FILE} because a cleanup step failed."
  echo "Fix the warnings above, then re-run: ./teardown.sh ${STACK_NAME}"
  echo ""
  echo "============================================="
  echo "  Teardown finished with warnings."
  echo "============================================="
  exit 1
else
  echo ""
  echo "Removing ${OUTPUTS_FILE}..."
  rm -f "${OUTPUTS_FILE}"

  echo ""
  echo "============================================="
  echo "  Teardown complete."
  echo "============================================="
fi
