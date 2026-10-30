#!/bin/bash
# create-ami.sh — Automate the EE Marketplace AMI build
#
# Launches an Amazon Linux 2023 instance, runs the build steps via UserData,
# waits for the instance to stop, creates an AMI with IMDSv2 enforced, and
# cleans up the build instance.
#
# Prerequisites:
#   - AWS CLI configured with the marketplace profile (neo4j-marketplace account)
#   - A key pair is NOT required — there is no SSH step
#
# Usage:
#   AWS_PROFILE=marketplace ./create-ami.sh   # Marketplace account: marketplace mode
#   ./create-ami.sh                            # any other account: iteration mode
#
# The AMI is a base OS image with SSH hardening, OS patches, and static
# deployment tooling pre-baked.
# Neo4j Enterprise is installed at deploy time via the CloudFormation UserData
# script, not baked into the AMI.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
REGION="us-east-1"
# Set AMI_NAME_SUFFIX to build a second AMI on the same day while the first is
# still referenced by a launch template.
AMI_NAME="neo4j-ee-base-$(date +%Y%m%d)${AMI_NAME_SUFFIX:+-${AMI_NAME_SUFFIX}}"
INSTANCE_TYPE="t3.medium"
MARKETPLACE_ACCOUNT_ID="385155106615"

# AMI_BUILD_MODE defaults from the caller's account: 'marketplace' only in the
# Marketplace account, 'iteration' everywhere else. The single hard guard is
# that marketplace mode must run in the Marketplace account; iteration builds
# just run. Mode and tags are resolved below, once the account is known.

# ---------------------------------------------------------------------------
# Preflight: verify we're in the correct AWS account
# ---------------------------------------------------------------------------
echo "=== Neo4j EE AMI Builder ==="
echo ""
echo "Verifying AWS identity..."

CALLER_IDENTITY=$(aws sts get-caller-identity --output json 2>&1) || {
  echo "ERROR: Failed to call sts get-caller-identity."
  echo "Make sure you are authenticated."
  exit 1
}

ACCOUNT_ID=$(echo "${CALLER_IDENTITY}" | grep -o '"Account": "[^"]*"' | cut -d'"' -f4)
CALLER_ARN=$(echo "${CALLER_IDENTITY}" | grep -o '"Arn": "[^"]*"' | cut -d'"' -f4)

if [ "${ACCOUNT_ID}" = "${MARKETPLACE_ACCOUNT_ID}" ]; then
  AMI_BUILD_MODE="${AMI_BUILD_MODE:-marketplace}"
else
  AMI_BUILD_MODE="${AMI_BUILD_MODE:-iteration}"
fi

case "${AMI_BUILD_MODE}" in
  marketplace|iteration)
    ;;
  *)
    echo "ERROR: AMI_BUILD_MODE must be 'marketplace' or 'iteration'."
    exit 1
    ;;
esac

BUILD_PURPOSE="marketplace-ami-build"
if [ "${AMI_BUILD_MODE}" = "iteration" ]; then
  BUILD_PURPOSE="default-account-ami-iteration"
fi

TAGS="ResourceType=instance,Tags=[{Key=Name,Value=neo4j-ee-ami-build},{Key=Purpose,Value=${BUILD_PURPOSE}}]"

echo "  Account:  ${ACCOUNT_ID}"
echo "  Identity: ${CALLER_ARN}"
echo "  Profile:  ${AWS_PROFILE:-default credential chain}"
echo "  Region:   ${REGION} (overrides profile default)"
echo "  Mode:     ${AMI_BUILD_MODE}"
echo ""
echo "AMI name:      ${AMI_NAME}"
echo ""

if [ "${AMI_BUILD_MODE}" = "marketplace" ] && [ "${ACCOUNT_ID}" != "${MARKETPLACE_ACCOUNT_ID}" ]; then
  echo "ERROR: Marketplace mode must run in account ${MARKETPLACE_ACCOUNT_ID}."
  echo "Omit AMI_BUILD_MODE to build an iteration AMI in this account instead."
  exit 1
fi

if [ "${AMI_BUILD_MODE}" = "iteration" ]; then
  echo "Iteration mode: writing the resulting AMI ID to marketplace/ami-id.txt."
  echo ""
fi

# ---------------------------------------------------------------------------
# Step 1: Deregister any existing AMI with the same name
# ---------------------------------------------------------------------------
EXISTING_AMI=$(aws ec2 describe-images \
  --region "${REGION}" \
  --owners self \
  --filters "Name=name,Values=${AMI_NAME}" \
  --query "Images[0].ImageId" \
  --output text 2>/dev/null || true)

if [ -n "${EXISTING_AMI}" ] && [ "${EXISTING_AMI}" != "None" ]; then
  echo "Found existing AMI with name '${AMI_NAME}': ${EXISTING_AMI}"

  # Refuse to deregister the AMI if any launch template in this account still
  # references it. Deregistering an in-use AMI orphans the launch template:
  # ASGs cannot replace failed instances and resilience tests time out.
  IN_USE_LT=$(aws ec2 describe-launch-template-versions \
    --region "${REGION}" \
    --filters "Name=image-id,Values=${EXISTING_AMI}" \
    --query "LaunchTemplateVersions[].[LaunchTemplateId,VersionNumber]" \
    --output text 2>/dev/null || true)

  if [ -n "${IN_USE_LT}" ] && [ "${IN_USE_LT}" != "None" ]; then
    echo "ERROR: AMI ${EXISTING_AMI} is still referenced by one or more launch templates:"
    echo "${IN_USE_LT}"
    echo ""
    echo "Deregistering it would orphan the stacks using those launch templates."
    echo "Tear down those stacks first, or build a new AMI name and update the deploy."
    exit 1
  fi

  SNAP_IDS=$(aws ec2 describe-images \
    --region "${REGION}" \
    --image-ids "${EXISTING_AMI}" \
    --query "Images[0].BlockDeviceMappings[].Ebs.SnapshotId" \
    --output text 2>/dev/null || true)

  echo "Deregistering ${EXISTING_AMI}..."
  aws ec2 deregister-image \
    --region "${REGION}" \
    --image-id "${EXISTING_AMI}"

  for snap in ${SNAP_IDS}; do
    if [ -n "${snap}" ] && [ "${snap}" != "None" ]; then
      echo "Deleting snapshot ${snap}..."
      aws ec2 delete-snapshot --region "${REGION}" --snapshot-id "${snap}" 2>/dev/null || true
    fi
  done

  echo "Old AMI cleaned up."
else
  echo "No existing AMI named '${AMI_NAME}' found. Proceeding."
fi
echo ""

# ---------------------------------------------------------------------------
# Step 2: Resolve the latest Amazon Linux 2023 AMI
# ---------------------------------------------------------------------------
echo "Resolving latest Amazon Linux 2023 AMI..."
BASE_AMI=$(aws ssm get-parameters \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --region "${REGION}" \
  --query "Parameters[0].Value" \
  --output text)

if [ -z "${BASE_AMI}" ] || [ "${BASE_AMI}" == "None" ]; then
  echo "ERROR: Could not resolve Amazon Linux 2023 AMI. Check SSM parameter store access."
  exit 1
fi
echo "Base AMI: ${BASE_AMI}"

# ---------------------------------------------------------------------------
# Step 3: Create UserData that runs the build steps inline
# ---------------------------------------------------------------------------
echo "Preparing UserData..."

USERDATA=$(cat <<'BUILDSCRIPT'
#!/bin/bash
set -euo pipefail

echo "=== Neo4j EE Base AMI Build (via UserData) ==="

# --- Patch the OS ---
echo "Patching OS..."
dnf update -y

# --- Static deployment tooling ---
# aws-cfn-bootstrap (cfn-init, cfn-signal) is installed explicitly, not assumed
# from the AL2023 base, so a base-image change that drops or relocates the
# helpers fails test-ami.sh at build time rather than a customer launch (NFR-10).
# openssl is baked here, not dnf-installed at boot: it is OS-level, immutable,
# and Neo4j-independent (Placement Decision Rule branch 3). configure-tls.sh
# uses it to generate the per-instance self-signed cert, so a missing openssl
# must fail at AMI build, not on the ASG self-heal path with no template fix.
echo "Installing static deployment tooling..."
dnf remove -y awscli-2 2>/dev/null || true
dnf install -y unzip python3.11 jq openssl amazon-cloudwatch-agent aws-cfn-bootstrap

ARCH="x86_64"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o /tmp/awscliv2.zip
unzip -q /tmp/awscliv2.zip -d /tmp/
/tmp/aws/install --bin-dir /usr/local/bin --install-dir /usr/local/aws-cli --update
rm -rf /tmp/awscliv2.zip /tmp/aws

echo "Configuring Neo4j yum repository..."
rpm --import https://debian.neo4j.com/neotechnology.gpg.key
cat > /etc/yum.repos.d/neo4j.repo <<'NEO4JREPO'
[neo4j]
name=Neo4j RPM Repository
baseurl=https://yum.neo4j.com/stable/latest
enabled=1
gpgcheck=1
NEO4JREPO

echo "Creating pinned neo4j system user..."
getent group neo4j >/dev/null || groupadd -g 500 neo4j
getent passwd neo4j >/dev/null || useradd -u 500 -g 500 -r -s /sbin/nologin -d /var/lib/neo4j neo4j

# --- SSH Hardening ---
sed -i 's/^#PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i '/UseDNS/d' /etc/ssh/sshd_config
echo "UseDNS no" >> /etc/ssh/sshd_config
passwd -l root
shred -u /etc/ssh/*_key /etc/ssh/*_key.pub
rm -f /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys

# --- Clean up ---
dnf clean all
cloud-init clean --logs
rm -rf /var/cache/dnf /tmp/* /var/tmp/*
shred -u /root/.bash_history 2>/dev/null || rm -f /root/.bash_history

# --- Signal completion and shut down ---
echo "=== Build complete. Shutting down. ==="
shutdown -h now
BUILDSCRIPT
)

USERDATA_B64=$(echo "${USERDATA}" | base64)

# ---------------------------------------------------------------------------
# Step 4: Launch the build instance
# ---------------------------------------------------------------------------
echo "Launching build instance..."
INSTANCE_ID=$(aws ec2 run-instances \
  --region "${REGION}" \
  --image-id "${BASE_AMI}" \
  --instance-type "${INSTANCE_TYPE}" \
  --user-data "${USERDATA_B64}" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
  --tag-specifications "${TAGS}" \
  --block-device-mappings "DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true}" \
  --query "Instances[0].InstanceId" \
  --output text)

if [ -z "${INSTANCE_ID}" ]; then
  echo "ERROR: Failed to launch instance."
  exit 1
fi
echo "Instance launched: ${INSTANCE_ID}"

# ---------------------------------------------------------------------------
# Step 5: Wait for build to finish (instance stops itself)
# ---------------------------------------------------------------------------
echo ""
echo "Waiting for instance to start..."
aws ec2 wait instance-running \
  --region "${REGION}" \
  --instance-ids "${INSTANCE_ID}"

echo "Instance running. Build in progress (typically 3-5 minutes)..."
echo "Waiting for instance to stop (build shuts down when done)..."
echo ""

# The build script only reaches shutdown when every step succeeded, because of
# set -e. A failed step leaves the instance running, so a timeout here means the
# bake failed. The builder has no IAM profile, so it cannot report why.
BUILD_STOPPED=false
for _ in $(seq 1 48); do
  STATE=$(aws ec2 describe-instances \
    --region "${REGION}" \
    --instance-ids "${INSTANCE_ID}" \
    --query "Reservations[0].Instances[0].State.Name" \
    --output text)
  if [ "${STATE}" = "stopped" ]; then
    BUILD_STOPPED=true
    break
  fi
  echo "  build instance is ${STATE}..."
  sleep 15
done

if [ "${BUILD_STOPPED}" != "true" ]; then
  echo "ERROR: Build instance ${INSTANCE_ID} did not stop within 12 minutes."
  echo "A build step in the UserData script most likely failed."
  echo "Terminating the build instance. No AMI was created."
  aws ec2 terminate-instances \
    --region "${REGION}" \
    --instance-ids "${INSTANCE_ID}" > /dev/null
  exit 1
fi

echo "Instance stopped. Build complete."

# ---------------------------------------------------------------------------
# Step 6: Create AMI from the stopped instance
# ---------------------------------------------------------------------------
echo "Creating AMI: ${AMI_NAME}..."
IMAGE_ID=$(aws ec2 create-image \
  --region "${REGION}" \
  --instance-id "${INSTANCE_ID}" \
  --name "${AMI_NAME}" \
  --description "Neo4j EE base image on Amazon Linux 2023 with OS patches, AWS CLI v2, CloudWatch agent, jq, Python 3.11, and Neo4j repo/user pre-baked; Neo4j installed at deploy time" \
  --query "ImageId" \
  --output text)

echo "AMI creation initiated: ${IMAGE_ID}"
echo "Waiting for AMI to become available..."

aws ec2 wait image-available \
  --region "${REGION}" \
  --image-ids "${IMAGE_ID}"

echo "AMI available: ${IMAGE_ID}"

# Enforce IMDSv2 on the AMI itself — required by AWS Marketplace policy.
# create-image does not accept --imds-support, so we set it post-creation.
echo "Enforcing IMDSv2 on AMI..."
aws ec2 modify-image-attribute \
  --region "${REGION}" \
  --image-id "${IMAGE_ID}" \
  --imds-support v2.0

# ---------------------------------------------------------------------------
# Step 7: Tag the AMI
# ---------------------------------------------------------------------------
echo "Tagging AMI..."
aws ec2 create-tags \
  --region "${REGION}" \
  --resources "${IMAGE_ID}" \
  --tags \
    Key=Name,Value="${AMI_NAME}" \
    Key=Neo4jEdition,Value="enterprise-base" \
    Key=BaseOS,Value="Amazon Linux 2023" \
    Key=IMDSv2,Value="v2.0-required" \
    Key=Purpose,Value="${BUILD_PURPOSE}" \
    Key=AmiBuildMode,Value="${AMI_BUILD_MODE}"

# ---------------------------------------------------------------------------
# Step 8: Save AMI ID for downstream scripts
# ---------------------------------------------------------------------------
AMI_ID_FILE="$(dirname "$0")/ami-id.txt"
echo "${IMAGE_ID}" > "${AMI_ID_FILE}"
echo "AMI ID written to ${AMI_ID_FILE}"

# ---------------------------------------------------------------------------
# Step 9: Terminate the build instance
# ---------------------------------------------------------------------------
echo "Terminating build instance ${INSTANCE_ID}..."
aws ec2 terminate-instances \
  --region "${REGION}" \
  --instance-ids "${INSTANCE_ID}" > /dev/null

echo ""
echo "============================================="
echo "  AMI Build Complete"
echo "============================================="
echo ""
echo "  AMI ID:        ${IMAGE_ID}"
echo "  AMI Name:      ${AMI_NAME}"
echo "  Region:        ${REGION}"
echo ""
echo "Next steps:"
if [ "${AMI_BUILD_MODE}" = "marketplace" ]; then
  echo "  1. Test the AMI:"
  echo "       AWS_PROFILE=marketplace ./marketplace/test-ami.sh"
  echo ""
  echo "  2. Run Marketplace AMI scan:"
  echo "       Marketplace Portal > Products > Server > Request changes"
  echo "       > Update versions > Test 'Add version'"
  echo ""
  echo "  3. Update the CFT ImageId parameter default with the product code"
  echo "       once the Marketplace listing is published."
else
  echo "  1. Inspect the AMI in the default account:"
  echo "       AWS_PROFILE=default ./marketplace/test-ami.sh"
  echo ""
  echo "  2. Deploy default-account stacks against this AMI before Marketplace testing."
fi
echo "============================================="
