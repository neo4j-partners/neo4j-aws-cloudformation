#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["validate-private"]
#
# [tool.uv.sources]
# validate-private = { path = "..", editable = true }
# ///

"""Open an SSM port-forward tunnel to Neo4j Browser on local port 7473.

The tunnel runs until Ctrl-C or SIGTERM. Closing it also stops the Session
Manager plugin, so local port 7473 is free again.
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

BROWSER_PORT = 7473


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open an SSM port-forward tunnel to Neo4j Browser."
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

    print("=== Neo4j Browser Tunnel ===")
    print()
    print(f"  Stack:         {stack_name}")
    print(f"  Region:        {region}")
    print(f"  Bastion:       {bastion_id}")
    print(f"  AdvertisedDNS: {advertised_dns}")
    print()
    print(f"  Tunnel:  localhost:7473  ->  {nlb_dns}:7473")
    print()
    print("  Add to your laptop's /etc/hosts so the cert SAN matches:")
    print(f"    127.0.0.1 {advertised_dns}")
    print()
    print("  Once the tunnel opens:")
    print(f"    Browser: https://{advertised_dns}:7473")
    print(f"    Bolt:    {bolt_scheme}://{advertised_dns}:7687")
    print("             once bolt-tunnel.py is running in a second terminal")
    print()
    print("  Press Ctrl-C to close.")
    print()
    sys.stdout.flush()

    forward = PortForward(
        region, bastion_id, nlb_dns, BROWSER_PORT, local_port=BROWSER_PORT
    )
    try:
        forward.run_until_interrupted()
    except TunnelError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print("Tunnel closed.")


if __name__ == "__main__":
    main()
