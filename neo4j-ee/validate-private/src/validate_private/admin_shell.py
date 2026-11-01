"""CLI entry point: uv run admin-shell

Runs cypher-shell on this machine through an SSM port-forward to the
internal NLB. The password is read with the operator's AWS credentials and
passed to cypher-shell in its environment only, never on the command line.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError

from validate_private.config import StackConfig, load_config
from validate_private.runner import BOLT_PORT
from validate_private.tunnel import PortForward, TunnelError

log = logging.getLogger(__name__)

_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"
_LOOPBACK = {"127.0.0.1", "::1"}

# cypher-shell has no custom resolver. A bolt:// URI sends no routing
# context, so server-side routing does not apply and a write that lands on a
# follower fails with NotALeader. The routed scheme makes cypher-shell follow
# the routing table, whose entries name the advertised Bolt host on 7687. The
# /etc/hosts line maps that host to 127.0.0.1, which is what makes every
# routed address land on this tunnel. The local port must therefore be 7687.


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


def _local_port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", BOLT_PORT), timeout=1):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def _tunnel(config: StackConfig) -> Iterator[None]:
    if _local_port_open():
        log.info("  Using the existing tunnel on %s.", BOLT_PORT)
        yield
        return
    log.info("  Opening the SSM port-forward on 127.0.0.1:%s...", BOLT_PORT)
    with PortForward(
        config.region, config.bastion_id, config.nlb_dns, BOLT_PORT,
        local_port=BOLT_PORT,
    ):
        yield


def _advertised_host(config: StackConfig) -> str:
    """Return the host the cluster advertises in its Bolt routing table."""
    scheme = "bolt+ssc" if "+ssc" in config.bolt_scheme else "bolt"
    with GraphDatabase.driver(
        f"{scheme}://127.0.0.1:{BOLT_PORT}",
        auth=("neo4j", config.password),
    ) as driver:
        records, _, _ = driver.execute_query(
            "CALL dbms.routing.getRoutingTable({})"
        )
    address = records[0]["servers"][0]["addresses"][0]
    host, _, _ = address.rpartition(":")
    return host


def _resolves_to_loopback(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, BOLT_PORT, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return all(info[4][0] in _LOOPBACK for info in infos)


def _run_shell(cypher_shell: str, uri: str, config: StackConfig) -> int:
    env = {
        **os.environ,
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": config.password,
    }
    proc = subprocess.Popen([cypher_shell, "-a", uri], env=env)
    # Ctrl-C belongs to cypher-shell, which uses it to cancel a query. The
    # handler is set after the fork so the child keeps the default.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return proc.wait()
    finally:
        signal.signal(signal.SIGINT, previous)


def main() -> None:
    args = sys.argv[1:]
    stack = args[0] if args else None

    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stderr)

    cypher_shell = shutil.which("cypher-shell")
    if cypher_shell is None:
        log.error(
            "ERROR: cypher-shell is not on PATH. Install cypher-shell "
            "(brew install cypher-shell) and run admin-shell again."
        )
        sys.exit(1)

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
    log.info("  NLB:     %s:%s", config.nlb_dns, BOLT_PORT)
    log.info("")

    try:
        with _tunnel(config):
            host = _advertised_host(config)
            if not _resolves_to_loopback(host):
                log.error(
                    "ERROR: cypher-shell follows the routing table to %s, so "
                    "that name must resolve to 127.0.0.1. Add this line to "
                    "/etc/hosts:",
                    host,
                )
                print(f"127.0.0.1 {host}")
                sys.exit(1)
            uri = f"{config.bolt_scheme}://{host}:{BOLT_PORT}"
            log.info("  URI:     %s", uri)
            log.info("  Type ':exit' or press Ctrl-D to close the session.")
            log.info("")
            rc = _run_shell(cypher_shell, uri, config)
    except TunnelError as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)
    except (Neo4jError, DriverError) as exc:
        log.error("ERROR: Could not read the routing table: %s", exc)
        sys.exit(1)
    sys.exit(rc)
