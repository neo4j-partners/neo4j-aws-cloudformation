"""Resilience tests: EBS persistence across ASG instance replacement."""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from neo4j import RoutingControl

from test_neo4j.aws_helpers import (
    get_asg_instance_id,
    get_all_ee_asg_instance_ids,
    get_stack_resources,
    terminate_instance,
    wait_for_replacement_instance,
)
from test_neo4j.config import StackConfig
from test_neo4j.movies_dataset import (
    cleanup_movies_dataset,
    create_movies_dataset,
    verify_movies_dataset,
)
from test_neo4j.neo4j_checks import run_simple_tests
from test_neo4j.reporting import TestReporter
from test_neo4j.volume_checks import run_volume_checks
from test_neo4j.wait import wait_for_neo4j

if TYPE_CHECKING:
    import boto3

log = logging.getLogger(__name__)


def _write_sentinel(config: StackConfig, reporter: TestReporter, test_run_id: str) -> bool:
    """Create a sentinel node and verify it was written. Return True on success."""
    with reporter.test("Write sentinel data") as ctx:
        try:
            with config.driver() as driver:
                driver.execute_query(
                    "CREATE (s:Sentinel {test_id: $tid, value: $val})",
                    tid=test_run_id,
                    val="persistence-check",
                )
                records, _, _ = driver.execute_query(
                    "MATCH (s:Sentinel {test_id: $tid}) RETURN s.value AS value",
                    tid=test_run_id,
                )
                if records and records[0]["value"] == "persistence-check":
                    ctx.pass_(f"Sentinel node created (test_id={test_run_id[:8]}...)")
                    return True
                ctx.fail("Sentinel node not found immediately after creation")
                return False
        except Exception as exc:
            ctx.fail(f"Failed to write sentinel data: {exc}")
            return False


def _terminate_and_wait(
    config: StackConfig,
    reporter: TestReporter,
    session: boto3.Session,
    resource_map: dict[str, str],
    replacement_timeout: int,
    asg_logical_id: str = "Neo4jAutoScalingGroup",
) -> bool:
    """Terminate the current instance and wait for a healthy replacement. Return True on success."""
    original_instance_id = get_asg_instance_id(
        session, config.stack_name, resource_map, asg_logical_id=asg_logical_id
    )
    log.info("  Original instance: %s\n", original_instance_id)

    with reporter.test("Terminate EC2 instance") as ctx:
        try:
            terminate_instance(session, original_instance_id)
            ctx.pass_(f"Terminated {original_instance_id}")
        except Exception as exc:
            ctx.fail(f"Failed to terminate instance: {exc}")
            return False

    with reporter.test("Wait for ASG replacement") as ctx:
        try:
            new_instance_id = wait_for_replacement_instance(
                session,
                config.stack_name,
                resource_map,
                exclude_instance=original_instance_id,
                timeout=replacement_timeout,
                asg_logical_id=asg_logical_id,
            )
            ctx.pass_(
                f"Replacement {new_instance_id} is InService "
                f"(was {original_instance_id})"
            )
        except Exception as exc:
            ctx.fail(str(exc))
            return False

    log.info("Waiting for Neo4j on the replacement instance...")
    if not wait_for_neo4j(config, timeout=300, interval=10):
        with reporter.test("Post-recovery Neo4j readiness") as ctx:
            ctx.fail("Neo4j did not become reachable on the replacement within 300s")
        return False
    return True


def _verify_sentinel(config: StackConfig, reporter: TestReporter, test_run_id: str) -> None:
    """Check that the sentinel node survived instance replacement."""
    with reporter.test("Verify sentinel data persisted") as ctx:
        try:
            with config.driver() as driver:
                records, _, _ = driver.execute_query(
                    "MATCH (s:Sentinel {test_id: $tid}) RETURN s.value AS value",
                    tid=test_run_id,
                )
                if not records:
                    ctx.fail(
                        "Sentinel node NOT found after instance replacement. "
                        "EBS data volume was lost or reformatted."
                    )
                elif records[0]["value"] == "persistence-check":
                    ctx.pass_(
                        f"Sentinel node persisted across instance replacement "
                        f"(test_id={test_run_id[:8]}...)"
                    )
                else:
                    ctx.fail(f"Unexpected sentinel value: {records[0]['value']}")
        except Exception as exc:
            ctx.fail(f"Failed to query sentinel data: {exc}")


def _cleanup_sentinel(config: StackConfig, test_run_id: str) -> None:
    """Delete the sentinel node (best-effort, does not affect test results)."""
    try:
        with config.driver() as driver:
            driver.execute_query(
                "MATCH (s:Sentinel {test_id: $tid}) DELETE s",
                tid=test_run_id,
            )
        log.info("  Cleaned up sentinel node.\n")
    except Exception:
        log.warning("  Could not clean up sentinel node (non-fatal).\n")


def run_ee_cluster_resilience_tests(
    config: StackConfig,
    reporter: TestReporter,
    session: boto3.Session,
    replacement_timeout: int = 600,
    resource_map: dict[str, str] | None = None,
) -> None:
    """EE resilience: terminate a follower, verify cluster re-forms, verify data persisted.

    For single-node EE, falls back to the CE-style EBS persistence test.
    """
    from test_neo4j.cluster_checks import check_cluster_topology  # noqa: PLC0415

    if resource_map is None:
        resource_map = get_stack_resources(session, config.stack_name)

    if config.number_of_servers == 1:
        # Single-node EE: same EBS persistence flow as CE but with EE
        # logical IDs (Neo4jNode1ASG / Neo4jNode1DataVolume).
        from test_neo4j.volume_checks import run_ee_volume_checks  # noqa: PLC0415

        run_resilience_tests(
            config, reporter, session, replacement_timeout, resource_map,
            asg_logical_id="Neo4jNode1ASG",
            volume_check_fn=run_ee_volume_checks,
        )
        return

    test_run_id = uuid.uuid4().hex

    if not _write_sentinel(config, reporter, test_run_id):
        return

    # Pick node 2 as the instance to terminate. All bolt-advertised addresses
    # point to the NLB DNS (not per-node private IPs), so we cannot map Neo4j
    # server roles to specific EC2 instances via Cypher. Node 2 is a valid
    # choice: a 3-node Raft cluster survives losing any single node, regardless
    # of whether it is the current leader.
    node_pairs = get_all_ee_asg_instance_ids(
        session, config.stack_name, resource_map, config.number_of_servers
    )

    follower_instance_id: str | None = None
    follower_asg_logical_id: str | None = None

    with reporter.test("Select node 2 for termination") as ctx:
        for asg_logical_id, instance_id in node_pairs:
            if asg_logical_id == "Neo4jNode2ASG":
                follower_instance_id = instance_id
                follower_asg_logical_id = asg_logical_id
                break
        if follower_instance_id:
            ctx.pass_(
                f"Selected {follower_instance_id} "
                f"({follower_asg_logical_id}) for termination"
            )
        else:
            ctx.fail("Could not find Neo4jNode2ASG instance")
            return

    with reporter.test("Terminate follower EC2 instance") as ctx:
        try:
            terminate_instance(session, follower_instance_id)
            ctx.pass_(f"Terminated {follower_instance_id}")
        except Exception as exc:
            ctx.fail(f"Failed to terminate instance: {exc}")
            return

    with reporter.test("Wait for follower ASG replacement") as ctx:
        try:
            new_instance_id = wait_for_replacement_instance(
                session,
                config.stack_name,
                resource_map,
                exclude_instance=follower_instance_id,
                timeout=replacement_timeout,
                asg_logical_id=follower_asg_logical_id,
            )
            ctx.pass_(
                f"Replacement {new_instance_id} is InService "
                f"(replaced {follower_instance_id})"
            )
        except Exception as exc:
            ctx.fail(str(exc))
            return

    log.info("Waiting for Neo4j cluster to stabilize...")
    if not wait_for_neo4j(config, timeout=300, interval=10):
        with reporter.test("Post-recovery Neo4j readiness") as ctx:
            ctx.fail("Neo4j did not become reachable after follower replacement within 300s")
        return

    # HTTP up doesn't mean the replacement has rejoined the Raft quorum yet.
    # Poll until all expected members are Enabled/Available (up to 120s).
    log.info("  Waiting for cluster to reach %d Enabled members...\n", config.number_of_servers)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with config.driver() as driver:
                records, _, _ = driver.execute_query(
                    "SHOW SERVERS YIELD state, health",
                    routing_=RoutingControl.READ,
                )
                enabled = [
                    r for r in records
                    if r["state"] == "Enabled" and r["health"] == "Available"
                ]
                if len(enabled) >= config.number_of_servers:
                    break
        except Exception:
            pass
        time.sleep(10)

    check_cluster_topology(config, reporter)
    from test_neo4j.robust_checks import check_license_files_on_disk  # noqa: PLC0415

    check_license_files_on_disk(session, config, reporter, resource_map)
    _verify_sentinel(config, reporter, test_run_id)
    _cleanup_sentinel(config, test_run_id)


def run_resilience_tests(
    config: StackConfig,
    reporter: TestReporter,
    session: boto3.Session,
    replacement_timeout: int = 600,
    resource_map: dict[str, str] | None = None,
    asg_logical_id: str = "Neo4jAutoScalingGroup",
    volume_check_fn = None,
) -> None:
    """EBS persistence test: write data, terminate instance, verify data survived on replacement."""
    test_run_id = uuid.uuid4().hex

    if resource_map is None:
        resource_map = get_stack_resources(session, config.stack_name)

    if volume_check_fn is None:
        volume_check_fn = run_volume_checks
    volume_check_fn(config, reporter, session, resource_map)

    if not _write_sentinel(config, reporter, test_run_id):
        return

    if not create_movies_dataset(config, reporter):
        return

    if not _terminate_and_wait(
        config, reporter, session, resource_map, replacement_timeout,
        asg_logical_id=asg_logical_id,
    ):
        return

    run_simple_tests(config, reporter)
    from test_neo4j.robust_checks import check_license_files_on_disk  # noqa: PLC0415

    check_license_files_on_disk(session, config, reporter, resource_map)

    _verify_sentinel(config, reporter, test_run_id)
    verify_movies_dataset(config, reporter)

    _cleanup_sentinel(config, test_run_id)
    cleanup_movies_dataset(config)
