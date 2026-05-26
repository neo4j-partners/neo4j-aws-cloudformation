from __future__ import annotations

from collections.abc import Iterator
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import textwrap
from types import ModuleType
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is expected in dev/CI
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "neo4j-ee" / "templates" / "src"
PARTIALS = SRC / "partials"
BUILD_PY = REPO_ROOT / "neo4j-ee" / "templates" / "build.py"


def load_build_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ee_template_build", BUILD_PY)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# CloudFormation-aware YAML loading
#
# The rendered templates use short-form intrinsic tags (!Ref, !GetAtt, !Sub,
# !If, ...). yaml.safe_load rejects them, so a multi-constructor normalizes
# every "!Foo" tag into its long form ({"Fn::Foo": value}, or {"Ref": value}
# / {"Condition": value} for the two non-Fn intrinsics).
# ---------------------------------------------------------------------------

PSEUDO_PARAMETERS = {
    "AWS::Region",
    "AWS::AccountId",
    "AWS::StackName",
    "AWS::StackId",
    "AWS::Partition",
    "AWS::URLSuffix",
    "AWS::NoValue",
    "AWS::NotificationARNs",
}

_NO_FN_PREFIX = {"Ref", "Condition"}

# Service-wide wildcard like "ec2:*" — a prefixed wildcard ("DescribeStack*")
# is fine and must not match.
_SERVICE_WILDCARD_RE = re.compile(r"^[A-Za-z0-9]+:\*$")
_SUB_VAR_RE = re.compile(r"\$\{([^}]+)\}")


def _build_cfn_loader():
    if yaml is None:  # pragma: no cover
        return None

    class CfnLoader(yaml.SafeLoader):
        pass

    def _multi(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            value = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        key = tag_suffix if tag_suffix in _NO_FN_PREFIX else f"Fn::{tag_suffix}"
        return {key: value}

    CfnLoader.add_multi_constructor("!", _multi)
    return CfnLoader


_CFN_LOADER = _build_cfn_loader()


def load_cfn(text: str) -> dict:
    return yaml.load(text, Loader=_CFN_LOADER)


def iter_nodes(obj: object) -> Iterator[dict]:
    """Yield every dict node anywhere in a loaded template."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from iter_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_nodes(item)


def collect_ingress_rules(resource: dict) -> list[dict]:
    """Return all ingress rule dicts for a SecurityGroup/SecurityGroupIngress."""
    rtype = resource.get("Type")
    props = resource.get("Properties", {})
    if rtype == "AWS::EC2::SecurityGroupIngress":
        return [props]
    if rtype == "AWS::EC2::SecurityGroup":
        ingress = props.get("SecurityGroupIngress", [])
        return ingress if isinstance(ingress, list) else [ingress]
    return []


def flatten_policy_statements(statements: list) -> list[dict]:
    """Flatten a policy Statement list, unwrapping Fn::If true-branches."""
    flat: list[dict] = []
    for item in statements:
        if isinstance(item, dict) and set(item) == {"Fn::If"}:
            _cond, true_branch, _false = item["Fn::If"]
            if isinstance(true_branch, dict):
                flat.append(true_branch)
        elif isinstance(item, dict):
            flat.append(item)
    return flat


def _assemble_all_templates() -> dict[str, str]:
    module = load_build_module()
    return {
        "private": module._assemble_private(),
        "public": module._assemble_public(),
        "existing_vpc": module._assemble_existing_vpc(),
    }


class ShellPartialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.command_log = self.tmp / "commands.log"
        self.fail_log = self.tmp / "fail.log"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def write_stub(self, name: str, body: str) -> Path:
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return path

    def run_shell(
        self,
        script: str,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        run_env = os.environ.copy()
        run_env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{run_env['PATH']}",
                "TEST_TMP": str(self.tmp),
                "COMMAND_LOG": str(self.command_log),
                "FAIL_LOG": str(self.fail_log),
            }
        )
        if env:
            run_env.update(env)
        return subprocess.run(
            ["bash", "-c", textwrap.dedent(script)],
            cwd=REPO_ROOT,
            env=run_env,
            text=True,
            capture_output=True,
            check=False,
        )

    def fail_function(self) -> str:
        return """
        fail() {
          echo "$*" >> "$FAIL_LOG"
          exit 42
        }
        """

    def test_set_neo4j_conf_replaces_uncomments_appends_and_is_idempotent(self) -> None:
        conf = self.tmp / "neo4j.conf"
        conf.write_text("#alpha=old\nbeta=old\nserverXdefault_address=keep\n")

        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_CONF="{conf}"
            source "{PARTIALS / 'set-neo4j-conf.sh'}"
            set_neo4j_conf alpha one
            set_neo4j_conf beta two
            set_neo4j_conf gamma three
            set_neo4j_conf alpha one
            set_neo4j_conf server.default_address 0.0.0.0
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            conf.read_text().splitlines(),
            [
                "alpha=one",
                "beta=two",
                "serverXdefault_address=keep",
                "gamma=three",
                "server.default_address=0.0.0.0",
            ],
        )

    def test_fetch_and_install_license_writes_secret_with_restricted_mode(self) -> None:
        self.write_stub(
            "aws",
            """
            printf 'license-body'
            """,
        )
        self.write_stub(
            "chown",
            """
            echo "chown $*" >> "$COMMAND_LOG"
            """,
        )
        dest = self.tmp / "licenses" / "neo4j-gds.license"

        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'install-license.sh'}"
            {self.fail_function()}
            fetch_and_install_license arn:test "{dest}" GDS us-east-1
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(dest.read_text(), "license-body")
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        self.assertIn("chown neo4j:neo4j", self.command_log.read_text())

    def test_fetch_and_install_license_fails_on_empty_none_and_aws_errors(self) -> None:
        for mode, body, expected in [
            ("empty", "exit 0", "returned an empty SecretString payload"),
            ("none", "echo None", "returned an empty SecretString payload"),
            ("fail", "exit 99", "Failed to fetch Bloom license secret"),
        ]:
            with self.subTest(mode=mode):
                self.write_stub("aws", body)
                self.write_stub("chown", "exit 0\n")
                self.fail_log.write_text("")
                dest = self.tmp / f"{mode}.license"

                result = self.run_shell(
                    f"""
                    set -euo pipefail
                    source "{PARTIALS / 'install-license.sh'}"
                    {self.fail_function()}
                    fetch_and_install_license arn:test "{dest}" Bloom us-east-1
                    """
                )

                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertIn(expected, self.fail_log.read_text())

    def test_configure_plugin_settings_covers_four_flag_combinations(self) -> None:
        bloom_lic = "/var/lib/neo4j/licenses/neo4j-bloom.license"
        gds_lic = "/var/lib/neo4j/licenses/neo4j-gds.license"
        cases = [
            # installBloom, bloomArn, installGDS, gdsArn,
            # expect_bloom_class, expect_bloom_lic, expect_gds_lic
            ("true", "arn:bloom", "true", "arn:gds", True, True, True),
            ("true", "", "true", "", True, False, False),
            ("false", "", "false", "", False, False, False),
            ("false", "arn:bloom", "true", "arn:gds", False, False, True),
        ]
        for (
            ib,
            barn,
            ig,
            garn,
            want_class,
            want_blic,
            want_glic,
        ) in cases:
            with self.subTest(installBloom=ib, installGDS=ig, barn=barn, garn=garn):
                set_log = self.tmp / "set-conf.log"
                set_log.write_text("")
                result = self.run_shell(
                    f"""
                    set -euo pipefail
                    source "{PARTIALS / 'configure-neo4j.sh'}"
                    set_neo4j_conf() {{
                      echo "$1=$2" >> "{set_log}"
                    }}
                    configure_plugin_settings "{ib}" "{barn}" "{ig}" "{garn}"
                    """
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = set_log.read_text()
                if want_class:
                    self.assertIn(
                        "server.unmanaged_extension_classes=com.neo4j.bloom.server=/bloom,semantics.extension=/rdf",
                        calls,
                    )
                else:
                    self.assertNotIn("server.unmanaged_extension_classes", calls)
                if want_blic:
                    self.assertIn(f"dbms.bloom.license_file={bloom_lic}", calls)
                else:
                    self.assertNotIn("dbms.bloom.license_file", calls)
                if want_glic:
                    self.assertIn(f"gds.enterprise.license_file={gds_lic}", calls)
                else:
                    self.assertNotIn("gds.enterprise.license_file", calls)

    def test_remove_jdwp_default_strips_jdwp_line_only(self) -> None:
        conf = self.tmp / "neo4j.conf"
        conf.write_text(
            "dbms.jvm.additional=-agentlib:jdwp=transport=dt_socket\nkeep=true\n"
        )
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_CONF="{conf}"
            source "{PARTIALS / 'configure-neo4j.sh'}"
            remove_jdwp_default
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(conf.read_text(), "keep=true\n")
        self.assertFalse((self.tmp / "neo4j.conf.bak").exists())

    def test_configure_memory_recommendation_applies_admin_output_safely(self) -> None:
        self.write_stub(
            "neo4j-admin",
            "printf '%s\\n' '# comment' 'server.memory.heap.initial_size=4g' "
            "'not an assignment' 'server.memory.heap.max_size=4g'\n",
        )
        set_log = self.tmp / "set-conf.log"
        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{
              echo "$1=$2" >> "{set_log}"
            }}
            configure_memory_recommendation
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            set_log.read_text().splitlines(),
            [
                "server.memory.heap.initial_size=4g",
                "server.memory.heap.max_size=4g",
            ],
        )

    def test_configure_memory_recommendation_fails_without_config_keys(self) -> None:
        self.write_stub("neo4j-admin", "printf '%s\\n' '# comment' 'no keys here'\n")
        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{
              :
            }}
            configure_memory_recommendation
            """
        )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn(
            "memory recommendation returned no configuration keys",
            self.fail_log.read_text(),
        )

    def test_configure_cluster_single_node_writes_nothing(self) -> None:
        set_log = self.tmp / "set-conf.log"
        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{
              echo "$1=$2" >> "{set_log}"
            }}
            configure_cluster 1 us-east-1 stack-arn
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set_log.read_text() if set_log.exists() else "", "")

    def test_configure_cluster_discovers_peer_endpoints(self) -> None:
        self.write_stub("hostname", "echo '172.31.0.10'\n")
        self.write_stub("sleep", "exit 0\n")
        self.write_stub(
            "aws",
            """
            args="$*"
            if [[ "$args" == *"AutoScalingGroups[?Tags"* ]]; then
              echo "asg-a"
            elif [[ "$args" == *"AutoScalingGroups[].Instances[].InstanceId"* ]]; then
              echo "i-1 i-2 i-3"
            elif [[ "$args" == *"Reservations[].Instances[].PrivateIpAddress"* ]]; then
              printf '10.0.0.1\\n10.0.0.2\\n10.0.0.3\\n'
            fi
            """,
        )
        set_log = self.tmp / "set-conf.log"

        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{
              echo "$1=$2" >> "{set_log}"
            }}
            configure_cluster 3 us-east-1 stack-arn
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        calls = set_log.read_text()
        self.assertIn("server.cluster.advertised_address=172.31.0.10:6000", calls)
        self.assertIn("initial.dbms.default_secondaries_count=0", calls)
        self.assertIn("dbms.cluster.discovery.resolver_type=LIST", calls)
        self.assertIn("dbms.cluster.endpoints=10.0.0.1:6000,10.0.0.2:6000,10.0.0.3:6000", calls)

    def test_configure_cluster_fails_when_peer_discovery_finds_no_members(self) -> None:
        self.write_stub("hostname", "echo '172.31.0.10'\n")
        self.write_stub("sleep", "exit 0\n")
        self.write_stub("aws", "exit 0\n")

        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{ :; }}
            configure_cluster 3 us-east-1 stack-arn
            """
        )

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Peer discovery failed after 5 minutes.", self.fail_log.read_text())

    def test_configure_tls_plaintext_when_no_advertised_dns(self) -> None:
        set_log = self.tmp / "set-conf.log"
        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-tls.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{ echo "$1=$2" >> "{set_log}"; }}
            configure_tls "" lb.example.com
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = set_log.read_text()
        self.assertIn("server.default_advertised_address=lb.example.com", calls)
        self.assertIn("server.bolt.advertised_address=lb.example.com:7687", calls)
        self.assertIn("server.http.enabled=true", calls)
        self.assertIn("server.http.listen_address=0.0.0.0:7474", calls)
        self.assertIn("server.http.advertised_address=lb.example.com:7474", calls)
        self.assertIn("server.https.enabled=false", calls)
        self.assertNotIn("dbms.ssl.policy", calls)
        self.assertNotIn("server.bolt.tls_level", calls)

    def test_configure_tls_generates_certs_and_sets_keys(self) -> None:
        self.write_stub("chown", 'echo "chown $*" >> "$COMMAND_LOG"\n')
        openssl_log = self.tmp / "openssl.log"
        self.write_stub(
            "openssl",
            f"""
            echo "$*" >> "{openssl_log}"
            out="" key=""
            while [[ $# -gt 0 ]]; do
              case "$1" in
                -keyout) key="$2"; shift 2;;
                -out) out="$2"; shift 2;;
                *) shift;;
              esac
            done
            : > "$key"
            : > "$out"
            """,
        )
        set_log = self.tmp / "set-conf.log"
        certs = self.tmp / "certs"
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_CERTS_DIR="{certs}"
            source "{PARTIALS / 'configure-tls.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{ echo "$1=$2" >> "{set_log}"; }}
            configure_tls neo4j.example.com lb.example.com
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for proto in ("bolt", "https"):
            self.assertTrue((certs / proto / "private.key").exists())
            self.assertTrue((certs / proto / "public.crt").exists())
        calls = set_log.read_text()
        # default_advertised_address stays AdvertisedDNS (Jetty's no-SNI
        # fallback host for the NLB HTTPS health check). Only Bolt is routed
        # and advertises the always-resolvable NLB DNS; Bolt has no
        # sniHostCheck so a non-cert-SAN host is safe there.
        self.assertIn("server.default_advertised_address=neo4j.example.com", calls)
        self.assertIn("server.bolt.advertised_address=lb.example.com:7687", calls)
        self.assertIn("server.http.enabled=false", calls)
        self.assertIn("server.https.enabled=true", calls)
        self.assertIn("server.https.listen_address=0.0.0.0:7473", calls)
        self.assertIn("server.https.advertised_address=neo4j.example.com:7473", calls)
        self.assertIn("dbms.ssl.policy.bolt.enabled=true", calls)
        self.assertIn(f"dbms.ssl.policy.bolt.base_directory={certs}/bolt", calls)
        self.assertIn(f"dbms.ssl.policy.https.base_directory={certs}/https", calls)
        self.assertIn("server.bolt.tls_level=REQUIRED", calls)
        # Both certs must carry CN + SAN = AdvertisedDNS so Neo4j's Jetty
        # sniHostCheck accepts the NLB-re-encrypted HTTPS request (else the
        # browser fails with 400 Invalid SNI). One openssl call per proto.
        openssl_calls = openssl_log.read_text().splitlines()
        self.assertEqual(len(openssl_calls), 2, openssl_calls)
        for call in openssl_calls:
            self.assertIn("-subj /CN=neo4j.example.com", call)
            self.assertIn("subjectAltName=DNS:neo4j.example.com", call)

    def test_configure_tls_reuses_existing_certs(self) -> None:
        self.write_stub("chown", 'echo "chown $*" >> "$COMMAND_LOG"\n')
        self.write_stub("openssl", 'echo "openssl MUST NOT RUN" >&2; exit 9\n')
        certs = self.tmp / "certs"
        for proto in ("bolt", "https"):
            (certs / proto).mkdir(parents=True)
            (certs / proto / "private.key").write_text("EXISTING")
            (certs / proto / "public.crt").write_text("EXISTING")
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_CERTS_DIR="{certs}"
            source "{PARTIALS / 'configure-tls.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{ :; }}
            configure_tls neo4j.example.com lb.example.com
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((certs / "bolt" / "private.key").read_text(), "EXISTING")

    def test_configure_tls_does_not_write_any_base_conf_key(self) -> None:
        source = (PARTIALS / "configure-tls.sh").read_text()
        for key in _EXPECTED_BASE_CONF:
            self.assertNotRegex(
                source,
                rf"^\s*set_neo4j_conf\s+{re.escape(key)}\b",
                f"configure-tls.sh sets base-conf key {key} inline",
            )

    def test_configure_tls_does_not_install_packages_at_boot(self) -> None:
        # NFR-14 / AD-4: openssl is baked into the AMI, never dnf-installed on
        # the boot path. A package install during ASG self-heal is an
        # availability failure with no template fix.
        source = (PARTIALS / "configure-tls.sh").read_text()
        self.assertNotRegex(source, r"\b(dnf|yum)\s+install\b")

    def test_create_ami_bakes_openssl(self) -> None:
        # NFR-14 / AD-4: the AMI builder must install openssl so the boot-path
        # check in configure-tls.sh resolves without a network dependency.
        ami = REPO_ROOT / "neo4j-ee" / "marketplace" / "create-ami.sh"
        self.assertRegex(
            ami.read_text(),
            r"dnf install -y [^\n]*\bopenssl\b",
        )

    def test_install_plugins_success_and_missing_jar_failures(self) -> None:
        self.write_stub("chown", "echo \"chown $*\" >> \"$COMMAND_LOG\"\n")
        neo4j_home = self.tmp / "neo4j"
        for subdir in ["labs", "products", "plugins"]:
            (neo4j_home / subdir).mkdir(parents=True, exist_ok=True)
        (neo4j_home / "labs" / "apoc-5-core.jar").write_text("apoc")
        (neo4j_home / "products" / "bloom-plugin-1.jar").write_text("bloom")
        (neo4j_home / "products" / "neo4j-graph-data-science-1.jar").write_text("gds")

        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_HOME="{neo4j_home}"
            source "{PARTIALS / 'install-plugins.sh'}"
            {self.fail_function()}
            install_apoc
            install_plugin Bloom "bloom-plugin-*.jar"
            install_plugin GDS "neo4j-graph-data-science-*.jar"
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((neo4j_home / "plugins" / "apoc-5-core.jar").read_text(), "apoc")
        self.assertEqual((neo4j_home / "plugins" / "bloom-plugin-1.jar").read_text(), "bloom")
        self.assertEqual((neo4j_home / "plugins" / "neo4j-graph-data-science-1.jar").read_text(), "gds")

        (neo4j_home / "labs" / "apoc-5-core.jar").unlink()
        self.fail_log.write_text("")
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_HOME="{neo4j_home}"
            source "{PARTIALS / 'install-plugins.sh'}"
            {self.fail_function()}
            install_apoc
            """
        )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("APOC core JAR not found", self.fail_log.read_text())

        (neo4j_home / "products" / "neo4j-graph-data-science-1.jar").unlink()
        self.fail_log.write_text("")
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_HOME="{neo4j_home}"
            source "{PARTIALS / 'install-plugins.sh'}"
            {self.fail_function()}
            install_plugin GDS "neo4j-graph-data-science-*.jar"
            """
        )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("GDS JAR not found", self.fail_log.read_text())

    def test_install_neo4j_commands_and_first_boot_password(self) -> None:
        self.write_stub("yum", "echo \"yum $* accept=${NEO4J_ACCEPT_LICENSE_AGREEMENT:-}\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("systemctl", "echo \"systemctl $*\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("service", "echo \"service $*\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("neo4j-admin", "echo \"neo4j-admin $*\" >> \"$COMMAND_LOG\"\n")

        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'install-neo4j.sh'}"
            install_neo4j_from_yum
            start_neo4j secret true
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.command_log.read_text()
        self.assertIn("yum -y install neo4j-enterprise accept=yes", log)
        self.assertIn("systemctl enable neo4j", log)
        self.assertIn("service neo4j start", log)
        self.assertIn("neo4j-admin dbms set-initial-password secret", log)

        self.command_log.write_text("")
        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'install-neo4j.sh'}"
            start_neo4j secret false
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("set-initial-password", self.command_log.read_text())

    def test_cloudwatch_config_is_valid_json_and_invokes_agent_control(self) -> None:
        self.write_stub("amazon-cloudwatch-agent-ctl", "echo \"cwctl $*\" >> \"$COMMAND_LOG\"\n")
        config = self.tmp / "amazon-cloudwatch-agent.json"

        result = self.run_shell(
            f"""
            set -euo pipefail
            export CW_AGENT_CONFIG="{config}"
            export CW_AGENT_CTL=amazon-cloudwatch-agent-ctl
            source "{PARTIALS / 'configure-cloudwatch.sh'}"
            install_cloudwatch_agent demo
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(config.read_text())
        collect_list = payload["logs"]["logs_collected"]["files"]["collect_list"]
        paths = {item["file_path"] for item in collect_list}
        self.assertEqual(
            paths,
            {
                "/var/log/neo4j/security.log",
                "/var/log/neo4j/debug.log",
                "/var/log/cloud-init-output.log",
            },
        )
        self.assertTrue(all(item["log_group_name"] == "/neo4j/demo/application" for item in collect_list))
        self.assertIn(f"file:{config}", self.command_log.read_text())

    def write_attach_stubs(self) -> None:
        self.write_stub(
            "aws",
            """
            args="$*"
            echo "aws $args" >> "$COMMAND_LOG"
            if [[ "$args" == *"attach-volume"* ]]; then
              count_file="$TEST_TMP/attach-count"
              count=0
              [[ -f "$count_file" ]] && count=$(cat "$count_file")
              count=$((count + 1))
              echo "$count" > "$count_file"
              if (( count <= ${AWS_ATTACH_FAILS:-0} )); then
                exit 1
              fi
              exit 0
            fi
            if [[ "$args" == *"Volumes[*].VolumeId"* ]]; then
              printf '%s\\n' "${AWS_VOLUMES-vol-123456}"
            elif [[ "$args" == *"Volumes[0].State"* ]]; then
              echo "${AWS_VOLUME_STATE:-available}"
            elif [[ "$args" == *"Attachments[0].State"* ]]; then
              echo "${AWS_ATTACHMENT_STATE:-attached}"
            fi
            """,
        )
        self.write_stub("sleep", "echo \"sleep $*\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("udevadm", "echo \"udevadm $*\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("lsblk", "echo \"${LSBLK_SERIAL:-vol123456}\"\n")
        self.write_stub(
            "blkid",
            """
            if [[ "${1:-}" == "-s" ]]; then
              printf '%s\\n' "${BLKID_UUID-uuid-1234}"
              exit 0
            fi
            if [[ "${BLKID_HAS_FS:-true}" == "true" ]]; then
              exit 0
            fi
            exit 1
            """,
        )
        self.write_stub("mkfs.xfs", "echo \"mkfs.xfs $*\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("mount", "echo \"mount $*\" >> \"$COMMAND_LOG\"\n")
        self.write_stub("chown", "echo \"chown $*\" >> \"$COMMAND_LOG\"\n")

    def run_attach(self, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        dev = self.tmp / "nvme1n1"
        dev.touch()
        data_dir = self.tmp / "data"
        fstab = self.tmp / "fstab"
        run_env = {
            "NEO4J_DATA_DIR": str(data_dir),
            "FSTAB_PATH": str(fstab),
            "NVME_DEVICE_GLOB": str(self.tmp / "nvme?n1"),
            "ALLOW_REGULAR_NVME_DEVICES": "true",
            "AWS_VOLUMES": "vol-123456",
            "LSBLK_SERIAL": "vol123456",
            "BLKID_UUID": "uuid-1234",
        }
        if env:
            run_env.update(env)
        return self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'attach-data-volume.sh'}"
            {self.fail_function()}
            first_boot=false
            attach_and_mount_data_volume us-east-1 stack-arn us-east-1a i-123 first_boot
            echo "first_boot=$first_boot" > "{self.tmp / 'first-boot-state'}"
            """,
            env=run_env,
        )

    def test_attach_data_volume_validates_volume_count(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"AWS_VOLUMES": ""})
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Expected 1 data volume", self.fail_log.read_text())

        self.fail_log.write_text("")
        result = self.run_attach(env={"AWS_VOLUMES": "vol-1 vol-2"})
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Expected 1 data volume", self.fail_log.read_text())

    def test_attach_data_volume_fails_when_volume_never_available(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"AWS_VOLUME_STATE": "in-use"})

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("not available after 10m", self.fail_log.read_text())

    def test_attach_data_volume_retries_attach_and_mounts_existing_filesystem(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"AWS_ATTACH_FAILS": "2", "BLKID_HAS_FS": "true"})

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.command_log.read_text()
        self.assertEqual((self.tmp / "attach-count").read_text().strip(), "3")
        self.assertIn(f"mount {self.tmp / 'data'}", log)
        self.assertNotIn("mkfs.xfs", log)
        self.assertIn(f"UUID=uuid-1234  {self.tmp / 'data'}  xfs", (self.tmp / "fstab").read_text())
        self.assertEqual((self.tmp / "first-boot-state").read_text().strip(), "first_boot=false")

    def test_attach_data_volume_fails_after_attach_retry_exhaustion(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"AWS_ATTACH_FAILS": "3"})

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("attach-volume failed", self.fail_log.read_text())

    def test_attach_data_volume_fails_when_attachment_state_never_attached(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"AWS_ATTACHMENT_STATE": "attaching"})

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("not attached in 2m", self.fail_log.read_text())

    def test_attach_data_volume_formats_first_boot_and_chowns_data_dir(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"BLKID_HAS_FS": "false"})

        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.command_log.read_text()
        self.assertIn(f"mkfs.xfs {self.tmp / 'nvme1n1'}", log)
        self.assertIn(f"chown neo4j:neo4j {self.tmp / 'data'}", log)
        self.assertEqual((self.tmp / "first-boot-state").read_text().strip(), "first_boot=true")

    def test_attach_data_volume_fails_when_nvme_device_cannot_be_resolved(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"LSBLK_SERIAL": "different-serial"})

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Could not resolve NVMe device", self.fail_log.read_text())

    def test_attach_data_volume_fails_when_uuid_is_missing(self) -> None:
        self.write_attach_stubs()

        result = self.run_attach(env={"BLKID_UUID": ""})

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("No UUID", self.fail_log.read_text())


    def test_apply_base_conf_applies_every_key_and_skips_noise(self) -> None:
        base = self.tmp / "neo4j-base.conf"
        # Comment, blank line, real keys, and an unterminated final line.
        base.write_text(
            "# a comment\n"
            "\n"
            "server.metrics.filter=*\n"
            "internal.dbms.cypher_ip_blocklist=10.0.0.0/8,169.254.169.0/24\n"
            "no_newline_key=tail"
        )
        set_log = self.tmp / "set-conf.log"
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_BASE_CONF="{base}"
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{
              echo "$1=$2" >> "{set_log}"
            }}
            apply_base_conf
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = set_log.read_text().splitlines()
        self.assertIn("server.metrics.filter=*", lines)
        self.assertIn(
            "internal.dbms.cypher_ip_blocklist=10.0.0.0/8,169.254.169.0/24",
            lines,
        )
        # Unterminated final line is still applied.
        self.assertIn("no_newline_key=tail", lines)
        # Comment and blank lines never reach set_neo4j_conf.
        self.assertNotIn("# a comment", set_log.read_text())
        self.assertEqual(len(lines), 3)

    def test_apply_base_conf_fails_on_malformed_line(self) -> None:
        for bad in ("no_equals_here", "=empty_key"):
            with self.subTest(bad=bad):
                base = self.tmp / "neo4j-base.conf"
                base.write_text(f"good=1\n{bad}\n")
                self.fail_log.write_text("")
                result = self.run_shell(
                    f"""
                    set -euo pipefail
                    export NEO4J_BASE_CONF="{base}"
                    source "{PARTIALS / 'configure-neo4j.sh'}"
                    {self.fail_function()}
                    set_neo4j_conf() {{ :; }}
                    apply_base_conf
                    """
                )
                self.assertEqual(result.returncode, 42, result.stderr)
                self.assertIn(
                    "Malformed line in neo4j-base.conf",
                    self.fail_log.read_text(),
                )

    def test_apply_base_conf_default_path_is_opt_neo4j_conf(self) -> None:
        # Phase 4: cfn-init stages the base conf at /opt/neo4j/conf, a
        # cfn-init-owned tree with no collision against the RPM-owned
        # /var/lib/neo4j (AD-3 staging-path resolution).
        source = (PARTIALS / "configure-neo4j.sh").read_text()
        self.assertIn(
            'local base="${NEO4J_BASE_CONF:-/opt/neo4j/conf/neo4j-base.conf}"',
            source,
        )
        self.assertNotIn("/var/lib/neo4j/neo4j-base.conf", source)

    def test_assert_security_invariant_present_absent_empty(self) -> None:
        cases = [
            ("internal.dbms.cypher_ip_blocklist=10.0.0.0/8\nkeep=1\n", 0),
            ("keep=1\n", 42),
            ("internal.dbms.cypher_ip_blocklist=\nkeep=1\n", 42),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                conf = self.tmp / "neo4j.conf"
                conf.write_text(content)
                self.fail_log.write_text("")
                result = self.run_shell(
                    f"""
                    set -euo pipefail
                    export NEO4J_CONF="{conf}"
                    source "{PARTIALS / 'configure-neo4j.sh'}"
                    {self.fail_function()}
                    assert_security_invariant
                    """
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_assert_security_invariant_does_not_validate_cidr_contents(self) -> None:
        # AD-2 / NFR-9: presence-only. A non-empty but content-bogus value
        # must still pass — the function must not parse or compare CIDRs.
        conf = self.tmp / "neo4j.conf"
        conf.write_text("internal.dbms.cypher_ip_blocklist=not-a-cidr\n")
        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_CONF="{conf}"
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            assert_security_invariant
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        body = (PARTIALS / "configure-neo4j.sh").read_text()
        func = body.split("assert_security_invariant()", 1)[1].split("\n}", 1)[0]
        for cidr_token in ("169.254", "10.0.0.0", "/8", "fc00"):
            self.assertNotIn(cidr_token, func)

    def test_set_neo4j_conf_handles_sed_special_characters_in_values(self) -> None:
        # set_neo4j_conf is sed-based with '|' as the delimiter; '&' is the
        # replacement backref. A value containing these must land verbatim,
        # both when replacing an existing key and when appending a new one.
        conf = self.tmp / "neo4j.conf"
        conf.write_text("existing=old\n")

        result = self.run_shell(
            f"""
            set -euo pipefail
            export NEO4J_CONF="{conf}"
            source "{PARTIALS / 'set-neo4j-conf.sh'}"
            set_neo4j_conf existing 'a|b&c/d'
            set_neo4j_conf fresh 'x|y&z/w'
            """
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = conf.read_text().splitlines()
        self.assertIn("existing=a|b&c/d", lines)
        self.assertIn("fresh=x|y&z/w", lines)

    def test_configure_cluster_fails_on_partial_peer_discovery(self) -> None:
        # Only one member is ever discovered for a 3-node cluster. The script
        # must fail rather than configure dbms.cluster.endpoints with an
        # incomplete member list (which would form a split-brain cluster).
        self.write_stub("hostname", "echo '172.31.0.10'\n")
        self.write_stub("sleep", "exit 0\n")
        self.write_stub(
            "aws",
            """
            args="$*"
            if [[ "$args" == *"AutoScalingGroups[?Tags"* ]]; then
              echo "asg-a"
            elif [[ "$args" == *"AutoScalingGroups[].Instances[].InstanceId"* ]]; then
              echo "i-1"
            elif [[ "$args" == *"Reservations[].Instances[].PrivateIpAddress"* ]]; then
              printf '10.0.0.1\\n'
            fi
            """,
        )
        set_log = self.tmp / "set-conf.log"

        result = self.run_shell(
            f"""
            set -euo pipefail
            source "{PARTIALS / 'configure-neo4j.sh'}"
            {self.fail_function()}
            set_neo4j_conf() {{
              echo "$1=$2" >> "{set_log}"
            }}
            configure_cluster 3 us-east-1 stack-arn
            """
        )

        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Peer discovery failed after 5 minutes.", self.fail_log.read_text())
        self.assertNotIn(
            "dbms.cluster.endpoints=",
            set_log.read_text() if set_log.exists() else "",
        )


class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_build_module()

    def test_read_raises_build_error_for_missing_source_partial(self) -> None:
        with self.assertRaisesRegex(
            self.module.BuildError,
            "source partial src/does-not-exist.yaml not found",
        ):
            self.module._read("does-not-exist.yaml")

    def test_inline_partials_raises_build_error_for_missing_partial(self) -> None:
        with self.assertRaisesRegex(
            self.module.BuildError,
            "references missing partial",
        ):
            self.module._inline_partials(
                "# include partials/does-not-exist.sh\n",
                "demo.sh",
            )

    def test_bootstrap_source_declares_expected_partial_includes(self) -> None:
        # Phase 4: the orchestration partials moved out of userdata.sh into
        # the template-owned bootstrap. UserData is now a thin wrapper that
        # declares no partial includes; the bootstrap declares them all.
        bootstrap = (SRC / "bootstrap" / "neo4j-bootstrap.sh").read_text()
        for partial in (
            "set-neo4j-conf.sh",
            "attach-data-volume.sh",
            "install-license.sh",
            "install-neo4j.sh",
            "install-plugins.sh",
            "configure-neo4j.sh",
            "configure-cloudwatch.sh",
        ):
            self.assertIn(f"# include partials/{partial}", bootstrap)
        self.assertNotRegex(
            (SRC / "userdata.sh").read_text(),
            r"#\s*include\s+partials/",
        )


# Every static/security key the base conf owns. Mirrors templates/src/
# neo4j-base.conf; the content test below is the single source of truth.
_EXPECTED_BASE_CONF = {
    "server.default_listen_address": "0.0.0.0",
    "server.bolt.listen_address": "0.0.0.0:7687",
    "dbms.routing.default_router": "SERVER",
    "server.metrics.enabled": "true",
    "server.metrics.jmx.enabled": "true",
    "server.metrics.prefix": "neo4j",
    "server.metrics.filter": "*",
    "server.metrics.csv.interval": "5s",
    "internal.dbms.cypher_ip_blocklist": (
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
        "169.254.169.0/24,fc00::/7,fe80::/10,ff00::/8"
    ),
    "dbms.security.procedures.unrestricted": "gds.*,apoc.*,bloom.*",
    "dbms.security.http_auth_allowlist": "/,/browser.*,/bloom.*",
    "dbms.security.procedures.allowlist": "apoc.*,gds.*,bloom.*",
}


class Neo4jBaseConfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = SRC / "neo4j-base.conf"
        cls.lines = [
            ln
            for ln in cls.path.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]

    def test_every_expected_key_present_with_correct_value(self) -> None:
        parsed = dict(ln.split("=", 1) for ln in self.lines)
        self.assertEqual(parsed, _EXPECTED_BASE_CONF)

    def test_security_invariant_key_is_present_and_non_empty(self) -> None:
        parsed = dict(ln.split("=", 1) for ln in self.lines)
        value = parsed.get("internal.dbms.cypher_ip_blocklist", "")
        self.assertTrue(value)
        for cidr in REQUIRED_BLOCKLIST_CIDRS:
            self.assertIn(cidr, value)

    def test_no_key_appears_more_than_once(self) -> None:
        # Neo4j strict validation refuses to start on a duplicate key
        # (only server.jvm.additional may repeat, and the base file has none).
        keys = [ln.split("=", 1)[0] for ln in self.lines]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(dupes, [], f"duplicate keys in neo4j-base.conf: {dupes}")


class StaticKeyOwnershipTests(unittest.TestCase):
    """NFR-1: no base-conf key is also written by an inline set_neo4j_conf."""

    def test_no_static_key_written_inline_in_any_partial(self) -> None:
        offenders: list[str] = []
        for sh in sorted(PARTIALS.glob("*.sh")):
            text = sh.read_text()
            for key in _EXPECTED_BASE_CONF:
                if re.search(
                    rf"^\s*set_neo4j_conf\s+{re.escape(key)}\b",
                    text,
                    re.MULTILINE,
                ):
                    offenders.append(f"{sh.name}: {key}")
        self.assertEqual(offenders, [], f"static keys set inline: {offenders}")


class RenderedTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        module = load_build_module()
        cls.templates = {
            "private": module._assemble_private(),
            "public": module._assemble_public(),
            "existing_vpc": module._assemble_existing_vpc(),
        }
        cls.docs = (
            {name: load_cfn(body) for name, body in cls.templates.items()}
            if yaml is not None
            else {}
        )

    @staticmethod
    def _launch_template(doc: dict) -> dict:
        return next(
            r
            for r in doc["Resources"].values()
            if r.get("Type") == "AWS::EC2::LaunchTemplate"
        )

    @classmethod
    def _init_files(cls, doc: dict) -> dict:
        lt = cls._launch_template(doc)
        return lt["Metadata"]["AWS::CloudFormation::Init"]["config"]["files"]

    @classmethod
    def _userdata_text(cls, doc: dict) -> str:
        # UserData is Fn::Base64 -> Fn::Join ['', [literals + intrinsics]].
        # Intrinsics resolve at deploy time; joining the literal parts is
        # enough to assert presence, ordering, and absence in the wrapper.
        lt = cls._launch_template(doc)
        join = lt["Properties"]["LaunchTemplateData"]["UserData"]["Fn::Base64"][
            "Fn::Join"
        ]
        return "".join(p for p in join[1] if isinstance(p, str))

    def test_all_templates_include_shared_userdata_contracts(self) -> None:
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertIn("fetch_and_install_license", template)
                self.assertIn("attach_and_mount_data_volume", template)
                self.assertIn("apply_base_conf", template)
                self.assertIn("configure_cluster", template)
                # TLS partial (replaces configure_bolt_tls +
                # configure_network_advertised_addresses): its definition and a
                # distinctive runtime key must be embedded in the bootstrap.
                self.assertIn("configure_tls()", template)
                self.assertIn("dbms.ssl.policy.bolt.enabled", template)
                self.assertNotIn("configure_bolt_tls", template)
                self.assertNotIn("configure_network_advertised_addresses", template)
                self.assertIn("start_neo4j", template)
                self.assertLess(template.index("start_neo4j"), template.index("cfn-signal --success true"))
                self.assertIn('secret-id "neo4j/${stackName}/password"', template)
                self.assertNotIn('password="', template)

    def test_rendered_templates_have_no_unresolved_markers_and_define_helpers_once(
        self,
    ) -> None:
        helpers = (
            "set_neo4j_conf",
            "attach_and_mount_data_volume",
            "fetch_and_install_license",
            "install_neo4j_from_yum",
            "start_neo4j",
            "install_apoc",
            "install_plugin",
            "install_cloudwatch_agent",
            "apply_base_conf",
            "assert_security_invariant",
            "configure_tls",
            "configure_memory_recommendation",
            "configure_cluster",
            "configure_plugin_settings",
            "remove_jdwp_default",
        )
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertNotRegex(template, r"#\s*include\s+partials/")
                self.assertNotRegex(template, r"#\s*embed-conf\s+")
                self.assertNotIn("build_neo4j_conf_file", template)
                self.assertNotIn("extension_config", template)
                for helper in helpers:
                    definitions = re.findall(
                        rf"^\s*{re.escape(helper)}\(\)\s*\{{",
                        template,
                        re.MULTILINE,
                    )
                    self.assertEqual(
                        len(definitions),
                        1,
                        f"{name}: {helper} defined {len(definitions)} times",
                    )

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_base_conf_block_embedded_verbatim_and_blocklist_once(self) -> None:
        # Phase 4: the base conf is no longer a UserData heredoc. It is a
        # cfn-init file resource in the LaunchTemplate Metadata, embedded as
        # a verbatim YAML literal block. Same invariants, new home.
        base_text = (SRC / "neo4j-base.conf").read_text().rstrip("\n")
        base_lines = [
            ln.strip()
            for ln in base_text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        blk = "internal.dbms.cypher_ip_blocklist="
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                files = self._init_files(doc)
                entry = files["/opt/neo4j/conf/neo4j-base.conf"]
                self.assertEqual(entry["mode"], "000644")
                self.assertEqual(entry["owner"], "root")
                content = entry["content"]
                for key_line in base_lines:
                    self.assertIn(key_line, content)
                # Blocklist appears exactly once, inside the metadata block,
                # and never in the UserData wrapper.
                self.assertEqual(content.count(blk), 1, name)
                self.assertNotIn(blk, self._userdata_text(doc))
                # No static base key is written via an inline set_neo4j_conf
                # anywhere in the rendered template (NFR-1 at render level).
                for key_line in base_lines:
                    key = key_line.split("=", 1)[0]
                    self.assertNotIn(
                        f"set_neo4j_conf {key} ", self.templates[name]
                    )

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_cfn_init_metadata_on_launch_template_and_userdata_invokes_it(
        self,
    ) -> None:
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                files = self._init_files(doc)
                boot = files["/opt/neo4j/bin/neo4j-bootstrap.sh"]
                self.assertEqual(boot["mode"], "000700")
                self.assertEqual(boot["owner"], "root")
                self.assertIn("/opt/neo4j/conf/neo4j-base.conf", files)
                ud = self._userdata_text(doc)
                self.assertRegex(
                    ud,
                    r"cfn-init\s+--stack\s+\"\$stackName\"\s+"
                    r"--resource\s+Neo4jLaunchTemplate",
                )

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_bootstrap_and_base_conf_delivered_via_metadata_not_userdata(
        self,
    ) -> None:
        orchestration_defs = (
            "set_neo4j_conf()",
            "apply_base_conf()",
            "configure_cluster()",
            "attach_and_mount_data_volume()",
            "install_neo4j_from_yum()",
            "assert_security_invariant()",
        )
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                boot = self._init_files(doc)[
                    "/opt/neo4j/bin/neo4j-bootstrap.sh"
                ]["content"]
                ud = self._userdata_text(doc)
                for fn in orchestration_defs:
                    self.assertIn(fn, boot)
                    self.assertNotIn(fn, ud)
                self.assertNotIn("neo4j-base.conf <<", ud)
                self.assertNotRegex(ud, r"#\s*embed-conf")

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_cfn_init_runs_before_bootstrap_before_signal(self) -> None:
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                ud = self._userdata_text(doc)
                # Anchor on the real commands, not prose mentions in comments.
                init_pos = ud.index('cfn-init --stack "$stackName"')
                boot_pos = ud.index("\n/opt/neo4j/bin/neo4j-bootstrap.sh\n")
                signal_pos = ud.index("cfn-signal --success true --stack")
                self.assertLess(init_pos, boot_pos)
                self.assertLess(boot_pos, signal_pos)

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_bootstrap_invoked_with_no_args_and_password_by_env(self) -> None:
        # NFR-5: the bootstrap takes no positional args and the Secrets
        # Manager password is handed over by exported env var, never argv.
        # The bootstrap must immediately copy it into a non-exported shell
        # variable and unset the exported name before any child commands run.
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                ud = self._userdata_text(doc)
                boot = self._init_files(doc)[
                    "/opt/neo4j/bin/neo4j-bootstrap.sh"
                ]["content"]
                invocation = next(
                    ln.strip()
                    for ln in ud.splitlines()
                    if "/opt/neo4j/bin/neo4j-bootstrap.sh" in ln
                )
                self.assertEqual(
                    invocation, "/opt/neo4j/bin/neo4j-bootstrap.sh"
                )
                self.assertRegex(ud, r"export\b[^\n]*\bpassword\b")
                # No token after the script path on the invocation line.
                self.assertNotRegex(
                    ud, r"neo4j-bootstrap\.sh[ \t]+\S"
                )
                copy_pos = boot.index('initialPassword="${password}"')
                unset_pos = boot.index("unset password")
                first_external_pos = min(
                    boot.index('install_cloudwatch_agent "${stackName}"'),
                    boot.index('attach_and_mount_data_volume "${region}"'),
                )
                self.assertLess(copy_pos, unset_pos)
                self.assertLess(unset_pos, first_external_pos)

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_nonzero_cfn_init_or_bootstrap_exit_signals_failure(self) -> None:
        # NFR-9: set -e plus the ERR trap means a non-zero exit from either
        # cfn-init or the bootstrap signals failure and never reaches the
        # success signal. Neither call may swallow its exit with `|| true`.
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                ud = self._userdata_text(doc)
                self.assertIn("set -euo pipefail", ud)
                self.assertRegex(
                    ud,
                    r"trap '[^']*cfn-signal --success false[^']*' ERR",
                )
                for line in ud.splitlines():
                    s = line.strip()
                    if s.startswith("cfn-init ") or s == (
                        "/opt/neo4j/bin/neo4j-bootstrap.sh"
                    ):
                        self.assertNotIn("|| true", s)

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_rendered_userdata_under_16kb_base64(self) -> None:
        # NFR-11: the wrapper must stay well under the 16 KB EC2 cap. The
        # bootstrap and base conf travel through cfn-init metadata, which
        # does not count against the cap.
        import base64

        for name, doc in self.docs.items():
            with self.subTest(template=name):
                ud = self._userdata_text(doc)
                size = len(base64.b64encode(ud.encode()))
                self.assertLess(size, 16384, f"{name} UserData {size}B")

    @staticmethod
    def _invocation_pos(template: str, fn: str) -> int:
        # The call site: a line that is the bare function name (with optional
        # args), not the `fn() {` definition. Indentation-agnostic so it does
        # not couple to build.py's YAML block-literal indent.
        matches = [
            m.start()
            for m in re.finditer(
                rf"^[ \t]*{re.escape(fn)}(?:\s.*)?$",
                template,
                re.MULTILINE,
            )
            if not re.match(
                rf"^[ \t]*{re.escape(fn)}\(\)", m.group(0)
            )
        ]
        if not matches:
            raise AssertionError(f"{fn} is never invoked in the template")
        return matches[-1]

    def test_security_invariant_asserted_after_config_before_signal(self) -> None:
        overlay_fns = (
            "apply_base_conf",
            "configure_tls",
            "configure_memory_recommendation",
            "configure_cluster",
            "configure_plugin_settings",
            "remove_jdwp_default",
        )
        for name, template in self.templates.items():
            with self.subTest(template=name):
                assert_pos = self._invocation_pos(
                    template, "assert_security_invariant"
                )
                start_pos = self._invocation_pos(template, "start_neo4j")
                signal_pos = template.index("cfn-signal --success true")
                self.assertLess(assert_pos, start_pos)
                self.assertLess(assert_pos, signal_pos)
                for fn in overlay_fns:
                    call_pos = self._invocation_pos(template, fn)
                    self.assertLess(
                        call_pos,
                        assert_pos,
                        f"{name}: {fn} called after assert_security_invariant",
                    )

    def test_runtime_overlay_functions_called_with_expected_arguments(self) -> None:
        patterns = (
            r'install_cloudwatch_agent\s+"\$\{stackName\}"',
            r'attach_and_mount_data_volume\s+"\$\{region\}"\s+"\$\{_stack_id\}"\s+"\$\{_az\}"\s+"\$\{_instance_id\}"\s+isFirstBoot',
            r'fetch_and_install_license\s+"\$\{bloomLicenseSecretArn\}"\s+/var/lib/neo4j/licenses/neo4j-bloom\.license\s+"Bloom"\s+"\$\{region\}"',
            r'fetch_and_install_license\s+"\$\{gdsLicenseSecretArn\}"\s+/var/lib/neo4j/licenses/neo4j-gds\.license\s+"GDS"\s+"\$\{region\}"',
            r'configure_tls\s+"\$\{advertisedDNS:-\}"\s+"\$\{loadBalancerDNSName\}"',
            r'configure_cluster\s+"\$\{nodeCount\}"\s+"\$\{region\}"\s+"\$\{_stack_id\}"',
            r'configure_plugin_settings\s+"\$\{installBloom\}"\s+"\$\{bloomLicenseSecretArn\}"\s+"\$\{installGDS\}"\s+"\$\{gdsLicenseSecretArn\}"',
            r'start_neo4j\s+"\$\{initialPassword\}"\s+"\$\{isFirstBoot\}"',
        )
        for name, template in self.templates.items():
            with self.subTest(template=name):
                for pat in patterns:
                    self.assertRegex(template, pat)

    def test_templates_preserve_plugin_license_rules(self) -> None:
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertIn("BloomLicenseRequiredWhenInstalled", template)
                self.assertIn("GdsLicenseRequiredWhenInstalled", template)
                self.assertIn("BloomLicenseSecretArn", template)
                self.assertIn("GdsLicenseSecretArn", template)

    def test_license_iam_grants_are_conditional_and_not_wildcarded(self) -> None:
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertIn("BloomEnabledAndLicensed", template)
                self.assertIn("GdsEnabledAndLicensed", template)
                self.assertIn("secretsmanager:GetSecretValue", template)
                self.assertIn("AWS::NoValue", template)
                self.assertNotIn("secret:*", template)

    def test_ebs_and_asg_creation_contracts_are_preserved(self) -> None:
        for name, template in self.templates.items():
            with self.subTest(template=name):
                self.assertIn("DeletionPolicy: Retain", template)
                self.assertIn("CreationPolicy:", template)
                self.assertIn("ResourceSignal:", template)

    def _ssm_keys_to_logical(self, template_name: str) -> tuple[dict, dict]:
        resources = self.docs[template_name]["Resources"]
        ssm = {
            logical: res
            for logical, res in resources.items()
            if res.get("Type") == "AWS::SSM::Parameter"
        }
        keys_to_logical: dict[str, str] = {}
        for logical, res in ssm.items():
            name_node = res["Properties"]["Name"]
            self.assertEqual(
                set(name_node), {"Fn::Sub"}, f"{logical} Name is not !Sub"
            )
            sub_value = name_node["Fn::Sub"]
            prefix = "/neo4j-ee/${AWS::StackName}/"
            self.assertTrue(
                sub_value.startswith(prefix),
                f"{logical} path {sub_value!r} not under {prefix}",
            )
            key = sub_value[len(prefix):]
            self.assertNotIn(key, keys_to_logical, f"duplicate key {key!r}")
            keys_to_logical[key] = logical
        return ssm, keys_to_logical

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_public_template_publishes_exact_ssm_contract(self) -> None:
        expected_keys = {
            "nlb-dns",
            "password-secret-arn",
            "advertised-dns",
            "bolt-url",
            "https-url",
            "number-of-servers",
            "region",
            "stack-name",
        }
        ssm, keys_to_logical = self._ssm_keys_to_logical("public")
        self.assertEqual(set(keys_to_logical), expected_keys)

        advertised = ssm[keys_to_logical["advertised-dns"]]
        self.assertEqual(advertised.get("Condition"), "UsePublicTLS")
        for key in expected_keys - {"advertised-dns"}:
            self.assertNotIn(
                "Condition",
                ssm[keys_to_logical[key]],
                f"{key} must be unconditional",
            )

        # bolt-url / https-url branch on UsePublicTLS (and CreateCluster for
        # the plain-TCP Bolt path). The producer owns the scheme decision so
        # consumers never have to derive it.
        bolt_url = ssm[keys_to_logical["bolt-url"]]["Properties"]["Value"]
        self.assertEqual(set(bolt_url), {"Fn::If"})
        self.assertEqual(bolt_url["Fn::If"][0], "UsePublicTLS")
        bolt_url_no_tls = bolt_url["Fn::If"][2]
        self.assertEqual(set(bolt_url_no_tls), {"Fn::If"})
        self.assertEqual(bolt_url_no_tls["Fn::If"][0], "CreateCluster")

        https_url = ssm[keys_to_logical["https-url"]]["Properties"]["Value"]
        self.assertEqual(set(https_url), {"Fn::If"})
        self.assertEqual(https_url["Fn::If"][0], "UsePublicTLS")

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_private_template_publishes_exact_ssm_contract(self) -> None:
        expected_keys = {
            "vpc-id",
            "nlb-dns",
            "advertised-dns",
            "bolt-url",
            "https-url",
            "private-dns-hosted-zone-id",
            "private-dns-record",
            "region",
            "stack-name",
            "private-subnet-1-id",
            "private-route-table-1-id",
            "private-subnet-2-id",
            "password-secret-arn",
            "external-sg-id",
            "vpc-endpoint-sg-id",
        }
        ssm, keys_to_logical = self._ssm_keys_to_logical("private")
        self.assertEqual(set(keys_to_logical), expected_keys)

        # Private/ExistingVpc enforce AdvertisedDNS non-empty via
        # rules-tls-required.yaml, so bolt-url and https-url use it
        # unconditionally with the operator/deploy.py-supplied scheme.
        bolt_url = ssm[keys_to_logical["bolt-url"]]["Properties"]["Value"]
        self.assertEqual(
            bolt_url, {"Fn::Sub": "${BoltTlsScheme}://${AdvertisedDNS}:7687"}
        )
        https_url = ssm[keys_to_logical["https-url"]]["Properties"]["Value"]
        self.assertEqual(
            https_url, {"Fn::Sub": "https://${AdvertisedDNS}:7473"}
        )

        # Private DNS keys are conditional; bolt-url and https-url are not.
        for key in ("bolt-url", "https-url"):
            self.assertNotIn(
                "Condition",
                ssm[keys_to_logical[key]],
                f"{key} must be unconditional",
            )

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_existing_vpc_template_publishes_exact_ssm_contract(self) -> None:
        expected_keys = {
            "vpc-id",
            "nlb-dns",
            "advertised-dns",
            "bolt-url",
            "https-url",
            "private-dns-hosted-zone-id",
            "private-dns-record",
            "region",
            "stack-name",
            "private-subnet-1-id",
            "private-route-table-1-id",
            "private-subnet-2-id",
            "password-secret-arn",
            "external-sg-id",
            "vpc-endpoint-sg-id",
        }
        ssm, keys_to_logical = self._ssm_keys_to_logical("existing_vpc")
        self.assertEqual(set(keys_to_logical), expected_keys)

        bolt_url = ssm[keys_to_logical["bolt-url"]]["Properties"]["Value"]
        self.assertEqual(
            bolt_url, {"Fn::Sub": "${BoltTlsScheme}://${AdvertisedDNS}:7687"}
        )
        https_url = ssm[keys_to_logical["https-url"]]["Properties"]["Value"]
        self.assertEqual(
            https_url, {"Fn::Sub": "https://${AdvertisedDNS}:7473"}
        )

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_bolt_tls_scheme_parameter_scoped_to_private_templates(self) -> None:
        # BoltTlsScheme lives in parameters-bolt-tls-scheme.yaml, wired only
        # into the Private and ExistingVpc specs. Public derives its scheme
        # from UsePublicTLS and must not carry the parameter, both to avoid
        # cfn-lint W2001 noise and to keep the Public template's user-facing
        # parameter list focused on inputs Public actually consumes.
        for template_name in ("private", "existing_vpc"):
            with self.subTest(template=template_name):
                params = self.docs[template_name].get("Parameters", {})
                self.assertIn("BoltTlsScheme", params)
                param = params["BoltTlsScheme"]
                self.assertEqual(param["Type"], "String")
                self.assertEqual(
                    sorted(param["AllowedValues"]),
                    ["neo4j+s", "neo4j+ssc"],
                )
                self.assertEqual(param["Default"], "neo4j+ssc")
        public_params = self.docs["public"].get("Parameters", {})
        self.assertNotIn("BoltTlsScheme", public_params)

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_bolt_and_https_url_outputs_present_in_all_templates(self) -> None:
        for template_name in ("private", "public", "existing_vpc"):
            with self.subTest(template=template_name):
                outputs = self.docs[template_name].get("Outputs", {})
                self.assertIn("Neo4jBoltUrl", outputs)
                self.assertIn("Neo4jHttpsUrl", outputs)
                # Old Public-only names must not survive the rename.
                self.assertNotIn("Neo4jURI", outputs)
                self.assertNotIn("Neo4jBrowserURL", outputs)

    @unittest.skipUnless(yaml is not None, "pyyaml is required")
    def test_private_contracts_do_not_publish_public_only_ssm_keys(self) -> None:
        # The proposal deliberately scopes `number-of-servers` to the public
        # SSM contract only — it is the discriminator off-VPC consumers use to
        # detect a Public stack. Lock that decision so the key can't drift
        # into the private or existing-vpc contracts without an explicit
        # test update.
        public_only_keys = {"number-of-servers"}
        prefix = "/neo4j-ee/${AWS::StackName}/"
        for template_name in ("private", "existing_vpc"):
            with self.subTest(template=template_name):
                resources = self.docs[template_name]["Resources"]
                keys_published = set()
                for res in resources.values():
                    if res.get("Type") != "AWS::SSM::Parameter":
                        continue
                    name_node = res["Properties"]["Name"]
                    if set(name_node) != {"Fn::Sub"}:
                        continue
                    sub_value = name_node["Fn::Sub"]
                    if sub_value.startswith(prefix):
                        keys_published.add(sub_value[len(prefix):])
                leaked = keys_published & public_only_keys
                self.assertEqual(
                    leaked,
                    set(),
                    f"{template_name} must not publish public-only SSM "
                    f"keys; found {sorted(leaked)}",
                )


# Actions permitted on Resource: "*" in an instance role: read-only verbs
# (AWS does not support resource-level scoping on Describe/List/Get), plus a
# small set of documented non-readonly exceptions.
_WILDCARD_RESOURCE_EXCEPTIONS = {"cloudformation:SignalResource"}


def _allowed_on_wildcard_resource(action: str) -> bool:
    if action in _WILDCARD_RESOURCE_EXCEPTIONS:
        return True
    local = action.split(":", 1)[-1]
    return local.startswith(("Describe", "List", "Get"))

REQUIRED_BLOCKLIST_CIDRS = (
    "169.254.169.0/24",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "fc00::/7",
    "fe80::/10",
    "ff00::/8",
)


@unittest.skipUnless(yaml is not None, "pyyaml is required for structural tests")
class RenderedTemplateSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _assemble_all_templates()
        cls.docs = {name: load_cfn(body) for name, body in cls.text.items()}

    def _resources(self, name: str) -> dict:
        return self.docs[name].get("Resources", {})

    def test_imdsv2_is_required_on_every_launch_template(self) -> None:
        for name in self.docs:
            with self.subTest(template=name):
                lts = [
                    r
                    for r in self._resources(name).values()
                    if r.get("Type") == "AWS::EC2::LaunchTemplate"
                ]
                self.assertTrue(lts, "no LaunchTemplate found")
                for lt in lts:
                    meta = (
                        lt["Properties"]["LaunchTemplateData"].get("MetadataOptions", {})
                    )
                    self.assertEqual(meta.get("HttpTokens"), "required")

    @staticmethod
    def _expand_if_rules(rules: list) -> list[dict]:
        # Public's NLB ingress wraps the Browser port in
        # !If [UsePublicTLS, {..7473..}, {..7474..}]; evaluate both branches
        # so the CIDR/port assertions see the concrete rule dicts.
        out: list[dict] = []
        for rule in rules:
            if isinstance(rule, dict) and set(rule) == {"Fn::If"}:
                _cond, t_branch, f_branch = rule["Fn::If"]
                for branch in (t_branch, f_branch):
                    if isinstance(branch, dict):
                        out.append(branch)
            elif isinstance(rule, dict):
                out.append(rule)
        return out

    def test_security_groups_only_expose_neo4j_ports_to_allowed_cidr(self) -> None:
        # TLS: Browser moved 7474->7473. Private/ExistingVpc expose 7473+7687;
        # Public exposes 7473 (TLS branch), 7474 (non-TLS branch), and 7687.
        expected_cidr_ports = {
            "private": {7473, 7687},
            "existing_vpc": {7473, 7687},
            "public": {7473, 7474, 7687},
        }
        for name in self.docs:
            with self.subTest(template=name):
                cidr_exposed_ports = set()
                for rname, resource in self._resources(name).items():
                    for rule in self._expand_if_rules(
                        collect_ingress_rules(resource)
                    ):
                        from_port = rule.get("FromPort")
                        to_port = rule.get("ToPort", from_port)
                        if isinstance(from_port, int):
                            self.assertFalse(
                                from_port <= 22 <= to_port,
                                f"{name}:{rname} exposes port 22",
                            )
                        if "CidrIp" in rule:
                            self.assertEqual(
                                rule["CidrIp"],
                                {"Ref": "AllowedCIDR"},
                                f"{name}:{rname} CidrIp is not Ref AllowedCIDR",
                            )
                            self.assertIn(
                                from_port,
                                (7473, 7474, 7687),
                                f"{name}:{rname} opens unexpected port to a CIDR",
                            )
                            cidr_exposed_ports.add(from_port)
                        else:
                            self.assertIn(
                                "SourceSecurityGroupId",
                                rule,
                                f"{name}:{rname} ingress has neither CidrIp "
                                "nor SourceSecurityGroupId",
                            )
                # Non-vacuity guard: every template must expose exactly the
                # expected Neo4j ports to AllowedCIDR, so the positive path
                # above is genuinely exercised and no extra port leaks.
                self.assertEqual(
                    cidr_exposed_ports,
                    expected_cidr_ports[name],
                    f"{name}: unexpected CIDR-exposed port set",
                )

    def test_iam_policies_are_least_privilege(self) -> None:
        for name in self.docs:
            with self.subTest(template=name):
                roles = [
                    r
                    for r in self._resources(name).values()
                    if r.get("Type") == "AWS::IAM::Role"
                ]
                self.assertTrue(roles)
                for role in roles:
                    for policy in role["Properties"].get("Policies", []):
                        statements = policy["PolicyDocument"]["Statement"]
                        for stmt in flatten_policy_statements(statements):
                            actions = stmt.get("Action", [])
                            if isinstance(actions, str):
                                actions = [actions]
                            for action in actions:
                                self.assertNotEqual(
                                    action, "*", f"{name}: wildcard Action"
                                )
                                self.assertIsNone(
                                    _SERVICE_WILDCARD_RE.match(action),
                                    f"{name}: service-wide wildcard {action}",
                                )
                            resource = stmt.get("Resource")
                            if resource == "*":
                                offenders = [
                                    a
                                    for a in actions
                                    if not _allowed_on_wildcard_resource(a)
                                ]
                                self.assertFalse(
                                    offenders,
                                    f"{name}: Resource '*' with non-readonly "
                                    f"actions {offenders}",
                                )
                            if "secretsmanager:GetSecretValue" in actions:
                                self.assertIsInstance(
                                    resource,
                                    dict,
                                    f"{name}: secret grant not scoped to a Ref",
                                )
                                self.assertIn("Ref", resource)
                                self.assertNotEqual(resource.get("Ref"), "*")

    def test_conditional_secret_grants_stay_wrapped_in_conditions(self) -> None:
        for name in self.docs:
            with self.subTest(template=name):
                role = next(
                    r
                    for rn, r in self._resources(name).items()
                    if rn == "Neo4jRole"
                )
                statements = role["Properties"]["Policies"][0]["PolicyDocument"][
                    "Statement"
                ]
                if_conditions = {
                    item["Fn::If"][0]
                    for item in statements
                    if isinstance(item, dict) and set(item) == {"Fn::If"}
                }
                # BoltTLSEnabled was removed with the Secrets-Manager-PEM Bolt
                # grant (TLS now terminates ACM at the NLB). Only the Bloom/GDS
                # licence-secret grants remain conditional.
                for expected in (
                    "BloomEnabledAndLicensed",
                    "GdsEnabledAndLicensed",
                ):
                    self.assertIn(expected, if_conditions)
                self.assertNotIn("BoltTLSEnabled", if_conditions)
                for item in statements:
                    if isinstance(item, dict) and set(item) == {"Fn::If"}:
                        self.assertEqual(
                            item["Fn::If"][2],
                            {"Ref": "AWS::NoValue"},
                            f"{name}: conditional grant lacks NoValue false-branch",
                        )

    def test_cypher_ip_blocklist_invariant_in_all_rendered_templates(self) -> None:
        for name, body in self.text.items():
            with self.subTest(template=name):
                # Owned by the embedded neo4j-base.conf block, not an
                # inline set_neo4j_conf call (NFR-1/NFR-2).
                marker = "internal.dbms.cypher_ip_blocklist="
                self.assertEqual(body.count(marker), 1, name)
                line = next(
                    ln for ln in body.splitlines() if marker in ln
                )
                for cidr in REQUIRED_BLOCKLIST_CIDRS:
                    self.assertIn(cidr, line)


@unittest.skipUnless(yaml is not None, "pyyaml is required for structural tests")
class RenderedTemplateReferenceIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.docs = {
            name: load_cfn(body)
            for name, body in _assemble_all_templates().items()
        }

    def _check(self, name: str, doc: dict) -> None:
        params = set(doc.get("Parameters", {}))
        resources = set(doc.get("Resources", {}))
        conditions = set(doc.get("Conditions", {}))
        mappings = set(doc.get("Mappings", {}))
        ref_targets = params | resources | PSEUDO_PARAMETERS

        def check_sub(value) -> None:
            if isinstance(value, list):
                template_str = value[0]
                localvars = set(value[1]) if len(value) > 1 else set()
            else:
                template_str, localvars = value, set()
            for token in _SUB_VAR_RE.findall(template_str):
                token = token.strip()
                if token.startswith("!"):
                    continue
                head = token.split(".")[0]
                if "." in token:
                    self.assertIn(
                        head,
                        resources,
                        f"{name}: Fn::Sub GetAtt on unknown resource {head}",
                    )
                else:
                    self.assertTrue(
                        token in ref_targets or token in localvars,
                        f"{name}: Fn::Sub references unknown {token}",
                    )

        for node in iter_nodes(doc):
            keys = set(node)
            if keys == {"Ref"}:
                target = node["Ref"]
                self.assertIn(
                    target, ref_targets, f"{name}: dangling Ref {target}"
                )
            if keys == {"Condition"} and isinstance(node["Condition"], str):
                self.assertIn(
                    node["Condition"],
                    conditions,
                    f"{name}: unknown Condition {node['Condition']}",
                )
            if "Fn::GetAtt" in node:
                v = node["Fn::GetAtt"]
                res = v.split(".")[0] if isinstance(v, str) else v[0]
                self.assertIn(
                    res, resources, f"{name}: GetAtt unknown resource {res}"
                )
            if "Fn::If" in node:
                cond = node["Fn::If"][0]
                self.assertIn(
                    cond, conditions, f"{name}: Fn::If unknown condition {cond}"
                )
            if "Fn::Sub" in node:
                check_sub(node["Fn::Sub"])
            if "Fn::FindInMap" in node:
                m = node["Fn::FindInMap"][0]
                if isinstance(m, str):
                    self.assertIn(
                        m, mappings, f"{name}: FindInMap unknown map {m}"
                    )

        for section in ("Resources", "Outputs"):
            for rname, entry in doc.get(section, {}).items():
                cond = entry.get("Condition")
                if isinstance(cond, str):
                    self.assertIn(
                        cond,
                        conditions,
                        f"{name}: {rname} unknown Condition {cond}",
                    )
                depends = entry.get("DependsOn")
                if depends is not None:
                    deps = [depends] if isinstance(depends, str) else depends
                    for dep in deps:
                        self.assertIn(
                            dep,
                            resources,
                            f"{name}: {rname} DependsOn unknown {dep}",
                        )

    def test_templates_parse_and_have_no_dangling_references(self) -> None:
        for name, doc in self.docs.items():
            with self.subTest(template=name):
                self.assertIn("Resources", doc)
                self._check(name, doc)


@unittest.skipUnless(yaml is not None, "pyyaml is required for structural tests")
class NlbAndRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _assemble_all_templates()
        cls.docs = {
            name: load_cfn(body) for name, body in cls.text.items()
        }

    def _by_type(self, name: str, rtype: str) -> dict:
        return {
            rn: r
            for rn, r in self.docs[name]["Resources"].items()
            if r.get("Type") == rtype
        }

    def test_private_existing_listeners_and_tgs_are_tls(self) -> None:
        # TLS is mandatory for Private/ExistingVpc: Browser 7473 and Bolt 7687
        # listeners are Protocol TLS with the ACM cert + modern SslPolicy, and
        # both target groups are Protocol TLS. No plain 7474 anywhere.
        for name in ("private", "existing_vpc"):
            with self.subTest(template=name):
                listeners = self._by_type(
                    name, "AWS::ElasticLoadBalancingV2::Listener"
                )
                ports = {}
                for r in listeners.values():
                    p = r["Properties"]
                    ports[p["Port"]] = p
                    self.assertEqual(p["Protocol"], "TLS")
                    self.assertEqual(
                        p["Certificates"],
                        [{"CertificateArn": {"Ref": "CertificateArn"}}],
                    )
                    self.assertEqual(
                        p["SslPolicy"], "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09"
                    )
                self.assertEqual(set(ports), {7473, 7687})

                tgs = self._by_type(
                    name, "AWS::ElasticLoadBalancingV2::TargetGroup"
                )
                tg_ports = set()
                for r in tgs.values():
                    p = r["Properties"]
                    self.assertEqual(p["Protocol"], "TLS")
                    tg_ports.add(p["Port"])
                    # Both target groups use a TCP health check. 7473 cannot
                    # use HTTPS: the NLB health checker sends no SNI and
                    # Jetty's sniHostCheck answers GET / with 400 Invalid
                    # SNI, so the target would never go healthy (kill loop
                    # under HealthCheckType=ELB). 7687 Bolt is binary.
                    self.assertEqual(p["HealthCheckProtocol"], "TCP")
                    self.assertNotIn("HealthCheckPath", p)
                    self.assertNotIn("Matcher", p)
                self.assertEqual(tg_ports, {7473, 7687})
                # No plain-HTTP listener/target group: the {7473,7687} port
                # sets above already prove 7474 is absent from the data path
                # (test_security_groups_* covers the SG side).

    def test_public_listeners_are_tls_conditional(self) -> None:
        # Public TLS is opt-in: the Browser listener Port/Protocol and the
        # Certificates/SslPolicy are gated on the UsePublicTLS condition.
        listeners = self._by_type(
            "public", "AWS::ElasticLoadBalancingV2::Listener"
        )
        browser = next(
            r
            for r in listeners.values()
            if r["Properties"]["Port"]
            == {"Fn::If": ["UsePublicTLS", 7473, 7474]}
        )
        bp = browser["Properties"]
        self.assertEqual(
            bp["Protocol"], {"Fn::If": ["UsePublicTLS", "TLS", "TCP"]}
        )
        self.assertEqual(bp["Certificates"]["Fn::If"][0], "UsePublicTLS")
        self.assertEqual(bp["SslPolicy"]["Fn::If"][0], "UsePublicTLS")
        bolt = next(
            r for r in listeners.values() if r["Properties"]["Port"] == 7687
        )
        self.assertEqual(
            bolt["Properties"]["Protocol"],
            {"Fn::If": ["UsePublicTLS", "TLS", "TCP"]},
        )

    def test_route53_private_dns_is_conditional_and_private_only(self) -> None:
        for name in ("private", "existing_vpc"):
            with self.subTest(template=name):
                zones = self._by_type(name, "AWS::Route53::HostedZone")
                records = self._by_type(name, "AWS::Route53::RecordSet")
                self.assertEqual(
                    [z.get("Condition") for z in zones.values()],
                    ["CreatePrivateDnsHostedZone"],
                )
                self.assertEqual(
                    [r.get("Condition") for r in records.values()],
                    ["CreatePrivateDns"],
                )
        self.assertFalse(self._by_type("public", "AWS::Route53::HostedZone"))
        self.assertFalse(self._by_type("public", "AWS::Route53::RecordSet"))

    def test_outputs_use_tls_form(self) -> None:
        for name in ("private", "existing_vpc"):
            with self.subTest(template=name):
                outputs = self.docs[name]["Outputs"]
                self.assertIn("Neo4jSSMHTTPSCommand", outputs)
                self.assertNotIn("Neo4jSSMHTTPCommand", outputs)
                self.assertIn(
                    "portNumber=7473",
                    outputs["Neo4jSSMHTTPSCommand"]["Value"]["Fn::Sub"],
                )
                for key in ("Neo4jPrivateDnsHostedZoneId", "Neo4jPrivateDnsRecord"):
                    self.assertEqual(
                        outputs[key].get("Condition"), "CreatePrivateDns"
                    )
        # Outputs delegate to the SSM parameter so the URL expression lives in
        # exactly one place per template; the SSM contract tests pin the
        # expression itself.
        for name in ("private", "existing_vpc", "public"):
            with self.subTest(template=name):
                outputs = self.docs[name]["Outputs"]
                self.assertEqual(
                    outputs["Neo4jBoltUrl"]["Value"],
                    {"Fn::GetAtt": "Neo4jConfigBoltUrlParameter.Value"},
                )
                self.assertEqual(
                    outputs["Neo4jHttpsUrl"]["Value"],
                    {"Fn::GetAtt": "Neo4jConfigHttpsUrlParameter.Value"},
                )

    def test_nlb_scheme_matches_template_exposure(self) -> None:
        expected = {
            "private": "internal",
            "existing_vpc": "internal",
            "public": "internet-facing",
        }
        for name, scheme in expected.items():
            with self.subTest(template=name):
                lbs = [
                    r
                    for r in self.docs[name]["Resources"].values()
                    if r.get("Type")
                    == "AWS::ElasticLoadBalancingV2::LoadBalancer"
                ]
                self.assertTrue(lbs)
                for lb in lbs:
                    self.assertEqual(lb["Properties"].get("Scheme"), scheme)

    def test_private_neo4j_instance_subnets_egress_via_nat_not_igw(self) -> None:
        # The IGW default route legitimately exists for the NAT-hosting public
        # subnets. The invariant is narrower: the subnets the Neo4j ASGs launch
        # into must reach the internet only through a NAT gateway, never an IGW.
        resources = self.docs["private"]["Resources"]

        def ref(value):
            return value.get("Ref") if isinstance(value, dict) else None

        instance_subnets = set()
        for r in resources.values():
            if r.get("Type") == "AWS::AutoScaling::AutoScalingGroup":
                for entry in r["Properties"].get("VPCZoneIdentifier", []):
                    if ref(entry):
                        instance_subnets.add(ref(entry))
        self.assertTrue(instance_subnets, "no ASG instance subnets found")

        subnet_to_rt = {}
        for r in resources.values():
            if r.get("Type") == "AWS::EC2::SubnetRouteTableAssociation":
                props = r["Properties"]
                subnet_to_rt[ref(props.get("SubnetId"))] = ref(
                    props.get("RouteTableId")
                )

        default_route_target = {}
        for r in resources.values():
            if r.get("Type") == "AWS::EC2::Route":
                props = r["Properties"]
                if props.get("DestinationCidrBlock") == "0.0.0.0/0":
                    default_route_target[ref(props.get("RouteTableId"))] = props

        for subnet in instance_subnets:
            rt = subnet_to_rt.get(subnet)
            self.assertIsNotNone(
                rt, f"{subnet} has no route table association"
            )
            props = default_route_target.get(rt)
            self.assertIsNotNone(
                props, f"route table {rt} for {subnet} has no default route"
            )
            self.assertNotIn(
                "GatewayId",
                props,
                f"{subnet} default route targets an IGW",
            )
            self.assertIn("NatGatewayId", props)


class BuildVerifyTests(unittest.TestCase):
    def test_committed_templates_match_source(self) -> None:
        module = load_build_module()
        for spec in module._TEMPLATE_SPECS:
            with self.subTest(template=spec.filename):
                committed = (module.OUT / spec.filename).read_text()
                self.assertEqual(
                    module._assemble(spec),
                    committed,
                    f"{spec.filename} is stale; run templates/build.py",
                )


if __name__ == "__main__":
    unittest.main()
