#!/bin/bash
# snapshot.sh — Create or list EBS snapshots for a deployed Neo4j EE stack
#
# Reads .deploy/<stack-name>.txt to get volume IDs and region.
# Snapshots are stored in AWS-managed S3 (not in your S3 bucket).
#
# Usage:
#   ./snapshot.sh [--list] [stack-name]
#
# If stack-name is omitted, uses the most recently modified file in .deploy/.
# --list: show all existing snapshots for the stack instead of creating new ones.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-default}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="${SCRIPT_DIR}/.deploy"

# ---------------------------------------------------------------------------
# Parse arguments: [--list] [stack-name]
# ---------------------------------------------------------------------------
LIST=false
STACK_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)
      LIST=true
      shift
      ;;
    -*)
      echo "ERROR: Unknown option '$1'." >&2
      echo "Usage: $0 [--list] [stack-name]" >&2
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
# Resolve the outputs file
# ---------------------------------------------------------------------------
if [ -n "${STACK_ARG}" ]; then
  OUTPUTS_FILE="${DEPLOY_DIR}/${STACK_ARG}.txt"
elif [ -d "${DEPLOY_DIR}" ]; then
  OUTPUTS_FILE=$(ls -t "${DEPLOY_DIR}"/*.txt 2>/dev/null | head -1 || true)
else
  OUTPUTS_FILE=""
fi

if [ -z "${OUTPUTS_FILE}" ] || [ ! -f "${OUTPUTS_FILE}" ]; then
  echo "ERROR: No deployment found." >&2
  if [ -n "${STACK_ARG}" ]; then
    echo "File not found: ${DEPLOY_DIR}/${STACK_ARG}.txt" >&2
  else
    echo "No .txt files in ${DEPLOY_DIR}/" >&2
  fi
  echo "Usage: $0 [stack-name]" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Helper: read a value from a "Key = Value" file
# ---------------------------------------------------------------------------
read_field() {
  grep "^${1}" "$OUTPUTS_FILE" | sed 's/^[^=]*= *//' | tr -d '\r'
}

STACK_NAME=$(read_field "StackName")
REGION=$(read_field "Region")
VOLUME_IDS=$(read_field "Neo4jDataVolumeIds")

echo "Stack:   ${STACK_NAME}"
echo "Region:  ${REGION}"
echo "Volumes: ${VOLUME_IDS}"
echo ""

# ---------------------------------------------------------------------------
# --list: show existing snapshots and exit
# ---------------------------------------------------------------------------
if [ "${LIST}" = true ]; then
  aws ec2 describe-snapshots \
    --filters "Name=tag:stack,Values=${STACK_NAME}" \
    --region "${REGION}" \
    --query 'Snapshots[*].[SnapshotId,StartTime,State,Progress,VolumeSize,Description]' \
    --output table
  exit 0
fi

# ---------------------------------------------------------------------------
# Snapshot each volume (comma-separated in the outputs file)
# ---------------------------------------------------------------------------
DATE=$(date -u +%Y-%m-%d)
IFS=',' read -ra VOLS <<< "${VOLUME_IDS}"
for VOL in "${VOLS[@]}"; do
  VOL="${VOL// /}"  # strip whitespace
  echo -n "Snapshotting ${VOL} ... "
  SNAP_ID=$(aws ec2 create-snapshot \
    --volume-id "${VOL}" \
    --description "${STACK_NAME} ${DATE}" \
    --tag-specifications "ResourceType=snapshot,Tags=[{Key=stack,Value=${STACK_NAME}},{Key=date,Value=${DATE}}]" \
    --region "${REGION}" \
    --query 'SnapshotId' \
    --output text)
  echo "${SNAP_ID}"
done

echo ""
echo "Snapshots initiated. Monitor progress:"
echo "  ./snapshot.sh --list ${STACK_NAME}"
