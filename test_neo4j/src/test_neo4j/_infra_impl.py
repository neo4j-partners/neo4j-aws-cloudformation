"""Infrastructure validation implementation.

Public callers should import from infra_checks, infra_ce, infra_ee,
security_checks, or robust_checks.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from test_neo4j.config import StackConfig
from test_neo4j.reporting import TestReporter
from test_neo4j.ssm_runner import run_ssm_shell

if TYPE_CHECKING:
    import boto3

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_EE_TEMPLATE_DIR = _REPO_ROOT / "neo4j-ee" / "templates"
_EE_RENDERED_TEMPLATES = (
    "neo4j-public.template.yaml",
    "neo4j-private.template.yaml",
    "neo4j-private-existing-vpc.template.yaml",
)


def check_stack_status(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
) -> None:
    """Verify the CloudFormation stack is in a healthy terminal state."""
    with reporter.test("CloudFormation stack status") as ctx:
        try:
            cfn = session.client("cloudformation")
            resp = cfn.describe_stacks(StackName=config.stack_name)
            status = resp["Stacks"][0]["StackStatus"]
            if status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                ctx.pass_(f"Stack status is {status}")
            else:
                ctx.fail(
                    f"Stack status is {status} "
                    "(expected CREATE_COMPLETE or UPDATE_COMPLETE)"
                )
        except Exception as exc:
            ctx.fail(f"Failed to query stack status: {exc}")


def _expected_neo4j_ports(config: StackConfig) -> set[int]:
    """Return the externally reachable Neo4j ports for this stack.

    Bolt is always 7687. The Browser/HTTP port depends on TLS termination: an
    EE stack with TLS at the NLB (signalled by AdvertisedDNS, recorded as
    config.bolt_tls_enabled) serves HTTPS on 7473; CE and plain EE serve HTTP
    on 7474. Mirrors the listener split documented in phase 6 of
    neo4j-ee/worklog/tls.md.
    """
    browser_port = 7473 if (config.edition == "ee" and config.bolt_tls_enabled) else 7474
    return {browser_port, 7687}


def check_security_group_ports(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify the security group allows inbound on the expected Neo4j ports."""
    with reporter.test("Security group ports") as ctx:
        sg_id = resource_map.get("Neo4jExternalSecurityGroup")
        if not sg_id:
            ctx.fail("Neo4jExternalSecurityGroup not found in stack resources")
            return

        expected_ports = _expected_neo4j_ports(config)
        try:
            ec2 = session.client("ec2")
            resp = ec2.describe_security_groups(GroupIds=[sg_id])
            permissions = resp["SecurityGroups"][0]["IpPermissions"]
            open_ports = {p["FromPort"] for p in permissions if "FromPort" in p}

            missing = expected_ports - open_ports
            if not missing:
                ctx.pass_(f"{sg_id} allows ports {sorted(expected_ports)}")
            else:
                ctx.fail(
                    f"{sg_id} missing expected ports: {missing} "
                    f"(open: {sorted(open_ports)})"
                )
        except Exception as exc:
            ctx.fail(f"Failed to describe security group {sg_id}: {exc}")


# ---------------------------------------------------------------------------
# CE-specific checks
# ---------------------------------------------------------------------------

def check_elastic_ip(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """CE only: verify the Elastic IP is allocated and associated with an instance."""
    with reporter.test("Elastic IP association") as ctx:
        eip_ip = resource_map.get("Neo4jElasticIP")
        if not eip_ip:
            ctx.fail("Neo4jElasticIP not found in stack resources")
            return

        try:
            ec2 = session.client("ec2")
            resp = ec2.describe_addresses(PublicIps=[eip_ip])
            addr = resp["Addresses"][0]
            association = addr.get("AssociationId")
            alloc_id = addr.get("AllocationId", "unknown")

            if association:
                instance_id = addr.get("InstanceId", "unknown")
                ctx.pass_(
                    f"EIP {eip_ip} ({alloc_id}) associated with {instance_id}"
                )
            else:
                ctx.fail(
                    f"EIP {eip_ip} ({alloc_id}) is allocated but not associated "
                    "with any instance"
                )
        except Exception as exc:
            ctx.fail(f"Failed to describe Elastic IP {eip_ip}: {exc}")


def check_asg_config(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """CE only: verify the ASG has min=max=desired=1 and health check type is EC2."""
    with reporter.test("ASG configuration") as ctx:
        asg_name = resource_map.get("Neo4jAutoScalingGroup")
        if not asg_name:
            ctx.fail("Neo4jAutoScalingGroup not found in stack resources")
            return

        try:
            asg_client = session.client("autoscaling")
            groups = asg_client.describe_auto_scaling_groups(
                AutoScalingGroupNames=[asg_name]
            )["AutoScalingGroups"]

            if not groups:
                ctx.fail(f"ASG {asg_name} not found")
                return

            asg = groups[0]
            min_size = asg["MinSize"]
            max_size = asg["MaxSize"]
            desired = asg["DesiredCapacity"]
            health_check = asg["HealthCheckType"]

            issues = []
            if min_size != 1:
                issues.append(f"MinSize={min_size}")
            if max_size != 1:
                issues.append(f"MaxSize={max_size}")
            if desired != 1:
                issues.append(f"DesiredCapacity={desired}")
            if health_check != "EC2":
                issues.append(f"HealthCheckType={health_check}")

            if not issues:
                ctx.pass_(
                    f"ASG {asg_name}: min=1, max=1, desired=1, health_check=EC2"
                )
            else:
                ctx.fail(
                    f"ASG {asg_name} unexpected config: {', '.join(issues)}"
                )
        except Exception as exc:
            ctx.fail(f"Failed to describe ASG {asg_name}: {exc}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def run_infra_checks(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Run CE infrastructure validation tests."""
    check_stack_status(session, config, reporter)
    check_security_group_ports(session, config, reporter, resource_map)
    check_elastic_ip(session, config, reporter, resource_map)
    check_asg_config(session, config, reporter, resource_map)


# ---------------------------------------------------------------------------
# EE-specific checks
# ---------------------------------------------------------------------------

def check_nlb_scheme(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """EE public mode: verify the NLB is internet-facing."""
    with reporter.test("NLB scheme (internet-facing)") as ctx:
        nlb_arn = resource_map.get("Neo4jNetworkLoadBalancer")
        if not nlb_arn:
            ctx.fail("Neo4jNetworkLoadBalancer not found in stack resources")
            return
        try:
            elb = session.client("elbv2")
            resp = elb.describe_load_balancers(LoadBalancerArns=[nlb_arn])
            lbs = resp.get("LoadBalancers", [])
            if not lbs:
                ctx.fail(f"NLB {nlb_arn} not found")
                return
            scheme = lbs[0]["Scheme"]
            if scheme == "internet-facing":
                ctx.pass_(f"NLB {nlb_arn} is internet-facing")
            else:
                ctx.fail(f"NLB scheme is {scheme!r} (expected 'internet-facing')")
        except Exception as exc:
            ctx.fail(f"Failed to describe NLB {nlb_arn}: {exc}")


def check_ee_asg_configs(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """EE: verify each node ASG has min=max=desired=1 and HealthCheckType=ELB."""
    asg_client = session.client("autoscaling")
    for n in range(1, config.number_of_servers + 1):
        asg_logical_id = f"Neo4jNode{n}ASG"
        asg_name = resource_map.get(asg_logical_id)
        with reporter.test(f"ASG configuration (node {n})") as ctx:
            if not asg_name:
                ctx.fail(f"{asg_logical_id} not found in stack resources")
                continue
            try:
                groups = asg_client.describe_auto_scaling_groups(
                    AutoScalingGroupNames=[asg_name]
                )["AutoScalingGroups"]
                if not groups:
                    ctx.fail(f"ASG {asg_name} not found")
                    continue
                asg = groups[0]
                issues = []
                if asg["MinSize"] != 1:
                    issues.append(f"MinSize={asg['MinSize']}")
                if asg["MaxSize"] != 1:
                    issues.append(f"MaxSize={asg['MaxSize']}")
                if asg["DesiredCapacity"] != 1:
                    issues.append(f"DesiredCapacity={asg['DesiredCapacity']}")
                if asg["HealthCheckType"] != "ELB":
                    issues.append(f"HealthCheckType={asg['HealthCheckType']}")
                if not issues:
                    ctx.pass_(f"{asg_name}: min=1, max=1, desired=1, health_check=ELB")
                else:
                    ctx.fail(f"{asg_name} unexpected config: {', '.join(issues)}")
            except Exception as exc:
                ctx.fail(f"Failed to describe ASG {asg_name}: {exc}")


def run_ee_infra_checks(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
    *,
    run_security: bool = False,
) -> None:
    """Run EE infrastructure validation tests."""
    check_stack_status(session, config, reporter)
    check_nlb_scheme(session, config, reporter, resource_map)
    check_ee_asg_configs(session, config, reporter, resource_map)
    check_security_group_ports(session, config, reporter, resource_map)

    if run_security:
        run_network_security_checks(session, config, reporter, resource_map)


# ---------------------------------------------------------------------------
# Network and instance security checks
# ---------------------------------------------------------------------------

def check_external_sg_cidr(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify external SG ingress CIDRs on the Neo4j ports match the AllowedCIDR stack parameter.

    On CE the user-facing SG is Neo4jExternalSecurityGroup and AllowedCIDR is
    bound directly there. On EE public the NLB fronts the cluster: AllowedCIDR
    is enforced on Neo4jNLBSecurityGroup and Neo4jExternalSecurityGroup only
    sources from the NLB SG (no CidrIp rules at all). Pick the right SG by
    edition so the check matches the template shape.
    """
    with reporter.test("External SG ingress CIDR") as ctx:
        try:
            cfn = session.client("cloudformation")
            cfn_resp = cfn.describe_stacks(StackName=config.stack_name)
            params = {
                p["ParameterKey"]: p["ParameterValue"]
                for p in cfn_resp["Stacks"][0].get("Parameters", [])
            }
            expected_cidr = params.get("AllowedCIDR")
            if expected_cidr is None:
                ctx.fail(
                    "AllowedCIDR parameter not found in stack — "
                    "stack may have been deployed before this parameter was added"
                )
                return

            if config.edition == "ee":
                sg_logical_id = "Neo4jNLBSecurityGroup"
            else:
                sg_logical_id = "Neo4jExternalSecurityGroup"
            sg_id = resource_map.get(sg_logical_id)
            if not sg_id:
                ctx.fail(f"{sg_logical_id} not found in stack resources")
                return

            ec2 = session.client("ec2")
            ec2_resp = ec2.describe_security_groups(GroupIds=[sg_id])
            permissions = ec2_resp["SecurityGroups"][0]["IpPermissions"]

            expected_ports = _expected_neo4j_ports(config)
            port_cidrs: dict[int, list[str]] = {}
            for perm in permissions:
                port = perm.get("FromPort")
                if port in expected_ports:
                    port_cidrs[port] = [r["CidrIp"] for r in perm.get("IpRanges", [])]

            issues = []
            for port in sorted(expected_ports):
                cidrs = port_cidrs.get(port, [])
                if not cidrs:
                    issues.append(f"port {port}: no CIDR found")
                elif expected_cidr not in cidrs:
                    issues.append(f"port {port}: {cidrs} (expected {expected_cidr})")

            if not issues:
                ctx.pass_(
                    f"{sg_id} ports {sorted(expected_ports)} restricted to "
                    f"{expected_cidr}"
                )
            else:
                ctx.fail(f"CIDR mismatch on {sg_id}: {'; '.join(issues)}")
        except Exception as exc:
            ctx.fail(f"Failed to check external SG CIDR: {exc}")


def check_port_5005_absent(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify port 5005 (JDWP remote debug) is not open in the internal security group."""
    with reporter.test("Port 5005 absent from internal security group") as ctx:
        sg_id = resource_map.get("Neo4jInternalSecurityGroup")
        if not sg_id:
            ctx.fail("Neo4jInternalSecurityGroup not found in stack resources")
            return

        try:
            ec2 = session.client("ec2")
            resp = ec2.describe_security_groups(GroupIds=[sg_id])
            permissions = resp["SecurityGroups"][0]["IpPermissions"]
            open_ports = {p["FromPort"] for p in permissions if "FromPort" in p}

            if 5005 in open_ports:
                ctx.fail(f"{sg_id} has port 5005 open (JDWP remote debug port must be closed)")
            else:
                ctx.pass_(f"{sg_id} does not allow port 5005")
        except Exception as exc:
            ctx.fail(f"Failed to check internal SG ports: {exc}")


def check_internal_sg_self_reference(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify internal SG ingress rules source from the internal SG itself, not the external SG."""
    with reporter.test("Internal SG ingress sources itself") as ctx:
        int_sg_id = resource_map.get("Neo4jInternalSecurityGroup")
        if not int_sg_id:
            ctx.fail("Neo4jInternalSecurityGroup not found in stack resources")
            return

        try:
            ec2 = session.client("ec2")
            resp = ec2.describe_security_groups(GroupIds=[int_sg_id])
            permissions = resp["SecurityGroups"][0]["IpPermissions"]

            wrong_source = []
            for perm in permissions:
                port = perm.get("FromPort")
                for pair in perm.get("UserIdGroupPairs", []):
                    source = pair.get("GroupId")
                    if source != int_sg_id:
                        wrong_source.append(f"port {port}: source={source}")

            if wrong_source:
                ctx.fail(
                    f"{int_sg_id} has rules sourcing from an external group: "
                    f"{'; '.join(wrong_source)}"
                )
            else:
                ctx.pass_(f"{int_sg_id} all cluster port ingress rules source from itself")
        except Exception as exc:
            ctx.fail(f"Failed to check internal SG source: {exc}")


def check_imdsv2_enforced(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify the launch template enforces IMDSv2 (HttpTokens: required)."""
    with reporter.test("IMDSv2 enforced on launch template") as ctx:
        lt_id = resource_map.get("Neo4jLaunchTemplate")
        if not lt_id:
            ctx.fail("Neo4jLaunchTemplate not found in stack resources")
            return

        try:
            ec2 = session.client("ec2")
            resp = ec2.describe_launch_template_versions(
                LaunchTemplateId=lt_id,
                Versions=["$Latest"],
            )
            versions = resp.get("LaunchTemplateVersions", [])
            if not versions:
                ctx.fail(f"No versions found for launch template {lt_id}")
                return

            metadata_opts = versions[0].get("LaunchTemplateData", {}).get("MetadataOptions", {})
            http_tokens = metadata_opts.get("HttpTokens", "")

            if http_tokens == "required":
                ctx.pass_(f"{lt_id} MetadataOptions.HttpTokens = required")
            else:
                ctx.fail(
                    f"{lt_id} MetadataOptions.HttpTokens = {http_tokens!r} (expected 'required')"
                )
        except Exception as exc:
            ctx.fail(f"Failed to check launch template metadata options: {exc}")


def _edition_instance_pairs(
    session: boto3.Session,
    config: StackConfig,
    resource_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Return [(asg_logical_id, instance_id)] covering every cluster node.

    CE has a single Neo4jAutoScalingGroup. EE has Neo4jNode1ASG..NeoNodeNASG.
    Both editions return the same shape so per-node fan-out checks share code.
    """
    from test_neo4j.aws_helpers import (  # noqa: PLC0415
        get_all_ee_asg_instance_ids,
        get_asg_instance_id,
    )

    if config.edition == "ee":
        return get_all_ee_asg_instance_ids(
            session, config.stack_name, resource_map, config.number_of_servers
        )
    instance_id = get_asg_instance_id(session, config.stack_name, resource_map)
    return [("Neo4jAutoScalingGroup", instance_id)]


def check_jdwp_absent(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify JDWP is not configured in neo4j.conf on every cluster node.

    Fans out across every ASG instance in the stack (one for CE, one per node
    for EE) and grep-checks /etc/neo4j/neo4j.conf via SSM Run Command.
    """
    with reporter.test("JDWP absent from neo4j.conf") as ctx:
        try:
            pairs = _edition_instance_pairs(session, config, resource_map)
        except Exception as exc:
            ctx.fail(f"Could not resolve cluster instance IDs: {exc}")
            return

        failures: list[str] = []
        passes: list[str] = []
        for logical_id, instance_id in pairs:
            try:
                status, stdout, stderr = run_ssm_shell(
                    session,
                    instance_id,
                    [
                        "grep -iq jdwp /etc/neo4j/neo4j.conf "
                        "&& echo JDWP_FOUND "
                        "|| echo JDWP_NOT_FOUND"
                    ],
                    timeout_seconds=30,
                )
            except Exception as exc:
                failures.append(f"{logical_id} ({instance_id}): SSM error: {exc}")
                continue
            if status != "Success":
                failures.append(
                    f"{logical_id} ({instance_id}): SSM status={status} stderr={stderr!r}"
                )
                continue
            output = stdout.strip()
            if "JDWP_NOT_FOUND" in output:
                passes.append(f"{logical_id} ({instance_id})")
            elif "JDWP_FOUND" in output:
                failures.append(
                    f"{logical_id} ({instance_id}): JDWP line found in neo4j.conf"
                )
            else:
                failures.append(
                    f"{logical_id} ({instance_id}): unexpected SSM output: {output!r}"
                )

        if failures:
            ctx.fail("; ".join(failures))
        else:
            ctx.pass_(f"JDWP not present on {len(passes)} node(s): " + ", ".join(passes))


def run_network_security_checks(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify network hardening and instance security configuration.

    Checks that external SG ingress CIDRs match the deployed AllowedCIDR parameter,
    that the JDWP remote debug port is closed, that internal cluster ports are
    reachable only from within the cluster, that IMDSv2 is enforced on the launch
    template, and that JDWP is not configured in neo4j.conf on running instances.
    """
    check_external_sg_cidr(session, config, reporter, resource_map)
    check_port_5005_absent(session, config, reporter, resource_map)
    check_internal_sg_self_reference(session, config, reporter, resource_map)
    check_imdsv2_enforced(session, config, reporter, resource_map)
    check_jdwp_absent(session, config, reporter, resource_map)


# ---------------------------------------------------------------------------
# Robust-tests gap checks
# ---------------------------------------------------------------------------

def _parse_neo4j_conf(text: str) -> dict[str, str]:
    """Return the last 'key=value' assignment for each key in neo4j.conf text."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _node_asg_instance_ids(
    session: boto3.Session,
    config: StackConfig,
    resource_map: dict[str, str],
) -> list[tuple[str, str]]:
    """Return [(logical_id, instance_id)] for every Neo4jNode*ASG in this stack."""
    from test_neo4j.aws_helpers import get_all_ee_asg_instance_ids  # noqa: PLC0415

    return get_all_ee_asg_instance_ids(
        session, config.stack_name, resource_map, config.number_of_servers
    )


def check_neo4j_conf_keys(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Audit /etc/neo4j/neo4j.conf on every cluster node for the keys that the
    bloom-branch template changes introduced or rely on.

    Reads the file once per node via SSM, parses in-memory, then asserts each
    required key. Reports the first failing instance/key pair so it is clear
    which node drifted.
    """
    with reporter.test("neo4j.conf key audit (all nodes)") as ctx:
        try:
            pairs = _node_asg_instance_ids(session, config, resource_map)
        except Exception as exc:
            ctx.fail(f"Could not resolve cluster instance IDs: {exc}")
            return

        bloom_license_path = "/var/lib/neo4j/licenses/neo4j-bloom.license"
        gds_license_path = "/var/lib/neo4j/licenses/neo4j-gds.license"

        required_exact: dict[str, str] = {
            "server.metrics.csv.interval": "5s",
        }
        required_nonempty: list[str] = [
            "internal.dbms.cypher_ip_blocklist",
        ]
        # The license_file keys are part of the licensed contract, not just the
        # install contract. CloudFormation Rules reject InstallBloom=true with
        # an empty BloomLicenseSecretArn, and runtime secret/JAR failures never
        # reach a healthy node, so the audit should key on the recorded license
        # state from the deploy outputs.
        if config.bloom_expected and config.bloom_licensed:
            required_exact["dbms.bloom.license_file"] = bloom_license_path
        if config.gds_expected and config.gds_licensed:
            required_exact["gds.enterprise.license_file"] = gds_license_path

        # Grep only the keys we care about so the SSM response stays well under
        # the get-command-invocation ~24KB StandardOutputContent cap. neo4j.conf
        # is ~43KB on EE and set_neo4j_conf appends the required keys near the
        # end of the file, so a plain `cat` truncates them out of the response.
        all_keys = list(required_exact.keys()) + required_nonempty
        grep_pattern = "^(" + "|".join(re.escape(k) for k in all_keys) + ")="
        grep_cmd = f"grep -E '{grep_pattern}' /etc/neo4j/neo4j.conf || true"

        failures: list[str] = []
        passes: list[str] = []
        for logical_id, instance_id in pairs:
            try:
                status, stdout, stderr = run_ssm_shell(
                    session,
                    instance_id,
                    [grep_cmd],
                    timeout_seconds=45,
                )
            except Exception as exc:
                failures.append(f"{logical_id} ({instance_id}): SSM error: {exc}")
                continue
            if status != "Success":
                failures.append(
                    f"{logical_id} ({instance_id}): SSM status={status} stderr={stderr!r}"
                )
                continue
            conf = _parse_neo4j_conf(stdout)
            for key, expected in required_exact.items():
                actual = conf.get(key)
                if actual != expected:
                    failures.append(
                        f"{logical_id} ({instance_id}): {key}={actual!r} "
                        f"(expected {expected!r})"
                    )
            for key in required_nonempty:
                actual = conf.get(key, "")
                if not actual:
                    failures.append(
                        f"{logical_id} ({instance_id}): {key} missing or empty"
                    )
            passes.append(logical_id)

        if failures:
            ctx.fail("; ".join(failures))
        else:
            ctx.pass_(
                f"All required keys present on {len(passes)} node(s): "
                + ", ".join(passes)
            )


def _template_contract_issues(template_name: str, text: str) -> list[str]:
    """Return rendered-template issues for the plugin/licence contract."""
    checks = {
        "BloomLicenseSecretArn parameter": "BloomLicenseSecretArn:" in text,
        "GdsLicenseSecretArn parameter": "GdsLicenseSecretArn:" in text,
        "Bloom rule assertion": "BloomLicenseSecretArn must be provided when InstallBloom is true." in text,
        "GDS rule assertion": "GdsLicenseSecretArn must be provided when InstallGDS is true." in text,
        "Bloom condition": "BloomEnabledAndLicensed:" in text
        and "- !Equals [!Ref InstallBloom, 'true']" in text
        and "- !Not [!Equals [!Ref BloomLicenseSecretArn, '']]" in text,
        "GDS condition": "GdsEnabledAndLicensed:" in text
        and "- !Equals [!Ref InstallGDS, 'true']" in text
        and "- !Not [!Equals [!Ref GdsLicenseSecretArn, '']]" in text,
        "Bloom IAM Fn::If": "- !If\n                - BloomEnabledAndLicensed\n                - Effect: Allow" in text
        and "Resource: !Ref BloomLicenseSecretArn\n                - !Ref AWS::NoValue" in text,
        "GDS IAM Fn::If": "- !If\n                - GdsEnabledAndLicensed\n                - Effect: Allow" in text
        and "Resource: !Ref GdsLicenseSecretArn\n                - !Ref AWS::NoValue" in text,
        "Bloom conf gate": 'if [[ "${installBloom}" == "true" ]]; then' in text
        and 'if [[ -n "${bloomLicenseSecretArn}" ]]; then' in text
        and "set_neo4j_conf dbms.bloom.license_file /var/lib/neo4j/licenses/neo4j-bloom.license" in text,
        "GDS conf gate": 'if [[ "${installGDS}" == "true" && -n "${gdsLicenseSecretArn}" ]]; then' in text
        and "set_neo4j_conf gds.enterprise.license_file /var/lib/neo4j/licenses/neo4j-gds.license" in text,
        "Bloom runtime missing-ARN fail": 'fail "InstallBloom=true requires BloomLicenseSecretArn to be set."' in text,
        "GDS runtime missing-ARN fail": 'fail "InstallGDS=true requires GdsLicenseSecretArn to be set."' in text,
        "No legacy inline policy": "Neo4jLicenseSecretsRead" not in text,
    }
    return [f"{template_name}: {name}" for name, ok in checks.items() if not ok]


def check_template_plugin_license_contract(reporter: TestReporter) -> None:
    """Validate the rendered EE templates encode the default-off licence contract."""
    with reporter.test("Rendered template plugin/licence contract") as ctx:
        issues: list[str] = []
        for template_name in _EE_RENDERED_TEMPLATES:
            path = _EE_TEMPLATE_DIR / template_name
            if not path.exists():
                issues.append(f"{template_name}: file missing at {path}")
                continue
            issues.extend(_template_contract_issues(template_name, path.read_text()))
        if issues:
            ctx.fail("; ".join(issues))
        else:
            ctx.pass_(
                "All rendered EE templates gate licence IAM, UserData fetch, "
                "and neo4j.conf licence keys on the matching install+ARN predicate"
            )


def check_license_files_on_disk(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Verify expected Neo4j plugin licence files exist and no extras are present."""
    with reporter.test("Neo4j licence files on disk") as ctx:
        try:
            pairs = _edition_instance_pairs(session, config, resource_map)
        except Exception as exc:
            ctx.fail(f"Could not resolve cluster instance IDs: {exc}")
            return

        bloom_license_path = "/var/lib/neo4j/licenses/neo4j-bloom.license"
        gds_license_path = "/var/lib/neo4j/licenses/neo4j-gds.license"
        expected: set[str] = set()
        if config.bloom_expected and config.bloom_licensed:
            expected.add(bloom_license_path)
        if config.gds_expected and config.gds_licensed:
            expected.add(gds_license_path)

        failures: list[str] = []
        passes: list[str] = []
        for logical_id, instance_id in pairs:
            try:
                status, stdout, stderr = run_ssm_shell(
                    session,
                    instance_id,
                    [
                        "find /var/lib/neo4j/licenses -maxdepth 1 "
                        "-type f -print 2>/dev/null | sort || true"
                    ],
                    timeout_seconds=45,
                )
            except Exception as exc:
                failures.append(f"{logical_id} ({instance_id}): SSM error: {exc}")
                continue
            if status != "Success":
                failures.append(
                    f"{logical_id} ({instance_id}): SSM status={status} stderr={stderr!r}"
                )
                continue
            found = {line.strip() for line in stdout.splitlines() if line.strip()}
            # ACCEPT_LICENSE_AGREEMENT is a marker file shipped by the
            # neo4j-enterprise yum package itself, not a plugin license.
            license_files = {p for p in found if p.endswith(".license")}
            missing = sorted(expected - license_files)
            unexpected = sorted(license_files - expected)
            if missing or unexpected:
                detail = []
                if missing:
                    detail.append(f"missing={missing}")
                if unexpected:
                    detail.append(f"unexpected={unexpected}")
                failures.append(f"{logical_id} ({instance_id}): " + ", ".join(detail))
            else:
                passes.append(logical_id)

        if failures:
            ctx.fail("; ".join(failures))
        else:
            expected_label = ", ".join(sorted(expected)) if expected else "no licence files"
            ctx.pass_(
                f"{expected_label} present on {len(passes)} node(s): "
                + ", ".join(passes)
            )


def check_nlb_dns_matches_outputs(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Assert the host in the deploy-outputs Neo4j URI equals the corresponding
    CloudFormation stack-output host.

    Catches drift in the template output shape that deploy.py's
    nlb_dns_from_outputs and downstream tooling depend on.
    """
    with reporter.test("Neo4j URI host matches stack outputs") as ctx:
        try:
            cfn = session.client("cloudformation")
            outputs = {
                o["OutputKey"]: o["OutputValue"]
                for o in cfn.describe_stacks(StackName=config.stack_name)["Stacks"][0]
                .get("Outputs", [])
            }
        except Exception as exc:
            ctx.fail(f"describe-stacks failed: {exc}")
            return

        from urllib.parse import urlparse  # noqa: PLC0415

        outputs_host: str | None = None
        source_key: str | None = None
        if outputs.get("Neo4jHttpsUrl"):
            outputs_host = urlparse(outputs["Neo4jHttpsUrl"]).hostname
            source_key = "Neo4jHttpsUrl"
        elif outputs.get("Neo4jURI"):
            outputs_host = urlparse(outputs["Neo4jURI"]).hostname
            source_key = "Neo4jURI"
        elif outputs.get("Neo4jBrowserURL"):
            outputs_host = urlparse(outputs["Neo4jBrowserURL"]).hostname
            source_key = "Neo4jBrowserURL"
        elif outputs.get("Neo4jInternalDNS"):
            outputs_host = outputs["Neo4jInternalDNS"]
            source_key = "Neo4jInternalDNS"

        if not outputs_host:
            ctx.fail(
                "Stack outputs do not expose any of Neo4jHttpsUrl, Neo4jURI, "
                "Neo4jBrowserURL, or Neo4jInternalDNS — deploy.py and the test "
                "runner cannot resolve the NLB DNS name."
            )
            return

        if config.host == outputs_host:
            ctx.pass_(f"host={config.host} matches stack output {source_key}")
        else:
            ctx.fail(
                f"Mismatch: deploy-outputs host {config.host!r} vs "
                f"stack output {source_key}={outputs_host!r}"
            )


def check_cloudwatch_log_delivery(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
    *,
    max_event_age_seconds: int = 1800,
    retry_attempts: int = 6,
    retry_delay_seconds: int = 15,
) -> None:
    """Verify the per-stack CloudWatch log group exists and is receiving events.

    Confirms two things in one check: the agent baked into the AMI is actually
    running (cwagent was moved from yum-install at boot to a one-shot config
    push), and the userdata config push activated the streams. Tolerates an
    initial silent window since the agent flushes on its own cadence.
    """
    # Prefer the stack output (Neo4jAppLogGroupName) so a future template
    # rename does not silently mask a real log-delivery regression; fall back
    # to the conventional path only if the output is absent.
    fallback_log_group = f"/neo4j/{config.stack_name}/application"
    with reporter.test("CloudWatch log delivery") as ctx:
        logs = session.client("logs")

        try:
            cfn = session.client("cloudformation")
            outputs = {
                o["OutputKey"]: o["OutputValue"]
                for o in cfn.describe_stacks(StackName=config.stack_name)
                .get("Stacks", [{}])[0]
                .get("Outputs", [])
            }
        except Exception:
            outputs = {}
        log_group = outputs.get("Neo4jAppLogGroupName") or fallback_log_group

        # Confirm the log group exists.
        try:
            groups = logs.describe_log_groups(logGroupNamePrefix=log_group).get(
                "logGroups", []
            )
        except Exception as exc:
            ctx.fail(f"describe-log-groups failed: {exc}")
            return
        if not any(g["logGroupName"] == log_group for g in groups):
            ctx.fail(f"log group {log_group} does not exist")
            return

        # Poll for a recent event. The agent typically posts within a couple of
        # minutes of first boot; tolerate up to retry_attempts * retry_delay
        # before failing.
        latest_event_ms = 0
        latest_stream = ""
        for attempt in range(1, retry_attempts + 1):
            try:
                streams = logs.describe_log_streams(
                    logGroupName=log_group,
                    orderBy="LastEventTime",
                    descending=True,
                    limit=5,
                ).get("logStreams", [])
            except Exception as exc:
                ctx.fail(f"describe-log-streams failed: {exc}")
                return
            for s in streams:
                last = s.get("lastEventTimestamp") or 0
                if last > latest_event_ms:
                    latest_event_ms = last
                    latest_stream = s.get("logStreamName", "")
            if latest_event_ms:
                break
            time.sleep(retry_delay_seconds)

        if not latest_event_ms:
            ctx.fail(
                f"log group {log_group} exists but no streams have ever "
                "received an event — CloudWatch agent likely not running"
            )
            return

        age_seconds = max(0, time.time() - (latest_event_ms / 1000.0))
        if age_seconds > max_event_age_seconds:
            ctx.fail(
                f"latest event on {latest_stream} is {age_seconds:.0f}s old "
                f"(> {max_event_age_seconds}s threshold)"
            )
        else:
            ctx.pass_(
                f"latest event on {latest_stream} is {age_seconds:.0f}s old"
            )


def check_ami_build_mode_tag(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> None:
    """Confirm the AMI used by the cluster carries the expected AmiBuildMode tag.

    Iteration-mode AMIs and Marketplace-mode AMIs are built by the same script
    in different accounts; this check is the cheapest guardrail against an
    iteration-mode AMI accidentally being deployed through a production path.

    The check is best-effort: for AMIs sourced from the live Marketplace
    listing (AmiSource=marketplace) tags are owned by Marketplace, not this
    project, so the check is skipped with a pass note. For copied AMIs in
    non-source regions, tags are not copied by default, so when no
    AmiBuildMode tag is found on the in-region image the check falls back to
    the source region's AMI via the SSM parameter or the deploy outputs.
    """
    with reporter.test("AMI build-mode tag") as ctx:
        if not config.ami_id:
            if config.ami_source == "marketplace":
                ctx.pass_("AmiSource=marketplace — tag check not applicable")
                return
            ctx.fail("AmiId missing from deploy outputs")
            return

        ec2 = session.client("ec2")
        try:
            images = ec2.describe_images(ImageIds=[config.ami_id]).get("Images", [])
        except Exception as exc:
            ctx.fail(f"describe-images({config.ami_id}) failed: {exc}")
            return
        if not images:
            ctx.fail(f"AMI {config.ami_id} not found in {config.region}")
            return

        tags = {t["Key"]: t["Value"] for t in images[0].get("Tags", [])}
        build_mode = tags.get("AmiBuildMode")

        # When the AMI was copied across regions, tags are not carried by
        # copy_image. Try to look up the source AMI in the source region as
        # a fallback before failing.
        #
        # Contract: deploy.py owns the source-region/source-AMI metadata. It
        # writes "Copied from <id> in <region>" as the copy's Description and
        # (after Phase 11) tags the copy with SourceAmiId / SourceRegion.
        # Both pathways exist so this check stays correct even if one side
        # regresses; prefer the tags when both are present.
        if not build_mode:
            source_image_id = images[0].get("ImageId")
            src_tag_ami = tags.get("SourceAmiId")
            src_tag_region = tags.get("SourceRegion")
            description = images[0].get("Description", "")
            if src_tag_ami and src_tag_region:
                try:
                    src_ec2 = session.client("ec2", region_name=src_tag_region)
                    src_images = src_ec2.describe_images(
                        ImageIds=[src_tag_ami]
                    ).get("Images", [])
                    if src_images:
                        src_tags = {t["Key"]: t["Value"] for t in src_images[0].get("Tags", [])}
                        build_mode = src_tags.get("AmiBuildMode")
                        source_image_id = src_tag_ami
                except Exception:
                    pass
            if not build_mode and description.startswith("Copied from "):
                try:
                    parts = description.split()
                    source_ami_id = parts[2]
                    source_region = parts[4]
                    src_ec2 = session.client("ec2", region_name=source_region)
                    src_images = src_ec2.describe_images(
                        ImageIds=[source_ami_id]
                    ).get("Images", [])
                    if src_images:
                        src_tags = {t["Key"]: t["Value"] for t in src_images[0].get("Tags", [])}
                        build_mode = src_tags.get("AmiBuildMode")
                        source_image_id = source_ami_id
                except Exception:
                    pass

            if not build_mode:
                ctx.fail(
                    f"AMI {source_image_id} has no AmiBuildMode tag "
                    "(expected 'iteration' or 'marketplace')"
                )
                return

        # Local-built AMIs should be marketplace or iteration, both acceptable
        # for tests; the failure mode this check exists to catch is a
        # completely untagged or wrongly-tagged AMI.
        if build_mode in ("iteration", "marketplace"):
            ctx.pass_(f"AmiBuildMode={build_mode}")
        else:
            ctx.fail(
                f"AmiBuildMode={build_mode!r} — expected 'iteration' or 'marketplace'"
            )


def check_launch_template_amis_exist(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> bool:
    """Fail-fast guard: every launch template in the stack must reference an AMI
    that still exists in EC2. Returns True only when every AMI exists, so the
    caller can skip destructive tests that depend on ASG self-heal.

    Without this, an unrelated `create-ami.sh` run that deregistered the AMI
    out from under a live stack only shows up as a 600-second timeout in the
    resilience test. With this guard, the same situation fails immediately
    with the launch template and missing image id named in the message.
    """
    with reporter.test("Launch template AMIs exist") as ctx:
        ec2 = session.client("ec2")
        lt_ids: list[tuple[str, str]] = []
        for logical_id, physical_id in resource_map.items():
            if physical_id and physical_id.startswith("lt-"):
                lt_ids.append((logical_id, physical_id))
        if not lt_ids:
            ctx.fail("No launch templates found in stack resources")
            return False

        failures: list[str] = []
        passes: list[str] = []
        for logical_id, lt_id in lt_ids:
            try:
                resp = ec2.describe_launch_template_versions(
                    LaunchTemplateId=lt_id, Versions=["$Latest"]
                )
                versions = resp.get("LaunchTemplateVersions", [])
                if not versions:
                    failures.append(f"{logical_id} ({lt_id}): no versions")
                    continue
                ami_id = versions[0].get("LaunchTemplateData", {}).get("ImageId")
                if not ami_id:
                    failures.append(f"{logical_id} ({lt_id}): no ImageId on latest")
                    continue
                images = ec2.describe_images(ImageIds=[ami_id]).get("Images", [])
                if not images:
                    failures.append(
                        f"{logical_id} ({lt_id}): AMI {ami_id} no longer exists in EC2"
                    )
                else:
                    passes.append(f"{logical_id} ({lt_id})={ami_id}")
            except Exception as exc:
                response = getattr(exc, "response", None)
                code = (
                    response.get("Error", {}).get("Code", "")
                    if isinstance(response, dict)
                    else ""
                )
                if code in ("InvalidAMIID.NotFound", "InvalidAMIID.Unavailable"):
                    failures.append(f"{logical_id} ({lt_id}): AMI not found ({code})")
                else:
                    failures.append(f"{logical_id} ({lt_id}): {exc}")

        if failures:
            ctx.fail("; ".join(failures))
            return False
        ctx.pass_(f"All launch template AMIs exist: " + ", ".join(passes))
        return True


def run_robust_tests_checks(
    session: boto3.Session,
    config: StackConfig,
    reporter: TestReporter,
    resource_map: dict[str, str],
) -> bool:
    """Run the gap-closure checks introduced by the robust-tests plan.

    Returns False when the launch template AMI guard fails, so the caller can
    skip destructive resilience tests.
    """
    check_template_plugin_license_contract(reporter)
    amis_exist = check_launch_template_amis_exist(
        session, config, reporter, resource_map
    )
    check_neo4j_conf_keys(session, config, reporter, resource_map)
    check_license_files_on_disk(session, config, reporter, resource_map)
    check_nlb_dns_matches_outputs(session, config, reporter, resource_map)
    check_cloudwatch_log_delivery(session, config, reporter, resource_map)
    check_ami_build_mode_tag(session, config, reporter, resource_map)
    return amis_exist
