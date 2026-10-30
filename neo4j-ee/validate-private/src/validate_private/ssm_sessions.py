"""CLI entry point for scripts/ssm_check_sessions.py."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys

import boto3


_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"
sys.path.insert(0, str(_NEO4J_EE_DIR / "src"))
from neo4j_ee.outputs import (  # noqa: E402
    read_outputs,
    require_field,
    require_private_mode,
    resolve_outputs_file,
)
_ASG_FIELDS = ("Neo4jNode1ASGName", "Neo4jNode2ASGName", "Neo4jNode3ASGName")


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    owner: str
    start_date: str
    status: str


def _resolve_outputs_path(stack: str | None) -> Path:
    try:
        return resolve_outputs_file(_DEPLOY_DIR, stack)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{exc}. Run deploy.py first, or pass a stack name."
        ) from exc


def _in_service_instances(autoscaling, asg_name: str) -> list[str]:
    response = autoscaling.describe_auto_scaling_groups(
        AutoScalingGroupNames=[asg_name],
    )
    groups = response.get("AutoScalingGroups", [])
    if not groups:
        return []
    return [
        instance["InstanceId"]
        for instance in groups[0].get("Instances", [])
        if instance.get("LifecycleState") == "InService"
    ]


def _active_sessions(ssm, instance_id: str) -> list[SessionInfo]:
    paginator = ssm.get_paginator("describe_sessions")
    sessions: list[SessionInfo] = []
    for page in paginator.paginate(
        State="Active",
        Filters=[{"key": "Target", "value": instance_id}],
    ):
        for session in page.get("Sessions", []):
            sessions.append(
                SessionInfo(
                    session_id=session.get("SessionId", ""),
                    owner=session.get("Owner", ""),
                    start_date=str(session.get("StartDate", "")),
                    status=session.get("Status", ""),
                )
            )
    return sessions


def _print_sessions(sessions: list[SessionInfo]) -> None:
    if not sessions:
        print("  (none)")
        return

    rows = [
        ("SessionId", "Owner", "StartDate", "Status"),
        *[
            (session.session_id, session.owner, session.start_date, session.status)
            for session in sessions
        ],
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for index, row in enumerate(rows):
        print("  " + "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)))
        if index == 0:
            print("  " + "  ".join("-" * width for width in widths))


def _print_local_ports() -> None:
    result = subprocess.run(
        ["lsof", "-i", ":7473", "-i", ":7687"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print(result.stdout.rstrip())
    else:
        print("  Ports 7473 and 7687 are free")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List active SSM sessions for a Neo4j EE Private stack's instances."
        ),
    )
    parser.add_argument(
        "stack",
        nargs="?",
        help="Stack name. Defaults to the most recent ../.deploy/*.txt file.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])

    try:
        outputs_path = _resolve_outputs_path(args.stack)
        fields = read_outputs(outputs_path)
        require_private_mode(fields)
        stack_name = require_field(fields, "StackName", outputs_path)
        region = require_field(fields, "Region", outputs_path)
        bastion_id = require_field(fields, "Neo4jOperatorBastionId", outputs_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    autoscaling = boto3.client("autoscaling", region_name=region)
    ssm = boto3.client("ssm", region_name=region)

    print(f"=== Active SSM sessions for stack: {stack_name} ===")

    instance_ids: list[str] = []
    for key in _ASG_FIELDS:
        asg_name = fields.get(key, "")
        if not asg_name:
            continue
        print(f"{key}: {asg_name}")
        instance_ids.extend(_in_service_instances(autoscaling, asg_name))

    instance_ids.append(bastion_id)

    print(f"Instances: {' '.join(instance_ids)}")
    print()

    for instance_id in instance_ids:
        print(f"--- Sessions for {instance_id} ---")
        _print_sessions(_active_sessions(ssm, instance_id))

    print()
    print("=== Local ports in use ===")
    _print_local_ports()
