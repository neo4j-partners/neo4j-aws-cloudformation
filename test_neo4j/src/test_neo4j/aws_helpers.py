"""AWS operations via boto3: ASG lookup, instance termination, replacement polling."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import boto3

log = logging.getLogger(__name__)


def get_stack_resources(session: boto3.Session, stack_name: str) -> dict[str, str]:
    """Return a mapping of LogicalResourceId -> PhysicalResourceId for the stack."""
    cfn = session.client("cloudformation")
    paginator = cfn.get_paginator("list_stack_resources")
    return {
        r["LogicalResourceId"]: r["PhysicalResourceId"]
        for page in paginator.paginate(StackName=stack_name)
        for r in page["StackResourceSummaries"]
    }


def get_asg_instance_id(
    session: boto3.Session,
    stack_name: str,
    resource_map: dict[str, str] | None = None,
    exclude_instance: str | None = None,
    asg_logical_id: str = "Neo4jAutoScalingGroup",
) -> str:
    """Return the EC2 instance ID of the first InService instance in the stack's ASG.

    Skips *exclude_instance* if provided.
    """
    if resource_map is None:
        resource_map = get_stack_resources(session, stack_name)

    asg_name = resource_map.get(asg_logical_id)
    if not asg_name:
        raise RuntimeError(f"{asg_logical_id} not found in stack {stack_name}")

    asg_client = session.client("autoscaling")
    groups = asg_client.describe_auto_scaling_groups(
        AutoScalingGroupNames=[asg_name]
    )["AutoScalingGroups"]

    if not groups:
        raise RuntimeError(f"ASG {asg_name} not found")

    instances = groups[0]["Instances"]
    for instance in instances:
        if instance["LifecycleState"] == "InService" and instance["InstanceId"] != exclude_instance:
            return instance["InstanceId"]

    states = [f"{i['InstanceId']}={i['LifecycleState']}" for i in instances]
    raise RuntimeError(
        f"No InService instance in ASG {asg_name}. "
        f"Current states: {', '.join(states) or 'none'}"
    )


def get_all_ee_asg_instance_ids(
    session: boto3.Session,
    stack_name: str,
    resource_map: dict[str, str],
    number_of_servers: int,
) -> list[tuple[str, str]]:
    """Return [(asg_logical_id, instance_id), ...] for each EE node ASG.

    Iterates Neo4jNode1ASG through Neo4jNode{number_of_servers}ASG.
    """
    result = []
    for n in range(1, number_of_servers + 1):
        asg_logical_id = f"Neo4jNode{n}ASG"
        instance_id = get_asg_instance_id(
            session, stack_name, resource_map, asg_logical_id=asg_logical_id
        )
        result.append((asg_logical_id, instance_id))
    return result


def terminate_instance(session: boto3.Session, instance_id: str) -> None:
    """Terminate an EC2 instance. The ASG will launch a replacement."""
    ec2 = session.client("ec2")
    ec2.terminate_instances(InstanceIds=[instance_id])
    log.info("  Terminated instance %s", instance_id)


def wait_for_replacement_instance(
    session: boto3.Session,
    stack_name: str,
    resource_map: dict[str, str],
    *,
    exclude_instance: str | None = None,
    timeout: int = 600,
    interval: int = 15,
    asg_logical_id: str = "Neo4jAutoScalingGroup",
) -> str:
    """Poll the ASG until a new InService instance appears. Return its instance ID.

    If *exclude_instance* is given, ignore that ID (the just-terminated instance
    may briefly remain in the ASG).
    """
    asg_name = resource_map.get(asg_logical_id)
    if not asg_name:
        raise RuntimeError(f"{asg_logical_id} not found in stack {stack_name}")

    asg_client = session.client("autoscaling")
    deadline = time.monotonic() + timeout

    while True:
        try:
            groups = asg_client.describe_auto_scaling_groups(
                AutoScalingGroupNames=[asg_name]
            )["AutoScalingGroups"]

            if groups:
                for instance in groups[0]["Instances"]:
                    iid = instance["InstanceId"]
                    state = instance["LifecycleState"]
                    if state == "InService" and iid != exclude_instance:
                        elapsed = timeout - (deadline - time.monotonic())
                        log.info(
                            "  Replacement %s is InService (%.0fs elapsed)", iid, elapsed
                        )
                        return iid

                elapsed = timeout - (deadline - time.monotonic())
                states = [
                    f"{i['InstanceId']}={i['LifecycleState']}"
                    for i in groups[0]["Instances"]
                ]
                if states:
                    log.info("  Instances: %s (%.0fs elapsed)", ", ".join(states), elapsed)
                else:
                    log.info("  No instances in ASG yet (%.0fs elapsed)", elapsed)

        except Exception as exc:
            elapsed = timeout - (deadline - time.monotonic())
            log.info("  API call failed (%.0fs elapsed): %s — retrying", elapsed, exc)

        if time.monotonic() >= deadline:
            break

        time.sleep(interval)

    raise TimeoutError(
        f"No InService replacement instance in ASG {asg_name} after {timeout}s. "
        "Check ASG activity and instance logs."
    )
