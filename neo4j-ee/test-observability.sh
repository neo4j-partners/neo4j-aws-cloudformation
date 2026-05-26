#!/bin/bash
# test-observability.sh — Automated Phase 1 observability checks for Neo4j EE
#
# Reads .deploy/<stack-name>.txt written by deploy.py and verifies each
# Phase 1 observability component is working. Prints [PASS] or [FAIL] for
# each automated check and a [MANUAL REQUIRED] reminder for SNS email delivery.
#
# Usage:
#   ./test-observability.sh [stack-name] [--step <name>]
#
# Steps: cloudwatch, logs, flowlogs, alarm, cloudtrail
# If --step is omitted all steps run. If stack-name is omitted, uses the
# most recently modified file in .deploy/.

set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-default}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEPLOY_DIR="${SCRIPT_DIR}/.deploy"

PASS=0
FAIL=0
declare -a MANUAL_STEPS=()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
read_field() {
  local file="$1" key="$2"
  grep "^${key}" "$file" 2>/dev/null | sed 's/^[^=]*= *//' | tr -d '\r' || true
}

pass()   { echo "  [PASS] $*";             PASS=$((PASS + 1)); }
fail()   { echo "  [FAIL] $*";             FAIL=$((FAIL + 1)); }
info()   { echo "         $*"; }
warn()   { echo "  [WARN] $*"; }
manual() { echo "  [MANUAL REQUIRED] $*"; MANUAL_STEPS+=("$*"); }
hdr()      { echo ""; echo "=== $* ==="; }
run_step() { [ -z "${STEP}" ] || [ "${STEP}" = "$1" ]; }

# ---------------------------------------------------------------------------
# Resolve deploy file
# ---------------------------------------------------------------------------
STEP=""
STACK_ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --step)   STEP="$2"; shift 2 ;;
    --step=*) STEP="${1#--step=}"; shift ;;
    -*)       echo "ERROR: Unknown option: $1" >&2; exit 1 ;;
    *)
      if [ -z "${STACK_ARG}" ]; then STACK_ARG="$1"
      else echo "ERROR: Unexpected argument: $1" >&2; exit 1
      fi
      shift ;;
  esac
done

if [ -n "${STEP}" ]; then
  case "${STEP}" in
    cloudwatch|logs|flowlogs|alarm|cloudtrail) ;;
    *)
      echo "ERROR: Unknown --step '${STEP}'. Valid: cloudwatch, logs, flowlogs, alarm, cloudtrail" >&2
      exit 1 ;;
  esac
fi

if [ -n "${STACK_ARG}" ]; then
  OUTPUTS_FILE="${DEPLOY_DIR}/${STACK_ARG}.txt"
elif [ -d "${DEPLOY_DIR}" ]; then
  OUTPUTS_FILE=$(ls -t "${DEPLOY_DIR}"/*.txt 2>/dev/null | head -1 || true)
else
  OUTPUTS_FILE=""
fi

if [ -z "${OUTPUTS_FILE}" ] || [ ! -f "${OUTPUTS_FILE}" ]; then
  echo "ERROR: No deployment found." >&2
  [ $# -ge 1 ] && echo "  File not found: ${DEPLOY_DIR}/$1.txt" >&2
  echo "  Usage: $0 [stack-name]" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Load values from the deploy file
# ---------------------------------------------------------------------------
STACK_NAME=$(read_field "${OUTPUTS_FILE}" "StackName")
REGION=$(read_field "${OUTPUTS_FILE}" "Region")
LOG_GROUP=$(read_field "${OUTPUTS_FILE}" "Neo4jAppLogGroupName")
ALERT_TOPIC_ARN=$(read_field "${OUTPUTS_FILE}" "Neo4jAlertTopicArn")
BROWSER_URL=$(read_field "${OUTPUTS_FILE}" "Neo4jHttpsUrl")
PASSWORD=$(read_field "${OUTPUTS_FILE}" "Password")
NUMBER_OF_SERVERS=$(read_field "${OUTPUTS_FILE}" "NumberOfServers")

# These outputs were added in the Phase 1 template update. Fall back to
# deriving from the stack name if an older .deploy file is missing them.
FLOW_LOG_GROUP=$(read_field "${OUTPUTS_FILE}" "VpcFlowLogGroupName")
if [ -z "${FLOW_LOG_GROUP}" ]; then
  FLOW_LOG_GROUP="/aws/vpc/flowlogs/${STACK_NAME}"
  echo "  (VpcFlowLogGroupName not in deploy file — derived from stack name)"
fi

ALARM_NAME=$(read_field "${OUTPUTS_FILE}" "FailedAuthAlarmName")
if [ -z "${ALARM_NAME}" ]; then
  ALARM_NAME="Neo4j-FailedAuth-${STACK_NAME}"
  echo "  (FailedAuthAlarmName not in deploy file — derived from stack name)"
fi

# Extract hostname from the browser URL (http://hostname:7474)
NEO4J_HOST=$(echo "${BROWSER_URL}" | sed 's|http://||; s|:.*||')

echo ""
echo "============================================="
echo "  Neo4j EE Phase 1 Observability Test"
echo "============================================="
echo "  Stack:   ${STACK_NAME}"
echo "  Region:  ${REGION}"
echo "  Nodes:   ${NUMBER_OF_SERVERS}"
echo "============================================="

# ===========================================================================
# Step 1: CloudWatch agent running on all nodes (via SSM send-command)
# ===========================================================================
if run_step "cloudwatch"; then
hdr "Step 1: CloudWatch Agent Status (SSM)"

COMMAND_ID=$(aws ssm send-command \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["systemctl is-active amazon-cloudwatch-agent"]' \
  --targets "Key=tag:Role,Values=neo4j-cluster-node" \
  --region "${REGION}" \
  --query "Command.CommandId" \
  --output text 2>/dev/null || echo "")

if [ -z "${COMMAND_ID}" ]; then
  fail "SSM send-command failed — instances may not be SSM-managed. Ensure AmazonSSMManagedInstanceCore is attached to the instance role and that the instances have registered with SSM (allow a minute after boot)."
else
  info "Command ${COMMAND_ID} sent. Polling for results (up to 60s)..."

  DEADLINE=$((SECONDS + 60))
  while [ $SECONDS -lt $DEADLINE ]; do
    PENDING=$(aws ssm list-command-invocations \
      --command-id "${COMMAND_ID}" \
      --region "${REGION}" \
      --query "length(CommandInvocations[?Status=='InProgress' || Status=='Pending'])" \
      --output text 2>/dev/null || echo "0")
    [ "${PENDING}" = "0" ] && break
    sleep 5
  done

  INVOCATION_COUNT=0
  ACTIVE_COUNT=0
  while IFS=$'\t' read -r instance_id response_code; do
    [ -z "${instance_id}" ] && continue
    INVOCATION_COUNT=$((INVOCATION_COUNT + 1))
    if [ "${response_code}" = "0" ]; then
      info "${instance_id}: active"
      ACTIVE_COUNT=$((ACTIVE_COUNT + 1))
    else
      fail "${instance_id}: cloudwatch agent not active (exit code: ${response_code:-<no result>})"
    fi
  done < <(aws ssm list-command-invocations \
    --command-id "${COMMAND_ID}" \
    --details \
    --region "${REGION}" \
    --query "CommandInvocations[*].[InstanceId, CommandPlugins[0].ResponseCode]" \
    --output text 2>/dev/null || true)

  if [ "${INVOCATION_COUNT}" -eq 0 ]; then
    fail "No SSM invocations returned — instances may not be SSM-registered yet. Wait a minute and retry."
  elif [ "${ACTIVE_COUNT}" -eq "${INVOCATION_COUNT}" ]; then
    pass "CloudWatch agent active on all ${ACTIVE_COUNT} node(s)"
  fi
fi
fi

# ===========================================================================
# Step 2: Application log group and streams
# ===========================================================================
if run_step "logs"; then
hdr "Step 2: Application Log Group and Streams"

info "Log group: ${LOG_GROUP}"

STREAM_COUNT=$(aws logs describe-log-streams \
  --log-group-name "${LOG_GROUP}" \
  --region "${REGION}" \
  --query "length(logStreams)" \
  --output text 2>/dev/null || echo "0")

EXPECTED_TOTAL=$((NUMBER_OF_SERVERS * 3))
info "Streams found: ${STREAM_COUNT} (expected ${EXPECTED_TOTAL})"

if [ "${STREAM_COUNT}" -ge "${EXPECTED_TOTAL}" ]; then
  pass "Stream count: ${STREAM_COUNT}"
else
  fail "Stream count: ${STREAM_COUNT} of expected ${EXPECTED_TOTAL} — streams may still be initializing"
fi

for SUFFIX in security debug cloud-init-output; do
  COUNT=$(aws logs describe-log-streams \
    --log-group-name "${LOG_GROUP}" \
    --region "${REGION}" \
    --query "logStreams[?contains(logStreamName, '/${SUFFIX}')] | length(@)" \
    --output text 2>/dev/null || echo "0")
  if [ "${COUNT}" -ge "${NUMBER_OF_SERVERS}" ]; then
    pass "Stream type '${SUFFIX}': ${COUNT} stream(s)"
  else
    fail "Stream type '${SUFFIX}': ${COUNT} stream(s), expected ${NUMBER_OF_SERVERS}"
  fi
done
fi

# ===========================================================================
# Step 3: VPC flow logs
# ===========================================================================
if run_step "flowlogs"; then
hdr "Step 3: VPC Flow Logs"

info "Flow log group: ${FLOW_LOG_GROUP}"

GROUP_EXISTS=$(aws logs describe-log-groups \
  --log-group-name-prefix "${FLOW_LOG_GROUP}" \
  --region "${REGION}" \
  --query "length(logGroups)" \
  --output text 2>/dev/null || echo "0")

if [ "${GROUP_EXISTS}" -ge 1 ]; then
  pass "Flow log group exists"

  FLOW_STREAM_COUNT=$(aws logs describe-log-streams \
    --log-group-name "${FLOW_LOG_GROUP}" \
    --region "${REGION}" \
    --query "length(logStreams)" \
    --output text 2>/dev/null || echo "0")

  if [ "${FLOW_STREAM_COUNT}" -ge 1 ]; then
    pass "Flow log streams present: ${FLOW_STREAM_COUNT} ENI stream(s)"
  else
    fail "No flow log streams in ${FLOW_LOG_GROUP} — may still be initializing, retry in a few minutes"
  fi
else
  fail "Flow log group not found: ${FLOW_LOG_GROUP}"
fi
fi

# ===========================================================================
# Step 4: Failed authentication alarm
# ===========================================================================
if run_step "alarm"; then
hdr "Step 4: Failed Authentication Alarm"

# Check SNS subscription status before running the test
info "Checking SNS subscription status..."
PENDING_SUBS=$(aws sns list-subscriptions-by-topic \
  --topic-arn "${ALERT_TOPIC_ARN}" \
  --region "${REGION}" \
  --query "length(Subscriptions[?SubscriptionArn=='PendingConfirmation'])" \
  --output text 2>/dev/null || echo "0")

if [ "${PENDING_SUBS}" -gt 0 ]; then
  warn "SNS subscription is PendingConfirmation."
  warn "The alarm will fire but the email notification will NOT be delivered until you"
  warn "click the confirmation link in the AWS notification email sent to your AlertEmail address."
else
  info "SNS subscription confirmed (or no email subscription configured)"
fi

# Send 12 authentication requests with the wrong password to trigger the metric filter.
# The Neo4j HTTP transactional API returns 401 and writes "Failed to log in" to security.log.
info "Sending 12 failed authentication requests to ${NEO4J_HOST}:7474..."
for i in $(seq 1 12); do
  curl -s -o /dev/null \
    -u "obstest_${i}_${RANDOM}:WrongPassword" \
    -H "Content-Type: application/json" \
    -d '{"statements":[{"statement":"RETURN 1"}]}' \
    "http://${NEO4J_HOST}:7474/db/neo4j/tx" \
    --connect-timeout 5 \
    --max-time 10 \
    2>/dev/null || true
done
info "Requests sent. Waiting up to 7 minutes for the alarm to fire (5-minute evaluation window)..."

ALARM_DEADLINE=$((SECONDS + 420))
ALARM_STATE="UNKNOWN"
while [ $SECONDS -lt $ALARM_DEADLINE ]; do
  ALARM_STATE=$(aws cloudwatch describe-alarms \
    --alarm-names "${ALARM_NAME}" \
    --region "${REGION}" \
    --query "MetricAlarms[0].StateValue" \
    --output text 2>/dev/null || echo "UNKNOWN")

  if [ "${ALARM_STATE}" = "ALARM" ]; then
    break
  fi
  info "Alarm state: ${ALARM_STATE} — checking again in 30s..."
  sleep 30
done

if [ "${ALARM_STATE}" = "ALARM" ]; then
  pass "Alarm '${ALARM_NAME}' transitioned to ALARM"
else
  fail "Alarm '${ALARM_NAME}' did not transition to ALARM within 7 minutes (final state: ${ALARM_STATE})"
  info "Check that /var/log/neo4j/security.log exists on the instance and contains 'Failed to log in' entries."
  info "The cloud-init-output stream should show any errors from the CloudWatch agent configuration step."
fi

# SNS email delivery cannot be verified programmatically
manual "Check your inbox for an alarm notification from no-reply@sns.amazonaws.com for alarm '${ALARM_NAME}'. If no email arrived, see the SNS section in TESTING_GUIDE.md."
fi

# ===========================================================================
# Step 5: CloudTrail (account-level check)
# ===========================================================================
if run_step "cloudtrail"; then
hdr "Step 5: CloudTrail (Account-Level)"

EVENT_COUNT=$(aws cloudtrail lookup-events \
  --region "${REGION}" \
  --start-time "$(date -u -v-24H '+%Y-%m-%dT%H:%M:%SZ')" \
  --max-results 1 \
  --query "length(Events)" \
  --output text 2>/dev/null || echo "0")

if [ "${EVENT_COUNT}" -ge 1 ]; then
  pass "CloudTrail is recording management events in ${REGION}"
else
  fail "No CloudTrail events found in ${REGION} in the last 24h — verify a trail is enabled for this region"
fi
fi

# ===========================================================================
# Summary
# ===========================================================================
echo ""
echo "============================================="
echo "  Phase 1 Observability Test Summary"
echo "============================================="
printf "  Passed:  %d\n" "${PASS}"
printf "  Failed:  %d\n" "${FAIL}"
echo ""

if [ "${#MANUAL_STEPS[@]}" -gt 0 ]; then
  echo "  Manual step(s) required:"
  for item in "${MANUAL_STEPS[@]}"; do
    echo "    - ${item}"
  done
  echo ""
fi

if [ "${FAIL}" -eq 0 ]; then
  echo "  All automated checks passed."
  if [ "${#MANUAL_STEPS[@]}" -gt 0 ]; then
    echo "  Complete the manual step(s) above, then update security.md."
  else
    echo "  Phase 1 complete — update security.md."
  fi
else
  echo "  Some checks failed. Review the output above before marking Phase 1 complete."
fi

echo "============================================="
echo ""

[ "${FAIL}" -eq 0 ]
