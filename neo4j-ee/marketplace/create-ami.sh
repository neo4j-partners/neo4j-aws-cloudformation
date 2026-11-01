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
#   AWS_PROFILE=marketplace NEO4J_VERSION=2026.10.1 ./create-ami.sh   # marketplace mode
#   ./create-ami.sh                                                     # iteration mode
#
# The AMI carries OS patches, SSH hardening, static deployment tooling, and
# Neo4j Enterprise plus cypher-shell at one pinned version (AD-1). Neo4j is
# installed but never enabled or started here, and the AMI carries no Neo4j
# configuration this project authors. The template applies configuration and
# starts the service at boot.
#
# NEO4J_VERSION is required in marketplace mode. In iteration mode it defaults
# to the newest neo4j-enterprise version in yum.neo4j.com/stable/latest.

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
# Resolve the Neo4j version to bake
# ---------------------------------------------------------------------------
# Print the newest neo4j-enterprise version the stable/latest repo serves.
resolve_latest_neo4j_version() {
  local repo="https://yum.neo4j.com/stable/latest"
  local primary
  primary=$(curl -fsSL "${repo}/repodata/repomd.xml" \
    | grep -o 'href="repodata/[^"]*-primary\.xml\.gz"' | cut -d'"' -f2)
  [ -n "${primary}" ] || return 1
  curl -fsSL "${repo}/${primary}" | gunzip | python3 -I -c '
import re, sys
xml = sys.stdin.read()
versions = re.findall(
    r"<name>neo4j-enterprise</name>.*?<version epoch=\"\d+\" ver=\"([^\"]+)\"",
    xml,
    re.S,
)
print(max(versions, key=lambda v: tuple(int(p) for p in v.split("."))))
'
}

if [ -z "${NEO4J_VERSION:-}" ]; then
  if [ "${AMI_BUILD_MODE}" = "marketplace" ]; then
    echo "ERROR: NEO4J_VERSION is required in marketplace mode, for example NEO4J_VERSION=2026.10.1."
    exit 1
  fi
  echo "Resolving the newest Neo4j version in yum.neo4j.com/stable/latest..."
  NEO4J_VERSION=$(resolve_latest_neo4j_version) || {
    echo "ERROR: Could not resolve the newest Neo4j version. Set NEO4J_VERSION."
    exit 1
  }
fi

if ! [[ "${NEO4J_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "ERROR: NEO4J_VERSION '${NEO4J_VERSION}' is not a version such as 2026.10.1."
  exit 1
fi
echo "Neo4j version: ${NEO4J_VERSION}"
echo ""

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

NEO4J_VERSION="__NEO4J_VERSION__"

echo "=== Neo4j EE AMI Build (via UserData) ==="

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
# gnupg2-minimal has no gpg-agent, and the gpg --import below needs one.
dnf swap -y gnupg2-minimal gnupg2-full

# The AWS CLI zip is verified against the AWS CLI team's PGP key before it is
# unpacked. The key block is copied from the AWS CLI install guide, which also
# publishes the fingerprint checked below. The guide republishes the key when
# AWS extends its expiry; replace the block here when that happens.
ARCH="x86_64"
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip" -o /tmp/awscliv2.zip
curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${ARCH}.zip.sig" -o /tmp/awscliv2.sig
AWSCLI_KEY_FINGERPRINT="FB5DB77FD5C118B80511ADA8A6310ACC4672475C"
export GNUPGHOME=$(mktemp -d)
gpg --batch --quiet --import <<'AWSCLIKEY'
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBF2Cr7UBEADJZHcgusOJl7ENSyumXh85z0TRV0xJorM2B/JL0kHOyigQluUG
ZMLhENaG0bYatdrKP+3H91lvK050pXwnO/R7fB/FSTouki4ciIx5OuLlnJZIxSzx
PqGl0mkxImLNbGWoi6Lto0LYxqHN2iQtzlwTVmq9733zd3XfcXrZ3+LblHAgEt5G
TfNxEKJ8soPLyWmwDH6HWCnjZ/aIQRBTIQ05uVeEoYxSh6wOai7ss/KveoSNBbYz
gbdzoqI2Y8cgH2nbfgp3DSasaLZEdCSsIsK1u05CinE7k2qZ7KgKAUIcT/cR/grk
C6VwsnDU0OUCideXcQ8WeHutqvgZH1JgKDbznoIzeQHJD238GEu+eKhRHcz8/jeG
94zkcgJOz3KbZGYMiTh277Fvj9zzvZsbMBCedV1BTg3TqgvdX4bdkhf5cH+7NtWO
lrFj6UwAsGukBTAOxC0l/dnSmZhJ7Z1KmEWilro/gOrjtOxqRQutlIqG22TaqoPG
fYVN+en3Zwbt97kcgZDwqbuykNt64oZWc4XKCa3mprEGC3IbJTBFqglXmZ7l9ywG
EEUJYOlb2XrSuPWml39beWdKM8kzr1OjnlOm6+lpTRCBfo0wa9F8YZRhHPAkwKkX
XDeOGpWRj4ohOx0d2GWkyV5xyN14p2tQOCdOODmz80yUTgRpPVQUtOEhXQARAQAB
tCFBV1MgQ0xJIFRlYW0gPGF3cy1jbGlAYW1hem9uLmNvbT6JAlQEEwEIAD4CGwMF
CwkIBwIGFQoJCAsCBBYCAwECHgECF4AWIQT7Xbd/1cEYuAURraimMQrMRnJHXAUC
akV0ygUJDqP4lQAKCRCmMQrMRnJHXFHjD/9eyZLYcKuQOlLvtqSDtUBiEZf6ZZjM
i3ygYH8rJNtuToUH+HvSpe819urJCquXhDrlK6N+aqW0hCLtNABJG/vsafIgvIYJ
hSGgpgtNnQyMV1jViRWqPjbouw8OkYKBThUfT1i2Y+wn58ifs6ODBCmTexWtXspA
Si+Gt49xDOW0APmbOPnI+a4HJW6tVEo6MWS0WjzpiBayR3d1A4pt4YrPfSdDgpLo
h2SLQqlRqvvVZJaWBjhkErNFpfsBA06sDcPEOb0G8LBUbR4WOcdvhe5LubJbZuxC
AG9kNPCVeQP1ixwjgjXKysaxeQ6rv0VzIQgRp6tLVLWhy6AKDNvLjFSsmXZ1Wl08
Y/RlOHXlzLuQMRE6sR1wOdRxc9TsrNWTGiBK65cvSWOy03JeBkQQ8pesqltiyxI9
U21kkgiXtTSKNGfKK8pO27D81YANhRqPK7iTp6kuFiY2WtOg90KTMNlIT+Ff85Y2
b1rHj6Z0SrCkJujhWk3IBPic/wJgz01LEc/OAdUPlby90RJZcIBhSlWhT7mXnXIO
c0HWlNQrns2s3CTyYwZSiSlYe9ApeLwhjDo8NhbFuCAy61l6O5UsR4AfZxx/rGKv
2wFb1/RN/P4gNe6vmxZAPjR0AQcwD3tc2McimOLr/22kmPz8IH3I0X7WoSFr0Biz
E91G7bb0hOb/cA==
=knv7
-----END PGP PUBLIC KEY BLOCK-----
AWSCLIKEY
SIGNER=$(gpg --batch --status-fd 1 --verify /tmp/awscliv2.sig /tmp/awscliv2.zip 2>/dev/null \
  | awk '$2 == "VALIDSIG" {print $NF}')
if [ "${SIGNER}" != "${AWSCLI_KEY_FINGERPRINT}" ]; then
  echo "ERROR: AWS CLI zip signature did not verify against ${AWSCLI_KEY_FINGERPRINT}."
  exit 1
fi
rm -rf "${GNUPGHOME}" /tmp/awscliv2.sig
unset GNUPGHOME
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

# Neo4j and cypher-shell are baked at one pinned version (AD-1). The neo4j
# user above already exists, so the RPM reuses uid and gid 500. Java comes
# from the AL2023 repositories as an RPM dependency.
echo "Installing Neo4j Enterprise ${NEO4J_VERSION}..."
NEO4J_ACCEPT_LICENSE_AGREEMENT=yes dnf install -y \
  "neo4j-enterprise-${NEO4J_VERSION}" "cypher-shell-${NEO4J_VERSION}"

# The repository stays configured but disabled. Nodes never reach
# yum.neo4j.com, and an OS update on a node cannot move the Neo4j version.
sed -i 's/^enabled=1$/enabled=0/' /etc/yum.repos.d/neo4j.repo

# Neo4j must never start in the AMI. A first start creates the system
# database, and the boot-time set-initial-password would then fail. The RPM
# creates /var/lib/neo4j/data/databases empty, and a first start populates it.
systemctl disable neo4j 2>/dev/null || true
if systemctl is-active --quiet neo4j || [ -n "$(ls -A /var/lib/neo4j/data/databases 2>/dev/null)" ]; then
  echo "ERROR: Neo4j started during the AMI build."
  exit 1
fi

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

# The build script is a quoted heredoc, so the pinned version is substituted in.
USERDATA="${USERDATA//__NEO4J_VERSION__/${NEO4J_VERSION}}"
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

echo "Instance running. Build in progress (typically 5-8 minutes)..."
echo "Waiting for instance to stop (build shuts down when done)..."
echo ""

# The build script only reaches shutdown when every step succeeded, because of
# set -e. A failed step leaves the instance running, so a timeout here means the
# bake failed. The builder has no IAM profile, so it cannot report why.
BUILD_STOPPED=false
for _ in $(seq 1 80); do
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
  echo "ERROR: Build instance ${INSTANCE_ID} did not stop within 20 minutes."
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
  --description "Neo4j Enterprise ${NEO4J_VERSION} on Amazon Linux 2023 with OS patches, AWS CLI v2, CloudWatch agent, jq, Python 3.11 and cypher-shell; configured and started at deploy time" \
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
    Key=Neo4jEdition,Value="enterprise" \
    Key=Neo4jVersion,Value="${NEO4J_VERSION}" \
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
echo "  Neo4j:         ${NEO4J_VERSION}"
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
