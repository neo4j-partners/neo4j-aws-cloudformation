"""CLI entry point: uv run validate-private"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from validate_private.checks import run_checks
from validate_private.config import load_config
from validate_private.reporting import TestReporter

log = logging.getLogger(__name__)

# validate-private/src/validate_private/cli.py -> up 4 levels -> neo4j-ee/
_NEO4J_EE_DIR = Path(__file__).resolve().parent.parent.parent.parent
_DEPLOY_DIR = _NEO4J_EE_DIR / ".deploy"

_FAILOVER_CASES = {"follower-with-data", "leader", "rolling", "reads"}


def _resolve_outputs_path(explicit: Path | None, stack: str | None) -> Path:
    if explicit:
        return explicit
    if stack:
        return _DEPLOY_DIR / f"{stack}.txt"
    if _DEPLOY_DIR.is_dir():
        txt_files = sorted(
            _DEPLOY_DIR.glob("*.txt"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if txt_files:
            return txt_files[0]
    raise FileNotFoundError(
        f"No deployment found in {_DEPLOY_DIR}. "
        "Run deploy.py first, or pass --stack <name>."
    )


def _run_failover_suite(config, reporter) -> None:
    from validate_private.failover import (  # noqa: PLC0415
        run_follower_with_data,
        run_leader,
        run_reads,
        run_rolling,
    )
    run_follower_with_data(config, reporter)
    run_leader(config, reporter)
    run_rolling(config, reporter)
    run_reads(config, reporter)


def _run_resilience_suite(config, reporter, timeout: int | None) -> None:
    from validate_private.resilience import run_single_loss, run_total_loss  # noqa: PLC0415
    run_single_loss(config, reporter, timeout=timeout or 900)
    run_total_loss(config, reporter, timeout=timeout or 1200)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a deployed EE Private Neo4j stack via the operator bastion"
    )
    parser.add_argument(
        "--stack",
        help="Stack name — resolves to neo4j-ee/.deploy/<name>.txt",
    )
    parser.add_argument(
        "--outputs-file",
        type=Path,
        help="Explicit path to outputs file (overrides --stack)",
    )
    parser.add_argument(
        "--password",
        help="Override the password (skips Secrets Manager fetch)",
    )

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--case",
        choices=[
            "single-loss", "total-loss", "server-ids",
            "follower-with-data", "leader", "rolling", "reads",
        ],
        help=(
            "Run a specific test case. "
            "Resilience (terminate/EBS): single-loss (~5 min), total-loss (~10-15 min). "
            "Failover (systemctl stop/start): follower-with-data (~60s), leader (~90s), "
            "rolling (~4-15 min), reads (~90s). "
            "Diagnostic: server-ids (~30s)."
        ),
    )
    mode_group.add_argument(
        "--suite",
        choices=["release", "tls", "failover", "resilience", "all"],
        help=(
            "Run a suite of test cases. "
            "release: default validation plus version inventory. "
            "tls: end-to-end TLS enforcement audit only. "
            "failover: follower-with-data, leader, rolling, reads. "
            "resilience: single-loss, total-loss. "
            "all: failover then resilience (resilience skipped if failover has any failures)."
        ),
    )
    parser.add_argument(
        "--expected-neo4j-version",
        help=(
            "Expected Neo4j Kernel/package version for --suite release. "
            "When omitted, the release suite records the observed version without enforcing one."
        ),
    )
    parser.add_argument(
        "--min-java-major",
        type=int,
        help=(
            "Minimum acceptable Java major version for --suite release. "
            "When omitted, the release suite records the observed version without enforcing one."
        ),
    )
    parser.add_argument(
        "--expected-cypher-default",
        help=(
            "Expected effective db.query.default_language for --suite release. "
            "Required for the release suite so Cypher default drift is asserted."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        help="Override the ASG replacement timeout in seconds (resilience cases only; "
             "default: 900 single-loss, 1200 total-loss)",
    )
    args = parser.parse_args()

    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)

    try:
        outputs_path = _resolve_outputs_path(args.outputs_file, args.stack)
        config = load_config(outputs_path, args.password)
    except (FileNotFoundError, ValueError) as exc:
        log.error("ERROR: %s", exc)
        sys.exit(1)

    reporter = TestReporter()

    log.info("=== Neo4j EE Private Validator ===")
    log.info("")
    log.info("  Stack:   %s", config.stack_name)
    log.info("  Region:  %s", config.region)
    log.info("  Bastion: %s", config.bastion_id)
    log.info("  NLB:     %s", config.nlb_dns)
    if args.case:
        if args.case in _FAILOVER_CASES:
            module = "failover"
        elif args.case == "server-ids":
            module = "diagnostic"
        else:
            module = "resilience"
        log.info("  Mode:    %s/%s", module, args.case)
    elif args.suite:
        log.info("  Mode:    suite/%s", args.suite)
    log.info("")

    if args.case:
        if args.case == "server-ids":
            from validate_private.checks import run_server_id_check  # noqa: PLC0415
            run_server_id_check(config, reporter)
        elif args.case in _FAILOVER_CASES:
            from validate_private import failover  # noqa: PLC0415
            getattr(failover, f"run_{args.case.replace('-', '_')}")(config, reporter)
        else:
            from validate_private.resilience import run_single_loss, run_total_loss  # noqa: PLC0415
            if args.case == "single-loss":
                run_single_loss(config, reporter, timeout=args.timeout or 900)
            else:
                run_total_loss(config, reporter, timeout=args.timeout or 1200)
    elif args.suite:
        if args.suite == "release":
            if not args.expected_cypher_default:
                log.error(
                    "ERROR: --expected-cypher-default is required with --suite release"
                )
                sys.exit(1)
            from validate_private.checks import run_release_gate  # noqa: PLC0415
            run_release_gate(
                config,
                reporter,
                expected_neo4j_version=args.expected_neo4j_version,
                min_java_major=args.min_java_major,
                expected_cypher_default=args.expected_cypher_default,
            )
            exit_code = reporter.summary(
                stack_name=config.stack_name,
                bastion_id=config.bastion_id,
            )
            sys.exit(exit_code)
        if args.suite == "tls":
            from validate_private.checks import run_tls_checks  # noqa: PLC0415
            run_tls_checks(config, reporter)
        if args.suite in ("failover", "all"):
            _run_failover_suite(config, reporter)
        if args.suite in ("resilience", "all"):
            if args.suite == "all" and reporter.had_failures():
                log.info("  Failover suite had failures — skipping resilience suite.\n")
            else:
                _run_resilience_suite(config, reporter, args.timeout)
    else:
        run_checks(config, reporter)

    exit_code = reporter.summary(
        stack_name=config.stack_name,
        bastion_id=config.bastion_id,
    )
    sys.exit(exit_code)
