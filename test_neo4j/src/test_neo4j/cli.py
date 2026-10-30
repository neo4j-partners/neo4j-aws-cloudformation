"""CLI entry point for the Neo4j CE stack test suite."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from test_neo4j.config import load_config
from test_neo4j.movies_dataset import (
    cleanup_movies_dataset,
    create_movies_dataset,
    verify_movies_dataset,
)
from test_neo4j.neo4j_checks import run_simple_tests
from test_neo4j.neo4j_deep_checks import run_deep_neo4j_checks
from test_neo4j.reporting import TestReporter
from test_neo4j.resilience import run_ee_cluster_resilience_tests, run_resilience_tests
from test_neo4j.wait import wait_for_neo4j

log = logging.getLogger(__name__)

# Repo root: test_neo4j/src/test_neo4j/cli.py -> up 3 levels -> test_neo4j/ -> up 1 -> repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "neo4j-ee" / "src"))
from neo4j_ee.outputs import latest_outputs_file  # noqa: E402


def _deploy_dir(edition: str) -> Path:
    return _REPO_ROOT / f"neo4j-{edition}" / ".deploy"


def _resolve_outputs_path(
    explicit: Path | None,
    stack: str | None,
    edition: str,
) -> Path:
    """Return the path to the deployment outputs file.

    Resolution order:
    1. Explicit --outputs-file path
    2. --stack <name> -> neo4j-{edition}/.deploy/<name>.txt
    3. Most recently modified .txt in neo4j-{edition}/.deploy/
    """
    if explicit:
        return explicit

    deploy_dir = _deploy_dir(edition)

    if stack:
        return deploy_dir / f"{stack}.txt"

    latest = latest_outputs_file(deploy_dir)
    if latest:
        return latest

    return deploy_dir / "no-deployment-found.txt"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test a deployed Neo4j CloudFormation stack",
    )
    parser.add_argument(
        "--edition",
        choices=["ce", "ee"],
        default="ce",
        help="Which edition to test: ce (Community) or ee (Enterprise). Default: ce",
    )
    parser.add_argument(
        "--simple",
        action="store_true",
        help=(
            "Run only connectivity tests (HTTP, auth, Bolt, APOC). "
            "Default is full mode which also tests EBS persistence."
        ),
    )
    parser.add_argument(
        "--password",
        help="Override the password from the outputs file",
    )
    parser.add_argument(
        "--stack",
        help="Stack name — resolves to neo4j-ce/.deploy/<stack-name>.txt",
    )
    parser.add_argument(
        "--outputs-file",
        type=Path,
        help="Explicit path to outputs file (overrides --stack)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout in seconds for ASG replacement (default: 600)",
    )
    parser.add_argument(
        "--infra-security",
        action="store_true",
        help=(
            "Run network and instance security checks: external SG CIDR, "
            "port 5005 absence, internal SG self-reference, IMDSv2 enforcement, "
            "and JDWP absence in neo4j.conf (via SSM)."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)

    outputs_path = _resolve_outputs_path(args.outputs_file, args.stack, args.edition)

    try:
        config = load_config(outputs_path, args.password)
    except (FileNotFoundError, ValueError) as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)

    mode = "simple" if args.simple else "full"
    if args.infra_security:
        mode += "+infra-security"
    reporter = TestReporter()

    edition_label = "EE" if args.edition == "ee" else "CE"
    log.info("=== Neo4j %s Stack Tester ===", edition_label)
    log.info("")
    log.info("  Stack:   %s", config.stack_name)
    log.info("  Host:    %s", config.host)
    log.info("  Edition: %s (%d node)", edition_label, config.number_of_servers)
    log.info("  Mode:    %s", mode)
    log.info("")

    session = None
    resource_map = None
    need_aws = not args.simple or args.infra_security

    if need_aws:
        import boto3  # noqa: PLC0415

        from test_neo4j.aws_helpers import get_stack_resources  # noqa: PLC0415

        session = boto3.Session(region_name=config.region)
        resource_map = get_stack_resources(session, config.stack_name)

    if not wait_for_neo4j(config, timeout=300, interval=10):
        log.info("")
        log.info("Troubleshooting:")
        log.info(
            "  aws cloudformation describe-stacks --stack-name %s --region %s",
            config.stack_name,
            config.region,
        )
        sys.exit(1)
    log.info("")

    if args.edition == "ee":
        _run_ee(args, config, reporter, session, resource_map)
    else:
        _run_ce(args, config, reporter, session, resource_map)

    exit_code = reporter.summary(
        stack_name=config.stack_name,
        endpoint=config.host,
    )
    sys.exit(exit_code)


def _run_ce(args, config, reporter, session, resource_map) -> None:
    run_simple_tests(config, reporter)
    run_deep_neo4j_checks(config, reporter)

    if create_movies_dataset(config, reporter):
        verify_movies_dataset(config, reporter)
        cleanup_movies_dataset(config)

    if not args.simple:
        from test_neo4j.infra_ce import run_infra_checks  # noqa: PLC0415

        run_infra_checks(session, config, reporter, resource_map)
        run_resilience_tests(
            config, reporter, session,
            replacement_timeout=args.timeout,
            resource_map=resource_map,
        )

    if args.infra_security:
        from test_neo4j.security_checks import run_network_security_checks  # noqa: PLC0415

        run_network_security_checks(session, config, reporter, resource_map)


def _run_ee(args, config, reporter, session, resource_map) -> None:
    from test_neo4j.cluster_checks import run_cluster_checks  # noqa: PLC0415
    from test_neo4j.infra_ee import run_ee_infra_checks  # noqa: PLC0415
    from test_neo4j.volume_checks import run_ee_volume_checks  # noqa: PLC0415

    run_simple_tests(config, reporter)
    run_deep_neo4j_checks(config, reporter)

    if create_movies_dataset(config, reporter):
        verify_movies_dataset(config, reporter)
        cleanup_movies_dataset(config)

    if not args.simple:
        from test_neo4j.robust_checks import run_robust_tests_checks  # noqa: PLC0415

        run_ee_infra_checks(
            session, config, reporter, resource_map,
            run_security=args.infra_security,
        )
        amis_exist = run_robust_tests_checks(session, config, reporter, resource_map)
        run_cluster_checks(config, reporter, session, resource_map)
        run_ee_volume_checks(config, reporter, session, resource_map)
        if amis_exist:
            run_ee_cluster_resilience_tests(
                config, reporter, session,
                replacement_timeout=args.timeout,
                resource_map=resource_map,
            )
        else:
            log.error(
                "Skipping resilience tests: a launch template AMI is missing, "
                "so the ASG could not replace a terminated node."
            )
    elif args.infra_security:
        from test_neo4j.security_checks import run_network_security_checks  # noqa: PLC0415

        run_network_security_checks(session, config, reporter, resource_map)
