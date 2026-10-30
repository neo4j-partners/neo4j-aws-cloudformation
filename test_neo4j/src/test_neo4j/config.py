"""Parse deploy output files and expose configuration as a typed dataclass."""

from __future__ import annotations

import contextlib
import dataclasses
import getpass
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

from neo4j import Driver, GraphDatabase

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "neo4j-ee" / "src"))
from neo4j_ee.outputs import read_outputs, truthy  # noqa: E402


_REQUIRED_FIELDS_CE = (
    "Neo4jBrowserURL",
    "Neo4jURI",
    "Username",
    "StackName",
    "Region",
)

_REQUIRED_FIELDS_EE_PUBLIC = (
    "Neo4jBrowserURL",
    "Neo4jURI",
    "Username",
    "StackName",
    "Region",
)

_REQUIRED_FIELDS_COMMON = (
    "Username",
    "StackName",
    "Region",
)


@dataclasses.dataclass(frozen=True)
class StackConfig:
    """Immutable configuration parsed from deploy.sh output (.deploy/<stack>.txt)."""

    browser_url: str  # e.g. http://<host>:7474
    neo4j_uri: str    # e.g. neo4j://<host>:7687 or neo4j+ssc://<host>:7687
    username: str
    password: str
    stack_name: str
    region: str
    install_apoc: bool
    host: str         # bare hostname extracted from browser_url
    edition: str      # "ce" or "ee"
    number_of_servers: int  # 1 or 3 for EE
    # Set when deploy.py passed a licence ARN via --bloom-license-secret-id /
    # --gds-license-secret-id; absence means the licence checks are skipped.
    bloom_licensed: bool
    gds_licensed: bool
    # bloom_expected mirrors the InstallBloom template parameter recorded by
    # deploy.py. The install gate is `InstallBloom=true AND BloomLicenseSecretArn`
    # non-empty; license-file conf-key and license-on-disk checks key on both
    # bloom_expected and bloom_licensed.
    bloom_expected: bool
    # gds_expected mirrors the InstallGDS template parameter recorded by
    # deploy.py; the GDS plugin install is gated on this in the UserData.
    gds_expected: bool
    # bolt_tls_enabled is True when the stack terminates TLS at the NLB,
    # signalled by a non-empty AdvertisedDNS output. When True the runner uses
    # the +ssc Bolt scheme so the Neo4j driver tolerates the self-signed cert.
    bolt_tls_enabled: bool
    # AMI used by the running cluster instances; "local" when built by
    # marketplace/create-ami.sh (Marketplace or iteration mode), "marketplace"
    # when sourced from the live Marketplace listing via --marketplace.
    ami_id: str
    ami_source: str

    @contextlib.contextmanager
    def driver(self) -> Iterator[Driver]:
        """Yield a Neo4j driver connected with this config's credentials."""
        drv = GraphDatabase.driver(
            self.neo4j_uri,
            auth=(self.username, self.password),
        )
        try:
            yield drv
        finally:
            drv.close()


def load_config(
    outputs_path: Path,
    password_override: str | None = None,
) -> StackConfig:
    if not outputs_path.exists():
        raise FileNotFoundError(
            f"{outputs_path} not found. Run deploy.sh first to create a stack."
        )

    fields = read_outputs(outputs_path)

    edition = fields.get("Edition", "ce").lower()
    number_of_servers = int(fields.get("NumberOfServers", "1"))

    missing = [f for f in _REQUIRED_FIELDS_COMMON if f not in fields]
    if missing:
        raise ValueError(
            f"Required field(s) missing from {outputs_path.name}: {', '.join(missing)}"
        )

    # EE publishes Neo4jHttpsUrl / Neo4jBoltUrl; CE still publishes the older
    # Neo4jBrowserURL / Neo4jURI names.
    browser_key = "Neo4jHttpsUrl" if edition == "ee" else "Neo4jBrowserURL"
    uri_key = "Neo4jBoltUrl" if edition == "ee" else "Neo4jURI"
    if browser_key not in fields or uri_key not in fields:
        if edition == "ee":
            raise ValueError(
                "This test runner only supports public EE stacks (internet-facing NLB). "
                f"The deploy output is missing {browser_key} and {uri_key}. "
                "Private EE stacks require SSM tunneling and are not supported here."
            )
        raise ValueError(
            f"Required field(s) missing from {outputs_path.name}: "
            + ", ".join(f for f in (browser_key, uri_key) if f not in fields)
        )

    # Private and ExistingVpc EE stacks publish the same URL keys, so the
    # deployment mode is the only thing that keeps them out of this runner.
    if edition == "ee" and fields.get("DeploymentMode", "Public") != "Public":
        raise ValueError(
            "This test runner only supports public EE stacks (internet-facing NLB). "
            f"Stack {fields['StackName']!r} has "
            f"DeploymentMode={fields['DeploymentMode']}; private EE stacks "
            "require SSM tunneling and are not supported here."
        )

    browser_url = fields[browser_key]
    neo4j_uri = fields[uri_key]
    host = urlparse(browser_url).hostname or browser_url

    install_apoc = fields.get("InstallAPOC", "no").lower() == "yes"

    # TLS is signalled by a non-empty AdvertisedDNS (the NLB terminates TLS and
    # the stack enforces server.bolt.tls_level=REQUIRED), so the runner uses the
    # +ssc scheme to tolerate the self-signed cert imported at deploy time. This
    # mirrors neo4j_ee.outputs.resolve_bolt_scheme. Test-only: +ssc skips
    # certificate verification, so do not copy it into production clients.
    bolt_tls_enabled = bool(fields.get("AdvertisedDNS"))
    if bolt_tls_enabled and neo4j_uri.startswith("neo4j://"):
        neo4j_uri = "neo4j+ssc://" + neo4j_uri[len("neo4j://"):]

    password = (
        password_override
        if password_override is not None
        else fields.get("Password", "")
    )
    if not password and sys.stdin.isatty():
        password = getpass.getpass("Enter neo4j password: ")
    if not password:
        raise ValueError(
            "No password available. Provide --password or ensure Password is in "
            f"{outputs_path.name}."
        )

    return StackConfig(
        browser_url=browser_url,
        neo4j_uri=neo4j_uri,
        username=fields["Username"],
        password=password,
        stack_name=fields["StackName"],
        region=fields["Region"],
        install_apoc=install_apoc,
        host=host,
        edition=edition,
        number_of_servers=number_of_servers,
        bloom_licensed=bool(fields.get("BloomLicenseSecretArn")),
        gds_licensed=bool(fields.get("GdsLicenseSecretArn")),
        bloom_expected=truthy(fields.get("BloomExpected", fields.get("InstallBloom"))),
        gds_expected=truthy(fields.get("InstallGDS")),
        bolt_tls_enabled=bolt_tls_enabled,
        ami_id=fields.get("AmiId", ""),
        ami_source=fields.get("AmiSource", ""),
    )
