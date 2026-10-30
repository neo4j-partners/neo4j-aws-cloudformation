"""SSM Run Command helpers used by infrastructure checks."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    import boto3


def run_ssm_shell(
    session: boto3.Session,
    instance_id: str,
    commands: list[str],
    timeout_seconds: int = 60,
) -> tuple[str, str, str]:
    """Run shell commands on an instance via SSM.

    Returns `(status, stdout, stderr)` when the invocation reaches a terminal
    state. Raises if SSM transport fails or the command times out.
    """
    ssm = session.client("ssm")
    resp = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": commands},
    )
    command_id = resp["Command"]["CommandId"]

    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            inv = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "InvocationDoesNotExist":
                raise
        else:
            status = inv["Status"]
            if status in ("Success", "Failed", "Cancelled", "TimedOut"):
                return (
                    status,
                    inv.get("StandardOutputContent", "") or "",
                    inv.get("StandardErrorContent", "") or "",
                )
        if time.monotonic() >= deadline:
            break
        time.sleep(2)
    raise TimeoutError(
        f"SSM command did not complete within {timeout_seconds}s on {instance_id}"
    )
