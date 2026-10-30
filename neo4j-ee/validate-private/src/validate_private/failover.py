"""Service-bounce failover test cases for EE Private clusters.

follower-with-data: stop a random follower via SSM; verify sentinel persists.
leader:            stop the current leader via SSM; verify new leader elected + sentinel.
rolling:           serialized stop/start of follower, follower, leader; verify sentinel.
reads:             measure driver read failures during a follower bounce.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING

from botocore.config import Config
from validate_private.aws_helpers import asg_instances as _asg_instances
from validate_private.aws_helpers import stack_resources as _stack_resources
from validate_private.quorum import check_quorum, preflight_healthy
from validate_private.runner import run_cypher_on_bastion, run_shell_on_instance, run_ssm_command
from validate_private.sentinel import cleanup_sentinel, verify_sentinel, write_sentinel
from validate_private.server_ids import build_uuid_to_instance_map

if TYPE_CHECKING:
    from validate_private.config import StackConfig
    from validate_private.reporting import TestReporter

log = logging.getLogger(__name__)

_ASG_LOGICAL_IDS = ("Neo4jNode1ASG", "Neo4jNode2ASG", "Neo4jNode3ASG")
_RETRY_CFG = Config(retries={"mode": "standard"})

# Inline probe shipped to the bastion for run_reads. Resolves credentials from
# Secrets Manager and NLB DNS from SSM Parameter Store using the bastion's IAM role.
_READS_PROBE = """\
import sys, json, time, boto3
from neo4j import GraphDatabase

stack, region, bolt_scheme, n_iter = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])

sm = boto3.client("secretsmanager", region_name=region)
password = sm.get_secret_value(SecretId=f"neo4j/{stack}/password")["SecretString"]
ssm_client = boto3.client("ssm", region_name=region)
nlb_dns = ssm_client.get_parameter(Name=f"/neo4j-ee/{stack}/nlb-dns")["Parameter"]["Value"]

driver = GraphDatabase.driver(f"{bolt_scheme}://{nlb_dns}:7687", auth=("neo4j", password))
failures = 0
exc_types = []
try:
    for _ in range(n_iter):
        try:
            driver.execute_query("MATCH (n) RETURN count(n) AS c")
        except Exception as exc:
            failures += 1
            t = type(exc).__name__
            if t not in exc_types:
                exc_types.append(t)
        time.sleep(1)
finally:
    driver.close()
print(json.dumps({"iterations": n_iter, "failures": failures, "exception_types": exc_types}))
"""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_cluster_instances(cfn, asg, config: "StackConfig") -> list[str]:
    resources = _stack_resources(cfn, config.stack_name)
    instance_ids: list[str] = []
    for logical in _ASG_LOGICAL_IDS:
        asg_name = resources.get(logical)
        if asg_name:
            instance_ids.extend(_asg_instances(asg, asg_name))
    return instance_ids


def _classify_roles(config: "StackConfig") -> dict[str, bool]:
    """Return {server_uuid: is_writer} from SHOW DATABASE."""
    rows = run_cypher_on_bastion(
        config, "SHOW DATABASE neo4j YIELD serverID, writer", database="system"
    )
    return {r["serverID"]: bool(r.get("writer")) for r in rows if r.get("serverID")}


def _wait_health(
    config: "StackConfig",
    server_uuid: str,
    target_states: set[str],
    timeout: int,
) -> bool:
    """Poll SHOW SERVERS until server_uuid's health is in target_states."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = run_cypher_on_bastion(config, "SHOW SERVERS", database="system")
            for row in rows:
                if row.get("name") == server_uuid:
                    if row.get("health") in target_states:
                        return True
                    break
        except Exception:
            pass
        time.sleep(5)
    return False


def _stop_neo4j(ssm, instance_id: str) -> tuple[bool, str]:
    ok, _, err = run_shell_on_instance(ssm, instance_id, "systemctl stop neo4j")
    if not ok:
        return False, f"systemctl stop failed on {instance_id}: {err}"
    return True, f"neo4j stopped on {instance_id}"


def _start_neo4j(ssm, instance_id: str) -> tuple[bool, str]:
    ok, _, err = run_shell_on_instance(ssm, instance_id, "systemctl start neo4j")
    if not ok:
        return False, f"systemctl start failed on {instance_id}: {err}"
    return True, f"neo4j started on {instance_id}"


def _discover_roles(
    cfn, asg, ssm, config: "StackConfig", reporter: "TestReporter", label: str
) -> tuple[dict[str, bool], dict[str, str]] | None:
    """Discover cluster roles and instance map; records a reporter row on failure. Returns None on error."""
    start = time.monotonic()
    try:
        instance_ids = _get_cluster_instances(cfn, asg, config)
        roles = _classify_roles(config)
        uuid_to_instance = build_uuid_to_instance_map(ssm, instance_ids)
        return roles, uuid_to_instance
    except Exception as exc:
        reporter.record(label, False, str(exc), time.monotonic() - start)
        return None


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def run_follower_with_data(config: "StackConfig", reporter: "TestReporter") -> None:
    """Stop a random follower via SSM; verify sentinel persists through the bounce."""
    import boto3

    log.info("=== follower-with-data ===")
    log.info("")

    cfn = boto3.client("cloudformation", region_name=config.region, config=_RETRY_CFG)
    asg = boto3.client("autoscaling", region_name=config.region, config=_RETRY_CFG)
    ssm = boto3.client("ssm", region_name=config.region, config=_RETRY_CFG)

    start = time.monotonic()
    ok, detail = preflight_healthy(config, expected_nodes=3)
    reporter.record("Preflight cluster healthy", ok, detail, time.monotonic() - start)
    if not ok:
        return

    test_run_id = uuid.uuid4().hex

    start = time.monotonic()
    ok, detail = write_sentinel(config, test_run_id)
    reporter.record("Write sentinel", ok, detail, time.monotonic() - start)
    if not ok:
        return

    start = time.monotonic()
    result = _discover_roles(cfn, asg, ssm, config, reporter, "Identify follower")
    if result is None:
        cleanup_sentinel(config, test_run_id)
        return
    roles, uuid_to_instance = result

    follower_uuid = next(
        (u for u, is_writer in roles.items() if not is_writer and u in uuid_to_instance), None
    )
    if not follower_uuid:
        reporter.record("Identify follower", False, "No follower found in cluster", time.monotonic() - start)
        cleanup_sentinel(config, test_run_id)
        return

    target_instance = uuid_to_instance[follower_uuid]
    reporter.record(
        "Identify follower", True,
        f"UUID {follower_uuid[:8]}… → {target_instance}",
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = _stop_neo4j(ssm, target_instance)
    reporter.record("Stop follower", ok, detail, time.monotonic() - start)
    if not ok:
        cleanup_sentinel(config, test_run_id)
        return

    start = time.monotonic()
    gone = _wait_health(config, follower_uuid, {"Unavailable", "Unknown", "Down"}, timeout=90)
    reporter.record(
        "Follower health Unavailable", gone,
        f"{follower_uuid[:8]}… went Unavailable" if gone else "Timed out waiting for Unavailable (90s)",
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = _start_neo4j(ssm, target_instance)
    reporter.record("Start follower", ok, detail, time.monotonic() - start)
    if not ok:
        cleanup_sentinel(config, test_run_id)
        return

    start = time.monotonic()
    back = _wait_health(config, follower_uuid, {"Available"}, timeout=180)
    reporter.record(
        "Follower health Available", back,
        f"{follower_uuid[:8]}… rejoined Available" if back else "Timed out waiting for Available (180s)",
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = verify_sentinel(config, test_run_id)
    reporter.record("Sentinel persisted", ok, detail, time.monotonic() - start)

    check_quorum(config, reporter, expected_nodes=3)
    cleanup_sentinel(config, test_run_id)
    log.info("")


def run_leader(config: "StackConfig", reporter: "TestReporter") -> None:
    """Stop the current leader via SSM; verify a new leader is elected and sentinel persists."""
    import boto3

    log.info("=== leader ===")
    log.info("")

    cfn = boto3.client("cloudformation", region_name=config.region, config=_RETRY_CFG)
    asg = boto3.client("autoscaling", region_name=config.region, config=_RETRY_CFG)
    ssm = boto3.client("ssm", region_name=config.region, config=_RETRY_CFG)

    start = time.monotonic()
    ok, detail = preflight_healthy(config, expected_nodes=3)
    reporter.record("Preflight cluster healthy", ok, detail, time.monotonic() - start)
    if not ok:
        return

    test_run_id = uuid.uuid4().hex

    start = time.monotonic()
    ok, detail = write_sentinel(config, test_run_id)
    reporter.record("Write sentinel", ok, detail, time.monotonic() - start)
    if not ok:
        return

    start = time.monotonic()
    result = _discover_roles(cfn, asg, ssm, config, reporter, "Identify leader")
    if result is None:
        cleanup_sentinel(config, test_run_id)
        return
    roles, uuid_to_instance = result

    leader_uuid = next(
        (u for u, is_writer in roles.items() if is_writer and u in uuid_to_instance), None
    )
    if not leader_uuid:
        reporter.record("Identify leader", False, "No leader found in cluster", time.monotonic() - start)
        cleanup_sentinel(config, test_run_id)
        return

    target_instance = uuid_to_instance[leader_uuid]
    reporter.record(
        "Identify leader", True,
        f"UUID {leader_uuid[:8]}… → {target_instance}",
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = _stop_neo4j(ssm, target_instance)
    reporter.record("Stop leader", ok, detail, time.monotonic() - start)
    if not ok:
        cleanup_sentinel(config, test_run_id)
        return

    # Poll for a new leader. Swallow exceptions — NLB may briefly route to the
    # stopped leader before health checks remove it.
    start = time.monotonic()
    new_leader_uuid = None
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            rows = run_cypher_on_bastion(
                config,
                "SHOW DATABASE neo4j YIELD serverID, writer WHERE writer = true",
                database="system",
            )
            for row in rows:
                sid = row.get("serverID")
                if sid and sid != leader_uuid:
                    new_leader_uuid = sid
                    break
        except Exception:
            pass
        if new_leader_uuid:
            break
        time.sleep(5)

    reporter.record(
        "New leader elected", bool(new_leader_uuid),
        f"New leader: {new_leader_uuid[:8]}…" if new_leader_uuid else "No new leader within 120s",
        time.monotonic() - start,
    )
    if not new_leader_uuid:
        cleanup_sentinel(config, test_run_id)
        return

    start = time.monotonic()
    ok, detail = _start_neo4j(ssm, target_instance)
    reporter.record("Restart former leader", ok, detail, time.monotonic() - start)
    if not ok:
        cleanup_sentinel(config, test_run_id)
        return

    start = time.monotonic()
    rejoined = _wait_health(config, leader_uuid, {"Available"}, timeout=180)
    reporter.record(
        "Former leader rejoined", rejoined,
        f"{leader_uuid[:8]}… rejoined Available" if rejoined else "Timed out waiting for Available (180s)",
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = verify_sentinel(config, test_run_id)
    reporter.record("Sentinel persisted", ok, detail, time.monotonic() - start)

    check_quorum(config, reporter, expected_nodes=3)
    cleanup_sentinel(config, test_run_id)
    log.info("")


def run_rolling(config: "StackConfig", reporter: "TestReporter") -> None:
    """Serialized stop/start of follower, follower, leader; verify sentinel persists."""
    import boto3

    log.info("=== rolling ===")
    log.info("")

    cfn = boto3.client("cloudformation", region_name=config.region, config=_RETRY_CFG)
    asg = boto3.client("autoscaling", region_name=config.region, config=_RETRY_CFG)
    ssm = boto3.client("ssm", region_name=config.region, config=_RETRY_CFG)

    start = time.monotonic()
    ok, detail = preflight_healthy(config, expected_nodes=3)
    reporter.record("Preflight cluster healthy", ok, detail, time.monotonic() - start)
    if not ok:
        return

    test_run_id = uuid.uuid4().hex

    start = time.monotonic()
    ok, detail = write_sentinel(config, test_run_id)
    reporter.record("Write sentinel", ok, detail, time.monotonic() - start)
    if not ok:
        return

    # Classify roles once upfront — do not re-query between cycles.
    start = time.monotonic()
    result = _discover_roles(cfn, asg, ssm, config, reporter, "Classify cluster roles")
    if result is None:
        cleanup_sentinel(config, test_run_id)
        return
    roles, uuid_to_instance = result

    leader_uuid = next(
        (u for u, is_writer in roles.items() if is_writer and u in uuid_to_instance), None
    )
    follower_uuids = [
        u for u, is_writer in roles.items() if not is_writer and u in uuid_to_instance
    ]

    if not leader_uuid or len(follower_uuids) < 2:
        reporter.record(
            "Classify cluster roles", False,
            f"Need 1 leader + 2 followers; found leader={bool(leader_uuid)}, followers={len(follower_uuids)}",
            time.monotonic() - start,
        )
        cleanup_sentinel(config, test_run_id)
        return

    reporter.record(
        "Classify cluster roles", True,
        f"Leader: {leader_uuid[:8]}…  Followers: {follower_uuids[0][:8]}…, {follower_uuids[1][:8]}…",
        time.monotonic() - start,
    )

    # Bounce order: follower, follower, leader — leader last for maximum write stability.
    bounce_order = [
        (follower_uuids[0], "follower1"),
        (follower_uuids[1], "follower2"),
        (leader_uuid, "leader"),
    ]

    for cycle, (server_uuid, role_label) in enumerate(bounce_order, start=1):
        instance_id = uuid_to_instance[server_uuid]
        log.info("  Cycle %d/3: %s (%s)\n", cycle, server_uuid[:8] + "…", role_label)

        start = time.monotonic()
        ok, detail = _stop_neo4j(ssm, instance_id)
        reporter.record(f"Stop {role_label} (cycle {cycle})", ok, detail, time.monotonic() - start)
        if not ok:
            cleanup_sentinel(config, test_run_id)
            return

        start = time.monotonic()
        gone = _wait_health(config, server_uuid, {"Unavailable", "Unknown", "Down"}, timeout=90)
        reporter.record(
            f"Health Unavailable (cycle {cycle})", gone,
            f"{server_uuid[:8]}… went Unavailable" if gone else "Timed out (90s)",
            time.monotonic() - start,
        )

        start = time.monotonic()
        ok, detail = _start_neo4j(ssm, instance_id)
        reporter.record(f"Start {role_label} (cycle {cycle})", ok, detail, time.monotonic() - start)
        if not ok:
            cleanup_sentinel(config, test_run_id)
            return

        start = time.monotonic()
        back = _wait_health(config, server_uuid, {"Available"}, timeout=180)
        reporter.record(
            f"Health Available (cycle {cycle})", back,
            f"{server_uuid[:8]}… rejoined Available" if back else "Timed out (180s)",
            time.monotonic() - start,
        )

        quorum_ok = check_quorum(
            config, reporter, expected_nodes=3,
            label=f"Quorum check (after cycle {cycle})",
            expected_writer_uuid=leader_uuid if cycle < len(bounce_order) else None,
        )
        if not quorum_ok:
            log.info("  Aborting rolling test — quorum check failed after cycle %d.\n", cycle)
            cleanup_sentinel(config, test_run_id)
            return

    start = time.monotonic()
    ok, detail = verify_sentinel(config, test_run_id)
    reporter.record("Sentinel persisted", ok, detail, time.monotonic() - start)

    cleanup_sentinel(config, test_run_id)
    log.info("")


def run_reads(config: "StackConfig", reporter: "TestReporter") -> None:
    """Measure driver read failures during a follower bounce."""
    import boto3
    from botocore.exceptions import ClientError

    log.info("=== reads ===")
    log.info("")

    cfn = boto3.client("cloudformation", region_name=config.region, config=_RETRY_CFG)
    asg = boto3.client("autoscaling", region_name=config.region, config=_RETRY_CFG)
    ssm = boto3.client("ssm", region_name=config.region, config=_RETRY_CFG)

    start = time.monotonic()
    ok, detail = preflight_healthy(config, expected_nodes=3)
    reporter.record("Preflight cluster healthy", ok, detail, time.monotonic() - start)
    if not ok:
        return

    start = time.monotonic()
    discovery = _discover_roles(cfn, asg, ssm, config, reporter, "Identify follower")
    if discovery is None:
        return
    roles, uuid_to_instance = discovery

    follower_uuid = next(
        (u for u, is_writer in roles.items() if not is_writer and u in uuid_to_instance), None
    )
    if not follower_uuid:
        reporter.record("Identify follower", False, "No follower found", time.monotonic() - start)
        return

    target_instance = uuid_to_instance[follower_uuid]
    reporter.record(
        "Identify follower", True,
        f"UUID {follower_uuid[:8]}… → {target_instance}",
        time.monotonic() - start,
    )

    # Fire the read probe on the bastion async — ssm.send_command returns immediately.
    n_iter = 30
    b64_probe = base64.b64encode(_READS_PROBE.encode()).decode()
    probe_cmd = (
        f"echo {b64_probe} | base64 -d > /tmp/vp_probe.py && "
        f"python3.11 /tmp/vp_probe.py {config.stack_name} {config.region} "
        f"{config.bolt_scheme} {n_iter}"
    )

    start = time.monotonic()
    try:
        resp = ssm.send_command(
            InstanceIds=[config.bastion_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [probe_cmd]},
        )
    except ClientError as exc:
        reporter.record("Launch read probe", False, str(exc), time.monotonic() - start)
        return

    probe_cmd_id = resp["Command"]["CommandId"]
    reporter.record(
        "Launch read probe", True,
        f"Command {probe_cmd_id}, {n_iter} iterations at 1s cadence",
        time.monotonic() - start,
    )

    # Brief pause so the probe begins reading before the follower goes down.
    time.sleep(2)

    start = time.monotonic()
    ok, detail = _stop_neo4j(ssm, target_instance)
    reporter.record("Stop follower", ok, detail, time.monotonic() - start)
    if not ok:
        return

    start = time.monotonic()
    gone = _wait_health(config, follower_uuid, {"Unavailable", "Unknown", "Down"}, timeout=90)
    reporter.record(
        "Follower health Unavailable", gone,
        f"{follower_uuid[:8]}… went Unavailable" if gone else "Timed out (90s)",
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = _start_neo4j(ssm, target_instance)
    reporter.record("Start follower", ok, detail, time.monotonic() - start)
    if not ok:
        return

    start = time.monotonic()
    back = _wait_health(config, follower_uuid, {"Available"}, timeout=180)
    reporter.record(
        "Follower health Available", back,
        f"{follower_uuid[:8]}… rejoined Available" if back else "Timed out (180s)",
        time.monotonic() - start,
    )

    # Collect probe results. The stop/start cycle takes longer than the 30s probe run,
    # so the probe is already done by now; run_ssm_command returns immediately.
    start = time.monotonic()
    status, stdout, stderr = run_ssm_command(ssm, probe_cmd_id, config.bastion_id, timeout_s=120)

    if status != "Success":
        reporter.record(
            "Read probe result", False,
            f"Probe failed (status={status}): {stderr}",
            time.monotonic() - start,
        )
        return

    try:
        probe_result = json.loads(stdout)
        failures = probe_result.get("failures", -1)
        exc_types = probe_result.get("exception_types", [])
        n = probe_result.get("iterations", n_iter)
        passed = failures == 0
        if exc_types:
            detail = f"{failures} of {n} reads failed ({', '.join(exc_types)})"
        else:
            detail = f"{failures} of {n} reads failed"
        reporter.record("Read probe result", passed, detail, time.monotonic() - start)
    except (json.JSONDecodeError, Exception) as exc:
        reporter.record(
            "Read probe result", False,
            f"Could not parse probe output: {exc}",
            time.monotonic() - start,
        )

    log.info("")
