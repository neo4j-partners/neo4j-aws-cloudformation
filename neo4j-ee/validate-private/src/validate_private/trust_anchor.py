"""Read the stack's published certificate trust anchor and verify TLS with it.

Every node publishes the NLB certificate chain to /neo4j-ee/<stack>/ca-pem
before it signals CloudFormation. Until then the parameter holds the
placeholder "pending". Any value that does not start with a PEM certificate
header means the anchor is not published, and the tools refuse to connect.
"""

from __future__ import annotations

import socket
import ssl

import boto3
from botocore.exceptions import BotoCoreError, ClientError

PEM_PREFIX = "-----BEGIN CERTIFICATE-----"


class TrustAnchorError(ValueError):
    pass


def read_parameter(name: str, region: str) -> str:
    ssm = boto3.client("ssm", region_name=region)
    return ssm.get_parameter(Name=name)["Parameter"]["Value"]


def read_ca_pem(stack_name: str, region: str) -> str:
    """Return the published trust anchor, or raise TrustAnchorError."""
    name = f"/neo4j-ee/{stack_name}/ca-pem"
    try:
        value = read_parameter(name, region)
    except (BotoCoreError, ClientError) as exc:
        raise TrustAnchorError(
            f"Could not read {name}. Your AWS credentials need "
            f"ssm:GetParameter on it. {exc}"
        ) from exc
    if not value.startswith(PEM_PREFIX):
        raise TrustAnchorError(
            f"{name} holds no PEM certificate. It starts with {value[:20]!r}. "
            "No node has published the trust anchor, so the tools refuse "
            "to connect."
        )
    return value


def ssl_context(ca_pem: str) -> ssl.SSLContext:
    """Return a client context that trusts only the published anchor.

    The chain is verified. The hostname is not, because the SSM tunnel
    replaces the server name with 127.0.0.1. The context is built directly
    instead of with create_default_context, whose VERIFY_X509_STRICT flag on
    Python 3.13 can reject a self-signed leaf. VERIFY_X509_PARTIAL_CHAIN lets
    an intermediate in the published chain act as the anchor, as it does in
    a Java truststore.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.load_verify_locations(cadata=ca_pem)
    return context


def verify_handshake(ca_pem: str, port: int, timeout_s: float = 15) -> str:
    """Complete a TLS handshake on 127.0.0.1:port and return the TLS version.

    A certificate the anchor does not verify raises ssl.SSLCertVerificationError.
    """
    context = ssl_context(ca_pem)
    with socket.create_connection(("127.0.0.1", port), timeout=timeout_s) as sock:
        with context.wrap_socket(sock) as tls:
            return tls.version() or "unknown"
