#!/usr/bin/env bash
# Recover a failed Neo4j EE deployment.
# Usage: ./recover.sh [stack-name] [region]
# With no arguments, uses the most recent .deploy/*.txt file.
# Falls back to querying AWS directly when no deploy artifact exists.
set -euo pipefail

# .deploy may not exist on a fresh checkout, so resolve the script directory
# and append .deploy without changing into it.
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)/.deploy"

STACK=""
REGION=""
ASG=""

if [[ $# -ge 1 ]]; then
  DEPLOY_FILE="$DEPLOY_DIR/$1.txt"
  STACK="$1"
else
  DEPLOY_FILE=$(ls -t "$DEPLOY_DIR"/*.txt 2>/dev/null | head -1 || true)
fi

if [[ -n "${DEPLOY_FILE:-}" && -f "$DEPLOY_FILE" ]]; then
  STACK=$(awk '/^StackName/          {print $3}' "$DEPLOY_FILE")
  REGION=$(awk '/^Region/            {print $3}' "$DEPLOY_FILE")
  ASG=$(awk    '/^Neo4jNode1ASGName/ {print $3}' "$DEPLOY_FILE")
else
  echo "No deploy artifact found — querying AWS directly."
  if [[ -z "$STACK" ]]; then
    echo "ERROR: pass a stack name: ./recover.sh <stack-name> [region]"
    exit 1
  fi
  REGION="${2:-us-east-2}"
  ASG=$(aws cloudformation describe-stack-resources \
    --stack-name "$STACK" \
    --region "$REGION" \
    --query "StackResources[?LogicalResourceId=='Neo4jNode1ASG'].PhysicalResourceId" \
    --output text 2>/dev/null || true)
  if [[ -z "$ASG" || "$ASG" == "None" ]]; then
    echo "ERROR: could not find Neo4jNode1ASG in stack $STACK ($REGION)"
    exit 1
  fi
fi

echo "Stack:  $STACK"
echo "Region: $REGION"
echo "ASG:    $ASG"

INSTANCE_ID=$(aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names "$ASG" \
  --region "$REGION" \
  --query 'AutoScalingGroups[0].Instances[0].InstanceId' \
  --output text)

if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
  echo "ERROR: no instance found in $ASG"
  exit 1
fi

echo "Instance: $INSTANCE_ID"
echo ""

# ── cloud-init log ────────────────────────────────────────────────────────────
echo "=== cloud-init-output.log (last 60 lines) ==="
CMD_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["tail -60 /var/log/cloud-init-output.log"]' \
  --output text \
  --query 'Command.CommandId')
sleep 5
aws ssm get-command-invocation \
  --region "$REGION" \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query 'StandardOutputContent' \
  --output text

# ── neo4j status ─────────────────────────────────────────────────────────────
echo ""
echo "=== neo4j service status ==="
CMD_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl status neo4j --no-pager || true"]' \
  --query 'Command.CommandId' --output text)
sleep 5
STATUS=$(aws ssm get-command-invocation \
  --region "$REGION" \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query 'StandardOutputContent' \
  --output text)
echo "$STATUS"

if echo "$STATUS" | grep -q "active (running)"; then
  echo ""
  echo "Neo4j is already running — no action needed."
  exit 0
fi

# ── attempt restart ───────────────────────────────────────────────────────────
echo ""
echo "Neo4j is not running. Attempting restart..."
CMD_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name AWS-RunShellScript \
  --parameters 'commands=[
    "systemctl start neo4j",
    "sleep 15",
    "systemctl status neo4j --no-pager"
  ]' \
  --query 'Command.CommandId' --output text)
sleep 20
aws ssm get-command-invocation \
  --region "$REGION" \
  --command-id "$CMD_ID" \
  --instance-id "$INSTANCE_ID" \
  --query '[Status,StandardOutputContent,StandardErrorContent]' \
  --output text
