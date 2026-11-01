"""CLI entry point: uv run preflight"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import logging
from pathlib import Path
import sys
import time
from collections.abc import Callable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from validate_private.runner import run_shell_on_instance

log = logging.getLogger(__name__)

_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"
sys.path.insert(0, str(_NEO4J_EE_DIR / "src"))
from neo4j_ee.outputs import (  # noqa: E402
    read_outputs,
    require_field,
    resolve_outputs_file,
)
_RETRY_CFG = Config(retries={"mode": "standard"})

_CONTRACT_PARAMS = (
    "vpc-id",
    "nlb-dns",
    "external-sg-id",
    "password-secret-arn",
    "vpc-endpoint-sg-id",
)
_BASE_OPERATIONAL_PARAMS = ("region", "stack-name", "private-subnet-1-id")
_INTERFACE_ENDPOINTS = ("secretsmanager", "logs", "ssm", "ssmmessages")


@dataclass(frozen=True)
class PreflightContext:
    outputs_path: Path
    fields: dict[str, str]
    stack_name: str
    region: str
    bastion_id: str
    number_of_servers: str


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    detail: str


def _resolve_outputs_path(stack: str | None) -> Path:
    try:
        return resolve_outputs_file(_DEPLOY_DIR, stack)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"{exc}. Run deploy.py first, or pass a stack name."
        ) from exc


def _load_context(stack: str | None) -> PreflightContext:
    outputs_path = _resolve_outputs_path(stack)
    fields = read_outputs(outputs_path)

    mode = fields.get("DeploymentMode", "Public")
    if mode not in {"Private", "ExistingVpc"}:
        stack_name = fields.get("StackName", "unknown")
        raise ValueError(
            "This command requires a Private or ExistingVpc stack. "
            f"Stack '{stack_name}' has DeploymentMode={mode}."
        )

    return PreflightContext(
        outputs_path=outputs_path,
        fields=fields,
        stack_name=require_field(fields, "StackName", outputs_path),
        region=require_field(fields, "Region", outputs_path),
        bastion_id=require_field(fields, "Neo4jOperatorBastionId", outputs_path),
        number_of_servers=fields.get("NumberOfServers", "3"),
    )


def _client(service_name: str, ctx: PreflightContext):
    return boto3.client(service_name, region_name=ctx.region, config=_RETRY_CFG)


def _stack_complete(ctx: PreflightContext) -> CheckResult:
    cfn = _client("cloudformation", ctx)
    stack = cfn.describe_stacks(StackName=ctx.stack_name)["Stacks"][0]
    status = stack["StackStatus"]
    return CheckResult(
        status in {"CREATE_COMPLETE", "UPDATE_COMPLETE"},
        f"StackStatus={status}",
    )


def _ssm_online(ctx: PreflightContext) -> CheckResult:
    ssm = _client("ssm", ctx)
    response = ssm.describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [ctx.bastion_id]}],
    )
    instances = response.get("InstanceInformationList", [])
    ping = instances[0].get("PingStatus", "missing") if instances else "missing"
    return CheckResult(ping == "Online", f"PingStatus={ping}")


def _run_bastion_check(ctx: PreflightContext, command: str) -> CheckResult:
    ssm = _client("ssm", ctx)
    ok, stdout, stderr = run_shell_on_instance(
        ssm,
        ctx.bastion_id,
        command,
        timeout_s=45,
    )
    detail = stdout.strip() or stderr.strip() or "no output"
    return CheckResult(ok, detail)


def _neo4j_driver_installed(ctx: PreflightContext) -> CheckResult:
    return _run_bastion_check(
        ctx,
        "python3.11 -c 'import neo4j; print(neo4j.__version__)'",
    )


def _cypher_shell_installed(ctx: PreflightContext) -> CheckResult:
    return _run_bastion_check(ctx, "cypher-shell --version")


def _secret_exists(ctx: PreflightContext) -> CheckResult:
    secret_id = (
        ctx.fields.get("Neo4jPasswordSecretArn")
        or f"neo4j/{ctx.stack_name}/password"
    )
    secrets = _client("secretsmanager", ctx)
    secret = secrets.describe_secret(SecretId=secret_id)
    return CheckResult(True, f"Secret={secret['Name']}")


def _tls_params_set(ctx: PreflightContext) -> CheckResult:
    cert = ctx.fields.get("CertificateArn", "")
    dns = ctx.fields.get("AdvertisedDNS", "")
    missing = [
        name
        for name, value in (("CertificateArn", cert), ("AdvertisedDNS", dns))
        if not value
    ]
    if missing:
        return CheckResult(
            False,
            f"missing/empty: {', '.join(missing)} "
            "(TLS is mandatory for Private and ExistingVpc)",
        )
    return CheckResult(True, f"AdvertisedDNS={dns}")


def _params_exist(
    ctx: PreflightContext,
    names: tuple[str, ...],
) -> CheckResult:
    ssm = _client("ssm", ctx)
    missing: list[str] = []
    for name in names:
        try:
            ssm.get_parameter(Name=f"/neo4j-ee/{ctx.stack_name}/{name}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ParameterNotFound":
                missing.append(name)
                continue
            raise

    if missing:
        return CheckResult(False, f"Missing: {', '.join(missing)}")
    return CheckResult(True, f"Found: {', '.join(names)}")


def _contract_params_exist(ctx: PreflightContext) -> CheckResult:
    return _params_exist(ctx, _CONTRACT_PARAMS)


def _operational_params_exist(ctx: PreflightContext) -> CheckResult:
    names = list(_BASE_OPERATIONAL_PARAMS)
    if ctx.number_of_servers != "1":
        names.append("private-subnet-2-id")
    return _params_exist(ctx, tuple(names))


def _vpc_id(ctx: PreflightContext) -> str:
    value = ctx.fields.get("VpcId", "")
    if value:
        return value
    ssm = _client("ssm", ctx)
    param = ssm.get_parameter(Name=f"/neo4j-ee/{ctx.stack_name}/vpc-id")
    return param["Parameter"]["Value"]


def _vpc_endpoints_exist(ctx: PreflightContext) -> CheckResult:
    ec2 = _client("ec2", ctx)
    response = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [_vpc_id(ctx)]},
            {"Name": "vpc-endpoint-state", "Values": ["available"]},
        ],
    )
    services = {endpoint["ServiceName"] for endpoint in response["VpcEndpoints"]}
    required = {f"com.amazonaws.{ctx.region}.{name}" for name in _INTERFACE_ENDPOINTS}
    missing = sorted(required - services)
    if missing:
        return CheckResult(False, f"Missing: {', '.join(missing)}")
    return CheckResult(True, f"Found: {', '.join(sorted(required))}")


def _endpoint_reachable(ctx: PreflightContext, service: str) -> CheckResult:
    host = f"{service}.{ctx.region}.amazonaws.com"
    command = (
        "code=$(curl -m 5 -sS -o /dev/null -w '%{http_code}' "
        f"https://{host}/ 2>/tmp/preflight-curl.err || true); "
        "case \"$code\" in "
        "400|403|404) echo \"http_code=$code\"; exit 0 ;; "
        "*) echo \"http_code=$code\"; cat /tmp/preflight-curl.err; exit 1 ;; "
        "esac"
    )
    result = _run_bastion_check(ctx, command)
    return CheckResult(result.passed, result.detail)


def _record(label: str, check: Callable[[], CheckResult]) -> bool:
    start = time.monotonic()
    try:
        result = check()
    except Exception as exc:
        result = CheckResult(False, f"ERROR: {exc}")

    status = "PASS" if result.passed else "FAIL"
    elapsed = time.monotonic() - start
    log.info("  [%s] %s: %s  (%.1fs)", status, label, result.detail, elapsed)
    return result.passed


def _record_info(label: str, check: Callable[[], CheckResult]) -> None:
    start = time.monotonic()
    try:
        result = check()
    except Exception as exc:
        result = CheckResult(False, f"ERROR: {exc}")

    status = "INFO" if result.passed else "WARN"
    elapsed = time.monotonic() - start
    log.info("  [%s] %s: %s  (%.1fs)", status, label, result.detail, elapsed)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run preflight checks for a Neo4j EE Private stack.",
    )
    parser.add_argument(
        "stack",
        nargs="?",
        help="Stack name. Defaults to the most recent ../.deploy/*.txt file.",
    )
    return parser.parse_args(argv)


def main() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
    args = _parse_args(sys.argv[1:])

    try:
        ctx = _load_context(args.stack)
    except (FileNotFoundError, ValueError) as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)

    log.info("=== Preflight Checks ===")
    log.info("")
    log.info("  Stack:   %s", ctx.stack_name)
    log.info("  Region:  %s", ctx.region)
    log.info("  Bastion: %s", ctx.bastion_id)
    log.info("")

    required_checks: tuple[tuple[str, Callable[[], CheckResult]], ...] = (
        (
            "Stack status = CREATE_COMPLETE or UPDATE_COMPLETE",
            lambda: _stack_complete(ctx),
        ),
        ("Bastion SSM PingStatus = Online", lambda: _ssm_online(ctx)),
        ("neo4j Python driver installed on bastion", lambda: _neo4j_driver_installed(ctx)),
        ("cypher-shell installed on bastion", lambda: _cypher_shell_installed(ctx)),
        (f"Secret 'neo4j/{ctx.stack_name}/password' exists", lambda: _secret_exists(ctx)),
        (
            "TLS params set: CertificateArn, AdvertisedDNS",
            lambda: _tls_params_set(ctx),
        ),
        (
            "Contract SSM params: "
            "vpc-id, nlb-dns, external-sg-id, "
            "password-secret-arn, vpc-endpoint-sg-id",
            lambda: _contract_params_exist(ctx),
        ),
        (
            "VPC interface endpoints: secretsmanager, logs, ssm, ssmmessages",
            lambda: _vpc_endpoints_exist(ctx),
        ),
        (
            f"Endpoint reachable: secretsmanager.{ctx.region}.amazonaws.com",
            lambda: _endpoint_reachable(ctx, "secretsmanager"),
        ),
        (
            f"Endpoint reachable: logs.{ctx.region}.amazonaws.com",
            lambda: _endpoint_reachable(ctx, "logs"),
        ),
        (
            f"Endpoint reachable: ssm.{ctx.region}.amazonaws.com",
            lambda: _endpoint_reachable(ctx, "ssm"),
        ),
        (
            f"Endpoint reachable: ssmmessages.{ctx.region}.amazonaws.com",
            lambda: _endpoint_reachable(ctx, "ssmmessages"),
        ),
    )

    passed = 0
    for label, check in required_checks:
        if _record(label, check):
            passed += 1

    operational_label = "Operational SSM params: region, stack-name, private-subnet-1-id"
    if ctx.number_of_servers != "1":
        operational_label += ", private-subnet-2-id"
    _record_info(operational_label, lambda: _operational_params_exist(ctx))

    failed = len(required_checks) - passed
    log.info("")
    log.info("  %d passed, %d failed", passed, failed)

    if failed:
        log.info("")
        log.info("  Troubleshooting:")
        log.info("    Bastion not ready? UserData may still be running, wait 2-3 min and retry.")
        log.info(
            "    Check SSM status: aws ssm describe-instance-information "
            "--filters Key=InstanceIds,Values=%s --region %s",
            ctx.bastion_id,
            ctx.region,
        )
        sys.exit(1)

    log.info("  All checks passed.")


if __name__ == "__main__":
    main()
