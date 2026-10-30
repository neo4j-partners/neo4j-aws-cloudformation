"""CLI entry point: uv run admin-shell"""

from __future__ import annotations

import base64
import logging
import os
import sys
import time
from pathlib import Path

from validate_private.config import load_config

log = logging.getLogger(__name__)

_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"

# Launcher script that runs ON the bastion. Placeholders __STACK__, __REGION__,
# and __BOLT_SCHEME__ are replaced before base64 encoding, so no shell expansion
# is needed.
_LAUNCHER_TEMPLATE = """\
#!/bin/bash
set -euo pipefail
export NEO4J_PASSWORD=$(aws secretsmanager get-secret-value \\
  --secret-id 'neo4j/__STACK__/password' \\
  --query SecretString --output text --region '__REGION__')
NLB_DNS=$(aws ssm get-parameter \\
  --name '/neo4j-ee/__STACK__/nlb-dns' \\
  --query Parameter.Value --output text --region '__REGION__')
exec cypher-shell -a "__BOLT_SCHEME__://${NLB_DNS}:7687" -u neo4j -p "${NEO4J_PASSWORD}"
"""


def _resolve_outputs_path(stack: str | None) -> Path:
    if stack:
        return _DEPLOY_DIR / f"{stack}.txt"
    if _DEPLOY_DIR.is_dir():
        txt_files = sorted(
            _DEPLOY_DIR.glob("*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if txt_files:
            return txt_files[0]
    raise FileNotFoundError(
        f"No deployment found in {_DEPLOY_DIR}. "
        "Run deploy.py first, or pass a stack name as the first argument."
    )


def main() -> None:
    args = sys.argv[1:]
    stack = args[0] if args else None

    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stderr)

    try:
        outputs_path = _resolve_outputs_path(stack)
        config = load_config(outputs_path)
    except (FileNotFoundError, ValueError) as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)

    log.info("=== Neo4j Admin Shell ===")
    log.info("")
    log.info("  Stack:   %s", config.stack_name)
    log.info("  Region:  %s", config.region)
    log.info("  Bastion: %s", config.bastion_id)
    log.info("  URI:     %s://%s:7687", config.bolt_scheme, config.nlb_dns)
    log.info("")
    log.info("  Password is resolved on the bastion — not visible here or in CloudTrail.")
    log.info("  Type ':exit' or press Ctrl-D to close the session.")
    log.info("")

    import boto3

    launcher = (
        _LAUNCHER_TEMPLATE
        .replace("__STACK__", config.stack_name)
        .replace("__REGION__", config.region)
        .replace("__BOLT_SCHEME__", config.bolt_scheme)
    )
    b64_launcher = base64.b64encode(launcher.encode()).decode()

    ssm = boto3.client("ssm", region_name=config.region)

    # Step 1: write the launcher script to the bastion via RunShellScript.
    log.info("  Preparing bastion...")
    write_cmd = f"echo {b64_launcher} | base64 -d > /tmp/neo4j-shell.sh && chmod +x /tmp/neo4j-shell.sh"
    resp = ssm.send_command(
        InstanceIds=[config.bastion_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [write_cmd]},
    )
    command_id = resp["Command"]["CommandId"]

    terminal = {"Success", "Failed", "Cancelled", "TimedOut"}
    deadline = time.monotonic() + 30
    inv: dict = {}
    status = "Pending"

    while True:
        if time.monotonic() >= deadline:
            log.error("ERROR: Timed out waiting for bastion preparation")
            sys.exit(1)
        try:
            inv = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=config.bastion_id,
            )
            status = inv["Status"]
            if status in terminal:
                break
        except ssm.exceptions.InvocationDoesNotExist:
            pass
        time.sleep(2)

    if status != "Success":
        stderr = inv.get("StandardErrorContent", "").strip()
        log.error("ERROR: Failed to prepare bastion (status=%s)\n%s", status, stderr)
        sys.exit(1)

    # Step 2: replace this process with an interactive SSM session that runs the launcher.
    os.execvp(
        "aws",
        [
            "aws", "ssm", "start-session",
            "--target", config.bastion_id,
            "--region", config.region,
            "--document-name", "AWS-StartInteractiveCommand",
            "--parameters", '{"command": ["/tmp/neo4j-shell.sh"]}',
        ],
    )
