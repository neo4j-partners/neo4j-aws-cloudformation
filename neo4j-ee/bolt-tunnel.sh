#!/usr/bin/env bash
# bolt-tunnel.sh: Open an SSM tunnel to Neo4j Bolt (port 7687) on a Private
# stack, through the stack's operator bastion.
#
# Usage:
#   ./bolt-tunnel.sh [stack-name]
#
# If stack-name is omitted, uses the most recently modified file in .deploy/.
# AWS_PROFILE defaults to marketplace; set it to use another profile.
set -euo pipefail

export AWS_PROFILE="${AWS_PROFILE:-marketplace}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}/validate-private"
exec uv run scripts/bolt-tunnel.py "$@"
