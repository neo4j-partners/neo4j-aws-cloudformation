"""Shared deploy-output file helpers.

Deploy output files use a simple `Key = Value` format. Keep parsing and
resolution behavior here so deploy, validation, sample app, and test tooling do
not drift.
"""

from __future__ import annotations

from pathlib import Path


def parse_key_value_text(text: str) -> dict[str, str]:
    """Parse `Key = Value` lines into a dictionary."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def read_outputs(path: Path) -> dict[str, str]:
    """Read a deploy output file into a dictionary."""
    return parse_key_value_text(path.read_text())


def latest_outputs_file(deploy_dir: Path, pattern: str = "*.txt") -> Path | None:
    """Return the most recently modified output file matching pattern."""
    if not deploy_dir.is_dir():
        return None
    candidates = sorted(
        deploy_dir.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def resolve_outputs_file(
    deploy_dir: Path,
    stack_name: str | None,
    *,
    pattern: str = "*.txt",
) -> Path:
    """Resolve an explicit stack name or the newest deploy output file."""
    if stack_name:
        path = deploy_dir / f"{stack_name.removesuffix('.txt')}.txt"
    else:
        path = latest_outputs_file(deploy_dir, pattern) or Path()

    if path.is_file():
        return path

    if stack_name:
        raise FileNotFoundError(f"File not found: {path}")
    raise FileNotFoundError(f"No {pattern} files in {deploy_dir}")


def require_field(fields: dict[str, str], key: str, source: Path) -> str:
    """Return a required output field or raise a clear ValueError."""
    value = fields.get(key, "")
    if not value:
        raise ValueError(f"Could not read {key} from {source}.")
    return value


def truthy(value: str | None) -> bool:
    """Return whether a deploy output field uses a truthy string."""
    return (value or "").lower() in {"1", "true", "yes", "y"}


def resolve_bolt_scheme(fields: dict[str, str]) -> str:
    """Return the Bolt URI scheme implied by output fields.

    TLS is signalled by a non-empty AdvertisedDNS. Private and ExistingVpc
    always set it. Public sets it only with --enable-public-tls.

    With TLS on, the scheme uses ``+ssc``. It encrypts but verifies neither
    the chain nor the hostname, so it works through a tunnel whatever
    certificate the NLB presents. It also connects to the NLB DNS name
    whatever the certificate SAN says. admin-shell uses this scheme. The
    validate-private driver tools use it only when the stack's bolt-url
    scheme is neo4j+ssc. On a neo4j+s stack they keep the base scheme and
    verify the chain against /neo4j-ee/<stack>/ca-pem. They skip the
    hostname check because the tunnel replaces the server name.
    """
    base = "bolt" if fields.get("NumberOfServers", "3") == "1" else "neo4j"
    if fields.get("AdvertisedDNS", ""):
        return f"{base}+ssc"
    return base


def resolve_direct_bolt_scheme(fields: dict[str, str]) -> str:
    """Return the non-routing Bolt scheme for a local port-forward.

    A routed ``neo4j`` scheme makes the driver follow the routing table to the
    NLB's private DNS name, which a laptop cannot reach. A direct ``bolt``
    connection stays on the forwarded port, but it sends no routing context,
    so server-side routing does not apply. A direct connection is safe for
    reads only. A write that lands on a follower fails with NotALeader, so
    writes need the routed scheme.

    With TLS on, the client connects to AdvertisedDNS mapped to 127.0.0.1 in
    /etc/hosts, so the certificate SAN matches. The scheme is ``bolt+s``,
    which verifies the chain and the hostname. A record with
    AutoImportedCertificateArn means deploy.py imported a self-signed
    certificate, which no system trust store holds, so the scheme is
    ``bolt+ssc``. Without AdvertisedDNS the stack serves plain Bolt.
    """
    if not fields.get("AdvertisedDNS", ""):
        return "bolt"
    if fields.get("AutoImportedCertificateArn", ""):
        return "bolt+ssc"
    return "bolt+s"


def require_private_mode(fields: dict[str, str]) -> None:
    """Validate that output fields describe a private EE deployment."""
    mode = fields.get("DeploymentMode", "Public")
    if mode not in {"Private", "ExistingVpc"}:
        stack_name = fields.get("StackName", "unknown")
        raise ValueError(
            "This command requires a Private or ExistingVpc stack. "
            f"Stack '{stack_name}' has DeploymentMode={mode}."
        )
