#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["validate-private"]
#
# [tool.uv.sources]
# validate-private = { path = "..", editable = true }
# ///

"""Run transient write operations over the SSM tunnel to the cluster."""

from __future__ import annotations

import argparse
import sys

from private_tools import resolve_outputs_file
from validate_private.config import load_config
from validate_private.runner import Neo4jQueryError, run_cypher
from validate_private.tunnel import TunnelError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run transient Neo4j write operations over the SSM tunnel."
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


def main() -> None:
    args = parse_args()
    outputs_file = resolve_outputs_file(args.stack_name)
    try:
        config = load_config(outputs_file)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    print("=== Smoke Write Test ===")
    print()
    print(f"  Stack:      {config.stack_name}")
    print(f"  Region:     {config.region}")
    print(f"  Bastion:    {config.bastion_id}")
    if config.verify_tls:
        scheme = config.bolt_scheme.split("+", 1)[0]
        print(f"  URI:        {scheme}://{config.nlb_dns}:7687")
        print("  TLS:        verified against the published ca-pem")
    else:
        print(f"  URI:        {config.bolt_scheme}://{config.nlb_dns}:7687")
    print(f"  Iterations: {args.iterations}")
    print()

    successes = 0
    for i in range(1, args.iterations + 1):
        try:
            run_cypher(config, "CREATE (n:_SmokeWrite) DELETE n")
        except (Neo4jQueryError, TunnelError) as exc:
            print(f"  [{i}/{args.iterations}] FAIL: {exc}", flush=True)
            continue
        successes += 1
        print(f"  [{i}/{args.iterations}] OK", flush=True)

    print(f"\nResult: {successes}/{args.iterations} succeeded", flush=True)
    if successes != args.iterations:
        print("ERROR: Smoke write test FAILED", file=sys.stderr)
        raise SystemExit(1)
    print("Smoke write test PASSED.")


if __name__ == "__main__":
    main()
