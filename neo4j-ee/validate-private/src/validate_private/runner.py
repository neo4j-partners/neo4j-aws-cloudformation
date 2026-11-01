"""Run Cypher on the cluster over an SSM tunnel, and shell commands via SSM.

Cypher runs in this process. The neo4j driver connects through an SSM
port-forward to the internal NLB, relayed by the operator bastion. The
password comes from the operator's own AWS credentials and stays in memory.
It is never written to disk and never sent in an SSM command.
"""

from __future__ import annotations

import atexit
import logging
import threading
from typing import TYPE_CHECKING

from neo4j import Driver, GraphDatabase, Query
from neo4j.exceptions import DriverError, Neo4jError

from validate_private.trust_anchor import ssl_context
from validate_private.tunnel import PortForward

if TYPE_CHECKING:
    from validate_private.config import StackConfig

log = logging.getLogger(__name__)

BOLT_PORT = 7687

_lock = threading.Lock()
_tunnels: dict[str, PortForward] = {}
_drivers: dict[tuple[str, str], Driver] = {}


class Neo4jQueryError(RuntimeError):
    pass


def bolt_tunnel(config: "StackConfig") -> PortForward:
    """Return the stack's Bolt tunnel, starting or restarting it as needed.

    A restart picks a new local port, so the drivers bound to the old port
    are closed and rebuilt on next use.
    """
    with _lock:
        return _live_tunnel(config)


def _live_tunnel(config: "StackConfig") -> PortForward:
    tunnel = _tunnels.get(config.stack_name)
    if tunnel is None:
        tunnel = PortForward(config.region, config.bastion_id, config.nlb_dns, BOLT_PORT)
        _tunnels[config.stack_name] = tunnel
    if not tunnel.alive():
        for key in [k for k in _drivers if k[0] == config.stack_name]:
            _drivers.pop(key).close()
        log.debug("Opening Bolt tunnel for %s", config.stack_name)
        tunnel.start()
    return tunnel


def open_driver(
    config: "StackConfig",
    auth: tuple[str, str] | None = None,
) -> Driver:
    """Open a new driver over the stack's Bolt tunnel. The caller closes it.

    The resolver sends every address to the tunnel, including the addresses
    in the routing table, so routing behaves as it does for an in-VPC client
    of the NLB. The resolver also replaces the TLS server name with
    127.0.0.1, so the driver never checks the hostname. On a neo4j+s stack
    the driver still verifies the chain against the published ca-pem. On a
    neo4j+ssc stack it uses the +ssc scheme and verifies nothing.
    """
    port = bolt_tunnel(config).local_port
    return _new_driver(config, port, auth)


def _new_driver(
    config: "StackConfig",
    port: int,
    auth: tuple[str, str] | None,
) -> Driver:
    scheme = config.bolt_scheme
    options: dict = {}
    if config.verify_tls:
        # ssl_context is accepted only with the plain neo4j:// or bolt://
        # scheme, and it turns encryption on.
        scheme = scheme.split("+", 1)[0]
        options["ssl_context"] = ssl_context(config.ca_pem)
    return GraphDatabase.driver(
        f"{scheme}://{config.nlb_dns}:{BOLT_PORT}",
        auth=auth or ("neo4j", config.password),
        resolver=lambda _address: [("127.0.0.1", port)],
        connection_timeout=15,
        **options,
    )


def _shared_driver(config: "StackConfig", auth: tuple[str, str] | None) -> Driver:
    username = auth[0] if auth else "neo4j"
    key = (config.stack_name, username)
    with _lock:
        tunnel = _live_tunnel(config)
        driver = _drivers.get(key)
        if driver is None:
            driver = _new_driver(config, tunnel.local_port, auth)
            _drivers[key] = driver
        return driver


def run_cypher(
    config: "StackConfig",
    cypher: str,
    params: dict | None = None,
    database: str | None = None,
    timeout_s: int = 60,
    auth: tuple[str, str] | None = None,
) -> list[dict]:
    """Execute a Cypher query over the Bolt tunnel and return the rows.

    Pass database="system" for admin queries like SHOW SERVERS. Pass auth to
    run as a user other than neo4j. Server and driver errors raise
    Neo4jQueryError. A tunnel that cannot open raises TunnelError.
    """
    driver = _shared_driver(config, auth)
    kwargs: dict = {"parameters_": params or {}}
    if database:
        kwargs["database_"] = database
    try:
        result = driver.execute_query(Query(cypher, timeout=timeout_s), **kwargs)
    except (Neo4jError, DriverError) as exc:
        raise Neo4jQueryError(f"Query failed: {exc}") from exc
    return [record.data() for record in result.records]


@atexit.register
def close_all() -> None:
    """Close every driver and tunnel this process opened."""
    with _lock:
        while _drivers:
            _drivers.popitem()[1].close()
        while _tunnels:
            _tunnels.popitem()[1].close()


def run_ssm_command(
    ssm,
    command_id: str,
    instance_id: str,
    timeout_s: int,
) -> tuple[str, str, str]:
    """Wait for an SSM command via the CommandExecuted waiter. Returns (status, stdout, stderr).

    SSM caps StandardOutputContent at 24,000 chars. A warning is logged when output
    reaches that limit so callers know the response may be truncated.
    """
    from botocore.exceptions import WaiterError

    max_attempts = max(1, timeout_s // 5)
    try:
        ssm.get_waiter("command_executed").wait(
            CommandId=command_id,
            InstanceId=instance_id,
            WaiterConfig={"Delay": 5, "MaxAttempts": max_attempts},
        )
    except WaiterError:
        pass  # fall through — get_command_invocation captures output on all terminal states

    try:
        inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
    except ssm.exceptions.InvocationDoesNotExist:
        return "TimedOut", "", f"SSM command {command_id} not yet registered on {instance_id}"

    stdout = inv.get("StandardOutputContent", "").strip()
    stderr = inv.get("StandardErrorContent", "").strip()
    if len(stdout) >= 24000:
        s3_url = inv.get("StandardOutputUrl", "")
        log.warning(
            "SSM output reached the 24,000-char limit and may be truncated. "
            "Full output: %s",
            s3_url or "(configure OutputS3BucketName on send_command to get an S3 URL)",
        )
    return inv.get("Status", "Unknown"), stdout, stderr



def run_shell_on_instance(
    ssm,
    instance_id: str,
    shell_cmd: str,
    timeout_s: int = 120,
) -> tuple[bool, str, str]:
    """Run a shell command on any SSM-managed instance via RunShellScript.

    ssm must be a pre-created boto3 SSM client (with standard retry config).
    Returns (success, stdout, stderr). Does not raise on command failure so
    callers can decide how to handle partial results.
    """
    from botocore.exceptions import ClientError

    try:
        resp = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [shell_cmd]},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "InvalidInstanceId":
            return False, "", f"Instance {instance_id} is not SSM-registered"
        return False, "", str(exc)

    command_id = resp["Command"]["CommandId"]
    status, stdout, stderr = run_ssm_command(ssm, command_id, instance_id, timeout_s)
    return status == "Success", stdout, stderr
