#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["validate-private"]
#
# [tool.uv.sources]
# validate-private = { path = "..", editable = true }
# ///

"""Open an SSM port-forward tunnel to Neo4j Bolt on local port 7687.

The tunnel runs until Ctrl-C or SIGTERM. Closing it also stops the Session
Manager plugin, so local port 7687 is free again.
"""

from __future__ import annotations

import argparse
import sys

from private_tools import (
    read_outputs,
    require_field,
    require_private_mode,
    resolve_direct_bolt_scheme,
    resolve_outputs_file,
)
from validate_private.tunnel import PortForward, TunnelError

BOLT_PORT = 7687


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
    print("  Add both lines to your laptop's /etc/hosts:")
    print(f"    127.0.0.1 {advertised_dns}")
    print(f"    127.0.0.1 {nlb_dns}")
    print("  Bolt advertises AdvertisedDNS when the stack's private DNS record")
    print("  resolves it in the VPC, and the NLB DNS otherwise. uv run")
    print("  admin-shell reports which one is live.")
    print()
    print(f"  Connect with: {bolt_scheme}://{advertised_dns}:7687")
    print("  Run browser-tunnel.py in a second terminal for Browser access.")
    print()
    print("  Press Ctrl-C to close.")
    print()
    sys.stdout.flush()

    forward = PortForward(
        region, bastion_id, nlb_dns, BOLT_PORT, local_port=BOLT_PORT
    )
    try:
        forward.run_until_interrupted()
    except TunnelError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print("Tunnel closed.")


if __name__ == "__main__":
    main()
