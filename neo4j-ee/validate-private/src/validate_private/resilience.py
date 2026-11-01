"""Resilience test cases for EE Private clusters.

single-loss: terminate one cluster node; verify volume reattach + data persistence
             + quorum reform.
total-loss:  terminate all three nodes simultaneously; verify all three volumes
             reattach + sentinel intact + quorum from retained volumes.

Both cases use a marker file dropped on the data volume before termination.
Marker present on the replacement instance = volume was reattached (not reformatted).
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
import uuid
from typing import TYPE_CHECKING

from botocore.config import Config
from validate_private.aws_helpers import asg_instances as _asg_instances
from validate_private.aws_helpers import stack_resources as _stack_resources
from validate_private.quorum import check_quorum
from validate_private.runner import run_cypher, run_shell_on_instance
from validate_private.sentinel import cleanup_sentinel, verify_sentinel, write_sentinel

if TYPE_CHECKING:
    from validate_private.config import StackConfig
    from validate_private.reporting import TestReporter

log = logging.getLogger(__name__)

_MARKER_PATH = "/var/lib/neo4j/data/.resilience-marker-{}"
_ASG_LOGICAL_IDS = ("Neo4jNode1ASG", "Neo4jNode2ASG", "Neo4jNode3ASG")
_RETRY_CFG = Config(retries={"mode": "standard"})


# ---------------------------------------------------------------------------
# AWS helpers — accept pre-created clients; no boto3 imports inside helpers
# ---------------------------------------------------------------------------

def _terminate_instance(ec2, instance_id: str) -> None:
    ec2.terminate_instances(InstanceIds=[instance_id])


def _wait_for_new_instance(
    asg,
    asg_name: str,
    prior_ids: set[str],
    timeout: int,
) -> str:
    """Poll until the ASG has an InService instance not in prior_ids. Returns the new ID."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        groups = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"]
        instances = (
            [i["InstanceId"] for i in groups[0]["Instances"] if i["LifecycleState"] == "InService"]
            if groups else []
        )
        candidates = [i for i in instances if i not in prior_ids]
        if candidates:
            return candidates[0]
        time.sleep(15)
    raise TimeoutError(
        f"ASG {asg_name} did not produce a replacement within {timeout}s. "
        "Check ASG activity history and instance UserData logs."
    )


def _wait_all_replaced(
    asg,
    asg_prior: dict[str, set[str]],
    timeout: int,
) -> dict[str, str]:
    """Wait until every ASG in asg_prior has a new InService instance.

    asg_prior maps asg_name -> set of instance IDs that existed before termination.
    Returns asg_name -> new_instance_id.
    """
    deadline = time.monotonic() + timeout
    remaining = dict(asg_prior)
    new_instances: dict[str, str] = {}

    while remaining and time.monotonic() < deadline:
        for asg_name, prior_ids in list(remaining.items()):
            groups = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])["AutoScalingGroups"]
            instances = (
                [i["InstanceId"] for i in groups[0]["Instances"] if i["LifecycleState"] == "InService"]
                if groups else []
            )
            candidates = [i for i in instances if i not in prior_ids]
            if candidates:
                new_instances[asg_name] = candidates[0]
                del remaining[asg_name]
                log.info("  Replacement ready: %s → %s", asg_name, candidates[0])
        if remaining:
            time.sleep(15)

    if remaining:
        missing = ", ".join(remaining)
        raise TimeoutError(
            f"ASGs [{missing}] did not produce replacements within {timeout}s. "
            "This may mean the cluster never reformed quorum — check ASG activity "
            "and /var/log/cloud-init-output.log on partial replacements."
        )
    return new_instances


def _wait_ssm_online(ssm, instance_id: str, timeout: int = 300) -> bool:
    """Poll SSM until the instance PingStatus is Online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        items = resp.get("InstanceInformationList", [])
        if items and items[0].get("PingStatus") == "Online":
            return True
        time.sleep(10)
    return False


def _wait_volume_mounted(ssm, instance_id: str, timeout: int = 300) -> bool:
    """Poll via SSM until /var/lib/neo4j/data is a mountpoint on the instance."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, stdout, _ = run_shell_on_instance(
            ssm, instance_id,
            "mountpoint -q /var/lib/neo4j/data && echo MOUNTED || echo NOT_MOUNTED",
            timeout_s=30,
        )
        if ok and stdout.strip() == "MOUNTED":
            return True
        time.sleep(15)
    return False


# ---------------------------------------------------------------------------
# Marker file: volume reattach verification
# ---------------------------------------------------------------------------

def _drop_marker(ssm, instance_id: str, marker_id: str) -> bool:
    """Touch the marker file on the data volume via SSM. Returns True on success."""
    path = _MARKER_PATH.format(marker_id)
    ok, _, err = run_shell_on_instance(ssm, instance_id, f"touch {path} && echo ok")
    if not ok:
        log.warning("  Failed to drop marker on %s: %s", instance_id, err)
    return ok


def _check_marker(ssm, instance_id: str, marker_id: str) -> bool:
    """Return True if the marker file exists on the instance's data volume."""
    path = _MARKER_PATH.format(marker_id)
    ok, stdout, _ = run_shell_on_instance(
        ssm, instance_id, f"test -f {path} && echo FOUND || echo MISSING"
    )
    return ok and "FOUND" in stdout


# ---------------------------------------------------------------------------
# Neo4j checks over the Bolt tunnel
# ---------------------------------------------------------------------------

def _wait_neo4j(config: "StackConfig", timeout: int) -> bool:
    """Retry a trivial Cypher query over the Bolt tunnel until Neo4j is reachable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            rows = run_cypher(config, "RETURN 1 AS ok", timeout_s=30)
            if rows and rows[0].get("ok") == 1:
                return True
        except Exception:
            pass
        time.sleep(15)
    return False


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def run_single_loss(
    config: "StackConfig",
    reporter: "TestReporter",
    timeout: int = 900,
) -> None:
    """Terminate one cluster node and verify volume reattach + data persistence."""
    import boto3

    log.info("=== Single-loss resilience test ===")
    log.info("")

    cfn = boto3.client("cloudformation", region_name=config.region, config=_RETRY_CFG)
    asg = boto3.client("autoscaling", region_name=config.region, config=_RETRY_CFG)
    ec2 = boto3.client("ec2", region_name=config.region, config=_RETRY_CFG)
    ssm = boto3.client("ssm", region_name=config.region, config=_RETRY_CFG)

    resources = _stack_resources(cfn, config.stack_name)
    asg_map = {k: resources[k] for k in _ASG_LOGICAL_IDS if k in resources}

    if len(asg_map) < 2:
        log.info(
            "Single-node deployment (found %d cluster ASG(s)) — "
            "single-loss test requires a 3-node cluster. Skipping.\n",
            len(asg_map),
        )
        return

    test_run_id = uuid.uuid4().hex

    start = time.monotonic()
    ok, detail = write_sentinel(config, test_run_id)
    reporter.record("Write sentinel", ok, detail, time.monotonic() - start)
    if not ok:
        return

    target_asg = asg_map[_ASG_LOGICAL_IDS[0]]
    prior_instances = _asg_instances(asg, target_asg)
    if not prior_instances:
        reporter.record("Find target instance", False, f"No InService instances in {target_asg}", 0)
        return
    target = prior_instances[0]
    log.info("  Target: %s (ASG: %s)\n", target, target_asg)

    start = time.monotonic()
    ok = _drop_marker(ssm, target, test_run_id)
    reporter.record(
        "Drop volume marker", ok,
        f"Marker written on {target}" if ok else f"Could not write marker on {target}",
        time.monotonic() - start,
    )
    if not ok:
        return

    start = time.monotonic()
    try:
        _terminate_instance(ec2, target)
        reporter.record("Terminate cluster node", True, f"Terminated {target}", time.monotonic() - start)
    except Exception as exc:
        reporter.record("Terminate cluster node", False, str(exc), time.monotonic() - start)
        return

    log.info("  Waiting for ASG replacement (timeout: %ds)…\n", timeout)
    start = time.monotonic()
    try:
        new_instance = _wait_for_new_instance(asg, target_asg, set(prior_instances), timeout)
        reporter.record(
            "ASG replacement InService", True,
            f"{target} → {new_instance}",
            time.monotonic() - start,
        )
    except TimeoutError as exc:
        reporter.record("ASG replacement InService", False, str(exc), time.monotonic() - start)
        return

    log.info("  Waiting for SSM on %s…\n", new_instance)
    start = time.monotonic()
    ok = _wait_ssm_online(ssm, new_instance, timeout=300)
    reporter.record(
        "Replacement SSM ready", ok,
        f"{new_instance} is SSM Online" if ok else f"{new_instance} not SSM Online within 300s",
        time.monotonic() - start,
    )
    if not ok:
        return

    log.info("  Waiting for Neo4j post-recovery (timeout: 300s)…\n")
    start = time.monotonic()
    ok = _wait_neo4j(config, timeout=300)
    reporter.record(
        "Neo4j reachable post-recovery", ok,
        "Accepting Cypher over the Bolt tunnel" if ok else "Neo4j not reachable within 300s",
        time.monotonic() - start,
    )
    if not ok:
        return

    # Wait for data volume mounted on replacement, then check marker.
    # _wait_neo4j only waits for *any* cluster member to respond via NLB; the
    # replacement's UserData may still be running. Poll mountpoint directly.
    start = time.monotonic()
    vol_ready = _wait_volume_mounted(ssm, new_instance, timeout=300)
    found = _check_marker(ssm, new_instance, test_run_id) if vol_ready else False
    reporter.record(
        "Volume reattach (marker)",
        found,
        "Marker present — data volume was reattached" if found
        else ("Data volume not mounted within 300s" if not vol_ready
              else "Marker MISSING — volume was reformatted or wrong volume attached"),
        time.monotonic() - start,
    )

    start = time.monotonic()
    ok, detail = verify_sentinel(config, test_run_id)
    reporter.record("Sentinel persisted", ok, detail, time.monotonic() - start)

    check_quorum(config, reporter, expected_nodes=len(asg_map))
    cleanup_sentinel(config, test_run_id)
    log.info("")


def run_total_loss(
    config: "StackConfig",
    reporter: "TestReporter",
    timeout: int = 1200,
) -> None:
    """Terminate all cluster nodes simultaneously and verify full cluster recovery."""
    import boto3

    log.info("=== Total-loss resilience test ===")
    log.info("")

    cfn = boto3.client("cloudformation", region_name=config.region, config=_RETRY_CFG)
    asg = boto3.client("autoscaling", region_name=config.region, config=_RETRY_CFG)
    ec2 = boto3.client("ec2", region_name=config.region, config=_RETRY_CFG)
    ssm = boto3.client("ssm", region_name=config.region, config=_RETRY_CFG)

    resources = _stack_resources(cfn, config.stack_name)
    asg_map = {k: resources[k] for k in _ASG_LOGICAL_IDS if k in resources}

    if len(asg_map) < 3:
        log.info(
            "Found %d cluster ASG(s) — total-loss test requires all 3. "
            "Is this a single-node or partial-cluster deployment? Skipping.\n",
            len(asg_map),
        )
        return

    test_run_id = uuid.uuid4().hex

    start = time.monotonic()
    ok, detail = write_sentinel(config, test_run_id)
    reporter.record("Write sentinel", ok, detail, time.monotonic() - start)
    if not ok:
        return

    asg_prior: dict[str, set[str]] = {}
    target_instances: dict[str, str] = {}

    for logical, asg_name in asg_map.items():
        instances = _asg_instances(asg, asg_name)
        if not instances:
            reporter.record(
                "Find cluster instances", False,
                f"No InService instances in {asg_name} ({logical})", 0,
            )
            return
        asg_prior[asg_name] = set(instances)
        target_instances[logical] = instances[0]

    log.info("  Targets: %s\n", ", ".join(target_instances.values()))

    # Drop markers in parallel — each is a 120s-timeout SSM command; sequential
    # would triple the worst-case time.
    t0 = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as exe:
        drop_futs = {
            exe.submit(_drop_marker, ssm, iid, test_run_id): (logical, iid)
            for logical, iid in target_instances.items()
        }
        marker_ok = True
        for fut in concurrent.futures.as_completed(drop_futs):
            logical, iid = drop_futs[fut]
            ok = fut.result()
            reporter.record(
                f"Drop marker ({logical})", ok,
                f"Marker on {iid}" if ok else f"Could not write marker on {iid}",
                time.monotonic() - t0,
            )
            if not ok:
                marker_ok = False
    if not marker_ok:
        return

    # Terminate all three in parallel so they go down as simultaneously as possible.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as exe:
        term_futs = {
            exe.submit(_terminate_instance, ec2, iid): (logical, iid, time.monotonic())
            for logical, iid in target_instances.items()
        }
        for fut in concurrent.futures.as_completed(term_futs):
            logical, iid, t_start = term_futs[fut]
            try:
                fut.result()
                reporter.record(f"Terminate {logical}", True, f"Terminated {iid}", time.monotonic() - t_start)
            except Exception as exc:
                reporter.record(f"Terminate {logical}", False, str(exc), time.monotonic() - t_start)

    log.info("  Waiting for all 3 ASG replacements (timeout: %ds)…\n", timeout)
    start = time.monotonic()
    try:
        new_instances = _wait_all_replaced(asg, asg_prior, timeout)
        reporter.record(
            "All ASGs replaced", True,
            f"New instances: {', '.join(new_instances.values())}",
            time.monotonic() - start,
        )
    except TimeoutError as exc:
        reporter.record("All ASGs replaced", False, str(exc), time.monotonic() - start)
        return

    # Wait for SSM online in parallel — marker checks are deferred until after
    # Neo4j is up, when the data volume is guaranteed to be mounted.
    logical_map = {v: k for k, v in asg_map.items()}

    def _wait_ssm(asg_name: str, new_instance: str) -> tuple[str, str, bool, float]:
        logical = logical_map[asg_name]
        t_ssm = time.monotonic()
        ssm_ok = _wait_ssm_online(ssm, new_instance, timeout=300)
        return logical, new_instance, ssm_ok, time.monotonic() - t_ssm

    log.info("  Waiting for SSM on all replacements…\n")
    ssm_ready: dict[str, str] = {}  # logical_id -> new_instance
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as exe:
        ssm_futs = [
            exe.submit(_wait_ssm, asg_name, new_instance)
            for asg_name, new_instance in new_instances.items()
        ]
        for fut in concurrent.futures.as_completed(ssm_futs):
            logical, new_instance, ssm_ok, ssm_elapsed = fut.result()
            reporter.record(
                f"SSM ready ({logical})", ssm_ok,
                f"{new_instance} Online" if ssm_ok else f"{new_instance} not Online within 300s",
                ssm_elapsed,
            )
            if ssm_ok:
                ssm_ready[logical] = new_instance

    # All 3 nodes must start and elect a leader before Neo4j accepts queries.
    log.info("  Waiting for Neo4j quorum post-recovery (timeout: 600s)…\n")
    start = time.monotonic()
    ok = _wait_neo4j(config, timeout=600)
    reporter.record(
        "Neo4j reachable post-recovery", ok,
        "Accepting Cypher over the Bolt tunnel" if ok
        else (
            "Neo4j not reachable within 600s — cluster did not reform quorum. "
            "Check /var/log/cloud-init-output.log and /var/log/neo4j/debug.log "
            "on each replacement instance."
        ),
        time.monotonic() - start,
    )
    if not ok:
        return

    # Check markers: wait for data volume mounted on each replacement, then check.
    # _wait_neo4j only waits for any cluster member via NLB; individual nodes
    # may still be running UserData. Poll mountpoint directly on each.
    def _check_vol(logical: str, new_instance: str) -> tuple[str, bool, float]:
        t = time.monotonic()
        vol_ready = _wait_volume_mounted(ssm, new_instance, timeout=300)
        found = _check_marker(ssm, new_instance, test_run_id) if vol_ready else False
        return logical, found, vol_ready, time.monotonic() - t

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as exe:
        vol_futs = [
            exe.submit(_check_vol, logical, inst)
            for logical, inst in ssm_ready.items()
        ]
        for fut in concurrent.futures.as_completed(vol_futs):
            logical, found, vol_ready, elapsed = fut.result()
            reporter.record(
                f"Volume reattach ({logical})",
                found,
                "Marker present" if found
                else ("Data volume not mounted within 300s" if not vol_ready
                      else "Marker MISSING — volume may have been reformatted"),
                elapsed,
            )

    start = time.monotonic()
    ok, detail = verify_sentinel(config, test_run_id)
    reporter.record("Sentinel persisted", ok, detail, time.monotonic() - start)

    check_quorum(config, reporter, expected_nodes=len(asg_map))
    cleanup_sentinel(config, test_run_id)
    log.info("")
