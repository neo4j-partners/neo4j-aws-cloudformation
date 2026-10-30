"""Send Cypher queries to the Neo4j cluster via SSM RunShellScript on the operator bastion."""

from __future__ import annotations

import base64
import json
import logging
import time
from typing import TYPE_CHECKING

from botocore.config import Config

if TYPE_CHECKING:
    from validate_private.config import StackConfig

log = logging.getLogger(__name__)

_RETRY_CFG = Config(retries={"mode": "standard"})

# Script that runs ON the bastion. It reads stack/region from argv, fetches
# credentials from Secrets Manager and NLB DNS from SSM Parameter Store using
# the bastion's own IAM role, then executes the Cypher query.
_BASTION_SCRIPT = """\
import sys
import json
import boto3
from neo4j import GraphDatabase

stack = sys.argv[1]
region = sys.argv[2]
bolt_scheme = sys.argv[3]

data = json.load(sys.stdin)
cypher = data["cypher"]
params = data.get("params") or {}
database = data.get("database")

sm = boto3.client("secretsmanager", region_name=region)
password = sm.get_secret_value(SecretId=f"neo4j/{stack}/password")["SecretString"]

ssm_client = boto3.client("ssm", region_name=region)
nlb_dns = ssm_client.get_parameter(Name=f"/neo4j-ee/{stack}/nlb-dns")["Parameter"]["Value"]

driver = GraphDatabase.driver(f"{bolt_scheme}://{nlb_dns}:7687", auth=("neo4j", password))
try:
    kwargs = {"parameters_": params}
    if database:
        kwargs["database_"] = database
    result = driver.execute_query(cypher, **kwargs)
    print(json.dumps([r.data() for r in result.records]))
except Exception as exc:
    print(json.dumps({"error": str(exc)}))
    sys.exit(1)
finally:
    driver.close()
"""


class BastionCommandError(RuntimeError):
    pass


class Neo4jQueryError(RuntimeError):
    pass


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


def run_cypher_on_bastion(
    config: "StackConfig",
    cypher: str,
    params: dict | None = None,
    database: str | None = None,
    timeout_s: int = 60,
) -> list[dict]:
    """Execute a Cypher query via SSM RunShellScript on the operator bastion.

    The bastion resolves the password from Secrets Manager and the NLB DNS from
    SSM Parameter Store using its own IAM role. Base64 encoding sidesteps all
    shell-quoting issues — the Cypher can contain single quotes, double quotes,
    backticks, or newlines without special handling.

    Pass database="system" for admin queries like SHOW SERVERS.
    """
    import boto3
    from botocore.exceptions import ClientError

    b64_script = base64.b64encode(_BASTION_SCRIPT.encode()).decode()

    payload = json.dumps({
        "cypher": cypher,
        "params": params or {},
        "database": database,
    })
    b64_payload = base64.b64encode(payload.encode()).decode()

    command = (
        f"echo {b64_script} | base64 -d > /tmp/vprun.py && "
        f"echo {b64_payload} | base64 -d | python3.11 /tmp/vprun.py "
        f"{config.stack_name} {config.region} {config.bolt_scheme}"
    )

    ssm = boto3.Session(region_name=config.region).client("ssm", config=_RETRY_CFG)

    try:
        resp = ssm.send_command(
            InstanceIds=[config.bastion_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command]},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "InvalidInstanceId":
            raise BastionCommandError(
                f"Bastion {config.bastion_id} is not SSM-registered. "
                "Run uv run preflight from neo4j-ee/validate-private to diagnose."
            ) from exc
        raise

    command_id = resp["Command"]["CommandId"]
    log.debug("SSM command %s dispatched to bastion %s", command_id, config.bastion_id)

    status, stdout, stderr = run_ssm_command(ssm, command_id, config.bastion_id, timeout_s)

    if status != "Success":
        raise BastionCommandError(
            f"SSM command failed (status={status}).\nstdout: {stdout}\nstderr: {stderr}"
        )

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise BastionCommandError(
            f"Bastion returned non-JSON output (output may be truncated at 24,000 chars): {stdout!r}"
        ) from exc

    if isinstance(result, dict) and "error" in result:
        raise Neo4jQueryError(f"Query failed on bastion: {result['error']}")

    if not isinstance(result, list):
        raise BastionCommandError(
            f"Bastion returned unexpected output (expected JSON array): {stdout!r}"
        )

    return result


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
