"""Validation checks run against the Neo4j cluster via the operator bastion."""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from validate_private.runner import Neo4jQueryError, run_cypher_on_bastion

if TYPE_CHECKING:
    from validate_private.config import StackConfig
    from validate_private.reporting import TestReporter

log = logging.getLogger(__name__)


def _run(config: "StackConfig", reporter: "TestReporter", name: str, cypher: str, check_fn) -> None:
    log.info("")
    log.info("--- %s ---", name)
    start = time.monotonic()
    try:
        rows = run_cypher_on_bastion(config, cypher)
        passed, detail = check_fn(rows)
    except Neo4jQueryError as exc:
        passed, detail = False, str(exc)
    except Exception as exc:
        passed, detail = False, f"ERROR: {exc}"
    reporter.record(name, passed, detail, time.monotonic() - start)


def check_bolt(config: "StackConfig", reporter: "TestReporter") -> None:
    def _check(rows):
        val = rows[0]["result"] if rows else None
        return val == 1, f"Bolt connected via bastion, Cypher returned: {val}"

    _run(config, reporter, "Bolt connectivity", "RETURN 1 AS result", _check)


def check_server_status(config: "StackConfig", reporter: "TestReporter") -> None:
    cypher = "CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition"

    def _check(rows):
        if not rows:
            return False, "No rows returned"
        r = rows[0]
        edition = r.get("edition", "")
        version = r.get("version", "unknown")
        passed = edition == "enterprise"
        detail = f"Neo4j Kernel {version} ({edition})"
        return passed, detail

    _run(config, reporter, "Neo4j server status", cypher, _check)


def check_listen_address(config: "StackConfig", reporter: "TestReporter") -> None:
    cypher = "CALL dbms.listConfig('server.default_listen_address') YIELD name, value RETURN value"

    def _check(rows):
        val = rows[0]["value"] if rows else ""
        passed = val == "0.0.0.0"
        return passed, f"server.default_listen_address = {val!r}"

    _run(config, reporter, "Listen address", cypher, _check)


def check_memory_config(config: "StackConfig", reporter: "TestReporter") -> None:
    cypher = (
        "CALL dbms.listConfig() YIELD name, value "
        "WHERE name IN ['server.memory.heap.initial_size', 'server.memory.heap.max_size', "
        "'server.memory.pagecache.size'] "
        "RETURN name, value ORDER BY name"
    )

    def _check(rows):
        if not rows:
            return False, "No memory config rows returned"
        parts = [f"{r['name']}={r['value']}" for r in rows]
        return True, ", ".join(parts)

    _run(config, reporter, "Memory configuration", cypher, _check)


def check_data_directory(config: "StackConfig", reporter: "TestReporter") -> None:
    cypher = "CALL dbms.listConfig('server.directories.data') YIELD name, value RETURN value"

    def _check(rows):
        val = rows[0]["value"] if rows else ""
        passed = val == "/var/lib/neo4j/data"
        return passed, f"data directory = {val!r}"

    _run(config, reporter, "Data directory", cypher, _check)


def check_apoc(config: "StackConfig", reporter: "TestReporter") -> None:
    if not config.install_apoc:
        return

    cypher = "CALL apoc.help('apoc') YIELD name RETURN count(name) AS procedures"

    def _check(rows):
        count = rows[0]["procedures"] if rows else 0
        return count > 0, f"APOC loaded, {count} procedures available"

    _run(config, reporter, "APOC plugin", cypher, _check)


def check_gds(config: "StackConfig", reporter: "TestReporter") -> None:
    if not config.install_gds:
        return

    cypher = "RETURN gds.version() AS version"

    def _check(rows):
        version = rows[0]["version"] if rows else None
        return bool(version), f"GDS {version}" if version else "no result"

    _run(config, reporter, "GDS plugin", cypher, _check)


def check_bloom(config: "StackConfig", reporter: "TestReporter") -> None:
    if not config.install_bloom:
        return

    cypher = "CALL bloom.checkLicenseCompliance() YIELD success, status, daysLeft RETURN success, status, daysLeft"

    def _check(rows):
        if not rows:
            return False, "no result"
        r = rows[0]
        success = r.get("success", False)
        status = r.get("status", "")
        days_left = r.get("daysLeft", 0)
        passed = success is True and status == "valid"
        return passed, f"Bloom license {status} ({days_left:.0f} days remaining)"

    _run(config, reporter, "Bloom license", cypher, _check)


def check_cluster_roles(config: "StackConfig", reporter: "TestReporter") -> None:
    """Verify SHOW DATABASE returns per-node serverId with exactly one writer."""
    start = time.monotonic()
    try:
        rows = run_cypher_on_bastion(
            config,
            "SHOW DATABASE neo4j YIELD serverID, writer",
            database="system",
        )
    except Exception as exc:
        reporter.record("Cluster roles (serverId)", False, str(exc), time.monotonic() - start)
        return

    if not rows:
        reporter.record(
            "Cluster roles (serverId)", False,
            "SHOW DATABASE returned no rows — serverId column may not be supported on this version",
            time.monotonic() - start,
        )
        return

    server_ids = [r.get("serverID") for r in rows]
    missing = [s for s in server_ids if not s]
    writers = [r for r in rows if r.get("writer")]
    duplicates = len(server_ids) != len(set(server_ids))

    issues = []
    if missing:
        issues.append(f"{len(missing)} row(s) with null/missing serverId")
    if duplicates:
        issues.append("duplicate serverIds — address-join collision would still occur")
    if len(writers) != 1:
        issues.append(f"expected exactly 1 writer, got {len(writers)}")

    if issues:
        reporter.record(
            "Cluster roles (serverId)", False,
            "; ".join(issues),
            time.monotonic() - start,
        )
    else:
        followers = len(rows) - 1
        reporter.record(
            "Cluster roles (serverId)", True,
            f"{len(rows)} node(s): 1 writer, {followers} follower(s); all serverIds distinct",
            time.monotonic() - start,
        )


_ASG_LOGICAL_IDS = ("Neo4jNode1ASG", "Neo4jNode2ASG", "Neo4jNode3ASG")


def _java_major(version_line: str) -> int | None:
    """Return the Java major version from a java -version first line."""
    match = re.search(r'version "([^"]+)"', version_line)
    if not match:
        return None
    version = match.group(1)
    if version.startswith("1."):
        parts = version.split(".")
        return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    major = version.split(".", 1)[0]
    return int(major) if major.isdigit() else None


def _parse_key_value_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def run_server_id_check(config: "StackConfig", reporter: "TestReporter") -> None:
    """Verify each cluster node's server_id binary file decodes to the UUID SHOW SERVERS reports."""
    import boto3

    from botocore.config import Config as BotocoreConfig
    from validate_private.aws_helpers import asg_instances, stack_resources
    from validate_private.server_ids import read_server_uuid

    retry_cfg = BotocoreConfig(retries={"mode": "standard"})
    cfn = boto3.client("cloudformation", region_name=config.region, config=retry_cfg)
    asg = boto3.client("autoscaling", region_name=config.region, config=retry_cfg)
    ssm = boto3.client("ssm", region_name=config.region, config=retry_cfg)

    log.info("")
    log.info("=== Server ID file check ===")
    log.info("")

    # Discover running instances via CFN stack → ASG → EC2.
    start = time.monotonic()
    try:
        resources = stack_resources(cfn, config.stack_name)
    except Exception as exc:
        reporter.record("Discover cluster instances", False, str(exc), time.monotonic() - start)
        return

    instance_ids: list[str] = []
    for logical in _ASG_LOGICAL_IDS:
        asg_name = resources.get(logical)
        if asg_name:
            instance_ids.extend(asg_instances(asg, asg_name))

    if not instance_ids:
        reporter.record(
            "Discover cluster instances", False,
            f"No running instances found for ASGs {_ASG_LOGICAL_IDS} in stack {config.stack_name}",
            time.monotonic() - start,
        )
        return

    reporter.record(
        "Discover cluster instances", True,
        f"Found {len(instance_ids)} instance(s): {', '.join(instance_ids)}",
        time.monotonic() - start,
    )

    # Get the authoritative UUIDs from SHOW SERVERS via the bastion.
    start = time.monotonic()
    try:
        server_rows = run_cypher_on_bastion(config, "SHOW SERVERS YIELD name", database="system")
        authoritative_uuids = {r["name"] for r in server_rows if r.get("name")}
    except Exception as exc:
        reporter.record("SHOW SERVERS (authoritative UUIDs)", False, str(exc), time.monotonic() - start)
        return

    reporter.record(
        "SHOW SERVERS (authoritative UUIDs)", True,
        f"{len(authoritative_uuids)} UUID(s): {', '.join(sorted(authoritative_uuids))}",
        time.monotonic() - start,
    )

    # Decode the binary server_id file on each instance and compare.
    for iid in instance_ids:
        start = time.monotonic()
        decoded = read_server_uuid(ssm, iid)
        if decoded is None:
            reporter.record(
                f"server_id decode ({iid})", False,
                "SSM command failed — check instance SSM connectivity",
                time.monotonic() - start,
            )
            continue
        match = decoded in authoritative_uuids
        reporter.record(
            f"server_id decode ({iid})", match,
            f"decoded={decoded!r} {'in' if match else 'NOT IN'} SHOW SERVERS",
            time.monotonic() - start,
        )

    log.info("")


def run_blocklist_check(config: "StackConfig", reporter: "TestReporter") -> None:
    """Assert internal.dbms.cypher_ip_blocklist is present and non-empty on
    every node's effective neo4j.conf (NFR-2, ties to the G3 conf-key audit).

    Presence-only by design (AD-2): a non-empty value passes; CIDR content is
    owned by the build-time contract tests, not this runtime audit.
    """
    import boto3

    from botocore.config import Config as BotocoreConfig
    from validate_private.aws_helpers import asg_instances, stack_resources
    from validate_private.runner import run_shell_on_instance

    retry_cfg = BotocoreConfig(retries={"mode": "standard"})
    cfn = boto3.client("cloudformation", region_name=config.region, config=retry_cfg)
    asg = boto3.client("autoscaling", region_name=config.region, config=retry_cfg)
    ssm = boto3.client("ssm", region_name=config.region, config=retry_cfg)

    log.info("")
    log.info("=== Cypher IP blocklist invariant (per node) ===")

    start = time.monotonic()
    try:
        resources = stack_resources(cfn, config.stack_name)
    except Exception as exc:
        reporter.record("Blocklist: discover instances", False, str(exc), time.monotonic() - start)
        return

    instance_ids: list[str] = []
    for logical in _ASG_LOGICAL_IDS:
        asg_name = resources.get(logical)
        if asg_name:
            instance_ids.extend(asg_instances(asg, asg_name))

    if not instance_ids:
        reporter.record(
            "Blocklist: discover instances", False,
            f"No running instances found for ASGs {_ASG_LOGICAL_IDS} in stack {config.stack_name}",
            time.monotonic() - start,
        )
        return

    extract = (
        "awk -F= '/^internal\\.dbms\\.cypher_ip_blocklist=/ "
        "{sub(/^[^=]*=/, \"\"); print; exit}' /etc/neo4j/neo4j.conf"
    )
    for iid in instance_ids:
        start = time.monotonic()
        ok, stdout, stderr = run_shell_on_instance(ssm, iid, extract)
        value = stdout.strip()
        passed = ok and bool(value)
        if passed:
            detail = f"present and non-empty ({value})"
        elif not ok:
            detail = f"SSM command failed: {stderr.strip() or 'unknown error'}"
        else:
            detail = "internal.dbms.cypher_ip_blocklist absent or empty"
        reporter.record(
            f"Blocklist active ({iid})", passed, detail, time.monotonic() - start
        )

    log.info("")


# AWS console default; hybrid post-quantum (ML-KEM), backward compatible.
# Must match the SslPolicy rendered by the networking partials (NFR-12).
_PQ_SSL_POLICY = "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"

# Effective neo4j.conf keys configure_tls sets on the TLS path. Asserted
# per node so the instance side of NLB re-encryption is provably enforced,
# not merely available (mirrors the presence-only blocklist invariant).
_TLS_CONF_EXPECTED = {
    "server.http.enabled": "false",
    "server.https.enabled": "true",
    "dbms.ssl.policy.bolt.enabled": "true",
    "server.bolt.tls_level": "REQUIRED",
    "dbms.ssl.policy.https.enabled": "true",
}


def _audit_nlb_tls(elbv2, config: "StackConfig", reporter: "TestReporter") -> None:
    """Control-plane audit: NLB listeners and target groups via elbv2."""
    listeners_check = "NLB TLS listeners"
    tg_check = "NLB target-group health checks"
    start = time.monotonic()
    lb_arn = None
    try:
        paginator = elbv2.get_paginator("describe_load_balancers")
        for page in paginator.paginate():
            for lb in page["LoadBalancers"]:
                if lb.get("DNSName") == config.nlb_dns:
                    lb_arn = lb["LoadBalancerArn"]
                    break
            if lb_arn:
                break
    except Exception as exc:
        reporter.record(listeners_check, False, str(exc), time.monotonic() - start)
        return

    if not lb_arn:
        reporter.record(
            listeners_check, False,
            f"No load balancer found with DNSName {config.nlb_dns}",
            time.monotonic() - start,
        )
        return

    start = time.monotonic()
    try:
        listeners = elbv2.describe_listeners(LoadBalancerArn=lb_arn)["Listeners"]
    except Exception as exc:
        reporter.record(listeners_check, False, str(exc), time.monotonic() - start)
        return

    by_port = {ln["Port"]: ln for ln in listeners}
    issues = []
    for port in (7473, 7687):
        ln = by_port.get(port)
        if ln is None:
            issues.append(f"no listener on {port}")
            continue
        if ln.get("Protocol") != "TLS":
            issues.append(f"port {port} protocol {ln.get('Protocol')} != TLS")
        if ln.get("SslPolicy") != _PQ_SSL_POLICY:
            issues.append(
                f"port {port} SslPolicy {ln.get('SslPolicy')} != {_PQ_SSL_POLICY}"
            )
        if not ln.get("Certificates"):
            issues.append(f"port {port} has no certificate")
    passed = not issues
    detail = (
        f"7473/7687 TLS with {_PQ_SSL_POLICY}" if passed else "; ".join(issues)
    )
    reporter.record(listeners_check, passed, detail, time.monotonic() - start)

    start = time.monotonic()
    try:
        tgs = elbv2.describe_target_groups(LoadBalancerArn=lb_arn)["TargetGroups"]
    except Exception as exc:
        reporter.record(
            tg_check, False, str(exc),
            time.monotonic() - start,
        )
        return

    tg_by_port = {tg["Port"]: tg for tg in tgs}
    tg_issues = []
    browser = tg_by_port.get(7473)
    bolt = tg_by_port.get(7687)
    if browser is None:
        tg_issues.append("no target group on 7473")
    elif browser.get("HealthCheckProtocol") != "TCP":
        # 7473 must be a TCP health check, not HTTPS: the NLB health
        # checker opens the TLS connection with no SNI, Jetty's
        # sniHostCheck answers an HTTPS GET / probe 400 Invalid SNI, so
        # the target never goes ELB-healthy. With HealthCheckType=ELB on
        # the node ASGs that is a permanent self-heal kill loop. An
        # HTTPS health check here is the bug, not the expectation.
        tg_issues.append(
            f"7473 health check {browser.get('HealthCheckProtocol')} != TCP"
        )
    if bolt is None:
        tg_issues.append("no target group on 7687")
    elif bolt.get("HealthCheckProtocol") != "TCP":
        tg_issues.append(
            f"7687 health check {bolt.get('HealthCheckProtocol')} != TCP"
        )
    passed = not tg_issues
    detail = "7473 TCP, 7687 TCP" if passed else "; ".join(tg_issues)
    reporter.record(
        tg_check, passed, detail, time.monotonic() - start
    )


def _probe_tls_dataplane(ssm, config: "StackConfig", reporter: "TestReporter") -> None:
    """Data-plane probes run from the in-VPC bastion (openssl/curl/getent)."""
    from validate_private.runner import run_shell_on_instance

    nlb = config.nlb_dns
    dns = config.advertised_dns
    bastion = config.bastion_id

    start = time.monotonic()
    # Neo4j's Jetty enforces sniHostCheck: the TLS SNI (not just the HTTP
    # Host) must equal the served cert SAN, i.e. AdvertisedDNS. Hitting the
    # raw NLB hostname yields 400. Force SNI/Host to AdvertisedDNS with
    # --resolve against an NLB IP; this works whether or not AdvertisedDNS
    # has an in-VPC record (matches the cert-identity probe's -servername).
    # Retry up to ~90s: right after deploy the NLB target group has no
    # healthy target yet and curl returns 000. The check is correct, the
    # operator just ran early. On a settled stack the first curl returns
    # 200 and the loop exits immediately, adding no latency.
    # Test-only: curl -k skips certificate verification because the default
    # test path serves a self-signed cert. Identity is checked separately by
    # the cert-identity probe.
    cmd = (
        f"IP=$(getent hosts {nlb} | awk '{{print $1; exit}}'); "
        f"for i in $(seq 1 9); do "
        f"code=$(curl -sk -o /dev/null -w '%{{http_code}}' --max-time 10 "
        f"--resolve {dns}:7473:$IP https://{dns}:7473/); "
        f'[ "$code" = "200" ] && break; '
        f"sleep 10; "
        f"done; "
        f'echo "$code"'
    )
    # Cover the loop's worst case (9 x (curl --max-time 10 + sleep 10))
    # so the client keeps polling until the command finishes.
    ok, out, err = run_shell_on_instance(ssm, bastion, cmd, timeout_s=210)
    code = out.strip()
    passed = ok and code == "200"
    reporter.record(
        "HTTPS reachable on 7473", passed,
        f"GET https://{dns}:7473/ (SNI {dns}) -> "
        f"{code or err.strip() or 'no response'}",
        time.monotonic() - start,
    )

    start = time.monotonic()
    cmd = (
        f"if curl -s -o /dev/null --max-time 5 http://{nlb}:7474/; "
        f"then echo REACHABLE; else echo REFUSED; fi"
    )
    ok, out, err = run_shell_on_instance(ssm, bastion, cmd)
    refused = "REFUSED" in out
    reporter.record(
        "Plaintext HTTP 7474 refused", ok and refused,
        "no plaintext HTTP listener on 7474" if refused
        else f"7474 unexpectedly reachable: {out.strip() or err.strip()}",
        time.monotonic() - start,
    )

    start = time.monotonic()
    cmd = (
        f"echo | openssl s_client -connect {nlb}:7687 2>/dev/null "
        f"| grep -c 'BEGIN CERTIFICATE' || true"
    )
    ok, out, err = run_shell_on_instance(ssm, bastion, cmd)
    value = out.strip()
    has_cert = ok and value.isdigit() and int(value) > 0
    reporter.record(
        "Bolt 7687 requires TLS", has_cert,
        "TLS handshake on 7687 returned a server certificate" if has_cert
        else f"no TLS certificate from {nlb}:7687 "
             f"({err.strip() or 'handshake failed'})",
        time.monotonic() - start,
    )

    start = time.monotonic()
    cmd = (
        f"echo | openssl s_client -connect {nlb}:7473 -servername {dns} "
        f"2>/dev/null | openssl x509 -noout -subject -ext subjectAltName "
        f"2>/dev/null"
    )
    ok, out, err = run_shell_on_instance(ssm, bastion, cmd)
    matched = ok and dns in out
    reporter.record(
        "Served cert identity matches AdvertisedDNS", matched,
        f"cert subject/SAN contains {dns}" if matched
        else f"AdvertisedDNS {dns} not in served cert: "
             f"{out.strip() or err.strip()}",
        time.monotonic() - start,
    )

    start = time.monotonic()
    if not config.create_private_dns:
        # Default Private/ExistingVpc deploy: AdvertisedDNS is a synthetic
        # cert SAN with no Route 53 record by design; in-VPC clients connect
        # via the NLB hostname. Asserting resolution here would be a guaranteed
        # false failure. Recorded as a skip-pass with the reason stated,
        # mirroring the plaintext-mode skip in run_tls_checks.
        reporter.record(
            "AdvertisedDNS resolves in-VPC", True,
            f"{dns} is a synthetic cert SAN (CreatePrivateDns not set); "
            f"in-VPC clients use {config.bolt_scheme}://{nlb} — no in-VPC "
            f"record expected",
            time.monotonic() - start,
        )
    else:
        cmd = (
            f"echo adv=$(getent hosts {dns} | awk '{{print $1; exit}}') "
            f"nlb=$(getent hosts {nlb} | awk '{{print $1}}' | sort -u "
            f"| paste -sd, -)"
        )
        ok, out, err = run_shell_on_instance(ssm, bastion, cmd)
        adv_ip = ""
        nlb_ips: set[str] = set()
        for tok in out.split():
            if tok.startswith("adv="):
                adv_ip = tok[len("adv="):]
            elif tok.startswith("nlb="):
                nlb_ips = {x for x in tok[len("nlb="):].split(",") if x}
        aliased = bool(adv_ip) and adv_ip in nlb_ips
        reporter.record(
            "AdvertisedDNS resolves in-VPC", ok and aliased,
            f"{dns} -> {adv_ip} (alias to NLB {sorted(nlb_ips)})" if aliased
            else f"{dns} did not resolve to an NLB IP from bastion: "
                 f"{out.strip() or err.strip() or 'no address'}",
            time.monotonic() - start,
        )


def _audit_instance_tls_conf(
    cfn, asg, ssm, config: "StackConfig", reporter: "TestReporter"
) -> None:
    """Per-node audit: effective neo4j.conf SSL keys via SSM (mirrors blocklist)."""
    from validate_private.aws_helpers import asg_instances, stack_resources
    from validate_private.runner import run_shell_on_instance

    discover_check = "TLS conf: discover instances"
    start = time.monotonic()
    try:
        resources = stack_resources(cfn, config.stack_name)
    except Exception as exc:
        reporter.record(
            discover_check, False, str(exc), time.monotonic() - start
        )
        return

    instance_ids: list[str] = []
    for logical in _ASG_LOGICAL_IDS:
        asg_name = resources.get(logical)
        if asg_name:
            instance_ids.extend(asg_instances(asg, asg_name))

    if not instance_ids:
        reporter.record(
            discover_check, False,
            f"No running instances found for ASGs {_ASG_LOGICAL_IDS} "
            f"in stack {config.stack_name}",
            time.monotonic() - start,
        )
        return

    keys = "|".join(re.escape(k) for k in _TLS_CONF_EXPECTED)
    extract = f"grep -E '^({keys})=' /etc/neo4j/neo4j.conf || true"
    for iid in instance_ids:
        start = time.monotonic()
        ok, stdout, stderr = run_shell_on_instance(ssm, iid, extract)
        if not ok:
            reporter.record(
                f"TLS conf ({iid})", False,
                f"SSM command failed: {stderr.strip() or 'unknown error'}",
                time.monotonic() - start,
            )
            continue
        found = _parse_key_value_lines(stdout)
        issues = []
        for key, expected in _TLS_CONF_EXPECTED.items():
            actual = found.get(key)
            if actual is None:
                issues.append(f"{key} missing")
            elif actual != expected:
                issues.append(f"{key}={actual} != {expected}")
        passed = not issues
        detail = (
            "TLS conf enforced (bolt REQUIRED, https on, http off)"
            if passed else "; ".join(issues)
        )
        reporter.record(f"TLS conf ({iid})", passed, detail, time.monotonic() - start)


def run_tls_checks(config: "StackConfig", reporter: "TestReporter") -> None:
    """End-to-end TLS enforcement audit (NFR-12..NFR-14).

    Three independent layers, mirroring the blocklist invariant:
      * control plane  — elbv2 listeners/target groups (TLS + PQ policy)
      * data plane     — openssl/curl/getent probes from the in-VPC bastion
      * instance config — per-node neo4j.conf SSL keys via SSM

    Asserts TLS is enforced (plaintext refused, bolt REQUIRED), not merely
    available. Skips with a recorded note only for genuine plaintext stacks.
    """
    if not config.advertised_dns:
        reporter.record(
            "TLS enabled", True,
            "stack has no AdvertisedDNS — plaintext mode, TLS audit skipped",
            0.0,
        )
        return

    import boto3

    from botocore.config import Config as BotocoreConfig

    retry_cfg = BotocoreConfig(retries={"mode": "standard"})
    elbv2 = boto3.client("elbv2", region_name=config.region, config=retry_cfg)
    cfn = boto3.client("cloudformation", region_name=config.region, config=retry_cfg)
    asg = boto3.client("autoscaling", region_name=config.region, config=retry_cfg)
    ssm = boto3.client("ssm", region_name=config.region, config=retry_cfg)

    log.info("")
    log.info("=== TLS enforcement audit ===")

    _audit_nlb_tls(elbv2, config, reporter)
    _probe_tls_dataplane(ssm, config, reporter)
    _audit_instance_tls_conf(cfn, asg, ssm, config, reporter)

    log.info("")


def run_version_inventory(
    config: "StackConfig",
    reporter: "TestReporter",
    *,
    expected_neo4j_version: str | None = None,
    min_java_major: int | None = None,
    expected_cypher_default: str,
) -> None:
    """Record and optionally assert deployed Neo4j, Java, and Cypher versions."""
    import boto3

    from botocore.config import Config as BotocoreConfig
    from validate_private.aws_helpers import asg_instances, stack_resources
    from validate_private.runner import run_shell_on_instance

    log.info("")
    log.info("=== Release version inventory ===")

    start = time.monotonic()
    try:
        rows = run_cypher_on_bastion(
            config,
            "CALL dbms.components() YIELD name, versions, edition "
            "RETURN name, versions[0] AS version, edition",
        )
        if not rows:
            reporter.record("Neo4j component version", False, "No rows returned", time.monotonic() - start)
        else:
            row = rows[0]
            version = row.get("version", "unknown")
            edition = row.get("edition", "unknown")
            passed = edition == "enterprise"
            if expected_neo4j_version:
                passed = passed and version == expected_neo4j_version
            detail = f"Neo4j Kernel {version} ({edition})"
            if expected_neo4j_version:
                detail += f"; expected {expected_neo4j_version}"
            reporter.record("Neo4j component version", passed, detail, time.monotonic() - start)
    except Exception as exc:
        reporter.record("Neo4j component version", False, str(exc), time.monotonic() - start)

    start = time.monotonic()
    try:
        rows = run_cypher_on_bastion(
            config,
            "CALL dbms.listConfig('db.query.default_language') "
            "YIELD name, value RETURN value",
        )
        cypher_default = rows[0].get("value", "") if rows else ""
        passed = cypher_default == expected_cypher_default
        reporter.record(
            "Cypher default language",
            passed,
            f"db.query.default_language={cypher_default or 'unset'}; "
            f"expected {expected_cypher_default}",
            time.monotonic() - start,
        )
    except Exception as exc:
        reporter.record("Cypher default language", False, str(exc), time.monotonic() - start)

    retry_cfg = BotocoreConfig(retries={"mode": "standard"})
    cfn = boto3.client("cloudformation", region_name=config.region, config=retry_cfg)
    asg = boto3.client("autoscaling", region_name=config.region, config=retry_cfg)
    ssm = boto3.client("ssm", region_name=config.region, config=retry_cfg)

    start = time.monotonic()
    try:
        resources = stack_resources(cfn, config.stack_name)
    except Exception as exc:
        reporter.record("Version inventory: discover instances", False, str(exc), time.monotonic() - start)
        return

    instance_ids: list[str] = []
    for logical in _ASG_LOGICAL_IDS:
        asg_name = resources.get(logical)
        if asg_name:
            instance_ids.extend(asg_instances(asg, asg_name))

    if not instance_ids:
        reporter.record(
            "Version inventory: discover instances",
            False,
            f"No running instances found for ASGs {_ASG_LOGICAL_IDS} in stack {config.stack_name}",
            time.monotonic() - start,
        )
        return

    reporter.record(
        "Version inventory: discover instances",
        True,
        f"Found {len(instance_ids)} instance(s): {', '.join(instance_ids)}",
        time.monotonic() - start,
    )

    inventory_cmd = "\n".join([
        "printf 'neo4j_rpm_version=%s\\n' \"$(rpm -q --qf '%{VERSION}' neo4j-enterprise 2>/dev/null || true)\"",
        "printf 'neo4j_rpm_release=%s\\n' \"$(rpm -q --qf '%{RELEASE}' neo4j-enterprise 2>/dev/null || true)\"",
        "printf 'java_version=%s\\n' \"$(java -version 2>&1 | head -n 1)\"",
        "printf 'cypher_default=%s\\n' \"$(awk -F= '/^db.query.default_language=/ {sub(/^[^=]*=/, \"\"); print; exit}' /etc/neo4j/neo4j.conf)\"",
    ])

    for iid in instance_ids:
        start = time.monotonic()
        ok, stdout, stderr = run_shell_on_instance(ssm, iid, inventory_cmd)
        if not ok:
            reporter.record(
                f"Version inventory ({iid})",
                False,
                f"SSM command failed: {stderr.strip() or 'unknown error'}",
                time.monotonic() - start,
            )
            continue

        values = _parse_key_value_lines(stdout)
        rpm_version = values.get("neo4j_rpm_version", "")
        java_line = values.get("java_version", "")
        cypher_default = values.get("cypher_default", "")

        issues = []
        if not rpm_version:
            issues.append("neo4j-enterprise RPM version unavailable")
        if expected_neo4j_version and rpm_version != expected_neo4j_version:
            issues.append(f"rpm version {rpm_version!r} != expected {expected_neo4j_version!r}")
        java_major = _java_major(java_line)
        if java_major is None:
            issues.append(f"could not parse Java major from {java_line!r}")
        elif min_java_major is not None and java_major < min_java_major:
            issues.append(f"Java major {java_major} < required {min_java_major}")

        detail = (
            f"rpm={rpm_version}-{values.get('neo4j_rpm_release', '')}; "
            f"java={java_line or 'unknown'}; "
            f"db.query.default_language={cypher_default or 'unset'}"
        )
        if issues:
            detail = "; ".join(issues) + f"; {detail}"
        reporter.record(f"Version inventory ({iid})", not issues, detail, time.monotonic() - start)

    log.info("")


def run_release_gate(
    config: "StackConfig",
    reporter: "TestReporter",
    *,
    expected_neo4j_version: str | None = None,
    min_java_major: int | None = None,
    expected_cypher_default: str,
) -> None:
    """Run the deploy-time release checks for a Private EE stack."""
    run_checks(config, reporter)
    run_version_inventory(
        config,
        reporter,
        expected_neo4j_version=expected_neo4j_version,
        min_java_major=min_java_major,
        expected_cypher_default=expected_cypher_default,
    )


def run_checks(config: "StackConfig", reporter: "TestReporter") -> None:
    check_bolt(config, reporter)
    check_server_status(config, reporter)
    check_listen_address(config, reporter)
    check_memory_config(config, reporter)
    check_data_directory(config, reporter)
    check_apoc(config, reporter)
    check_gds(config, reporter)
    check_bloom(config, reporter)
    check_cluster_roles(config, reporter)
    run_blocklist_check(config, reporter)
    run_tls_checks(config, reporter)
