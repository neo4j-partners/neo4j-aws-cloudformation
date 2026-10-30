#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///

"""Open an SSM port-forward tunnel to Neo4j Bolt on port 7687."""

from __future__ import annotations

import argparse
import os
import sys

from private_tools import (
    read_outputs,
    require_field,
    require_private_mode,
    resolve_direct_bolt_scheme,
    resolve_outputs_file,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open an SSM port-forward tunnel to Neo4j Bolt."
    )
    parser.add_argument("stack_name", nargs="?", help="EE stack name.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outputs_file = resolve_outputs_file(args.stack_name)
    fields = read_outputs(outputs_file)
    require_private_mode(fields)

    stack_name = require_field(fields, "StackName", outputs_file)
    region = require_field(fields, "Region", outputs_file)
    bastion_id = require_field(fields, "Neo4jOperatorBastionId", outputs_file)
    nlb_dns = require_field(fields, "Neo4jInternalDNS", outputs_file)
    advertised_dns = require_field(fields, "AdvertisedDNS", outputs_file)
    bolt_scheme = resolve_direct_bolt_scheme(fields)

    print("=== Neo4j Bolt Tunnel ===")
    print()
    print(f"  Stack:   {stack_name}")
    print(f"  Region:  {region}")
    print(f"  Bastion: {bastion_id}")
    print(f"  AdvertisedDNS: {advertised_dns}")
    print()
    print(f"  Tunnel:  localhost:7687  ->  {nlb_dns}:7687")
    print()
    print("  Add to your laptop's /etc/hosts so the cert SAN matches:")
    print(f"    127.0.0.1 {advertised_dns}")
    print()
    print(f"  Connect with: {bolt_scheme}://{advertised_dns}:7687")
    print("  Run browser-tunnel.py in a second terminal for Browser access.")
    print()
    print("  Press Ctrl-C to close.")
    print()

    # os.execvp replaces this process without flushing Python's buffers; under
    # non-TTY (block-buffered) stdout the header above would otherwise be lost.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(
        "aws",
        [
            "aws",
            "ssm",
            "start-session",
            "--target",
            bastion_id,
            "--region",
            region,
            "--document-name",
            "AWS-StartPortForwardingSessionToRemoteHost",
            "--parameters",
            f"host={nlb_dns},portNumber=7687,localPortNumber=7687",
        ],
    )


if __name__ == "__main__":
    main()
