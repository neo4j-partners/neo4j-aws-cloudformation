#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Run transient write operations through the operator bastion."""

from __future__ import annotations

import argparse
import base64
import shlex
import sys
import time

import boto3

from private_tools import (
    read_outputs,
    require_field,
    require_private_mode,
    resolve_bolt_scheme,
    resolve_outputs_file,
)


REMOTE_SCRIPT = r"""
import sys

import boto3
from neo4j import GraphDatabase

stack = sys.argv[1]
region = sys.argv[2]
bolt_scheme = sys.argv[3]
n = int(sys.argv[4])

sm = boto3.client("secretsmanager", region_name=region)
password = sm.get_secret_value(SecretId=f"neo4j/{stack}/password")["SecretString"]

ssm_client = boto3.client("ssm", region_name=region)
nlb_dns = ssm_client.get_parameter(
    Name=f"/neo4j-ee/{stack}/nlb-dns"
)["Parameter"]["Value"]

driver = GraphDatabase.driver(
    f"{bolt_scheme}://{nlb_dns}:7687",
    auth=("neo4j", password),
)
successes = 0
failures = 0
try:
    for i in range(1, n + 1):
        try:
            driver.execute_query("CREATE (n:_SmokeWrite) DELETE n")
            successes += 1
            print(f"  [{i}/{n}] OK", flush=True)
        except Exception as exc:
            failures += 1
            print(f"  [{i}/{n}] FAIL: {exc}", flush=True)
finally:
    driver.close()

print(f"\nResult: {successes}/{n} succeeded", flush=True)
if failures > 0:
    sys.exit(1)
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run transient Neo4j write operations through the bastion."
    )
    parser.add_argument("stack_name", nargs="?", help="EE stack name.")
    parser.add_argument(
        "iterations",
        nargs="?",
        default=20,
        type=int,
        help="Number of CREATE/DELETE iterations.",
    )
    return parser.parse_args()


def wait_for_command(ssm, command_id: str, instance_id: str, timeout_s: int) -> int:
    terminal = {"Success", "Failed", "Cancelled", "TimedOut"}
    deadline = time.monotonic() + timeout_s
    status = "Pending"
    invocation = {}

    while time.monotonic() < deadline:
        try:
            invocation = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
            status = invocation["Status"]
        except ssm.exceptions.InvocationDoesNotExist:
            status = "Pending"

        if status in terminal:
            stdout = invocation.get("StandardOutputContent", "")
            stderr = invocation.get("StandardErrorContent", "")
            if stdout:
                print(stdout, end="" if stdout.endswith("\n") else "\n")
            if status == "Success":
                print("Smoke write test PASSED.")
                return 0
            print(f"ERROR: Smoke write test FAILED (status={status})", file=sys.stderr)
            if stderr:
                print(stderr, file=sys.stderr, end="" if stderr.endswith("\n") else "\n")
            return 1

        time.sleep(2)

    print("ERROR: Timed out waiting for smoke write test.", file=sys.stderr)
    return 1


def main() -> None:
    args = parse_args()
    outputs_file = resolve_outputs_file(args.stack_name)
    fields = read_outputs(outputs_file)
    require_private_mode(fields)

    stack_name = require_field(fields, "StackName", outputs_file)
    region = require_field(fields, "Region", outputs_file)
    bastion_id = require_field(fields, "Neo4jOperatorBastionId", outputs_file)
    nlb_dns = require_field(fields, "Neo4jInternalDNS", outputs_file)
    bolt_scheme = resolve_bolt_scheme(fields)

    print("=== Smoke Write Test ===")
    print()
    print(f"  Stack:      {stack_name}")
    print(f"  Region:     {region}")
    print(f"  Bastion:    {bastion_id}")
    print(f"  URI:        {bolt_scheme}://{nlb_dns}:7687")
    print(f"  Iterations: {args.iterations}")
    print()

    payload = base64.b64encode(REMOTE_SCRIPT.encode()).decode()
    write_cmd = (
        "python3.11 -c "
        + shlex.quote(
            "import base64, pathlib; "
            "pathlib.Path('/tmp/vpsmoke.py').write_text("
            f"base64.b64decode('{payload}').decode())"
        )
    )
    run_cmd = " ".join(
        [
            "python3.11",
            "/tmp/vpsmoke.py",
            shlex.quote(stack_name),
            shlex.quote(region),
            shlex.quote(bolt_scheme),
            str(args.iterations),
        ]
    )

    ssm = boto3.client("ssm", region_name=region)
    command = ssm.send_command(
        InstanceIds=[bastion_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [write_cmd, run_cmd]},
    )["Command"]
    command_id = command["CommandId"]

    print(f"  SSM command: {command_id}")
    print(f"  Waiting (est. ~{args.iterations * 3}s)...")
    print()

    timeout_s = args.iterations * 10 + 120
    raise SystemExit(wait_for_command(ssm, command_id, bastion_id, timeout_s))


if __name__ == "__main__":
    main()
