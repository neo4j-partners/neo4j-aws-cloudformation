#!/bin/bash
# validate.sh - Trigger the resilience test: stop a follower via SSM, verify it rejoins.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_FILE=$(ls -t "${SCRIPT_DIR}/../.deploy"/sample-private-app-*.json 2>/dev/null | head -1 || true)
if [ -z "${APP_FILE}" ]; then
  echo "ERROR: No sample-private-app deployment found. Run uv run deploy-sample-private-app.py first." >&2
  exit 1
fi

VALIDATE_URL=$(python3 -c "import json; d=json.load(open('${APP_FILE}')); print(d['validate_url'])")
REGION=$(python3 -c "import json; d=json.load(open('${APP_FILE}')); print(d['region'])")
if [ -z "${VALIDATE_URL}" ]; then
  echo "ERROR: This sample app was deployed without --enable-resilience." >&2
  echo "Redeploy with uv run deploy-sample-private-app.py --enable-resilience to create the test-only stop/start Lambda." >&2
  exit 1
fi

eval "$(aws configure export-credentials --format env 2>/dev/null)"

BODY_FILE=$(mktemp)
STATUS=$(curl --silent --show-error --output "${BODY_FILE}" --write-out "%{http_code}" \
  --max-time 310 --aws-sigv4 "aws:amz:${REGION}:lambda" \
  --user "${AWS_ACCESS_KEY_ID}:${AWS_SECRET_ACCESS_KEY}" \
  -H "x-amz-security-token: ${AWS_SESSION_TOKEN:-}" \
  -H "Content-Type: application/json" \
  "${VALIDATE_URL}")

if [ "${STATUS}" -lt 200 ] || [ "${STATUS}" -ge 300 ]; then
  echo "ERROR: Function URL returned HTTP ${STATUS}" >&2
  cat "${BODY_FILE}" >&2
  echo >&2
  rm -f "${BODY_FILE}"
  exit 1
fi

python3 -m json.tool <"${BODY_FILE}"
rm -f "${BODY_FILE}"
