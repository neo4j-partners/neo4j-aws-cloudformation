#!/bin/bash
# test-ami.sh — Verify an EE Marketplace base AMI via SSM Run Command
#
# Launches a temporary instance from the AMI, runs verification commands
# over SSM (no SSH key or port 22 required), reports pass/fail, and
# cleans up.
#
# The EE AMI is a base OS image with SSH hardening, OS patches, and static
# deployment tooling pre-baked.
# Neo4j Enterprise is installed at deploy time via CloudFormation UserData,
# so this script only verifies the base image properties.
#
# Prerequisites:
#   - AWS CLI configured with the marketplace profile (neo4j-marketplace account)
#   - The AMI must exist in us-east-1
#
# Usage:
#   AWS_PROFILE=marketplace ./test-ami.sh [ami-id]
#
# If ami-id is omitted, reads from ami-id.txt (written by create-ami.sh).

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REGION="us-east-1"
INSTANCE_TYPE="t3.medium"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Resolve AMI ID: argument > ami-id.txt
if [ -n "${1:-}" ]; then
  AMI_ID="$1"
elif [ -f "${SCRIPT_DIR}/ami-id.txt" ]; then
  AMI_ID=$(cat "${SCRIPT_DIR}/ami-id.txt")
else
  echo "ERROR: No AMI ID provided and ${SCRIPT_DIR}/ami-id.txt not found."
  echo "Usage: AWS_PROFILE=marketplace $0 [ami-id]"
  echo ""
  echo "Either pass an AMI ID as an argument or run create-ami.sh first"
  echo "(it writes the AMI ID to ami-id.txt automatically)."
  exit 1
fi

INSTANCE_TAGS="ResourceType=instance,Tags=[{Key=Name,Value=neo4j-ee-ami-test},{Key=Purpose,Value=marketplace-ami-test}]"

# ---------------------------------------------------------------------------
# Preflight: verify AWS account
# ---------------------------------------------------------------------------
echo "=== Neo4j EE AMI Tester ==="
echo ""
echo "Verifying AWS identity..."

CALLER_IDENTITY=$(aws sts get-caller-identity --output json 2>&1) || {
  echo "ERROR: Failed to call sts get-caller-identity."
  echo "Make sure you are authenticated."
  exit 1
}

ACCOUNT_ID=$(echo "${CALLER_IDENTITY}" | grep -o '"Account": "[^"]*"' | cut -d'"' -f4)

echo "  Account: ${ACCOUNT_ID}"
echo "  AMI:     ${AMI_ID}"
echo "  Region:  ${REGION}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Verify the AMI exists
# ---------------------------------------------------------------------------
echo "Verifying AMI ${AMI_ID} exists..."
AMI_STATE=$(aws ec2 describe-images \
  --region "${REGION}" \
  --image-ids "${AMI_ID}" \
  --query "Images[0].State" \
  --output text 2>/dev/null || true)

if [ -z "${AMI_STATE}" ] || [ "${AMI_STATE}" == "None" ]; then
  echo "ERROR: AMI ${AMI_ID} not found in ${REGION}."
  exit 1
fi

if [ "${AMI_STATE}" != "available" ]; then
  echo "ERROR: AMI ${AMI_ID} is in state '${AMI_STATE}' (expected 'available')."
  exit 1
fi
echo "AMI is available."

# ---------------------------------------------------------------------------
# Step 2: Create a temporary IAM role with SSM permissions
# ---------------------------------------------------------------------------
ROLE_NAME="neo4j-ee-ami-test-ssm-role"
PROFILE_NAME="neo4j-ee-ami-test-ssm-profile"

echo ""
echo "Setting up IAM role for SSM access..."

aws iam create-role \
  --role-name "${ROLE_NAME}" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "ec2.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }' \
  --tags Key=Purpose,Value=marketplace-ami-test \
  2>/dev/null || true

aws iam attach-role-policy \
  --role-name "${ROLE_NAME}" \
  --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" \
  2>/dev/null || true

aws iam create-instance-profile \
  --instance-profile-name "${PROFILE_NAME}" \
  2>/dev/null || true

aws iam add-role-to-instance-profile \
  --instance-profile-name "${PROFILE_NAME}" \
  --role-name "${ROLE_NAME}" \
  2>/dev/null || true

echo "Waiting for IAM propagation..."
sleep 10

# ---------------------------------------------------------------------------
# Step 3: Launch test instance
# ---------------------------------------------------------------------------
echo ""
echo "Launching test instance from ${AMI_ID}..."
INSTANCE_ID=$(aws ec2 run-instances \
  --region "${REGION}" \
  --image-id "${AMI_ID}" \
  --instance-type "${INSTANCE_TYPE}" \
  --iam-instance-profile "Name=${PROFILE_NAME}" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
  --tag-specifications "${INSTANCE_TAGS}" \
  --query "Instances[0].InstanceId" \
  --output text)

if [ -z "${INSTANCE_ID}" ]; then
  echo "ERROR: Failed to launch test instance."
  exit 1
fi
echo "Instance launched: ${INSTANCE_ID}"

# ---------------------------------------------------------------------------
# Cleanup trap — always terminate the test instance on exit
# ---------------------------------------------------------------------------
cleanup() {
  echo ""
  echo "Cleaning up: terminating test instance ${INSTANCE_ID}..."
  aws ec2 terminate-instances \
    --region "${REGION}" \
    --instance-ids "${INSTANCE_ID}" > /dev/null 2>&1 || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Step 4: Wait for instance to be running and SSM agent to register
# ---------------------------------------------------------------------------
echo ""
echo "Waiting for instance to start..."
aws ec2 wait instance-running \
  --region "${REGION}" \
  --instance-ids "${INSTANCE_ID}"
echo "Instance running."

echo "Waiting for SSM agent to register (may take 30-60 seconds)..."
SSM_READY=false
for i in $(seq 1 30); do
  SSM_STATUS=$(aws ssm describe-instance-information \
    --region "${REGION}" \
    --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
    --query "InstanceInformationList[0].PingStatus" \
    --output text 2>/dev/null || true)

  if [ "${SSM_STATUS}" == "Online" ]; then
    SSM_READY=true
    break
  fi
  sleep 5
done

if [ "${SSM_READY}" != "true" ]; then
  echo "ERROR: SSM agent did not register within 150 seconds."
  echo "The instance may not have internet access or the IAM role may not have propagated."
  exit 1
fi
echo "SSM agent online."

# ---------------------------------------------------------------------------
# Step 5: Run verification commands via SSM Run Command
# ---------------------------------------------------------------------------
echo ""
echo "Running verification commands..."

COMMAND_ID=$(aws ssm send-command \
  --region "${REGION}" \
  --instance-ids "${INSTANCE_ID}" \
  --document-name "AWS-RunShellScript" \
  --comment "Neo4j EE base AMI verification" \
  --parameters 'commands=[
    "echo \"=== CHECK 1: SSH password authentication ===\"",
    "grep -i PasswordAuthentication /etc/ssh/sshd_config | head -5",
    "echo \"\"",
    "echo \"=== CHECK 2: Root login disabled ===\"",
    "grep -i PermitRootLogin /etc/ssh/sshd_config | head -5",
    "echo \"\"",
    "echo \"=== CHECK 3: sshd DNS checks ===\"",
    "grep -i UseDNS /etc/ssh/sshd_config | head -5",
    "echo \"\"",
    "echo \"=== CHECK 4: OS identity ===\"",
    "cat /etc/os-release 2>&1 | head -5 || echo FAIL: cannot read os-release",
    "echo \"\"",
    "echo \"=== CHECK 5: Running kernel ===\"",
    "uname -r",
    "echo \"\"",
    "echo \"=== CHECK 5B: Installed kernel packages ===\"",
    "rpm -q kernel 2>&1 || echo FAIL: kernel package not installed",
    "rpm -q kernel-core 2>&1 || echo INFO: kernel-core package not installed separately",
    "echo \"\"",
    "echo \"=== CHECK 6: AWS CLI v2 installed at /usr/local/bin, distro awscli-2 package absent ===\"",
    "aws --version 2>&1 || echo FAIL: aws command missing",
    "test \"$(command -v aws)\" = /usr/local/bin/aws && echo PASS: aws resolves to /usr/local/bin/aws || echo FAIL: aws resolves to $(command -v aws)",
    "rpm -q awscli-2 > /dev/null 2>&1 && echo FAIL: awscli-2 package present || echo PASS: awscli-2 package absent",
    "echo \"\"",
    "echo \"=== CHECK 7: Pre-baked tools ===\"",
    "command -v jq && jq --version",
    "command -v python3.11 && python3.11 --version",
    "command -v unzip && unzip -v | head -1",
    "test -x /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl && echo PASS: CloudWatch agent control binary present",
    "test -d /opt/aws/amazon-cloudwatch-agent/etc && echo PASS: CloudWatch agent etc directory present",
    "command -v openssl && echo PASS: openssl present",
    "command -v find && command -v hostname && echo PASS: findutils and hostname present",
    "echo \"\"",
    "echo \"=== CHECK 8: Neo4j repo and system user ===\"",
    "test -f /etc/yum.repos.d/neo4j.repo && echo PASS: Neo4j repo present",
    "rpm -qa --qf \"%{SUMMARY}\\n\" gpg-pubkey* | grep -qi neo4j && echo PASS: Neo4j GPG key imported",
    "getent passwd neo4j",
    "getent group neo4j",
    "echo \"\"",
    "echo \"=== CHECK 9: Volume helper prerequisites ===\"",
    "command -v lsblk && echo PASS: lsblk present",
    "command -v mkfs.xfs && echo PASS: mkfs.xfs present",
    "echo \"\"",
    "echo \"=== CHECK 10: No Neo4j configuration or bootstrap baked in AMI ===\"",
    "test ! -e /etc/neo4j/neo4j.conf && test ! -e /opt/neo4j/conf/neo4j-base.conf && test ! -e /opt/neo4j/bin/neo4j-bootstrap.sh && echo PASS: No Neo4j config on AMI || echo FAIL: Neo4j config present on AMI",
    "echo \"\"",
    "echo \"=== CHECK 11: cfn helpers resolve ===\"",
    "command -v cfn-init && echo PASS: cfn-init resolves || echo FAIL: cfn-init does not resolve",
    "command -v cfn-signal && echo PASS: cfn-signal resolves || echo FAIL: cfn-signal does not resolve"
  ]' \
  --query "Command.CommandId" \
  --output text)

echo "Command sent: ${COMMAND_ID}"
echo "Waiting for results..."

# Use the AWS CLI waiter — polls get-command-invocation every 5s until Success,
# exits 255 on failure or timeout. The || true ensures we always retrieve output.
aws ssm wait command-executed \
  --region "${REGION}" \
  --command-id "${COMMAND_ID}" \
  --instance-id "${INSTANCE_ID}" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 6: Retrieve and display results
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
echo "  AMI Verification Results"
echo "============================================="
echo ""

COMMAND_OUTPUT=$(aws ssm get-command-invocation \
  --region "${REGION}" \
  --command-id "${COMMAND_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --query "StandardOutputContent" \
  --output text 2>/dev/null || true)

COMMAND_ERROR=$(aws ssm get-command-invocation \
  --region "${REGION}" \
  --command-id "${COMMAND_ID}" \
  --instance-id "${INSTANCE_ID}" \
  --query "StandardErrorContent" \
  --output text 2>/dev/null || true)

echo "${COMMAND_OUTPUT}"

if [ -n "${COMMAND_ERROR}" ] && [ "${COMMAND_ERROR}" != "None" ]; then
  echo ""
  echo "--- stderr ---"
  echo "${COMMAND_ERROR}"
fi

# ---------------------------------------------------------------------------
# Step 7: Evaluate pass/fail
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
FAILURES=0

if echo "${COMMAND_OUTPUT}" | grep -q "^PasswordAuthentication no"; then
  echo "  PASS: SSH password authentication is disabled"
else
  echo "  FAIL: SSH password authentication may not be disabled"
  FAILURES=$((FAILURES + 1))
fi

if echo "${COMMAND_OUTPUT}" | grep -qE "^PermitRootLogin (prohibit-password|without-password)"; then
  echo "  PASS: Root login is restricted"
else
  echo "  FAIL: Root login may not be properly restricted"
  FAILURES=$((FAILURES + 1))
fi

if echo "${COMMAND_OUTPUT}" | grep -q "^UseDNS no"; then
  echo "  PASS: sshd DNS checks disabled"
else
  echo "  FAIL: UseDNS no not set in sshd_config"
  FAILURES=$((FAILURES + 1))
fi

if echo "${COMMAND_OUTPUT}" | grep -q 'VERSION_ID="2023"'; then
  echo "  PASS: OS is Amazon Linux 2023"
else
  echo "  FAIL: OS does not appear to be Amazon Linux 2023"
  FAILURES=$((FAILURES + 1))
fi

KERNEL_VERSION=$(echo "${COMMAND_OUTPUT}" | awk '/^=== CHECK 5: Running kernel ===/{getline; print; exit}')
KERNEL_MIN="6.1.168-203.330.amzn2023"
if [ -n "${KERNEL_VERSION}" ] && [ "$(printf '%s\n%s\n' "${KERNEL_MIN}" "${KERNEL_VERSION}" | sort -V | head -1)" = "${KERNEL_MIN}" ]; then
  echo "  PASS: Running kernel is ${KERNEL_VERSION}"
else
  echo "  FAIL: Running kernel '${KERNEL_VERSION}' is older than ${KERNEL_MIN}"
  FAILURES=$((FAILURES + 1))
fi

if echo "${COMMAND_OUTPUT}" | grep -q "aws-cli/2"; then
  echo "  PASS: AWS CLI v2 is installed"
else
  echo "  FAIL: AWS CLI v2 is missing"
  FAILURES=$((FAILURES + 1))
fi

if echo "${COMMAND_OUTPUT}" | grep -q "PASS: aws resolves to /usr/local/bin/aws"; then
  echo "  PASS: aws resolves to the AWS CLI v2 installer path"
else
  echo "  FAIL: aws does not resolve to /usr/local/bin/aws"
  FAILURES=$((FAILURES + 1))
fi

if echo "${COMMAND_OUTPUT}" | grep -q "PASS: awscli-2 package absent"; then
  echo "  PASS: distro awscli-2 package is absent"
else
  echo "  FAIL: distro awscli-2 package may still be present"
  FAILURES=$((FAILURES + 1))
fi

for expected in \
  "PASS: CloudWatch agent control binary present" \
  "PASS: CloudWatch agent etc directory present" \
  "PASS: openssl present" \
  "PASS: findutils and hostname present" \
  "PASS: Neo4j repo present" \
  "PASS: Neo4j GPG key imported" \
  "neo4j:x:500:500" \
  "neo4j:x:500:" \
  "PASS: lsblk present" \
  "PASS: mkfs.xfs present"; do
  if echo "${COMMAND_OUTPUT}" | grep -q "${expected}"; then
    echo "  PASS: ${expected#PASS: }"
  else
    echo "  FAIL: Missing expected output '${expected}'"
    FAILURES=$((FAILURES + 1))
  fi
done

if echo "${COMMAND_OUTPUT}" | grep -q "PASS: No Neo4j config on AMI"; then
  echo "  PASS: No Neo4j configuration or bootstrap is baked into the AMI"
else
  echo "  FAIL: Neo4j config or bootstrap found on the AMI (NFR-3/AD-3 violation)"
  FAILURES=$((FAILURES + 1))
fi

for helper in cfn-init cfn-signal; do
  if echo "${COMMAND_OUTPUT}" | grep -q "PASS: ${helper} resolves"; then
    echo "  PASS: ${helper} resolves on the AMI"
  else
    echo "  FAIL: ${helper} does not resolve on the AMI (NFR-10 violation)"
    FAILURES=$((FAILURES + 1))
  fi
done

for tool in jq python3.11 unzip; do
  if echo "${COMMAND_OUTPUT}" | grep -q "/${tool}"; then
    echo "  PASS: ${tool} is installed"
  else
    echo "  FAIL: ${tool} is missing"
    FAILURES=$((FAILURES + 1))
  fi
done

echo "============================================="
echo ""

if [ "${FAILURES}" -gt 0 ]; then
  echo "  RESULT: ${FAILURES} check(s) FAILED"
  echo ""
  echo "  Review the output above and fix the AMI build."
  exit 1
else
  echo "  RESULT: All checks PASSED"
  echo ""
  echo "  AMI ${AMI_ID} is ready for Marketplace scanning."
  echo "  Next: Submit via Marketplace Portal > Test 'Add version'"
fi
echo "============================================="
# Instance is terminated by the cleanup trap
