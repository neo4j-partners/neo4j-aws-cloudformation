#!/usr/bin/env bash
# common.sh - shared helpers sourced by Bash-only validate-private scripts.

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="${SCRIPTS_DIR}/../../.deploy"

# Read a "Key = Value" deploy file field, stripping whitespace.
read_field() {
  local file="$1" key="$2"
  grep "^${key}" "$file" | sed 's/^[^=]*= *//' | tr -d '\r'
}

# Resolve the .deploy outputs file.
# If a stack name is given, use it; otherwise pick the most recently modified file.
# Prints the file path to stdout; exits 1 with an error message on failure.
resolve_stack() {
  local stack_arg="${1:-}"
  local path=""

  if [ -n "$stack_arg" ]; then
    path="${DEPLOY_DIR}/${stack_arg}.txt"
  elif [ -d "${DEPLOY_DIR}" ]; then
    path=$(ls -t "${DEPLOY_DIR}"/*.txt 2>/dev/null | head -1 || true)
  fi

  if [ -z "${path}" ] || [ ! -f "${path}" ]; then
    echo "ERROR: No deployment found." >&2
    if [ -n "${stack_arg}" ]; then
      echo "  File not found: ${DEPLOY_DIR}/${stack_arg}.txt" >&2
    else
      echo "  No .txt files in ${DEPLOY_DIR}/" >&2
    fi
    echo "  Run deploy.py first, or pass a stack name." >&2
    exit 1
  fi

  echo "$path"
}

# Exit with an error if the stack is not Private or ExistingVpc mode.
require_private_mode() {
  local outputs_file="$1"
  local mode stack_name
  mode=$(read_field "$outputs_file" "DeploymentMode")
  if [ "$mode" != "Private" ] && [ "$mode" != "ExistingVpc" ]; then
    stack_name=$(read_field "$outputs_file" "StackName")
    echo "ERROR: This script requires a Private or ExistingVpc stack." >&2
    echo "  Stack '${stack_name}' has DeploymentMode=${mode}." >&2
    echo "  For Public stacks, connect directly to the NLB endpoint." >&2
    exit 1
  fi
}
