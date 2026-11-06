#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3>=1.43.117"]
# ///

"""Open Browser and Bolt SSM tunnels for a deployed EE stack."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

from botocore.exceptions import BotoCoreError, ClientError


SCRIPT_DIR = Path(__file__).resolve().parent
DEPLOY_DIR = SCRIPT_DIR.parent / ".deploy"

sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))
from neo4j_ee.outputs import (  # noqa: E402
    read_outputs,
    require_field,
    resolve_direct_bolt_scheme,
    resolve_outputs_file,
)
from neo4j_ee.password import fetch_admin_password  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open Neo4j Browser and Bolt SSM tunnels for an EE stack."
    )
    parser.add_argument("stack_name", nargs="?", help="EE stack name.")
    parser.add_argument(
        "--show-password",
        action="store_true",
        help=(
            "Fetch the Neo4j password from Secrets Manager and print it "
            "(default: print the secret ARN)."
        ),
    )
    return parser.parse_args()


def start_tunnel(
    region: str,
    bastion_id: str,
    nlb_host: str,
    port: int,
) -> subprocess.Popen:
    return subprocess.Popen(
        [
            "aws",
            "ssm",
            "start-session",
            "--region",
            region,
            "--target",
            bastion_id,
            "--document-name",
            "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters",
            f"host={nlb_host},portNumber={port},localPortNumber={port}",
        ]
    )


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> None:
    os.environ.setdefault("AWS_PROFILE", "default")
    args = parse_args()
    try:
        outputs_file = resolve_outputs_file(DEPLOY_DIR, args.stack_name)
        fields = read_outputs(outputs_file)
        region = require_field(fields, "Region", outputs_file)
        bastion_id = require_field(fields, "Neo4jOperatorBastionId", outputs_file)
        nlb_host = require_field(fields, "Neo4jInternalDNS", outputs_file)
        advertised_dns = require_field(fields, "AdvertisedDNS", outputs_file)
        secret_arn = require_field(fields, "Neo4jPasswordSecretArn", outputs_file)
        stack_name = require_field(fields, "StackName", outputs_file)
        password = (
            fetch_admin_password(fields, outputs_file) if args.show_password else ""
        )
    except (FileNotFoundError, ValueError, BotoCoreError, ClientError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    username = fields.get("Username", "neo4j")
    bolt_scheme = resolve_direct_bolt_scheme(fields)

    print(f"Stack:         {stack_name}")
    print(f"Region:        {region}")
    print(f"Bastion:       {bastion_id}")
    print()
    print("Opening SSM port-forwards to localhost:7473 and localhost:7687")
    print()
    print("Add to your laptop's /etc/hosts so the cert SAN matches:")
    print(f"  127.0.0.1 {advertised_dns}")
    print()
    print(f"Then open: https://{advertised_dns}:7473")
    print(f"Bolt URI:  {bolt_scheme}://{advertised_dns}:7687")
    print(f"Username: {username}")
    if args.show_password:
        print(f"Password: {password}")
    else:
        print(
            f"Password: in Secrets Manager at {secret_arn} "
            "(pass --show-password to print it)"
        )
    print()
    print("Press Ctrl-C to stop the tunnels.")
    print()

    bolt_process: subprocess.Popen | None = None
    try:
        bolt_process = start_tunnel(region, bastion_id, nlb_host, 7687)
        time.sleep(1)
        browser_process = start_tunnel(region, bastion_id, nlb_host, 7473)
        raise SystemExit(browser_process.wait())
    except KeyboardInterrupt:
        raise SystemExit(130)
    finally:
        stop_process(bolt_process)


if __name__ == "__main__":
    main()
