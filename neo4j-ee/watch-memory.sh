#!/usr/bin/env bash
set -euo pipefail

STACK=ee-memorystar
REGION=us-east-2
ASG_NAME=ee-memorystar-Neo4jNode1ASG-lW2xPuywOkin
INTERVAL=${1:-30}

echo "Watching memory for stack: $STACK ($REGION)"
echo "ASG: $ASG_NAME"
echo "Polling every ${INTERVAL}s — Ctrl-C to stop"
echo ""

get_instance_id() {
  rtk proxy aws autoscaling describe-auto-scaling-groups \
    --auto-scaling-group-names "$ASG_NAME" \
    --region "$REGION" \
    --query 'AutoScalingGroups[0].Instances[?HealthStatus==`Healthy`].InstanceId' \
    --output text | awk '{print $1}'
}

get_mem() {
  local instance_id=$1
  local end
  local start
  end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  start=$(date -u -v-5M +%Y-%m-%dT%H:%M:%SZ)

  rtk proxy aws cloudwatch get-metric-statistics \
    --namespace CWAgent \
    --metric-name mem_used_percent \
    --dimensions "Name=InstanceId,Value=${instance_id}" \
    --start-time "$start" \
    --end-time "$end" \
    --period 60 \
    --statistics Average \
    --region "$REGION" \
    --query 'sort_by(Datapoints, &Timestamp)[-1].Average' \
    --output text
}

while true; do
  TS=$(date '+%H:%M:%S')

  INSTANCE_ID=$(get_instance_id)
  if [[ -z "$INSTANCE_ID" || "$INSTANCE_ID" == "None" ]]; then
    echo "[$TS] No healthy instance found in ASG"
    sleep "$INTERVAL"
    continue
  fi

  MEM=$(get_mem "$INSTANCE_ID")
  if [[ -z "$MEM" || "$MEM" == "None" ]]; then
    echo "[$TS] $INSTANCE_ID — no CWAgent data (agent may not be running)"
  else
    printf "[%s] %s  mem_used_percent = %.1f%%\n" "$TS" "$INSTANCE_ID" "$MEM"
  fi

  sleep "$INTERVAL"
done
